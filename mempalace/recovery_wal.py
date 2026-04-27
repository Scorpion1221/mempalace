"""recovery_wal.py — Recovery Write-Ahead Log for orphaned async-save payloads.

When a background ``_async_save_worker`` (spawned by the stop hook) cannot
acquire the palace write lock within its budget, the user data it was
about to persist would otherwise be silently lost — directly violating
MemPalace's "100% recall / verbatim always" promise.

This module persists the unwritten payload to a per-palace recovery
directory so a future writer can drain it. Each timeout produces a NEW
file (timestamped + PID) so two concurrent timeouts cannot clobber each
other.

Layout:
    ~/.mempalace/recovery/<palace_id>/<iso_timestamp>_<pid>.jsonl

Each line is one JSON record describing a single write intent (op type
+ args). One worker invocation typically produces one file containing
multiple records (diary, drawer(s), kg fact(s), tunnel(s)).

Format note: ``palace_id`` is the first 16 hex chars of
``sha256(Path(palace_path).resolve())``, matching the scheme used by
``palace_write_lock`` so operators can correlate lock filenames with
recovery directories.

Drainer: ``drain_recovery_wal`` replays pending files into the palace
via a caller-supplied ``apply_records`` callback. ``_async_save_worker``
invokes the drainer at startup (inside its own ``palace_write_lock``)
so orphaned payloads land in the palace before the new payload is
processed. ``mempal drain-recovery`` exposes the same operation as a
manual CLI command.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional

logger = logging.getLogger(__name__)


def _palace_id(palace_path: str | Path) -> str:
    """Stable 16-hex-char fingerprint of a palace path.

    Mirrors ``palace_write_lock``'s naming scheme so a recovery directory
    name can be matched by eye to the lock file for the same palace.
    """
    resolved = str(Path(palace_path).resolve())
    return hashlib.sha256(resolved.encode()).hexdigest()[:16]


def recovery_dir_for_palace(palace_path: str | Path) -> Path:
    """Return the recovery directory for a palace (does NOT create it)."""
    return Path(os.path.expanduser("~")) / ".mempalace" / "recovery" / _palace_id(palace_path)


def _next_recovery_path(palace_path: str | Path, pid: Optional[int] = None) -> Path:
    """Compute (but do not create) the next recovery file path.

    Format: ``<iso_timestamp>_<pid>.jsonl``. The iso_timestamp is filesystem-
    safe (no colons) by design — colons are valid on POSIX but reserved on
    NTFS, and we want operators to be able to ``cp`` recovery dirs around
    without rewriting filenames.
    """
    pid = os.getpid() if pid is None else pid
    # ISO 8601 with sub-second resolution, but with ':' replaced so the name
    # is portable across POSIX/NTFS.
    ts = datetime.now().isoformat(timespec="microseconds").replace(":", "-")
    return recovery_dir_for_palace(palace_path) / f"{ts}_{pid}.jsonl"


def persist_records(
    palace_path: str | Path,
    records: Iterable[dict],
    pid: Optional[int] = None,
) -> Path:
    """Persist a sequence of write-intent records as a single JSONL file.

    Each record is a dict with at least an ``op`` field and the args needed
    to replay it. Caller decides the record schema; this module only
    serialises and writes.

    Returns the path to the written file. Raises any underlying OSError
    (callers should catch and log — but never silently swallow, because
    losing recovery data is exactly the failure mode we're guarding
    against).
    """
    target = _next_recovery_path(palace_path, pid=pid)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Open with 'x' to refuse to overwrite — even though the timestamp+pid
    # combination should already be unique, refusing to clobber an existing
    # recovery file is cheaper than restoring lost data.
    with open(target, "x", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True))
            fh.write("\n")
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            # Some filesystems don't support fsync — tolerate, the kernel
            # will still flush on close.
            pass
    return target


def persist_async_save_payload(
    palace_path: str | Path,
    *,
    diary: Optional[dict] = None,
    drawers: Optional[list[dict]] = None,
    kg_facts: Optional[list[dict]] = None,
    tunnels: Optional[list[dict]] = None,
    context: Optional[dict] = None,
    pid: Optional[int] = None,
) -> Path:
    """High-level helper for ``_async_save_worker``'s payload shape.

    Builds a JSONL file with one record per write intent. ``context`` is an
    optional metadata dict (session id, wing, prompt hash, etc.) that gets
    written as the first record so an operator can identify the source of
    the orphaned payload at a glance.
    """
    records: list[dict] = []
    if context:
        records.append({"op": "context", "args": context})
    if diary:
        records.append({"op": "diary", "args": diary})
    for d in drawers or []:
        records.append({"op": "drawer", "args": d})
    for fact in kg_facts or []:
        records.append({"op": "kg_triple", "args": fact})
    for t in tunnels or []:
        records.append({"op": "tunnel", "args": t})
    if not records:
        # Nothing to persist — still write an empty marker so an operator
        # knows a timeout occurred but produced no payload (rare, but worth
        # logging).
        records.append({"op": "empty", "args": {"reason": "lock_timeout_no_payload"}})
    return persist_records(palace_path, records, pid=pid)


# ── Drainer ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DrainResult:
    """Outcome of a ``drain_recovery_wal`` invocation.

    Frozen by design — once returned, the caller should not mutate it; a
    fresh result is produced per drain pass. ``failures`` lists the
    files that errored along with a short error message so operators
    can decide whether to retry, edit, or delete.
    """

    palace_path: Path
    files_processed: int
    files_failed: int
    records_replayed: int
    failures: list[tuple[Path, str]] = field(default_factory=list)
    duration_seconds: float = 0.0


def list_pending(palace_path: str | Path) -> list[Path]:
    """Return the recovery WAL files for ``palace_path``, oldest first.

    Returns an empty list when the recovery directory does not exist —
    a fresh palace with no orphaned payloads is the common case and
    must NOT raise.
    """
    rec_dir = recovery_dir_for_palace(palace_path)
    if not rec_dir.is_dir():
        return []
    try:
        files = [p for p in rec_dir.iterdir() if p.is_file() and p.suffix == ".jsonl"]
    except OSError:
        return []
    # mtime ascending = oldest first; ties are broken by name (which encodes
    # an ISO timestamp + pid so it is also monotonic in practice).
    files.sort(key=lambda p: (p.stat().st_mtime, p.name))
    return files


def _read_jsonl_records(path: Path) -> list[dict]:
    """Read JSONL records from ``path``, skipping malformed lines.

    Behavior decision (documented in the module docstring): a single
    malformed line is logged and SKIPPED, but the rest of the file
    still drains. Rationale — losing 99 valid records to recover from
    one corrupt line would be a worse violation of the "100% recall"
    promise than the corrupt line itself.

    Raises:
        OSError: when the file cannot be opened (caller treats as a
            per-file failure and leaves the file on disk for retry).
    """
    records: list[dict] = []
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                logger.warning(
                    "recovery_wal: skipping malformed line %d in %s: %s",
                    lineno,
                    path,
                    exc,
                )
                continue
    return records


def drain_recovery_wal(
    palace_path: str | Path,
    *,
    apply_records: Callable[[list[dict]], None],
    timeout_per_file_s: float = 30.0,  # noqa: ARG001 — reserved for future use
) -> DrainResult:
    """Replay all pending recovery WAL files for ``palace_path``.

    For each file (oldest first):

    1. Read all JSONL records (malformed lines are skipped per the
       behavior documented in ``_read_jsonl_records``).
    2. Call ``apply_records(records)`` — caller is responsible for the
       actual ChromaDB / KG / tunnel writes and MUST already hold
       ``palace_write_lock`` when those writes need to be serialised.
    3. On success, delete the file.
    4. On failure, log the error, leave the file in place, and continue
       with the next file.

    Returns a ``DrainResult``. Per-file errors NEVER raise (the whole
    point of the WAL is durability across crashes); only programmer
    errors — like an unset ``apply_records`` — propagate.

    The ``timeout_per_file_s`` parameter is reserved for a future
    implementation that could enforce a hard wall-clock cap per file;
    today it is documented but unused so the signature is stable.
    """
    if apply_records is None:  # programmer error, not a runtime fault
        raise TypeError("drain_recovery_wal requires apply_records callback")

    palace = Path(palace_path)
    started = time.monotonic()
    pending = list_pending(palace)

    files_processed = 0
    files_failed = 0
    records_replayed = 0
    failures: list[tuple[Path, str]] = []

    for path in pending:
        try:
            records = _read_jsonl_records(path)
        except OSError as exc:
            files_failed += 1
            failures.append((path, f"read failed: {exc!r}"))
            logger.warning("recovery_wal: cannot read %s: %s", path, exc)
            continue

        if not records:
            # Empty file (or only-malformed) — drop it so it does not
            # accumulate. There is nothing to replay; failing to delete
            # would leave operational noise without value.
            try:
                path.unlink()
                files_processed += 1
            except OSError as exc:
                files_failed += 1
                failures.append((path, f"unlink (empty file) failed: {exc!r}"))
                logger.warning("recovery_wal: could not unlink empty %s: %s", path, exc)
            continue

        try:
            apply_records(records)
        except Exception as exc:  # pragma: no cover - exercised via tests
            files_failed += 1
            failures.append((path, f"apply failed: {exc!r}"))
            logger.warning(
                "recovery_wal: apply_records failed for %s: %s; leaving file on disk for retry",
                path,
                exc,
            )
            continue

        try:
            path.unlink()
        except OSError as exc:
            # The records DID land — but we couldn't remove the file.
            # Treat as a failure so the operator is alerted, otherwise
            # the next drain would re-apply the same records (causing
            # duplicates). The downstream writes are idempotent (drawers
            # are upserted by ID, tunnels are dedup'd by canonical ID,
            # KG triples short-circuit on identical-current matches),
            # so the practical impact is small but worth flagging.
            files_failed += 1
            failures.append((path, f"unlink failed after apply: {exc!r}"))
            logger.warning(
                "recovery_wal: applied %d records but could not unlink %s: %s",
                len(records),
                path,
                exc,
            )
            continue

        files_processed += 1
        records_replayed += len(records)

    return DrainResult(
        palace_path=palace,
        files_processed=files_processed,
        files_failed=files_failed,
        records_replayed=records_replayed,
        failures=failures,
        duration_seconds=time.monotonic() - started,
    )
