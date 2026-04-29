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

**Question 2: Do you have a Gemini API key?**
- Header: "LiteLLM Backend"
- Options:
  - "Yes, I have a Gemini API key" (description: "Simplest path — get one at https://aistudio.google.com/apikey")
  - "I use Vertex AI" (description: "For orgs already on Vertex — requires a service-account JSON file")
  - "Not yet — I'll set it up later" (description: "Skip LiteLLM setup for now")

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

### Step 4: Set up LiteLLM Proxy

LiteLLM runs on the user's machine, not inside the repo working tree. Always
stage config under `~/.litellm/` so edits survive `git pull` and don't dirty
the checkout. The repo's `litellm/` directory is the source of templates only.

> Note: `~/git/mempalace/litellm/setup.sh` exists but is a repo-local helper
> that operates inside the checkout. This guide intentionally bypasses it and
> uses plain `docker compose` from `~/.litellm/` instead.

Common preparation (run regardless of backend choice):

```bash
mkdir -p ~/.litellm
cp -n ~/git/mempalace/litellm/config.yaml       ~/.litellm/config.yaml
cp -n ~/git/mempalace/litellm/docker-compose.yml ~/.litellm/docker-compose.yml
cp -n ~/git/mempalace/litellm/.env.example      ~/.litellm/.env.example
[ -f ~/.litellm/.env ] || cp ~/.litellm/.env.example ~/.litellm/.env
```

Based on user's backend choice:

#### If "Yes, I have a Gemini API key":

1. Run the common preparation above.
2. Edit `~/.litellm/.env` and set `GEMINI_API_KEY`.
3. Start the proxy from the home-dir workspace:

```bash
cd ~/.litellm
docker compose up -d
curl -fs http://127.0.0.1:4000/health/readiness && echo "LiteLLM proxy: OK"
```

**Tell the user**: "I copied the LiteLLM templates to `~/.litellm/`. Please
edit `~/.litellm/.env` and add your `GEMINI_API_KEY`, then let me know — I'll
start the container with `cd ~/.litellm && docker compose up -d`."

#### If "I use Vertex AI":

1. Run the common preparation above.
2. Edit `~/.litellm/.env` and set `VERTEXAI_PROJECT` and `VERTEXAI_LOCATION`.
3. Edit `~/.litellm/config.yaml`:
   - Comment out the `gemini/*` model entries.
   - Uncomment the `vertex_ai/*` ones (they already use real preview IDs like
     `vertex_ai/gemini-embedding-2-preview` and
     `vertex_ai/gemini-3.1-flash-lite-preview`).
   - On each enabled Vertex entry, set
     `vertex_credentials: /path/to/vertex-service-account.json` to a local
     service-account JSON path.
4. If running via Docker and the credentials JSON lives outside the default
   mounts, add a read-only mount in `~/.litellm/docker-compose.yml`, e.g.
   `- /host/path/vertex-service-account.json:/secrets/vertex.json:ro`, and
   point `vertex_credentials` at the in-container path (`/secrets/vertex.json`).
5. Start the proxy:

```bash
cd ~/.litellm
docker compose up -d
curl -fs http://127.0.0.1:4000/health/readiness && echo "LiteLLM proxy: OK"
```

**Tell the user**: "I copied the LiteLLM templates to `~/.litellm/`. Please:
1. Edit `~/.litellm/.env` and set `VERTEXAI_PROJECT` and `VERTEXAI_LOCATION`
2. Edit `~/.litellm/config.yaml`: disable `gemini/*`, enable `vertex_ai/*`,
   and set `vertex_credentials` to your local service-account JSON path
3. If the JSON path is outside the default mounts, add it as a read-only
   mount in `~/.litellm/docker-compose.yml`
4. Tell me when done — I'll run `cd ~/.litellm && docker compose up -d`"

#### If "Not yet — I'll set it up later":

Skip LiteLLM setup. Tell the user:

"MemPalace is installed but won't work until you set up the LiteLLM proxy.
When you're ready:
1. Get a Gemini API key at https://aistudio.google.com/apikey
2. Stage the config under your home dir:
   ```bash
   mkdir -p ~/.litellm
   cp -n ~/git/mempalace/litellm/config.yaml       ~/.litellm/config.yaml
   cp -n ~/git/mempalace/litellm/docker-compose.yml ~/.litellm/docker-compose.yml
   cp ~/git/mempalace/litellm/.env.example          ~/.litellm/.env
   ```
3. Edit `~/.litellm/.env` and add your `GEMINI_API_KEY`
4. Run `cd ~/.litellm && docker compose up -d`"

---

### Step 5: Verify Installation

Run these commands and show output:

```bash
mempalace status
python3 -c "import mempalace; print(f'MemPalace {mempalace.__version__} installed')"
curl -fs http://127.0.0.1:4000/health/readiness && echo "LiteLLM proxy: OK" || echo "LiteLLM proxy: NOT RUNNING"
```

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

User: [selects "Both Claude Code + Codex", "Yes, I have a Gemini API key", "~/git/mempalace"]

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

### "Docker not found"

If bringing up LiteLLM in `~/.litellm/` fails with "Docker not found":

**Option 1**: Install Docker
- macOS: `brew install --cask docker`
- Linux: `sudo apt install docker.io` or equivalent

**Option 2**: Use Python LiteLLM (still reading config from `~/.litellm/`)
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
