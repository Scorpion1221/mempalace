#!/usr/bin/env bash
set -euo pipefail
HOOK_NAME="${1:?Usage: mempal-hook.sh <hook-name>}"
export SSL_CERT_FILE="${SSL_CERT_FILE:-/opt/homebrew/etc/openssl@3/cert.pem}"
# MemPalace embedding via LiteLLM (or any OpenAI-compatible) proxy.
# All three vars must be set for the proxy embedding path; if any is missing
# the embedding factory falls back to ChromaDB's built-in MiniLM (384 dims),
# which mismatches a palace built with Gemini (3072 dims).
export MEMPAL_EMBEDDING_MODEL="${MEMPAL_EMBEDDING_MODEL:-gemini-embedding-2-preview}"
export MEMPAL_EMBEDDING_ENDPOINT="${MEMPAL_EMBEDDING_ENDPOINT:-http://127.0.0.1:4000}"
export MEMPAL_EMBEDDING_KEY="${MEMPAL_EMBEDDING_KEY:-sk-litellm-local}"
# LLM endpoint shared by async-save and recall enhancement. Without
# these the Stop hook silently skips save (no-LLM mode), which is the
# wrong default. Canonical MEMPAL_LLM_*; legacy MEMPAL_RECALL_* aliases
# are still honored if the user already set them in ~/.mempalace/env.
export MEMPAL_LLM_ENDPOINT="${MEMPAL_LLM_ENDPOINT:-${MEMPAL_RECALL_ENDPOINT:-http://127.0.0.1:4000/v1}}"
export MEMPAL_LLM_MODEL="${MEMPAL_LLM_MODEL:-${MEMPAL_RECALL_MODEL:-gemini-3.1-flash-lite-preview}}"
export MEMPAL_LLM_KEY="${MEMPAL_LLM_KEY:-${MEMPAL_RECALL_KEY:-sk-litellm-local}}"

run_mempalace_hook() {
  _cleanup_stderr_file() {
    local file="${1:-}"
    [ -n "$file" ] || return 0
    /bin/rm -f "$file" 2>/dev/null || :
  }

  _emit_file_to_stderr() {
    local file="${1:-}"
    [ -n "$file" ] || return 0
    [ -s "$file" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
      printf '%s\n' "$line" >&2
    done < "$file"
  }

  _run_with_clean_stdout() {
    local suppress_status2="$1"
    shift
    local output status stderr_file
    stderr_file="${TMPDIR:-/tmp}/mempalace-hook-stderr.$$.$RANDOM"
    : > "$stderr_file" || return 1

    if output="$("$@" 2>"$stderr_file")"; then
      printf '%s\n' "$output"
      _cleanup_stderr_file "$stderr_file"
      return 0
    fi

    status=$?
    # argparse exits 2 for stale CLIs that do not know a newly added hook.
    # Continue to the Python/runtime fallback instead of blocking the host app.
    if [ "$suppress_status2" = "yes" ] && [ "$status" -eq 2 ]; then
      _cleanup_stderr_file "$stderr_file"
      return 1
    fi

    _emit_file_to_stderr "$stderr_file"
    if [ -n "$output" ]; then
      printf '%s\n' "$output" >&2
    fi
    _cleanup_stderr_file "$stderr_file"
    return "$status"
  }

  _try_mempalace_cli() {
    local cli="$1"
    shift
    if [ -z "$cli" ]; then
      return 1
    fi
    if [[ "$cli" == */* ]]; then
      [ -x "$cli" ] || return 1
    else
      command -v "$cli" >/dev/null 2>&1 || return 1
    fi

    _run_with_clean_stdout yes "$cli" hook run "$@"
  }

  _try_python_runner() {
    local py="$1"
    shift
    if [ -z "$py" ]; then
      return 1
    fi
    if [[ "$py" == */* ]]; then
      [ -x "$py" ] || return 1
    else
      command -v "$py" >/dev/null 2>&1 || return 1
    fi
    if "$py" -c "import mempalace" >/dev/null 2>&1; then
      _run_with_clean_stdout no "$py" -m mempalace hook run "$@"
      return $?
    fi
    return 1
  }

  # GUI-launched agents often have a minimal PATH. Prefer the dedicated
  # MemPalace runtime installed by install.sh/update before any PATH shim.
  _try_mempalace_cli "${MEMPAL_RUNTIME_CLI:-$HOME/.mempalace/venv/bin/mempalace}" "$@" && return 0
  _try_mempalace_cli mempalace "$@" && return 0
  _try_python_runner "${MEMPAL_RUNTIME_PYTHON:-$HOME/.mempalace/venv/bin/python3}" "$@" && return 0
  _try_python_runner python3 "$@" && return 0
  _try_python_runner python "$@" && return 0

  echo '{}'
  return 0
}
run_mempalace_hook --hook "$HOOK_NAME" --harness codex
