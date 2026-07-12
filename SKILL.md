---
name: agnes-free-video
version: 3.2.0
description: "Agnes-Video 文生视频/图生视频/关键帧。v3.2.0 起 4 key 粘性轮换（RPM=1 安全）。触发：文生视频、图生视频"
---

# Agnes Free Video

Use this skill to create and poll asynchronous video generation tasks with `agnes-video-v2.0` (Agnes AI 的免费视频模型，$0/秒)。

## When to Use

- 用户要 **T2V / I2V / 多图视频 / 关键帧动画** 中的任何一种
- 用户提到 `Agnes` / `agnes-video-v2.0` / "免费的视频生成"
- 用户已有 `video_id` 或 `task_id` 想查询状态
- 用户给了图 URL 想要"让这张图动起来"（I2V 首选）

**不要用**：用户要更专业的模型（如 Sora / Veo / 可灵）→ 改用其他 skill；只要图片 → 用 `agnes-free-image`。

## First-Time Setup

**方式 A（v3.1.2 推荐）：XDG 全局配置** — 真 key 不会被 skill 目录的 git/sync/backup 误带走

```bash
mkdir -p ~/.config/openclaw && chmod 700 ~/.config/openclaw
cat > ~/.config/openclaw/agnes-free-video.env <<EOF
AGNES_API_KEY=sk-你的真实key
EOF
chmod 600 ~/.config/openclaw/agnes-free-video.env

# 验证（无需 source，脚本自动加载 XDG 文件）
python3 scripts/agnes_video.py status --task-id smoke-test --format agent
# 看到 STATUS: error + MESSAGE: task_not_exist (HTTP 400) 就是正常的
# (API 把 "任务不存在" 返 400，不是 404；只要不是 401/403 就说明 key 通了)
```

**方式 B（向后兼容）：skill 本地 .env**

```bash
# 1. 配 key（编辑 .env 写入你的 agnes API key）
cp .env.example .env
chmod 600 .env
nano .env   # 写入 AGNES_API_KEY=sk-xxx

# 2. 注入到当前 shell
set -a && source ./.env && set +a

# 3. 验证（同上）
python3 scripts/agnes_video.py status --task-id smoke-test --format agent
```

**方式 C：临时环境变量**

```bash
export AGNES_API_KEY=sk-xxx
python3 scripts/agnes_video.py status --task-id smoke-test --format agent
```

**优先级（高 → 低）**：环境变量 > XDG 全局 > skill 本地 .env（跨源自动合并去重）

支持多 key 轮换（逗号分隔）：`AGNES_API_KEY=sk-a,sk-b,sk-c`

## Quick Start (Agent 视角)

**默认输出格式：`agent`**，stdout 输出结构化 `STATUS/PATH/URL/VIDEO_ID/...`，错误也走 stdout（agent 一定看得到）。

### 1. 文生视频 (T2V)

```bash
python3 scripts/agnes_video.py create \
  --prompt "A cinematic shot of a cat walking on the beach at sunset, soft ocean waves, warm golden lighting" \
  --num-frames 121 --frame-rate 24 --download
```

### 2. 图生视频 (I2V) — 首选用于"让图片动起来"

```bash
python3 scripts/agnes_video.py create \
  --prompt "The woman slowly turns around and looks back at the camera, natural motion" \
  --image-url "https://example.com/photo.png" \
  --download
```

### 3. 多图视频 / 关键帧动画

```bash
python3 scripts/agnes_video.py create \
  --prompt "Smooth cinematic transition between the keyframes" \
  --image-url "https://example.com/k1.png" \
  --image-url "https://example.com/k2.png" \
  --mode keyframes \
  --download
```

### 4. 查询已有任务（**用 video_id，新 API 推荐**）

```bash
python3 scripts/agnes_video.py status --video-id video_xxx --wait --download
# task_id 也兼容（兜底）：
python3 scripts/agnes_video.py status --task-id task_xxx --wait
```

