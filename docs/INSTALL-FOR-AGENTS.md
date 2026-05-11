# MemPalace Installation Guide for AI Agents

**Audience**: Claude Code, Codex, Cursor, Hermes, and other AI agents helping a
human install MemPalace.

This file is an **orchestration guide**, not the source of product truth.

- System facts live in [INSTALL.md](../INSTALL.md)
- This guide tells the agent:
  - what to ask
  - which install command to run
  - what to verify
  - which follow-up instructions to give the user

Do **not** duplicate low-level install facts from `INSTALL.md` in your answer.
If you need paths, singleton behavior, env rules, or backend details, read
`INSTALL.md` and cite it implicitly in your actions.

---

## Trigger conditions

Use this guide when the user says things like:

- "help me install mempalace"
- "set up mempalace"
- "install mempalace for claude code"
- "configure mempalace"
- "make mempalace work on this machine"

Do **not** trigger on:

- "what is mempalace"
- "how does mempalace work"
- "show me mempalace docs"

---

## High-level install flow

1. Ask which agent(s) the user wants to use
2. Ask how MemPalace should reach an embedding/LLM backend
3. Choose the correct `install.sh` command
4. Run the install
5. Stage or validate the backend
6. Verify singleton + bridge + basic package health
7. Tell the user exactly what manual restart / UI steps remain

---

## Step 1 — Ask the user 2–3 questions

### Question 1 — Which agent(s)?

Ask the user which of these they want to use on this machine:

- Claude Code
- Codex
- Hermes
- Cursor

Multi-select is allowed.

Map selections to install flags:

| User selection | Install flags |
|---|---|
| Claude Code only | `--claude` |
| Codex only | `--codex` |
| Hermes only | `--hermes` |
| Cursor only | `--cursor` |
| Multiple | combine them |
| All four | `--all` |
| "whatever you detect" | no agent flags (auto-detect mode) |

### Question 2 — Backend mode

Ask which backend mode they want:

- LiteLLM via Docker
- LiteLLM via Python
- Bring my own endpoint
- Offline (no LLM)

### Question 2a — Only if they chose LiteLLM

Ask which provider they want behind LiteLLM:

- Gemini API
- Vertex AI

### Question 3 — Optional install path

Default is:

- `~/git/mempalace`

If they want a different path, use it consistently for clone/install commands.

---

## Step 2 — Clone the repository

Default:

```bash
git clone git@github.com:Scorpion1221/mempalace.git ~/git/mempalace
cd ~/git/mempalace
```

If already cloned, do **not** reclone. Use the existing checkout.

---

## Step 3 — Run the installer

### Default recommendation

Unless the user explicitly asks otherwise, prefer the singleton architecture.
That means appending:

- `--singleton`

Examples:

```bash
# Auto-detect installed agents + install singleton
bash install.sh --singleton

# Explicit agents
bash install.sh --claude --codex --singleton
bash install.sh --all --singleton

# Contributor install
bash install.sh --all --singleton --dev
```

### Important framing for the user

Tell them, in plain language:

- MemPalace now prefers **one local shared server per machine**
- Their agents still use stdio MCP locally, but they do so through
  `mempalace-mcp-bridge`
- If the singleton is not available, the bridge automatically falls back to a
  per-agent stdio subprocess

Do **not** over-explain the transport unless they ask.

---

## Step 4 — Backend-specific follow-up

Do not restate all backend details from `INSTALL.md`. Use the minimal action
sequence needed.

### Path A — LiteLLM via Docker

Stage config under `~/.litellm/`:

```bash
mkdir -p ~/.litellm
cp -n ~/git/mempalace/litellm/config.yaml        ~/.litellm/config.yaml
cp -n ~/git/mempalace/litellm/docker-compose.yml ~/.litellm/docker-compose.yml
cp -n ~/git/mempalace/litellm/.env.example       ~/.litellm/.env.example
[ -f ~/.litellm/.env ] || cp ~/.litellm/.env.example ~/.litellm/.env
```

Then tell the user what they must edit:

- Gemini API → add `GEMINI_API_KEY` to `~/.litellm/.env`
- Vertex AI → edit both `~/.litellm/.env` and `~/.litellm/config.yaml`

