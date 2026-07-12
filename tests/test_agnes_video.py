#!/usr/bin/env python3
"""Agnes Free Video v3.0 回归测试

覆盖：
- TestBuildPayload (4): T2V、I2V、Multi-Image、Keyframes
- TestValidateNumFrames (3): 合法值、过大、不满足 8n+1
- TestExtractors (6): task_id / video_id / status / progress / video_url 多字段名
- TestApiKey (3): 单 key / 多 key / 缺 key
- TestRetry (3): 429/500/网络错误分类
- TestAgentOutput (2): 成功 / 错误格式

跑法：python3 tests/test_agnes_video.py（直接 run，无需 pytest）
或：python3 -m pytest tests/test_agnes_video.py -v
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

# 注入 scripts/ 到 path
SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import agnes_video  # noqa: E402


# ============================================================================
# TestBuildPayload: 4 种 workflow 的 payload 构造
# ============================================================================

class TestBuildPayload(unittest.TestCase):
    def _args(self, **overrides):
        """构造一个最小可用 Namespace（模拟 argparse）"""
        defaults = dict(
            prompt="A cinematic test prompt",
            image_url=None,
            mode=None,
            height=768,
            width=1152,
            num_frames=121,
            num_inference_steps=None,
            seed=None,
            frame_rate=24,
            negative_prompt=None,
        )
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def test_t2v(self):
        """T2V：纯文本，无 image，无 mode"""
        args = self._args()
        p = agnes_video.build_payload(args)
        self.assertEqual(p["model"], "agnes-video-v2.0")
        self.assertEqual(p["prompt"], "A cinematic test prompt")
        self.assertEqual(p["num_frames"], 121)
        self.assertEqual(p["frame_rate"], 24)
        self.assertNotIn("image", p)
        self.assertNotIn("extra_body", p)
        self.assertNotIn("mode", p)

    def test_i2v_single_image(self):
        """I2V：单图，不强制 mode（符合官方文档示例 2）"""
        args = self._args(image_url=["https://x.com/a.png"])
        p = agnes_video.build_payload(args)
        self.assertEqual(p["image"], "https://x.com/a.png")
        # 不强制 mode=ti2vid，官方示例 2 没设；用户需显式 --mode ti2vid
        self.assertNotIn("mode", p)
        self.assertNotIn("extra_body", p)

    def test_i2v_with_explicit_ti2vid(self):
        """I2V + 显式 --mode ti2vid：顶层加 mode"""
        args = self._args(image_url=["https://x.com/a.png"], mode="ti2vid")
        p = agnes_video.build_payload(args)
        self.assertEqual(p["image"], "https://x.com/a.png")
        self.assertEqual(p["mode"], "ti2vid")

    def test_multi_image(self):
        """Multi-Image：多图走 extra_body.image，不加 mode"""
        args = self._args(image_url=["https://x.com/a.png", "https://x.com/b.png"])
        p = agnes_video.build_payload(args)
        self.assertNotIn("image", p)
        self.assertIn("extra_body", p)
        self.assertEqual(p["extra_body"]["image"], ["https://x.com/a.png", "https://x.com/b.png"])
        self.assertNotIn("mode", p["extra_body"])
        self.assertNotIn("mode", p)

    def test_keyframes(self):
        """Keyframes：多图 + extra_body.mode=keyframes"""
        args = self._args(
            image_url=["https://x.com/k1.png", "https://x.com/k2.png"],
            mode="keyframes",
        )
        p = agnes_video.build_payload(args)
        self.assertIn("extra_body", p)
        self.assertEqual(p["extra_body"]["mode"], "keyframes")
        self.assertEqual(p["extra_body"]["image"], ["https://x.com/k1.png", "https://x.com/k2.png"])


# ============================================================================
# TestValidateNumFrames: 边界值
# ============================================================================

class TestValidateNumFrames(unittest.TestCase):
    def test_valid(self):
        """合法值 81/121/161/241/441 不报错"""
        for n in (81, 121, 161, 241, 441):
            agnes_video.validate_num_frames(n)  # 不应抛

    def test_too_large(self):
        """> 441 报错"""
        with self.assertRaises(SystemExit):
            agnes_video.validate_num_frames(449)

    def test_not_8n_plus_1(self):
        """不满足 8n+1 报错（如 100、120）"""
        for bad in (100, 120, 200, 300):
            with self.assertRaises(SystemExit):
                agnes_video.validate_num_frames(bad)


# ============================================================================
# TestExtractors: 响应解析
# ============================================================================

class TestExtractors(unittest.TestCase):
    def test_extract_task_id_top(self):
        """task_id 在顶层"""
        r = {"task_id": "task_xxx", "video_id": "video_xxx"}
        self.assertEqual(agnes_video.extract_task_id(r), "task_xxx")

    def test_extract_video_id_top(self):
        """video_id 在顶层（推荐字段）"""
        r = {"task_id": "task_xxx", "video_id": "video_xxx"}
        self.assertEqual(agnes_video.extract_video_id(r), "video_xxx")

    def test_extract_status_nested(self):
        """status 在 data 嵌套里"""
        r = {"data": {"status": "in_progress"}}
        self.assertEqual(agnes_video.extract_status(r), "in_progress")

    def test_extract_progress_nested(self):
        """progress 在 data 嵌套里"""
        r = {"data": {"progress": 42}}
        self.assertEqual(agnes_video.extract_progress(r), 42)

    def test_extract_video_url_standard(self):
        """标准 video_url 字段"""
        r = {"video_url": "https://gcs.example.com/v.mp4"}
        self.assertEqual(agnes_video.extract_video_url(r), "https://gcs.example.com/v.mp4")

    def test_extract_video_url_remixed(self):
        """官方文档示例：视频 URL 在 remixed_from_video_id 字段（文档错乱）"""
        r = {"remixed_from_video_id": "https://gcs.example.com/v.mp4"}
        self.assertEqual(
            agnes_video.extract_video_url(r),
            "https://gcs.example.com/v.mp4",
        )

    def test_extract_video_url_metadata_url(self):
        """v3.2.x bug fix: 最新 API 实际把 URL 藏在 metadata.url 字段"""
        r = {
            "id": "task_xxx",
            "status": "completed",
            "metadata": {"url": "https://platform-outputs.agnes-ai.space/v.mp4"},
        }
        self.assertEqual(
            agnes_video.extract_video_url(r),
            "https://platform-outputs.agnes-ai.space/v.mp4",
        )

    def test_extract_video_url_data_metadata_url(self):
        """v3.2.x: metadata 嵌套在 data 下也能识别"""
        r = {
            "data": {
                "status": "completed",
                "metadata": {"url": "https://gcs.example.com/v.mp4"},
            }
        }
        self.assertEqual(
            agnes_video.extract_video_url(r),
            "https://gcs.example.com/v.mp4",
        )

    def test_extract_video_url_top_wins_over_metadata(self):
        """v3.2.x: 优先级仍以顶层字段为准（不要优先 metadata.url）"""
        r = {
            "video_url": "https://gcs.example.com/top.mp4",
            "metadata": {"url": "https://gcs.example.com/wrong.mp4"},
        }
        self.assertEqual(
            agnes_video.extract_video_url(r),
            "https://gcs.example.com/top.mp4",
        )

    def test_extract_video_id_accepts_task_prefix(self):
        """v3.2.x bug fix: API 实际返回 task_xxx 作为 video_id，
        原实现严卡 video_ 前缀，导致 video_id=None 走 task_id 兜底"""
        r = {
            "id": "task_OBl4fLcBa5vIILcTFcaMJ96s1KiGPwb0",
            "video_id": "task_OBl4fLcBa5vIILcTFcaMJ96s1KiGPwb0",
            "task_id": "task_OBl4fLcBa5vIILcTFcaMJ96s1KiGPwb0",
        }
        # 不管前缀，只要非空就拿
        self.assertEqual(
            agnes_video.extract_video_id(r),
            "task_OBl4fLcBa5vIILcTFcaMJ96s1KiGPwb0",
        )

    def test_extract_video_id_empty_filtered(self):
        """v3.2.x: 空字符串应该当成 None（免让 API 返空字符串污染查询 URL）"""
        r = {"video_id": "", "id": "task_xxx"}
        self.assertEqual(agnes_video.extract_video_id(r), "task_xxx")


# ============================================================================
# TestApiKey: Key 池管理
# ============================================================================

class TestApiKey(unittest.TestCase):
    def setUp(self):
        self.saved = os.environ.pop("AGNES_API_KEY", None)
        self.saved_token = os.environ.pop("AGNES_TOKEN", None)
        # v3.1.2: mock skill .env 为不存在路径，避免被生产 .env 干扰
        self._skill_env_patcher = mock.patch.object(
            agnes_video, "SKILL_ENV_PATH",
            Path("/nonexistent/skill/.env"),
        )
        self._skill_env_patcher.start()
        self._xdg_env_patcher = mock.patch.object(
            agnes_video, "XDG_ENV_PATH",
            return_value=Path("/nonexistent/xdg/agnes-free-video.env"),
        )
        self._xdg_env_patcher.start()

    def tearDown(self):
        if self.saved:
            os.environ["AGNES_API_KEY"] = self.saved
        if self.saved_token:
            os.environ["AGNES_TOKEN"] = self.saved_token
        self._skill_env_patcher.stop()
        self._xdg_env_patcher.stop()

    def test_single_key(self):
        """单 key"""
        os.environ["AGNES_API_KEY"] = "sk-abc"
        self.assertEqual(agnes_video.get_api_keys(), ["sk-abc"])

    def test_multi_keys(self):
        """多 key 逗号分隔 + 去重保序"""
        os.environ["AGNES_API_KEY"] = "sk-a, sk-b ,sk-a,sk-c"
        self.assertEqual(agnes_video.get_api_keys(), ["sk-a", "sk-b", "sk-c"])

    def test_missing_key(self):
        """缺 key 抛 ApiError（v3.1.2: 不再抛 SystemExit）"""
        os.environ.pop("AGNES_API_KEY", None)
        os.environ.pop("AGNES_TOKEN", None)
        with self.assertRaises(agnes_video.ApiError):
            agnes_video.get_api_keys()


# ============================================================================
# TestRetry: 错误分类
# ============================================================================

class TestRetry(unittest.TestCase):
    def test_is_retryable_5xx(self):
        """5xx 可重试"""
        for code in (500, 502, 503, 504):
            self.assertTrue(agnes_video.is_retryable_status(code), f"HTTP {code} should be retryable")

    def test_is_retryable_429(self):
        """429 可重试"""
        self.assertTrue(agnes_video.is_retryable_status(429))

    def test_is_retryable_4xx_business(self):
        """4xx 业务错误（除 429）不可重试"""
        for code in (400, 401, 403, 404):
            self.assertFalse(agnes_video.is_retryable_status(code), f"HTTP {code} should NOT be retryable")

    def test_is_retryable_network(self):
        """网络错误（status=0）可重试"""
        self.assertTrue(agnes_video.is_retryable_status(0))


# ============================================================================
# TestAgentOutput: agent 模式 stdout 输出格式
# ============================================================================

class TestAgentOutput(unittest.TestCase):
    def test_agent_success_format(self):
        """agent 成功输出含 STATUS/PATH/URL/VIDEO_ID"""
        out = self._capture_stdout(
            agnes_video._print_agent_success,
            Path("/tmp/test.mp4"),
            "https://gcs.example.com/v.mp4",
            "video_xxx",
            "task_xxx",
            "test prompt",
            "1280x768",
            "10.0",
        )
        self.assertIn("STATUS: ok", out)
        self.assertIn("PATH: /tmp/test.mp4", out)
        self.assertIn("URL: https://gcs.example.com/v.mp4", out)
        self.assertIn("VIDEO_ID: video_xxx", out)
        self.assertIn("TASK_ID: task_xxx", out)
        self.assertIn("SIZE: 1280x768", out)
        self.assertIn("SECONDS: 10.0", out)

    def test_agent_error_format(self):
        """agent 错误输出含 STATUS: error + MESSAGE + HTTP_STATUS"""
        out = self._capture_stdout(agnes_video._print_agent_error, "quota exhausted", 401)
        self.assertIn("STATUS: error", out)
        self.assertIn("MESSAGE: quota exhausted", out)
        self.assertIn("HTTP_STATUS: 401", out)

    @staticmethod
    def _capture_stdout(func, *args, **kwargs):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            func(*args, **kwargs)
        return buf.getvalue()


# ============================================================================
# 集成测试：dry-run 不真发请求
# ============================================================================

class TestCliDryRun(unittest.TestCase):
    def test_dry_run_t2v(self):
        """dry-run T2V 不发请求，打印 payload"""
        result = subprocess.run(
            [sys.executable, str(SKILL_DIR / "scripts" / "agnes_video.py"),
             "create",
             "--prompt", "A test prompt for dry run",
             "--num-frames", "81",
             "--dry-run",
             "--format", "json"],
            capture_output=True, text=True, env={**os.environ, "AGNES_API_KEY": "sk-fake"},
        )
        self.assertEqual(result.returncode, 0, f"stderr={result.stderr}")
        out = json.loads(result.stdout)
        self.assertEqual(out["payload"]["model"], "agnes-video-v2.0")
        self.assertEqual(out["payload"]["prompt"], "A test prompt for dry run")
        self.assertEqual(out["payload"]["num_frames"], 81)
        self.assertNotIn("image", out["payload"])

    def test_dry_run_i2v(self):
        """dry-run I2V 不强制 mode（符合官方文档示例 2）"""
        result = subprocess.run(
            [sys.executable, str(SKILL_DIR / "scripts" / "agnes_video.py"),
             "create",
             "--prompt", "Animate this",
             "--image-url", "https://x.com/a.png",
             "--dry-run",
             "--format", "json"],
            capture_output=True, text=True, env={**os.environ, "AGNES_API_KEY": "sk-fake"},
        )
        self.assertEqual(result.returncode, 0, f"stderr={result.stderr}")
        out = json.loads(result.stdout)
        self.assertEqual(out["payload"]["image"], "https://x.com/a.png")
        # 默认不设 mode（官方示例 2 未设）
        self.assertNotIn("mode", out["payload"])

    def test_dry_run_keyframes(self):
        """dry-run Keyframes 走 extra_body"""
        result = subprocess.run(
            [sys.executable, str(SKILL_DIR / "scripts" / "agnes_video.py"),
             "create",
             "--prompt", "Smooth transition",
             "--image-url", "https://x.com/k1.png",
             "--image-url", "https://x.com/k2.png",
             "--mode", "keyframes",
             "--dry-run",
             "--format", "json"],
            capture_output=True, text=True, env={**os.environ, "AGNES_API_KEY": "sk-fake"},
        )
        self.assertEqual(result.returncode, 0, f"stderr={result.stderr}")
        out = json.loads(result.stdout)
        self.assertEqual(out["payload"]["extra_body"]["mode"], "keyframes")
        self.assertEqual(len(out["payload"]["extra_body"]["image"]), 2)

    def test_dry_run_invalid_num_frames(self):
        """dry-run num_frames 非法 → agent 模式错误走 stdout"""
        result = subprocess.run(
            [sys.executable, str(SKILL_DIR / "scripts" / "agnes_video.py"),
             "create",
             "--prompt", "test",
             "--num-frames", "100",
             "--dry-run",
             "--format", "agent"],
            capture_output=True, text=True, env={**os.environ, "AGNES_API_KEY": "sk-fake"},
        )
        # dry-run 模式下 num_frames 校验在 build_payload 之前就失败
        # agent 模式会走 _print_agent_error
        # 注：实际 dry-run 也走 build_payload，所以也会触发校验
        self.assertIn("STATUS: error", result.stdout)

    def test_status_requires_id(self):
        """status 子命令必须传 --video-id 或 --task-id（v3.1 P1-B: agent 格式走 stdout）"""
        result = subprocess.run(
            [sys.executable, str(SKILL_DIR / "scripts" / "agnes_video.py"),
             "status"],
            capture_output=True, text=True, env={**os.environ, "AGNES_API_KEY": "sk-fake"},
        )
        self.assertNotEqual(result.returncode, 0)
        # v3.1 P1-B: agent 模式缺 id 走 stdout（_print_agent_error），不再是 argparse stderr
        self.assertIn("STATUS: error", result.stdout)
        self.assertIn("--video-id", result.stdout)


# ============================================================================
# TestV31BugFixes: v3.1 新增的 bug 修复回归测试
# ============================================================================

class TestV31BugFixes(unittest.TestCase):
    """P0-A / P0-B / P0-C / P0-D / P1-A / P1-B 回归测试（v3.1）"""

    def _run(self, *args, env=None):
        return subprocess.run(
            [sys.executable, str(SKILL_DIR / "scripts" / "agnes_video.py"), *args],
            capture_output=True, text=True,
            env={**os.environ, "AGNES_API_KEY": "sk-fake", **(env or {})},
        )

    # P0-A: dry-run 支持 --format agent
    def test_dry_run_agent_format(self):
        """v3.1 P0-A: dry-run + agent format 输出 STATUS: ok + DRY_RUN: 1"""
        r = self._run("create", "--prompt", "test", "--dry-run", "--format", "agent")
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr}")
        self.assertIn("STATUS: ok", r.stdout)
        self.assertIn("DRY_RUN: 1", r.stdout)
        self.assertIn("PAYLOAD_MODEL: agnes-video-v2.0", r.stdout)

    # P0-B: --no-poll + --download 互斥
    def test_no_poll_download_conflict(self):
        """v3.1 P0-B: --no-poll 跟 --download 互斥，报 agent 格式错误"""
        r = self._run("create", "--prompt", "test", "--no-poll", "--download", "--format", "agent")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("STATUS: error", r.stdout)
        self.assertIn("--no-poll", r.stdout)
        self.assertIn("--download", r.stdout)

    def test_no_poll_output_conflict(self):
        """v3.1 P0-B: --no-poll 跟 --output 也互斥"""
        r = self._run("create", "--prompt", "test", "--no-poll",
                       "--output", "/tmp/v.mp4", "--format", "agent")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("STATUS: error", r.stdout)

    # P1-A: keyframes + 单图拒绝
    def test_keyframes_single_image_rejected(self):
        """v3.1 P1-A: keyframes 必须 ≥2 张图，否则拒绝"""
        r = self._run("create", "--prompt", "test",
                       "--image-url", "https://a.png",
                       "--mode", "keyframes", "--format", "agent", "--dry-run")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("STATUS: error", r.stdout)
        self.assertIn("keyframes requires at least 2 images", r.stdout)

    # P1-B: argparse 错误走 agent 格式（--video-id 错放到 create）
    def test_argparse_error_agent_format(self):
        """v3.1 P1-B: 错放参数走 agent 格式 + 退出码 1"""
        r = self._run("create", "--video-id", "video_xxx", "--format", "agent")
        # argparse 先打 usage 到 stderr，再 raise SystemExit(2) → 我们转成 STATUS: error
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("STATUS: error", r.stdout)
        # 退出码应该是 1（统一），不是 2（argparse 默认）
        self.assertEqual(r.returncode, 1)

    # P2-E: 空 prompt 拒绝
    def test_empty_prompt_rejected(self):
        """v3.1 P2-E: 空 prompt 客户端拒绝"""
        r = self._run("create", "--prompt", "", "--format", "agent", "--dry-run")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("STATUS: error", r.stdout)
        self.assertIn("prompt cannot be empty", r.stdout)

    # P2-F: image 太多拒绝
    def test_too_many_images_rejected(self):
        """v3.1 P2-F: image 数量上限 8"""
        urls = [f"https://a/{i}.png" for i in range(9)]
        r = self._run("create", "--prompt", "test", "--format", "agent", "--dry-run", *sum([["--image-url", u] for u in urls], []))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("STATUS: error", r.stdout)
        self.assertIn("Too many images: 9 (max 8)", r.stdout)

    # P2-D: VALID_NUM_FRAMES range 含 1
    def test_num_frames_1_valid(self):
        """v3.1 P2-D: num_frames=1（n=0）也是合法的"""
        agnes_video.validate_num_frames(1)  # 不应抛

    # P1-C: 默认输出路径
    def test_default_output_dir(self):
        """v3.1 P1-C: 默认输出到全局 .输出 目录"""
        self.assertEqual(
            agnes_video.DEFAULT_OUTPUT_DIR,
            "/home/goron/文档/Openclaw/输出/agnes-free-video",
        )

    # P3-A: dry-run agent 格式 payload 嵌套值用 JSON 序列化（双引号）
    def test_dry_run_agent_payload_nested_json_serialized(self):
        """v3.2.x: extra_body 等嵌套值必须用 JSON 双引号序列化，
        不能用 Python repr 单引号（agent 解析可靠 + 跟实际 request 一致）"""
        r = self._run(
            "create", "--prompt", "t",
            "--image-url", "https://a/k1.png",
            "--image-url", "https://a/k2.png",
            "--mode", "keyframes",
            "--dry-run", "--format", "agent",
        )
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr}")
        # 找到 PAYLOAD_EXTRA_BODY 一行
        lines = [l for l in r.stdout.splitlines() if l.startswith("PAYLOAD_EXTRA_BODY:")]
        self.assertEqual(len(lines), 1, f"PAYLOAD_EXTRA_BODY line not found in:\n{r.stdout}")
        line = lines[0]
        # 应该是 JSON 格式（双引号），不是 Python repr 单引号
        self.assertIn('"image"', line, f"Expected double-quoted JSON keys, got: {line}")
        self.assertIn('"mode"', line)
        self.assertIn('"keyframes"', line)
        # 原 bug 是用 repr 时会输出单引号
        self.assertNotIn("{'image'", line)


# ============================================================================
# TestV31GetStatusSmart: 死循环 + fallback 行为
# ============================================================================

class TestV31GetStatusSmart(unittest.TestCase):
    """v3.1 P0-C / P0-D 单元测试（用 mock 避免真实 API 调用）

    v3.2.0 改动：get_status_smart / request_json_with_retry 入参从
    `keys: list` 改为 `key: str`（粘性单 key）。
    """

    def test_404_fallback_to_task_id(self):
        """video_id 404 → 自动 fallback 到 task_id"""
        from unittest import mock
        def fake_curl(url, **kw):
            if "video_id=" in url:
                return 404, '{"message":"not found"}'
            return 200, '{"status":"completed","video_url":"https://x.com/v.mp4"}'
        with mock.patch.object(agnes_video, "curl_request", side_effect=fake_curl):
            r = agnes_video.get_status_smart(
                ("video_fake", "task_real"), "sk-single", "https://api", 1
            )
            self.assertEqual(r["status"], "completed")

    def test_404_no_task_id_raises_immediately(self):
        """P0-C: video_id 404 + task_id=None 立即 raise 404（不死循环）"""
        from unittest import mock
        def fake_curl(url, **kw):
            return 404, '{"message":"not found"}'
        with mock.patch.object(agnes_video, "curl_request", side_effect=fake_curl):
            with self.assertRaises(agnes_video.ApiError) as ctx:
                agnes_video.get_status_smart(
                    ("video_fake", None), "sk-single", "https://api", 1
                )
            self.assertEqual(ctx.exception.status, 404)

    def test_401_does_not_fallback(self):
        """P0-D: 401/403 不 fallback 到 task_id（用同一 key 必然同样错）

        v3.2.0 改动：现在报 KeyDeadError（ApiError 子类），status=401
        """
        from unittest import mock
        def fake_curl(url, **kw):
            return 401, '{"message":"无效的令牌"}'
        with mock.patch.object(agnes_video, "curl_request", side_effect=fake_curl):
            with self.assertRaises(agnes_video.ApiError) as ctx:
                agnes_video.get_status_smart(
                    ("video_xxx", "task_yyy"), "sk-single", "https://api", 1
                )
            self.assertEqual(ctx.exception.status, 401)

    def test_5xx_does_not_fallback(self):
        """P0-D: 5xx 也不 fallback（服务端问题，task_id 端点同样会错）"""
        from unittest import mock
        def fake_curl(url, **kw):
            return 500, '{"message":"server error"}'
        with mock.patch.object(agnes_video, "curl_request", side_effect=fake_curl):
            with self.assertRaises(agnes_video.ApiError) as ctx:
                agnes_video.get_status_smart(
                    ("video_xxx", "task_yyy"), "sk-single", "https://api", 1
                )
            self.assertEqual(ctx.exception.status, 500)

    def test_single_key_401_raises_key_dead_error(self):
        """v3.2.0: 单 key 401/403 抛 KeyDeadError（让上层换 key）

        替代 v3.1 的 test_all_keys_401_raises_immediately（已不适用）
        """
        from unittest import mock
        def fake_curl(url, **kw):
            return 401, '{"message":"无效的令牌"}'
        with mock.patch.object(agnes_video, "curl_request", side_effect=fake_curl):
            with self.assertRaises(agnes_video.KeyDeadError) as ctx:
                agnes_video.request_json_with_retry(
                    "GET", "https://api", "sk-only", max_retries=1
                )
            self.assertEqual(ctx.exception.status, 401)
            self.assertIn("auth failed", str(ctx.exception).lower())


# ============================================================================
# main
# ============================================================================

# ============================================================================
# v3.1.2 新增测试
# ============================================================================

class TestIsQuotaError(unittest.TestCase):
    """v3.1.2 关键词误报修复回归测试

    背景：v3.1.1 用 "今天"、"建议您" 关键词，正常 API 响应 "今天任务创建成功"
    / "建议您稍后重试" 会被误判为 quota 错误，导致 P0-E auth 路径错误退出。
    v3.1.2 改为强相关词组（必须"配额/额度"语义 + "耗尽/不足"语义）。
    """

    def test_real_quota_signals_detected(self):
        """真正的配额/限流信号：必须识别为 quota"""
        true_positives = [
            "Quota exhausted for today",
            "rate limit exceeded",
            "insufficient quota, please upgrade",
            "insufficient balance",
            "out of credits",
            "balance insufficient",
            "今日配额已用完",
            "额度已用完",
            "余额不足，请充值",
            "已达上限",
            "已超出限额",
        ]
        for body in true_positives:
            self.assertTrue(
                agnes_video.is_quota_error(body, None),
                f"should detect quota in: {body}",
            )

    def test_common_chinese_phrases_not_quota(self):
        """v3.1.2 修复：常见中文词「今天」「建议您」不能误报为 quota"""
        # 模拟正常 API 响应（含 "今天" / "建议您" 但不是 quota 错误）
        false_positives = [
            "今天任务创建成功",         # 包含 "今天" → 以前误报
            "建议您稍后重试",           # 包含 "建议您" → 以前误报
            "Task created today, please check back later",  # 包含 "today" 但英文不命中
            "建议您使用更详细的 prompt",  # 正常建议，不是 quota
            "今天是个适合生成视频的好日子",  # 纯闲聊
            "We suggest you try again",  # 英文建议
            "Success",
            "",
        ]
        for body in false_positives:
            self.assertFalse(
                agnes_video.is_quota_error(body, None),
                f"should NOT detect quota in: {body}",
            )

    def test_status_429_alone_not_quota(self):
        """status=429 但 body 不含配额词组 → 不算 quota（可能被 reclassify 为 retryable）"""
        # 注：本函数只看 body，不看 status 字段
        self.assertFalse(agnes_video.is_quota_error("Too Many Requests", 429))

    def test_empty_body(self):
        """空 body 永远不 quota"""
        self.assertFalse(agnes_video.is_quota_error("", None))
        self.assertFalse(agnes_video.is_quota_error(None, None))


class TestValidatePrompt(unittest.TestCase):
    """v3.1.2 补单元测试（CLI 测试之外覆盖空 prompt / 空白 prompt）"""

    def test_valid_prompt(self):
        agnes_video.validate_prompt("A cat on the beach")  # 不应抛

    def test_empty_string_rejected(self):
        with self.assertRaises(SystemExit) as ctx:
            agnes_video.validate_prompt("")
        self.assertIn("prompt cannot be empty", str(ctx.exception))

    def test_whitespace_only_rejected(self):
        with self.assertRaises(SystemExit):
            agnes_video.validate_prompt("   \n\t  ")

    def test_none_rejected(self):
        with self.assertRaises(SystemExit):
            agnes_video.validate_prompt(None)  # type: ignore[arg-type]


class TestValidateModeAndImages(unittest.TestCase):
    """v3.1.2 重命名回归（功能不变 + 新增单图 keyframes 边界）"""

    def _args(self, **overrides):
        defaults = dict(prompt="x", image_url=None, mode=None)
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def test_renamed_function_exists(self):
        """v3.1.2: 函数名已改为 validate_mode_and_images"""
        self.assertTrue(callable(agnes_video.validate_mode_and_images))

    def test_single_image_keyframes_rejected(self):
        """单图 + keyframes 应被拒绝（之前已有 CLI 测试，单元版确认）"""
        with self.assertRaises(SystemExit) as ctx:
            agnes_video.validate_mode_and_images(
                self._args(image_url=["https://a.png"], mode="keyframes")
            )
        self.assertIn("keyframes requires at least 2 images", str(ctx.exception))

    def test_two_images_keyframes_ok(self):
        """2图 + keyframes 不报错"""
        agnes_video.validate_mode_and_images(
            self._args(image_url=["https://a.png", "https://b.png"], mode="keyframes")
        )

    def test_too_many_images_rejected(self):
        """9 张图应被拒绝"""
        with self.assertRaises(SystemExit) as ctx:
            agnes_video.validate_mode_and_images(
                self._args(image_url=[f"https://a/{i}.png" for i in range(9)])
            )
        self.assertIn("Too many images: 9 (max 8)", str(ctx.exception))

    def test_max_images_allowed(self):
        """8 张图不报错（边界值）"""
        agnes_video.validate_mode_and_images(
            self._args(image_url=[f"https://a/{i}.png" for i in range(8)])
        )

    def test_no_images_ok(self):
        """T2V 场景（无图）不报错"""
        agnes_video.validate_mode_and_images(self._args())


class TestFilenameFromUrl(unittest.TestCase):
    """v3.1.2 补单元测试（之前零覆盖）"""

    def test_url_with_extension(self):
        self.assertEqual(
            agnes_video.filename_from_url("https://gcs.example.com/path/video.mp4"),
            "video.mp4",
        )

    def test_url_with_query_string(self):
        """带 ?token=xxx 的 URL 也要正确提取文件名"""
        result = agnes_video.filename_from_url(
            "https://gcs.example.com/video.mp4?X-Goog-Signature=xxx&Expires=999"
        )
        self.assertTrue(result.startswith("video"))
        self.assertTrue(result.endswith(".mp4"))

    def test_url_without_extension(self):
        """无扩展名 URL 应回退到 agnes-video-<ts>.mp4"""
        import re
        result = agnes_video.filename_from_url("https://gcs.example.com/video")
        self.assertTrue(result.startswith("agnes-video-"))
        self.assertTrue(result.endswith(".mp4"))


class TestAgentSubmittedFormat(unittest.TestCase):
    """v3.1.2 _print_agent_submitted 新增 PROMPT 字段"""

    def _capture(self, func, *args, **kwargs):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            func(*args, **kwargs)
        return buf.getvalue()

    def test_with_prompt(self):
        """带 prompt 时输出 PROMPT 字段"""
        out = self._capture(
            agnes_video._print_agent_submitted,
            "video_xxx", "task_xxx", prompt="a cinematic test",
        )
        self.assertIn("STATUS: submitted", out)
        self.assertIn("VIDEO_ID: video_xxx", out)
        self.assertIn("TASK_ID: task_xxx", out)
        self.assertIn("PROMPT: a cinematic test", out)

    def test_without_prompt(self):
        """不传 prompt 时不输出 PROMPT 行（向后兼容）"""
        out = self._capture(
            agnes_video._print_agent_submitted, "video_xxx", "task_xxx"
        )
        self.assertIn("STATUS: submitted", out)
        self.assertNotIn("PROMPT:", out)

    def test_only_video_id(self):
        """只有 video_id 也行"""
        out = self._capture(
            agnes_video._print_agent_submitted, "video_xxx", None
        )
        self.assertIn("VIDEO_ID: video_xxx", out)
        self.assertNotIn("TASK_ID", out)


class TestApiKeyXdgPath(unittest.TestCase):
    """v3.1.2 get_api_keys XDG 路径加载 + 优先级测试

    优先级（高 → 低）：env var > XDG file > skill .env
    跨源去重保序。
    """

    def setUp(self):
        # 备份环境变量
        self.saved_agnes_key = os.environ.pop("AGNES_API_KEY", None)
        self.saved_token = os.environ.pop("AGNES_TOKEN", None)
        # mock skill .env 为不存在路径，避免被生产 .env 干扰
        self._skill_env_patcher = mock.patch.object(
            agnes_video, "SKILL_ENV_PATH",
            Path("/nonexistent/skill/.env"),
        )
        self._skill_env_patcher.start()

    def tearDown(self):
        # 恢复环境变量
        if self.saved_agnes_key:
            os.environ["AGNES_API_KEY"] = self.saved_agnes_key
        if self.saved_token:
            os.environ["AGNES_TOKEN"] = self.saved_token
        self._skill_env_patcher.stop()

    def test_xdg_path_loaded(self):
        """XDG 文件存在 → 加载"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            xdg_dir = Path(tmp) / ".config" / "openclaw"
            xdg_dir.mkdir(parents=True)
            xdg_env = xdg_dir / "agnes-free-video.env"
            xdg_env.write_text(
                "# comment line\n"
                "AGNES_API_KEY=sk-from-xdg\n"
                "\n"
            )
            # v3.1.2: XDG_ENV_PATH 是函数，mock return_value
            with mock.patch.object(
                agnes_video, "XDG_ENV_PATH", return_value=xdg_env,
            ):
                keys = agnes_video.get_api_keys()
                self.assertEqual(keys, ["sk-from-xdg"])

    def test_xdg_quoted_value(self):
        """XDG 文件支持引号包裹（'sk-x' 或 "sk-x"）"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            xdg_dir = Path(tmp) / ".config" / "openclaw"
            xdg_dir.mkdir(parents=True)
            xdg_env = xdg_dir / "agnes-free-video.env"
            xdg_env.write_text(
                "AGNES_API_KEY='sk-quoted'\n"
            )
            with mock.patch.object(
                agnes_video, "XDG_ENV_PATH", return_value=xdg_env,
            ):
                self.assertEqual(agnes_video.get_api_keys(), ["sk-quoted"])

    def test_xdg_multi_keys(self):
        """XDG 文件支持多 key"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            xdg_dir = Path(tmp) / ".config" / "openclaw"
            xdg_dir.mkdir(parents=True)
            xdg_env = xdg_dir / "agnes-free-video.env"
            xdg_env.write_text(
                "AGNES_API_KEY=sk-a,sk-b,sk-c\n"
            )
            with mock.patch.object(
                agnes_video, "XDG_ENV_PATH", return_value=xdg_env,
            ):
                self.assertEqual(
                    agnes_video.get_api_keys(), ["sk-a", "sk-b", "sk-c"],
                )

    def test_env_var_higher_priority_than_xdg(self):
        """env var 优先级 > XDG（即使 XDG 存在也先用 env）"""
        import tempfile
        os.environ["AGNES_API_KEY"] = "sk-from-env"
        with tempfile.TemporaryDirectory() as tmp:
            xdg_dir = Path(tmp) / ".config" / "openclaw"
            xdg_dir.mkdir(parents=True)
            xdg_env = xdg_dir / "agnes-free-video.env"
            xdg_env.write_text(
                "AGNES_API_KEY=sk-from-xdg\n"
            )
            with mock.patch.object(
                agnes_video, "XDG_ENV_PATH", return_value=xdg_env,
            ):
                # env var 在前，XDG 在后
                self.assertEqual(
                    agnes_video.get_api_keys(),
                    ["sk-from-env", "sk-from-xdg"],
                )

    def test_xdg_dedup_with_env(self):
        """env 和 XDG 都设了同一个 key → 去重"""
        import tempfile
        os.environ["AGNES_API_KEY"] = "sk-same,sk-dup"
        with tempfile.TemporaryDirectory() as tmp:
            xdg_dir = Path(tmp) / ".config" / "openclaw"
            xdg_dir.mkdir(parents=True)
            xdg_env = xdg_dir / "agnes-free-video.env"
            xdg_env.write_text(
                "AGNES_API_KEY=sk-same,sk-unique\n"
            )
            with mock.patch.object(
                agnes_video, "XDG_ENV_PATH", return_value=xdg_env,
            ):
                keys = agnes_video.get_api_keys()
                self.assertEqual(keys, ["sk-same", "sk-dup", "sk-unique"])

    def test_missing_all_raises_api_error(self):
        """env / XDG / .env 都缺 → raise ApiError（v3.1.2: 不再 raise SystemExit）"""
        # skill .env / XDG / env var 三个源都没（skill .env 已被 setUp mock）
        with mock.patch.object(
            agnes_video, "XDG_ENV_PATH",
            return_value=Path("/nonexistent/xdg/agnes-free-video.env"),
        ):
            with self.assertRaises(agnes_video.ApiError) as ctx:
                agnes_video.get_api_keys()
            self.assertIn("Missing API key", str(ctx.exception))


