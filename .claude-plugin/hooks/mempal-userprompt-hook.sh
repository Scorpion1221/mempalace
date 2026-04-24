#!/bin/bash
# MemPalace UserPromptSubmit Hook — search palace and inject relevant memories
export SSL_CERT_FILE="${SSL_CERT_FILE:-/opt/homebrew/etc/openssl@3/cert.pem}"
# MemPalace embedding via LiteLLM (or any OpenAI-compatible) proxy. All three
# must be set together; a missing one falls back to ChromaDB's built-in
# MiniLM (384 dims), which mismatches a palace built with Gemini (3072 dims).
export MEMPAL_EMBEDDING_MODEL="${MEMPAL_EMBEDDING_MODEL:-gemini-embedding-2-preview}"
export MEMPAL_EMBEDDING_ENDPOINT="${MEMPAL_EMBEDDING_ENDPOINT:-http://127.0.0.1:4000}"
export MEMPAL_EMBEDDING_KEY="${MEMPAL_EMBEDDING_KEY:-sk-litellm-local}"

run_mempalace_hook() {
  if command -v mempalace >/dev/null 2>&1; then
    mempalace hook run "$@"
    return $?
  fi
  if command -v python3 >/dev/null 2>&1 && python3 -c "import mempalace" >/dev/null 2>&1; then
    python3 -m mempalace hook run "$@"
    return $?
  fi
  echo '{}'
  return 0
}

run_mempalace_hook --hook userprompt --harness claude-code
