# MemPalace Installation Guide

## Prerequisites

- Python 3.10+
- pip3
- Claude Code, Codex CLI, or Cursor IDE
- Docker (for LiteLLM proxy) or `pip install litellm`

## Quick Install

```bash
git clone git@github.com:Scorpion1221/mempalace.git ~/git/mempalace
cd ~/git/mempalace

# Step 1: Install MemPalace
bash install.sh          # Both Claude Code + Codex (default)
# bash install.sh --claude  # Claude Code only
# bash install.sh --codex   # Codex only

# Step 2: Set up LiteLLM proxy (embedding + recall LLM)
cd litellm
bash setup.sh            # Auto-detects Docker/Python, guides through config
```

That's it. The `install.sh` script handles Python package, CLI, palace init, and
plugin sync. The `litellm/setup.sh` script detects existing installs, validates
config, and starts the proxy.

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
| MCP server | `~/.codex/config.toml` → `[mcp_servers.mempalace]` |
| Hooks | `~/.codex/hooks.json` → UserPromptSubmit + Stop |
| Skills | `~/.codex/vendor_imports/skills/skills/.curated/mempalace-*` |
| Feature flag | `~/.codex/config.toml` → `[features] codex_hooks = true` |

### Cursor IDE

| Component | Location |
|-----------|----------|
| Plugin | `~/.cursor/plugins/local/mempalace` (symlink) |
| Hooks | `sessionStart` (palace map), `stop` (auto-save), `preCompact` (emergency) |
| MCP server | Auto-registered via `plugin.json.mcpServers` |
| Rule | `rules/mempalace-recall.mdc` — instructs agent to call `mempalace_search` |

Cursor has no per-prompt hook for context injection. Auto-recall uses:
- `sessionStart` injects a palace map (top wings, recent saves, KG entities)
- `rules/mempalace-recall.mdc` tells the agent to call `mempalace_search` MCP

### Hermes

Hermes users: run `bash scripts/sync-plugins.sh --hermes` after the above.

## LiteLLM Proxy Setup

MemPalace needs a LiteLLM proxy for embedding (palace vectorisation) and recall
LLM (rerank + save extraction). The `litellm/` directory ships a ready-to-run
Docker config.

```bash
cd ~/git/mempalace/litellm
bash setup.sh
```

The script:
1. Auto-detects existing LiteLLM (Docker running/stopped, Python, or none)
2. Creates `.env` from template if missing (prompts for `GEMINI_API_KEY`)
3. Validates backend alignment (Gemini API vs Vertex AI)
4. Starts/restarts the proxy with health check

### Backends

Two ways to reach Gemini:

**(A) Gemini API** (default — simplest)
```bash
# litellm/.env
GEMINI_API_KEY=your-key-here
```
Get a key at https://aistudio.google.com/apikey

**(B) Vertex AI** (for orgs already on Vertex)
```bash
# litellm/.env
VERTEXAI_PROJECT=your-vertex-project-id
VERTEXAI_LOCATION=global
```
Then edit `litellm/config.yaml`: comment out `gemini/*` models, uncomment
`vertex_ai/*` ones. The shipped Vertex examples use the real preview model
IDs (`vertex_ai/gemini-embedding-2-preview`,
`vertex_ai/gemini-3.1-flash-lite-preview`). Set `vertex_credentials` on each
uncommented Vertex entry to your own service-account JSON path, e.g.
`vertex_credentials: /path/to/vertex-service-account.json`.

## Environment Variables

MemPalace uses a single-source env file at `~/.mempalace/env` that propagates
to all agents. The `scripts/sync-plugins.sh` script (called by `install.sh`)
creates this from `scripts/mempalace-env.template` if missing.

Default values (match LiteLLM proxy defaults):
```bash
export MEMPAL_EMBEDDING_MODEL="gemini-embedding-2-preview"
export MEMPAL_EMBEDDING_ENDPOINT="http://127.0.0.1:4000"
export MEMPAL_EMBEDDING_KEY="sk-litellm-local"

# One LLM endpoint, shared by async-save and recall enhancement.
# Default-on whenever endpoint+model are set. Opt out with MEMPAL_LLM=0.
export MEMPAL_LLM_ENDPOINT="http://127.0.0.1:4000/v1"
export MEMPAL_LLM_MODEL="gemini-3.1-flash-lite-preview"
export MEMPAL_LLM_KEY="sk-litellm-local"
```

> **Backward compat**: pre-3.4 envs that set `MEMPAL_RECALL_ENDPOINT` /
> `MEMPAL_RECALL_MODEL` / `MEMPAL_RECALL_KEY` (or `MEMPAL_RECALL_LLM=1`)
> are still honored as legacy aliases — no migration needed.

