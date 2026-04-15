#!/bin/bash
# MemPalace UserPromptSubmit Hook — search palace and inject relevant memories
# All logic lives in mempalace.hooks_cli for cross-harness extensibility
INPUT=$(cat)
echo "$INPUT" | python3 -m mempalace hook run --hook userprompt --harness claude-code
