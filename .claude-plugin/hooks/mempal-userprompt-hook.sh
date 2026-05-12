#!/bin/bash
# MemPalace UserPromptSubmit Hook — search palace and inject relevant memories
export SSL_CERT_FILE="${SSL_CERT_FILE:-/opt/homebrew/etc/openssl@3/cert.pem}"
# MemPalace embedding via LiteLLM (or any OpenAI-compatible) proxy. All three
# must be set together; a missing one falls back to ChromaDB's built-in
# MiniLM (384 dims), which mismatches a palace built with Gemini (3072 dims).
export MEMPAL_EMBEDDING_MODEL="${MEMPAL_EMBEDDING_MODEL:-gemini-embedding-2-preview}"
export MEMPAL_EMBEDDING_ENDPOINT="${MEMPAL_EMBEDDING_ENDPOINT:-http://127.0.0.1:4000}"
export MEMPAL_EMBEDDING_KEY="${MEMPAL_EMBEDDING_KEY:-sk-litellm-local}"
# LLM endpoint shared by async-save and recall (Stage 1 query rewrite,
# Stage 5 rerank). Canonical MEMPAL_LLM_*; legacy MEMPAL_RECALL_* aliases
# remain honored.
export MEMPAL_LLM_ENDPOINT="${MEMPAL_LLM_ENDPOINT:-${MEMPAL_RECALL_ENDPOINT:-http://127.0.0.1:4000/v1}}"
export MEMPAL_LLM_MODEL="${MEMPAL_LLM_MODEL:-${MEMPAL_RECALL_MODEL:-gemini-3.1-flash-lite-preview}}"
export MEMPAL_LLM_KEY="${MEMPAL_LLM_KEY:-${MEMPAL_RECALL_KEY:-sk-litellm-local}}"

run_mempalace_hook() {
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

    local output status
    if output="$($cli hook run "$@" 2>&1)"; then
      printf '%s\n' "$output"
      return 0
    fi
    status=$?
    # argparse exits 2 for stale CLIs that do not know a newly added hook.
    # Continue to the Python/runtime fallback instead of blocking the host app.
    if [ "$status" -eq 2 ]; then
      return 1
    fi
    printf '%s\n' "$output" >&2
    return "$status"
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
      "$py" -m mempalace hook run "$@"
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
run_mempalace_hook --hook userprompt --harness claude-code
