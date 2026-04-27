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

TODO(wave-3): Add a drainer that ``_async_save_worker`` calls at startup
to replay any orphaned recovery files from previous runs. Until that
lands, files in this directory must be replayed manually (or by a future
``mempalace repair --replay-recovery`` command). They are NEVER deleted
automatically — losing orphaned writes silently is the bug we're trying
to prevent.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional


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
