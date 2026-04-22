#!/usr/bin/env bash
# sync-mempalace-plugins.sh — One command to sync all 3 agents' plugin files
set -euo pipefail

REPO="$HOME/git/mempalace"
HERMES_REPO="$HOME/git/hermes-mempalace-plugin"

echo "=== Syncing MemPalace plugins to all 3 agents ==="

# 1. Reinstall Python package (picks up new entry points like mempalace-mcp)
echo "[1/5] Reinstalling Python package (editable)..."
pip install -e "$REPO" -q 2>/dev/null
echo "  → $(python3 -c 'import mempalace; print(f"mempalace {mempalace.__version__}")')"

# 2. Claude Code: sync plugin cache
CLAUDE_CACHE="$HOME/.claude/plugins/cache/mempalace/mempalace/3.3.0"
if [ -d "$CLAUDE_CACHE" ]; then
    echo "[2/5] Syncing Claude Code plugin cache..."
    cp "$REPO/.claude-plugin/hooks/"mempal-*.sh "$CLAUDE_CACHE/hooks/" 2>/dev/null && echo "  → hooks synced" || echo "  → no hooks to sync"
    cp "$REPO/.claude-plugin/plugin.json" "$CLAUDE_CACHE/plugin.json" 2>/dev/null && echo "  → plugin.json synced" || true
else
    echo "[2/5] Claude Code plugin cache not found, skipping"
fi

# 3. Codex: sync plugin hooks
CODEX_PLUGIN="$HOME/.agents/plugins/mempalace/.codex-plugin"
if [ -d "$CODEX_PLUGIN" ]; then
    echo "[3/5] Syncing Codex plugin hooks..."
    cp "$REPO/.codex-plugin/hooks/"*.sh "$CODEX_PLUGIN/hooks/" 2>/dev/null && echo "  → hooks synced" || echo "  → no hooks to sync"
else
    echo "[3/5] Codex plugin dir not found, skipping"
fi

# 4. Hermes: sync plugin + restart
HERMES_RUNTIME="$HOME/.hermes/hermes-agent/plugins/memory/mempalace"
if [ -d "$HERMES_RUNTIME" ] && [ -f "$HERMES_REPO/plugins/memory/mempalace/__init__.py" ]; then
    echo "[4/5] Syncing Hermes plugin..."
    cp "$HERMES_REPO/plugins/memory/mempalace/__init__.py" "$HERMES_RUNTIME/__init__.py"
    echo "  → __init__.py synced"

    # Also reinstall in Hermes venv if it exists
    HERMES_VENV="$HOME/.hermes/hermes-agent/venv"
    if [ -f "$HERMES_VENV/bin/python" ]; then
        "$HERMES_VENV/bin/python" -m pip install -e "$REPO" -q 2>/dev/null && echo "  → Hermes venv updated" || true
    fi
else
    echo "[4/5] Hermes plugin dir not found, skipping"
fi

# 5. Restart Hermes if running
echo "[5/5] Restarting Hermes gateway..."
if pgrep -f "hermes_cli.main gateway" >/dev/null 2>&1; then
    kill $(pgrep -f "hermes_cli.main gateway") 2>/dev/null
    sleep 2
    if pgrep -f "hermes_cli.main gateway" >/dev/null 2>&1; then
        echo "  → Hermes restarted (launchd respawn)"
    else
        echo "  → Hermes killed, waiting for launchd respawn..."
        sleep 3
    fi
else
    echo "  → Hermes not running, skipping"
fi

echo ""
echo "Done. Claude Code and Codex need a new session to pick up changes."
