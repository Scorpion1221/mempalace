# MemPalace Installation Guide

MemPalace now supports a **singleton-first** local architecture:

- One long-lived local `mempalace-mcp --singleton` server per machine
- One Unix-domain socket shared by every agent: `~/.mempalace/mcp.sock`
- Agent hosts keep using stdio MCP, but they talk to the singleton through
  `mempalace-mcp-bridge`
- If the singleton is not available, the bridge automatically falls back to
  launching a local stdio `mempalace-mcp` subprocess

That gives you the best of both worlds:

- **Shared memory backend + shared process** when the singleton is running
- **Zero hard dependency** on the singleton while bootstrapping or debugging

This guide is the **single source of truth** for installation behavior.
`docs/INSTALL-FOR-AGENTS.md` is the companion guide for AI agents helping a
human perform the install.

## Mental model

### What is shared

These are shared across Claude Code, Codex, Cursor, Hermes, and any future
agent on the same machine:

- `~/.mempalace/palace/` — ChromaDB palace
- `~/.mempalace/knowledge_graph.sqlite3` — temporal KG
- `~/.mempalace/env` — single-source runtime configuration
- `~/.mempalace/mcp.sock` — singleton server socket (when enabled)

### What is per-agent

Each host still has its own local config format:

- Claude Code → plugin cache + `~/.claude/settings.json`
- Codex → `~/.codex/config.toml` + `~/.codex/hooks.json`
- Hermes → plugin runtime + `~/.hermes/.env`
- Cursor → plugin link + hooks.json + launchctl/systemd env

The job of `scripts/sync-plugins.sh` is to keep those per-agent configs aligned
with the single source of truth: `~/.mempalace/env`.

### stdio vs singleton mode

MemPalace supports two ways for agents to talk to MCP:

1. **Preferred** — bridge → singleton socket
   - agent command: `mempalace-mcp-bridge`
   - shared server: `mempalace-mcp --singleton`
   - transport: stdio from agent → UDS socket to singleton

2. **Fallback** — direct stdio subprocess
   - agent command: `mempalace-mcp`
   - one process per client
   - still fully supported

The bridge automatically chooses #1 when `~/.mempalace/mcp.sock` is reachable,
and falls back to #2 when it is not.

## Prerequisites

- Python 3.10+
- `pip3`
- One or more of: Claude Code, Codex CLI, Cursor IDE, Hermes
- Optional LLM backend:
  - Docker (recommended for LiteLLM), or
  - `pip install litellm`, or
  - your own OpenAI-compatible endpoint, or
  - offline mode

## Quick install

### Fast path — auto-detect agents + install singleton

```bash
git clone git@github.com:Scorpion1221/mempalace.git ~/git/mempalace
cd ~/git/mempalace
bash install.sh --singleton
```

What this does:

1. Installs the Python package
2. Installs three binaries on PATH:
   - `mempalace`
   - `mempalace-mcp`
   - `mempalace-mcp-bridge`
3. Initializes `~/.mempalace/` if missing
4. Auto-detects Claude / Codex / Hermes / Cursor and syncs supported ones
5. Installs the platform singleton service:
   - macOS → launchd user agent
   - Linux → systemd `--user` service
6. Starts the singleton immediately

### Explicit agent selection

```bash
# Claude Code only
bash install.sh --claude --singleton

# Codex + Hermes
bash install.sh --codex --hermes --singleton

# All four agents
bash install.sh --all --singleton

# Contributors: editable install
bash install.sh --all --singleton --dev
```

## What gets installed

### Claude Code

| Component | Location |
|---|---|
| Plugin cache | `~/.claude/plugins/cache/mempalace/mempalace/<version-or-local>/` |
| MCP command | `mempalace-mcp-bridge` |
| Hooks | UserPromptSubmit, Stop, PreCompact |
| Env sync target | `~/.claude/settings.json` → `env` |
| Skills | `/mempalace:search`, `/mempalace:status`, `/mempalace:mine`, etc. |

### Codex CLI

| Component | Location |
|---|---|
| MCP command | `~/.codex/config.toml` → `[mcp_servers.mempalace]` |
| Hooks | `~/.codex/hooks.json` |
| Env sync target | `~/.codex/config.toml` → `[mcp_servers.mempalace].env` + `[shell_environment_policy.set]` |
| Skills | `~/.codex/vendor_imports/skills/skills/.curated/mempalace-*` |
| Feature flag | `[features] hooks = true` |

### Cursor IDE

| Component | Location |
|---|---|
| Plugin | `~/.cursor/plugins/local/mempalace` |
| MCP command | `mempalace-mcp-bridge` |
| Hooks | `sessionStart`, `stop`, `preCompact` |
| Env sync target | launchctl (macOS) or user env + systemd unit (Linux) |
| Rule | `rules/mempalace-recall.mdc` |

