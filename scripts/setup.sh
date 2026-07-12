#!/usr/bin/env bash
# Agnes Free Video — 交互式配 Key 脚本
# 把 Agnes API Key 写入 .env（600 权限），并提示注入到当前 shell。

set -euo pipefail

# 脚本在 scripts/setup.sh，SKILL_DIR 是 skill 根目录（父级）
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$SKILL_DIR/.env"
ENV_EXAMPLE="$SKILL_DIR/.env.example"

# 1. .env 存在性检查
if [[ ! -f "$ENV_EXAMPLE" ]]; then
    echo "❌ 找不到 $ENV_EXAMPLE" >&2
    exit 1
fi

# v3.1.2: regex 支持多 key（sk-a,sk-b,sk-c），原 regex 只匹配单 key
# v3.2.x: IFS= 让 read 保留前后空格；|| true 兜底 stdin EOF 让 set -e 不中断
if [[ -f "$ENV_FILE" ]] && grep -qE '^AGNES_API_KEY=(sk-[A-Za-z0-9_-]{10,})(,sk-[A-Za-z0-9_-]{10,})*$' "$ENV_FILE"; then
    echo "✅ .env 已有有效 key（不打印内容，避免泄漏）"
    read -rp "要覆盖吗？[y/N] " overwrite || true
    if [[ ! "${overwrite:-}" =~ ^[Yy]$ ]]; then
        echo "👌 保留现有 key，退出"
        exit 0
    fi
fi

# 2. 复制模板
cp "$ENV_EXAMPLE" "$ENV_FILE"
chmod 600 "$ENV_FILE"
echo "✅ 已建 $ENV_FILE (600 权限)"

# 3. 交互式输入
echo
echo "请输入你的 Agnes API Key（形如 sk-xxx）："
echo "  - 多个 key 用逗号分隔（自动轮换）"
echo "  - 留空跳过手动输入，事后编辑 .env"
read -rp "AGNES_API_KEY=" input_key || true

if [[ -n "$input_key" ]]; then
    # 替换占位符
    sed -i.bak "s|^AGNES_API_KEY=.*|AGNES_API_KEY=$input_key|" "$ENV_FILE"
    rm -f "$ENV_FILE.bak"
    chmod 600 "$ENV_FILE"
    echo "✅ 已写入 key 到 $ENV_FILE"
else
    echo "⏭️  跳过，使用占位符；请事后编辑 $ENV_FILE"
fi

# 4. 提示注入
echo
echo "💡 调用脚本前请注入到当前 shell："
echo "   set -a && source $ENV_FILE && set +a"
echo
echo "🧪 验证："
echo "   set -a && source $ENV_FILE && set +a"
echo "   python3 $SKILL_DIR/scripts/agnes_video.py status --task-id smoke-test --format agent"
echo "   （看到 STATUS: error + MESSAGE: task_not_exist (HTTP 400) 就是正常的：key 通了，但任务不存在）"
