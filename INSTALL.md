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

## Post-Install

1. **Restart** Claude Code / Codex CLI
2. **Verify**: run `mempalace status` or use `/mempalace:status` in Claude Code

## Environment Variables (Required for Full Functionality)

`install.sh` installs the package and plugins but does **not** write env vars. Each agent reads from its own config — `~/.zshrc` alone is not enough (MCP servers and launchd services don't source shell profiles).

Two groups of variables, both routed through a LiteLLM proxy at `127.0.0.1:4000`:

| Group | Vars | Purpose |
|---|---|---|
| Embedding | `MEMPAL_EMBEDDING_{MODEL,ENDPOINT,KEY}` | ChromaDB vectorization (Gemini embedding). All three required; missing any falls back to MiniLM which mismatches a 3072-dim palace. |
| Recall LLM | `MEMPAL_RECALL_LLM=1` + `MEMPAL_RECALL_{ENDPOINT,MODEL,KEY}` | Gates async save + recall rewrite/rerank (Gemini 3.1 Flash-Lite). Missing the quartet silently disables both — buffered turns get dropped. |

Recommended values (LiteLLM proxy form, consistent across agents):
```
MEMPAL_EMBEDDING_MODEL=gemini-embedding-2-preview
MEMPAL_EMBEDDING_ENDPOINT=http://127.0.0.1:4000
MEMPAL_EMBEDDING_KEY=sk-litellm-local
MEMPAL_RECALL_LLM=1
MEMPAL_RECALL_ENDPOINT=http://127.0.0.1:4000/v1
MEMPAL_RECALL_MODEL=gemini-3.1-flash-lite-preview
MEMPAL_RECALL_KEY=sk-litellm-local
```

Where to put them:

- **Claude Code** → `~/.claude/settings.json` → `env` block
- **Codex** → `~/.codex/config.toml` in **both** `[mcp_servers.mempalace].env` (for the MCP server) and `[shell_environment_policy.set]` (for hook subprocesses — they don't inherit the MCP env)
- **Hermes** → `~/Library/LaunchAgents/ai.hermes.gateway.plist` under `EnvironmentVariables`, then `launchctl unload && launchctl load` the plist

See `CHANGELOG.md` → **Multi-Agent Environment Setup** for the full copy-paste snippets.

### Verify and sync

```bash
bash scripts/sync-plugins.sh
```

Step `[6/6]` checks all seven variables are present in each agent's config and warns on drift.

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
