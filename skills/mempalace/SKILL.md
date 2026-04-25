---
name: mempalace
description: "MemPalace — Local AI memory with 96.6% recall. Semantic search over past conversations, knowledge graph with temporal facts, palace architecture (wings/rooms/drawers). Free, no cloud, no API keys. Highest LongMemEval score ever published."
version: 1.4.0
homepage: https://github.com/milla-jovovich/mempalace
user-invocable: true
metadata:
  openclaw:
    emoji: "🏛️"
    requires:
      anyBins:
        - mempalace
        - python3
    install:
      - id: mempalace-pip
        kind: uv
        label: "Install MemPalace (Python, local ChromaDB)"
        package: mempalace
        bins:
          - mempalace
---

# MemPalace — Local AI Memory System

You have access to a local memory palace that stores verbatim conversation history plus a temporal knowledge graph. All data lives on the user's machine — zero cloud, zero API calls.

## Architecture

- **Wings** = people or projects (e.g. `wing_alice`, `wing_myproject`)
- **Halls** = broad categories (facts, events, preferences, advice)
- **Rooms** = specific topics or time groupings (e.g. `chromadb-setup`, `decisions-2026-04`, `riley-school`)
- **Drawers** = individual verbatim text chunks — your exact words, never summarized
- **Knowledge Graph** = typed entity-relationship facts with `valid_from` / `valid_to` temporal windows
- **Tunnels** = cross-wing hyperlinks between rooms — either implicit (same room name in two wings) or explicit (manually created edges)

## Tools

All 29 MCP tools are prefixed `mempalace_`. The sections below give purpose + a concrete scenario for each.

### Search & Query

- **`mempalace_search`** — Semantic search over drawer content. Returns verbatim chunks with similarity scores, filtered by `max_distance`.
  - *Scenario:* "What did we decide about ChromaDB segfaults?" → `search(query="chromadb segfault decision", limit=5)`.
- **`mempalace_status`** — Palace overview: total drawers, wing and room counts.
  - *Scenario:* First time exploring a palace, or sanity-checking after bulk ingest.
- **`mempalace_list_wings`** — Every wing with its drawer count.
  - *Scenario:* "Which projects do I have memory for?"
- **`mempalace_list_rooms`** — Rooms in a specific wing (or all rooms).
  - *Scenario:* "What topics exist under `wing_myproject`?" → `list_rooms(wing="wing_myproject")`.
- **`mempalace_list_drawers`** — Paginated drawer listing with ID + content preview.
  - *Scenario:* Auditing what lives in `wing_alice/room_preferences` before running `update_drawer`.
- **`mempalace_get_taxonomy`** — Full wing → room → drawer-count tree as one object.
  - *Scenario:* Building a table-of-contents for the palace before a long session.
- **`mempalace_get_drawer`** — Fetch one drawer by ID; returns full content + metadata.
  - *Scenario:* A search result shows drawer `abc123`; you want the complete untruncated text.
- **`mempalace_memories_filed_away`** — Check whether the most recent auto-save hook ran; returns message count + timestamp.
  - *Scenario:* User asks "did my last conversation save?" — confirm before offering to re-save.

### Knowledge Graph

- **`mempalace_kg_query`** — Query an entity's typed relationships, optionally `as_of` a date.
  - *Scenario:* "Who does Max live with right now?" → `kg_query(entity="Max", as_of="2026-04-26")`.
- **`mempalace_kg_add`** — Add a `subject → predicate → object` fact with optional `valid_from`.
  - *Scenario:* User says "Max started Year 7 today" → `kg_add("Max", "started_school", "Year 7", valid_from="2026-09-01")`.
- **`mempalace_kg_invalidate`** — Mark a fact as no longer true; sets `valid_to` (default today).
  - *Scenario:* "I quit my job at Acme" → `kg_invalidate("user", "works_at", "Acme")`.
- **`mempalace_kg_stats`** — Entity / triple / relationship-type counts; current vs expired.
  - *Scenario:* Deciding whether the KG is rich enough to answer a question or search is needed instead.
- **`mempalace_kg_timeline`** — Chronological list of facts for one entity (or the whole graph).
  - *Scenario:* "Walk me through Max's school history" → `kg_timeline(entity="Max")`.

### Palace Graph & Tunnels

- **`mempalace_traverse`** — Walk outward from a room, across both implicit and explicit tunnels, up to `max_hops`.
  - *Scenario:* Starting at `chromadb-setup` in `wing_code`, discover it also appears in `wing_myproject` (planning) and `wing_user` (reactions).