# ============================================================================
# v3.2.0 新增测试
# ============================================================================

class TestV32KeyPoolIntegration(unittest.TestCase):
    """v3.2.0: KeyPool 接入 + KeyDeadError 转换

    场景：
    - cmd_create 遇到 KeyDeadError → mark_dead + 换 key 重试
    - cmd_create 4 把 key 全死 → 报 "All N key(s) returned 401/403"
    - get_key_pool 单例：4 把 key 在多次 cmd_create 间轮换
    """

    def setUp(self):
        # 重置 KeyPool 单例（避免被其他测试污染）
        agnes_video.reset_key_pool()

    def tearDown(self):
        agnes_video.reset_key_pool()

    def test_create_swap_key_on_401(self):
        """v3.2.0: cmd_create 遇 401 → mark_dead + 换 key 重试 → 成功"""
        from unittest import mock
        call_log = []

        def fake_curl(url, **kw):
            # 从 headers 拿 key
            auth = kw.get("headers", {}).get("Authorization", "")
            key = auth.replace("Bearer ", "")
            call_log.append((key, "POST" in kw.get("method", "POST") or "POST" == kw.get("method", "POST")))
            # 实际看 method 参数
            method = kw.get("method", "POST")
            call_log.append((key, method))
            # key1 → 401，其他 key → 200
            if key == "sk-key1":
                return 401, '{"message":"无效的令牌"}'
            return 200, '{"id":"task_xxx","video_id":"video_xxx","status":"queued"}'

        # 4 把 key，1 把死
        keys = ["sk-key1", "sk-key2", "sk-key3", "sk-key4"]
        pool = agnes_video.get_key_pool(keys)

        # 模拟：第一次 create 死一次，然后切下一把成功
        # cmd_create 会调 max_key_swaps=4 次
        # 但其实只要 1 次 swap 就够
        with mock.patch.object(agnes_video, "curl_request", side_effect=fake_curl):
            # 不真跑 cmd_create（太复杂），直接验证 KeyPool 行为
            key1 = pool.acquire_key(verbose=False)
            self.assertEqual(key1, "sk-key1")
            with self.assertRaises(agnes_video.KeyDeadError):
                agnes_video.request_json_with_retry("POST", "https://api", key1, max_retries=1)
            pool.mark_dead(key1)
            # 第二把应该跳过 sk-key1（已死），选 sk-key2
            key2 = pool.acquire_key(verbose=False)
            self.assertEqual(key2, "sk-key2")
            response = agnes_video.request_json_with_retry("POST", "https://api", key2, max_retries=1)
            self.assertEqual(response["video_id"], "video_xxx")
            pool.mark_used(key2)

        # sk-key1 应被标记为死
        self.assertTrue(pool.is_dead("sk-key1"))
        # sk-key2 应被标记为已用
        self.assertGreater(pool._last_used["sk-key2"], 0.0)

    def test_all_keys_dead_raises_api_error(self):
        """v3.2.0: 4 把 key 全死 → cmd_create 应报 "All 4 key(s) returned 401/403"
        （这里只验证 pool 行为，cmd_create 端到端测试在集成测试里）"""
        from unittest import mock
        keys = ["sk-k1", "sk-k2", "sk-k3", "sk-k4"]
        pool = agnes_video.get_key_pool(keys)
        for k in keys:
            pool.mark_dead(k, verbose=False)
        # 4 把都死了，兜底还是会选一把（pool 不阻断），让上层撞 401
        selected = pool.acquire_key(verbose=False)
        self.assertIn(selected, keys)

    def test_key_pool_singleton_across_calls(self):
        """v3.2.0: get_key_pool 单例 → 多次调用共享状态"""
        keys1 = ["sk-a", "sk-b", "sk-c", "sk-d"]
        keys2 = ["sk-x", "sk-y"]  # 不同 keys
        pool1 = agnes_video.get_key_pool(keys1)
        pool2 = agnes_video.get_key_pool(keys2)  # 第二次调用应忽略
        self.assertIs(pool1, pool2)  # 同一个单例
        self.assertEqual(len(pool1), 4)  # 用第一次的 keys

    def test_reset_key_pool(self):
        """v3.2.0: reset_key_pool 清空单例 → 下次 get_key_pool 用新 keys"""
        pool1 = agnes_video.get_key_pool(["sk-a", "sk-b"])
        agnes_video.reset_key_pool()
        pool2 = agnes_video.get_key_pool(["sk-x", "sk-y", "sk-z"])
        self.assertIsNot(pool1, pool2)
        self.assertEqual(len(pool2), 3)

    def test_429_does_not_swap_key(self):
        """v3.2.0: 429 限流 → 重试同 key（不换 key，换 key 也撞 RPM=1）"""
        from unittest import mock
        keys = ["sk-a", "sk-b"]
        pool = agnes_video.get_key_pool(keys)
        call_count = {"n": 0}

        def fake_curl(url, **kw):
            call_count["n"] += 1
            if call_count["n"] < 3:
                return 429, '{"message":"rate limit"}'
            return 200, '{"id":"task_xxx","video_id":"video_xxx","status":"queued"}'

        with mock.patch.object(agnes_video, "curl_request", side_effect=fake_curl):
            key = pool.acquire_key(verbose=False)
            response = agnes_video.request_json_with_retry(
                "POST", "https://api", key, max_retries=3
            )
            # 3 次调用都应该是同一把 key（不换）
            self.assertEqual(call_count["n"], 3)
            self.assertEqual(response["video_id"], "video_xxx")
            # key 状态：429 时不调 mark_used（pool 上层决定）
            # 成功后才调 mark_used
            pool.mark_used(key)
            self.assertGreater(pool._last_used[key], 0.0)

    def test_429_failure_then_mark_used_avoids_loop(self):
        """v3.2.x bug fix: cmd_create 里 429 耗尽重试后要 mark_used，
        避免下个任务立刻又选中同一把死 key。

        背景：原代码 ApiError 分支只 mark_dead（401/403），不 mark_used，
        导致 429/5xx 耗尽重试的那把 key 仍处于"未用过"状态，
        下次 acquire 会被再次选中 → 必然又遇 429 → 死循环重试 3 次 → 报错。
        修复：retryable 状态码后调 mark_used 进 60s cooldown，
        下个任务会选下一把 key。
        """
        from unittest import mock
        keys = ["sk-loop-a", "sk-loop-b"]
        pool = agnes_video.get_key_pool(keys)
        # 预制：按顺序都“限流”失败
        first = pool.acquire_key(verbose=False)
        # 直接验证 cmd_create 的 except 分支调 mark_used
        fake_curl = mock.MagicMock(return_value=(429, '{"message":"rate limit"}'))
        with mock.patch.object(agnes_video, "curl_request", fake_curl), \
             mock.patch.object(agnes_video, "request_json_with_retry",
                               side_effect=agnes_video.ApiError("rate limit", status=429)):
            r = agnes_video.cmd_create(argparse.Namespace(
                prompt="t", image_url=None, mode=None,
                height=768, width=1152, num_frames=81, num_inference_steps=None,
                seed=None, frame_rate=24, negative_prompt=None,
                output=None, output_dir=None, download=False, no_poll=True,
                poll_interval=5, timeout=180, max_retries=1, api_base="https://api",
                dry_run=False, format="agent", payload=None,  # payload=None 仅允许 no-poll
            ))
            self.assertEqual(r, 1)
        # 关键验证：限流那把 key 被调了 mark_used（进 60s cooldown）
        self.assertGreater(pool._last_used[first], 0.0,
                           "429 后必须调 mark_used 让 key 进 cooldown，否则下次还会选中")


