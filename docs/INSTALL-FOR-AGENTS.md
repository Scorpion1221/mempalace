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

Edit `~/.mempalace/env`. There are **6 keys** that matter (plus
`SSL_CERT_FILE` on macOS):

```bash
# Embedding — endpoint is the BASE; mempalace appends /v1/embeddings itself
export MEMPAL_EMBEDDING_MODEL="text-embedding-3-large"      # or vx/gemini-embedding-2-preview, etc.
export MEMPAL_EMBEDDING_ENDPOINT="https://your-proxy.example.com"
export MEMPAL_EMBEDDING_KEY="sk-..."

# LLM — endpoint MUST include /v1 (OpenAI-compatible chat/completions)
export MEMPAL_LLM_ENDPOINT="https://your-proxy.example.com/v1"
export MEMPAL_LLM_MODEL="gemini-3.1-flash-lite-preview"     # any model your proxy routes
export MEMPAL_LLM_KEY="sk-..."
```

**Two footguns to surface to the user:**

1. **Embedding dimension must match the palace.** Defaults:
   - `text-embedding-3-large` / Gemini `gemini-embedding-2-preview` → 3072
   - `text-embedding-3-small` → 1536
   - Vertex `text-embedding-005` → 768
   - ChromaDB built-in MiniLM (no proxy) → 384

   Switching dimension on a non-empty palace **breaks recall**. If the user
   changes embedding model on a populated palace, warn them and offer:
   ```bash
   rm -rf ~/.mempalace/palace ~/.mempalace/wal
   # then re-mine
   ```

2. **Endpoint URLs are NOT symmetric.** `MEMPAL_LLM_ENDPOINT` includes
   `/v1`; `MEMPAL_EMBEDDING_ENDPOINT` does NOT (the embedding caller
   appends `/v1/embeddings`). Mixing them up gives 404s.

After editing, propagate AND restart agents (they cache env at launch):

```bash
bash ~/git/mempalace/scripts/sync-plugins.sh
# sync-plugins also bounces the singleton; agents (Claude Code/Codex)
# still need their own restart.
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

Claude Code requires both **marketplace registration** AND **plugin install**
— neither alone is enough. Tell the user:

1. Open Claude Code
2. Run: `/plugin marketplace add ~/git/mempalace`
3. Run: `/plugin install mempalace@mempalace`
4. Re-run `bash ~/git/mempalace/scripts/sync-plugins.sh` from a terminal
   (now that the plugin cache exists, this deploys hooks/skills into it)
5. Restart Claude Code

Note: the env block in `~/.claude/settings.json` is written by
`sync-plugins.sh` regardless of whether `/plugin install` has been run —
so endpoint changes are propagated immediately. But hooks live inside the
plugin cache, which only exists after `/plugin install`, hence the
re-run.

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

- Hermes plugin/env were synced by install. **Verify** the package
  actually installed into Hermes' venv (uv-managed venvs occasionally
  miss the pip path):
  ```bash
  ~/.hermes/hermes-agent/venv/bin/python -c 'import mempalace; print(mempalace.__version__)'
  ```
  If this raises `ImportError`, the memory plugin will be silently inactive.
  Fix:
  ```bash
  uv pip install --python ~/.hermes/hermes-agent/venv/bin/python ~/git/mempalace
  hermes gateway restart
  ```
- **Optional but recommended**: create `~/.hermes/mempalace.json`:
  ```json
  {
    "default_wing": "hermes"
  }
  ```
  Without this, Hermes' wing defaults to the platform user_id (e.g.
  `ou_e148fd...` for Feishu, similar IDs for Telegram/Discord), which is
  visible in `mempalace status` and ugly. `"hermes"` matches the
  convention; any human-readable string works. After creating the file,
  `hermes gateway restart` so the plugin re-reads its cached config.

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

## Troubleshooting recipes

Concrete failure modes seen in the wild, with the actual fix. Use these
when the user reports the symptom — don't ask them to re-read the install
guide from the top.

### Hook log says `no LLM rewrite available, using original query`

Cause: the hook subprocess didn't inherit `MEMPAL_LLM_*` env, so the LLM
call fell back to `http://127.0.0.1:4000/v1` and failed silently. This
shouldn't happen with recent hook scripts (they source `~/.mempalace/env`
at the top), but if the user is on an older copy, patch the hook:

```bash
# Insert near the top of the hook (before any `:-` default fallback):
if [ -f "$HOME/.mempalace/env" ]; then
  . "$HOME/.mempalace/env"
fi
```

Confirm what env the hook actually inherits by temporarily adding:

```bash
env | grep -E "^MEMPAL_|^SSL_CERT" > ~/.mempalace/logs/hook-env-debug.log
```

at the top, re-trigger, and read the log.

### Singleton has stale env after `~/.mempalace/env` change

The singleton (launchd / systemd --user) inherits env at process launch,
not on each MCP call. `sync-plugins.sh` bounces it automatically, but if
the user is hand-editing env without running sync:

```bash
mempalace singleton stop && mempalace singleton start
```

Verify the new values are visible:

```bash
mempalace singleton status | grep MEMPAL_LLM_ENDPOINT
```

### Proxy returns `data: [DONE]` after JSON in a non-streaming response

Symptom: `_call_openai_compat` returns None → "no LLM rewrite available".
The body looks like:

```
{"id":"...","choices":[...]}data: [DONE]
```

This is an upstream proxy bug (LiteLLM with `stream=False` should not
emit the SSE terminator). Two options:

1. **Server side**: ask the proxy operator to drop the SSE terminator on
   non-streaming responses (in LiteLLM: ensure `stream_options` aren't
   force-applied to non-streaming requests).
2. **Client side workaround**: swap `MEMPAL_LLM_ENDPOINT` to a known-good
   endpoint.

### Embedding hits 429 burst during `mempalace mine`

The miner makes up to 10 concurrent embedding calls with a short backoff
(0.5s × 3 retries). Proxies with tight per-minute limits will reject
bursts and may temp-ban the connection (subsequent calls fail with
`SSL: UNEXPECTED_EOF_WHILE_READING`).

Workarounds, in order of preference:

1. Switch to an embedding endpoint sized for bulk mining.
2. Use `mempalace mine ... --limit 1` to verify the path works on a
   single file before bulk ingest.
3. Raise the proxy's per-minute embedding cap.

### Hermes plugin loads but writes nothing to palace

Verify it can `import mempalace`:

```bash
~/.hermes/hermes-agent/venv/bin/python -c 'import mempalace; print(mempalace.__version__)'
```

If `ImportError`, the package never installed into Hermes' venv. Fix:

```bash
uv pip install --python ~/.hermes/hermes-agent/venv/bin/python ~/git/mempalace
hermes gateway restart
```

(Recent `sync-plugins.sh` will fail loudly if this install fails; older
copies swallow the error.)

### Hermes wing shows up as a platform user_id (`ou_...`, etc.)

By design, Hermes scopes memory per-user when no override is set. Create
`~/.hermes/mempalace.json`:

```json
{ "default_wing": "hermes" }
```

Then `hermes gateway restart`.

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
