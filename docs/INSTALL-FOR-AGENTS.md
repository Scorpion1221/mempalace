# MemPalace Installation Guide for AI Agents

**Audience**: Claude Code, Codex, Cursor, and other AI coding assistants

This guide tells you (the AI agent) how to help a user install MemPalace by
collecting their preferences via `AskUserQuestion`, then running the right
commands on their behalf.

---

## Installation Flow

### Step 1: Collect User Preferences

Use `AskUserQuestion` to gather install configuration. Ask 2-3 questions:

**Question 1: Which AI agent(s) are you using?**
- Header: "AI Agent"
- Options:
  - "Claude Code" (description: "Anthropic's official CLI and desktop app (Recommended)")
  - "Codex" (description: "Codex CLI by Anthropic")
  - "Cursor" (description: "Cursor IDE — requires manual setup after install")
  - "Hermes" (description: "Hermes agent — advanced users only")
- Multi-select: true (user can pick multiple — e.g., Claude Code + Codex)

**Question 2: How will MemPalace reach an LLM + embedding backend?**
- Header: "LLM Backend"
- Options:
  - "LiteLLM via Docker" (description: "Recommended — we ship templates, you bring a key (Gemini API or Vertex AI)")
  - "LiteLLM via Python" (description: "Same config, no Docker — you manage the process yourself")
  - "Bring my own endpoint" (description: "You already run vLLM / Ollama / OpenAI-compat gateway elsewhere")
  - "Offline (no LLM)" (description: "Skip LLM stack entirely — auto-save still works in verbatim mode, recall falls back to MiniLM")

If user picks "LiteLLM via Docker" or "LiteLLM via Python", ask follow-up
sub-question for backend:

**Question 2a: Gemini API or Vertex AI?**
- Header: "LiteLLM Provider"
- Options:
  - "Gemini API" (description: "Simplest — get a key at https://aistudio.google.com/apikey")
  - "Vertex AI" (description: "For orgs already on Vertex — requires a service-account JSON file")

**Question 3: Install location (optional)**
- Header: "Install Path"
- Options:
  - "~/git/mempalace" (description: "Default location (Recommended)")
  - "Custom path" (description: "I'll specify a different directory")

---

### Step 2: Clone Repository

Based on user's install path choice:

```bash
git clone git@github.com:Scorpion1221/mempalace.git ~/git/mempalace
cd ~/git/mempalace
```

Or if custom path, ask for it and use that.

---

### Step 3: Run install.sh

Map user's agent selection (multi-select) to install.sh flags:

| User selected | Command |
|---|---|
| Claude Code only | `bash install.sh --claude` |
| Codex only | `bash install.sh --codex` |
| Claude Code + Codex | `bash install.sh --all` |
| Cursor (any combination) | `bash install.sh --claude` (Cursor shares Claude plugin) |
| Hermes (any combination) | `bash install.sh --claude && bash scripts/sync-plugins.sh --hermes` |

If the user picked multiple agents, combine flags accordingly — `--all` covers
Claude + Codex; add `sync-plugins.sh --hermes` if Hermes was also selected;
Cursor needs no extra flag beyond `--claude`.

Run the command and show output to user.

---

### Step 4: Set up LLM Backend

MemPalace only requires an OpenAI-compatible endpoint for embeddings + recall
LLM. Pick one of four deployment paths below based on the user's answer to
Question 2.

> Note: `~/git/mempalace/litellm/setup.sh` exists but is a repo-local helper
> that mutates the repo working tree. This guide intentionally bypasses it
> and uses plain `docker compose` (or `litellm` CLI) from `~/.litellm/`.

#### Common LiteLLM preparation (paths A and B only)

```bash
mkdir -p ~/.litellm
cp -n ~/git/mempalace/litellm/config.yaml       ~/.litellm/config.yaml
cp -n ~/git/mempalace/litellm/docker-compose.yml ~/.litellm/docker-compose.yml
cp -n ~/git/mempalace/litellm/.env.example      ~/.litellm/.env.example
[ -f ~/.litellm/.env ] || cp ~/.litellm/.env.example ~/.litellm/.env
```

If user picked **Vertex AI** in Question 2a (instead of Gemini API), also do
this once (applies to both A and B):

1. Edit `~/.litellm/.env`: set `VERTEXAI_PROJECT` and `VERTEXAI_LOCATION`.
2. Edit `~/.litellm/config.yaml`: comment out `gemini/*` model entries,
   uncomment the `vertex_ai/*` ones (the shipped examples use the real
   preview IDs `vertex_ai/gemini-embedding-2-preview` and
   `vertex_ai/gemini-3.1-flash-lite-preview`).
3. Set `vertex_credentials: /path/to/vertex-service-account.json` on each
   enabled Vertex entry.

For Gemini API, edit `~/.litellm/.env` and set `GEMINI_API_KEY`.

---

#### Path A: LiteLLM via Docker (recommended)

```bash
cd ~/.litellm
docker compose up -d
curl -fs http://127.0.0.1:4000/health/readiness && echo "LiteLLM proxy: OK"
```

