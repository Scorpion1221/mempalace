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
