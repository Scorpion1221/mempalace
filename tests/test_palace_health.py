"""Tests for ``mempalace.health`` (Layer 4 of the concurrency fix).

These exercise non-destructive diagnostics, safe quarantine, and the
``rebuild_from_verbatim`` recovery path. Most tests use real ChromaDB
clients against on-disk palace directories so we know the code paths
are exercised end-to-end (rather than just against ``MagicMock`` shapes
that may not match production reality).
"""

from __future__ import annotations

import multiprocessing as mp
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import chromadb
import pytest

from mempalace.health import (
    HealthIssue,
    HealthReport,
    check_palace_health,
    quarantine_corrupt_segments,
)
from mempalace.repair import RebuildReport, rebuild_from_verbatim


# ── Helpers ────────────────────────────────────────────────────────────


def _seed_palace(palace_path: Path, *, drawers: int = 3) -> None:
    """Build a real ChromaDB palace with ``drawers`` mempalace drawers in it.

    Mirrors the on-disk layout produced by ``miner.py`` so health checks
    see realistic file shapes.
    """
    palace_path.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(palace_path))
    col = client.get_or_create_collection("mempalace_drawers", metadata={"hnsw:space": "cosine"})
    if drawers:
        col.upsert(
            ids=[f"drawer_test_room_{i:03d}" for i in range(drawers)],
            documents=[
                f"Verbatim drawer body {i} — never paraphrase, never summarise."
                for i in range(drawers)
            ],
            metadatas=[
                {
                    "wing": "test",
                    "room": "room",
                    "source_file": f"/tmp/source_{i}.md",
                    "chunk_index": 0,
                    "added_by": "test",
                    "filed_at": "2026-04-28T12:00:00",
                }
                for i in range(drawers)
            ],
        )
    # Drop strong refs so the chromadb mmap is released — important for
    # tests that subsequently quarantine or delete files.
    del col
    del client


def _list_segment_dirs(palace_path: Path) -> list[Path]:
    """Return the HNSW segment directories visible inside ``palace_path``."""
    return [
        p
        for p in palace_path.iterdir()
        if p.is_dir()
        and "-" in p.name
        and not p.name.startswith((".", "_"))
        and ".drift-" not in p.name
        and ".quarantine-" not in p.name
    ]


# ── 1. clean palace -> ok ──────────────────────────────────────────────


def test_health_report_ok_for_clean_palace(tmp_path):
    palace = tmp_path / "clean_palace"
    _seed_palace(palace, drawers=4)

    report = check_palace_health(palace)

    assert isinstance(report, HealthReport)
    assert report.status == "ok", f"unexpected issues: {report.issues}"
    assert report.sqlite_integrity_ok is True
    assert report.drawer_count_sqlite == 4
    assert report.drawer_count_verbatim == 4
    assert report.drawer_count_hnsw == 4
    # Every issue should be info or absent — never warn/corrupt for a clean palace.
    assert all(i.severity == "info" for i in report.issues)


# ── 2. count mismatch -> corrupt ──────────────────────────────────────


