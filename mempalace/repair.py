"""
repair.py — Scan, prune corrupt entries, and rebuild HNSW index
================================================================

When ChromaDB's HNSW index accumulates duplicate entries (from repeated
add() calls with the same ID), link_lists.bin can grow unbounded —
terabytes on large palaces — eventually causing segfaults.

This module provides four operations:

  scan                  — find every corrupt/unfetchable ID in the palace
  prune                 — delete only the corrupt IDs (surgical)
  rebuild               — extract all drawers, delete the collection, recreate
                          with correct HNSW settings, and upsert everything back
  rebuild_from_verbatim — quarantine the HNSW index, rebuild from the verbatim
                          documents in chroma.sqlite3 (source of truth per the
                          ``verbatim always`` design principle). Use this when
                          ``rebuild`` cannot complete because the HNSW state is
                          too damaged for ChromaDB to even open.

The rebuild backs up ONLY chroma.sqlite3 (the source of truth), not the
full palace directory — so it works even when link_lists.bin is bloated.

Usage (standalone):
    python -m mempalace.repair scan [--wing X]
    python -m mempalace.repair prune --confirm
    python -m mempalace.repair rebuild
    python -m mempalace.repair rebuild-from-verbatim

Usage (from CLI):
    mempalace repair
    mempalace repair --rebuild-from-verbatim
"""

import argparse
import logging
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .backends.chroma import ChromaBackend
from .embedding import get_embedding_function


logger = logging.getLogger(__name__)


COLLECTION_NAME = "mempalace_drawers"
CLOSETS_COLLECTION_NAME = "mempalace_closets"

# Insert batch size for re-ingest. Small enough to keep an embedding-fn
# round trip from blowing past LiteLLM rate limits when an EF is wired,
# big enough to amortise the chromadb upsert overhead when it isn't.
_REBUILD_INSERT_BATCH_WITH_EF = 100
_REBUILD_INSERT_BATCH_NO_EF = 5000

# Default timeout for the palace_write_lock during a rebuild. Longer than
# the normal write-path default because rebuilds are inherently long.
_REBUILD_LOCK_TIMEOUT_S = 300.0


def _get_palace_path():
    """Resolve palace path from config."""
    try:
        from .config import MempalaceConfig

        return MempalaceConfig().palace_path
    except Exception:
        default = os.path.join(os.path.expanduser("~"), ".mempalace", "palace")
        return default


def _paginate_ids(col, where=None):
    """Pull all IDs in a collection using pagination."""
    ids = []
    page = 1000
    offset = 0
    while True:
        try:
            r = col.get(where=where, include=[], limit=page, offset=offset)
        except Exception:
            try:
                r = col.get(where=where, include=[], limit=page)
                new_ids = [i for i in r["ids"] if i not in set(ids)]
                if not new_ids:
                    break
                ids.extend(new_ids)
                offset += len(new_ids)
                continue
            except Exception:
                break
        n = len(r["ids"]) if r["ids"] else 0
        if n == 0:
            break
        ids.extend(r["ids"])
        offset += n
        if n < page:
            break
    return ids