- **`mempalace_find_tunnels`** — Rooms that bridge two wings (implicit: same room name in both wings).
  - *Scenario:* "What topics do `wing_code` and `wing_team` have in common?" → `find_tunnels("wing_code", "wing_team")`.
- **`mempalace_follow_tunnels`** — From one `(wing, room)` location, return all connected rooms in other wings with drawer previews.
  - *Scenario:* You're reading a decision drawer and want everything related across other projects in one call.
- **`mempalace_list_tunnels`** — Every explicit (manually created) tunnel, optionally filtered by wing.
  - *Scenario:* Audit which cross-project links the user has curated.
- **`mempalace_create_tunnel`** — Create a symmetric, undirected edge between two `(wing, room)` endpoints with a label.
  - *Scenario:* The API schema decision in `wing_project_api / room_auth` constrains the DB migration in `wing_project_database / room_user-table` → `create_tunnel(source_wing="wing_project_api", source_room="auth", target_wing="wing_project_database", target_room="user-table", label="auth scheme dictates user-table shape")`.
- **`mempalace_delete_tunnel`** — Delete an explicit tunnel by ID (does not affect implicit tunnels).
  - *Scenario:* Removing a connection that's no longer accurate after a refactor.
- **`mempalace_graph_stats`** — Total rooms, tunnel count, inter-wing edges.
  - *Scenario:* Quick diagnostic of how "connected" the palace is.

### Write

- **`mempalace_add_drawer`** — File verbatim content into `(wing, room)`. Dedup check runs automatically.
  - *Scenario:* The user just stated a preference in chat; store it exactly.
- **`mempalace_update_drawer`** — Change content, wing, or room of an existing drawer (by ID).
  - *Scenario:* Drawer was filed under the wrong wing; move it without losing history.
- **`mempalace_delete_drawer`** — Remove a drawer by ID. Irreversible.
  - *Scenario:* User asks to forget a specific statement.
- **`mempalace_diary_write`** — Per-agent diary entry; plain natural language; write in the same language as the session.
  - *Scenario:* End of a design session — log the decisions you made so future-you can recall.
- **`mempalace_diary_read`** — Read your recent diary entries (default 10).
  - *Scenario:* Resuming a project after a week — see what past-you noted.
- **`mempalace_check_duplicate`** — See whether content is already filed before writing (`threshold` default 0.9).
  - *Scenario:* Before bulk-importing, verify you're not re-filing the same quote.

### Meta / Admin

- **`mempalace_get_aaak_spec`** — Return the AAAK compressed-index dialect spec.
  - *Scenario:* You need to read or emit AAAK-format pointer lines.
- **`mempalace_hook_settings`** — View or set hook behavior (`silent_save`, `desktop_toast`). No args = view.
  - *Scenario:* User complains about MCP hook noise → `hook_settings(silent_save=True)`.
- **`mempalace_reconnect`** — Force the MCP server to reload the ChromaDB collection after external CLI writes.
  - *Scenario:* User ran `mempalace mine` in another terminal; index is stale → reconnect.

## Decision Guide

| User asks / signal | Tool to use |
| --- | --- |
| "What's my wife's birthday?" / personal fact | `kg_query(entity="<person>")`, fallback `search` on `wing_<person>` |
| "What did we decide about X?" | `search(query="X decision", room="decisions")` |
| "What's true right now about Y?" | `kg_query(entity="Y", as_of="<today>")` |
| "When did Z change?" | `kg_timeline(entity="Z")` |
| "What connects project A and project B?" | `find_tunnels(wing_a, wing_b)`, then `follow_tunnels` |
| "Explore everything related to topic T" | `traverse(start_room="T", max_hops=2)` |
| "Show me all my projects" | `list_wings` |
| "What topics are in project P?" | `list_rooms(wing="wing_P")` |
| User states a new fact about a person | `kg_add` **and** `add_drawer` (drawer = verbatim, KG = queryable) |
| User corrects a fact | `kg_invalidate` the old, `kg_add` the new |
| "Did memory save?" | `memories_filed_away` |
| Starting a fresh session on an active project | `diary_read(agent_name="you")` |
| End of a substantive session | `diary_write` (hooks usually auto-save; check `memories_filed_away` first) |

## Tunnels (two kinds)

Tunnels are **location-level hyperlinks** between palace spots. Compare to the Knowledge Graph, which stores **typed entity facts**. Use tunnels to jump between places that discuss related work; use the KG to answer "who / when / what relationship".

