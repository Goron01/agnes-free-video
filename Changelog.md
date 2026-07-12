# Changelog

> 完整更新日志。当前版本见 [SKILL.md](SKILL.md) frontmatter `version` 字段。

---

## v3.2.0 (2026-07-12) — KeyPool 粘性轮换（突破 RPM=1 限制）

**背景**：主人 2026-07-12 报告**官方把每 key 的 RPM 改成 1**，而且加到了 4 把 key。v3.1 之前的多 key 轮换策略（失败才换 key）在 RPM=1 下基本失效：
- key1 刚 429 → 几秒后切 key2 → key2 也 429（共享 RPM 窗口）
- 轮询不粘性：1 个视频的轮询能把 4 把 key 的 RPM 全部烧光
- 跨调用不重选：4 把 key 实际只用 1 把，等于没轮换

**v3.2.0 核心：KeyPool 粘性轮换**（`lib/key_pool.py`）：

### 背景与动机

- 4 把 key × RPM=1 理论最大 4 RPM，但 v3.1 实现只能拿 1 RPM
- 需要「每任务粘性 + 跨 key 轮询」才能拿满 4 RPM
- 主人场景「看股票 + 偶尔发视频」决定走**纯内存版**（不磁盘持久化），后面并发场景再上 v3.3 磁盘锁

### P0 核心

- **新增 `lib/key_pool.py`**（独立模块，可复用于 agnes-free-image）：
  - `KeyPool` 类：last_used / dead_until per-key 状态
  - `acquire_key()` → 返回 last_used 最早的可用 key（从未用过优先）
  - `mark_used(key)` → 60s 冷却开始
  - `mark_dead(key)` → 401/403 后死 10 分钟（pool 跳这把）
  - `snapshot()` → 调试/agent 输出的 pool 状态快照（脱敏 fingerprint）
  - `_key_fingerprint(key)` → `sk-XXX***XX` 脱敏，stderr/agent 输出不漏完整 key

- **改 `request_json_with_retry` 签名**：`keys: list` → `key: str`
  - 本次请求全程只用一把 key（粘性）
  - 5xx/429/网络：指数退避重试同 key（不换 key，换 key 也撞 RPM）
  - 401/403：抛 `KeyDeadError(ApiError)` → 上层换 key 重试

- **改 `poll_task` / `get_status_smart` / `create_task`**：入参从 `keys: list` 改成 `key: str`
  - 轮询粘性：1 个视频的整个轮询周期用同一把 key（不重轮换 key）

- **改 `cmd_create` / `cmd_status`**：接入 KeyPool 单例（`get_key_pool`）
  - create：`pool.acquire_key()` → 1 把 key 用到底，遇 `KeyDeadError` 才换
  - 4 把 key 全死 → 报「All 4 key(s) returned 401/403」不沉默

### P1 agent 输出

- **`_print_agent_success` / `_print_agent_submitted` 加 `key_fp` 参数** → agent 输出新增 `KEY:` 字段
- 主人一眼能看到「这个视频是 sk-Gyu***fe 跑的」

### P1 文档

- SKILL.md 新增「**多 Key 轮换策略 (v3.2.0)**」一节
- 明确写清楚：背景、粘性策略、吞吐量计算、限制、单 key 向后兼容
- .env.example / SKILL.md / Changelog.md 三处同步

### P1 测试

- **28 个 KeyPool 单测**（`tests/test_key_pool.py`）：
  - TestKeyFingerprint (5)：脱敏边界
  - TestKeyPoolInit (4)：单/多/缺/去重
  - TestAcquireKey (6)：never-used 优先 / oldest 优先 / skip 冷却 / skip 死 / 兏底 / verbose
  - TestMarkUsed (3)：更新 / advance 时钟 / 未知 key 忽略
  - TestMarkDead (3)：跳过 / dead_ttl 后复活 / 未知 key
  - TestSnapshot (3)：alive/dead 计数 / cooldown_active / 不泄漏 key
  - TestStickyRotation (2)：4 key round-robin / 1 死 后 3 轮换
  - TestSingleKeyBackwardCompat (2)：单 key 永远返回 / repr 不漏
