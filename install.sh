#!/usr/bin/env bash
set -euo pipefail

# MemPalace installer — one command for Claude Code, Codex, Cursor, and Hermes.
#
# The install ALWAYS calls scripts/sync-plugins.sh which applies configuration
# drift protection (single source of truth: ~/.mempalace/env). Per-agent flags
# pick which agents participate.
#
# Usage:
#   bash install.sh                             # auto-detect installed agents
#   bash install.sh --all                       # force all four agents
#   bash install.sh --claude --codex            # pick specific agents
#   bash install.sh --singleton                 # also install the shared singleton
#                                                (launchd on macOS, systemd --user
#                                                 on Linux)
#   bash install.sh --dev                       # editable install for contributors
#
# See docs/INSTALL-FOR-AGENTS.md for the agent-guided flow.

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
INSTALL_HERMES=false
INSTALL_CURSOR=false
ENABLE_SINGLETON=false
AUTO_DETECT=false
DEV_MODE=false

if [[ $# -eq 0 ]]; then
  AUTO_DETECT=true
else
  for arg in "$@"; do
    case "$arg" in
      --claude)     INSTALL_CLAUDE=true ;;
      --codex)      INSTALL_CODEX=true ;;
      --hermes)     INSTALL_HERMES=true ;;
      --cursor)     INSTALL_CURSOR=true ;;
      --all)
        INSTALL_CLAUDE=true; INSTALL_CODEX=true
        INSTALL_HERMES=true; INSTALL_CURSOR=true
        ;;
      --auto)       AUTO_DETECT=true ;;
      --singleton)  ENABLE_SINGLETON=true ;;
      --dev)        DEV_MODE=true ;;
      --help|-h)
        cat <<'EOF'
Usage: bash install.sh [agent-flags] [--singleton] [--dev]

Agents (multi-select; default is auto-detect):
  --claude        Claude Code
  --codex         Codex CLI
  --hermes        Hermes agent
  --cursor        Cursor IDE
  --all           All four (Claude + Codex + Hermes + Cursor)
  --auto          Auto-detect which agents are installed (default when no flags)

Singleton (optional):
  --singleton     Also install the shared singleton service
                  (launchd on macOS, systemd --user on Linux).
                  Agents then talk to ~/.mempalace/mcp.sock via
                  the mempalace-mcp-bridge shim.

Other:
  --dev           Editable install for contributors
EOF
        exit 0
        ;;
      *) fail "Unknown option: $arg" ;;
    esac
  done
fi

if $AUTO_DETECT; then
  info "Auto-detecting installed agents..."
  [[ -d "$HOME/.claude" ]]            && INSTALL_CLAUDE=true && ok "  found Claude Code"
  [[ -f "$HOME/.codex/config.toml" ]] && INSTALL_CODEX=true  && ok "  found Codex"
  [[ -d "$HOME/.hermes" ]]            && INSTALL_HERMES=true && ok "  found Hermes"
  [[ -d "$HOME/.cursor" ]]            && INSTALL_CURSOR=true && ok "  found Cursor"
  if ! $INSTALL_CLAUDE && ! $INSTALL_CODEX && ! $INSTALL_HERMES && ! $INSTALL_CURSOR; then
    warn "No supported agents detected. Installing Python package only."
    warn "Re-run with explicit flags (e.g. --claude) once you've installed an agent."
  fi
fi

# ─── Step 1: Python package ───
if ! command -v python3 &>/dev/null; then
  fail "python3 not found"
fi

PYTHON_BIN="$(command -v python3)"

if $DEV_MODE; then
  info "Installing Python package in editable mode (--dev)..."
  "$PYTHON_BIN" -m pip install -q -e "$REPO_DIR"
else
  # Snapshot install from the LOCAL checkout (not the remote tag).
  # This preserves unpushed branch changes such as portable-install work.
  info "Installing Python package from local checkout snapshot..."
  "$PYTHON_BIN" -m pip install -q --force-reinstall --no-deps "$REPO_DIR"
fi
ok "Python package installed: $($PYTHON_BIN -c 'import mempalace, mempalace.version; print(f"v{mempalace.version.__version__} ({mempalace.__file__})")')"

# ─── Step 2: CLI + MCP binaries on PATH ───
# Three binaries:
#   mempalace              CLI
#   mempalace-mcp          MCP server
#   mempalace-mcp-bridge   stdio ↔ UDS shim
#
# For GUI agents, prefer system-visible bin dirs. On Apple Silicon macOS,
# /opt/homebrew/bin is often writable even when /usr/local/bin is not.
PY_SCRIPT_DIR="$($PYTHON_BIN -c "import sysconfig; print(sysconfig.get_path('scripts'))")"

