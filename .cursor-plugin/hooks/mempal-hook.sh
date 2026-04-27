#!/usr/bin/env bash
# MemPalace Cursor hook wrapper — thin shim over `mempalace hook run`.
#
# Cursor's GUI-app launch path does NOT source ~/.zshrc (only ~/.zshenv is
# sourced by non-interactive shells, and not even reliably for LaunchServices
# descendants). To stay robust, every MEMPAL_* var defaults to a working local
# config here via ${VAR:-default} fallback. Override by editing ~/.mempalace/env
# and re-running scripts/sync-plugins.sh, or by setting launchctl env vars.
set -euo pipefail
HOOK_NAME="${1:?Usage: mempal-hook.sh <hook-name>}"

export SSL_CERT_FILE="${SSL_CERT_FILE:-/opt/homebrew/etc/openssl@3/cert.pem}"

# Embedding triple — all three required; a missing one falls back to
# ChromaDB's built-in MiniLM (384 dims) which mismatches a palace built
# with Gemini (3072 dims) → every upsert silently fails with a dim error.
export MEMPAL_EMBEDDING_MODEL="${MEMPAL_EMBEDDING_MODEL:-gemini-embedding-2-preview}"
export MEMPAL_EMBEDDING_ENDPOINT="${MEMPAL_EMBEDDING_ENDPOINT:-http://127.0.0.1:4000}"
export MEMPAL_EMBEDDING_KEY="${MEMPAL_EMBEDDING_KEY:-sk-litellm-local}"

# LLM endpoint — shared by async save + recall (Stage 1 rewrite, Stage
# 5 rerank). Missing endpoint+model silently drops save and falls back to
# raw vector search. Defaults point at the same LiteLLM proxy used by
# embedding so a fresh install works out of the box. Canonical
# MEMPAL_LLM_*; legacy MEMPAL_RECALL_* aliases set for backward compat.
export MEMPAL_LLM_ENDPOINT="${MEMPAL_LLM_ENDPOINT:-${MEMPAL_RECALL_ENDPOINT:-http://127.0.0.1:4000/v1}}"
export MEMPAL_LLM_MODEL="${MEMPAL_LLM_MODEL:-${MEMPAL_RECALL_MODEL:-gemini-3.1-flash-lite-preview}}"
export MEMPAL_LLM_KEY="${MEMPAL_LLM_KEY:-${MEMPAL_RECALL_KEY:-sk-litellm-local}}"

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

run_mempalace_hook --hook "$HOOK_NAME" --harness cursor
