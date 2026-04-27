"""
health.py — Non-destructive palace diagnostics + safe quarantine.
================================================================

Layer 4 of the concurrency-fix stack. Layer 1 (``palace_write_lock``)
and Layer 2 (``ChromaBackend._client_for_write``) prevent NEW corruption
on top of the existing palace. This module addresses the OTHER half of
the problem: palaces that may already be damaged from prior incidents.

Two surfaces are exposed:

    check_palace_health(palace_path) -> HealthReport
        Read-only diagnostic. NEVER mutates state. NEVER raises on
        corruption — corruption manifests as ``HealthIssue`` entries
        and a ``status`` of ``"corrupt"`` or ``"warn"``. Only raises
        on programmer error (palace_path doesn't exist, etc.).

    quarantine_corrupt_segments(palace_path, *, reason="manual") -> Path
        Move HNSW segment directories (and other ChromaDB index state
        we believe is corrupt) to ``~/.mempalace/quarantine/<palace_id>/
        <iso_timestamp>/``. The verbatim text inside ``chroma.sqlite3``
        is left untouched — that's the source of truth for rebuilds.

The ChromaDB on-disk layout we rely on:

    palace/
        chroma.sqlite3           # metadata + verbatim documents
        <segment-uuid>/          # one per collection; HNSW state
            data_level0.bin
            length.bin
            link_lists.bin
            header.bin

A "verbatim drawer file" in this codebase means the ``chroma:document``
row in the SQLite ``embedding_metadata`` table. There are no separate
``.md`` files written to disk — verbatim text lives in SQLite, the HNSW
files are just the recall index over that text.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────

DRAWERS_COLLECTION = "mempalace_drawers"
QUARANTINE_DIRNAME = "quarantine"

# Files that constitute the HNSW segment for a single ChromaDB collection.
# Presence of *any* of these inside a UUID-shaped directory marks it as a
# segment dir; we move the whole directory together (they're meaningless
# split apart).
_HNSW_SEGMENT_FILES = frozenset(
    {
        "data_level0.bin",
        "length.bin",
        "link_lists.bin",
        "header.bin",
        "index_metadata.pickle",
    }
)

# Files that may legitimately be 0 bytes:
#   - ``link_lists.bin`` is empty until the index has enough vectors to
#     need cross-layer links (HNSW only writes layer >0 lists when the
#     graph spans more than one layer).
#   - ``index_metadata.pickle`` is sometimes absent or empty until the
#     first compaction.
# Empty values in any of the OTHER files (data_level0.bin, length.bin,
# header.bin) DO indicate a torn write.
_HNSW_FILES_THAT_MAY_BE_EMPTY = frozenset(
    {
        "link_lists.bin",
        "index_metadata.pickle",
    }
)

# Used by ``_iter_segment_dirs`` to recognise UUID-shaped subdirectories.
# ChromaDB segment directory names are random UUID4 strings — we don't
# match the full UUID grammar, just "name has dashes and isn't dotfile /
# already drift / quarantine sentinel".
_SEGMENT_NAME_BLOCKLIST_PREFIXES = (".", "_")
_SEGMENT_NAME_BLOCKLIST_SUFFIX_MARKERS = (".drift-", ".quarantine-")

# Lock file age threshold for orphan-warning. Anything older than this is
# almost certainly a leaked lock from a crashed writer.
_ORPHAN_LOCK_AGE_SECONDS = 3600.0  # 1 hour

# Statuses, narrowed from ``Literal`` for runtime use.
_STATUS_OK: Literal["ok"] = "ok"
_STATUS_WARN: Literal["warn"] = "warn"
_STATUS_CORRUPT: Literal["corrupt"] = "corrupt"


# ── Data classes ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class HealthIssue:
    """One issue surfaced by ``check_palace_health``.

    Severities are ordered ``info < warn < corrupt``. The report's
    overall ``status`` is the highest severity present.
    """

    severity: Literal["info", "warn", "corrupt"]
    code: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HealthReport:
    """Full health snapshot for a palace.

    ``drawer_count_*`` fields may be ``None`` when the corresponding
    layer could not be queried at all (e.g. SQLite missing entirely).
    Use the ``issues`` list to interpret why.
    """

    status: Literal["ok", "warn", "corrupt"]
    palace_path: Path
    drawer_count_sqlite: int | None
    drawer_count_hnsw: int | None
    drawer_count_verbatim: int | None
    sqlite_integrity_ok: bool
    issues: list[HealthIssue]
    checked_at: datetime


# ── Internal helpers ──────────────────────────────────────────────────


def _normalise_palace_path(palace_path: str | Path) -> Path:
    """Coerce input to an absolute ``Path``. Doesn't require existence yet."""
    return Path(os.path.abspath(os.path.expanduser(str(palace_path))))