### Hermes

| Component | Location |
|---|---|
| Runtime plugin | `~/.hermes/hermes-agent/plugins/memory/mempalace` |
| MCP usage | Hermes itself uses Python import, not MCP, for its built-in tools |
| Env sync target | `~/.hermes/.env` |
| Restart behavior | `sync-plugins.sh` restarts Hermes when syncing Hermes |

## Single source of truth: `~/.mempalace/env`

MemPalace configuration is not supposed to be hand-maintained in four different
agent configs.

**Always edit:**

- `~/.mempalace/env`

**Then propagate:**

```bash
bash ~/git/mempalace/scripts/sync-plugins.sh
```

This pushes the values into each installed agent's native config format and then
validates that all of them match the env file.

### Default env template

The install flow creates `~/.mempalace/env` from
`scripts/mempalace-env.template` if it does not exist.

Current defaults are:

```bash
export MEMPAL_EMBEDDING_MODEL="gemini-embedding-2-preview"
export MEMPAL_EMBEDDING_ENDPOINT="http://127.0.0.1:4000"
export MEMPAL_EMBEDDING_KEY="***"

export MEMPAL_LLM_ENDPOINT="http://127.0.0.1:4000/v1"
export MEMPAL_LLM_MODEL="gemini-3.1-flash-lite-preview"
export MEMPAL_LLM_KEY="***"
```

### Hard rule

Do **not** hand-edit:

- `~/.codex/config.toml` for `MEMPAL_*`
- `~/.claude/settings.json` for `MEMPAL_*`
- Hermes `~/.hermes/.env` `MEMPAL_*` values
- Cursor launchctl env manually

Those are generated targets, not authoritative sources.

## Singleton manager

MemPalace now ships a small service manager wrapper:

```bash
mempalace singleton install --start
mempalace singleton status
mempalace singleton stop
mempalace singleton uninstall
```

### macOS

- Service manager: **launchd** user agent
- Template: `integrations/launchd/ai.mempalace.server.plist.template`
- Installed plist path: `~/Library/LaunchAgents/ai.mempalace.server.plist`

### Linux

- Service manager: **systemd --user**
- Template: `integrations/systemd/mempalace-server.service.template`
- Installed unit path: `~/.config/systemd/user/mempalace-server.service`

### How the singleton stays alive

The singleton service launches:

```bash
mempalace-mcp --singleton
```

`--singleton` is different from ordinary stdio MCP mode:

- it starts the socket listener
- it **does not** block on stdin JSON-RPC
- it waits for SIGTERM / SIGINT like a normal background service

Without `--singleton`, `mempalace-mcp` exits on stdin EOF, which is why a raw
launchd/systemd wrapper around the stdio mode is not sufficient.

## LLM backend setup

MemPalace needs an OpenAI-compatible endpoint for:

- embeddings
- recall rewrite / rerank
- async-save extraction

There are four supported deployment modes.

### Path A — LiteLLM via Docker (recommended)

```bash
mkdir -p ~/.litellm
cp -n ~/git/mempalace/litellm/config.yaml        ~/.litellm/config.yaml
cp -n ~/git/mempalace/litellm/docker-compose.yml ~/.litellm/docker-compose.yml
cp -n ~/git/mempalace/litellm/.env.example       ~/.litellm/.env.example
[ -f ~/.litellm/.env ] || cp ~/.litellm/.env.example ~/.litellm/.env

# edit ~/.litellm/.env to add GEMINI_API_KEY or Vertex vars
cd ~/.litellm && docker compose up -d
curl -fs http://127.0.0.1:4000/health/readiness && echo "LiteLLM proxy: OK"
```

### Path B — LiteLLM via Python (no Docker)

```bash
pip install litellm
litellm --config ~/.litellm/config.yaml --port 4000 &
```

This is supported, but you own the process lifecycle.

### Path C — Bring your own endpoint

Edit `~/.mempalace/env`:

```bash
export MEMPAL_EMBEDDING_MODEL="<embedding-model-id>"
export MEMPAL_EMBEDDING_ENDPOINT="<base-url>"
export MEMPAL_EMBEDDING_KEY="<api-key>"

export MEMPAL_LLM_ENDPOINT="<base-url>/v1"
export MEMPAL_LLM_MODEL="<llm-model-id>"
export MEMPAL_LLM_KEY="<api-key>"
```

Then:

```bash
bash ~/git/mempalace/scripts/sync-plugins.sh
```

### Path D — Offline mode

No LLM endpoint, no proxy.

