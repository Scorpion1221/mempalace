# Concurrency Fix Code Review

## Summary

The Wave 1+2 concurrency fix is well-structured and substantially complete. The
three-layer design (`palace_write_lock` for cross-process exclusion,
`refresh_for_write` for stale-cache invalidation, `recovery_wal` for orphaned
payloads on timeout) maps cleanly onto the production failure mode it targets.
Documentation is excellent — every non-trivial function explains the WHY
(citing the HNSW thread-safety issue, the chromadb cache rebuild semantics,
and the verbatim-always promise). All four MCP write tools, both miners, and
the async-save worker now go through the lock + refresh path. Tests cover the
basic primitives well and stress tests added in Wave 3 confirm no corruption
under 6-way and 10-way concurrent writers, no deadlock under SIGKILL, no torn
reads, no data loss after retries, and recovery WAL persistence on timeout.

One **HIGH-severity bug** was found and fixed during this review: the MCP
server's `_get_collection` was constructing `ChromaCollection` instances
without the `backend=`, `palace_path=`, or `collection_name=` kwargs, which
silently turned every `refresh_for_write()` call in the MCP server's write
tools into a no-op. The lock was being acquired correctly but the in-memory
HNSW state was never being rebound to a fresh client, defeating Layer 2 of
the fix in the most important call site. Mechanical fix applied (see
"Source code fixes applied" below).

Several other findings are documented in the **Issues** section. Overall
recommendation: **approve with fixes** — the HIGH-severity fix is already
in, two MEDIUM-severity items deserve attention before this ships to all
users, and the rest can wait.

## Strengths

- Excellent docstrings throughout. `palace_write_lock`, `_client_for_write`,
  `quarantine_corrupt_segments`, and `rebuild_from_verbatim` all cite the
  upstream chroma issues, the failure mode they prevent, and the
  contractual constraints on the caller.
- Lock ordering is consistent everywhere it matters: `mine_lock` (per-file)
  outer, `palace_write_lock` (per-palace) inner. Both miners enforce this
  in the same way and the docstrings call it out explicitly.
- `palace_write_lock` is fcntl-backed (Unix) / msvcrt-backed (Windows), so
  kernel cleanup releases the lock on SIGKILL — a crashed writer cannot
  permanently wedge the palace. Verified by the new
  `test_concurrent_writers_with_random_kill` stress test.
- Every cross-process write path uses non-blocking `LOCK_NB` + 50ms
  polling so a real timeout raises `PalaceWriteLockTimeout` instead of
  blocking the caller forever.
- Lock-file naming uses `Path.resolve()`, so symlink/relative path
  variants of the same palace map to the same lock file. Verified by an
  existing test plus the new sanity test in
  `tests/test_palace_concurrent_stress.py`.
- The recovery WAL preserves user data verbatim when a timeout fires,
  honouring the "verbatim always / 100% recall" promise. The on-disk
  format is JSONL with one record per write intent — operator-friendly.
- Health check is read-only and never raises on corruption. Quarantine is
  non-destructive (segments are renamed, not deleted; SQLite verbatim
  text is preserved). Rebuild-from-verbatim is idempotent.
- Critical sections are kept tight: hashing, JSON parsing, sanitisation,
  embedding generation all happen OUTSIDE the lock. Only the
  `refresh_for_write` + `add/upsert/update/delete` calls run inside.
- The miner gracefully degrades on `PalaceWriteLockTimeout` (skip the
  current file, return room counts unchanged, continue to the next file)
  instead of crashing the whole batch.
- `_async_save_worker` exits cleanly on lock timeout, persisting the
  payload to the recovery WAL and `sys.exit(0)` so the stop hook chain
  is not disrupted.

## Issues

### Critical (must fix before merge)