def _palace_id(palace_path: Path) -> str:
    """Stable, filesystem-safe identifier derived from the palace path.

    Used to scope quarantine subdirectories per-palace so multiple palaces
    on one machine don't collide.
    """
    import hashlib

    resolved = str(palace_path.resolve()) if palace_path.exists() else str(palace_path)
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]
    name_part = palace_path.name or "palace"
    # Replace anything that could conflict with filesystem safety. Keeping
    # it readable helps the user navigate ``~/.mempalace/quarantine/`` by hand.
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in name_part)[:40]
    return f"{safe_name}-{digest}"


def _quarantine_root() -> Path:
    """``~/.mempalace/quarantine`` — created on demand."""
    root = Path(os.path.expanduser("~")) / ".mempalace" / QUARANTINE_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def _iter_segment_dirs(palace_path: Path):
    """Yield ``Path`` objects for each ChromaDB HNSW segment directory.

    Heuristic: a direct subdirectory of the palace whose name has at least
    one ``-`` (UUID-shaped), is not a dotfile / quarantine sentinel, and
    contains at least one of the known HNSW segment files.
    """
    if not palace_path.is_dir():
        return
    try:
        entries = list(palace_path.iterdir())
    except OSError:
        return
    for entry in entries:
        if not entry.is_dir():
            continue
        name = entry.name
        if name.startswith(_SEGMENT_NAME_BLOCKLIST_PREFIXES):
            continue
        if any(marker in name for marker in _SEGMENT_NAME_BLOCKLIST_SUFFIX_MARKERS):
            continue
        if "-" not in name:
            continue
        try:
            children = {child.name for child in entry.iterdir()}
        except OSError:
            continue
        if children & _HNSW_SEGMENT_FILES:
            yield entry


def _highest_severity(
    issues: list[HealthIssue],
) -> Literal["ok", "warn", "corrupt"]:
    """Reduce a list of issues to the worst severity present."""
    rank = {"info": 0, "warn": 1, "corrupt": 2}
    worst = 0
    for issue in issues:
        worst = max(worst, rank.get(issue.severity, 0))
    if worst >= 2:
        return _STATUS_CORRUPT
    if worst == 1:
        return _STATUS_WARN
    return _STATUS_OK