If using Vertex AI and the credentials JSON lives outside the default
mounts, add a read-only mount in `~/.litellm/docker-compose.yml`, e.g.
`- /host/path/vertex-service-account.json:/secrets/vertex.json:ro`, and
point `vertex_credentials` at the in-container path (`/secrets/vertex.json`).

**Tell the user**: "I staged the LiteLLM config at `~/.litellm/`. Please
finish editing `~/.litellm/.env` (and `config.yaml` for Vertex), then let
me know — I'll run `cd ~/.litellm && docker compose up -d`."

---

#### Path B: LiteLLM via Python (no Docker)

```bash
pip install litellm
litellm --config ~/.litellm/config.yaml --port 4000 &
curl -fs http://127.0.0.1:4000/health/readiness && echo "LiteLLM proxy: OK"
```

**Caveat**: this leaves a foreground/background process the user is
responsible for. The proxy will not auto-restart on reboot. Tell the user:
"You're running LiteLLM as a plain Python process — it won't survive
reboots. If you want a managed service, switch to Path A (Docker) or wrap
it in launchd/systemd later."

---

#### Path C: Bring your own endpoint (vLLM / Ollama / OpenAI-compat gateway)

Skip LiteLLM entirely. The user already has an OpenAI-compatible endpoint
they want to use.

Edit `~/.mempalace/env` and set the four MEMPAL variables:

```bash
export MEMPAL_EMBEDDING_MODEL="<their-embedding-model-id>"
export MEMPAL_EMBEDDING_ENDPOINT="<their-base-url>"   # no /v1 suffix
export MEMPAL_EMBEDDING_KEY="<their-api-key>"

export MEMPAL_LLM_ENDPOINT="<their-base-url>/v1"      # /v1 required
export MEMPAL_LLM_MODEL="<their-llm-model-id>"
export MEMPAL_LLM_KEY="<their-api-key>"
```

Then:

```bash
bash ~/git/mempalace/scripts/sync-plugins.sh   # propagate env to all agents
curl -fs "$MEMPAL_EMBEDDING_ENDPOINT/v1/models" >/dev/null && echo "endpoint: OK"
```

**Caveat**: ChromaDB locks the embedding dimensionality at first call.
Ask the user to confirm their embedding model's output dim — anything other
than 3072 means starting fresh (no existing palace) or an explicit
`MEMPAL_EMBEDDING_DIMS` override.

---

#### Path D: Offline (no LLM)

No proxy, no endpoint. The user wants MemPalace as a local-only verbatim
log without LLM enhancements.

Make sure no LLM/embedding env vars are set:

```bash
unset MEMPAL_LLM_ENDPOINT MEMPAL_LLM_MODEL MEMPAL_LLM_KEY \
      MEMPAL_RECALL_ENDPOINT MEMPAL_RECALL_MODEL MEMPAL_RECALL_KEY \
      MEMPAL_EMBEDDING_MODEL MEMPAL_EMBEDDING_ENDPOINT MEMPAL_EMBEDDING_KEY
```

(or comment them out in `~/.mempalace/env` and re-run `sync-plugins.sh`.)

What works in offline mode:

- All MCP tools, all `mempalace` CLI subcommands
- Semantic search via ChromaDB built-in MiniLM (384d)
- Auto-save: writes each save window verbatim into `room=raw_transcript`
  under tag `async_offline_save`. Future runs with an LLM configured can
  re-classify these later. Opt out with `MEMPAL_OFFLINE_SAVE=0`.

What is degraded or off:

- UserPrompt recall: pure vector search, no LLM rewrite, no rerank,
  smaller pool (`USERPROMPT_RECALL_LIMIT` instead of `_POOL`)
- LLM-driven KG fact extraction, diary structuring, and per-drawer
  summarisation: all skipped silently

**Tell the user**: "MemPalace is installed in offline mode. The palace
will still grow — every Stop hook writes the raw transcript verbatim into
`room=raw_transcript`. When you later add a Gemini key (or any
OpenAI-compatible endpoint), set `MEMPAL_*_ENDPOINT/MODEL/KEY` in
`~/.mempalace/env` and re-run `sync-plugins.sh` — those raw drawers will
remain searchable, and new saves will use the structured LLM path."


---

### Step 5: Verify Installation

Run these commands and show output:

```bash
mempalace status
python3 -c "import mempalace; print(f'MemPalace {mempalace.__version__} installed')"
```

Then verify the chosen LLM path:

- Path A (Docker) or Path B (Python):
  `curl -fs http://127.0.0.1:4000/health/readiness && echo "LiteLLM proxy: OK" || echo "LiteLLM proxy: NOT RUNNING"`
- Path C (own endpoint):
  `curl -fs "$MEMPAL_EMBEDDING_ENDPOINT/v1/models" >/dev/null && echo "endpoint: OK" || echo "endpoint: NOT RUNNING"`
- Path D (offline): no proxy check; instead confirm
  `python3 -c "from mempalace.recall_llm import is_enabled; print('LLM enabled:', is_enabled())"` prints `False`.

---

### Step 6: Next Steps

Tell the user what to do next based on their agent:

**Claude Code** (requires one-time IDE-side install — Claude Code's plugin
manager is gated on `/plugin marketplace add` + `/plugin install`, no shell
equivalent exists):