None outstanding. The one CRITICAL issue found during the review (a missing
`refresh_for_write` rebind context in the MCP server's collection cache)
has been fixed in this same change — see "Source code fixes applied" at
the bottom of this document.

### High (should fix before merge)

#### H-1. Concurrent first-open of a fresh palace races chromadb's `CREATE TABLE`

**Where:** `mempalace/backends/chroma.py` — `ChromaBackend._client(palace_path)`
constructs `chromadb.PersistentClient(path=palace_path)` without holding
`palace_write_lock`. This is the gateway every miner / async-save / MCP server
uses on first call.

**Symptom (reproduced in this review):** when N subprocesses open a brand-new
palace simultaneously, one of them (the loser of the race) crashes with:

```
chromadb.errors.InternalError: error returned from database: (code: 1)
table collections already exists
```

`PersistentClient.__init__` is not idempotent across concurrent first-opens
because chromadb internally executes a `CREATE TABLE collections` statement
without `IF NOT EXISTS` semantics. Once the file exists this is harmless;
during the very first init it isn't.

**Production impact:** in normal use the palace is created once by
`mempalace init` (or the first MCP server's startup) and subsequent processes
attach to the existing palace, so this race almost never fires in production.
But the same code path is hit in the stress tests, which is how it surfaced.
A first-time user spinning up six Cursor windows in parallel against a
brand-new palace dir would see five out of six MCP servers crash on startup.

**Suggested fix (deferred — needs design judgment):**

Option A (cheapest, narrowest): wrap the `PersistentClient(...)` construction
inside `_client` and `_client_for_write` in `palace_write_lock`. The lock
hash is derived from `Path(palace_path).resolve()`, which is stable even
before chromadb creates the directory contents.

Option B (broader): add an init-time bootstrap step that the MCP server
runs once at startup before accepting any tool calls — eagerly call
`get_collection(create=True)` while holding `palace_write_lock`. This
amortises the race across all tools and matches what `_run_startup_health_check`
already does conceptually.

Option C (out-of-band): document the "first run must be `mempalace init`"
contract more loudly, so multi-process consumers know to bootstrap once
before parallel MCP servers attach.

I did NOT apply this fix because the right answer depends on whether
mempalace wants to support truly parallel first-opens (Option A/B) or
declare "first-init is single-writer" as a contract (Option C). I worked
around the race in the stress tests by pre-initialising the palace before
spawning workers, with a docstring explaining why.

#### H-2. Lock overhead is large when refresh_for_write is real

**Where:** `mempalace/backends/chroma.py` — `_client_for_write` calls
`gc.collect()` whenever the cached client must be evicted (which happens on
every cross-process write because some other writer just changed the SQLite
mtime).

**Measured cost (Wave 3 benchmark, MacBook Apple silicon):**

| Metric | Value |
|---|---|
| Baseline no-lock throughput | 209 writes/s |
| Uncontended w/lock throughput | 52.6 writes/s |
| **Lock overhead (uncontended)** | **74.86%** (4× slowdown) |
| 6-way contended throughput | 51.7 writes/s (within noise of uncontended — convoy is real) |
| Per-write no-lock p50 | 3.76 ms |
| Per-write w/lock p50 | 20.6 ms |
| **Per-write lock overhead p50** | **16.84 ms** |
| Per-write w/lock p99 (uncontended) | 28.4 ms |
| Per-write w/lock p99 (6-way contended) | 345 ms |

The 16.84 ms p50 overhead per write is well above the original
"< 5 ms p50 on a modern Mac" target you set for me. The dominant cost is
`gc.collect()` inside `_client_for_write` (forced on every cache eviction
to release the chromadb HNSW mmap).

**Production impact:** for the MCP server's interactive write tools this is
unmeasurable to a user (one drawer at a time). For the miner running
through 1000s of files, this multiplies linearly into the total mine
duration. Most affected: `mempalace mine` against a large transcript dir
when another process is also writing.

**Suggested fix (deferred — needs design judgment):**

1. Skip `gc.collect()` when `cached_stat == current_stat` (no eviction
   needed — we're reusing the cached client anyway).
2. Move `gc.collect()` to a background thread that runs only when an
   eviction actually happens AND the previous client had a non-trivial
   HNSW state (i.e. heuristically only on first-rebuild after a real
   write by some other process).
3. Drop `gc.collect()` entirely and rely on Python's normal refcount-
   driven cleanup; the next `client = chromadb.PersistentClient(...)`
   line drops the last strong ref to the old client immediately. The
   gc.collect was probably defensive for chromadb-internal cycles but
   may not be load-bearing.

I did NOT change this because the right call depends on whether the
defensive gc was added in response to a specific upstream bug. The
benchmark numbers are recorded in `tests/benchmarks/test_lock_overhead.py`
so future work has a baseline to compare against.

### Medium (consider fixing)

#### M-1. Recovery WAL has no drainer

**Where:** `mempalace/recovery_wal.py` — explicitly a `TODO(wave-3)` in the
module docstring. `_async_save_worker` writes the recovery WAL on
`PalaceWriteLockTimeout` but no code reads it back. Until a drainer lands,
orphaned recovery files require a manual `mempalace repair --replay-recovery`
(also not yet implemented).

**Impact:** if a user's stop-hook saves time out repeatedly during a
multi-window Cursor day, the recovery WAL builds up but never drains.
Data is preserved (good) but isn't searchable until manually replayed.

**Suggested fix:** add a startup drainer to `_async_save_worker`. On entry,
list `recovery_dir_for_palace(palace_path)`, replay each JSONL record under
the same `palace_write_lock`, then `os.unlink` the file. Keep replay idempotent
by using the same hash-derived ids the original write used (already true for
diary and drawer ops; would need verification for KG/tunnel ops).

#### M-2. `tool_create_tunnel` / `tool_delete_tunnel` / KG writes are NOT under `palace_write_lock`

**Where:** `mempalace/mcp_server.py` lines 580-630 (tunnel ops) and
905-960 (KG ops). They write to separate stores (tunnel JSON via
`mine_lock(_TUNNEL_FILE)`; KG SQLite via `KnowledgeGraph` which uses its
own SQLite locking).

**Why this is medium not critical:** these stores are NOT chromadb HNSW.
The race the lock prevents (HNSW segment corruption) doesn't apply here.
Tunnel writes are guarded by `mine_lock` on the tunnel file. KG writes
use SQLite WAL which has its own concurrency story.

**Why it's still worth looking at:** the `palace_write_lock` is conceptually
per-palace, not per-collection. A future maintainer may reasonably assume
that "any palace mutation" goes through it. Documenting that tunnel and
KG ops are intentionally outside would prevent future confusion.

**Suggested fix:** add a one-line note to `palace_write_lock`'s docstring:
"This lock guards ChromaDB HNSW writes only. Tunnel JSON and the
knowledge-graph SQLite have their own per-store locking and do NOT
participate in this lock." Alternatively: bring them under the same lock
so consumers don't have to reason about it.

#### M-3. `mempalace.repair.rebuild_from_verbatim` opens raw chromadb client without backend wiring

**Where:** `mempalace/repair.py:564` — `client = backend._client_for_write(...)`
is fine, but the subsequent `client.get_or_create_collection(...)` returns a
RAW chroma collection (not a `ChromaCollection` adapter). All the upserts
happen against this raw object, bypassing `_validate_where`, `embeddings`
type coercion, etc.

**Why it works today:** the rebuild path passes well-formed args
(documents/ids/metadatas only — no where filters that would need validation,
no exotic embedding types). So the raw object is fine.

**Why it might bite us later:** if anyone adds a filter or richer write
to `rebuild_from_verbatim`, they'll skip the protections on `ChromaCollection`
and hit a different failure mode than the rest of the codebase.

**Suggested fix:** wrap the raw collection in `ChromaCollection(raw, backend=...,
palace_path=..., collection_name=...)` so `refresh_for_write` is also wired
in case of a future need.

### Low (nits, optional)

#### L-1. Inconsistent timeout values

`mcp_server.py`, `miner.py`, `convo_miner.py`, `hooks_cli.py` all define
their own `_PALACE_WRITE_LOCK_TIMEOUT_S = 30.0`. A future change that
needs to bump this (e.g. to 60s for slow CI) requires touching four files.

**Suggested fix:** lift the constant into `mempalace/palace.py` (next to
`palace_write_lock` itself) and import it everywhere.

#### L-2. `_PALACE_LOCK_POLL_INTERVAL_S` is module-private but tunable knob

In `palace.py` line 352. Some operators may want a longer poll interval
to reduce CPU under contention. Currently no config knob.

**Suggested fix:** make it env-overridable
(`MEMPAL_PALACE_LOCK_POLL_MS`), or document the tradeoff in the
docstring so operators know to monkey-patch it.

#### L-3. `quarantine_corrupt_segments` does not acquire `palace_write_lock`

By design (the docstring explains: "by definition this is called when
the palace is suspected unhealthy, and taking the write lock could
deadlock against the corrupted state we're trying to clean up"). That
reasoning is sound.

But there is a related risk: if a non-corrupt palace is being actively
written to and an operator runs `mempalace doctor` followed by a
manual quarantine (or `repair --rebuild-from-verbatim`), the active
writer's HNSW state could be moved out from under it.

**Suggested fix:** the `rebuild_from_verbatim` path already handles this
correctly (acquires `palace_write_lock` before quarantining). The
standalone `quarantine_corrupt_segments` could log a warning when the
palace_write lock file is fresh-mtime, suggesting the operator stop
other writers first.

#### L-4. `health.py:_check_orphan_locks` only checks for the ONE expected lock file

If two unrelated palaces share the same `~/.mempalace/locks/` dir (one
per machine), an orphan lock from palace B will not be detected when
checking palace A. This is the correct narrow scope but a sweeper that
listed ALL old palace_write_*.lock files would catch crashes for palaces
not currently in scope.

**Suggested fix:** add a separate `mempalace doctor --all-locks` mode that
sweeps every lock file under `~/.mempalace/locks/`, not just the one for
the current palace.

#### L-5. Recovery WAL filename includes microseconds but two writes within one microsecond race

Extremely unlikely (microsecond precision + per-PID suffix) but the file
is opened with `'x'` flag which raises if the path exists. A failed
recovery WAL write would propagate up and re-raise into the caller's
error handling. The caller logs and continues, but the originally-lost
data IS lost.

**Suggested fix:** on `FileExistsError`, retry with a 1-microsecond bump
(or append a random suffix) before giving up.

#### L-6. Stress test docstring claim that `_PALACE_LOCK_POLL_INTERVAL_S` defaults to 50ms

Already in the docstring; this is fine. Just noting that a future
performance tweak (longer poll interval) would change the convoy
behaviour observed in `test_no_data_loss_under_contention`.

## Open Questions

1. **First-open race (H-1):** is the intent that mempalace supports
   parallel first-init, or is "single writer for first init" a
   reasonable contract to require?

2. **gc.collect() in `_client_for_write` (H-2):** was this defensive or
   was there a specific chromadb leak that motivated it? If defensive,
   we can drop it and recoup ~12-15 ms p50 per write.

3. **Recovery WAL drainer (M-1):** sketched as `TODO(wave-3)` but not
   landed. Wave 3 is wrapping up — should this become a Wave 4 ticket
   or land before merge?

4. **MCP server `_get_collection` cache invalidation interaction (just-fixed
   bug):** my fix passes `_BACKEND_FOR_WRITE` (a private singleton) to
   `ChromaCollection`. This means when `refresh_for_write` is called via
   the MCP path, it goes through `ChromaBackend._client_for_write`, which
   has its own `_clients` dict. The MCP server ALSO has `_client_cache`
   from `_get_client()`. Those two caches now diverge — `_get_client`
   serves reads from one cached client while `refresh_for_write` populates
   a separate client in the backend's dict. The two are not in sync.
   In practice the divergence is fine (read path uses `_get_client` cache,
   write path uses `_BACKEND_FOR_WRITE._client_for_write`), but a future
   maintainer should know that the MCP server effectively has TWO
   chroma client caches now: the legacy one for reads, the backend's
   for writes. Worth a comment, possibly worth consolidating later.

## Recommendation

**Approve with fixes.**

- The fix I applied (`mcp_server.py:_get_collection` now wires
  `ChromaCollection(...)` with backend/palace_path/collection_name) is
  required and lands in this change.
- Land **H-1** (first-open race) before announcing multi-window support
  to users. The narrowest fix is wrapping `_client`'s
  `PersistentClient(...)` construction in `palace_write_lock`. Not in
  scope of this PR but an obvious follow-up.
- Land **H-2** (lock overhead) when convenient. The 16.84 ms p50
  overhead is acceptable for current usage but worth shaving.
- Land **M-1** (recovery WAL drainer) when convenient. The recovery WAL
  preserves data, but operator UX of "manual replay" is poor.
- M-2/M-3 and the L-* items can wait.

The Wave 1+2 design is sound. The implementation hits its goals on five
out of five Wave 3 stress tests (six concurrent writers no corruption,
random-kill no deadlock, lock-timeout triggers WAL, no data loss after
retry, no torn reads), and the existing 1395-test suite still passes
zero-regression with my fix in place.

## Source code fixes applied

| File | Lines | Severity | Rationale |
|---|---|---|---|
| `mempalace/mcp_server.py` | 215-272 | HIGH | The cached `ChromaCollection` was being built with positional-only `(raw_collection,)` constructor, omitting the `backend`, `palace_path`, `collection_name`, `embedding_function`, `hnsw_space`, `create` kwargs that `refresh_for_write()` needs. The early-return in `ChromaCollection.refresh_for_write()` (lines 222-226 in `mempalace/backends/chroma.py`: `if self._backend is None or self._palace_path is None or self._collection_name is None: return`) silently turned the entire MCP server's lock + refresh path into "lock + no-op refresh". The lock was preventing concurrent writes from racing the SQLite-level commit, but the in-memory chromadb HNSW state was never being rebound to a fresh client between writes from sibling processes. Fix: introduce a module-level `_BACKEND_FOR_WRITE = ChromaBackend()` singleton and wire all six required kwargs when constructing the cached collection. Verified: all 90 mcp_server tests still pass plus the new Wave 3 stress tests pass with the fix in place. |