After editing `~/.mempalace/env`, run:
```bash
bash ~/git/mempalace/scripts/sync-plugins.sh
```

This propagates the values into every agent's native config format (Claude Code
`settings.json`, Codex `config.toml`, Hermes launchd plist, Cursor launchctl).

**Why single-source matters**: Manually keeping four agents in sync caused the
"drawer save silently fails on Hermes" bug (2026-04-25) when Hermes plist
drifted from the LiteLLM config the other agents had. Step `[7/8]` of
`sync-plugins.sh` validates all agents match `~/.mempalace/env` and complains
on drift.

## Key Features

### Auto-Recall (UserPromptSubmit Hook)

Every user message automatically triggers a MemPalace search. Relevant memories
are injected into the AI's context via `<mempalace-recall>` tags before the
model processes the prompt.

- Trivial prompts ("ok", "hi", "continue") are skipped
- Results are filtered by relevance (cosine distance < 1.5)
- Current project wing is soft-boosted based on cwd

### Auto-Save (Stop Hook)

Every 3 human messages, the AI is prompted to save session content to MemPalace
using AAAK-compressed diary entries and verbatim drawers.

### Codex-Specific Notes

- Codex requires `[features] codex_hooks = true` in `config.toml` for
  `additionalContext` injection to work
- Codex does NOT support `PreCompact` hook event
- Codex uses `prompt` field (not `user_prompt`) in hook input; handler supports both
- Codex reads hooks from `~/.codex/hooks.json` (global), not plugin directory

## Updating

Since it's an editable install, Python code changes take effect immediately:

```bash
cd ~/git/mempalace
git pull
bash scripts/sync-plugins.sh  # Sync plugin files to all agents
```

## Development Workflow

When you modify MemPalace code, use flag-based sync:

```bash
bash scripts/sync-plugins.sh              # Sync all 4 agents (auto-skip missing)
bash scripts/sync-plugins.sh --claude     # Sync Claude Code only
bash scripts/sync-plugins.sh --codex      # Sync Codex only
bash scripts/sync-plugins.sh --hermes     # Sync Hermes only
bash scripts/sync-plugins.sh --cursor     # Sync Cursor only
```

The script does 8 things:
1. Snapshot-reinstall Python package
2. Sync Claude Code plugin + `settings.json` env
3. Sync Codex plugin + `config.toml` env
4. Sync Hermes plugin + launchd plist env
5. Sync Cursor plugin + launchctl env
6. Restart Hermes if running
7. **Validate** all agents' env values match `~/.mempalace/env`
8. Summary

## AI Agent-Assisted Install

If you're installing via an AI coding assistant (Claude Code, Codex, Cursor),
see [docs/INSTALL-FOR-AGENTS.md](docs/INSTALL-FOR-AGENTS.md). That guide tells
the agent to use `AskUserQuestion` to collect install preferences, then run
the right commands on your behalf.

## Troubleshooting

### "Drawer save silently fails"

Check env var alignment:
```bash
bash ~/git/mempalace/scripts/sync-plugins.sh
```
Step `[7/8]` validates all agents match `~/.mempalace/env`. If drift is
detected, the script shows which agent has which value.

### "Embedding dimension mismatch"

ChromaDB stores embeddings with a fixed dimensionality. The palace is
initialised with whatever dimension the first embedding call returns (3072 for
`gemini-embedding-001` with `output_dimensionality: 3072`).

**Changing `output_dimensionality` in `litellm/config.yaml` after the palace
exists will silently break every upsert** — ChromaDB rejects vectors with the
wrong dimension.

If you must change it, wipe `~/.mempalace/palace/` and re-ingest.

### "LiteLLM proxy not responding"

```bash
cd ~/git/mempalace/litellm
docker compose logs -f              # Check logs
docker compose restart              # Restart proxy
curl http://127.0.0.1:4000/health/readiness  # Health check
```

### "Hooks not firing"

- **Claude Code**: Restart after `install.sh` or `sync-plugins.sh`
- **Codex**: Check `~/.codex/config.toml` has `[features] codex_hooks = true`
- **Cursor**: Check `~/.cursor/hooks.json` has `mempal-hook.sh` entries
- **Hermes**: Check plist env: `plutil -p ~/Library/LaunchAgents/ai.hermes.gateway.plist | grep MEMPAL`

## Uninstall

```bash
# Remove Python package
pip3 uninstall mempalace

# Remove palace data
rm -rf ~/.mempalace/

# Remove agent plugins
rm -rf ~/.claude/plugins/cache/mempalace/
rm -rf ~/.agents/plugins/mempalace/
rm -rf ~/.cursor/plugins/local/mempalace
rm -rf ~/.hermes/hermes-agent/plugins/memory/mempalace

# Remove hooks (manual — check each agent's hooks.json)
```