After they confirm, start it:

```bash
cd ~/.litellm && docker compose up -d
curl -fs http://127.0.0.1:4000/health/readiness && echo "LiteLLM proxy: OK"
```

### Path B — LiteLLM via Python

```bash
pip install litellm
litellm --config ~/.litellm/config.yaml --port 4000
```

Warn the user that this mode is not managed across reboots.

### Path C — Bring your own endpoint

Edit `~/.mempalace/env`, then propagate:

```bash
bash ~/git/mempalace/scripts/sync-plugins.sh
```

### Path D — Offline

Comment out or remove LLM/embedding endpoint vars from `~/.mempalace/env`, then:

```bash
bash ~/git/mempalace/scripts/sync-plugins.sh
```

Explain that recall will degrade to pure vector search and auto-save will use
verbatim raw transcript mode.

---

## Step 5 — Verify installation

Always verify with commands, not assumptions.

### Package health

```bash
mempalace status
python3 -c "import mempalace; print(mempalace.__version__)"
```

### Singleton health

If the install used `--singleton`, verify:

```bash
mempalace singleton status
```

Expected:

- service is running
- `~/.mempalace/mcp.sock` exists
- socket is reachable

### Bridge health

Send a small MCP initialize request through the bridge:

```bash
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}}}' \
  | mempalace-mcp-bridge
```

If the user did **not** install the singleton, you may instead verify direct MCP:

```bash
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}}}' \
  | MEMPAL_NO_SINGLETON=1 mempalace-mcp-bridge
```

---

## Step 6 — Tell the user the remaining manual steps

### Claude Code

Claude Code still requires IDE/plugin-side steps that shell commands do not
replace. Tell the user:

1. Open Claude Code
2. Run: `/plugin marketplace add ~/git/mempalace`
3. Run: `/plugin install mempalace@mempalace`
4. Restart Claude Code

### Codex

Tell the user:

- Restart Codex CLI
- Test with `$mempalace-search 'test query'`

### Cursor

Tell the user:

- Restart Cursor
- MemPalace uses `sessionStart` + MCP search, not per-prompt injected context

### Hermes

Tell the user:

- Hermes plugin/env were synced by install
- If Hermes was selected, the sync flow already handled the runtime plugin and
  service env updates

---

## Troubleshooting rules for the agent

### Rule 1 — Prefer `~/.mempalace/env` over local config edits

If the user wants to change endpoint/model/key values, edit:

- `~/.mempalace/env`

Then propagate with:

```bash
bash ~/git/mempalace/scripts/sync-plugins.sh
```

Do **not** hand-edit agent-local `MEMPAL_*` config unless you are debugging a
broken sync mechanism.

### Rule 2 — If bridge seems broken, check singleton first

```bash
mempalace singleton status
ls -la ~/.mempalace/mcp.sock
```

### Rule 3 — Use the bridge fallback intentionally when debugging

```bash
MEMPAL_NO_SINGLETON=1 mempalace-mcp-bridge
```

This tells you whether the problem is:

- singleton/socket lifecycle, or
- MCP server/runtime itself

### Rule 4 — On Linux vs macOS

Do not claim one universal service manager.

- macOS → launchd user agent
- Linux → systemd `--user`

The install command is the same (`mempalace singleton install --start`), but the
underlying manager differs.

---

## Example interaction

```text
User: Help me install MemPalace.

Agent:
- asks which agents they use
- asks backend mode
- asks whether default path is fine

User: Claude Code + Codex, Docker LiteLLM, Gemini API, default path.

Agent:
- clones ~/git/mempalace if needed
- runs: bash install.sh --claude --codex --singleton
- stages ~/.litellm/* files
- tells user to add GEMINI_API_KEY to ~/.litellm/.env

User: Done.

Agent:
- runs: cd ~/.litellm && docker compose up -d
- runs: mempalace status
- runs: mempalace singleton status
- runs bridge initialize check
- tells user to restart Claude Code and Codex
```

---

## Final reminder for the agent

When the user asks you to install MemPalace, they are authorizing the install
flow. Batch the work. Verify it. Report only the meaningful checkpoints.

Don't ask permission for each command unless the command is destructive.
