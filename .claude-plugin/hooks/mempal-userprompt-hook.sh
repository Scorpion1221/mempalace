#!/bin/bash
# MemPalace UserPromptSubmit Hook — search palace and inject relevant memories
export SSL_CERT_FILE="${SSL_CERT_FILE:-/opt/homebrew/etc/openssl@3/cert.pem}"

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
