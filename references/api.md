# Agnes Free Video API Reference

> Source: Agnes-Video-V2.0 官方接入指南 (2026-06-06)
>
> 此文档是 `scripts/agnes_video.py` 的接口事实来源。脚本已自动处理多 Key、重试、视频 URL 解析等。

## Model

- **Name**: `agnes-video-v2.0`
- **Use cases**: text-to-video, image-to-video, multi-image video, keyframe animation, scene motion control, cinematic marketing clips, product demos, social videos.

## Endpoints

```text
# 创建任务
POST https://apihub.agnes-ai.com/v1/videos
Authorization: Bearer YOUR_API_KEY
Content-Type: application/json

# 查询结果（推荐方式，新 API）
GET  https://apihub.agnes-ai.com/agnesapi?video_id=<VIDEO_ID>[&model_name=<MODEL>]

# 查询结果（兼容方式，旧 API）
GET  https://apihub.agnes-ai.com/v1/videos/{task_id}
```

> **v3.0 重大变化**：创建任务响应会**同时返回 `task_id` 和 `video_id`**，新接入**强烈推荐使用 `video_id`** 查询。脚本默认优先 video_id，task_id 兜底。

## Create Task Fields

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `model` | string | ✅ | 固定 `agnes-video-v2.0` |
| `prompt` | string | ✅ | 视频文本描述 |
| `image` | string \| array | ❌ | 单图 URL 或多图 URL 数组 |
| `mode` | string | ❌ | `ti2vid`（图生视频，默认）/`keyframes`（关键帧，via `extra_body.mode`） |
| `height` | integer | ❌ | 默认 `768` |
| `width` | integer | ❌ | 默认 `1152` |
| `num_frames` | integer | ❌ | 必须 `<= 441` 且满足 `8n + 1` |
| `frame_rate` | number | ❌ | 范围 `1-60`，默认 `24` |
| `num_inference_steps` | integer | ❌ | 推理步数 |
| `seed` | integer | ❌ | 复现种子 |
| `negative_prompt` | string | ❌ | 反向提示词 |
| `extra_body.image` | array | ❌ | 多图/关键帧输入图片 URL 数组 |
| `extra_body.mode` | string | ❌ | 关键帧模式设 `"keyframes"` |

## 4 种工作流

### 1. 文生视频 (Text-to-Video)

```json
{
  "model": "agnes-video-v2.0",
  "prompt": "A cinematic shot of a cat walking on the beach at sunset, soft ocean waves, warm golden lighting, realistic motion",
  "height": 768,
  "width": 1152,
  "num_frames": 121,
  "frame_rate": 24
}
```

### 2. 图生视频 (Image-to-Video)

```json
{
  "model": "agnes-video-v2.0",
  "prompt": "The woman slowly turns around and looks back at the camera, natural facial expression, cinematic camera movement",
  "image": "https://example.com/image.png",
  "mode": "ti2vid",
  "num_frames": 121,
  "frame_rate": 24
}
```

> 脚本默认会把单图 + 非 keyframes 模式自动加上 `mode=ti2vid`。

### 3. 多图视频 (Multi-Image Video)

```json
{
  "model": "agnes-video-v2.0",
  "prompt": "Create a smooth transformation scene between the two reference images, cinematic lighting, consistent character identity, natural motion",
  "extra_body": {
    "image": [
      "https://example.com/image1.png",
      "https://example.com/image2.png"
    ]
  },
  "num_frames": 121,
  "frame_rate": 24
}
```

### 4. 关键帧动画 (Keyframe Animation)

```json
{
  "model": "agnes-video-v2.0",
  "prompt": "Generate a smooth cinematic transition between the keyframes, maintaining visual consistency and natural camera movement",
  "extra_body": {
    "image": [
      "https://example.com/keyframe1.png",
      "https://example.com/keyframe2.png"
    ],
    "mode": "keyframes"
  },
  "num_frames": 121,
  "frame_rate": 24
}
```

## Create Task Response

```json
{
  "id": "task_YOUR_TASK_ID",
  "task_id": "task_YOUR_TASK_ID",
  "video_id": "video_YOUR_VIDEO_ID",
  "object": "video",
  "model": "agnes-video-v2.0",
  "status": "queued",
  "progress": 0,
  "created_at": 1780457477,
  "seconds": "10.0",
  "size": "1280x768"
}
```

## Status Values

| Status | Meaning |
| --- | --- |
| `queued` | 排队中 |
| `in_progress` | 生成中 |
| `completed` | ✅ 已完成，可取 `video_url` |
| `failed` | ❌ 失败，详见 `error` 字段 |

## Result Fields (completed 时)

| Field | Type | Notes |
| --- | --- | --- |
| `id` | string | 任务 ID |
| `video_id` | string | 视频 ID（推荐查询用） |
| `model` | string | 使用的模型 |
| `object` | string | 通常 `"video"` |
| `status` | string | 任务状态 |
| `progress` | integer | 进度 0-100 |
| `seconds` | string | 视频时长（秒） |
| `size` | string | 视频分辨率 |
| `video_url` | string | **最终视频 URL**（completed 时可用） |
| `remixed_from_video_id` | string | 某些响应里**实际是视频 URL**（文档字段名错乱，脚本已智能识别） |
| `error` | object \| null | 失败时返回 |

> **脚本智能识别**：尝试 `video_url` → `url` → `remixed_from_video_id` → `video` → `output_url` → 嵌套 `data.*` 多种字段名。

## 视频时长控制

```
seconds = num_frames / frame_rate
```

约束：
- `num_frames <= 441`
- `num_frames` 必须 `8n + 1`
- `frame_rate ∈ [1, 60]`

| 目标时长 | num_frames | frame_rate |
| --- | --- | --- |
| ~3s | 81 | 24 |
| ~5s | 121 | 24 |
| ~10s | 241 | 24 |
| ~18s（最大） | 441 | 24 |

## 错误码

| Status | Meaning |
| --- | --- |
| 400 | 请求参数错误（检查 `num_frames` 等） |
| 401 | 未授权（检查 API Key；脚本会自动切下一个 key） |
| 404 | 任务/视频不存在 |
| 429 | 限流（脚本会等更久再重试） |
| 500 | 服务器错误（脚本会自动重试 3 次） |
| 503 | 服务繁忙（脚本会自动重试 3 次） |

## 价格

| 类型 | 标准 | 当前 |
| --- | --- | --- |
| Video Duration | $0.005/second | **$0/second**（免费） |

## Prompt 最佳实践

### 文生视频
**结构**: `[主体] + [动作] + [场景] + [镜头运动] + [光照] + [风格]`

示例：
```
A young astronaut walking across a red desert planet, dust blowing in the wind, slow cinematic tracking shot, dramatic sunset lighting, realistic sci-fi style
```

### 图生视频
描述**哪些动 + 哪些保持稳定**：
```
Animate the character with subtle breathing motion, hair moving gently in the wind, background lights flickering softly, while keeping the face and outfit consistent
```

### 多图视频
描述**图片关系 + 画面如何过渡**：
```
Use the first image as the starting scene and the second image as the target scene. Create a smooth transformation with consistent lighting, natural motion, and cinematic pacing
```

### 关键帧动画
清晰描述**关键帧之间的过渡**：
```
Create a smooth transition from the first keyframe to the second keyframe, maintaining character identity, consistent camera angle, and natural motion between scenes
```