def test_health_detects_count_mismatch(tmp_path):
    palace = tmp_path / "mismatched_palace"
    _seed_palace(palace, drawers=3)

    # Inject a verbatim row mismatch by deleting one ``chroma:document``
    # row. The drawer still exists in the embeddings table but its
    # verbatim text is gone — this is exactly the corruption shape that
    # a torn write can produce.
    db_path = palace / "chroma.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            DELETE FROM embedding_metadata
             WHERE id = (
                SELECT id FROM embedding_metadata
                 WHERE key = 'chroma:document'
                 LIMIT 1
             )
               AND key = 'chroma:document'
            """
        )
        conn.commit()

    report = check_palace_health(palace)

    assert report.status == "corrupt"
    codes = {i.code for i in report.issues}
    assert "drawer_verbatim_mismatch" in codes


# ── 3. SQLite garbage -> integrity false ──────────────────────────────


def test_health_detects_sqlite_integrity_failure(tmp_path):
    palace = tmp_path / "broken_sqlite_palace"
    _seed_palace(palace, drawers=2)

    # Overwrite chroma.sqlite3 with random bytes. ``PRAGMA quick_check``
    # cannot succeed against a non-SQLite file.
    db_path = palace / "chroma.sqlite3"
    db_path.write_bytes(b"\x00\xffNOT A SQLITE FILE\x00" * 500)

    report = check_palace_health(palace)

    assert report.status == "corrupt"
    assert report.sqlite_integrity_ok is False
    codes = {i.code for i in report.issues}
    # We accept any of the SQLite-specific corruption codes — the exact
    # one depends on whether sqlite refuses to open at all (sqlite_open_failed)
    # or opens but fails the integrity pragmas.
    assert codes & {
        "sqlite_open_failed",
        "sqlite_quick_check_failed",
        "sqlite_integrity_failed",
        "sqlite_drawer_count_failed",
    }


# ── 4. never raise on broken input ────────────────────────────────────


def test_health_never_raises_on_corruption(tmp_path):
    palace = tmp_path / "very_broken_palace"
    _seed_palace(palace, drawers=2)

    # Construct as much damage as we can: zero-length segment files,
    # truncated SQLite, and a stray segment dir with garbage.
    db_path = palace / "chroma.sqlite3"
    db_path.write_bytes(b"x" * 16)  # truncated, definitely not valid SQLite

    for seg in _list_segment_dirs(palace):
        for f in seg.iterdir():
            try:
                f.write_bytes(b"")
            except OSError:
                pass

    bogus_dir = palace / "deadbeef-cafe-0000-0000-000000000000"
    bogus_dir.mkdir()
    (bogus_dir / "data_level0.bin").write_bytes(b"")

    # Must not raise.
    report = check_palace_health(palace)
    assert isinstance(report, HealthReport)
    # Whatever the count layer returns is fine — the contract is "no raise".
    assert report.status in ("warn", "corrupt", "ok")


def test_health_raises_only_on_missing_palace(tmp_path):
    """Programmer error (path doesn't exist) is the ONE thing we raise on."""
    with pytest.raises(ValueError):
        check_palace_health(tmp_path / "does-not-exist")


# ── 5. quarantine moves files, never deletes ──────────────────────────


def test_quarantine_moves_files_does_not_delete(tmp_path, monkeypatch):
    palace = tmp_path / "to_quarantine"
    _seed_palace(palace, drawers=2)

    # Snapshot the segment files before quarantine — content + sizes.
    segments_before = _list_segment_dirs(palace)
    assert segments_before, "test setup: palace has no segment dirs"
    file_snapshot: dict[str, bytes] = {}
    for seg in segments_before:
        for f in seg.iterdir():
            try:
                file_snapshot[f.name] = f.read_bytes()
            except OSError:
                pass
    assert file_snapshot, "test setup: no files captured"

    # Redirect the quarantine root to live inside tmp_path so the test
    # does not pollute ~/.mempalace.
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    target = quarantine_corrupt_segments(palace, reason="unit-test")

    # Source palace no longer has the segment dirs.
    assert _list_segment_dirs(palace) == []
    # Target dir was created and contains the moved segments.
    assert target.is_dir()
    moved_files = {p.name: p.read_bytes() for p in target.rglob("*") if p.is_file()}
    # Manifest is emitted alongside the moved segments.
    assert "manifest.txt" in moved_files
    # Every original segment file has its bytes preserved verbatim under target.
    for name, body in file_snapshot.items():
        assert name in moved_files, f"segment file {name} was not moved"
        assert moved_files[name] == body, f"segment file {name} content changed"


# ── 6. quarantine is idempotent across calls ──────────────────────────


def test_quarantine_idempotent(tmp_path, monkeypatch):
    palace = tmp_path / "twice_quarantined"
    _seed_palace(palace, drawers=2)

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    first = quarantine_corrupt_segments(palace, reason="round-1")
    # Re-seed so there's something to quarantine the second time.
    _seed_palace(palace, drawers=2)
    # Force the timestamp to differ — both calls happen in the same second
    # otherwise (the path uses second-precision YYYYmmddTHHMMSS).
    time.sleep(1.1)
    second = quarantine_corrupt_segments(palace, reason="round-2")

    assert first != second
    assert first.is_dir()
    assert second.is_dir()
    # Both subdirs live under the same per-palace folder.
    assert first.parent == second.parent


# ── 7. rebuild_from_verbatim restores drawer count ────────────────────


def test_rebuild_from_verbatim_restores_drawer_count(tmp_path, monkeypatch):
    palace = tmp_path / "rebuildable"
    _seed_palace(palace, drawers=5)

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    # Pre-quarantine the index so the rebuild has to recreate everything.
    quarantine_corrupt_segments(palace, reason="pre-rebuild")
    assert _list_segment_dirs(palace) == []

    report = rebuild_from_verbatim(palace)
    assert isinstance(report, RebuildReport)
    assert report.drawers_processed == 5
    assert report.drawers_failed == 0

    # Verify the rebuilt palace can be opened cleanly and has 5 drawers.
    client = chromadb.PersistentClient(path=str(palace))
    col = client.get_collection("mempalace_drawers")
    assert col.count() == 5
    rows = col.get()
    # Every original verbatim string survived the round-trip.
    for doc in rows["documents"]:
        assert "Verbatim drawer body" in doc
        assert "never paraphrase" in doc
    del col
    del client


# ── 8. rebuild is idempotent (twice in a row is safe) ─────────────────


def test_rebuild_idempotent(tmp_path, monkeypatch):
    palace = tmp_path / "rebuild_twice"
    _seed_palace(palace, drawers=3)

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    first = rebuild_from_verbatim(palace)
    assert first.drawers_processed == 3
    assert first.drawers_failed == 0

    # Second run quarantines the freshly-built segments and rebuilds again.
    # Drawer count must not change.
    second = rebuild_from_verbatim(palace)
    assert second.drawers_processed == 3
    assert second.drawers_failed == 0

    client = chromadb.PersistentClient(path=str(palace))
    col = client.get_collection("mempalace_drawers")
    assert col.count() == 3
    del col
    del client


def test_rebuild_from_verbatim_raises_on_missing_sqlite(tmp_path):
    palace = tmp_path / "no_sqlite"
    palace.mkdir()
    with pytest.raises(ValueError):
        rebuild_from_verbatim(palace)


def test_rebuild_from_verbatim_raises_on_missing_palace(tmp_path):
    with pytest.raises(FileNotFoundError):
        rebuild_from_verbatim(tmp_path / "does-not-exist")


def test_rebuild_from_verbatim_uses_chroma_collection_adapter(tmp_path, monkeypatch):
    """``rebuild_from_verbatim`` must write through the ``ChromaCollection``
    adapter, not the raw chromadb collection.

    Why this matters: writing through the adapter calls
    ``ChromaBackend._note_post_write`` after every upsert. Without that
    hook, the next ``_client_for_write`` call from the same process sees
    the on-disk stat tuple change against its cached copy and rebuilds
    the chromadb client unnecessarily — a real cost path documented in
    commit cb6c483 that this test guards against regressing for the
    rebuild path. It also keeps the rebuild on the same
    ``_validate_where`` / embeddings handling the rest of the codebase
    uses.

    Strategy: spy on ``ChromaBackend._note_post_write`` and assert it
    fires at least once per upsert during the rebuild. We use a real
    palace + real ChromaDB so the wiring is verified end-to-end (raw
    collection upserts would never reach the spy).
    """
    palace = tmp_path / "rebuild_adapter"
    _seed_palace(palace, drawers=4)

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    # Pre-quarantine so the rebuild has to do real upserts (not skip them).
    quarantine_corrupt_segments(palace, reason="adapter-test")
    assert _list_segment_dirs(palace) == []

    # Spy on ChromaBackend._note_post_write — this is the bypass surface
    # that the raw-collection bug used to hit.
    from mempalace.backends.chroma import ChromaBackend

    call_log: list[str] = []
    original = ChromaBackend._note_post_write

    def spy(self, palace_path: str) -> None:
        call_log.append(str(palace_path))
        original(self, palace_path)

    monkeypatch.setattr(ChromaBackend, "_note_post_write", spy)

    report = rebuild_from_verbatim(palace)
    assert report.drawers_processed == 4
    assert report.drawers_failed == 0

    # The adapter MUST have called _note_post_write at least once during
    # the rebuild — proving the upsert flowed through ChromaCollection
    # rather than the raw chromadb collection.
    assert call_log, (
        "_note_post_write was never called during rebuild_from_verbatim — "
        "the rebuild path is bypassing the ChromaCollection adapter"
    )
    # Every recorded call should target the palace under test (no stray
    # writes to other paths). String-resolve for symlink-safety.
    resolved = str(palace.resolve())
    for path in call_log:
        assert path == resolved, f"unexpected post-write target {path!r}, expected {resolved!r}"


# ── 9. doctor CLI exit codes ──────────────────────────────────────────


def _run_cli(args: list[str], env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """Invoke the mempalace CLI in a real subprocess.

    Subprocess (rather than calling main() in-process) so that argparse
    + sys.exit behave exactly as a user would see them, including the
    process exit code.
    """
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "mempalace", *args],
        capture_output=True,
        text=True,
        env=env,
    )


