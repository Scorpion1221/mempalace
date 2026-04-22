#!/usr/bin/env bash
# sync-mempalace-plugins.sh — One command to sync all 3 agents' plugin files
set -euo pipefail

REPO="$HOME/git/mempalace"
HERMES_REPO="$HOME/git/hermes-mempalace-plugin"

echo "=== Syncing MemPalace plugins to all 3 agents ==="

# 1. Reinstall Python package (picks up new entry points like mempalace-mcp)
echo "[1/6] Reinstalling Python package (editable)..."
pip install -e "$REPO" -q 2>/dev/null
echo "  → $(python3 -c 'import mempalace; print(f"mempalace {mempalace.__version__}")')"

# 2. Claude Code: sync plugin cache
CLAUDE_CACHE="$HOME/.claude/plugins/cache/mempalace/mempalace/3.3.0"
if [ -d "$CLAUDE_CACHE" ]; then
    echo "[2/6] Syncing Claude Code plugin cache..."
    cp "$REPO/.claude-plugin/hooks/"mempal-*.sh "$CLAUDE_CACHE/hooks/" 2>/dev/null && echo "  → hooks synced" || echo "  → no hooks to sync"
    cp "$REPO/.claude-plugin/plugin.json" "$CLAUDE_CACHE/plugin.json" 2>/dev/null && echo "  → plugin.json synced" || true
else
    echo "[2/6] Claude Code plugin cache not found, skipping"
fi

# 3. Codex: sync plugin hooks
CODEX_PLUGIN="$HOME/.agents/plugins/mempalace/.codex-plugin"
if [ -d "$CODEX_PLUGIN" ]; then
    echo "[3/6] Syncing Codex plugin hooks..."
    cp "$REPO/.codex-plugin/hooks/"*.sh "$CODEX_PLUGIN/hooks/" 2>/dev/null && echo "  → hooks synced" || echo "  → no hooks to sync"
else
    echo "[3/6] Codex plugin dir not found, skipping"
fi

# 4. Hermes: sync plugin
HERMES_RUNTIME="$HOME/.hermes/hermes-agent/plugins/memory/mempalace"
if [ -d "$HERMES_RUNTIME" ] && [ -f "$HERMES_REPO/plugins/memory/mempalace/__init__.py" ]; then
    echo "[4/6] Syncing Hermes plugin..."
    cp "$HERMES_REPO/plugins/memory/mempalace/__init__.py" "$HERMES_RUNTIME/__init__.py"
    echo "  → __init__.py synced"

    # Also reinstall in Hermes venv if it exists
    HERMES_VENV="$HOME/.hermes/hermes-agent/venv"
    if [ -f "$HERMES_VENV/bin/python" ]; then
        "$HERMES_VENV/bin/python" -m pip install -e "$REPO" -q 2>/dev/null && echo "  → Hermes venv updated" || true
    fi
else
    echo "[4/6] Hermes plugin dir not found, skipping"
fi

# 5. Restart Hermes if running
echo "[5/6] Restarting Hermes gateway..."
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

# 6. Verify env var config across all agents
echo "[6/6] Verifying env var configuration..."
REQUIRED_VARS="MEMPAL_EMBEDDING_MODEL GEMINI_API_KEY SSL_CERT_FILE"
HAS_WARNINGS=0

# Claude Code
if [ -f "$HOME/.claude/settings.json" ]; then
    for var in $REQUIRED_VARS; do
        if ! grep -q "$var" "$HOME/.claude/settings.json" 2>/dev/null; then
            echo "  ⚠ Claude Code: missing $var in ~/.claude/settings.json env"
            HAS_WARNINGS=1
        fi
    done
    [ $HAS_WARNINGS -eq 0 ] && echo "  ✓ Claude Code settings.json OK"
fi

# Codex — check BOTH mcp_servers.env AND shell_environment_policy.set
HAS_WARNINGS=0
if [ -f "$HOME/.codex/config.toml" ]; then
    # Check mcp_servers section
    for var in $REQUIRED_VARS; do
        if ! grep -A5 '\[mcp_servers.mempalace\]' "$HOME/.codex/config.toml" | grep -q "$var" 2>/dev/null; then
            echo "  ⚠ Codex MCP: missing $var in [mcp_servers.mempalace].env"
            HAS_WARNINGS=1
        fi
    done
    # Check shell_environment_policy.set (for hooks)
    for var in $REQUIRED_VARS MEMPAL_RECALL_LLM; do
        if ! grep -A10 '\[shell_environment_policy.set\]' "$HOME/.codex/config.toml" | grep -q "$var" 2>/dev/null; then
            echo "  ⚠ Codex hooks: missing $var in [shell_environment_policy.set]"
            HAS_WARNINGS=1
        fi
    done
    [ $HAS_WARNINGS -eq 0 ] && echo "  ✓ Codex config.toml OK"
fi

# Hermes
HAS_WARNINGS=0
HERMES_PLIST="$HOME/Library/LaunchAgents/ai.hermes.gateway.plist"
if [ -f "$HERMES_PLIST" ]; then
    for var in MEMPAL_EMBEDDING_MODEL GEMINI_API_KEY SSL_CERT_FILE; do
        if ! grep -q "$var" "$HERMES_PLIST" 2>/dev/null; then
            echo "  ⚠ Hermes: missing $var in launchd plist"
            HAS_WARNINGS=1
        fi
    done
    [ $HAS_WARNINGS -eq 0 ] && echo "  ✓ Hermes plist OK"
fi

echo ""
echo "Done. Claude Code and Codex need a new session to pick up changes."