def _check_sqlite_integrity(db_path: Path) -> tuple[bool, list[HealthIssue]]:
    """Run ``PRAGMA quick_check`` and ``PRAGMA integrity_check``.

    Returns ``(ok, issues)``. Never raises. A SQLite file that can't be
    opened at all is reported as a ``corrupt`` issue and ``ok=False``.
    """
    issues: list[HealthIssue] = []
    if not db_path.is_file():
        issues.append(
            HealthIssue(
                severity="corrupt",
                code="sqlite_missing",
                message=f"chroma.sqlite3 missing at {db_path}",
                detail={"db_path": str(db_path)},
            )
        )
        return False, issues

    try:
        # ``uri=True`` + ``mode=ro`` makes this strictly read-only so a
        # half-broken DB cannot be made worse by integrity checks.
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5) as conn:
            try:
                quick = conn.execute("PRAGMA quick_check;").fetchone()
                quick_ok = quick is not None and quick[0] == "ok"
            except sqlite3.DatabaseError as exc:
                issues.append(
                    HealthIssue(
                        severity="corrupt",
                        code="sqlite_quick_check_failed",
                        message=f"PRAGMA quick_check raised: {exc}",
                        detail={"db_path": str(db_path), "error": str(exc)},
                    )
                )
                return False, issues

            if not quick_ok:
                issues.append(
                    HealthIssue(
                        severity="corrupt",
                        code="sqlite_quick_check_failed",
                        message=f"PRAGMA quick_check returned {quick!r}",
                        detail={"db_path": str(db_path), "result": str(quick)},
                    )
                )

            try:
                integrity = conn.execute("PRAGMA integrity_check;").fetchall()
                integrity_ok = len(integrity) == 1 and integrity[0] and integrity[0][0] == "ok"
            except sqlite3.DatabaseError as exc:
                issues.append(
                    HealthIssue(
                        severity="corrupt",
                        code="sqlite_integrity_failed",
                        message=f"PRAGMA integrity_check raised: {exc}",
                        detail={"db_path": str(db_path), "error": str(exc)},
                    )
                )
                return False, issues

            if not integrity_ok:
                issues.append(
                    HealthIssue(
                        severity="corrupt",
                        code="sqlite_integrity_failed",
                        message="PRAGMA integrity_check did not return 'ok'",
                        detail={
                            "db_path": str(db_path),
                            "rows": [list(row) for row in integrity[:10]],
                        },
                    )
                )

            return (quick_ok and integrity_ok), issues
    except sqlite3.DatabaseError as exc:
        issues.append(
            HealthIssue(
                severity="corrupt",
                code="sqlite_open_failed",
                message=f"could not open chroma.sqlite3: {exc}",
                detail={"db_path": str(db_path), "error": str(exc)},
            )
        )
        return False, issues
    except OSError as exc:
        issues.append(
            HealthIssue(
                severity="corrupt",
                code="sqlite_open_failed",
                message=f"could not open chroma.sqlite3: {exc}",
                detail={"db_path": str(db_path), "error": str(exc)},
            )
        )
        return False, issues


def _drawer_count_from_sqlite(
    db_path: Path,
) -> tuple[int | None, list[HealthIssue]]:
    """Count distinct embedding_id rows in the drawers collection.

    We deliberately do NOT go through ChromaDB to read this — that would
    instantiate a ``PersistentClient`` and try to mmap HNSW segments,
    which is the very thing we're checking for corruption of.

    Returns ``(count, issues)``. ``count`` is ``None`` on any failure
    (caller can decide whether that's a corruption signal).
    """
    issues: list[HealthIssue] = []
    if not db_path.is_file():
        return None, issues
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5) as conn:
            row = conn.execute(
                """
                SELECT COUNT(DISTINCT e.embedding_id)
                  FROM embeddings AS e
                  JOIN segments AS s ON s.id = e.segment_id
                  JOIN collections AS c ON c.id = s.collection
                 WHERE c.name = ?
                """,
                (DRAWERS_COLLECTION,),
            ).fetchone()
            return (int(row[0]) if row is not None else 0), issues
    except sqlite3.DatabaseError as exc:
        issues.append(
            HealthIssue(
                severity="corrupt",
                code="sqlite_drawer_count_failed",
                message=f"could not read drawer count from sqlite: {exc}",
                detail={"db_path": str(db_path), "error": str(exc)},
            )
        )
        return None, issues
    except OSError as exc:
        issues.append(
            HealthIssue(
                severity="corrupt",
                code="sqlite_drawer_count_failed",
                message=f"could not read drawer count from sqlite: {exc}",
                detail={"db_path": str(db_path), "error": str(exc)},
            )
        )
        return None, issues