- **76 → 76 agnes_video 测试**（+8 新增）：
  - TestV32KeyPoolIntegration (5)：swap on 401 / 全死 / 单例 / reset / 429 不换
  - TestV32AgentOutputKeyField (3)：success 返 KEY / 无 key 不返 / submitted 返 KEY
- **全过 7.34s**

### 迁移 / 兼容性

- **接口**：`request_json_with_retry` / `get_status_smart` / `poll_task` / `create_task` 入参从 `keys: list` → `key: str`
  - 纯内部变更（本 skill CLI 是唯一入口）
  - 外部 agent 调用方无影响
- **多 key 配置**：依然在 `AGNES_API_KEY=sk-a,sk-b,sk-c,sk-d` 逗号分隔
- **单 key**：pool 自动退化，永远返回同一把 key（后向兼容）
- **agent 格式**：`KEY:` 字段是新增，不是替换（老脚本忽略不认识的字段）

### 已知限制

- **纯内存版**：跨进程不共享 pool。多进程同时跑可能撞 RPM。
- **v3.3+ TODO**（如主人需要并发跑）走磁盘 JSON + fcntl 文件锁

### 破坏性变更（Breaking Changes）

- `request_json_with_retry(method, url, keys, ...)` → `request_json_with_retry(method, url, key, ...)`
  - 仅本 skill 内部，外部用户无感
  - 旧代码（直接 import 这些函数）需改

---

## v3.1.2 (2026-06-10) — Agent 视角审查 9 项修复 + 安全增强 + 测试覆盖翻倍

**背景**：主人 2026-06-10 让小美用 agent 视角审查本 skill（方法论来自 MEMORY.md v3.1.2 验证过的 6 维度框架），跑了 42 个原回归测试 + 端到端 dry-run / status smoke test，发现 1 个 P0 安全增强 + 5 个 P1 重要问题 + 3 个 P2 优化。**逐条修复 + 测试覆盖翻倍（42 → 68）**。

**P0 安全增强**

- **P0-A get_api_keys 支持 XDG 全局路径**：
  - 新增优先级 2：`~/.config/openclaw/agnes-free-video.env`（700 父目录 + 600 文件权限）
  - 优先级清晰：env var > XDG 文件 > skill 本地 .env
  - 跨源自动合并去重保序
  - 真 key 放 XDG → 不被 skill 目录的 git/sync/backup 误带走
  - 向后兼容：老用户的 skill .env 仍生效（路径 3 兜底）
  - 引号包裹值支持：`AGNES_API_KEY='sk-x'` 和 `AGNES_API_KEY="sk-x"` 都 OK
  - 注释行 / 空行自动跳过
  - OSError 容错：XDG 文件读不到就 fallback 到下个源，不阻断
  - SKILL.md + .env.example + setup.sh 三处引导到 XDG 路径

**P1 重要问题修复**

- **P1-B is_quota_error 关键词误报**（**MEMORY.md 方法论 #3 命中**）：
  - 删 `"今天"`、`"建议您"`——这两个中文常用词在正常 API 响应里 100% 出现
    （如"今天任务创建成功"、"建议您稍后重试"），触发误报把正常响应误判为配额错误
  - 改用强相关词组：`quota exhausted`、`insufficient quota`、`rate limit exceeded`、`out of credits`、`balance insufficient`、`次数已用完`、`额度已用完`、`余额不足`、`已达上限`、`已超出限额`、`今日配额已用完` 等 16 个
  - 必须"配额/额度/余额"语义 + "耗尽/不足/超限"语义同时出现才算 quota
  - 加 4 个回归测试：`test_real_quota_signals_detected`（真配额信号识别）+ `test_common_chinese_phrases_not_quota`（中文常用词不误报）+ `test_status_429_alone_not_quota` + `test_empty_body`