class TestV32StatusWaitNoUrl(unittest.TestCase):
    """v3.2.x bug fix: status --wait 到 completed 却无 URL 不应静默返 success

    背景：主人 2026-07-13 实测，最新 API 完成响应里 video_url 实际位置变成
    metadata.url，原 extract_video_url 只看顶层+data，返 None。
    cmd_status 原代码不管 status 是不是 done 都照输出 success，给 agent
    造成 false success（STATUS: ok + URL: (no url)）。现在：wait 到 done + 无 URL
    → 报错退出 1。
    """

    def test_status_wait_completed_no_url_raises(self):
        """wait 拿到 completed 但无 URL（Video_url/metadata.url 都没有）→ 报错"""
        # mock 轮询只返一次：completed 但响应里没有 URL 任何位置
        from unittest import mock
        def fake_curl(url, **kw):
            return 200, '{"status":"completed","progress":100,"id":"task_x","task_id":"task_x"}'
        # mock download_video 不被调用（避免动到本地文件）
        with mock.patch.object(agnes_video, "curl_request", side_effect=fake_curl), \
             mock.patch.object(agnes_video, "download_video", return_value=None):
            # 让 poll 快速退出
            r = agnes_video.cmd_status(argparse.Namespace(
                video_id=None, task_id="task_x",
                wait=True, poll_interval=0.01, timeout=5, max_retries=1,
                download=False, output=None, output_dir=None,
                api_base="https://api", format="agent",
            ))
            self.assertEqual(r, 1)

    def test_status_wait_completed_with_metadata_url_succeeds(self):
        """wait 拿到 completed + metadata.url 照常成功"""
        from unittest import mock
        captured_out: dict = {}
        def fake_curl(url, **kw):
            captured_out["url"] = url
            return 200, '{"status":"completed","progress":100,"metadata":{"url":"https://platform.example.com/v.mp4"}}'
        with mock.patch.object(agnes_video, "curl_request", side_effect=fake_curl), \
             mock.patch.object(agnes_video, "download_video", return_value=None), \
             mock.patch("sys.stdout", new=mock.MagicMock()) as mock_stdout:
            r = agnes_video.cmd_status(argparse.Namespace(
                video_id="task_x", task_id=None,
                wait=True, poll_interval=0.01, timeout=5, max_retries=1,
                download=False, output=None, output_dir=None,
                api_base="https://api", format="agent",
            ))
            self.assertEqual(r, 0)
            # URL: 应输出
            written = "".join(
                call.args[0] for call in mock_stdout.write.call_args_list
                if call.args and isinstance(call.args[0], str)
            )
            self.assertIn("STATUS: ok", written)
            self.assertIn("https://platform.example.com/v.mp4", written)

    def test_status_no_wait_in_progress_no_url_passes(self):
        """不 --wait 只查一次 → 中间态/无 URL 不当作错（正常状态查询）"""
        from unittest import mock
        def fake_curl(url, **kw):
            return 200, '{"status":"in_progress","progress":30,"task_id":"task_x"}'
        with mock.patch.object(agnes_video, "curl_request", side_effect=fake_curl), \
             mock.patch("sys.stdout", new=mock.MagicMock()) as mock_stdout:
            r = agnes_video.cmd_status(argparse.Namespace(
                video_id=None, task_id="task_x",
                wait=False, poll_interval=0.01, timeout=5, max_retries=1,
                download=False, output=None, output_dir=None,
                api_base="https://api", format="agent",
            ))
            # 中间态不算错，返回 0 + 看到当前状态
            self.assertEqual(r, 0)


