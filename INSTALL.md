# MemPalace Installation Guide (Fork)

Private fork: `github.com/Scorpion1221/mempalace`

## Prerequisites

- Python 3.10+
- pip3
- Claude Code and/or Codex CLI installed

## Quick Install

```bash
git clone git@github.com:Scorpion1221/mempalace.git ~/git/mempalace
cd ~/git/mempalace
bash install.sh          # Both Claude Code + Codex
# bash install.sh --claude  # Claude Code only
# bash install.sh --codex   # Codex only
```

The script handles everything:
1. Python package (editable mode)
2. CLI on PATH
3. Palace initialization (`~/.mempalace/`)
4. Claude Code plugin + hooks
5. Codex MCP server + hooks + skills + feature flag

## What Gets Installed

### Claude Code

| Component | Location |
|-----------|----------|
| Plugin cache | `~/.claude/plugins/cache/mempalace/mempalace/local/` |
| Hooks | UserPromptSubmit (recall), Stop (auto-save), PreCompact (emergency save) |
| MCP server | Auto-registered by plugin |
| Skills | `/mempalace:search`, `/mempalace:status`, `/mempalace:mine`, etc. |

### Codex CLI

| Component | Location |
|-----------|----------|
| MCP server | `~/.codex/config.toml` -> `[mcp_servers.mempalace]` |
| Hooks | `~/.codex/hooks.json` -> UserPromptSubmit + Stop |
| Skills | `~/.codex/vendor_imports/skills/skills/.curated/mempalace-*` |
| Feature flag | `~/.codex/config.toml` -> `[features] codex_hooks = true` |

### Cursor IDE

| Component | Location |
|-----------|----------|
| Plugin | `~/.cursor/plugins/local/mempalace` (symlink to `.cursor-plugin/` in this repo) |
| Hooks | `sessionStart` (palace map injection), `stop` (auto-save), `preCompact` (emergency save) |
| MCP server | Auto-registered via `plugin.json.mcpServers` |
| Rule | `rules/mempalace-recall.mdc` — tells the agent to call `mempalace_search` on history-reference phrases |
| Skills | Symlinked from the Codex plugin's skill set (same `/search`, `/status`, `/mine`, `/init`, `/help`) |

Cursor has **no per-prompt hook that accepts context injection** (unlike Claude Code's `UserPromptSubmit` or Codex's equivalent). Auto-recall is instead:
- `sessionStart` injects a palace *map* (top wings, recent saves, KG entities) at the start of every Cursor chat
- `rules/mempalace-recall.mdc` instructs the agent to call `mempalace_search` MCP when the user references past work

Same `~/.mempalace/` palace as Claude Code / Codex / Hermes — cross-agent memory on one machine.

## Post-Install

1. **Restart** Claude Code / Codex CLI
2. **Verify**: run `mempalace status` or use `/mempalace:status` in Claude Code

## Environment Variables (single-source workflow)

There are 7 `MEMPAL_*` env vars driving embedding (3) and recall LLM (4). They live in **one** file: `~/.mempalace/env`.

**As of 3.3.4**, the mempalace package itself loads this file on import via `python-dotenv`, so any process that imports `mempalace` (CLI, `mempalace-mcp`, hooks) sees the values automatically — even when started by launchd / systemd / Docker, which do not source shell rc files. Process env still wins over the file, so explicit overrides keep working.

`scripts/sync-plugins.sh` is still useful when you want the values mirrored into each agent's native config (Claude Code `settings.json`, Codex `config.toml`, Hermes plist) for visibility or for non-mempalace consumers, but it is no longer required for mempalace itself to function.

```bash
# First time? Copy the shipped template:
cp scripts/mempalace-env.template ~/.mempalace/env
$EDITOR ~/.mempalace/env

# Optional: mirror values into every agent's native config + validate drift
bash scripts/sync-plugins.sh
```

What gets propagated where:

| Agent | Env target |
|---|---|
| Claude Code | `~/.claude/settings.json` → `env` block |
| Codex | `~/.codex/config.toml` → `[mcp_servers.mempalace].env` AND `[shell_environment_policy.set]` |
| Hermes | `~/Library/LaunchAgents/ai.hermes.gateway.plist` → `EnvironmentVariables` (then launchd reload) |
| Cursor (GUI) | `launchctl setenv` for the current login session + `~/Library/LaunchAgents/ai.mempalace.env.plist` for reboot persistence |

### Why this matters

- GUI apps (Cursor, sometimes Claude Desktop) launched via macOS LaunchServices do NOT source `~/.zshrc`. The launchctl + plist path is the only reliable way to seed their env.
- launchd daemons (Hermes) don't source any shell profile. Their plist is the only env source.
- Codex needs env in TWO places — its MCP server and its hook subprocesses don't share env.

Manually keeping all four agents in sync is what caused mempalace's "drawer save silently fails on Hermes" bug in 2026-04-25 (Hermes plist drifted from the LiteLLM proxy config the other agents had). The single-source workflow is the fix.

### Reference values

The shipped `scripts/mempalace-env.template`:

```bash
export MEMPAL_EMBEDDING_MODEL="gemini-embedding-2-preview"
export MEMPAL_EMBEDDING_ENDPOINT="http://127.0.0.1:4000"   # LiteLLM (or any OpenAI-compat /v1/embeddings) proxy
export MEMPAL_EMBEDDING_KEY="sk-litellm-local"             # LiteLLM master key
export MEMPAL_RECALL_LLM="1"                                # enable LLM-enhanced recall + async save
export MEMPAL_RECALL_ENDPOINT="http://127.0.0.1:4000/v1"   # OpenAI-compat endpoint
export MEMPAL_RECALL_MODEL="gemini-3.1-flash-lite-preview"
export MEMPAL_RECALL_KEY="sk-litellm-local"
```

Step `[7/8]` of `sync-plugins.sh` reads these from `~/.mempalace/env` and verifies every agent's config has the same value, complaining loudly on drift.

## Key Features

### Auto-Recall (UserPromptSubmit Hook)

Every user message automatically triggers a MemPalace search. Relevant memories are injected into the AI's context via `<mempalace-recall>` tags before the model processes the prompt.

- Trivial prompts ("ok", "hi", "continue") are skipped
- Results are filtered by relevance (cosine distance < 1.5)
- Current project wing is soft-boosted based on cwd

### Auto-Save (Stop Hook)

Every 3 human messages, the AI is prompted to save session content to MemPalace using AAAK-compressed diary entries and verbatim drawers.

### Codex-Specific Notes

- Codex requires `[features] codex_hooks = true` in `config.toml` for `additionalContext` injection to work
- Codex does NOT support `PreCompact` hook event
- Codex uses `prompt` field (not `user_prompt`) in hook input; the handler supports both
- Codex reads hooks from `~/.codex/hooks.json` (global), not from plugin directory

## Updating

Since it's an editable install, Python code changes take effect immediately:

```bash
cd ~/git/mempalace
git pull
# Restart Claude Code / Codex to reload hooks
```

If `pyproject.toml` dependencies changed:

```bash
pip3 install -e ~/git/mempalace
```

If hook scripts or plugin config changed, re-run the installer:

```bash
bash ~/git/mempalace/install.sh
```

## Uninstall

```bash
pip3 uninstall mempalace
rm -rf ~/.mempalace                              # Palace data (CAUTION: deletes all memories)
rm -rf ~/.claude/plugins/cache/mempalace          # Claude Code plugin cache
# Manually remove [mcp_servers.mempalace] from ~/.codex/config.toml
# Manually remove mempal hooks from ~/.codex/hooks.json
```
