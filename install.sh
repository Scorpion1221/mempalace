#!/usr/bin/env bash
set -euo pipefail

# MemPalace installer — one command for Claude Code + Codex CLI
# Usage: bash install.sh [--claude] [--codex] [--all]
#   No flags = --all (install both)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$SCRIPT_DIR"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}[mempalace]${NC} $*"; }
ok()    { echo -e "${GREEN}[mempalace]${NC} $*"; }
warn()  { echo -e "${YELLOW}[mempalace]${NC} $*"; }
fail()  { echo -e "${RED}[mempalace]${NC} $*"; exit 1; }

# ─── Parse args ───
INSTALL_CLAUDE=false
INSTALL_CODEX=false

if [[ $# -eq 0 ]]; then
  INSTALL_CLAUDE=true
  INSTALL_CODEX=true
else
  for arg in "$@"; do
    case "$arg" in
      --claude) INSTALL_CLAUDE=true ;;
      --codex)  INSTALL_CODEX=true ;;
      --all)    INSTALL_CLAUDE=true; INSTALL_CODEX=true ;;
      --help|-h)
        echo "Usage: bash install.sh [--claude] [--codex] [--all]"
        echo "  --claude   Install for Claude Code only"
        echo "  --codex    Install for Codex CLI only"
        echo "  --all      Install for both (default)"
        exit 0
        ;;
      *) fail "Unknown option: $arg" ;;
    esac
  done
fi

# ─── Step 1: Python package (editable) ───
info "Installing Python package (editable mode)..."
if command -v python3 &>/dev/null; then
  pip3 install -e "$REPO_DIR" --quiet 2>&1 | tail -1
  ok "Python package installed: $(python3 -c 'import mempalace; print(mempalace.__file__)')"
else
  fail "python3 not found"
fi

# ─── Step 2: CLI on PATH ───
MEMPALACE_BIN="$(python3 -c "import sysconfig; print(sysconfig.get_path('scripts'))")/mempalace"
if command -v mempalace &>/dev/null; then
  ok "CLI already on PATH: $(which mempalace)"
elif [[ -f "$MEMPALACE_BIN" ]]; then
  # Try ~/.local/bin first (no sudo), fall back to /usr/local/bin
  if [[ -d "$HOME/.local/bin" ]] && echo "$PATH" | grep -q "$HOME/.local/bin"; then
    ln -sf "$MEMPALACE_BIN" "$HOME/.local/bin/mempalace"
    ok "CLI symlinked: ~/.local/bin/mempalace"
  elif [[ -w /usr/local/bin ]]; then
    ln -sf "$MEMPALACE_BIN" /usr/local/bin/mempalace
    ok "CLI symlinked: /usr/local/bin/mempalace"
  else
    warn "CLI not on PATH. Add manually: ln -s $MEMPALACE_BIN ~/.local/bin/mempalace"
  fi
else
  warn "CLI binary not found at $MEMPALACE_BIN"
fi

# ─── Step 3: Initialize palace if needed ───
if [[ ! -d "$HOME/.mempalace/palace" ]]; then
  info "Initializing palace..."
  python3 -m mempalace init "$HOME/.mempalace" 2>/dev/null || true
  ok "Palace initialized at ~/.mempalace/"
else
  ok "Palace already exists at ~/.mempalace/"
fi

# ═══════════════════════════════════════════
# Claude Code
# ═══════════════════════════════════════════
if $INSTALL_CLAUDE; then
  info "─── Claude Code setup ───"

  CLAUDE_CACHE_BASE="$HOME/.claude/plugins/cache/mempalace/mempalace"

  # Clean old cached versions and sync from repo
  info "Syncing plugin to Claude Code cache..."
  rm -rf "$CLAUDE_CACHE_BASE"
  mkdir -p "$CLAUDE_CACHE_BASE/local"
  cp -r "$REPO_DIR/.claude-plugin/"* "$CLAUDE_CACHE_BASE/local/"
  # Also copy plugin.json to .claude-plugin/ subdir (Claude Code expects it there)
  mkdir -p "$CLAUDE_CACHE_BASE/local/.claude-plugin"
  cp "$REPO_DIR/.claude-plugin/plugin.json" "$CLAUDE_CACHE_BASE/local/.claude-plugin/plugin.json"
  ok "Plugin synced to $CLAUDE_CACHE_BASE/local"

  # Remove duplicate MCP server if exists (the manual one using "python" instead of "python3")
  if command -v claude &>/dev/null; then
    if claude mcp get mempalace &>/dev/null 2>&1; then
      claude mcp remove mempalace 2>/dev/null || true
      ok "Removed duplicate 'mempalace' MCP entry (plugin handles this)"
    fi
  fi

  ok "Claude Code setup complete"
  echo "  Restart Claude Code to activate plugin."
  echo "  Skills: /mempalace:mempalace, /mempalace:search, /mempalace:mine, etc."
fi