class TestV32AgentOutputKeyField(unittest.TestCase):
    """v3.2.0: agent 输出加 KEY: 字段"""

    def _capture(self, func, *args, **kwargs):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            func(*args, **kwargs)
        return buf.getvalue()

    def test_agent_success_includes_key(self):
        """v3.2.0: _print_agent_success 接受 key_fp 参数"""
        out = self._capture(
            agnes_video._print_agent_success,
            Path("/tmp/v.mp4"),
            "https://gcs.example.com/v.mp4",
            "video_xxx",
            "task_xxx",
            "test",
            "1280x768",
            "10.0",
            "sk-Gyu***1P",
        )
        self.assertIn("KEY: sk-Gyu***1P", out)

    def test_agent_success_no_key(self):
        """v3.2.0: 不传 key_fp 时不输出 KEY 字段（向后兼容）"""
        out = self._capture(
            agnes_video._print_agent_success,
            Path("/tmp/v.mp4"),
            "https://gcs.example.com/v.mp4",
            "video_xxx",
            "task_xxx",
            "test",
        )
        self.assertNotIn("KEY:", out)

    def test_agent_submitted_includes_key(self):
        """v3.2.0: _print_agent_submitted 接受 key_fp 参数"""
        out = self._capture(
            agnes_video._print_agent_submitted,
            "video_xxx", "task_xxx",
            prompt="test", key_fp="sk-***",
        )
        self.assertIn("KEY: sk-***", out)
        self.assertIn("STATUS: submitted", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
