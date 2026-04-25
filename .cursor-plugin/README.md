# MemPalace for Cursor

Give Cursor a persistent memory that pools with Claude Code, Codex, and Hermes on the same machine. One palace at `~/.mempalace/`, readable and writable from all four agents.

## What's in the box

- **MCP server `mempalace`** — 19 tools for search, KG query, drawer read/write, etc. Auto-registered via `plugin.json.mcpServers`.
- **Three hooks** on `sessionStart`, `stop`, `preCompact`:
  - **sessionStart** — injects a palace map (wings, recent saves, known entities) into the agent's context so it knows what memory exists before the first user message.
  - **stop** — async LLM-extracted save: distills the exchange into diary entry + verbatim drawers + KG facts, writes to `~/.mempalace/`.
  - **preCompact** — emergency save before Cursor compacts context. Never loses a conversation to compression.
- **Rule `rules/mempalace-recall.mdc`** — auto-applies on every session. Tells the agent when to call `mempalace_search` for historical queries.
- **Skills** — `/search`, `/status`, `/mine`, `/init`, `/help` (symlinked to the same skill files Codex uses, so both agents share definitions).

## Why it's different from the Claude Code / Codex plugins

Cursor does **not** expose a per-user-prompt hook that accepts context injection. Claude Code's `UserPromptSubmit` and Codex's equivalent both take an `additionalContext` response field; Cursor's `beforeSubmitPrompt` is validation-only (`allow` / `deny` / `ask`), which means we can't auto-inject matched memories on every turn.

Two-part workaround:

1. **sessionStart injection** — runs once per session. The agent sees the palace map (top wings + recent drawers + KG entities) in its initial system context.
2. **Rule-driven on-demand recall** — `rules/mempalace-recall.mdc` tells the agent to call `mempalace_search` whenever the user's message references past work ("之前", "上次", "remember", etc.). This trades a bit of precision for a Cursor-native primitive.

For most conversations the two mechanisms combine well. If you want strict per-prompt injection, run the same project through Claude Code or Codex — same palace, all three agents.

## Install

```bash
bash ~/git/mempalace/scripts/sync-plugins.sh
```

`[5/8]` handles Cursor:
- Symlinks `.cursor-plugin/` into `~/.cursor/plugins/local/mempalace/`
- Runs `launchctl setenv` for the 7 MEMPAL_* env vars (needed because Cursor GUI launch doesn't source `~/.zshrc`)
- Writes `~/Library/LaunchAgents/ai.mempalace.env.plist` so those env vars persist across reboots

Restart Cursor to pick up the new plugin.

## Verify

1. Open Cursor in any project, start a fresh chat.
2. Ask the agent: "What's in my MemPalace? Use the sessionStart context, don't call any tools."
3. It should quote wing names, recent drawer previews, and entity names.
4. Ask: "What did we decide about Hermes save trigger?" — the rule triggers the agent to call `mempalace_search`, which returns the decision drawer.
5. Close the chat. Reopen → `grep cursor ~/.mempalace/hook_state/hook.log | tail` should show a `hermes-llm-save`-style log line (from the cursor harness Stop hook).

## Troubleshooting

**Agent says "I don't have access to a memory system"** — the MCP server isn't registered. Check Cursor → Settings → Features → Model Context Protocol. Toggle `mempalace` on.

**sessionStart injection empty** — palace is empty, or env vars didn't propagate. `launchctl getenv MEMPAL_EMBEDDING_MODEL` should return `gemini-embedding-2-preview`. If empty, rerun `bash scripts/sync-plugins.sh`.

**Agent calls mempalace_search for every message** — rule interpretation is too eager. Edit `rules/mempalace-recall.mdc` to tighten the "skip" section.

**Save produces KG facts but no drawers** — embedding dim mismatch. Usually means the running Cursor didn't see `MEMPAL_EMBEDDING_MODEL`. See `CHANGELOG.md` `3072 vs 384` entries for the history and fix.
