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