### Implicit tunnels (automatic, zero config)

When the same room name exists under two different wings, the graph treats them as connected. No setup required — just name rooms consistently.

- *Example:* `wing_project_api/auth` and `wing_project_mobile/auth` automatically bridge because both have a room called `auth`.
- *Discover:* `find_tunnels(wing_a="wing_project_api", wing_b="wing_project_mobile")` returns the shared rooms. `traverse(start_room="auth")` crawls outward along these edges.

### Explicit tunnels (manual, curated)

When two rooms have **different names** but are semantically linked — or when you want to attach a descriptive label — create an explicit tunnel. Tunnels are **symmetric / undirected**: `create_tunnel(A, B)` and `create_tunnel(B, A)` resolve to the same canonical ID; a repeat call updates the label rather than duplicating.

- *Example:*
  ```
  create_tunnel(
    source_wing="wing_project_api",     source_room="auth-v2-design",
    target_wing="wing_project_database", target_room="user-table-migration",
    label="auth-v2 scheme dictates user-table FK shape"
  )
  ```
- *Discover:* `list_tunnels(wing="wing_project_api")` shows every explicit edge touching that wing. `follow_tunnels(wing, room)` returns the drawers on the far side of each tunnel from that specific location.
- *Remove:* `delete_tunnel(tunnel_id)` (implicit tunnels cannot be deleted — rename the room instead).

## How to call: CLI vs MCP

MemPalace ships with two call paths. Prefer MCP when registered; otherwise fall back to CLI.

- **MCP** (preferred when available): if your tool list contains `mcp__mempalace__*`, use those tools — they expose the full 29-tool API (KG, tunnels, drawer CRUD).
- **CLI** (works without MCP registration): only `search` and `status` have CLI equivalents today.
  ```bash
  mempalace search "query string" --wing wing_myproject --limit 5
  mempalace status
  ```
  Advanced operations (KG queries, tunnel management, drawer CRUD, diary) are **MCP-only** today. If you need them and MCP isn't registered, tell the user and suggest `claude mcp add mempalace -- python -m mempalace.mcp_server`.

Rule of thumb: check your tool list for `mcp__mempalace__*` first. If present → MCP. If absent → CLI for search/status, and tell the user what they're missing.

## Protocol (relaxed — use judgment)

This is not a wake-up checklist. Retrieve from memory **only when the request warrants it**.

- **Search first when** the task references past sessions, specific people, past decisions, stable preferences, or long-running project state (signals: "之前", "上次", "还记得", "why did we", "what did we decide", named people / projects beyond this thread). Otherwise skip retrieval.
- **Uncertain about a fact** (name, age, relationship, preference)? Say "let me check" and query. A wrong guess is worse than a two-second retrieval.
- **Storing new facts about people / projects / decisions**: use `kg_add` **in addition to** a verbatim `add_drawer`. Drawers preserve exact words; the KG enables temporal queries.
- **When facts change**: `kg_invalidate` the old fact, then `kg_add` the new one — never silently overwrite.
- **End-of-session diary**: hooks handle auto-save in most environments. Check `memories_filed_away` first; only call `diary_write` manually if the hook didn't fire or the session had non-obvious decisions worth annotating.

## Setup

Install and initialize (user's machine):

```bash
pip install mempalace
mempalace init ~/my-convos
mempalace mine ~/my-convos
```

Register as an MCP server (Claude Code, Cursor, etc.):

```bash
claude mcp add mempalace -- python -m mempalace.mcp_server
```

Or in an MCP config JSON:

```json
{
  "mcpServers": {
    "mempalace": {
      "command": "python3",
      "args": ["-m", "mempalace.mcp_server"]
    }
  }
}
```

## Tips

- **Search is semantic.** "How did we handle database performance regressions?" works better than keyword "database".
- **KG beats search for temporal questions.** `kg_query(entity, as_of=date)` tells you what was true at a point in time; search can't.
- **Tunnels are navigation shortcuts.** When a topic spans projects, one `traverse` or `follow_tunnels` call beats many `search` queries.
- **Name rooms consistently across wings** to get implicit tunnels for free. Reserve explicit tunnels for non-obvious cross-project links worth labeling.
- **Wings auto-detect from directory names** during `mempalace mine`. You can also create custom wings by calling `add_drawer` with a new wing name.
- **`query` must be keywords only.** Put background prose in `context` — it is not embedded, only reserved for future re-ranking.
- **After external CLI writes**, call `reconnect` so the MCP server sees the new data.