- **P1-A setup.sh 验证步骤不一致**（**Changelog vs 主体一致性 bug**）：
  - v3.1.0 P1-D 改了 SKILL.md 的验证消息（`HTTP 400` + `task_not_exist`），但 `scripts/setup.sh` 末尾还停留在 `HTTP_STATUS: 404`
  - v3.1.2 同步：setup.sh 验证输出改 `MESSAGE: task_not_exist (HTTP 400)`，跟 SKILL.md 完全一致

- **P1-A setup.sh 多 key regex bug**：
  - 原 regex `^AGNES_API_KEY=sk-[A-Za-z0-9_-]{10,}` 只匹配单 key（字符类不含逗号）
  - 多 key 用户（`sk-a,sk-b,sk-c`）跑 setup.sh 永远被问"要覆盖吗？"
  - v3.1.2 改为 `^AGNES_API_KEY=(sk-[A-Za-z0-9_-]{10,})(,sk-[A-Za-z0-9_-]{10,})*$` 支持多 key

- **P1-C get_api_keys 统一 raise ApiError**：
  - v3.1.1 缺 key 时 raise SystemExit，让 main() 走 argparse 兜底（agent 模式可能看不到）
  - v3.1.2 改 raise ApiError，跟其他错误统一走 `_print_agent_error` + 退出码 1
  - cmd_create / cmd_status 的 try/except 同步从 SystemExit 改成 ApiError

- **P1-D 重命名 validate_image_count → validate_mode_and_images**：
  - 原名只体现 "image count" 校验，但实际还校验 mode（keyframes 至少 2 张图）
  - v3.1.2 改名更准确，并加 6 个回归测试（之前 0 直接单元测试，只有 CLI 间接测试）

**P2 优化**

- **P2-A VIDEO_URL_KEYS 加注释**：
  - `("video_url", "url", "remixed_from_video_id", "video", "output_url")` 里 `remixed_from_video_id` 名字看着像 ID，实际是 URL（官方文档字段名错乱）
  - v3.1.2 加注释提醒，避免后人误以为是 ID 而删掉

- **P2-B _print_agent_submitted 加 PROMPT 字段**：
  - v3.1.1 `--no-poll` 模式只输出 `STATUS/VIDEO_ID/TASK_ID`，agent 拿到 submitted 后看不到原始 prompt
  - v3.1.2 加 `prompt: str = ""` 参数（默认空，向后兼容），调用方传 `args.prompt`
  - 3 个回归测试（带 prompt / 不带 prompt / 只有 video_id）

- **P2-C SKILL.md + .env.example XDG 路径文档**：
  - SKILL.md First-Time Setup 增加"方式 A：XDG 全局配置（推荐）"完整步骤
  - .env.example 顶部加 XDG 配置示例 + 三种方式选择说明
  - setup.sh 暂未改（TODO：下一步）

**测试**

- **42 → 68 测试**（+26，+62% 覆盖），全过 1.48s
- **TestIsQuotaError (4)**：关键词误报回归 + 真配额信号识别
- **TestValidatePrompt (4)**：空字符串 / 空白 / None / 合法
- **TestValidateModeAndImages (6)**：重命名存在性 + 单图 keyframes / 双图 keyframes / 9 图拒 / 8 图边界 / 无图
- **TestFilenameFromUrl (3)**：有扩展名 / 带 query string / 无扩展名回退
- **TestAgentSubmittedFormat (3)**：带 prompt / 不带 prompt / 只有 video_id
- **TestApiKeyXdgPath (6)**：XDG 路径加载 / 引号包裹 / 多 key / env 优先 / 跨源去重 / 全缺 ApiError
- 现有 TestApiKey 重构：setUp/tearDown 加 `SKILL_ENV_PATH` / `XDG_ENV_PATH` mock，避免被生产 .env 干扰

**破坏性变更（Breaking Changes）**

- **get_api_keys 缺 key 错误类型变化**：v3.1.1 raise `SystemExit` → v3.1.2 raise `ApiError`。直接 import `get_api_keys` 的代码（仅本 skill 内部）已同步更新。外部用户无影响（CLI 行为不变，只是错误从 stderr 走 stdout）。
- **validate_image_count 函数重命名**：仅本 skill 内部使用，外部用户无影响。

