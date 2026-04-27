#!/bin/bash
# MemPalace Stop Hook — thin wrapper calling Python CLI
export SSL_CERT_FILE="${SSL_CERT_FILE:-/opt/homebrew/etc/openssl@3/cert.pem}"
# MemPalace embedding via LiteLLM proxy — see userprompt hook for the
# rationale. Defaults match the standard local LiteLLM setup.
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
# All logic lives in mempalace.hooks_cli for cross-harness extensibility
run_mempalace_hook() {
  if command -v mempalace >/dev/null 2>&1; then
    mempalace hook run "$@"
    return $?
  fi

  if command -v python3 >/dev/null 2>&1 && python3 -c "import mempalace" >/dev/null 2>&1; then
    python3 -m mempalace hook run "$@"
    return $?
  fi

  if command -v python >/dev/null 2>&1 && python -c "import mempalace" >/dev/null 2>&1; then
    python -m mempalace hook run "$@"
    return $?
  fi

  echo "MemPalace hook error: could not find a runnable mempalace command or module" >&2
  return 1
}

run_mempalace_hook --hook stop --harness claude-code
