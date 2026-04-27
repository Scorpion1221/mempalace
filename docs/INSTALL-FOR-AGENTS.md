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
  - "I use GCP Vertex AI" (description: "For orgs already on GCP with gcloud auth")
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

Based on user's backend choice:

#### If "Yes, I have a Gemini API key":

```bash
cd ~/git/mempalace/litellm
bash setup.sh
```

The script will:
1. Create `.env` from template
2. Prompt user to edit `.env` and add `GEMINI_API_KEY`
3. Wait for user to confirm they've edited it
4. Start Docker container
5. Health check

**Tell the user**: "The setup script created `litellm/.env`. Please edit it and
add your `GEMINI_API_KEY`, then re-run `bash setup.sh`."

#### If "I use GCP Vertex AI":

```bash
cd ~/git/mempalace/litellm
bash setup.sh
```

**Tell the user**: "The setup script created `litellm/.env`. You need to:
1. Edit `.env` and set `VERTEXAI_PROJECT` and `VERTEXAI_LOCATION`
2. Edit `config.yaml`: comment out `gemini/*` models, uncomment `vertex_ai/*`
3. Ensure `gcloud auth application-default login` is set up
4. Re-run `bash setup.sh`"

#### If "Not yet — I'll set it up later":

Skip LiteLLM setup. Tell the user:

"MemPalace is installed but won't work until you set up the LiteLLM proxy.
When you're ready:
1. Get a Gemini API key at https://aistudio.google.com/apikey
2. Run: `cd ~/git/mempalace/litellm && bash setup.sh`"

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
  [runs: cd litellm && bash setup.sh]
  
  "The LiteLLM setup script created litellm/.env. Please:
   1. Edit ~/git/mempalace/litellm/.env
   2. Add your GEMINI_API_KEY (get one at https://aistudio.google.com/apikey)
   3. Let me know when done, and I'll restart the proxy"

User: "Done"

Agent:
  [runs: cd ~/git/mempalace/litellm && bash setup.sh]
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

If `bash setup.sh` fails with "Docker not found":

**Option 1**: Install Docker
- macOS: `brew install --cask docker`
- Linux: `sudo apt install docker.io` or equivalent

**Option 2**: Use Python LiteLLM
```bash
pip install litellm
litellm --config ~/git/mempalace/litellm/config.yaml --port 4000
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