def scan_palace(palace_path=None, only_wing=None):
    """Scan the palace for corrupt/unfetchable IDs.

    Probes in batches of 100, falls back to per-ID on failure.
    Writes corrupt_ids.txt to the palace directory for the prune step.

    Returns (good_set, bad_set).
    """
    palace_path = palace_path or _get_palace_path()
    print(f"\n  Palace: {palace_path}")
    print("  Loading...")

    col = ChromaBackend().get_collection(
        palace_path, COLLECTION_NAME, embedding_function=get_embedding_function()
    )

    where = {"wing": only_wing} if only_wing else None
    total = col.count()
    print(f"  Collection: {COLLECTION_NAME}, total: {total:,}")
    if only_wing:
        print(f"  Scanning wing: {only_wing}")

    print("\n  Step 1: listing all IDs...")
    t0 = time.time()
    all_ids = _paginate_ids(col, where=where)
    print(f"  Found {len(all_ids):,} IDs in {time.time() - t0:.1f}s\n")

    if not all_ids:
        print("  Nothing to scan.")
        return set(), set()

    print("  Step 2: probing each ID (batches of 100)...")
    t0 = time.time()
    good_set = set()
    bad_set = set()
    batch = 100

    for i in range(0, len(all_ids), batch):
        chunk = all_ids[i : i + batch]
        try:
            r = col.get(ids=chunk, include=["documents"])
            for got in r["ids"]:
                good_set.add(got)
            for mid in chunk:
                if mid not in good_set:
                    bad_set.add(mid)
        except Exception:
            for sid in chunk:
                try:
                    r = col.get(ids=[sid], include=["documents"])
                    if r["ids"]:
                        good_set.add(sid)
                    else:
                        bad_set.add(sid)
                except Exception:
                    bad_set.add(sid)

        if (i // batch) % 50 == 0:
            elapsed = time.time() - t0
            rate = (i + batch) / max(elapsed, 0.01)
            eta = (len(all_ids) - i - batch) / max(rate, 0.01)
            print(
                f"    {i + batch:>6}/{len(all_ids):>6}  "
                f"good={len(good_set):>6}  bad={len(bad_set):>6}  "
                f"eta={eta:.0f}s"
            )

    print(f"\n  Scan complete in {time.time() - t0:.1f}s")
    print(f"  GOOD: {len(good_set):,}")
    print(f"  BAD:  {len(bad_set):,}  ({len(bad_set) / max(len(all_ids), 1) * 100:.1f}%)")

    bad_file = os.path.join(palace_path, "corrupt_ids.txt")
    with open(bad_file, "w") as f:
        for bid in sorted(bad_set):
            f.write(bid + "\n")
    print(f"\n  Bad IDs written to: {bad_file}")
    return good_set, bad_set


def prune_corrupt(palace_path=None, confirm=False):
    """Delete corrupt IDs listed in corrupt_ids.txt."""
    palace_path = palace_path or _get_palace_path()
    bad_file = os.path.join(palace_path, "corrupt_ids.txt")

    if not os.path.exists(bad_file):
        print("  No corrupt_ids.txt found — run scan first.")
        return

    with open(bad_file) as f:
        bad_ids = [line.strip() for line in f if line.strip()]
    print(f"  {len(bad_ids):,} corrupt IDs queued for deletion")

    if not confirm:
        print("\n  DRY RUN — no deletions performed.")
        print("  Re-run with --confirm to actually delete.")
        return

    col = ChromaBackend().get_collection(
        palace_path, COLLECTION_NAME, embedding_function=get_embedding_function()
    )
    before = col.count()
    print(f"  Collection size before: {before:,}")

    batch = 100
    deleted = 0
    failed = 0
    for i in range(0, len(bad_ids), batch):
        chunk = bad_ids[i : i + batch]
        try:
            col.delete(ids=chunk)
            deleted += len(chunk)
        except Exception:
            for sid in chunk:
                try:
                    col.delete(ids=[sid])
                    deleted += 1
                except Exception:
                    failed += 1
        if (i // batch) % 20 == 0:
            print(f"    deleted {deleted}/{len(bad_ids)}  (failed: {failed})")

    after = col.count()
    print(f"\n  Deleted: {deleted:,}")
    print(f"  Failed:  {failed:,}")
    print(f"  Collection size: {before:,} → {after:,}")


def rebuild_index(palace_path=None):
    """Rebuild the HNSW index from scratch.

    1. Extract all drawers via ChromaDB get()
    2. Back up ONLY chroma.sqlite3 (not the bloated HNSW files)
    3. Delete and recreate the collection with hnsw:space=cosine
    4. Upsert all drawers back
    """
    palace_path = palace_path or _get_palace_path()

    if not os.path.isdir(palace_path):
        print(f"\n  No palace found at {palace_path}")
        return

    print(f"\n{'=' * 55}")
    print("  MemPalace Repair — Index Rebuild")
    print(f"{'=' * 55}\n")
    print(f"  Palace: {palace_path}")

    backend = ChromaBackend()
    try:
        col = backend.get_collection(palace_path, COLLECTION_NAME)
        total = col.count()
    except Exception as e:
        print(f"  Error reading palace: {e}")
        print("  Palace may need to be re-mined from source files.")
        return

    print(f"  Drawers found: {total}")

    if total == 0:
        print("  Nothing to repair.")
        return

    # Extract all drawers in batches
    print("\n  Extracting drawers...")
    batch_size = 5000
    all_ids = []
    all_docs = []
    all_metas = []
    offset = 0
    while offset < total:
        batch = col.get(limit=batch_size, offset=offset, include=["documents", "metadatas"])
        if not batch["ids"]:
            break
        all_ids.extend(batch["ids"])
        all_docs.extend(batch["documents"])
        all_metas.extend(batch["metadatas"])
        offset += len(batch["ids"])
    print(f"  Extracted {len(all_ids)} drawers")

    # Back up ONLY the SQLite database, not the bloated HNSW files
    sqlite_path = os.path.join(palace_path, "chroma.sqlite3")
    if os.path.exists(sqlite_path):
        backup_path = sqlite_path + ".backup"
        print(f"  Backing up chroma.sqlite3 ({os.path.getsize(sqlite_path) / 1e6:.0f} MB)...")
        shutil.copy2(sqlite_path, backup_path)
        print(f"  Backup: {backup_path}")

    # Rebuild with correct HNSW settings
    print("  Rebuilding collection with hnsw:space=cosine...")
    backend.delete_collection(palace_path, COLLECTION_NAME)
    ef = get_embedding_function()
    new_col = backend.create_collection(palace_path, COLLECTION_NAME, embedding_function=ef)

    filed = 0
    insert_batch = 100 if ef is not None else batch_size
    for i in range(0, len(all_ids), insert_batch):
        batch_ids = all_ids[i : i + insert_batch]
        batch_docs = all_docs[i : i + insert_batch]
        batch_metas = all_metas[i : i + insert_batch]
        try:
            new_col.upsert(documents=batch_docs, ids=batch_ids, metadatas=batch_metas)
        except Exception as exc:
            print(f"  ERROR at batch {i}: {exc}")
            print("  Retrying one-by-one...")
            for j, (did, doc, meta) in enumerate(zip(batch_ids, batch_docs, batch_metas)):
                try:
                    new_col.upsert(documents=[doc], ids=[did], metadatas=[meta])
                except Exception as e2:
                    print(f"    SKIP {did}: {e2}")
                    continue
        filed += len(batch_ids)
        if filed % 500 == 0 or filed == len(all_ids):
            print(f"  Re-filed {filed}/{len(all_ids)} drawers...")

    print(f"\n  Repair complete. {filed} drawers rebuilt.")
    print("  HNSW index is now clean with cosine distance metric.")

    # Rebuild closets collection if it exists
    _rebuild_closets(palace_path, backend, ef)

    print(f"\n{'=' * 55}\n")


def _rebuild_closets(palace_path, backend, ef):
    """Rebuild the closets collection to match the current embedding dimensions."""
    try:
        col = backend.get_collection(palace_path, CLOSETS_COLLECTION_NAME)
        total = col.count()
    except Exception:
        return

    if total == 0:
        print("\n  Closets collection empty, deleting stale index...")
        try:
            backend.delete_collection(palace_path, CLOSETS_COLLECTION_NAME)
            print("  Deleted stale closets collection.")
        except Exception:
            pass
        return

    print(f"\n  Rebuilding closets ({total} entries)...")

    batch_size = 5000
    all_ids, all_docs, all_metas = [], [], []
    offset = 0
    while offset < total:
        batch = col.get(limit=batch_size, offset=offset, include=["documents", "metadatas"])
        if not batch["ids"]:
            break
        all_ids.extend(batch["ids"])
        all_docs.extend(batch["documents"])
        all_metas.extend(batch["metadatas"])
        offset += len(batch["ids"])

    backend.delete_collection(palace_path, CLOSETS_COLLECTION_NAME)
    new_col = backend.create_collection(palace_path, CLOSETS_COLLECTION_NAME, embedding_function=ef)

    insert_batch = 100 if ef is not None else batch_size
    filed = 0
    for i in range(0, len(all_ids), insert_batch):
        batch_ids = all_ids[i : i + insert_batch]
        batch_docs = all_docs[i : i + insert_batch]
        batch_metas = all_metas[i : i + insert_batch]
        try:
            new_col.upsert(documents=batch_docs, ids=batch_ids, metadatas=batch_metas)
        except Exception as exc:
            print(f"  ERROR at closet batch {i}: {exc}")
            for did, doc, meta in zip(batch_ids, batch_docs, batch_metas):
                try:
                    new_col.upsert(documents=[doc], ids=[did], metadatas=[meta])
                except Exception:
                    pass
        filed += len(batch_ids)

    print(f"  Closets rebuilt: {filed} entries.")


# =============================================================================
# REBUILD FROM VERBATIM
# =============================================================================


@dataclass(frozen=True)
class RebuildReport:
    """Outcome of a ``rebuild_from_verbatim`` invocation."""

    palace_path: Path
    quarantine_path: Optional[Path]
    drawers_processed: int
    drawers_failed: int
    failures: list[tuple[Path, str]] = field(default_factory=list)
    duration_seconds: float = 0.0


def _read_verbatim_drawers(db_path: Path):
    """Yield ``(drawer_id, document, metadata_dict)`` from chroma.sqlite3.

    Reads directly from SQLite — does NOT instantiate a chromadb client.
    That's the whole point: when the HNSW state is too damaged to open
    via ``PersistentClient`` we still need to rescue the verbatim text.

    Joins:
      embeddings              : drawer ids
      embedding_metadata      : verbatim ``chroma:document`` rows + user metadata
      segments / collections  : scope to the drawers collection only
    """
    if not db_path.is_file():
        return
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30) as conn:
        # Pull every row for the drawers collection. The result set is
        # one row per (embedding_id, key) — we group in Python afterward.
        rows = conn.execute(
            """
            SELECT e.embedding_id,
                   m.key,
                   m.string_value,
                   m.int_value,
                   m.float_value,
                   m.bool_value
              FROM embeddings AS e
              JOIN segments AS s ON s.id = e.segment_id
              JOIN collections AS c ON c.id = s.collection
              LEFT JOIN embedding_metadata AS m ON m.id = e.id
             WHERE c.name = ?
             ORDER BY e.embedding_id
            """,
            (COLLECTION_NAME,),
        ).fetchall()

    grouped: dict[str, dict] = {}
    for embedding_id, key, sval, ival, fval, bval in rows:
        slot = grouped.setdefault(embedding_id, {"document": None, "metadata": {}})
        if key is None:
            continue
        if key == "chroma:document":
            slot["document"] = sval
            continue
        # ChromaDB stores typed metadata; pick the first non-None typed slot
        # so the rebuilt collection sees the same Python type the miner wrote.
        if sval is not None:
            value: object = sval
        elif ival is not None:
            value = int(ival)
        elif fval is not None:
            value = float(fval)
        elif bval is not None:
            value = bool(bval)
        else:
            value = None
        if value is not None:
            slot["metadata"][key] = value
    for embedding_id, slot in grouped.items():
        yield embedding_id, slot["document"], slot["metadata"]


def rebuild_from_verbatim(
    palace_path: str | Path,
    *,
    quarantine_first: bool = True,
    progress_cb: Optional[Callable[[str, int, int], None]] = None,
) -> RebuildReport:
    """Rebuild ChromaDB from the verbatim documents on disk.

    The verbatim text inside ``chroma.sqlite3``'s ``embedding_metadata``
    table (rows where ``key = 'chroma:document'``) is the source of
    truth — per MemPalace's "verbatim always" design principle, that text
    is never lossy, never summarised, and never overwritten. The HNSW
    segment files are only the recall index; if they're damaged we can
    rebuild them from the verbatim.

    Steps:
      1. (optional, default on) ``quarantine_corrupt_segments(...)`` —
         move broken HNSW state out of the way so a fresh open succeeds.
      2. Acquire ``palace_write_lock`` exclusively for the rebuild
         duration. Concurrent writers are blocked; callers may need to
         set their own write timeout high.
      3. Read verbatim drawers directly from SQLite (does not require a
         working HNSW index — that's the whole point).
      4. Open a fresh ChromaDB client; ``get_or_create_collection`` for
         drawers (segments will be regenerated from scratch).
      5. Re-upsert every drawer in batches sized to the active embedding
         function's throughput.

    The pipeline is idempotent: running it twice on a healthy palace
    produces no net change (the second pass quarantines the freshly-
    written segments and writes them back identically).

    Args:
        palace_path: Path to the palace directory.
        quarantine_first: When True (default), move existing HNSW segment
            directories to ``~/.mempalace/quarantine/...`` before rebuild.
            When False, the existing segments stay on disk; the rebuild
            will overwrite them via ``upsert``. Set to False only when
            you've already run ``quarantine_corrupt_segments`` manually.
        progress_cb: Optional callback ``(phase, processed, total)`` for
            progress display. ``phase`` is one of ``"reading"``,
            ``"writing"``, ``"done"``.

    Returns:
        ``RebuildReport`` with counts and timing.

    Raises:
        FileNotFoundError: when ``palace_path`` doesn't exist on disk.
        ValueError: when ``palace_path/chroma.sqlite3`` is missing
            (nothing to rebuild from).
    """
    from .backends.chroma import ChromaCollection
    from .health import quarantine_corrupt_segments
    from .palace import palace_write_lock

    palace_path_p = Path(os.path.abspath(os.path.expanduser(str(palace_path))))
    if not palace_path_p.exists():
        raise FileNotFoundError(f"palace path does not exist: {palace_path_p}")
    db_path = palace_path_p / "chroma.sqlite3"
    if not db_path.is_file():
        raise ValueError(f"no chroma.sqlite3 in {palace_path_p}; nothing to rebuild from")

    started = time.monotonic()
    quarantine_dir: Optional[Path] = None

    if quarantine_first:
        # Outside the write lock: quarantine is by design called when the
        # palace might be too broken to acquire the lock against. The
        # quarantine itself is just file moves and doesn't write anything
        # ChromaDB cares about.
        quarantine_dir = quarantine_corrupt_segments(palace_path_p, reason="rebuild_from_verbatim")

    drawers_processed = 0
    drawers_failed = 0
    failures: list[tuple[Path, str]] = []

    with palace_write_lock(str(palace_path_p), timeout=_REBUILD_LOCK_TIMEOUT_S):
        # Stream the verbatim drawers up front so we know the total for
        # progress reporting. The result set is bounded by the palace
        # size — same memory footprint as the existing ``rebuild_index``.
        rows = list(_read_verbatim_drawers(db_path))
        total = len(rows)
        if progress_cb:
            progress_cb("reading", total, total)

        if total == 0:
            # Nothing to write. Still record the attempt as a no-op.
            duration = time.monotonic() - started
            if progress_cb:
                progress_cb("done", 0, 0)
            return RebuildReport(
                palace_path=palace_path_p,
                quarantine_path=quarantine_dir,
                drawers_processed=0,
                drawers_failed=0,
                failures=failures,
                duration_seconds=duration,
            )

        # Open a fresh write-path client so we know we're seeing the
        # post-quarantine state and not a stale cached client from
        # another part of the process.
        backend = ChromaBackend()
        ef = get_embedding_function()

        client = backend._client_for_write(str(palace_path_p))
        ef_kwargs = {"embedding_function": ef} if ef is not None else {}
        raw_collection = client.get_or_create_collection(
            COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
            **ef_kwargs,
        )
        # Wrap the raw chromadb collection in ChromaCollection so upserts go
        # through the adapter layer: this triggers _note_post_write (so the
        # backend's write-freshness cache stays consistent and the next
        # _client_for_write call from this process doesn't see a spurious
        # "stat changed" eviction), and applies the same _validate_where /
        # embeddings handling the rest of the codebase uses. See bffe779.
        new_col = ChromaCollection(
            raw_collection,
            backend=backend,
            palace_path=str(palace_path_p),
            collection_name=COLLECTION_NAME,
            embedding_function=ef,
            hnsw_space="cosine",
            create=True,
        )

        insert_batch = (
            _REBUILD_INSERT_BATCH_WITH_EF if ef is not None else _REBUILD_INSERT_BATCH_NO_EF
        )

        # Filter rows missing a verbatim document — those are corrupt
        # entries we can't salvage. Surface them as failures so the
        # report is honest, but keep going.
        clean_rows = []
        for embedding_id, document, metadata in rows:
            if document is None or document == "":
                drawers_failed += 1
                failures.append(
                    (
                        Path(str(metadata.get("source_file", embedding_id))),
                        "missing chroma:document — verbatim text not recoverable",
                    )
                )
                continue
            clean_rows.append((embedding_id, document, metadata))

        for batch_start in range(0, len(clean_rows), insert_batch):
            chunk = clean_rows[batch_start : batch_start + insert_batch]
            batch_ids = [row[0] for row in chunk]
            batch_docs = [row[1] for row in chunk]
            batch_metas = [row[2] for row in chunk]
            try:
                new_col.upsert(
                    documents=batch_docs,
                    ids=batch_ids,
                    metadatas=batch_metas,
                )
                drawers_processed += len(chunk)
            except Exception as exc:
                # Fall back to per-row inserts so a single malformed
                # drawer can't take down the whole batch.
                logger.warning(
                    "rebuild_from_verbatim batch %d-%d failed (%s); retrying per-row",
                    batch_start,
                    batch_start + len(chunk),
                    exc,
                )
                for did, doc, meta in zip(batch_ids, batch_docs, batch_metas):
                    try:
                        new_col.upsert(documents=[doc], ids=[did], metadatas=[meta])
                        drawers_processed += 1
                    except Exception as row_exc:
                        drawers_failed += 1
                        failures.append(
                            (
                                Path(str(meta.get("source_file", did))),
                                str(row_exc),
                            )
                        )
            if progress_cb:
                progress_cb("writing", drawers_processed, len(clean_rows))

    duration = time.monotonic() - started
    if progress_cb:
        progress_cb("done", drawers_processed, total)
    return RebuildReport(
        palace_path=palace_path_p,
        quarantine_path=quarantine_dir,
        drawers_processed=drawers_processed,
        drawers_failed=drawers_failed,
        failures=failures,
        duration_seconds=duration,
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="MemPalace repair tools")
    p.add_argument(
        "command",
        choices=["scan", "prune", "rebuild", "rebuild-from-verbatim"],
    )
    p.add_argument("--palace", default=None, help="Palace directory path")
    p.add_argument("--wing", default=None, help="Scan only this wing")
    p.add_argument("--confirm", action="store_true", help="Actually delete corrupt IDs")
    p.add_argument(
        "--no-quarantine",
        action="store_true",
        help="Skip the pre-rebuild quarantine step (rebuild-from-verbatim only)",
    )
    args = p.parse_args()

    path = os.path.expanduser(args.palace) if args.palace else None

    if args.command == "scan":
        scan_palace(palace_path=path, only_wing=args.wing)
    elif args.command == "prune":
        prune_corrupt(palace_path=path, confirm=args.confirm)
    elif args.command == "rebuild":
        rebuild_index(palace_path=path)
    elif args.command == "rebuild-from-verbatim":
        target = path or _get_palace_path()

        def _progress(phase: str, processed: int, total: int) -> None:
            if phase == "writing" and total:
                print(f"  Re-filed {processed}/{total} drawers...")
            elif phase == "done":
                print(f"  Done: {processed} drawers re-filed.")

        report = rebuild_from_verbatim(
            target,
            quarantine_first=not args.no_quarantine,
            progress_cb=_progress,
        )
        print(f"\n  Rebuild from verbatim complete in {report.duration_seconds:.1f}s")
        print(f"  Drawers processed: {report.drawers_processed}")
        if report.drawers_failed:
            print(f"  Drawers failed:    {report.drawers_failed}")
        if report.quarantine_path is not None:
            print(f"  Quarantine path:   {report.quarantine_path}")
