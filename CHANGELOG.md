# Changelog

All notable changes to [MemPalace](https://github.com/MemPalace/mempalace) are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/).

---

## [Unreleased — Fork: Scorpion1221/mempalace dev branch]

This section documents changes in the fork that are not yet in upstream.
Based on upstream `3.3.2`.

### What's new (2026-04-24): recall precision overhaul + JSON-mode hardening

Latest batch of improvements on top of the fork's previous recall pipeline. All opt-in via existing env vars — no new flags.

- **Filter-aware rewrite** — `decide_recall` now outputs a `filters: {room?, hall?, wing?}` field alongside `query`. The gate reads the current palace taxonomy (rooms/halls list) from `active_context` and chooses precise filters per question type (personal facts → `hall=hall_diary`; decisions → `room=decisions`; bugs → `room=bugs`; etc.). Search runs with the filter applied, sharply cutting noise from unrelated rooms.
- **KG entity injection into the gate** — Top KG entities (by current-triple participation count, default 60) are passed into `active_context`. The gate echoes any entity name implicit in the user's query into the rewritten query, boosting both vector recall and KG matching. Replaces the brittle "tokenize and length-filter" path that dropped all CJK bigrams.
- **CJK + multilingual entity matching** — `_get_kg_context_for_recall` substring-matches the raw query against `KnowledgeGraph.list_entity_names()`. Fallback CJK-aware filter keeps any token containing a Chinese/Japanese/Korean character even when shorter than 3 chars. `KnowledgeGraph._entity_id` NFKC-normalizes names so half-width / full-width / combined-form variants collapse to the same id.
- **Predicate language consistency** — `_ASYNC_SAVE_PROMPT` requires KG triples to use a single language per triple (Chinese subject/object → Chinese predicate, English → English). Adds a Chinese predicate vocabulary (`使用 / 依赖 / 工作于 / 拥有 / 决定 / 状态 / 修复 / ...`) and a Chinese few-shot showing canonical predicate names.
- **Reranker rewritten for answer-relevance** — `_RERANK_PROMPT` now asks "would quoting this snippet move the answer forward, or just look topically related?" instead of "is this topically related". Includes explicit fact-vs-meta-discussion distinction, question-type calibration (factual questions → strict, exploratory → inclusive), and `NONE` is the correct return when nothing actually answers. The early-exit on small candidate sets is removed — every hit passes through the filter.
- **JSON mode for LLM calls** — `_call_llm` and the per-backend helpers (`_call_openai_compat` / `_call_anthropic` / `_call_vertex`) accept `json_mode=True`. OpenAI-compat sends `response_format: {"type": "json_object"}`; Anthropic / Vertex use the assistant-prefill trick (append `{"role": "assistant", "content": "{"}` and re-prepend `{` to the response). `decide_recall` and async save both call with `json_mode=True`, eliminating most "no JSON in response" failures.
- **Bracket-aware JSON extraction + raw dump on parse failure** — Replaces brittle `response.find("{")` / `response.rfind("}")` with `_extract_first_json_object`, a string-literal-aware brace counter that handles braces inside JSON string values. On any parse failure, the full raw response + first 2 KB of prompt are written to `~/.mempalace/hook_state/async_save_fail_<ts>.txt`. Previously the raw response was lost — observability blind spot is fixed.
- **Hall filter on `search_memories`** — New `hall=` keyword arg on `search_memories` and `build_where_filter`. The recall hook validates LLM-suggested filter values against the actual taxonomy before passing them in, and falls back to an unfiltered retry if the filter empties the result set (defensive against hallucinated labels).
- **Gemini 3.1 Flash-Lite Preview as a recall backend** — Recommended over Haiku 4.5 for the gate: 100% deterministic on classification (Haiku flapped between `personal_fact_query` and `self_contained_statement` on identical input), correctly handles proximal-context personal-fact queries, and significantly cheaper (~$0.10/1M input, $0.40/1M output). Wire it via the existing `MEMPAL_RECALL_*` env vars — no code changes needed.
- **De-personalized prompts** — Replaced session-specific examples (cat ownership, project-specific names) with generic biographical / infra examples (city of residence, employee id, staging api), and rewrote pattern-match rules into principle-based ones to keep the gate from over-fitting any one phrasing.

---

### Installation

```bash
# Clone the fork
git clone git@github.com:Scorpion1221/mempalace.git
cd mempalace && git checkout dev

# Install in editable mode (all 3 agents share the same code)
pip install -e ".[dev]"

# IMPORTANT: verify mempalace CLI uses pyenv Python, not system Python
which mempalace  # should be ~/.pyenv/shims/mempalace
# If it's ~/.local/bin/mempalace, remove it: rm ~/.local/bin/mempalace

# Initialize palace (first time only)
mempalace init ~/your-project-dir
```

### LiteLLM Proxy Setup (Recommended for Gemini Embedding)

Using Gemini embedding through a LiteLLM proxy avoids region restrictions, SSL issues, and API key rate limits. All three agents connect to the same local proxy.

```yaml
# ~/.litellm/config.yaml — add these models:

# Embedding model (used for both save and search vectors)
- model_name: gemini-embedding-2-preview
  litellm_params:
    model: vertex_ai/gemini-embedding-2-preview
    vertex_project: your-gcp-project
    vertex_location: us-central1
    vertex_credentials: /app/your-service-account-key.json

# Recall gate model (used by the LLM recall gate + async save)
# Gemini 3.1 Flash-Lite Preview: faster, cheaper, and more deterministic
# than Haiku 4.5 on the JSON classification task.
- model_name: gemini-3.1-flash-lite-preview
  litellm_params:
    model: vertex_ai/gemini-3.1-flash-lite-preview
    vertex_project: your-gcp-project
    vertex_location: global        # 3.1 Flash-Lite Preview is only in "global"
    vertex_credentials: /app/your-service-account-key.json
    input_cost_per_token: 1.0e-07
    output_cost_per_token: 4.0e-07

# ~/.litellm/docker-compose.yaml — mount the key:
#   volumes:
#     - ./your-service-account-key.json:/app/your-service-account-key.json:ro
```

```bash
# Start: cd ~/.litellm && docker compose up -d
# Test embedding:
curl http://127.0.0.1:4000/v1/embeddings \
  -H "Authorization: Bearer your-litellm-key" \
  -d '{"model":"gemini-embedding-2-preview","input":["test"]}'

# Test recall gate model:
curl http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer your-litellm-key" \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-3.1-flash-lite-preview","max_tokens":20,"messages":[{"role":"user","content":"Reply with exactly: OK"}]}'
```

> **Note**: LiteLLM's Vertex AI embedding proxy doesn't support batch input (returns 1 embedding for N inputs). The embedding module works around this with 10-thread concurrent single-text requests (~7s for 100 texts).

### Sync & Update

**Claude Code plugin** — update via the plugin system (requires marketplace pointed at local repo):
```bash
# One-time: point marketplace at local repo instead of upstream GitHub
claude plugin marketplace add /path/to/mempalace

# Update plugin to match local repo version
claude plugin update mempalace@mempalace
# Restart Claude Code session to apply
```

**All 3 agents** — sync hook scripts and Python package in one command:
```bash
bash scripts/sync-plugins.sh
```
This reinstalls the Python package, syncs hook scripts (preserving local env var customizations), updates Hermes runtime, and verifies env var configuration. The sync script dynamically resolves the Claude plugin cache path from the registry instead of hardcoding a version.

### Configuration — Environment Variables

All enhancements are **opt-in** via environment variables. Without them, behavior is identical to upstream 3.3.2.

**Important**: `~/.zshrc` alone is NOT enough — see [Multi-Agent Environment Setup](#important-multi-agent-environment-setup) below.

```bash
# ── Gemini Embedding via LiteLLM proxy (recommended) ──
export MEMPAL_EMBEDDING_MODEL=gemini-embedding-2-preview
export MEMPAL_EMBEDDING_ENDPOINT=http://127.0.0.1:4000   # LiteLLM proxy
export MEMPAL_EMBEDDING_KEY=your-litellm-key              # LiteLLM master key
# export MEMPAL_EMBEDDING_DIMS=3072                       # optional, default 3072

# ── Gemini Embedding direct (alternative — may hit region/rate limits) ──
# export MEMPAL_EMBEDDING_MODEL=gemini-embedding-2-preview
# export GEMINI_API_KEY=your-gemini-api-key
# export SSL_CERT_FILE=/opt/homebrew/etc/openssl@3/cert.pem

# ── LLM Recall Gate (smart recall + query rewrite + answer-relevance reranker) ──
export MEMPAL_RECALL_LLM=1                                 # enable LLM-enhanced recall

# Pick ONE backend for the recall LLM (in priority order):

# Option A (RECOMMENDED): Any OpenAI-compatible endpoint (LiteLLM, Ollama, etc.)
# Gemini 3.1 Flash-Lite Preview is the recommended model — deterministic on
# JSON classification, fast (~1s), and very cheap. Configure it in your LiteLLM
# config (see "LiteLLM Proxy Setup" above), then point MemPalace at it:
export MEMPAL_RECALL_ENDPOINT=http://127.0.0.1:4000/v1     # your LiteLLM proxy URL
export MEMPAL_RECALL_MODEL=gemini-3.1-flash-lite-preview   # model_name from config.yaml
export MEMPAL_RECALL_KEY=your-litellm-key                  # LiteLLM master key

# Alternative: Claude Haiku 4.5 via the same LiteLLM endpoint
# export MEMPAL_RECALL_MODEL=claude-haiku-4-5-20251001     # works but less stable than Gemini for the gate

# Option B: Vertex AI (when using Claude Code in Vertex mode)
# Requires CLAUDE_CODE_USE_VERTEX=1 + ANTHROPIC_VERTEX_PROJECT_ID + gcloud ADC credentials

# Option C: Anthropic API direct
# Requires ANTHROPIC_API_KEY (uses claude-haiku-4-5-20251001 by default)

# ── Proxy (if Gemini API is blocked in your region) ──
# export HTTPS_PROXY=http://127.0.0.1:7890
```

### Important: Multi-Agent Environment Setup

`~/.zshrc` alone is **not enough** — MCP servers and launchd services don't read shell profiles. You must configure environment variables in each agent's native config:

| Agent | Where to set env vars | Config file |
|---|---|---|
| **Claude Code** | `settings.json` → `env` section | `~/.claude/settings.json` |
| **Codex** (MCP) | `config.toml` → `[mcp_servers.mempalace]` → `env` | `~/.codex/config.toml` |
| **Codex** (hooks) | `config.toml` → `[shell_environment_policy.set]` | `~/.codex/config.toml` |
| **Hermes** | launchd plist → `EnvironmentVariables` | `~/Library/LaunchAgents/ai.hermes.gateway.plist` |

> **Codex gotcha**: Codex has TWO separate process trees — MCP server and hook subprocesses. They read env vars from different config sections. If you only set `[mcp_servers.mempalace].env`, hooks will fail silently (SSL errors, missing API key).

**Claude Code** — add to `~/.claude/settings.json`:
```json
{
  "env": {
    "MEMPAL_EMBEDDING_MODEL": "gemini-embedding-2-preview",
    "MEMPAL_EMBEDDING_ENDPOINT": "http://127.0.0.1:4000",
    "MEMPAL_EMBEDDING_KEY": "your-litellm-key",
    "MEMPAL_RECALL_LLM": "1",
    "MEMPAL_RECALL_ENDPOINT": "http://127.0.0.1:4000/v1",
    "MEMPAL_RECALL_MODEL": "gemini-3.1-flash-lite-preview",
    "MEMPAL_RECALL_KEY": "your-litellm-key"
  }
}
```

**Codex** — needs env vars in TWO places (MCP server + hook subprocess):
```toml
# 1. MCP server process:
[mcp_servers.mempalace]
command = "mempalace-mcp"
args = []
env = { MEMPAL_EMBEDDING_MODEL = "gemini-embedding-2-preview", MEMPAL_EMBEDDING_ENDPOINT = "http://127.0.0.1:4000", MEMPAL_EMBEDDING_KEY = "your-litellm-key" }

# 2. Hook subprocess (UserPromptSubmit recall runs here, NOT in MCP server):
[shell_environment_policy.set]
MEMPAL_EMBEDDING_MODEL = "gemini-embedding-2-preview"
MEMPAL_EMBEDDING_ENDPOINT = "http://127.0.0.1:4000"
MEMPAL_EMBEDDING_KEY = "your-litellm-key"
MEMPAL_RECALL_LLM = "1"
MEMPAL_RECALL_ENDPOINT = "http://127.0.0.1:4000/v1"
MEMPAL_RECALL_MODEL = "gemini-3.1-flash-lite-preview"
MEMPAL_RECALL_KEY = "your-litellm-key"
```

**Hermes** — add to `~/Library/LaunchAgents/ai.hermes.gateway.plist` inside `<dict>` under `EnvironmentVariables`:
```xml
<key>MEMPAL_EMBEDDING_MODEL</key>
<string>gemini-embedding-2-preview</string>
<key>MEMPAL_EMBEDDING_ENDPOINT</key>
<string>http://127.0.0.1:4000</string>
<key>MEMPAL_EMBEDDING_KEY</key>
<string>your-litellm-key</string>
```
Then reload: `launchctl unload ~/Library/LaunchAgents/ai.hermes.gateway.plist && launchctl load ~/Library/LaunchAgents/ai.hermes.gateway.plist`

> **Why not just `~/.zshrc`?** Shell profile env vars are only inherited by processes started from a login shell (e.g. terminal commands, `mempalace repair`). MCP servers are spawned as child processes by the agent harness without a login shell. launchd services have their own isolated environment. Each agent needs its own config.

### How the LLM Recall Gate Works

When `MEMPAL_RECALL_LLM=1` is set, every `UserPromptSubmit` hook goes through:

1. **Local fast-path** — Instant skip for obvious cases (continuations like "继续补", confirmations like "好的", execution commands like "run tests"). Zero API cost.
2. **Active-context assembly** — Snapshot the current palace taxonomy (rooms + halls from ChromaDB metadata) and the top ~60 KG entities by current-triple count. Passed to the gate so it can pick valid filter values and echo canonical entity names into the rewritten query.
3. **LLM decide + rewrite + filter** (~1–2s, JSON-mode enforced) — Decides whether memory recall is needed and outputs:
   - `query` — rewritten in the user's language, with any matching entity names echoed verbatim
   - `filters.room` / `filters.hall` — narrows the search before ranking (e.g. personal facts → `hall=hall_diary`, decisions → `room=decisions`, bugs → `room=bugs`)
   - `after` — ISO date for time-bounded queries
   - Personal-fact queries about the user always recall, even when the fact came up earlier in the thread — memory is the canonical source.
4. **Filtered multi-query search** — Searches with BOTH original query AND rewritten query in parallel (prevents rewrite failures from losing exact matches), filtered by the chosen room/hall. Falls back to unfiltered search if the filter empties the pool (defensive against hallucinated labels).
5. **BM25 hybrid ranking** — Runs on the FULL candidate pool from all retrieval paths before truncation.
6. **LLM rerank (answer-relevance)** (~1s, JSON-mode enforced) — Asks "would this snippet actually answer the question?", filtering topically-similar-but-unanswering meta-discussion. Returns `NONE` when nothing genuinely helps — injecting noise is worse than injecting nothing. Runs on every call regardless of pool size.
7. **KG enrichment** — Substring-matches the raw query against the current KG entity list (CJK-aware, NFKC-normalized), fetches current facts (valid_to IS NULL), appends as `[KG]` lines.

### How Async Haiku Save Works

Stop hook no longer blocks the conversation. Instead, it spawns a background subprocess that:

1. **Reads recent exchanges** from the JSONL transcript (since last save, up to 100K chars)
2. **Injects palace context** — current wings/rooms, recent saves (for dedup), KG entities
3. **Calls Haiku** — with 8 diverse few-shot examples covering config, bugs, architecture, code, CI/CD, tech selection, Chinese/English
4. **Writes diary** — natural language session summary under the project wing
5. **Writes drawers** — each tagged with auto-detected hall (facts/events/discoveries/preferences/advice)
6. **Writes KG facts** — entity-relationship triples with auto-invalidation of stale facts

Triggers on every stop with new exchanges. Cost: ~$0.001/save. Set `MEMPAL_VERBOSE=true` for old blocking behavior.

Total LLM cost per user turn (when recall is triggered, Gemini 3.1 Flash-Lite Preview): ~300 tokens ≈ $0.00004. Switching to Haiku raises it to ~$0.00006. Turns that don't need recall (continuations, code tasks, etc.) cost zero — the local fast-path or LLM gate skips them.

### New Features

**Pluggable embedding model with Gemini support** — Switch from ChromaDB's default all-MiniLM-L6-v2 (384d, MTEB multilingual ~56) to Google's gemini-embedding-2-preview (3072d, MTEB multilingual ~68) via a single environment variable. Real benchmark improvement: Chinese→Chinese similarity 0.76→0.85 (+12%), English→Chinese 0.56→0.73 (+30%).

- New module `mempalace/embedding.py` — `GeminiEmbeddingFunction` implements ChromaDB's `EmbeddingFunction` protocol via raw HTTP (no SDK dependency)
- Handles batching (100 texts/call), retry with exponential backoff
- Backward compatible: unset env var → default local MiniLM, zero API calls

**LLM-powered recall gate (opt-in)** — Two-stage pipeline that decides whether memory recall is needed and rewrites queries for better retrieval.

- Stage 1: `decide_recall()` — LLM judges whether the turn needs memory recall at all, with prioritized rules and 14 few-shot examples covering CJK and English edge cases
- Stage 2: `rerank()` — LLM selects the most relevant results from a larger candidate pool
- Previous assistant context (last 500 chars) passed to both stages for better disambiguation
- Local fast-path (`local_recall_decision()`) skips LLM for obvious cases (continuations, confirmations, self-contained tasks)
- Default model: `claude-haiku-4-5-20251001` (fast, cheap, sufficient for classification tasks)
- Supports 3 backends: Vertex AI, Anthropic API, any OpenAI-compatible endpoint (LiteLLM, Ollama, etc.)

**CJK hybrid search** — Bigram tokenizer for BM25 ranking, preferred-wing boost, and temporal filtering in the searcher.

**Auto-recall hooks for Claude Code and Codex** — `UserPromptSubmit` hook automatically searches the palace and injects relevant memories into the conversation context.

**Async Haiku-powered save (non-blocking)** — Stop hook spawns a background subprocess that calls Haiku to extract diary + drawers + KG facts from the transcript. Zero conversation interruption, ~$0.001/save.

- 8 diverse few-shot examples (Chinese/English, config/bugs/architecture/code/CI-CD/tech-selection)
- Auto-detects hall category per drawer via `detect_hall()`
- Injects current palace state (wings/rooms/recent saves/KG entities) as context for dedup
- Auto-invalidates stale KG facts when the same subject+predicate gets a new object value
- Triggers on every stop with new exchanges (configurable via `MEMPAL_SAVE_INTERVAL`)
- `MEMPAL_VERBOSE=true` restores old blocking behavior for debugging

**MCP Unix socket for hooks** — MCP server listens on `~/.mempalace/mcp.sock` alongside stdio. UserPromptSubmit hook tries socket first (hot HNSW cache, <1s), falls back to cold search (5s+).

**Multi-query search** — `search_memories()` accepts `extra_queries` for parallel vector retrieval. When LLM rewrites a query, both original and rewritten versions run as separate searches, merged by ID (best distance wins). Prevents rewrite failures from losing exact matches.

**Parallel keyword recall** — ChromaDB `$contains` full-text search runs alongside vector retrieval, catching exact keyword matches that vector similarity might miss. All paths merge into a single BM25 hybrid ranking pool.

**KG extraction in Haiku save** — Stop hook now writes entity-relationship facts to the knowledge graph. Predicates use a standard vocabulary guided by examples. Auto-invalidation ensures updated facts (e.g. domain change) mark old values as expired.

**KG-enriched recall** — Search results are augmented with knowledge graph facts. Queries both outgoing and incoming relationships for entities found in results and user query keywords. Only current facts (valid_to IS NULL) are included.

**Content-level noise filter** — `is_noise_content()` detects framework JSON markers in the first 400 chars. Wired into both `miner.py` and `convo_miner.py` chunk pipelines. `NORMALIZE_VERSION` bumped to 3.

### Bug Fixes

- **UserPromptSubmit hook pipe error** — Replaced `INPUT=$(cat)` + `echo "$INPUT" | python3 -m mempalace hook run` pattern with `run_mempalace_hook()` wrapper that inherits stdin directly (matching stop/precompact hooks). Fixes intermittent `line 6: Done echo "$INPUT"` non-blocking errors in Claude Code hook environment.
- **sync-plugins.sh hardcoded version** — Replaced `CLAUDE_CACHE="...3.3.0"` with dynamic resolution from `installed_plugins.json`, preventing version drift when the plugin is upgraded past the hardcoded path.
- **sync-plugins.sh missing Codex files** — Now syncs `plugin.json` and `hooks.json` for Codex (previously only synced `.sh` hook scripts).
- **Concurrent single-text calls for OpenAI-compat embedding proxy** — LiteLLM's Vertex AI embedding proxy doesn't support batch input; embedding module now uses 10-thread concurrent single-text requests as a workaround.
- **Rebuild MCP collection cache when embedding function becomes available** — Prevents stale cache from serving results with the wrong embedding dimensions after switching models.
- **repair.py now rebuilds closets collection** — Previously only handled `mempalace_drawers`, leaving `mempalace_closets` with stale embedding dimensions after switching models.
- **tool-results/ noise ingestion** — Added `tool-results` to `SKIP_DIRS` — 68% of palace drawers were raw tool output from `~/.claude/projects/*/tool-results/` that drowned out real memories.
- **Auto-mine removed from hooks** — Stop and PreCompact hooks no longer auto-mine JSONL transcripts. Valuable content is saved via Haiku async save and explicit MCP tool calls. Auto-mining was the primary source of noise in the palace.
- **BM25 reranking before truncation** — `_hybrid_rank()` now runs on the full candidate pool before `scored[:n_results]` truncation. Previously BM25 could only reorder within the already-truncated vector top-K, missing keyword-relevant results.
- **MCP server test fixtures** — Properly patched `_get_collection`, `_client_cache`, `_collection_cache`, and `_palace_db_inode/mtime` in test fixtures. Fixed 5 pre-existing test failures (update_drawer, diary_write, cache_invalidation).

### Improvements

- **GeminiEmbeddingFunction inherits ChromaDB base class** — Prevents interface mismatch issues (like the embed_query parameter name bug). Future ChromaDB upgrades won't silently break embedding.
- **Query rewrite preserves user's language** — Previously forced English translation, causing 20% retrieval penalty for Chinese content. Now keeps the same language as the user's message.
- **Diary and hooks use natural language instead of AAAK** — AAAK compressed format scored 84.2% vs raw 96.6% on LongMemEval. All prompts now guide plain natural language for better search recall.
- **Diary and hooks write in user's language** — Stop hook and `diary_write` tool description now instruct the model to write in the same language the user used during the session.
- **Content sanitization** — `sanitize_content()` now strips control characters, collapses excessive blank lines, and truncates gracefully with a `[truncated at limit]` marker instead of raising errors.
- **Hook hardening** — 15s internal budget timer, fail-closed with history-keyword fallback, system prompt detection (skips Codex title generation), save-checkpoint noise filtering for previous-assistant cache.
- **Proxy support** — Gemini embedding supports `HTTPS_PROXY` / `ALL_PROXY` for regions where the API is blocked.
- **Startup diagnostics** — Embedding probe at first use detects SSL cert errors, region blocks, and API key issues with actionable fix messages.
- **One-command plugin sync** — `bash scripts/sync-plugins.sh` updates all 3 agents (Claude Code, Codex, Hermes) in one command. Dynamically resolves Claude plugin cache path.
- **Test isolation** — `conftest.py` strips `MEMPAL_EMBEDDING_MODEL` and `MEMPAL_RECALL_LLM` so tests always use the default local model.
- **Recall format enriched** — Each hit now includes creation date. KG facts for related entities are appended as `[KG]` lines.
- **over_fetch increased** — Vector candidate pool from `n_results*3` to `n_results*6`, giving BM25 a larger pool to reorder.

### Troubleshooting

**Async save wrote 0 entries / malformed JSON** — Check `~/.mempalace/hook_state/async_save_fail_<timestamp>.txt` for the raw model response + first 2 KB of prompt. Since the 2026-04-24 update, every parse failure dumps here with a note in `hook.log`. Most common root cause: model returned prose before/after the JSON block — bracket-aware extraction handles that automatically, but dumps surface any remaining edge cases.

**Search returns no hits but data exists** — Check `~/.mempalace/hook_state/hook.log` for:
- `SSL: CERTIFICATE_VERIFY_FAILED` → Set `SSL_CERT_FILE` env var in all agent configs
- `User location is not supported` → Set `HTTPS_PROXY` to a US/EU proxy
- `Embedding dimension 384 does not match 3072` → Run `mempalace repair --yes` after switching models
- `embed_query` errors → Run `pip install -e .` to update entry points

**Hook times out (Codex)** — The LLM recall pipeline takes 8-12s. Set hook timeout to at least 20s in `~/.codex/hooks.json` and `.codex-plugin/hooks.json`.

**MCP server shows "No palace found"** — The MCP server process may be stale. Restart the Claude Code / Codex session to spawn a fresh `mempalace-mcp` process.

### Migration

After enabling Gemini embedding, you **must** re-embed existing drawers — ChromaDB cannot mix 384-dim (MiniLM) and 3072-dim (Gemini) vectors in the same collection. Attempting to write or search without migrating will fail with `Embedding dimension 384 does not match collection dimensionality 3072`.

```bash
# 1. Back up your palace first
cp -r ~/.mempalace/palace ~/.mempalace/palace.backup

# 2. Set the env var
export MEMPAL_EMBEDDING_MODEL=gemini-embedding-2-preview

# 3. Re-embed all drawers (uses Gemini API, ~35min for 45K drawers)
mempalace repair --yes
```

> **Note on repair batch size**: When a custom embedding function is configured, repair uses batch=100 (matching the Gemini API limit) instead of the default 5000. This avoids API timeouts but takes longer. For very large palaces (>50K drawers), consider running repair overnight.

---

## [3.3.1] — 2026-04-16

### New Features

**Multi-language entity detection** — lexical patterns (person verbs, pronouns, dialogue markers, project verbs, stopwords, candidate character classes) now live in the optional `entity` section of each locale JSON under `mempalace/i18n/<lang>.json`. Every public function in `entity_detector` accepts a `languages=` tuple and unions patterns across enabled locales. Default stays `("en",)` so existing English-only callers are unchanged. (#911)

- **Five new fully-supported locales** with CLI strings, AAAK compression instructions, and entity-detection patterns:
  - Brazilian Portuguese `pt-br` (#156)
  - Russian `ru` (#760)
  - Italian `it` (#907)
  - Hindi `hi` (#773)
  - Indonesian `id` (#778)
- **`MempalaceConfig.entity_languages`** — persistent palace-level language selection; `MEMPALACE_ENTITY_LANGUAGES` env override; `mempalace init --lang en,pt-br` flag that saves to `~/.mempalace/config.json` (#911)
- **Per-language `candidate_pattern`** — non-Latin scripts register their own character class, so names like `João`, `Инна`, `राज` are no longer silently dropped by the ASCII-only default (#911)
- **VSCode devcontainer** matching the CI environment (#881)
- `MEMPAL_VERBOSE` env toggle — developers see diaries surfaced in chat while the default remains silent (#871)
- `created_at` timestamps included in search results (#846)

### Bug Fixes

**i18n / Unicode**

- Script-aware word boundaries for combining-mark scripts — Python's `\b` fails on Devanagari vowel signs (`ा ी ु`), Arabic, Hebrew, Thai, Tamil, Khmer etc., truncating names like `अनीता` → `अनीत` and making person-verb patterns never fire. Locales now declare an optional `boundary_chars` field and the i18n loader expands `\b` into a script-aware lookaround boundary (#932)
- Case-insensitive BCP 47 language code resolution — `--lang PT-BR`, `zh-cn`, `Pt-Br` previously fell through to English silently; now resolve to the canonical locale file via lowercase matching, with the entity-pattern cache keyed on the canonical form so casing variations share one cache entry (#928)
- Wire i18n candidate patterns into `miner._extract_entities_for_metadata()`, `palace.build_closet_lines()`, and `entity_registry.extract_unknown_candidates()` — three code paths that still hardcoded ASCII-only `[A-Z][a-z]{2,}` and silently missed Cyrillic, accented Latin, and non-Latin entity metadata tags (#931)
- Explicit `encoding="utf-8"` on `Path.read_text()` calls across entity_registry, instructions_cli, split_mega_files, and onboarding tests — prevents Windows GBK (and other non-UTF-8) locales from corrupting UTF-8 files (#946, #776)
- `ko.json` `status_drawers` used `{drawers}` instead of `{count}`, showing the raw template string instead of the number (#758)
- Move `test_i18n.py` from inside the installed package into `tests/` so pytest actually collects it; remove the `sys.path.insert` hack (#758)
- `Dialect.from_config()` defaulted to `current_lang()` (module-global) when config had no `lang` key — replaced with explicit `"en"` fallback for determinism (#758)

**Other**

- Guard `KnowledgeGraph.close()` and `query_relationship`/`timeline`/`stats` methods with the instance lock to prevent concurrent-access corruption (#887, #884)
- Replace invalid `{"decision": "allow"}` with `{}` in hook responses — the string wasn't a valid decision value and triggered schema warnings (#885)
- `entity_registry.research()` defaults to local-only — previously made outbound Wikipedia HTTPS requests without explicit user opt-in; callers now must pass `allow_network=True` (#811)
- Precompact hook no longer blocks compaction when it fails or takes too long (#856, #858, #863)
- Redirect stdout to stderr during MCP server import so library logging can't corrupt the JSON-RPC channel (#225, #864)
- `mempalace init` auto-adds per-project files to `.gitignore` in git repositories so users don't accidentally commit `mempalace.yaml` / `entities.json` (#185, #866)
- Searcher guards against empty ChromaDB query results that previously raised on edge-case corpora (#195, #865)
- Return empty status instead of an error on a cold-start palace with no drawers yet (#830, #831)
- Restrict file permissions on sensitive palace data (#814)
- Slack transcript importer writes a provenance header and preserves speaker IDs (#815)
- Allow `mempalace mine` to run in directories without a local `mempalace.yaml` and surface the missing-yaml warning on stderr (#604)
- Security hook injection fix (#812)
- Save hook auto-mines transcripts even when `MEMPAL_DIR` is unset (#840)
- Pin the Pages custom domain via a shipped `CNAME` in the deploy artifact (#877)
- Version drift safeguard — sync pyproject + `version.py` + README badge in one place (#876)
- Deploy docs workflow now runs on `develop` only, preventing accidental main-branch deploys (#845)

### Improvements

- Regex compilation optimization for entity extraction — pre-compile per-entity pattern sets once and cache by `(name, languages)` tuple, so multi-language callers don't thrash the cache (#880)
- Knowledge-graph value sanitization now preserves natural punctuation (commas, colons, parentheses) that commonly appears in KG subject/object values (#873)

### Documentation

- Clarify that `mempalace init` requires a `<dir>` argument in CLI help text (#210, #862)
- Domain name and specific impostor sites called out in the scam-alert section (#869)
- Tightened `SECURITY.md` with a real version-support policy and the GHPVR-only reporting channel (#810)
- Fixed stale `pyproject.toml` URLs (#853)
- v4 planning prep (#852)

### Internal

- `palace_graph` tunnel helper test coverage (#908)

---

## [3.3.0] — 2026-04-13

### New Features
- Closet layer — a compact searchable index of pointers to verbatim drawers, enabling fast topical lookup without reading all content (#788)
- BM25 hybrid search — closets boost ranking, drawers remain the source of truth (#795, #829)
- Entity metadata on every drawer for filterable search (#829)
- Diary ingest — day-based rooms for conversation transcripts (#829)
- Cross-wing tunnels — explicit links between rooms in different wings for multi-project agents (#829)
- Drawer-grep — returns the best-matching chunk plus adjacent context drawers (#829)
- Offline fact checker against the entity registry and knowledge graph (#829)
- LLM-based closet regeneration — optional, bring-your-own endpoint, no mandatory API key (#793)
- Hall detection — routes drawer content to `emotions` / `technical` / `family` / `memory` / `identity` / `consciousness` / `creative` halls, enabling hall-based graph connectivity within wings (#835)
- Previous-assistant-context recall for Codex and Claude Code hooks — `Stop` caches the latest assistant reply per session and `UserPromptSubmit` uses its tail (500 chars) for recall query rewrite and LLM rerank, improving short follow-up prompts like “why?” and “continue”

### Bug Fixes
- Set `hnsw:space=cosine` metadata on all collection creation sites — fixes broken similarity scoring under ChromaDB's default L2 distance (#807, #218)
- File-level locking prevents duplicate drawers when agents mine the same file concurrently (#784, #826)
- Hybrid closet+drawer retrieval — closets boost ranking, never gate results (#795)
- Stop hooks from making agents write in chat — saves tokens on every turn (#786)
- Strip system tags, hook output, and Claude UI chrome from drawers before filing (#785)
- Verbatim-safe `strip_noise` scoped to Claude Code JSONL only (#785)
- Prevent diary entry ID collisions via microsecond timestamp and full content hash (#819)
- Auto-rebuild stale drawers via `NORMALIZE_VERSION` schema gate
- Enforce atomic topics in closets and extract richer pointers
- Sync `version.py` to match `pyproject.toml` (#820)
- Remove unused `main` import from `mempalace/__init__.py` (#827)
- README audit — fix 7 stale claims (tool count, version badge, wake-up token cost, `dialect.py` lossless disclaimer, `pyproject.toml` version) with 42 regression-guard tests (#835)
- Rerank now uses the same previous-assistant context as query rewrite, keeping short follow-up recall behavior consistent across the whole LLM-assisted pipeline

### Improvements
- Optimize entity detection with regex caching and pre-compilation (#828)
- Extract locked filing block into helper to keep `mine_convos` under C901 complexity

### Documentation
- Add `docs/CLOSETS.md` — closet layer overview
- Fix stale `milla-jovovich/*` org URLs in website and plugin manifests (#787)
- Fix remaining stale org URLs in contributor docs (#808)
- Rewrite `README.md` and `mempalaceofficial.com` benchmark pages to remove category-error cross-system comparisons (R@5 retrieval recall had been listed next to competitor QA accuracy under one column), remove the retracted "+34% palace boost" claim from the surfaces where it had remained, replace the `100%` Haiku-rerank headline with the honest held-out `98.4%` R@5, drop the LoCoMo `100%` top-50 row (retrieval-bypass artefact), and fix the broken `aya-thekeeper/mempal` reproduction URL (#875)
- Add `docs/HISTORY.md` as the canonical home for corrections, retractions, and public notices; move the 2026-04-07 "Note from Milla & Ben" and the 2026-04-11 impostor-domain notice out of `README.md`
- Add v3.3.0 reproduction result JSONLs and the deterministic `seed=42` 50/450 LongMemEval split under `benchmarks/` — every BENCHMARKS.md claim reproduces exactly

### Internal
- Add test coverage for `mine_lock`, closets, entity metadata, BM25, and diary
- Verify `mine_lock` via disjoint critical-section intervals
- Serialize `mine_lock` concurrency test with multiprocessing
- Make diary state path assertion platform-neutral
- Add `TestTunnels` coverage for cross-wing tunnel operations
- Ruff format with CI-pinned version (0.4.x); format `mempalace/palace.py`

---

## [3.2.0] — 2026-04-12

### Packaging
- Remove `chromadb<0.7` upper bound — unblocks installs against chromadb 1.x palaces (#690)
- Bump version to 3.2.0 across `pyproject.toml`, `mempalace/version.py`, README badge, and OpenClaw SKILL (#761)

### Security
- Harden palace deletion, WAL redaction, and MCP search input handling (#739)
- Consistent input validation, argument whitelisting, concurrency safety, and WAL fixes (#647)
- Remove hardcoded credential paths from benchmark runners (#177)
- Remove global SSL verification bypass in convomem_bench (#176)

### Bug Fixes
- Parse Claude.ai privacy export with `messages` key and sender field (#685, #677)
- Detect mtime changes in `_get_client` to prevent stale HNSW index (#757)
- Hash full content in `tool_add_drawer` drawer ID — stable re-mines (#716)
- Remove 10k drawer cap from status display (#707, #603)
- Correct typo in entity_detector interactive classification prompt (#755)
- Prevent convo_miner from re-processing 0-chunk files on every run (#732, #654)
- Remove silent 8-line AI response truncation in convo_miner (#708, #692)
- Store full AI response in convo_miner exchange chunking (#695)
- Fix `mine --dry-run` TypeError on files with room=None (#687, #586)
- Skip arg whitelist for handlers accepting `**kwargs` (#684, #572)
- Allow Unicode in `sanitize_name()` — Latvian, CJK, Cyrillic (#683, #637)
- Auto-repair BLOB seq_ids from chromadb 0.6→1.5 migration (#664)
- Remove no-op `ORT_DISABLE_COREML` env var (#653, #397)
- Disambiguate hook block reasons to name MemPalace explicitly (#666)
- Use epsilon comparison for mtime to prevent unnecessary re-mining (#610)
- Correct token count estimate in compress summary (#609)
- Implement MCP ping health checks (#600)
- Align `cmd_compress` dict keys with `compression_stats()` return values (#569)
- Skip unreachable reparse points in `detect_rooms_from_folders` on Windows (#558)
- Prevent HNSW index bloat from duplicate `add()` calls (#544, #525)
- Purge stale drawers before re-mine to avoid hnswlib segfault (#544)
- Mitigate system prompt contamination in search queries (#385, #333)
- Count Codex `user_message` turns in `_count_human_messages` (#373, #347)
- Paginate large collection reads and surface errors in MCP tools (#371, #339, #338)
- Expand `~` in split command directory argument (#361)
- Ignore `wait_for_previous` argument to support Gemini MCP clients (#322)
- Close KnowledgeGraph SQLite connections in test fixtures (#450)
- Remove duplicate cache variable declarations in mcp_server.py (#449)
- Add `--yes` flag to init instructions for non-interactive use (#682, #534)
- Add `mcp` command with setup guidance (#315)

### New Features
- i18n support — 8 languages (en, es, fr, de, ja, ko, zh-CN, zh-TW) (#718)
- New MCP tools: get/list/update drawer, hook settings, export (#667, #635)
- `mempalace migrate` — recover palaces from different ChromaDB versions (#502)
- Add OpenClaw/ClawHub skill (#491)
- Backend seam for pluggable storage backends (#413)

### Improvements
- Disable broken auto-bump workflow (#414)
- Improve agent readiness — AGENTS.md, dependabot, CODEOWNERS, labels (#497)

### Documentation
- Add CLAUDE.md and mission/principles to AGENTS.md (#720)
- Add VitePress documentation site (#439)
- Add warning about fake MemPalace websites (#598)
- Fix stale org URLs and PR branch target in contributor docs (#679)
- Fix misaligned architecture diagram (#734, #733)
- Add ROADMAP.md — v3.1.1 stability patch and v4.0.0-alpha plan

### Internal
- ruff format convo_miner.py (#741)
- ruff format all Python files (#675)
- CI: trigger tests on develop branch PRs and pushes (#674)
- CI: fix GitHub Pages publishing (#691)

---

## [3.1.0] — 2026-04-09

### Security
- Harden inputs, fix shell injection, optimize DB access (#387)
- Sanitize SESSION_ID in save hook to prevent path traversal (#141)
- Sanitize error responses and remove `sys.exit` from library code (#139)
- Shell injection fix in hooks, Claude Code mining, chromadb pin (#114)

### Bug Fixes
- MCP null args hang, repair infinite recursion, OOM on large files (#399)
- Release ChromaDB handles before rmtree on Windows (#392)
- Use `os.utime` in mtime test for Windows compatibility (#392)
- Negotiate MCP protocol version instead of hardcoding (#324)
- Use upsert and deterministic IDs to prevent data stagnation (#140)
- Make `drawer_id` deterministic for idempotent writes (#387)
- Honest AAAK stats — word-based token estimator, lossy labels (#147)
- Room detection checks keywords against folder paths (#145)
- Use actual detected room in mine summary stats (#165)
- Honour `--palace` flag in mcp_server (#264)
- Preserve default KG path when `--palace` not passed (#270)
- `--yes` flag skips all interactive prompts in init (#123)
- Repair command, split args, Claude export, room keywords (#119)
- Replace Unicode separator in convo_miner.py for Windows compatibility (#129)
- Coerce MCP integer arguments to native Python int (#84)
- Batch ChromaDB reads to avoid SQLite variable limit (#66)
- Respect nested .gitignore rules during mining (#78)
- Narrow bare `except Exception` to specific types where safe (#54)
- Mark MD5 as non-security in miner drawer ID generation (#53)
- Remove dead code and duplicate set items in entity_registry.py (#42)
- Silence ChromaDB telemetry warnings and CoreML segfault on Apple Silicon (#236)
- Unify package and MCP version reporting (#16)
- Fix broken AAAK Dialect link in README (#238)
- Update input prompt for entity confirmation (#83)
- Preserve CLI exit codes, log tracebacks, sanitize search errors (#139)
- Enable SQLite WAL mode and add consistent LIMIT to KG timeline (#136)
- Add limit=10000 safety cap to all unbounded ChromaDB `.get()` calls (#137)
- Re-mine modified files, idempotent `add_drawer`, cleanup ChromaDB handles (#140)
- Resolve formatting, regression logic, and pytest defaults (#270)
- Use `parse_known_args` to allow importing mcp_server during pytest (#270)

### New Features
- Package MemPalace as standard Claude and Codex plugins (#270)
- Add OpenAI Codex CLI JSONL normalizer (#61)
- Add Codex plugin support with hooks, commands, and documentation (#270)
- Add command documentation for help, init, mine, search, and status (#270)

### Improvements
- Cache ChromaDB `PersistentClient` instead of re-instantiating per call (#135)
- Tighten chromadb version range and add `py.typed` marker (#142)
- Consolidate split known-names config loading (#22)
- CI: add separate jobs for Windows and macOS testing
- CI: Upgrade GitHub Actions for Node 24 compatibility (#55)

### Documentation
- Add Gemini CLI setup guide and integration section (#106)
- Add beginner-friendly hooks tutorial (#103)
- Align MCP setup examples with shipped server (#21)
- Honest README update — own the mistakes, fix the claims

### Internal
- Expand test coverage from 20 to 92 tests, migrate to uv (#131)
- Add scale benchmark suite — 106 tests (#223)
- Increase test coverage from 30% to 85%, fix Windows encoding bugs (#281)
- Add WAL mode and entity timeline limit assertions
- Add coverage for `file_already_mined` mtime check

---

## [3.0.0] — 2026-04-06

Initial public release.

- Palace architecture with day-based rooms, drawers (verbatim), and closets (searchable index)
- AAAK compression dialect for memory folding
- Knowledge graph with entity detection and timeline queries
- MCP server for Claude, Codex, and Gemini integration
- CLI: `init`, `mine`, `search`, `status`, `compress`, `repair`, `split`
- Benchmark suite with recall and scale tests
- README with MCP flow, local model flow, and specialist agent documentation

---

[Unreleased]: https://github.com/MemPalace/mempalace/compare/v3.2.0...HEAD
[3.2.0]: https://github.com/MemPalace/mempalace/compare/v3.1.0...v3.2.0
[3.1.0]: https://github.com/MemPalace/mempalace/compare/v3.0.0...v3.1.0
[3.0.0]: https://github.com/MemPalace/mempalace/releases/tag/v3.0.0