### 5. 提交后立即返回（批量场景）

```bash
python3 scripts/agnes_video.py create --prompt "..." --no-poll
# stdout: STATUS: submitted \n VIDEO_ID: video_xxx \n TASK_ID: task_xxx
```

## Agent 输出格式 (默认)

```text
STATUS: ok
PATH: /home/goron/文档/Openclaw/输出/agnes-free-video/agnes-video-1234.mp4
URL: https://storage.googleapis.com/agnes-aigc/.../video_xxx.mp4
VIDEO_ID: video_xxx
TASK_ID: task_xxx
SIZE: 1280x768
SECONDS: 10.0
PROMPT: A cinematic shot of ...
```

错误时：
```text
STATUS: error
MESSAGE: Agnes API quota exhausted (HTTP 401): ...
HTTP_STATUS: 401
```

切换格式（`--format {agent|json|human}`）：
- `agent` (默认)：结构化字段，agent 解析友好
- `json` / `human`：dump 完整 API 响应到 stdout，错误到 stderr（调试用）

## 4 种工作流选哪个

| 用户需求 | Workflow | 关键参数 |
| --- | --- | --- |
| 纯文字描述 → 视频 | T2V | `--prompt` |
| 给一张图 → 动起来 | I2V | `--prompt` + `--image-url` × 1 |
| 给多张参考图 → 融合视频 | Multi-Image | `--image-url` × N（无 `--mode`） |
| 给首尾帧 → 平滑过渡 | Keyframes | `--image-url` × N + `--mode keyframes` |

## I2V 参考图来源（v3.1.1 新增）

`--image-url` / `--image-url × N` **必须是公网可访问的 HTTPS URL**，不接受本地路径 / base64。

#### ⭐ 优先复用 agnes-free-image 产出的图（推荐，省一次图床）

`agnes_image.py` 返回的 `URL:` 字段（`https://storage.googleapis.com/agnes-aigc/.../xxx.png`）**本身就是公网 HTTPS 直链**，可以直接喂给本 skill。**实测验证**（2026-06-06）：T2I 拿到的 URL 直接喂给 I2V，5-15 分钟顺利出视频，**完全跳过 catbox.moe / 0x0.st / gofile.io / file.io**。

```bash
# 第 1 步：用 agnes-free-image 生图（顺便解决角色一致性问题——先生成一张定妆照）
URL1=$(python3 ../agnes-free-image/scripts/agnes_image.py generate \
  --prompt "..." --format agent | grep '^URL:' | awk '{print $2}')

# 第 2 步：直接当 I2V 参考图用（不传 catbox！）
python3 scripts/agnes_video.py create \
  --prompt "The woman slowly turns around and looks back at the camera" \
  --image-url "$URL1" \
  --download
```

适用场景：想给小美做视频、想做"同角色不同动作"系列、想保证视频首帧画质（避免 T2V 卡 30% 概率出废片）。

#### 本地图（截图 / 照片）才走图床

非 Agnes 来源的本地图，需要先上传到公网图床：

1. **catbox.moe**（最稳，curl 一次即可，免费无注册，文件永久）
   ```bash
   curl -F "reqtype=fileupload" -F "fileToUpload=@/path/to/local.png" \
     https://catbox.moe/user/api.php
   # 返回的就是直链 URL，可直接用作 --image-url
   ```
2. 0x0.st（极简，不稳定）
3. tmpfiles.org（临时，1小时过期）

**注意**：catbox 偶尔连不上（OpenClaw 沙箱网络问题），如失败换 0x0.st 或再试一次。

**Prompt 通用结构**：`[主体] + [动作] + [场景] + [镜头] + [光照] + [风格]`

