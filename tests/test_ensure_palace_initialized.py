"""tests/test_ensure_palace_initialized.py — H-1 first-open race fix.

Covers ``mempalace.palace.ensure_palace_initialized``: the idempotent
bootstrap helper that serializes ChromaDB's ``CREATE TABLE`` race when N
processes open a brand-new palace simultaneously.

The cross-process race itself is exercised at the
``@pytest.mark.stress`` test in ``tests/test_palace_concurrent_stress.py``
(``test_concurrent_first_open_does_not_race``). The tests in this file
focus on the helper's contract: fast path, slow path, idempotency, and
graceful behaviour when the lock times out.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from mempalace.palace import (
    PalaceWriteLockTimeout,
    ensure_palace_initialized,
    palace_write_lock,
)


# ── Shared spawn context — chromadb + fcntl state must not leak. ──
_MP_CTX = mp.get_context("spawn")


# ── Subprocess targets (top-level so spawn can pickle them). ──


def _bootstrap_worker(palace_path: str, result_path: str) -> None:
    """Subprocess helper: call ensure_palace_initialized + one tiny write."""
    outcome: dict = {"pid": os.getpid()}
    try:
        from mempalace.backends.chroma import ChromaBackend
        from mempalace.palace import (
            ensure_palace_initialized as _bootstrap,
            palace_write_lock as _lock,
        )

        _bootstrap(palace_path, timeout=60.0)

        backend = ChromaBackend()
        col = backend.get_collection(palace_path, "mempalace_drawers", create=True)
        with _lock(palace_path, timeout=30.0):
            col.refresh_for_write()
            col.upsert(
                ids=[f"bootstrap_{os.getpid()}"],
                documents=[f"bootstrap drawer from pid {os.getpid()}"],
                metadatas=[
                    {
                        "wing": "bootstrap_test",
                        "room": "first_open",
                        "added_by": "ensure_palace_initialized_test",
                    }
                ],
                embeddings=[[0.1] * 384],
            )
        outcome["status"] = "ok"
    except Exception as exc:  # pragma: no cover — defensive
        outcome["status"] = "error"
        outcome["error_type"] = type(exc).__name__
        outcome["error_message"] = str(exc)
    Path(result_path).write_text(json.dumps(outcome))


def _hold_lock_until_release(palace_path: str, ready_path: str, release_path: str) -> None:
    """Hold ``palace_write_lock`` until the parent touches ``release_path``."""
    import time

    with palace_write_lock(palace_path, timeout=10.0):
        Path(ready_path).touch()
        deadline = time.monotonic() + 30.0
        while not Path(release_path).exists() and time.monotonic() < deadline:
            time.sleep(0.05)


# ── Tests ─────────────────────────────────────────────────────────────


def test_fast_path_when_sqlite_already_exists(tmp_path):
    """Pre-create chroma.sqlite3 — helper must NOT take the palace_write_lock."""
    palace = tmp_path / "palace"
    palace.mkdir()
    sqlite_path = palace / "chroma.sqlite3"
    # Any non-zero bytes count as "schema exists" for the fast path. The
    # helper never opens the file, so its actual contents don't matter.
    sqlite_path.write_bytes(b"sqlite-fake-bytes")

    with patch(
        "mempalace.palace.palace_write_lock",
        side_effect=AssertionError("palace_write_lock must not be acquired on the fast path"),
    ) as mocked_lock:
        ensure_palace_initialized(palace)
        assert mocked_lock.call_count == 0, (
            "fast path took the lock when sqlite3 was already present"
        )


def test_slow_path_creates_schema(tmp_path):
    """Fresh dir — helper creates chroma.sqlite3."""
    palace = tmp_path / "fresh_palace"
    # Note: we do NOT mkdir; the helper must handle non-existent dirs.

    sqlite_path = palace / "chroma.sqlite3"
    assert not sqlite_path.exists()

    ensure_palace_initialized(palace)

    assert sqlite_path.exists(), "expected chroma.sqlite3 to be created"
    assert sqlite_path.stat().st_size > 0, "schema should be initialized (non-empty file)"


def test_idempotent_within_process(tmp_path):
    """Two consecutive calls succeed; the second hits the fast path."""
    palace = tmp_path / "palace"

    # First call — slow path, creates the schema.
    ensure_palace_initialized(palace)
    sqlite_path = palace / "chroma.sqlite3"
    assert sqlite_path.exists()

    # Second call — must be the fast path. Patch the lock and assert it's
    # never acquired.
    with patch(
        "mempalace.palace.palace_write_lock",
        side_effect=AssertionError("second call must not take the palace_write_lock"),
    ) as mocked_lock:
        ensure_palace_initialized(palace)
        assert mocked_lock.call_count == 0


def test_concurrent_callers_only_one_creates(tmp_path):
    """6 subprocesses bootstrap a brand-new palace — none crash."""
    palace = tmp_path / "concurrent_palace"
    n_workers = 6

    procs: list = []
    result_paths: list = []
    for worker_id in range(n_workers):
        rp = tmp_path / f"bootstrap_{worker_id}.json"
        result_paths.append(rp)
        proc = _MP_CTX.Process(
            target=_bootstrap_worker,
            args=(str(palace), str(rp)),
        )
        proc.start()
        procs.append(proc)

    for p in procs:
        p.join(timeout=120.0)

    for i, p in enumerate(procs):
        assert not p.is_alive(), f"worker {i} hung"
        assert p.exitcode == 0, f"worker {i} exit code = {p.exitcode}"

    # Every worker must have completed without error and the palace must
    # be usable (each worker also does one tiny write).
    for i, rp in enumerate(result_paths):
        assert rp.exists(), f"worker {i} produced no result"
        result = json.loads(rp.read_text())
        assert result.get("status") == "ok", (
            f"worker {i} did not succeed: {result.get('error_type')}: {result.get('error_message')}"
        )

    # Palace must contain the 6 bootstrap writes — proves it is fully
    # functional after concurrent first-open.
    from mempalace.palace import get_collection

    col = get_collection(str(palace), create=False)
    assert col.count() >= n_workers, (
        f"expected >= {n_workers} drawers after bootstrap, got {col.count()}"
    )


def test_handles_existing_palace_with_zero_byte_sqlite(tmp_path):
    """Edge case: chroma.sqlite3 exists but is empty — helper must bootstrap.

    Models a previous bootstrap that crashed mid-init and left a
    zero-byte sqlite file. The fast-path size check filters these out, so
    the slow path runs and produces a real schema.
    """
    palace = tmp_path / "palace"
    palace.mkdir()
    sqlite_path = palace / "chroma.sqlite3"
    sqlite_path.touch()  # zero bytes
    assert sqlite_path.stat().st_size == 0

    ensure_palace_initialized(palace)

    assert sqlite_path.exists()
    assert sqlite_path.stat().st_size > 0, (
        "zero-byte sqlite must trigger the slow path and produce a real schema"
    )


def test_propagates_palace_write_lock_timeout(tmp_path):
    """When the lock cannot be acquired, raise PalaceWriteLockTimeout."""
    palace = tmp_path / "palace"
    palace.mkdir()
    # Ensure the fast path does NOT skip the lock — leave sqlite absent.

    ready = tmp_path / "ready"
    release = tmp_path / "release"

    holder = _MP_CTX.Process(
        target=_hold_lock_until_release,
        args=(str(palace), str(ready), str(release)),
    )
    holder.start()
    try:
        # Wait for the holder to actually hold the lock.
        deadline_s = 10.0
        import time

        deadline = time.monotonic() + deadline_s
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "lock holder failed to acquire"

        # With the lock held, ensure_palace_initialized must time out
        # instead of blocking forever.
        with pytest.raises(PalaceWriteLockTimeout):
            ensure_palace_initialized(palace, timeout=0.5)
    finally:
        release.touch()
        holder.join(timeout=10.0)
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=2.0)
