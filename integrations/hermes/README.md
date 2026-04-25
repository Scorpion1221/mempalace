# Hermes integration

The MemPalace memory provider for [Hermes Agent](https://hermes.ai). Lets a Hermes gateway (Feishu, Telegram, etc.) share the same `~/.mempalace/palace` that Claude Code and Codex use on this machine — so a conversation on Feishu about a project is recallable from a terminal Claude Code session and vice versa.

This directory lives inside the main MemPalace repo (moved from the old standalone `hermes-mempalace-plugin` repo on 2026-04-25) so cross-cutting changes — e.g. a refactor in `mempalace/recall_llm.py` and the matching call site in `plugins/memory/mempalace/__init__.py` — land in a single commit.

## Layout

```
integrations/hermes/
├── plugins/memory/mempalace/
│   ├── __init__.py       # MemPalaceMemoryProvider — the Hermes MemoryProvider contract
│   ├── cli.py            # `hermes mempalace ...` CLI hooks
│   ├── plugin.yaml       # Hermes plugin metadata
│   └── README.md         # provider-specific notes
├── tests/                # 55 unit tests — opt-in, not in the main test suite
│   ├── conftest.py
│   └── test_mempalace_provider.py
└── README.md             # (this file)
```

## Install / deploy

Use the repo's sync script — it installs the main `mempalace` package into the Hermes venv as a snapshot, copies the plugin files into the Hermes runtime, and restarts the gateway:

```bash
bash ~/git/mempalace/scripts/sync-plugins.sh
```

The Hermes-only parts of that script live in steps `[4/6]` (copy plugin + pip install snapshot) and `[5/6]` (gateway restart).

## Tests

Hermes tests depend on Hermes's gateway types and must be run with the Hermes venv (the global pyenv won't have `hermes_agent.*` available):

```bash
cd ~/git/mempalace
~/.hermes/hermes-agent/venv/bin/python -m pytest integrations/hermes/tests/ -v
```

The main `pytest tests/` ignores this directory (see `pyproject.toml` → `testpaths = ["tests"]`).

## Behaviour (as of 2026-04-25)

- Palace + KG paths default to the shared `~/.mempalace/` store (parity with Claude Code / Codex)
- Save trigger: every `MEMPAL_HERMES_SAVE_INTERVAL` non-trivial turns (default 3, set to 1 in most deployments) + a `session_end` flush
- Recall uses the same two-stage wing fallback as `mempalace.hooks_cli` (wing is a project-scoping signal, never widened)
- The previous assistant reply (500-char tail) is carried into the next turn's recall rewrite/rerank, so short follow-ups like "why?" and "继续" still pull the right drawers
