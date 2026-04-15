#!/usr/bin/env bash
# 一键将本地 mempalace fork 安装到 Hermes venv（editable mode）
# 用法：在 ~/git/mempalace 目录下跑 ./install-to-hermes.sh
#       或者任意位置跑 ~/git/mempalace/install-to-hermes.sh

set -euo pipefail

HERMES_VENV="$HOME/.hermes/hermes-agent/venv"
MEMPALACE_DIR="$(cd "$(dirname "$0")" && pwd)"

# 找 pip
if [[ -x "$HERMES_VENV/bin/pip3" ]]; then
    PIP="$HERMES_VENV/bin/pip3"
elif [[ -x "$HERMES_VENV/bin/pip" ]]; then
    PIP="$HERMES_VENV/bin/pip"
else
    echo "❌ Hermes venv not found at $HERMES_VENV"
    exit 1
fi

echo "📦 Installing mempalace from $MEMPALACE_DIR → Hermes venv"
$PIP install -e "$MEMPALACE_DIR" --quiet

# 验证
VERSION=$("$HERMES_VENV/bin/python3" -c "from mempalace import __version__; print(__version__)")
echo "✅ mempalace $VERSION installed (editable)"
echo ""
echo "⚠️  重启 Hermes 生效：hermes restart 或重启 gateway"