- embedding falls back to ChromaDB MiniLM (384d)
- auto-save falls back to verbatim raw transcript mode
- recall uses pure vector search, no LLM rewrite or rerank

To switch into offline mode, comment out or unset the `MEMPAL_*` endpoint/model/key
variables in `~/.mempalace/env`, then re-run sync.

## MCP wiring

### Preferred: singleton + bridge

```bash
mempalace singleton install --start
claude mcp add mempalace -- mempalace-mcp-bridge
```

The bridge will:

- connect to `~/.mempalace/mcp.sock` if the singleton is available
- otherwise fall back to spawning a local `mempalace-mcp`

### Forced direct stdio mode

```bash
claude mcp add mempalace -- mempalace-mcp
```

or temporarily:

```bash
MEMPAL_NO_SINGLETON=1 mempalace-mcp-bridge
```

### Quick helper output

```bash
mempalace mcp
```

This prints the singleton-preferred wiring commands.

## Verification

### Core installation

```bash
mempalace status
python3 -c "import mempalace; print(mempalace.__version__)"
```

### Singleton status

```bash
mempalace singleton status
```

Expected:

- service is running
- `~/.mempalace/mcp.sock` exists
- socket is reachable

### Bridge path

```bash
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}}}' \
  | mempalace-mcp-bridge
```

Expected JSON response includes:

- `serverInfo.name = mempalace`
- `serverInfo.version = <current version>`

## Development workflow

When changing MemPalace code:

```bash
bash scripts/sync-plugins.sh              # all installed agents
bash scripts/sync-plugins.sh --claude
bash scripts/sync-plugins.sh --codex
bash scripts/sync-plugins.sh --hermes
bash scripts/sync-plugins.sh --cursor
```

What `sync-plugins.sh` does:

1. Loads `~/.mempalace/env`
2. Reinstalls the Python package snapshot
3. Syncs Claude plugin + settings env
4. Syncs Codex plugin + config env
5. Syncs Hermes plugin + launchd env
6. Syncs Cursor plugin + launchctl/system env
7. Restarts Hermes if needed
8. Validates all agent configs match the env file

## Troubleshooting

### "Bridge hangs or no response"

Check whether the singleton is reachable:

```bash
mempalace singleton status
ls -la ~/.mempalace/mcp.sock
```

If singleton is down, the bridge should fall back automatically. To bypass the
singleton on purpose:

```bash
MEMPAL_NO_SINGLETON=1 mempalace-mcp-bridge
```

### "Singleton service keeps restarting"

Check logs:

```bash
# macOS
 tail -n 200 ~/.mempalace/logs/mcp.err.log

# Linux
 journalctl --user -u mempalace-server.service -n 200 --no-pager
```

### "Socket exists but is stale"

A stale socket file can remain after abrupt termination. This is safe.
The next `mempalace-mcp --singleton` start unlinks and rebinds it.

If you want to force a clean restart:

```bash
mempalace singleton stop
rm -f ~/.mempalace/mcp.sock
mempalace singleton start
```

### "Environment drift"

```bash
bash ~/git/mempalace/scripts/sync-plugins.sh
```

Step `[7/8]` reports which agent drifted from `~/.mempalace/env`.

### "Embedding dimension mismatch"

ChromaDB locks embedding dimensionality at first write. If you change to a
model with a different output dimension, you must rebuild or wipe the palace.

### "Claude plugin version looks stale"

Three version surfaces exist:

- Python runtime version (`mempalace.__version__`)
- plugin manifests (`plugin.json`, `marketplace.json`)
- Claude's installed plugin cache path metadata

The source of truth is the runtime/package version. The plugin manifests in this
fork are now aligned to `3.3.311`, and `sync-plugins.sh` also rewrites Claude's
`installed_plugins.json` + versioned cache directory to the runtime version. If
Claude still shows an old installed cache entry after a sync, that's now a bug in
the sync path rather than expected metadata drift.

## Uninstall

### Disable singleton

```bash
mempalace singleton uninstall
```

### Remove package + data

```bash
pip3 uninstall mempalace
rm -rf ~/.mempalace/
```

### Remove agent-side runtime files

```bash
rm -rf ~/.claude/plugins/cache/mempalace/
rm -rf ~/.agents/plugins/mempalace/
rm -rf ~/.cursor/plugins/local/mempalace
rm -rf ~/.hermes/hermes-agent/plugins/memory/mempalace
```

## AI-assisted install

If you're having an AI coding assistant install MemPalace on your behalf, see:

- [docs/INSTALL-FOR-AGENTS.md](docs/INSTALL-FOR-AGENTS.md)

That guide tells the agent what to ask, which command to run, and what to
verify — without duplicating the system facts documented here.