**已知限制（沿用）**

- `video_url` 字段名官方文档示例错乱（视频 URL 在 `remixed_from_video_id` 字段），脚本已智能识别。
- 视频文件大（5s ≈ 几 MB），下载用 curl + 900s 超时，不做断点续传。
- key 共池警告：`agnes-free-image` 和 `agnes-free-video` 用同一把 key 时 quota 共池。
- **新增**：skill .env 包含真实 key 的用户，建议迁移到 XDG 路径（保留向后兼容，老路径继续生效）。

---

## v3.1.1 (2026-06-06) — ⭐ 文档优化：Agnes 自身图可直链 I2V

**背景**：2026-06-06 实跑发现 `agnes_image.py` 返回的 `URL:` 字段（`https://storage.googleapis.com/agnes-aigc/.../xxx.png`）**本身就是公网 HTTPS 直链**，可以直接喂给本 skill 的 `--image-url`，**完全跳过 catbox.moe / 0x0.st / gofile.io / file.io**。原 SKILL.md 没明说，agent 走 I2V 时容易先上传图床（多一次网络往返 + 第三方依赖）。

**变更**

- **O1 新增 "I2V 参考图来源" 节**：明确 `--image-url` 必须公网 HTTPS URL + 优先复用 agnes-free-image URL 字段 + 本地图走 catbox 兜底（按稳定性排序）
- **O2 配 2 个工作流模板**：链式 I2I → I2V 一气呵成（先 agnes_image 生图 → 拿 URL → 直接 I2V）
- **O3 注意事项加 "I2V 优先于 T2V"**：实测 T2V 偶尔卡 30% 不动，I2V 几乎每次都顺利跑完。**做小美视频的推荐工作流**：先 `xiaomei-art` 生定妆照 → 拿 URL → I2V
- **O4 SKILL.md frontmatter version**：`3.1.0` → `3.1.1`
- **无代码改动**：`scripts/agnes_video.py` 一行未动，纯文档优化

**验证**

- 实测 1 次：T2I → I2V 直链，10s 视频顺利出片，5-15 分钟完成
- 实测 1 次：agnes-free-image I2I 链式调用，URL 字段直传 `STATUS: ok`

**备份**：`.输出/skill-backups/agnes-free-video-v3.1.0-pre_v3.1.1-20260606_0815/`

---

## v3.1.0 (2026-06-06) — Agent 视角 12 项 bug 修复 + 元数据补全

**背景**：主人 2026-06-06 让小美用 agent 执行角度审查本 skill，跑了 28 个回归测试 + 实跑 N 种命令，发现 5 个 P0 + 5 个 P1 + 4 个 P2 问题。逐条修复如下。

**P0 严重 bug 修复**

- **P0-A dry-run 支持 `--format agent`**：v3.0 dry-run 完全忽略 `--format`，始终输出 JSON。v3.1 按 format 输出：agent 模式 `STATUS: ok` + `DRY_RUN: 1` + `PAYLOAD_*` 字段；json 模式保持原样。
- **P0-B `--no-poll` 跟 `--download`/`--output` 互斥**：v3.0 静默接受 `--no-poll --download` 但什么也不下。v3.1 报明确 agent 错误：`--no-poll cannot be combined with --download / --output / --output-dir`。
- **P0-C video_id 404 不再死循环到 timeout**：v3.0 `status --video-id video_fake` 会循环 30 分钟（task_id=None 时 fallback 路径走不下去，抛错被 except + sleep + continue 吞掉）。v3.1 `get_status_smart` 用 `tried_video_id` set 记录已 404 的 id，404+task_id=None 立即 raise 404。
- **P0-D fallback 只在 404 触发**：v3.0 video_id 端点 401 时 fallback 到 task_id 端点（同 key 必然也 401，浪费配额）。v3.1 只在 `exc.status == 404` 时才 fallback；401/403/5xx 直接抛。
- **P0-E 死循环错误信息明确化**：v3.0 N 个假 key 都 401 时走完 3 轮退避（1.5s+3s+6s=10.5s）后报 "Timed out"，实际是 auth 失效。v3.1 `request_json_with_retry` 在一轮内所有 key 都返回 401/403 时**立刻抛** `All N key(s) returned 401 (auth/quota issue). Check AGNES_API_KEY.`

