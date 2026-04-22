#!/bin/bash
# MemPalace UserPromptSubmit Hook — search palace and inject relevant memories
export SSL_CERT_FILE="${SSL_CERT_FILE:-/opt/homebrew/etc/openssl@3/cert.pem}"
# All logic lives in mempalace.hooks_cli for cross-harness extensibility
INPUT=$(cat)
echo "$INPUT" | python3 -m mempalace hook run --hook userprompt --harness claude-code