After `install.sh` finishes, tell the user:
1. "Open Claude Code"
2. "Run: `/plugin marketplace add ~/git/mempalace`"
3. "Run: `/plugin install mempalace@mempalace`"
4. "Then in shell: `bash ~/git/mempalace/scripts/sync-plugins.sh --claude`
   to populate the env block in `settings.json` now that the plugin cache
   exists"
5. "Restart Claude Code"
6. "Test recall: `/mempalace:search 'your query'`"

**Codex** (fully automated — `install.sh` already installed the plugin and
registered it in `~/.codex/config.toml`. No manual steps):
- "Restart Codex CLI"
- "Test recall: `$mempalace-search 'your query'`"
- "Check status: `$mempalace-status`"

**Cursor**:
- "Restart Cursor IDE"
- "MemPalace will inject a palace map at session start"
- "Use MCP tool `mempalace_search` to query memories"

**Hermes** (only if `~/.hermes/` exists on the machine — otherwise the
sync silently skipped this agent):
- "Restart Hermes gateway: `hermes restart`"
- "Check launchd plist env: `plutil -p ~/Library/LaunchAgents/ai.hermes.gateway.plist | grep MEMPAL`"

---

## Example Interaction

```
User: "Help me install MemPalace"

Agent: [calls AskUserQuestion with 3 questions above]

User: [selects "Both Claude Code + Codex", "LiteLLM via Docker", "Gemini API", "~/git/mempalace"]

Agent:
  "I'll install MemPalace for Claude Code + Codex at ~/git/mempalace."
  
  [runs: git clone ...]
  [runs: bash install.sh --all]
  [runs: mkdir -p ~/.litellm && cp -n ~/git/mempalace/litellm/{config.yaml,docker-compose.yml,.env.example} ~/.litellm/ && cp ~/.litellm/.env.example ~/.litellm/.env]
  
  "I staged the LiteLLM config at ~/.litellm/. Please:
   1. Edit ~/.litellm/.env
   2. Add your GEMINI_API_KEY (get one at https://aistudio.google.com/apikey)
   3. Let me know when done, and I'll start the proxy"

User: "Done"

Agent:
  [runs: cd ~/.litellm && docker compose up -d]
  [runs: mempalace status]
  [runs: curl health check]
  
  "✓ MemPalace installed successfully!
   
   Next steps:
   - Restart Claude Code to activate the plugin
   - Test recall: /mempalace:search 'your query'
   - Check status: /mempalace:status"
```

---

## Troubleshooting During Install

### "Docker not found" (only relevant if user picked Path A)

Two responses:

**Option 1**: Install Docker
- macOS: `brew install --cask docker`
- Linux: `sudo apt install docker.io` or equivalent

**Option 2**: Switch to Path B (Python LiteLLM, same `~/.litellm/config.yaml`)
```bash
pip install litellm
litellm --config ~/.litellm/config.yaml --port 4000
```

### "Permission denied" on install.sh

```bash
chmod +x ~/git/mempalace/install.sh
bash ~/git/mempalace/install.sh --all
```

### "Palace already exists"

If user has an old palace at `~/.mempalace/`:

**Ask user**: "You have an existing palace at ~/.mempalace/. Options:
1. Keep it (install will skip palace init)
2. Wipe and start fresh (WARNING: deletes all memories)
3. Backup first, then wipe"

If user chooses wipe:
```bash
mv ~/.mempalace ~/.mempalace.backup.$(date +%Y%m%d-%H%M%S)
# then re-run install.sh
```

---

## Advanced: Custom Configuration

If user wants to customize env vars after install:

```bash
# Edit single-source env file
nano ~/.mempalace/env

# Propagate to all agents
bash ~/git/mempalace/scripts/sync-plugins.sh
```

Common customizations:
- Change LiteLLM endpoint (if running on different port)
- Switch embedding model
- Adjust recall LLM model
- Add SSL_CERT_FILE for corporate proxies

---

## Post-Install: Testing Recall

After install, help user test the recall flow:

1. **Save something to palace**:
   ```bash
   mempalace add --wing test --room demo --content "This is a test memory about installing MemPalace on $(date)"
   ```

2. **Search for it**:
   - Claude Code: `/mempalace:search 'test memory'`
   - Codex: `$mempalace-search 'test memory'`
   - CLI: `mempalace search 'test memory'`

3. **Check palace status**:
   - Claude Code: `/mempalace:status`
   - Codex: `$mempalace-status`
   - CLI: `mempalace status`

If recall works, the install is successful.

---

## When to Use This Guide

**Trigger phrases**:
- "help me install mempalace"
- "set up mempalace"
- "install mempalace for claude code"
- "configure mempalace"
- "mempalace installation"

**Do NOT trigger** on:
- "what is mempalace" (answer conceptually, don't install)
- "how does mempalace work" (explain, don't install)
- "mempalace documentation" (point to README/docs, don't install)

**When user is ready to install**, use `AskUserQuestion` to collect preferences,
then run the commands above. Don't ask permission for each command — the user
asked you to install, so batch the work and report progress.