## 关键参数速查

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--num-frames` | 121 | 必须 `<= 441` 且 `8n+1`：81/121/161/241/441 |
| `--frame-rate` | 24 | 1-60，越高越流畅但视频越短 |
| `--width` × `--height` | 1152×768 | 宽高 |
| `--poll-interval` | 5s | 轮询间隔（新 API 文档推荐） |
| `--timeout` | 1800s | 30 分钟（队列积压考虑） |
| `--max-retries` | 3 | 可重试错误重试轮数（5xx/429/网络） |
| `--no-poll` | - | 提交后立即返回（batch 场景） |
| `--dry-run` | - | 打印请求 JSON 不真发请求 |
| `--output` | - | 单文件输出路径（覆盖 `--output-dir`） |

## 默认输出路径

生成的视频文件默认保存到：
```
/home/goron/文档/Openclaw/输出/agnes-free-video/
```

可通过 `--output`（单文件路径）或 `--output-dir` 覆盖。

## 注意事项

- **共享 quota 提醒**：`agnes-video-v2.0` 与 `agnes-free-image` 用同一把 key 时 quota 共池，video 用多了 image 会失败。
- **角色一致性限制**：`agnes-video-v2.0` 是通用模型，**无角色一致性**。想稳定生成小美需要 I2V 工作流（先用 `sn-image-base` 拿到小美参考图，再以图生视频）。
- **I2V 优先于 T2V**：实测 T2V 偶尔卡 30% 不动，I2V 几乎每次都顺利跑完。**做小美视频的推荐工作流**：先 `agnes-free-image` / `xiaomei-art` 生一张定妆照 → 拿 `URL:` → 直接 I2V。详见上文 "I2V 参考图来源" 节。
- **队列积压**：晚高峰可能 15+ 分钟仍在 `queued`，这是上游问题。脚本默认超时 30 分钟够用。
- **不要硬编码 key**：调用前 `set -a && source ./.env && set +a` 注入，或在 shell rc 里 export。
- **video_url 仅在 `completed` 时可用**。

## Reference

- **API 详细字段/状态码/Prompt 模板**：[`references/api.md`](references/api.md)
- **HTTP 客户端实现**：[`lib/http_client.py`](lib/http_client.py)（curl 子进程绕开沙箱卡死）

> 📋 完整更新日志见 **[Changelog.md](Changelog.md)**。当前版本 v3.1.2，最近修复：
> - **P0 安全增强**：`get_api_keys` 支持 XDG 全局路径 `~/.config/openclaw/agnes-free-video.env`（真 key 不被 skill 目录带走），向后兼容 skill .env + env var
> - **P1-B 关键词误报修复**：`is_quota_error` 删「今天」「建议您」中文常用词（正常 API 响应误报 100%），改用强相关词组（quota/balance/credit + exhausted/exceeded/reached/insufficient）
> - **P1-A setup.sh 验证步骤**：`HTTP_STATUS: 404` → `MESSAGE: task_not_exist (HTTP 400)`（同步 SKILL.md 实际行为）
> - **P1-A setup.sh 多 key regex**：原 regex 只匹配单 key，多 key 一直被问是否覆盖，已修复
> - **P1-C get_api_keys 统一 ApiError**：缺 key 时不再 raise SystemExit，改 raise ApiError 让 main() 统一输出 agent 格式错误
> - **P1-D 重命名** `validate_image_count` → `validate_mode_and_images`（函数名反映实际校验逻辑：mode + image 数量）
> - **P2-A VIDEO_URL_KEYS 注释**：明确 `remixed_from_video_id` 名字是 ID 但实际是 URL（官方文档字段名错乱）
> - **P2-B _print_agent_submitted 加 PROMPT 字段**：agent 拿到 submitted 后还能看到原始 prompt
> - **v3.2.0 KeyPool 粘性轮换**（突破 RPM=1 限制）：4 把 key 走 KeyPool（`lib/key_pool.py`），每任务粘一把 create + poll 全程用，4 把 key 顺序轮询。agent 输出新增 `KEY:` 字段。104 个测试全过（76 agnes_video + 28 key_pool）
