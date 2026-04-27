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

# ─── Step 2: CLI + MCP server on PATH ───
# Two binaries get installed by `pip install -e .`:
#   - mempalace      (CLI)       — terminal use, ~/.local/bin is fine
#   - mempalace-mcp  (MCP server) — spawned by GUI Claude Code / Cursor /
#                                   Hermes via launchd, which does NOT see
#                                   ~/.pyenv/shims or ~/.local/bin. Must
#                                   live under /usr/local/bin (or similar
#                                   path that launchd resolves).
PY_SCRIPT_DIR="$(python3 -c "import sysconfig; print(sysconfig.get_path('scripts'))")"

install_symlink() {
  local name="$1"
  local src="$PY_SCRIPT_DIR/$name"
  local prefer_system="$2"  # "system" = must be /usr/local/bin for launchd visibility

  if [[ ! -f "$src" ]]; then
    warn "$name binary not found at $src"
    return 1
  fi

  if [[ "$prefer_system" == "system" ]]; then
    # MCP server — needs to be in launchd's default PATH
    if [[ -w /usr/local/bin ]]; then
      ln -sf "$src" "/usr/local/bin/$name"
      ok "$name symlinked: /usr/local/bin/$name"
    elif sudo -n true 2>/dev/null; then
      sudo ln -sf "$src" "/usr/local/bin/$name"
      ok "$name symlinked: /usr/local/bin/$name (via sudo)"
    else
      warn "$name needs /usr/local/bin for GUI Claude Code / Cursor / Hermes."
      warn "  Run manually: sudo ln -sf $src /usr/local/bin/$name"
    fi
  else
    # CLI — terminal use, either location works
    if command -v "$name" &>/dev/null; then
      ok "$name already on PATH: $(which "$name")"
    elif [[ -d "$HOME/.local/bin" ]] && echo "$PATH" | grep -q "$HOME/.local/bin"; then
      ln -sf "$src" "$HOME/.local/bin/$name"
      ok "$name symlinked: ~/.local/bin/$name"
    elif [[ -w /usr/local/bin ]]; then
      ln -sf "$src" "/usr/local/bin/$name"
      ok "$name symlinked: /usr/local/bin/$name"
    else
      warn "$name not on PATH. Add manually: ln -s $src ~/.local/bin/$name"
    fi
  fi
}

install_symlink "mempalace" ""          # CLI — flexible
install_symlink "mempalace-mcp" "system" # MCP — must be in launchd PATH

# ─── Step 3: Initialize palace if needed ───
if [[ ! -d "$HOME/.mempalace/palace" ]]; then
  info "Initializing palace..."
  python3 -m mempalace init "$HOME/.mempalace" 2>/dev/null || true
  ok "Palace initialized at ~/.mempalace/"
else
  ok "Palace already exists at ~/.mempalace/"
fi

# ─── Step 4: Delegate to sync-plugins.sh ───
SYNC_SCRIPT="$REPO_DIR/scripts/sync-plugins.sh"
if [[ ! -f "$SYNC_SCRIPT" ]]; then
  fail "sync-plugins.sh not found at $SYNC_SCRIPT"
fi

info "Syncing plugins via sync-plugins.sh..."
SYNC_FLAGS=""
if $INSTALL_CLAUDE && $INSTALL_CODEX; then
  SYNC_FLAGS="--claude --codex"
elif $INSTALL_CLAUDE; then
  SYNC_FLAGS="--claude"
elif $INSTALL_CODEX; then
  SYNC_FLAGS="--codex"
fi

bash "$SYNC_SCRIPT" $SYNC_FLAGS

# ─── Summary ───
echo ""
echo "═══════════════════════════════════════════"
echo -e "${GREEN}  MemPalace installation complete${NC}"
echo "═══════════════════════════════════════════"
echo ""
echo "  Palace:    ~/.mempalace/"
echo "  Package:   $(python3 -c 'import mempalace; print(mempalace.__file__)')"
echo "  CLI:       $(which mempalace 2>/dev/null || echo 'not on PATH')"
echo ""

if $INSTALL_CLAUDE; then
  echo "  Claude Code: Restart to activate plugin"
  echo "    Skills: /mempalace:search, /mempalace:status, /mempalace:mine"
fi

if $INSTALL_CODEX; then
  echo "  Codex CLI: Restart to activate"
  echo "    Skills: \$mempalace-search, \$mempalace-status, \$mempalace-mine"
fi

echo ""
echo "Next steps:"
echo "  1. Set up LiteLLM proxy (for embedding + recall LLM):"
echo "       cd $REPO_DIR/litellm && bash setup.sh"
echo "  2. Verify: mempalace status"
echo "  3. Test recall: /mempalace:search 'your query'"
