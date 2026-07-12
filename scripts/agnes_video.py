#!/usr/bin/env python3
"""Create, poll, and download Agnes-Video-V2.0 tasks.

v3.0 重构（2026-06-06）：Agent 视角 + 新 API 文档
- P0-1: 支持新 API 推荐的 video_id 查询（GET /agnesapi?video_id=...），保留 task_id 兼容
- P0-2: 多 Key 轮换（AGNES_API_KEY=key1,key2 逗号分隔；1 key 也兼容）
- P0-3: 指数退避重试（5xx/超时/网络重试 3 次，429/配额等更久）
- P1-1: --format {agent|json|human}（默认 agent，结构化 STATUS/PATH/URL/VIDEO_ID/...）
- P1-2: agent 模式错误走 stdout（agent 一定看得到）
- P1-3: 智能识别 video URL（video_url / remixed_from_video_id / 嵌套 data 字段）
- P2-1: urllib 改用 lib/http_client.curl_request（沙箱兼容）
- P2-2: mode 字段：图生默认 mode=ti2vid；多图无 mode；关键帧 extra_body.mode=keyframes
- P2-3: num_frames 校验更友好（提示合法值列表）
- P2-4: --output 单文件路径支持（除 --output-dir 之外）
- P2-5: --max-retries 可调
- P2-6: 超时默认 1800s（队列积压考虑）；轮询默认 5s（按新 API 文档推荐）
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Optional
from urllib import parse

# v3.0: 共享 curl 客户端（绕开 urllib 沙箱卡死）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from http_client import curl_request, download_file  # noqa: E402
# v3.2.0: Key 池（粘性 + 冷却轮换，绕开 RPM=1 限制）
from key_pool import KeyPool, _key_fingerprint, DEFAULT_COOLDOWN_SEC  # noqa: E402


API_BASE = os.environ.get("AGNES_API_BASE", "https://apihub.agnes-ai.com").rstrip("/")
MODEL = "agnes-video-v2.0"
DEFAULT_TIMEOUT = 1800  # 30 分钟（v2 队列积压 15+ 分钟的实际考虑）
DEFAULT_POLL_INTERVAL = 5  # 新 API 文档推荐 5s
MAX_RETRIES = 3
BASE_BACKOFF_SEC = 1.5
QUOTA_BACKOFF_SEC = 10

# 任务状态分类
RUNNING_STATES = {"queued", "in_progress", "processing", "submitted", "pending"}
DONE_STATES = {"completed", "succeeded", "success"}
FAILED_STATES = {"failed", "cancelled", "canceled", "error"}

# 视为可重试的 HTTP 状态码
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}

# num_frames 合法值（8n+1, n=0..55，覆盖 1..441）
VALID_NUM_FRAMES = [n for n in range(1, 442) if (n - 1) % 8 == 0]

# keyframes 模式最少图数（API 文档：首帧 + 尾帧）
KEYFRAMES_MIN_IMAGES = 2

# 一次请求最大图片数（API 兜底，避免发 8+ 张图被拒）
MAX_IMAGES_PER_REQUEST = 8

# 视频文件默认输出目录（遵循 TOOLS.md 全局默认输出规则）
DEFAULT_OUTPUT_DIR = "/home/goron/文档/Openclaw/输出/agnes-free-video"

# video_id 端点连续 404 上限：超过则放弃 fallback（避免 P0-C 死循环到 timeout）
VIDEO_ID_404_LIMIT = 2

# v3.1.2: Key 配置文件路径（提取成模块常量，方便测试 mock）
SKILL_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def XDG_ENV_PATH() -> Path:
    """v3.1.2: 用函数而不是常量，让测试改 HOME 后能拿到新路径

    也避免模块加载时 freeze 一个固定路径（用户改 HOME 后不会跟变）
    """
    return Path.home() / ".config" / "openclaw" / "agnes-free-video.env"


# ============================================================================
# v3.0: 错误 + Key 池 + 重试
# ============================================================================

class ApiError(RuntimeError):
    def __init__(self, message: str, status: Optional[int] = None, payload: Any = None):
        super().__init__(message)
        self.status = status
        self.payload = payload


class KeyDeadError(ApiError):
    """v3.2.0: 401/403 鉴权失败 → 上层应该换 key（pool.mark_dead + 重新 acquire）"""
    pass


# v3.2.0: KeyPool 单例（cmd_create / cmd_status 共用，让 polling 跟 create 用同一把 key）
# 单进程内 4 把 key 跨 create 任务均匀轮换
_KEY_POOL: Optional[KeyPool] = None


def get_key_pool(keys: list[str]) -> KeyPool:
    """获取（或懒初始化）KeyPool 单例

    v3.2.0 设计：
    - 单进程内一个 pool，所有 create/status 共享状态
    - 第一次调用时根据 keys 列表建 pool，后续调用忽略（keys 一致）
    - 这样跨 cmd_create 多次调用，4 把 key 真正轮换均匀
    """
    global _KEY_POOL
    if _KEY_POOL is None:
        _KEY_POOL = KeyPool(keys=keys)
    return _KEY_POOL


def reset_key_pool() -> None:
    """测试用：清空 pool 单例"""
    global _KEY_POOL
    _KEY_POOL = None


def get_api_keys() -> list[str]:
    """从多源读取 Key 池（按优先级，自动合并去重）

    优先级（高 → 低）：
    1. 环境变量 AGNES_API_KEY / AGNES_TOKEN（命令行 export）
    2. XDG 全局配置文件 ~/.config/openclaw/agnes-free-video.env（推荐，
       不随 skill 目录被 git/sync/backup 一起带走）
    3. skill 本地 .env 文件（向后兼容老用户）

    多 key 逗号分隔（key1,key2,key3），每源内去重保序，跨源也去重。

    v3.1.2 改动：
    - P0 安全：加 XDG 全局路径支持，给主人"把真 key 放到不被 skill 目录带走的地方"的选项
    - 优先级清晰（env > XDG > skill），向后兼容（老用户 .env 仍生效）
    - 缺 key 时改 raise ApiError（不再 raise SystemExit），让 main() 统一处理 agent 格式输出
    """
    raw_keys: list[str] = []

    # 优先级 1：环境变量
    for env_var in ("AGNES_API_KEY", "AGNES_TOKEN"):
        raw = os.environ.get(env_var) or ""
        raw_keys.extend(k.strip() for k in raw.split(",") if k.strip())

    # 优先级 2：XDG 全局配置（推荐放真 key）
    xdg_env = XDG_ENV_PATH()
    if xdg_env.is_file():
        try:
            for line in xdg_env.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith(("AGNES_API_KEY=", "AGNES_TOKEN=")):
                    val = line.split("=", 1)[1].strip()
                    val = val.strip('"').strip("'")  # 支持引号包裹
                    raw_keys.extend(k.strip() for k in val.split(",") if k.strip())
        except OSError:
            # XDG 文件读不到就跳过，不阻断（让优先级 3 兜底）
            pass

    # 优先级 3：skill 本地 .env（向后兼容）
    skill_env = SKILL_ENV_PATH
    if skill_env.is_file():
        try:
            for line in skill_env.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith(("AGNES_API_KEY=", "AGNES_TOKEN=")):
                    val = line.split("=", 1)[1].strip()
                    val = val.strip('"').strip("'")
                    raw_keys.extend(k.strip() for k in val.split(",") if k.strip())
        except OSError:
            pass

    # 去重保序
    seen: set = set()
    deduped: list = []
    for k in raw_keys:
        if k and k not in seen:
            seen.add(k)
            deduped.append(k)
    if not deduped:
        raise ApiError(
            "Missing API key. Set AGNES_API_KEY environment variable, or create "
            "~/.config/openclaw/agnes-free-video.env (recommended), or skill-local .env. "
            "Examples:\n"
            "  export AGNES_API_KEY='sk-xxx'\n"
            "  # or multiple (comma-separated):\n"
            "  export AGNES_API_KEY='sk-a,sk-b,sk-c'"
        )
    return deduped


def is_quota_error(body: str, status: Optional[int]) -> bool:
    """判断响应正文是否表示配额/限流耗尽

    v3.1.2 修复（关键词误报问题）：
    - 删 "今天"、"建议您"——这两个中文常用词在正常 API 响应里 100% 出现
      （如"今天任务创建成功"、"建议您稍后重试"），触发误报把正常响应误判为配额错误
    - 改用强相关词组（quota/balance/credit + exhausted/exceeded/reached/insufficient）
      才能匹配，避免任何单字常用词误报
    """
    if not body:
        return False
    body_lower = body.lower()
    # 强相关词组（必须包含"配额/额度"语义 + "耗尽"语义）
    quota_phrases = [
        "quota exhausted", "quota exceeded", "quota reached",
        "rate limit exceeded", "rate_limit_exceeded",
        "insufficient quota", "insufficient balance", "insufficient credit",
        "out of credits", "out of quota", "credit exhausted",
        "balance insufficient", "limit reached",
        "次数已用完", "额度已用完", "余额不足", "配额不足", "已达上限",
        "已超出限额", "已达今日上限", "今日配额已用完",
    ]
    return any(kw in body_lower for kw in quota_phrases)


def is_retryable_status(status: int) -> bool:
    """判断 HTTP 状态码是否值得重试"""
    if status == 0:
        return True
    return status in RETRYABLE_STATUS


def _try_parse_json(body: str) -> Any:
    if not body:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


def _extract_err_msg(payload: Any) -> Optional[str]:
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("type") or err)
        if isinstance(err, str):
            return err
        if payload.get("message"):
            return str(payload["message"])
    return None


def request_json_with_retry(
    method: str,
    url: str,
    key: str,
    payload: Optional[dict] = None,
    max_retries: int = MAX_RETRIES,
    timeout: int = 180,
) -> dict:
    """v3.2.0: 单 key 重试（粘性）+ 鉴权失败抛 KeyDeadError 让上层换 key

    策略变更（对比 v3.1）：
    - **入参从 `keys: list` 改成 `key: str`**：本请求全程只用这一把 key
    - 5xx/429/网络错误：指数退避（1.5s, 3s, 6s），还在同一把 key 上重试
    - 401/403 auth 错（**配额类** is_quota_error=True）：抛 KeyDeadError
      → 上层 pool.mark_dead(key) + 重新 acquire_key() 换下一把
    - 401/403 非配额（真鉴权错）：也抛 KeyDeadError（同样换 key）
    - 4xx 业务错误（400/404/422...）：直接抛 ApiError，不重试不换 key
    - 429 限流：**不换 key**（换 key 也撞 RPM=1），靠退避等到冷却完

    退避重试轮数（max_retries 默认 3）：
    - 5xx/429/网络：1.5s → 3s → 6s 退避
    - 429 额外 +10s（v3.0 沿用）

    与 KeyPool 配合（典型用法）：
        pool = get_key_pool(keys)
        for _ in range(3):  # 最多换 3 把 key
            try:
                key = pool.acquire_key()
                response = request_json_with_retry(method, url, key)
                pool.mark_used(key)
                return response
            except KeyDeadError:
                pool.mark_dead(key)
                continue
        raise ApiError("All keys returned 401/403 after 3 retries")
    """
    data_str: Optional[str] = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        data_str = json.dumps(payload, ensure_ascii=False)

    last_error: Optional[ApiError] = None
    key_fp = _key_fingerprint(key)

    for attempt in range(max_retries):
        headers["Authorization"] = f"Bearer {key}"
        try:
            code, body = curl_request(
                url=url,
                method=method,
                headers=headers,
                data=data_str,
                timeout=timeout,
            )
        except Exception as e:
            last_error = ApiError(f"Network error: {e}")
            print(
                f"# [retry] {key_fp} attempt {attempt + 1}/{max_retries} "
                f"network error: {e}, retrying same key...",
                file=sys.stderr,
            )
            # 网络错误不换 key，退避后重试
            if attempt < max_retries - 1:
                time.sleep(BASE_BACKOFF_SEC * (2 ** attempt))
            continue

        # 成功
        if 200 <= code < 300:
            parsed = _try_parse_json(body)
            if parsed is None:
                raise ApiError(f"Expected JSON object, got: {body[:300]}")
            if not isinstance(parsed, dict):
                raise ApiError(f"Expected JSON object, got: {body[:300]}")
            if parsed.get("error"):
                raise ApiError(
                    _extract_err_msg(parsed) or "API returned an error",
                    payload=parsed,
                )
            return parsed

        # 业务错误（4xx，非 429）
        if 400 <= code < 500 and code not in RETRYABLE_STATUS:
            if code in (401, 403):
                # 鉴权 / 配额失败 → 抛 KeyDeadError 让上层换 key
                quota = is_quota_error(body, code)
                kind = "quota" if quota else "auth"
                print(
                    f"# [retry] {key_fp} attempt {attempt + 1}/{max_retries} "
                    f"got {kind} error (status={code}), signal to swap key",
                    file=sys.stderr,
                )
                raise KeyDeadError(
                    f"Agnes API {kind} failed with {key_fp} (HTTP {code}): {body[:200]}",
                    status=code,
                )
            # 其他 4xx 业务错误：直接报错，不重试不换 key
            parsed = _try_parse_json(body)
            msg = _extract_err_msg(parsed) if isinstance(parsed, dict) else None
            raise ApiError(
                msg or f"HTTP {code}: {body[:200]}",
                status=code,
                payload=parsed,
            )

        # 可重试错误（5xx / 429 / 网络 code=0）
        last_error = ApiError(
            f"HTTP {code}: {body[:200]}" if body else f"Network error (http_code={code})",
            status=code,
        )
        retry_kind = "quota" if (code == 429 or is_quota_error(body, code)) else "transient"
        print(
            f"# [retry] {key_fp} attempt {attempt + 1}/{max_retries} "
            f"got {retry_kind} error (status={code}), retrying same key (don't swap - "
            f"next key also has RPM=1 cooldown)",
            file=sys.stderr,
        )
        if attempt < max_retries - 1:
            wait = BASE_BACKOFF_SEC * (2 ** attempt)
            if last_error and is_quota_error(str(last_error), last_error.status):
                wait = max(wait, QUOTA_BACKOFF_SEC)
            time.sleep(wait)

    raise last_error or ApiError("All retries exhausted")


# ============================================================================
# v3.0: 参数校验 + payload 构建
# ============================================================================

def validate_num_frames(value: int) -> None:
    if value > 441 or value < 1 or (value - 1) % 8 != 0:
        valid_str = ", ".join(str(n) for n in VALID_NUM_FRAMES if n <= 441)
        raise SystemExit(
            f"num_frames must be <= 441 and satisfy 8n + 1.\n"
            f"  Got: {value}\n"
            f"  Valid values: {valid_str[:200]}..."
        )


def validate_prompt(prompt: str) -> None:
    """P2-E: 客户端拒绝空 prompt，避免发空请求浪费 API 配额"""
    if not prompt or not prompt.strip():
        raise SystemExit(
            "prompt cannot be empty. Pass --prompt 'your description'."
        )


def validate_mode_and_images(args: argparse.Namespace) -> None:
    """P1-A + P2-F (v3.1.2 重命名): 客户端校验 image 数量 / keyframes 模式合法性

    原名 `validate_image_count` 容易误以为只校验 image 数量，实际还校验 mode
    （keyframes 至少 2 张图）。改名为更准确的 `validate_mode_and_images`。
    """
    image_urls = args.image_url or []
    if len(image_urls) > MAX_IMAGES_PER_REQUEST:
        raise SystemExit(
            f"Too many images: {len(image_urls)} (max {MAX_IMAGES_PER_REQUEST})."
        )
    if args.mode == "keyframes" and len(image_urls) < KEYFRAMES_MIN_IMAGES:
        raise SystemExit(
            f"--mode keyframes requires at least {KEYFRAMES_MIN_IMAGES} images "
            f"(you passed {len(image_urls)}). Use --image-url multiple times."
        )


def build_payload(args: argparse.Namespace) -> dict[str, Any]:
    validate_num_frames(args.num_frames)
    validate_prompt(args.prompt)
    validate_mode_and_images(args)
    payload: dict[str, Any] = {
        "model": MODEL,
        "prompt": args.prompt,
        "height": args.height,
        "width": args.width,
        "num_frames": args.num_frames,
        "frame_rate": args.frame_rate,
    }
    optional_values = {
        "num_inference_steps": args.num_inference_steps,
        "seed": args.seed,
        "negative_prompt": args.negative_prompt,
    }
    for key, value in optional_values.items():
        if value is not None:
            payload[key] = value

    image_urls = args.image_url or []
    if image_urls:
        if len(image_urls) == 1:
            # 单图图生视频：top-level image（不强制 mode=ti2vid，官方文档示例 2 未设）
            # 用户需显式 mode 时可加 --mode ti2vid
            payload["image"] = image_urls[0]
        else:
            # 多图 / 关键帧：extra_body.image 数组
            # 官方文档示例 3/4：多图/关键帧的 mode 都放在 extra_body 里，顶层不设
            payload["extra_body"] = {"image": image_urls}
            if args.mode == "keyframes":
                payload["extra_body"]["mode"] = "keyframes"

    # 显式 --mode 顶层透传（keyframes 已在 extra_body 设过，不重复）
    if args.mode == "ti2vid":
        payload.setdefault("mode", "ti2vid")

    return payload


# ============================================================================
# v3.0: 响应解析（兼容多种字段名 / 嵌套结构）
# ============================================================================

def _deep_get(d: Any, *keys: str) -> Any:
    """在 dict 中按顺序尝试 key 列表逐层取值"""
    for key in keys:
        if isinstance(d, dict):
            d = d.get(key)
        else:
            return None
    return d


def extract_task_id(response: dict[str, Any]) -> Optional[str]:
    for key in ("id", "task_id"):
        v = response.get(key)
        if isinstance(v, str) and v:
            return v
    return _deep_get(response, "data", "id") or _deep_get(response, "data", "task_id")


def extract_video_id(response: dict[str, Any]) -> Optional[str]:
    """提取 video_id（推荐查询用）

    v3.2.x 修：API 实测 video_id 可能是 `video_xxx`、`task_xxx` 或其他标识符，
    只要非空就拿，不再过滤前缀。原代码严卡 `video_` 前缀，致 video_id=None，
    走 task_id 兜底有效但委托到 fallback 路径，不美观。
    """
    for key in ("video_id", "id", "task_id"):
        v = response.get(key)
        if isinstance(v, str) and v.strip():
            return v
    nested = _deep_get(response, "data", "video_id")
    if isinstance(nested, str) and nested.strip():
        return nested
    return None


def extract_status(response: dict[str, Any]) -> str:
    value = response.get("status")
    if isinstance(value, str):
        return value.lower()
    data = response.get("data")
    if isinstance(data, dict) and isinstance(data.get("status"), str):
        return data["status"].lower()
    return "unknown"


def extract_progress(response: dict[str, Any]) -> Optional[int]:
    for path in (("progress",), ("data", "progress")):
        v = _deep_get(response, *path)
        if isinstance(v, (int, float)):
            return int(v)
    return None


# 视频 URL 字段候选（按文档/历史响应模式排序）
# v3.2.x 实测：最新 API completed 响应把 url 藏在 metadata.url（不是顶层 url）
# （v3.0 时报 references/api.md 顶层 video_url，已变动）
VIDEO_URL_KEYS = ("video_url", "url", "remixed_from_video_id", "video", "output_url")
# 嵌套容器 key（深递归时跳进去找 url，避免 data.metadata.url 瀰）
_RECURSE_CONTAINERS = ("metadata", "data")
_MAX_RECURSE_DEPTH = 4  # 数据 / metadata / 调参，正常响应最多 3-4 层


def extract_video_url(response: dict[str, Any]) -> Optional[str]:
    """智能识别视频 URL：尝试顶层 + 深嵌套 metadata/data.url

    v3.2.x 加 metadata.url（API 0. 最新位置）：它可能藏在
    metadata.url / data.metadata.url / data.url / 任何巢套容器里的 url。
    从顶层开始 BFS，找第一个 https:// 串就返。
    """
    if not isinstance(response, dict):
        return None
    stack: list[tuple[Any, int]] = [(response, 0)]
    seen: set[int] = set()
    while stack:
        node, depth = stack.pop()
        if not isinstance(node, dict):
            continue
        # 避免循环引用（response 本身或反身指向）
        if id(node) in seen:
            continue
        seen.add(id(node))
        # 1. 当前节点查 VIDEO_URL_KEYS（顶层有最高优先级——AVS 容器拿下节点不优先）
        for key in VIDEO_URL_KEYS:
            v = node.get(key)
            if isinstance(v, str) and v.startswith(("http://", "https://")):
                return v
        # 2. 推进嵌套容器（优先 metadata > data，因为状态量都抽别的为一展其填）
        if depth >= _MAX_RECURSE_DEPTH:
            continue
        for container in _RECURSE_CONTAINERS:
            sub = node.get(container)
            if isinstance(sub, dict):
                stack.append((sub, depth + 1))
    return None


# ============================================================================
# v3.0: HTTP 调用封装（POST + GET video_id + GET task_id 兜底）
# ============================================================================

def create_task(args: argparse.Namespace, key: str) -> dict[str, Any]:
    """POST 创建任务（v3.2.0: 单 key，粘性）"""
    url = f"{args.api_base.rstrip('/')}/v1/videos"
    return request_json_with_retry("POST", url, key, args.payload, max_retries=args.max_retries)


def get_status_by_video_id(video_id: str, key: str, api_base: str, max_retries: int) -> dict[str, Any]:
    """新 API 推荐方式：GET /agnesapi?video_id=...（含可选 model_name）"""
    url = f"{api_base.rstrip('/')}/agnesapi?video_id={parse.quote(video_id)}&model_name={MODEL}"
    return request_json_with_retry("GET", url, key, max_retries=max_retries)


def get_status_by_task_id(task_id: str, key: str, api_base: str, max_retries: int) -> dict[str, Any]:
    """兼容方式：GET /v1/videos/{task_id}"""
    url = f"{api_base.rstrip('/')}/v1/videos/{parse.quote(task_id)}"
    return request_json_with_retry("GET", url, key, max_retries=max_retries)


def get_status_smart(
    response_or_ids: dict | tuple,
    key: str,
    api_base: str,
    max_retries: int,
    tried_video_id: Optional[set] = None,
) -> dict:
    """v3.2.0: 单 key 粘性查询

    接受两种入参：
    - dict: 从 dict 里提取 video_id / task_id
    - tuple: (video_id, task_id) 二元组

    v3.1 行为（修复 P0-C / P0-D）：
    - P0-D：fallback 只在 video_id 端点返回 404（资源不存在）时触发；
      401/403/5xx 是 key 或服务端问题，task_id 端点用同一 key 必然同样错，不该 fallback
    - P0-C：用 `tried_video_id` 集合记录已确认 404 的 video_id，
      调用方（poll_task）累计到 VIDEO_ID_404_LIMIT 时抛错退出，避免死循环到 timeout

    v3.2.0 行为：
    - 入参从 `keys: list` 改成 `key: str`：整个 poll 用同一把 key
      → 避免一个视频 poll 把 4 把 key 的 RPM 全部烧光
    """
    if isinstance(response_or_ids, dict):
        video_id = extract_video_id(response_or_ids)
        task_id = extract_task_id(response_or_ids)
    else:
        video_id, task_id = response_or_ids

    tried = tried_video_id if tried_video_id is not None else set()

    if video_id and video_id not in tried:
        try:
            return get_status_by_video_id(video_id, key, api_base, max_retries)
        except ApiError as exc:
            # P0-D: fallback 只在 404（资源不存在）时触发
            if exc.status == 404:
                tried.add(video_id)
                print(
                    f"# [warn] video_id '{video_id}' not found (404), "
                    f"falling back to task_id",
                    file=sys.stderr,
                )
            else:
                # 401/403/5xx 等：key 或服务端问题，task_id 用同 key 同样错，直接抛
                raise

    if task_id:
        return get_status_by_task_id(task_id, key, api_base, max_retries)
    if tried and not task_id:
        # 之前 video_id 404 但 task_id 是 None → 资源真的不存在，不再重试
        raise ApiError(
            f"Resource not found: video_id='{next(iter(tried))}' "
            f"(and no task_id to fallback)",
            status=404,
        )
    raise ApiError("Neither video_id nor task_id available for status query")


# ============================================================================
# v3.0: 轮询 + 下载
# ============================================================================

def poll_task(
    video_id: Optional[str],
    task_id: Optional[str],
    key: str,
    api_base: str,
    interval: float,
    timeout: float,
    max_retries: int,
) -> dict[str, Any]:
    """v3.2.0: 粘性单 key 轮询

    关键改动：
    - 入参从 `keys: list` 改成 `key: str`：整个 poll 周期只用一把 key
    - 间隔默认 5s + 30 分钟超时：4 把 key 时一个视频占 1 把 ~5 RPM × 30 min = 150 calls
      单 key 完全可以 cover（v3.2.0 之前：一次失败轮询 4 把 key = 4 × 150 = 600 calls）
    - 5xx/429 时 sleep interval 后重试（不换 key）
    - 401/403 抛 KeyDeadError → 上层 catch 后换 key 重新 poll
    """
    deadline = time.time() + timeout
    last_response: dict[str, Any] | None = None
    last_progress = -1
    # v3.1 P0-C: 记录已 404 的 video_id，避免无效死循环到 timeout
    tried_video_id: set[str] = set()
    while time.time() <= deadline:
        try:
            response = get_status_smart(
                (video_id, task_id), key, api_base, max_retries,
                tried_video_id=tried_video_id,
            )
        except ApiError as exc:
            # v3.1 P0-C: 资源确认不存在 (404 + task_id=None) 或 auth 失败 → 立即终止
            if exc.status == 404 or exc.status in (401, 403):
                raise
            print(f"# [poll] query error: {exc}", file=sys.stderr)
            time.sleep(interval)
            continue
        last_response = response
        status = extract_status(response)
        progress = extract_progress(response)
        # 进度变化才打印（避免 spam）
        if progress != last_progress:
            print(
                f"# [poll] status={status} progress={progress}% "
                f"video_id={video_id} task_id={task_id}",
                file=sys.stderr,
            )
            last_progress = progress

        if status in DONE_STATES:
            return response
        if status in FAILED_STATES:
            err_msg = _extract_err_msg(response) or "no error message"
            raise ApiError(
                f"Video task failed with status '{status}': {err_msg}",
                payload=response,
            )
        if status not in RUNNING_STATES and status != "unknown":
            print(f"# [poll] unknown status '{status}', continuing", file=sys.stderr)
        time.sleep(interval)
    raise ApiError(
        f"Timed out waiting for task (video_id={video_id} task_id={task_id})",
        payload=last_response,
    )


def filename_from_url(url: str) -> str:
    parsed = parse.urlparse(url)
    name = Path(parsed.path).name
    if not name or "." not in name:
        ext = mimetypes.guess_extension("video/mp4") or ".mp4"
        name = f"agnes-video-{int(time.time())}{ext}"
    return name


def download_video(
    url: str,
    output_path: Optional[Path],
    output_dir: Optional[str],
) -> Optional[Path]:
    """下载视频到 output_path（优先）或 output_dir/<filename_from_url>

    v3.1 P1-C: 默认 output_dir 改为 /home/goron/文档/Openclaw/输出/agnes-free-video/
    （遵循 TOOLS.md 全局默认输出规则，跨 cwd 一致）
    """
    if output_path:
        path = Path(output_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
    else:
        directory = (
            Path(output_dir).expanduser()
            if output_dir
            else Path(DEFAULT_OUTPUT_DIR)
        )
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / filename_from_url(url)

    if download_file(str(url), str(path), timeout=900):
        return path
    return None


# ============================================================================
# v3.0: Agent 模式输出（结构化 key-value，agent 一定看得到）
# ============================================================================

def _print_agent_success(
    path: Optional[Path],
    video_url: str,
    video_id: Optional[str],
    task_id: Optional[str],
    prompt: str,
    size: str = "",
    seconds: str = "",
    key_fp: str = "",
) -> None:
    """agent 模式成功输出：结构化 key-value，每行一个字段

    v3.2.0 改动：加 key_fp 字段（last-used key 指纹），方便主人看哪个 key 跑完的
    """
    print("STATUS: ok")
    if path:
        print(f"PATH: {path}")
    print(f"URL: {video_url}")
    if video_id:
        print(f"VIDEO_ID: {video_id}")
    if task_id:
        print(f"TASK_ID: {task_id}")
    if size:
        print(f"SIZE: {size}")
    if seconds:
        print(f"SECONDS: {seconds}")
    if key_fp:
        print(f"KEY: {key_fp}")
    # PROMPT 放最后（可能很长，但 agent 可以选择忽略）
    print(f"PROMPT: {prompt}")


def _print_agent_error(message: str, status: Optional[int] = None) -> None:
    """agent 模式错误输出：走 stdout（agent 一定看得到）"""
    print("STATUS: error")
    print(f"MESSAGE: {message}")
    if status is not None:
        print(f"HTTP_STATUS: {status}")


def _print_agent_submitted(
    video_id: Optional[str],
    task_id: Optional[str],
    prompt: str = "",
    key_fp: str = "",
) -> None:
    """--no-poll 模式：只提交不轮询

    v3.1.2 改动：加 prompt 参数（agent 模式可选），方便后续 record
    v3.2.0 改动：加 key_fp 字段
    （PROMPT 放最后一行，agent 可以选择性忽略）
    """
    print("STATUS: submitted")
    if video_id:
        print(f"VIDEO_ID: {video_id}")
    if task_id:
        print(f"TASK_ID: {task_id}")
    if key_fp:
        print(f"KEY: {key_fp}")
    if prompt:
        print(f"PROMPT: {prompt}")


# ============================================================================
# v3.0: 命令实现
# ============================================================================

def _final_response_size_seconds(response: dict[str, Any]) -> tuple[str, str]:
    size = response.get("size") or _deep_get(response, "data", "size") or ""
    seconds = response.get("seconds") or _deep_get(response, "data", "seconds") or ""
    return str(size), str(seconds)


def cmd_create(args: argparse.Namespace) -> int:
    # v3.1 P0-B: --no-poll 跟 --download / --output 互斥（不轮询就拿不到 video_url）
    if args.no_poll and (args.download or args.output or args.output_dir):
        msg = (
            "--no-poll cannot be combined with --download / --output / --output-dir: "
            "the task is not polled, so no video_url is fetched. "
            "Run 'status --video-id <VIDEO_ID> --download' later to download."
        )
        if args.format == "agent":
            _print_agent_error(msg)
            return 1
        print(f"Error: {msg}", file=sys.stderr)
        return 1

    try:
        payload = build_payload(args)
    except SystemExit as exc:
        if args.format == "agent":
            _print_agent_error(str(exc))
            return 1
        raise
    args.payload = payload

    if args.dry_run:
        # v3.1 P0-A: dry-run 也尊重 --format 参数
        if args.format == "agent":
            print("STATUS: ok")
            print("DRY_RUN: 1")
            print("NOTE: request not sent; payload shown below")
            print(f"PROMPT: {args.prompt}")
            # 把 payload 放最后（agent 可以选择忽略）
            # v3.2.x: 用 JSON dumps 而不是 repr，避免 dict 列表值变成单引号
            # （agent 解析失败，且和 actual API request body 不一致）
            for k, v in payload.items():
                encoded = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
                print(f"PAYLOAD_{k.upper()}: {encoded}")
            return 0
        url = f"{args.api_base.rstrip('/')}/v1/videos"
        print(json.dumps({"url": url, "payload": payload}, ensure_ascii=False, indent=2))
        return 0

    try:
        keys = get_api_keys()
    except ApiError as exc:
        if args.format == "agent":
            _print_agent_error(str(exc))
            return 1
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    # v3.2.0: KeyPool 粘性轮换
    pool = get_key_pool(keys)
    # 最大换 key 次数 = pool 大小（4 把 key 最多换 3 次 + 第一次 = 4 次）
    max_key_swaps = len(pool)
    last_exc: Optional[ApiError] = None
    for swap_round in range(max_key_swaps):
        key = pool.acquire_key()
        key_fp = _key_fingerprint(key)
        try:
            response = create_task(args, key)
        except KeyDeadError as exc:
            # 401/403 → 这把 key 死了，标记后换下一把
            pool.mark_dead(key)
            last_exc = exc
            print(
                f"# [cmd_create] swap round {swap_round + 1}/{max_key_swaps}: "
                f"key {key_fp} dead, trying next",
                file=sys.stderr,
            )
            continue
        except ApiError as exc:
            # 其他错误（4xx 业务、5xx 重试耗尽等）→ 不换 key，直接报
            # v3.2.x: retryable (5xx/429/网络) 错误后 mark_used 进 60s cooldown
            if exc.status and is_retryable_status(exc.status):
                pool.mark_used(key)
            if args.format == "agent":
                _print_agent_error(str(exc), exc.status)
                return 1
            print(f"Agnes API error: {exc}", file=sys.stderr)
            if exc.payload is not None:
                print(json.dumps(exc.payload, ensure_ascii=False, indent=2), file=sys.stderr)
            return 1

        # create 成功 → 标记 key 已用
        pool.mark_used(key)
        break  # 跳出 swap 循环
    else:
        # 所有 key 都死了（4 把都 401/403）→ 报错
        msg = f"All {len(pool)} key(s) returned 401/403 (auth/quota exhausted)"
        if last_exc is not None:
            msg += f"\n  Last error: {last_exc}"
        if args.format == "agent":
            _print_agent_error(msg, status=last_exc.status if last_exc else None)
            return 1
        print(f"Error: {msg}", file=sys.stderr)
        return 1

    video_id = extract_video_id(response)
    task_id = extract_task_id(response)

    if args.no_poll:
        if args.format == "agent":
            _print_agent_submitted(video_id, task_id, prompt=args.prompt, key_fp=key_fp)
        else:
            print(json.dumps(response, ensure_ascii=False, indent=2))
        return 0

    # v3.2.0: poll 继续用同一把 key（粘性），如果死了再换 + 重 poll
    # 这里 key 还是上面 acquire 的那把；poll 过程中如果 KeyDeadError，需要 swap
    # 简化实现：poll 用现成 key，KeyDeadError 时换 key 重 poll
    final: Optional[dict[str, Any]] = None
    for poll_swap_round in range(max_key_swaps):
        try:
            final = poll_task(
                video_id, task_id, key, args.api_base,
                args.poll_interval, args.timeout, args.max_retries,
            )
            # poll 成功 → 这把 key 也算"用过"了（更新 last_used）
            pool.mark_used(key)
            break
        except KeyDeadError as exc:
            pool.mark_dead(key)
            print(
                f"# [cmd_create/poll] swap round {poll_swap_round + 1}/{max_key_swaps}: "
                f"key {key_fp} dead during poll, trying next",
                file=sys.stderr,
            )
            # 重新拿一把
            try:
                key = pool.acquire_key()
                key_fp = _key_fingerprint(key)
            except Exception as e:  # noqa: BLE001
                if args.format == "agent":
                    _print_agent_error(f"No more healthy keys: {e}")
                    return 1
                print(f"Error: No more healthy keys: {e}", file=sys.stderr)
                return 1
            continue
        except ApiError as exc:
            # v3.2.x: 429/5xx 等 retryable 错误抛 ApiError 后也要 mark_used
            # （否则这把 key 不进 60s cooldown，下个任务立刻又选中它，再被限流死循环）
            if exc.status and is_retryable_status(exc.status):
                pool.mark_used(key)
            if args.format == "agent":
                _print_agent_error(str(exc), exc.status)
                return 1
            print(f"Agnes API error: {exc}", file=sys.stderr)
            if exc.payload is not None:
                print(json.dumps(exc.payload, ensure_ascii=False, indent=2), file=sys.stderr)
            return 1

    if final is None:
        # 所有 key 在 poll 阶段都死了
        msg = f"All {len(pool)} key(s) died during polling"
        if args.format == "agent":
            _print_agent_error(msg, status=401)
            return 1
        print(f"Error: {msg}", file=sys.stderr)
        return 1

    video_url = extract_video_url(final)
    if not video_url:
        if args.format == "agent":
            _print_agent_error("No video URL in completed response")
            return 1
        print("No video URL in response.", file=sys.stderr)
        print(json.dumps(final, ensure_ascii=False, indent=2))
        return 1

    # 下载（--output 或 --output-dir 或 --download）
    if args.output or args.output_dir or args.download:
        path = download_video(
            video_url,
            Path(args.output) if args.output else None,
            args.output_dir,
        )
        if not path:
            if args.format == "agent":
                _print_agent_error(f"Download failed: {video_url}")
                return 1
            print(f"Download failed: {video_url}", file=sys.stderr)
            return 1
    else:
        path = None

    if args.format == "agent":
        size, seconds = _final_response_size_seconds(final)
        _print_agent_success(path, video_url, video_id, task_id, args.prompt, size, seconds, key_fp=key_fp)
        return 0

    # json / human 模式：dump 完整响应 + 下载路径
    output = dict(final)
    if path:
        output["local_path"] = str(path)
    output["key"] = key_fp
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    try:
        keys = get_api_keys()
    except ApiError as exc:
        if args.format == "agent":
            _print_agent_error(str(exc))
            return 1
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    # v3.2.0: KeyPool 粘性（这里选 1 把 key 用于本次 status 查询）
    # 注：如果该任务之前是 cmd_create 起的，理论上应该用同一把 key。
    #     但纯内存版跨调用不记 task→key 映射，所以这里用 round-robin 重新拿一把。
    #     后果是：1 个视频的 status 查询可能会换 1 把 key（不会撞 RPM，因为是单次查询）。
    pool = get_key_pool(keys)
    max_key_swaps = len(pool)
    last_exc: Optional[ApiError] = None

    final: Optional[dict[str, Any]] = None
    key: Optional[str] = None
    key_fp: str = ""
    for swap_round in range(max_key_swaps):
        key = pool.acquire_key()
        key_fp = _key_fingerprint(key)
        try:
            if args.wait:
                final = poll_task(
                    args.video_id, args.task_id, key, args.api_base,
                    args.poll_interval, args.timeout, args.max_retries,
                )
            else:
                final = get_status_smart(
                    (args.video_id, args.task_id), key, args.api_base, args.max_retries
                )
            # 成功 → 标记 used
            pool.mark_used(key)
            break
        except KeyDeadError as exc:
            pool.mark_dead(key)
            last_exc = exc
            print(
                f"# [cmd_status] swap round {swap_round + 1}/{max_key_swaps}: "
                f"key {key_fp} dead, trying next",
                file=sys.stderr,
            )
            continue
        except ApiError as exc:
            # v3.2.x: retryable 错误后也要 mark_used（避免下次拿同一把重被限流）
            if exc.status and is_retryable_status(exc.status):
                pool.mark_used(key)
            if args.format == "agent":
                _print_agent_error(str(exc), exc.status)
                return 1
            print(f"Agnes API error: {exc}", file=sys.stderr)
            if exc.payload is not None:
                print(json.dumps(exc.payload, ensure_ascii=False, indent=2), file=sys.stderr)
            return 1

    if final is None:
        msg = f"All {len(pool)} key(s) returned 401/403 (auth/quota exhausted)"
        if last_exc is not None:
            msg += f"\n  Last error: {last_exc}"
        if args.format == "agent":
            _print_agent_error(msg, status=last_exc.status if last_exc else None)
            return 1
        print(f"Error: {msg}", file=sys.stderr)
        return 1

    video_url = extract_video_url(final)
    # v3.2.x: 如果是 --wait 查到终态但没 URL，报错（避免 agent 看到 false success）
    final_status = extract_status(final)
    if args.wait and final_status in DONE_STATES and not video_url:
        msg = f"Task ended in '{final_status}' but no video URL in response"
        if args.format == "agent":
            _print_agent_error(msg)
            return 1
        print(f"Error: {msg}\n{json.dumps(final, ensure_ascii=False, indent=2)}", file=sys.stderr)
        return 1

    if video_url and (args.output or args.output_dir or args.download):
        path = download_video(
            video_url,
            Path(args.output) if args.output else None,
            args.output_dir,
        )
        if not path and args.format == "agent":
            _print_agent_error(f"Download failed: {video_url}")
            return 1
    else:
        path = None

    if args.format == "agent":
        size, seconds = _final_response_size_seconds(final)
        _print_agent_success(
            path, video_url or "(no url)", args.video_id, args.task_id,
            prompt="(status check)", size=size, seconds=seconds, key_fp=key_fp,
        )
        return 0

    output = dict(final)
    if path:
        output["local_path"] = str(path)
    output["key"] = key_fp
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


# ============================================================================
# v3.0: argparse
# ============================================================================

def add_common_create_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--prompt", required=True)
    parser.add_argument(
        "--image-url", action="append",
        help="Input image URL; repeat for multi-image or keyframes",
    )
    parser.add_argument(
        "--mode", choices=["ti2vid", "keyframes"],
        help="Generation mode: 'ti2vid' (default for single image), 'keyframes' (multi-image transition)",
    )
    parser.add_argument("--height", type=int, default=768)
    parser.add_argument("--width", type=int, default=1152)
    parser.add_argument("--num-frames", type=int, default=121)
    parser.add_argument("--num-inference-steps", type=int)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--frame-rate", type=float, default=24)
    parser.add_argument("--negative-prompt")
    parser.add_argument(
        "--output", "-o",
        help="Single output file path (overrides --output-dir filename)",
    )
    parser.add_argument("--output-dir", help="Directory to save downloaded video")
    parser.add_argument(
        "--download", action="store_true",
        help=f"Download video_url when the task completes (uses --output-dir or {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--no-poll", action="store_true",
        help="Only submit the task, print video_id/task_id, do not poll",
    )
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--max-retries", type=int, default=MAX_RETRIES)
    parser.add_argument("--api-base", default=API_BASE)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print request JSON without calling the API",
    )
    parser.add_argument(
        "--format", choices=["agent", "json", "human"], default="agent",
        help="Output format: 'agent' (default) emits structured STATUS/PATH/URL on stdout for AI agent parsing; 'json' dumps full API response (errors to stderr); 'human' is alias of 'json'.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create and retrieve Agnes-Video-V2.0 tasks.",
        epilog="Examples:\n"
               "  Text-to-video:\n"
               "    python3 agnes_video.py create --prompt 'A cat on the beach at sunset' --download\n"
               "  Image-to-video (use video_id for status):\n"
               "    python3 agnes_video.py create --prompt '...' --image-url https://x.png --download\n"
               "  Keyframes:\n"
               "    python3 agnes_video.py create --prompt '...' --image-url k1.png --image-url k2.png --mode keyframes --download\n"
               "  Check status (video_id first, task_id fallback):\n"
               "    python3 agnes_video.py status --video-id video_xxx --wait --download",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="Create a video generation task")
    add_common_create_args(create)
    create.set_defaults(func=cmd_create)

    status = subparsers.add_parser(
        "status", help="Retrieve a video task by video_id (preferred) or task_id (fallback)",
    )
    status.add_argument("--video-id", help="Video ID (preferred; from create response)")
    status.add_argument("--task-id", help="Task ID (fallback; legacy support)")
    status.add_argument(
        "--wait", action="store_true",
        help="Poll until the task completes or fails",
    )
    status.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL)
    status.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    status.add_argument("--max-retries", type=int, default=MAX_RETRIES)
    status.add_argument("--download", action="store_true")
    status.add_argument("--output", "-o", help="Single output file path")
    status.add_argument("--output-dir", help="Directory to save downloaded video")
    status.add_argument("--api-base", default=API_BASE)
    status.add_argument(
        "--format", choices=["agent", "json", "human"], default="agent",
        help="Output format (default 'agent')",
    )
    status.set_defaults(func=cmd_status)
    return parser


def main() -> int:
    parser = build_parser()
    is_agent_format = _cli_uses_agent_format()
    try:
        args = parser.parse_args()
    except SystemExit as exc:
        # v3.1 P1-B: argparse 错（如 --video-id 错放到 create、缺子命令等）
        # 在 agent 模式下转成 _print_agent_error，统一退出码 1
        code = exc.code if isinstance(exc.code, int) else 2
        if code == 2 and is_agent_format:
            _print_agent_error("Invalid CLI arguments (see usage above)")
            return 1
        raise
    if args.command == "status" and not (args.video_id or args.task_id):
        msg = "status requires --video-id or --task-id"
        if getattr(args, "format", "agent") == "agent":
            _print_agent_error(msg)
            return 1
        parser.error(msg)  # json / human 模式走 argparse 默认
    return args.func(args)


def _cli_uses_agent_format() -> bool:
    """检测命令行是否传了 --format。返回 True 表示用 agent 格式（默认）。"""
    argv = sys.argv[1:]
    if "--format" in argv:
        idx = argv.index("--format")
        if idx + 1 < len(argv):
            return argv[idx + 1] == "agent"
    return True


if __name__ == "__main__":
    raise SystemExit(main())