# ═══════════════════════════════════════════
# Codex CLI
# ═══════════════════════════════════════════
if $INSTALL_CODEX; then
  info "─── Codex CLI setup ───"

  # ── Skills ──
  CODEX_SKILLS_DIR="$HOME/.codex/vendor_imports/skills/skills/.curated"
  if [[ -d "$CODEX_SKILLS_DIR" ]]; then
    info "Installing skills to Codex curated directory..."
    for skill_dir in "$REPO_DIR/.codex-plugin/skills/"*/; do
      skill_name="$(basename "$skill_dir")"
      target="$CODEX_SKILLS_DIR/mempalace-${skill_name}"
      rm -rf "$target"
      cp -r "$skill_dir" "$target"
    done
    ok "Skills installed: mempalace-{help,init,mine,search,status}"
  else
    # Fallback to ~/.codex/skills/
    info "Curated dir not found, using ~/.codex/skills/..."
    mkdir -p "$HOME/.codex/skills"
    for skill_dir in "$REPO_DIR/.codex-plugin/skills/"*/; do
      skill_name="$(basename "$skill_dir")"
      target="$HOME/.codex/skills/mempalace-${skill_name}"
      rm -rf "$target"
      cp -r "$skill_dir" "$target"
    done
    ok "Skills installed to ~/.codex/skills/"
  fi

  # ── MCP server in config.toml ──
  CODEX_CONFIG="$HOME/.codex/config.toml"
  if [[ -f "$CODEX_CONFIG" ]]; then
    if ! grep -q 'mcp_servers.mempalace' "$CODEX_CONFIG" 2>/dev/null; then
      info "Adding MCP server to config.toml..."
      # Find first [mcp_servers.*] line and insert before it
      if grep -q '\[mcp_servers\.' "$CODEX_CONFIG"; then
        # Insert before the first mcp_servers entry
        FIRST_MCP_LINE=$(grep -n '\[mcp_servers\.' "$CODEX_CONFIG" | head -1 | cut -d: -f1)
        {
          head -n $((FIRST_MCP_LINE - 1)) "$CODEX_CONFIG"
          echo '[mcp_servers.mempalace]'
          echo 'command = "python3"'
          echo 'args = ["-m", "mempalace.mcp_server"]'
          echo 'enabled = true'
          echo 'startup_timeout_sec = 10'
          echo ''
          tail -n +$FIRST_MCP_LINE "$CODEX_CONFIG"
        } > "${CODEX_CONFIG}.tmp"
        mv "${CODEX_CONFIG}.tmp" "$CODEX_CONFIG"
      else
        # Append at end
        cat >> "$CODEX_CONFIG" <<'TOML'

[mcp_servers.mempalace]
command = "python3"
args = ["-m", "mempalace.mcp_server"]
enabled = true
startup_timeout_sec = 10
TOML
      fi
      ok "MCP server added to config.toml"
    else
      ok "MCP server already in config.toml"
    fi
  else
    warn "Codex config.toml not found at $CODEX_CONFIG"
  fi

  # ── Hooks in hooks.json ──
  CODEX_HOOKS="$HOME/.codex/hooks.json"
  HOOK_CMD="bash $REPO_DIR/.codex-plugin/hooks/mempal-hook.sh"
  if [[ -f "$CODEX_HOOKS" ]]; then
    if ! grep -q 'mempal-hook' "$CODEX_HOOKS" 2>/dev/null; then
      info "Adding hooks to hooks.json..."
      python3 -c "
import json, sys

hooks_path = '$CODEX_HOOKS'
hook_cmd_userprompt = '$HOOK_CMD userprompt'
hook_cmd_stop = '$HOOK_CMD stop'

with open(hooks_path) as f:
    data = json.load(f)

hooks = data.setdefault('hooks', {})

# Add UserPromptSubmit (memory recall injection)
if 'UserPromptSubmit' not in hooks:
    hooks['UserPromptSubmit'] = [{'hooks': []}]
hooks['UserPromptSubmit'][0].setdefault('hooks', []).append({
    'type': 'command',
    'command': hook_cmd_userprompt,
    'timeout': 10
})

# Add to Stop (periodic auto-save)
stop_hooks = hooks.setdefault('Stop', [{'hooks': []}])
stop_hooks[0]['hooks'].append({
    'type': 'command',
    'command': hook_cmd_stop,
    'timeout': 30
})

with open(hooks_path, 'w') as f:
    json.dump(data, f, indent=2)
    f.write('\n')
"
      ok "Hooks added (UserPromptSubmit + Stop)"
    else
      ok "Hooks already configured"
    fi
  else
    warn "Codex hooks.json not found at $CODEX_HOOKS"
  fi

  # ── Ensure [features] codex_hooks = true ──
  if [[ -f "$CODEX_CONFIG" ]]; then
    if ! grep -q 'codex_hooks' "$CODEX_CONFIG" 2>/dev/null; then
      info "Enabling codex_hooks feature flag..."
      cat >> "$CODEX_CONFIG" <<'TOML'

[features]
codex_hooks = true
TOML
      ok "Feature flag codex_hooks = true added to config.toml"
    else
      ok "codex_hooks feature flag already set"
    fi
  fi

  ok "Codex CLI setup complete"
  echo "  Restart Codex to activate."
  echo "  Skills: \$mempalace-search, \$mempalace-status, \$mempalace-mine, etc."
fi

# ─── Summary ───
echo ""
echo "═══════════════════════════════════════════"
echo -e "${GREEN}  MemPalace installation complete${NC}"
echo "═══════════════════════════════════════════"
echo ""
echo "  Palace:    ~/.mempalace/"
echo "  Package:   $(python3 -c 'import mempalace; print(mempalace.__file__)')"
echo "  CLI:       $(which mempalace 2>/dev/null || echo 'not on PATH')"
$INSTALL_CLAUDE && echo "  Claude:    plugin synced to cache"
$INSTALL_CODEX  && echo "  Codex:     skills + MCP + hooks configured"
echo ""
echo "  Verify:    mempalace status"
echo ""