install_symlink() {
  local name="$1"
  local prefer_system="$2"
  local src="$PY_SCRIPT_DIR/$name"

  if [[ ! -f "$src" ]]; then
    warn "$name binary not found at $src"
    return 1
  fi

  if [[ "$prefer_system" == "system" ]]; then
    local linked=false
    for dst_dir in /usr/local/bin /opt/homebrew/bin; do
      [[ -d "$dst_dir" ]] || continue
      if [[ -w "$dst_dir" ]]; then
        ln -sf "$src" "$dst_dir/$name"
        ok "$name symlinked: $dst_dir/$name"
        linked=true
      fi
    done
    if [[ "$linked" == false ]]; then
      warn "$name needs a GUI-visible bin dir. Tried /usr/local/bin and /opt/homebrew/bin."
      warn "  Run manually after granting write access:"
      warn "    ln -sf $src /usr/local/bin/$name"
      warn "  or"
      warn "    ln -sf $src /opt/homebrew/bin/$name"
    fi
  else
    if command -v "$name" &>/dev/null; then
      ok "$name already on PATH: $(which "$name")"
    elif [[ -d "$HOME/.local/bin" ]] && echo "$PATH" | grep -q "$HOME/.local/bin"; then
      ln -sf "$src" "$HOME/.local/bin/$name"
      ok "$name symlinked: ~/.local/bin/$name"
    elif [[ -w /opt/homebrew/bin ]]; then
      ln -sf "$src" "/opt/homebrew/bin/$name"
      ok "$name symlinked: /opt/homebrew/bin/$name"
    elif [[ -w /usr/local/bin ]]; then
      ln -sf "$src" "/usr/local/bin/$name"
      ok "$name symlinked: /usr/local/bin/$name"
    else
      warn "$name not on PATH. Add manually: ln -s $src ~/.local/bin/$name"
    fi
  fi
}

install_symlink "mempalace" ""
install_symlink "mempalace-mcp" "system"
install_symlink "mempalace-mcp-bridge" "system"

# ─── Step 3: Initialize palace if needed ───
if [[ ! -d "$HOME/.mempalace/palace" ]]; then
  info "Initializing palace..."
  "$PYTHON_BIN" -m mempalace init "$HOME/.mempalace" 2>/dev/null || true
  ok "Palace initialized at ~/.mempalace/"
else
  ok "Palace already exists at ~/.mempalace/"
fi

# ─── Step 4: Delegate to sync-plugins.sh ───
SYNC_SCRIPT="$REPO_DIR/scripts/sync-plugins.sh"
if [[ ! -f "$SYNC_SCRIPT" ]]; then
  fail "sync-plugins.sh not found at $SYNC_SCRIPT"
fi

SYNC_FLAGS=()
$INSTALL_CLAUDE && SYNC_FLAGS+=("--claude")
$INSTALL_CODEX  && SYNC_FLAGS+=("--codex")
$INSTALL_HERMES && SYNC_FLAGS+=("--hermes")
$INSTALL_CURSOR && SYNC_FLAGS+=("--cursor")

if [[ ${#SYNC_FLAGS[@]} -gt 0 ]]; then
  info "Syncing plugins via sync-plugins.sh ${SYNC_FLAGS[*]}..."
  bash "$SYNC_SCRIPT" "${SYNC_FLAGS[@]}"
else
  warn "No agents selected — skipping sync-plugins.sh. Run it manually once an agent is installed."
fi

# ─── Step 5: Optional singleton service ───
if $ENABLE_SINGLETON; then
  info "Installing shared-MCP singleton service..."
  "$PYTHON_BIN" -m mempalace.cli singleton install --start || warn "singleton install failed (see output above)"
fi

# ─── Summary ───
echo ""
echo "═══════════════════════════════════════════"
echo -e "${GREEN}  MemPalace installation complete${NC}"
echo "═══════════════════════════════════════════"
echo ""
echo "  Palace:    ~/.mempalace/"
echo "  Package:   $($PYTHON_BIN -c 'import mempalace; print(mempalace.__file__)')"
echo "  CLI:       $(command -v mempalace 2>/dev/null || echo 'not on PATH')"
echo "  MCP:       $(command -v mempalace-mcp 2>/dev/null || echo 'not on PATH')"
echo "  Bridge:    $(command -v mempalace-mcp-bridge 2>/dev/null || echo 'not on PATH')"
if $ENABLE_SINGLETON; then
  echo "  Singleton: installed (run: $PYTHON_BIN -m mempalace.cli singleton status)"
else
  echo "  Singleton: NOT installed (run: $PYTHON_BIN -m mempalace.cli singleton install --start)"
fi
echo ""

$INSTALL_CLAUDE && echo "  Claude Code: Restart to activate plugin"
$INSTALL_CODEX  && echo "  Codex CLI:   Restart to activate"
$INSTALL_HERMES && echo "  Hermes:      plugin/env synced; Hermes gateway restarted by sync-plugins.sh if running"
$INSTALL_CURSOR && echo "  Cursor:      Restart to activate plugin"

echo ""
echo "Next steps:"
echo "  1. LiteLLM proxy (embedding + recall LLM):"
echo "       cd $REPO_DIR/litellm && bash setup.sh"
echo "  2. Verify:      $PYTHON_BIN -m mempalace.cli status && $PYTHON_BIN -m mempalace.cli singleton status"
echo "  3. Test recall: /mempalace:search 'your query'"