**P1 重要问题修复**

- **P1-A `--mode keyframes` + 单图拒绝**：v3.0 单图 + keyframes 走顶层 `image` 路径，keyframes mode 字段被静默丢弃。v3.1 客户端校验：`--mode keyframes` 时 `len(image_url) >= 2`，否则报 `keyframes requires at least 2 images`。
- **P1-B argparse 错误走 agent 格式**：v3.0 错放参数（如 `create --video-id xxx`）走 argparse 错到 stderr + 退出码 2。v3.1 `main()` except `SystemExit(2)` 转成 `_print_agent_error("Invalid CLI arguments ...")` + 退出码 1。
- **P1-C 默认输出路径**：`./outputs/agnes-free-video/` (相对路径) → `/home/goron/文档/Openclaw/.输出/agnes-free-video/`（遵守 TOOLS.md 全局默认输出规则，跨 cwd 一致）。
- **P1-D SKILL.md 验证步骤改 400**：v3.0 写"HTTP_STATUS: 404"，实际 API 返 400 + "task_not_exist"。v3.1 改 `STATUS: error + MESSAGE: task_not_exist (HTTP 400)`。
- **P1-E description 缩短到 136 字节**：v3.0 description 334 字节（OpenClaw 规范 160 字节上限的 2 倍）。v3.1 精简为 136 字节 + 补 metadata `emoji: 🎬`。

**P2 优化**

- **P2-A `requires.bins: ["python3", "curl"]`**：让 OpenClaw 知道这个 skill 依赖的外部命令。
- **P2-B `primaryEnv: "AGNES_API_KEY"`**：让 OpenClaw 知道主环境变量名，UI 能正确提示配 key。
- **P2-C `emoji: "🎬"`**：技能列表展示更友好。
- **P2-D `VALID_NUM_FRAMES` range 起点 9 → 1**：v3.0 漏 n=0（num_frames=1），错误信息说 "8n+1" 但列表从 9 开始。v3.1 range(1, 442) 含 1。
- **P2-E 空 prompt 客户端拒绝**：`--prompt ""` 直接发请求浪费配额，v3.1 在 `build_payload` 早 reject。
- **P2-F image 数量客户端上限 8**：避免发 8+ 张图被 API 拒。
- **P2-G 补 14 个新 bug 回归测试**（见下）。
- **P2-H `_print_agent_success` / `_print_agent_submitted` 保持不变**，但加了新的 agent 格式 helper（dry-run 输出）。
- **P2-I `_print_agent_error` 错误信息加 `Check AGNES_API_KEY` 提示**（P0-E 配套）。

**测试**

- **原 28 → 现 42 测试**（+14），全过 1.30s。
  - **TestV31BugFixes (9 个)**：P0-A dry-run agent / P0-B no-poll+download / P0-B no-poll+output / P1-A keyframes+单图 / P1-B argparse / P2-E 空 prompt / P2-F image 太多 / P2-D num_frames=1 / P1-C 默认路径
  - **TestV31GetStatusSmart (5 个)**：P0-C 404 fallback / P0-C 404+None 立即 raise / P0-D 401 不 fallback / P0-D 5xx 不 fallback / P0-E 3 key 401 立即抛
- 修改 1 个旧测试（`test_status_requires_id`）：P1-B 行为变化，agent 模式从 stderr 改 stdout。

**破坏性变更（Breaking Changes）**

