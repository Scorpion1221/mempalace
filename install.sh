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
RUNTIME_DIR="${MEMPAL_RUNTIME_DIR:-$HOME/.mempalace/venv}"
RUNTIME_BIN_DIR="$RUNTIME_DIR/bin"
RUNTIME_PYTHON="$RUNTIME_BIN_DIR/python3"
RUNTIME_PIP=("$RUNTIME_PYTHON" -m pip)
RUNTIME_MANIFEST="${MEMPAL_RUNTIME_MANIFEST:-$HOME/.mempalace/runtime.json}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}[mempalace]${NC} $*"; }
ok()    { echo -e "${GREEN}[mempalace]${NC} $*"; }
warn()  { echo -e "${YELLOW}[mempalace]${NC} $*"; }
fail()  { echo -e "${RED}[mempalace]${NC} $*"; exit 1; }

ensure_runtime() {
  RUNTIME_CREATED=false
  if [[ -x "$RUNTIME_PYTHON" ]]; then
    return 0
  fi

  local bootstrap_python=""
  for candidate in python3 python; do
    if command -v "$candidate" &>/dev/null; then
      bootstrap_python="$(command -v "$candidate")"
      break
    fi
  done
  [[ -n "$bootstrap_python" ]] || fail "Neither python3 nor python found to bootstrap $RUNTIME_DIR"

  info "Creating dedicated runtime at $RUNTIME_DIR using $bootstrap_python..."
  mkdir -p "$(dirname "$RUNTIME_DIR")"
  "$bootstrap_python" -m venv "$RUNTIME_DIR"
  [[ -x "$RUNTIME_PYTHON" ]] || fail "Failed to create runtime python at $RUNTIME_PYTHON"
  RUNTIME_CREATED=true
}

write_runtime_manifest() {
  (
  cd /
  "$RUNTIME_PYTHON" - <<PY
import importlib.metadata as md
import json, sysconfig
from pathlib import Path
import mempalace
scripts = Path(sysconfig.get_path('scripts')).resolve()
manifest = {
    "version": md.version("mempalace"),
    "python": str(Path("$RUNTIME_PYTHON").expanduser()),
    "scripts_dir": str(scripts),
    "mcp": str((scripts / "mempalace-mcp").resolve()),
    "bridge": str((scripts / "mempalace-mcp-bridge").resolve()),
}
path = Path("$RUNTIME_MANIFEST").expanduser()
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(manifest, indent=2) + "\n")
print(path)
PY
  )
}

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
ensure_runtime

if $DEV_MODE; then
  info "Installing Python package in editable mode (--dev) into $RUNTIME_DIR..."
  "${RUNTIME_PIP[@]}" install -q -e "$REPO_DIR"
elif [[ "$RUNTIME_CREATED" == true ]]; then
  # Fresh runtime: pull in full dependency tree (chromadb, pyyaml, etc.).
  info "Installing Python package + dependencies into new runtime $RUNTIME_DIR..."
  "${RUNTIME_PIP[@]}" install -q --upgrade pip
  "${RUNTIME_PIP[@]}" install -q "$REPO_DIR"
else
  # Incremental upgrade against an existing runtime that already has deps.
  # ``--no-deps`` keeps upgrades fast and avoids churn on chromadb/pip metadata.
  info "Installing Python package from local checkout snapshot into $RUNTIME_DIR..."
  "${RUNTIME_PIP[@]}" install -q --force-reinstall --no-deps "$REPO_DIR"
fi
ok "Python package installed: $(cd / && $RUNTIME_PYTHON -c 'import importlib.metadata as md, mempalace; print("v%s (%s)" % (md.version("mempalace"), mempalace.__file__))')"
MANIFEST_PATH="$(write_runtime_manifest)"
ok "Runtime manifest written: $MANIFEST_PATH"

# ─── Step 2: CLI + MCP binaries on PATH ───
# Three binaries:
#   mempalace              CLI
#   mempalace-mcp          MCP server
#   mempalace-mcp-bridge   stdio ↔ UDS shim
#
# For GUI agents, prefer system-visible bin dirs. On Apple Silicon macOS,
# /opt/homebrew/bin is often writable even when /usr/local/bin is not.
PY_SCRIPT_DIR="$($RUNTIME_PYTHON -c "import sysconfig; print(sysconfig.get_path('scripts'))")"

