# agnes-free-video

> 用 Agnes Video V2.0 免费生成视频（文生视频 / 图生视频 / 关键帧动画）

## 功能

- **T2V 文生视频**（Text-to-Video）：输入文字描述生成视频
- **I2V 图生视频**（Image-to-Video）：输入图片 + 文字描述生成视频
- **关键帧动画**：多图关键帧驱动的视频生成
- **异步轮询**：支持异步任务状态查询
- **多 Key 轮用**：支持配置多个 API Key 轮换使用
- **视频下载**：生成完成后自动下载到本地

## 触发词

免费视频、生成视频、AI 视频、agnes-video、video generation

## 安装

```bash
# 复制环境变量模板
cp .env.example .env

# 编辑 .env，填入 API Key
AGNES_API_KEY=***
HTTP_PROXY=http://127.0.0.1:7897   # 视频下载必须走代理
```

## 快速开始

```bash
# 文生视频
python3 scripts/agnes_video.py "一只猫在草地上奔跑"

# 图生视频
python3 scripts/agnes_video.py "input.png" "转换成动画风格"

# 关键帧动画
python3 scripts/agnes_video.py "frame1.png,frame2.png,frame3.png" "平滑过渡"
```

## 目录结构

```
agnes-free-video/
├── SKILL.md              # Skill 入口文档
├── Changelog.md          # 版本变更历史
├── scripts/
│   └── agnes_video.py    # 主脚本
├── lib/
│   └── http_client.py    # HTTP 客户端（curl 封装）
├── agents/               # Agent 配置
├── tests/                # 测试用例
└── references/           # 参考资料
```

## API 说明

- **API**：Agnes Video V2.0
- **费用**：免费（$0/秒）
- **配额**：与 agnes-free-image 共享同一 API key quota 池
- **并发注意**：多 Key 用逗号分隔 `AGNES_API_KEY=***
- **视频下载**：必须走代理（HTTP_PROXY）

## 版本

当前版本：3.1.2