def test_doctor_command_exit_codes(tmp_path):
    # Clean palace -> exit 0
    clean = tmp_path / "doctor_ok"
    _seed_palace(clean, drawers=2)
    proc = _run_cli(["--palace", str(clean), "doctor"])
    assert proc.returncode == 0, (
        f"clean palace doctor failed: stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    assert "Status:" in proc.stdout
    assert "OK" in proc.stdout

    # Corrupt SQLite -> exit 2
    broken = tmp_path / "doctor_broken"
    _seed_palace(broken, drawers=2)
    (broken / "chroma.sqlite3").write_bytes(b"NOT-A-DB" * 100)
    proc = _run_cli(["--palace", str(broken), "doctor"])
    assert proc.returncode == 2, (
        f"broken palace doctor wrong code: stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )

    # Path that simply does not exist -> exit 2 (the CLI guards on isdir).
    proc = _run_cli(["--palace", str(tmp_path / "does-not-exist-anywhere"), "doctor"])
    assert proc.returncode == 2


# ── 10. repair holds palace_write_lock ────────────────────────────────


def _hold_palace_lock_worker(palace_path: str, ready_path: str, release_path: str) -> None:
    """Subprocess: acquire palace_write_lock and hold until told to release."""
    from mempalace.palace import palace_write_lock

    with palace_write_lock(palace_path, timeout=10.0):
        Path(ready_path).touch()
        deadline = time.monotonic() + 30.0
        while not Path(release_path).exists() and time.monotonic() < deadline:
            time.sleep(0.05)


_MP_CTX = mp.get_context("spawn")


def test_repair_command_uses_palace_write_lock(tmp_path):
    """rebuild_from_verbatim must wait for the palace_write_lock.

    Strategy: hold the lock in a child process, then attempt a rebuild
    with a tight timeout and assert it raises ``PalaceWriteLockTimeout``.
    """
    from mempalace.palace import PalaceWriteLockTimeout

    palace = tmp_path / "lock_contended"
    _seed_palace(palace, drawers=2)

    ready = tmp_path / "lock_ready"
    release = tmp_path / "lock_release"

    holder = _MP_CTX.Process(
        target=_hold_palace_lock_worker,
        args=(str(palace), str(ready), str(release)),
    )
    holder.start()
    try:
        # Wait for child to acquire the lock.
        deadline = time.monotonic() + 5.0
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists(), "child failed to acquire lock"

        # Patch the timeout so this test doesn't run for 5 minutes.
        import mempalace.repair as repair_mod

        original = repair_mod._REBUILD_LOCK_TIMEOUT_S
        repair_mod._REBUILD_LOCK_TIMEOUT_S = 0.5
        try:
            with pytest.raises(PalaceWriteLockTimeout):
                rebuild_from_verbatim(palace, quarantine_first=False)
        finally:
            repair_mod._REBUILD_LOCK_TIMEOUT_S = original
    finally:
        release.touch()
        holder.join(timeout=5.0)
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=2.0)


# ── Bonus: HealthIssue is a hashable frozen dataclass ─────────────────


def test_health_issue_is_immutable():
    issue = HealthIssue(severity="warn", code="x", message="y")
    with pytest.raises(Exception):
        issue.severity = "corrupt"  # type: ignore[misc]