install_symlink() {
  local name="$1"
  local prefer_system="$2"
  local src="$PY_SCRIPT_DIR/$name"

  if [[ ! -f "$src" ]]; then
    warn "$name binary not found at $src"
    return 1
  fi

  # Dedicated-runtime commands should win over stale shims from other venvs.
  # For MemPalace, a system-visible bin dir is the only reliable way to make
  # bare `mempalace` resolve to the dedicated runtime across shells / GUI apps.
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
    if [[ -d "$HOME/.local/bin" ]] && echo "$PATH" | grep -q "$HOME/.local/bin"; then
      ln -sf "$src" "$HOME/.local/bin/$name"
      ok "$name symlinked: ~/.local/bin/$name"
    elif [[ -w /opt/homebrew/bin ]]; then
      ln -sf "$src" "/opt/homebrew/bin/$name"
      ok "$name symlinked: /opt/homebrew/bin/$name"
    elif [[ -w /usr/local/bin ]]; then
      ln -sf "$src" "/usr/local/bin/$name"
      ok "$name symlinked: /usr/local/bin/$name"
    elif command -v "$name" &>/dev/null; then
      ok "$name already on PATH: $(which "$name")"
    else
      warn "$name not on PATH. Add manually: ln -s $src ~/.local/bin/$name"
    fi
  fi
}

install_symlink "mempalace" "system"
install_symlink "mempalace-mcp" "system"
install_symlink "mempalace-mcp-bridge" "system"

# ─── Step 3: Initialize palace if needed ───
if [[ ! -d "$HOME/.mempalace/palace" ]]; then
  info "Initializing palace..."
  "$RUNTIME_PYTHON" -m mempalace init "$HOME/.mempalace" 2>/dev/null || true
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

export MEMPAL_RUNTIME_DIR="$RUNTIME_DIR"
export MEMPAL_RUNTIME_PYTHON="$RUNTIME_PYTHON"
export MEMPAL_RUNTIME_MANIFEST="$RUNTIME_MANIFEST"

if [[ ${#SYNC_FLAGS[@]} -gt 0 ]]; then
  info "Syncing plugins via sync-plugins.sh ${SYNC_FLAGS[*]}..."
  bash "$SYNC_SCRIPT" "${SYNC_FLAGS[@]}"
else
  warn "No agents selected — skipping sync-plugins.sh. Run it manually once an agent is installed."
fi

# ─── Step 5: Optional singleton service ───
if $ENABLE_SINGLETON; then
  info "Installing shared-MCP singleton service..."
  "$RUNTIME_PYTHON" -m mempalace.cli singleton install --start || warn "singleton install failed (see output above)"
fi

# ─── Summary ───
echo ""
echo "═══════════════════════════════════════════"
echo -e "${GREEN}  MemPalace installation complete${NC}"
echo "═══════════════════════════════════════════"
echo ""
echo "  Palace:    ~/.mempalace/"
echo "  Runtime:   $RUNTIME_DIR"
echo "  Package:   $(cd / && $RUNTIME_PYTHON -c 'import mempalace; print(mempalace.__file__)')"
echo "  CLI:       $(command -v mempalace 2>/dev/null || echo 'not on PATH')"
echo "  MCP:       $(command -v mempalace-mcp 2>/dev/null || echo 'not on PATH')"
echo "  Bridge:    $(command -v mempalace-mcp-bridge 2>/dev/null || echo 'not on PATH')"
if $ENABLE_SINGLETON; then
  echo "  Singleton: installed (run: $RUNTIME_PYTHON -m mempalace.cli singleton status)"
else
  echo "  Singleton: NOT installed (run: $RUNTIME_PYTHON -m mempalace.cli singleton install --start)"
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
echo "  2. Verify:      $RUNTIME_PYTHON -m mempalace.cli status && $RUNTIME_PYTHON -m mempalace.cli singleton status"
echo "  3. Test recall: /mempalace:search 'your query'"