def _verbatim_document_count(db_path: Path) -> tuple[int | None, list[HealthIssue]]:
    """Count ``chroma:document`` rows — the actual verbatim text rows.

    A drawer in the ``embeddings`` table that has no matching
    ``embedding_metadata`` row with key ``chroma:document`` would mean
    the metadata row was lost (or was never written) — which manifests
    later as empty search results. A mismatch between this count and the
    drawer count from ``embeddings`` is a corruption signal.
    """
    issues: list[HealthIssue] = []
    if not db_path.is_file():
        return None, issues
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*)
                  FROM embedding_metadata AS m
                  JOIN embeddings AS e ON e.id = m.id
                  JOIN segments AS s ON s.id = e.segment_id
                  JOIN collections AS c ON c.id = s.collection
                 WHERE c.name = ?
                   AND m.key = 'chroma:document'
                """,
                (DRAWERS_COLLECTION,),
            ).fetchone()
            return (int(row[0]) if row is not None else 0), issues
    except sqlite3.DatabaseError as exc:
        issues.append(
            HealthIssue(
                severity="corrupt",
                code="verbatim_count_failed",
                message=f"could not read verbatim document count: {exc}",
                detail={"db_path": str(db_path), "error": str(exc)},
            )
        )
        return None, issues
    except OSError as exc:
        issues.append(
            HealthIssue(
                severity="corrupt",
                code="verbatim_count_failed",
                message=f"could not read verbatim document count: {exc}",
                detail={"db_path": str(db_path), "error": str(exc)},
            )
        )
        return None, issues


def _drawer_count_from_hnsw(palace_path: Path) -> tuple[int | None, list[HealthIssue]]:
    """Open a ChromaDB client and ask the drawers collection for ``count()``.

    This forces ChromaDB to mmap the HNSW segment files; if any segment
    is corrupt the call will raise. We surface the raise as a ``corrupt``
    issue and return ``None`` for the count rather than re-raising — the
    whole point of this module is to be safe to call on broken palaces.

    NOTE: we deliberately skip this when the SQLite layer already says
    "no drawers, ever" (count=0 from sqlite). Otherwise creating the
    collection would write fresh segment files and pollute the disk
    just to satisfy a probe.
    """
    issues: list[HealthIssue] = []

    db_path = palace_path / "chroma.sqlite3"
    if not db_path.is_file():
        return None, issues

    try:
        # Late import: chromadb is heavyweight, only pay the import cost
        # when actually probing.
        import chromadb
    except ImportError as exc:
        issues.append(
            HealthIssue(
                severity="warn",
                code="chromadb_unavailable",
                message=f"chromadb not importable: {exc}",
                detail={"error": str(exc)},
            )
        )
        return None, issues

    try:
        client = chromadb.PersistentClient(path=str(palace_path))
    except Exception as exc:  # broad on purpose — chromadb raises many types
        issues.append(
            HealthIssue(
                severity="corrupt",
                code="hnsw_client_init_failed",
                message=f"PersistentClient could not open palace: {exc}",
                detail={"palace_path": str(palace_path), "error": str(exc)},
            )
        )
        return None, issues

    try:
        try:
            collection = client.get_collection(DRAWERS_COLLECTION)
        except Exception:
            # Drawers collection does not exist — that's not corruption,
            # just an empty palace.
            return 0, issues

        try:
            count = collection.count()
            return int(count), issues
        except Exception as exc:
            issues.append(
                HealthIssue(
                    severity="corrupt",
                    code="hnsw_count_failed",
                    message=f"collection.count() raised: {exc}",
                    detail={"palace_path": str(palace_path), "error": str(exc)},
                )
            )
            return None, issues
    finally:
        # Drop the client so its mmap'd HNSW pages are released before
        # the caller potentially proceeds to quarantine.
        del client


def _check_segment_files(palace_path: Path) -> list[HealthIssue]:
    """Verify each HNSW segment dir has its expected files non-empty.

    ChromaDB will silently treat a zero-length ``data_level0.bin`` as
    "missing index" and rebuild it lazily — but the rebuild loses any
    embeddings that were only in HNSW. Surfacing the empty file as a
    ``corrupt`` issue gives the user a chance to investigate first.

    Some files (``link_lists.bin``, ``index_metadata.pickle``) are
    legitimately empty for small or freshly-compacted indices, and are
    excluded from the empty-file check — see ``_HNSW_FILES_THAT_MAY_BE_EMPTY``.
    """
    issues: list[HealthIssue] = []
    for segment_dir in _iter_segment_dirs(palace_path):
        for child in segment_dir.iterdir():
            if child.name not in _HNSW_SEGMENT_FILES:
                continue
            try:
                size = child.stat().st_size
            except OSError as exc:
                issues.append(
                    HealthIssue(
                        severity="corrupt",
                        code="hnsw_segment_unreadable",
                        message=f"could not stat {child.name}: {exc}",
                        detail={"file": str(child), "error": str(exc)},
                    )
                )
                continue
            if size == 0 and child.name not in _HNSW_FILES_THAT_MAY_BE_EMPTY:
                issues.append(
                    HealthIssue(
                        severity="corrupt",
                        code="hnsw_segment_empty",
                        message=f"HNSW segment file is zero bytes: {child}",
                        detail={"file": str(child)},
                    )
                )
    return issues


def _check_orphan_locks(palace_path: Path) -> list[HealthIssue]:
    """Warn about palace_write_*.lock files older than 1 hour.

    ``palace_write_lock`` uses ``fcntl.flock`` / ``msvcrt.locking`` which
    auto-release on process exit, so the lock FILES being old isn't itself
    a problem — but it's a useful smoke-signal that a writer crashed mid-
    operation and the user might want to investigate.
    """
    import hashlib

    issues: list[HealthIssue] = []
    lock_dir = Path(os.path.expanduser("~")) / ".mempalace" / "locks"
    if not lock_dir.is_dir():
        return issues

    try:
        resolved = str(palace_path.resolve())
    except OSError:
        resolved = str(palace_path)
    palace_hash = hashlib.sha256(resolved.encode()).hexdigest()[:16]
    expected_lock = lock_dir / f"palace_write_{palace_hash}.lock"

    if not expected_lock.is_file():
        return issues

    try:
        age = time.time() - expected_lock.stat().st_mtime
    except OSError:
        return issues

    if age > _ORPHAN_LOCK_AGE_SECONDS:
        issues.append(
            HealthIssue(
                severity="warn",
                code="orphan_lock",
                message=(
                    f"palace_write lock file is {age / 3600:.1f}h old — possible "
                    f"crashed writer (file: {expected_lock})"
                ),
                detail={"lock_file": str(expected_lock), "age_seconds": age},
            )
        )
    return issues


# ── Public surface ────────────────────────────────────────────────────


def check_palace_health(palace_path: str | Path) -> HealthReport:
    """Run all non-destructive diagnostics on ``palace_path``.

    Never raises on corruption. Raises ``ValueError`` only when the
    caller passes a path that doesn't exist on disk at all (programmer
    error, not a corruption signal).
    """
    palace_path_p = _normalise_palace_path(palace_path)
    if not palace_path_p.exists():
        raise ValueError(f"palace path does not exist: {palace_path_p}")

    issues: list[HealthIssue] = []

    db_path = palace_path_p / "chroma.sqlite3"

    if not db_path.is_file():
        # An empty palace dir (no chroma.sqlite3) isn't broken, just unused.
        # We still surface this as ``info`` so the doctor command can
        # explain what's going on.
        issues.append(
            HealthIssue(
                severity="info",
                code="palace_uninitialised",
                message=f"no chroma.sqlite3 in {palace_path_p} (palace not yet created)",
                detail={"palace_path": str(palace_path_p)},
            )
        )
        report = HealthReport(
            status=_highest_severity(issues),
            palace_path=palace_path_p,
            drawer_count_sqlite=None,
            drawer_count_hnsw=None,
            drawer_count_verbatim=None,
            sqlite_integrity_ok=False,
            issues=issues,
            checked_at=datetime.now(),
        )
        return report

    sqlite_ok, integrity_issues = _check_sqlite_integrity(db_path)
    issues.extend(integrity_issues)

    sqlite_count, sqlite_count_issues = _drawer_count_from_sqlite(db_path)
    issues.extend(sqlite_count_issues)

    verbatim_count, verbatim_count_issues = _verbatim_document_count(db_path)
    issues.extend(verbatim_count_issues)

    # If SQLite says zero drawers, opening the chromadb client just to
    # confirm "yes, zero" would create writeable state in the palace.
    # Skip the HNSW probe in that case.
    if sqlite_count is None or sqlite_count > 0:
        hnsw_count, hnsw_issues = _drawer_count_from_hnsw(palace_path_p)
        issues.extend(hnsw_issues)
    else:
        hnsw_count = 0

    issues.extend(_check_segment_files(palace_path_p))
    issues.extend(_check_orphan_locks(palace_path_p))

    # Cross-layer count consistency check. Only meaningful when both
    # counts were obtainable.
    if sqlite_count is not None and verbatim_count is not None:
        if sqlite_count != verbatim_count:
            issues.append(
                HealthIssue(
                    severity="corrupt",
                    code="drawer_verbatim_mismatch",
                    message=(
                        f"sqlite drawer count ({sqlite_count}) does not match "
                        f"verbatim document count ({verbatim_count})"
                    ),
                    detail={
                        "drawer_count_sqlite": sqlite_count,
                        "drawer_count_verbatim": verbatim_count,
                    },
                )
            )
    if sqlite_count is not None and hnsw_count is not None and sqlite_count != hnsw_count:
        issues.append(
            HealthIssue(
                severity="corrupt",
                code="drawer_hnsw_mismatch",
                message=(
                    f"sqlite drawer count ({sqlite_count}) does not match HNSW count ({hnsw_count})"
                ),
                detail={
                    "drawer_count_sqlite": sqlite_count,
                    "drawer_count_hnsw": hnsw_count,
                },
            )
        )

    return HealthReport(
        status=_highest_severity(issues),
        palace_path=palace_path_p,
        drawer_count_sqlite=sqlite_count,
        drawer_count_hnsw=hnsw_count,
        drawer_count_verbatim=verbatim_count,
        sqlite_integrity_ok=sqlite_ok,
        issues=issues,
        checked_at=datetime.now(),
    )


def quarantine_corrupt_segments(
    palace_path: str | Path,
    *,
    reason: str = "manual",
) -> Path:
    """Move HNSW segment dirs out of the palace into a timestamped quarantine.

    ``chroma.sqlite3`` is NOT moved — it stays inside the palace, because
    it holds the verbatim document text that is the source of truth for
    rebuilds. Only the HNSW index state is quarantined.

    Returns the directory we moved things INTO (always created, even when
    no segment dirs were found, so the caller has somewhere to drop a
    ``manifest.txt`` describing the action).

    Caller responsibility: the caller MUST close any ChromaDB clients
    against this palace BEFORE invoking this function. We deliberately
    do not acquire ``palace_write_lock`` here — by definition this is
    called when the palace is suspected unhealthy, and taking the write
    lock could deadlock against the corrupted state we're trying to
    clean up. Layer 1 + Layer 2 protect normal write paths; this is the
    forensic escape hatch.
    """
    palace_path_p = _normalise_palace_path(palace_path)
    if not palace_path_p.exists():
        raise ValueError(f"palace path does not exist: {palace_path_p}")

    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    target_root = _quarantine_root() / _palace_id(palace_path_p) / timestamp
    target_root.mkdir(parents=True, exist_ok=True)

    moved: list[Path] = []
    for segment_dir in list(_iter_segment_dirs(palace_path_p)):
        destination = target_root / segment_dir.name
        try:
            shutil.move(str(segment_dir), str(destination))
            moved.append(destination)
            logger.warning(
                "quarantined HNSW segment %s -> %s (reason=%s)",
                segment_dir,
                destination,
                reason,
            )
        except OSError:
            logger.exception(
                "failed to quarantine segment %s -> %s",
                segment_dir,
                destination,
            )

    # Drop a manifest so the user knows what was moved and why, without
    # having to grep logs. Append-friendly format in case quarantine is
    # called multiple times in the same second (very unlikely but cheap).
    manifest = target_root / "manifest.txt"
    try:
        with manifest.open("a", encoding="utf-8") as fh:
            fh.write(f"# quarantined at {datetime.now().isoformat()}\n")
            fh.write(f"# reason: {reason}\n")
            fh.write(f"# source palace: {palace_path_p}\n")
            for path in moved:
                fh.write(f"{path}\n")
            if not moved:
                fh.write("# (no segment directories found in source palace)\n")
            fh.write("\n")
    except OSError:
        logger.exception("could not write quarantine manifest %s", manifest)

    return target_root