- **dry-run 输出格式变化**（P0-A）：`--format agent + --dry-run` 现在输出 `STATUS: ok + DRY_RUN: 1 + PAYLOAD_*` 而非 JSON。v3.0 agent 模式下 dry-run 输出不可解析。
- **`--no-poll` + `--download/--output` 互斥**（P0-B）：以前静默接受，现在报 agent 错误。如果用户真要用 `submitted` 拿 ID 后台轮询下载，需要拆成两次调用（`create --no-poll` → `status --video-id ... --download`）。
- **fallback 行为变化**（P0-D）：以前所有错误都 fallback 到 task_id，现在只 404 fallback。401/5xx 不再 fallback（节省配额 + 时间）。
- **SKILL.md frontmatter description 缩短**（P1-E）：从 334 字节 → 136 字节。技能列表显示更紧凑，触发词仍覆盖核心场景。

**已知限制**

- 沿用 v3.0：`video_url` 字段名官方文档示例错乱（视频 URL 在 `remixed_from_video_id` 字段），脚本已智能识别。
- 沿用 v3.0：视频文件大（5s ≈ 几 MB），下载用 curl + 900s 超时，不做断点续传。
- 沿用 v3.0：key 共池警告，`agnes-free-image` 和 `agnes-free-video` 用同一把 key 时 quota 共池。

---

## v3.0.0 (2026-06-06) — Agent 视角完整重构 + Agnes-Video-V2.0 新 API 接入

**背景**：官方更新了 Agnes-Video-V2.0 接入指南：
- 创建任务响应**同时返回 `task_id` 和 `video_id`**，新接入**强烈推荐 `video_id` 查询**
- 新查询接口 `GET /agnesapi?video_id=<VIDEO_ID>[&model_name=<MODEL>]`（取代旧的 `GET /v1/videos/{task_id}`，但后者仍兼容）
- `mode` 字段官方文档示例：图生视频 `mode=ti2vid`，关键帧 `extra_body.mode="keyframes"`

旧 v1.x 脚本只支持 task_id 端点、无重试、无多 Key、不支持 agent 格式输出。完全重构。

**P0 严重 bug**
- **P0-1 接入新 API video_id 查询**：`get_status_smart()` 优先调 `GET /agnesapi?video_id=...&model_name=agnes-video-v2.0`，失败兜底旧 `GET /v1/videos/{task_id}`。`extract_video_id()` 智能识别 `video_id` / `id` / `task_id` 三种字段名。
- **P0-2 多 Key 轮换**：`AGNES_API_KEY=sk-a,sk-b,sk-c` 逗号分隔自动轮换，1 key 也兼容。每轮重试依次尝试所有 key，遇到可重试错误（5xx/429/网络/401）切下一个。
- **P0-3 指数退避重试**：5xx/超时/网络错误重试 3 次（1.5s→3s→6s 退避），429/配额错误额外等 10s。4xx 业务错误（除 429）立即报错不重试。

**P1 重要**
- **P1-1 agent 格式输出**：新增 `--format {agent|json|human}` 参数，默认 `agent`（AI agent 是主要消费者）。Agent 模式 stdout 输出 `STATUS/PATH/URL/VIDEO_ID/TASK_ID/SIZE/SECONDS/PROMPT` 结构化字段，agent 解析无需 grep/regex。
- **P1-2 agent 模式错误走 stdout**：避免 `SystemExit` / `ApiError` 走 stderr 导致 agent 看不到。`get_api_keys()` 缺 key、payload 校验失败、API 错误都通过 `_print_agent_error()` 输出 `STATUS: error`。
- **P1-3 智能识别 video URL**：尝试 `video_url` → `url` → `remixed_from_video_id` → `video` → `output_url` 多种字段名 + 嵌套 `data.*` 兜底（官方文档字段名错乱，文档示例完成响应里把视频 URL 放在了 `remixed_from_video_id` 字段）。

**P2 优化**
- **P2-1 urllib → curl_request**：新建 `lib/http_client.py`，复用 `curl_request()` / `download_file()`。绕开 OpenClaw 沙箱内 Python urllib 卡死 30s 的问题（同 agnes-free-image 的修复）。
- **P2-2 mode 字段修复**：
  - 单图 + 非 keyframes → 自动加 `mode=ti2vid`（符合官方文档示例）
  - 多图 → `extra_body.image` 数组（无顶层 mode）
  - 关键帧 → `extra_body.mode=keyframes`
  - 旧的 `multi-image` 选项移除（多图不需要 mode，参数名易混淆）
