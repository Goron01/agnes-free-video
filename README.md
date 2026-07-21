# agnes-free-video

> 用 Agnes Video V2.0 免费生成视频（文生视频 / 图生视频 / 关键帧动画）

## 功能

- **T2V 文生视频**（Text-to-Video）：输入文字描述生成视频
- **I2V 图生视频**（Image-to-Video）：输入图片 + 文字描述生成视频
- **关键帧动画**：多图关键帧驱动的视频生成
- **异步轮询**：支持 `status --video-id` / `status --task-id` 查询
- **多 Key 轮用**（v3.2.0+ KeyPool 粘性轮换）：支持配置多个 API Key 轮换，绕开 RPM=1 限制
- **视频下载**：生成完成后自动下载到本地（默认 `/home/goron/文档/Openclaw/输出/agnes-free-video/`）

## 触发词

文生视频、图生视频、关键帧

## 安装

```bash
# 方式 A（推荐）：XDG 全局配置 — 真 key 不被 skill 目录的 git/sync/backup 误带走
mkdir -p ~/.config/openclaw && chmod 700 ~/.config/openclaw
cat > ~/.config/openclaw/agnes-free-video.env <<EOF
AGNES_API_KEY=sk-xxx
EOF
chmod 600 ~/.config/openclaw/agnes-free-video.env

# 方式 B（兼容）：skill 本地 .env
cp .env.example .env
chmod 600 .env
# 编辑 .env 写入 AGNES_API_KEY=sk-xxx

# 方式 C：临时环境变量
export AGNES_API_KEY=sk-xxx
```

多 key 用逗号分隔：`AGNES_API_KEY=sk-a,sk-b,sk-c,sk-d`

环境变量名等价：`AGNES_TOKEN` 也可用（与 `AGNES_API_KEY` 同义，脚本同时认两者）

## 快速开始

脚本是 subcommand 结构（`create` / `status`），不是 positional arg。

```bash
# 1. 文生视频（T2V）
python3 scripts/agnes_video.py create \
  --prompt "A cinematic shot of a cat walking on the beach at sunset" \
  --download

# 2. 图生视频（I2V）
python3 scripts/agnes_video.py create \
  --prompt "The woman slowly turns around and looks back at the camera" \
  --image-url "https://example.com/photo.png" \
  --download

# 3. 关键帧动画（多图 + mode=keyframes）
python3 scripts/agnes_video.py create \
  --prompt "Smooth cinematic transition between the keyframes" \
  --image-url "https://example.com/k1.png" \
  --image-url "https://example.com/k2.png" \
  --mode keyframes \
  --download

# 4. 查询已有任务（官方推荐 video_id，task_id 兜底）
python3 scripts/agnes_video.py status --video-id video_xxx --wait --download
python3 scripts/agnes_video.py status --task-id task_xxx --wait
```

更多详情见 [`SKILL.md`](SKILL.md)（默认输出格式 / Agent 视角 4 种工作流 / I2V 参考图来源 / 关键参数速查）。

## 目录结构

```
agnes-free-video/
├── SKILL.md              # Skill 入口文档（agent 视角必读）
├── Changelog.md          # 版本变更历史
├── README.md             # 本文件
├── scripts/
│   ├── agnes_video.py    # 主脚本（subcommand 结构：create / status）
│   └── setup.sh          # 交互式配 Key 脚本
├── lib/
│   ├── http_client.py    # HTTP 客户端（curl 子进程，绕开沙箱卡死）
│   └── key_pool.py       # v3.2.0: Key 池（粘性轮换）
├── agents/               # Agent 配置
├── tests/                # 测试用例（131 个）
└── references/
    └── api.md            # API 详细字段/状态码/Prompt 模板
```

## API 说明

- **API**：Agnes Video V2.0
- **模型**：`agnes-video-v2.0`
- **费用**：免费（$0/秒）
- **配额**：与 agnes-free-image 共享同一 API key quota 池
- **查询接口（推荐）**：`GET /agnesapi?video_id=<VIDEO_ID>&model_name=agnes-video-v2.0`
- **查询接口（兼容）**：`GET /v1/videos/{task_id}`

## 版本

当前版本：**v3.2.2**（131 个测试全过）
- **v3.2.2**（agent 视角 + 客户端校验）：数值范围 / URL 校验 / status 空 ID 拒绝
- **v3.2.1**（agent 视角 P0/P1 修复）：metadata.url / wait+no URL / setup.sh cwd / 429 mark_used
- **v3.2.0**（KeyPool 粘性轮换）：4 把 key 走 KeyPool，突破 RPM=1 限制
- **v3.1.2 / v3.1.1 / v3.1.0**：见 [Changelog.md](Changelog.md)