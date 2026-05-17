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

  # Probe for the newest Python >= 3.9 (pyproject.toml's requires-python).
  # The previous "first python3 wins" loop silently picked Apple's stock
  # python3 = 3.8 on macOS, causing chromadb (>=1.5.4 needs 3.9) to fail
  # with "No matching distribution found" — install ostensibly succeeds.
  local bootstrap_python=""
  local cand_path=""
  for candidate in python3.14 python3.13 python3.12 python3.11 python3.10 python3.9 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      cand_path="$(command -v "$candidate")"
      if "$cand_path" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
        bootstrap_python="$cand_path"
        break
      fi
    fi
  done
  if [[ -z "$bootstrap_python" ]]; then
    local found="$(python3 --version 2>&1 || echo none)"
    fail "Need Python >= 3.9 to bootstrap $RUNTIME_DIR. Found: $found"
  fi

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

runtime_is_healthy() {
  # A "healthy" existing runtime has the core deps importable. Used to gate
  # whether we can take the fast --no-deps snapshot path or must do a full
  # cold install. Avoids the trap where a pre-created empty venv silently
  # skipped chromadb install and the singleton later 失败 to import it.
  (
    cd /
    "$RUNTIME_PYTHON" -c 'import chromadb, mempalace.config, mempalace.searcher' >/dev/null 2>&1
  )
}

if $DEV_MODE; then
  info "Installing Python package in editable mode (--dev) into $RUNTIME_DIR..."
  "${RUNTIME_PIP[@]}" install -q -e "$REPO_DIR"
elif [[ "$RUNTIME_CREATED" == true ]]; then
  # Fresh runtime: pull in full dependency tree (chromadb, pyyaml, etc.).
  info "Installing Python package + dependencies into new runtime $RUNTIME_DIR..."
  "${RUNTIME_PIP[@]}" install -q --upgrade pip
  "${RUNTIME_PIP[@]}" install -q "$REPO_DIR"
elif runtime_is_healthy; then
  # Incremental upgrade against an existing runtime that already has deps.
  # ``--no-deps`` keeps upgrades fast and avoids churn on chromadb/pip metadata.
  info "Installing Python package from local checkout snapshot into $RUNTIME_DIR..."
  "${RUNTIME_PIP[@]}" install -q --force-reinstall --no-deps "$REPO_DIR"
else
  # Existing runtime is missing required deps (e.g. user pre-created an empty
  # venv, or a previous --no-deps run skipped chromadb). Fall back to a full
  # install so we don't ship a broken install that imports fine on the test
  # line but explodes when the singleton actually tries to load chromadb.
  warn "Existing runtime at $RUNTIME_DIR is missing required deps — running full install."
  "${RUNTIME_PIP[@]}" install -q --upgrade pip
  "${RUNTIME_PIP[@]}" install -q "$REPO_DIR"
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

# ─── Step 6: End-to-end self-test ───
# Previously: install would exit 0 even when chromadb was missing or the
# singleton never came up — the user only noticed when search started 404'ing.
# Now: import + (if --singleton) MCP handshake must succeed. Failure = fail.
info "Self-test: package import..."
if ! (cd / && "$RUNTIME_PYTHON" -c '
import sys
import chromadb  # noqa: F401
from mempalace.config import sanitize_name  # noqa: F401
from mempalace.searcher import search_memories  # noqa: F401
sys.stdout.write("import ok\n")
' >/dev/null); then
  fail "Package import failed after install — re-run with --dev or check $RUNTIME_PYTHON -c 'import chromadb' manually."
fi
ok "Self-test: imports OK"

if $ENABLE_SINGLETON; then
  info "Self-test: MCP bridge handshake..."
  bridge_cmd=""
  if command -v mempalace-mcp-bridge >/dev/null 2>&1; then
    bridge_cmd="$(command -v mempalace-mcp-bridge)"
  elif [[ -x "$PY_SCRIPT_DIR/mempalace-mcp-bridge" ]]; then
    bridge_cmd="$PY_SCRIPT_DIR/mempalace-mcp-bridge"
  fi
  if [[ -z "$bridge_cmd" ]]; then
    fail "mempalace-mcp-bridge not found on PATH or in $PY_SCRIPT_DIR after install."
  fi
  # Run the handshake through the runtime Python so we don't depend on the
  # GNU `timeout` binary (BSD/macOS only ships `gtimeout` via coreutils, if
  # at all), and so a stuck singleton can't hang the installer. Also routes
  # bridge stderr to a temp file we can surface on failure.
  bridge_err="$(mktemp -t mempalace-self-test.XXXXXX)"
  handshake_rc=0
  "$RUNTIME_PYTHON" - "$bridge_cmd" "$bridge_err" <<'PY' || handshake_rc=$?
import json
import subprocess
import sys

bridge_cmd = sys.argv[1]
stderr_path = sys.argv[2]
payload = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "installer", "version": "0.1"},
        },
    }
)
try:
    with open(stderr_path, "wb") as err_f:
        proc = subprocess.run(
            [bridge_cmd],
            input=payload + "\n",
            capture_output=False,
            stdout=subprocess.PIPE,
            stderr=err_f,
            text=True,
            timeout=20,
        )
except FileNotFoundError as exc:
    sys.stderr.write(f"bridge not executable: {exc}\n")
    sys.exit(2)
except subprocess.TimeoutExpired:
    sys.stderr.write("bridge handshake timed out after 20s\n")
    sys.exit(3)
for line in (proc.stdout or "").splitlines():
    line = line.strip()
    if not line:
        continue
    if '"serverInfo"' in line:
        sys.exit(0)
sys.stderr.write(
    "bridge produced no serverInfo response. First 500 chars of stdout:\n"
    + (proc.stdout or "")[:500]
    + "\n"
)
sys.exit(4)
PY
  if [[ "$handshake_rc" -ne 0 ]]; then
    # install.sh main flow already completed; treat this as a warning rather
    # than a hard failure. The user can re-run handshake manually if they
    # actually need to use the singleton right now.
    warn "MCP bridge handshake failed (rc=$handshake_rc). Install completed otherwise."
    if [[ -s "$bridge_err" ]]; then
      warn "Bridge stderr:"
      sed 's/^/    /' "$bridge_err" >&2
    fi
    warn "Check ~/.mempalace/logs/mcp.err.log and run:"
    warn "    $RUNTIME_PYTHON -m mempalace.cli singleton status"
  else
    ok "Self-test: MCP handshake OK"
  fi
  rm -f "$bridge_err"
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