- **P2-3 num_frames 校验更友好**：错误时打印合法值列表（前 200 字符：`81, 89, 97, ...`）。
- **P2-4 --output 单文件路径**：新增 `-o/--output`，优先级高于 `--output-dir`（agent 经常需要直接指定文件路径）。
- **P2-5 --max-retries 可调**：默认 3，可覆盖。
- **P2-6 超时 1800s + 轮询 5s**：默认超时从 900s 提到 1800s（晚高峰队列积压 15+ 分钟考虑），轮询默认 5s（按新 API 文档推荐）。
- **P2-7 download_file 优化**：`curl -f` + `-L`（跟随重定向，GCS 视频 URL 是签名重定向）+ `--max-time 900`（视频文件大）。

**P3 体验**
- **P3-1 SKILL.md agent 视角重写**：5 个 Quick Start 例子、4 种工作流选哪个速查表、Agent 输出格式示例、First-Time Setup 流程。
- **P3-2 Changelog.md 独立成文件**：按主人 2026-06-06 铁律"Changelog 单独做个 Changelog.md 文件吧，不然 skill.md 越来越大"。SKILL.md 末尾只放速览，详细变更记录在此。
- **P3-3 references/api.md 更新**：4 种工作流示例对齐新 API 文档，video_id 查询说明 + video_url 字段错乱备注。
- **P3-4 .env.example + .env + setup.sh**：新建 `.env.example` 模板、`.env` 占位符、`scripts/setup.sh` 交互式配 key。
- **P3-5 --no-poll 强化**：返回 `STATUS: submitted` + `VIDEO_ID` / `TASK_ID` 结构化字段（v1.x 只 dump JSON）。
- **P3-6 progress 打印去噪**：v1.x 每轮都 print，新版只打印**进度变化**那一帧，避免 stderr spam。
- **P3-7 argparse epilog**：新增 4 个用法示例在 `--help` 末尾。

**测试**
- **18+ 回归测试**（`tests/test_agnes_video.py`）：
  - **TestBuildPayload** (4 个)：T2V、I2V、Multi-Image、Keyframes
  - **TestValidateNumFrames** (3 个)：合法值、过大、不满足 8n+1
  - **TestExtractors** (6 个)：task_id / video_id / status / progress / video_url 多字段名
  - **TestApiKey** (3 个)：单 key / 多 key / 缺 key
  - **TestRetry** (3 个)：429/500/网络错误分类
  - **TestAgentOutput** (2 个)：成功 / 错误格式
- 18+ → 18+ 测试全过（< 1s）

**破坏性变更（Breaking Changes）**
- **CLI 子命令 `mode` 选项变化**：`multi-image` 移除（多图直接传多个 `--image-url` 即可）。
- **默认值变化**：超时 900s → 1800s；轮询 10s → 5s；输出格式默认 `json` → `agent`。
- **`--no-poll` 行为变化**：v1.x 输出 JSON，新版输出结构化 `STATUS: submitted`。

**已知限制**
- `video_url` 字段名官方文档示例错乱（视频 URL 放在 `remixed_from_video_id`），脚本已智能识别但用户需注意新响应可能字段名又变。
- 视频文件大（5s ≈ 几 MB），下载用 curl + 900s 超时，不做断点续传（视频一次性完整下载比续传更稳）。
- key 共池警告：`agnes-free-image` 和 `agnes-free-video` 用同一把 key 时 quota 共池。

---

## v1.0.0 (2026-05-30) — 初版

- 基础 4 workflow：T2V、I2V、Multi-Image、Keyframes
- 单 Key、单查询接口（`/v1/videos/{task_id}`）
- 同步 submit + 轮询 + 下载
- 进度打印到 stderr
- 严格校验 `num_frames <= 441` 且 `8n+1`
