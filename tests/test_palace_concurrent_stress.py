"""tests/test_palace_concurrent_stress.py — Wave 3 cross-process stress tests.

Reproduces the production failure mode described in the original bug report:

    6 个 Cursor chat 各起一个 mempalace-mcp server 进程
    每次任意 chat 的 stop hook 触发，都会 fork 一个 mempalace mine 子进程或
    _async_save_worker 子进程
    这些进程全部独立打开 ~/.mempalace/palace/chroma.sqlite3 写入
    ChromaDB 的 HNSW writer 不是 MVCC、不支持多进程并发写

The fix lives in the Wave-1 + Wave-2 stack (palace_write_lock,
ChromaCollection.refresh_for_write, recovery_wal). These tests prove the
fix actually holds under realistic OS-level concurrency.

Cross-process is mandatory — fcntl.flock is a per-process advisory lock,
so threading would not exercise the cross-process behaviour we care about.
Every test below uses ``multiprocessing`` with the ``spawn`` context
(the default on macOS, and forced on Linux for parity) or
``subprocess.Popen`` for SIGKILL semantics.

Tests marked ``@pytest.mark.stress`` may be slow (multi-second
multiprocessing fan-outs that include real chromadb HNSW writes).
The pyproject default ``-m 'not benchmark and not slow and not stress'``
EXCLUDES them from a normal ``pytest`` run. To run them:

    pytest -m stress -o addopts="" tests/test_palace_concurrent_stress.py

(``-o addopts=""`` overrides the default exclusion clause from
pyproject.toml so the stress marker actually selects.)
"""

from __future__ import annotations

import hashlib
import json
import multiprocessing as mp
import os
import random
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mempalace.health import check_palace_health
from mempalace.palace import PalaceWriteLockTimeout, palace_write_lock
from mempalace.recovery_wal import recovery_dir_for_palace


# ── Shared multiprocessing context ────────────────────────────────────
# ``spawn`` re-imports the module in the child so chromadb / fcntl state
# never leaks across the fork boundary. macOS defaults to ``spawn`` on
# 3.8+; we force it on Linux too for parity.
_MP_CTX = mp.get_context("spawn")


# ── Subprocess targets ────────────────────────────────────────────────
# Top-level functions only — multiprocessing.spawn cannot pickle inner
# functions or lambdas. Each one writes its outcome to a per-worker file
# so the parent can inspect crashes after join().


def _writer_loop(
    palace_path: str,
    worker_id: int,
    n_writes: int,
    op_mix: list,
    result_path: str,
    lock_timeout: float = 60.0,
) -> None:
    """One stress writer subprocess.

    op_mix is a list of strings from {"add_drawer", "diary", "tunnel"}; the
    worker cycles through them so every subprocess exercises a mix of
    write paths instead of just one. Returns its outcome by writing JSON
    to ``result_path``.

    We pass pre-computed embeddings (zero vectors of the right
    dimensionality) instead of relying on the auto-embed path, so workers
    don't have to download the all-MiniLM-L6-v2 ONNX model the first
    time they run. The lock contract is what we're testing — the
    embedding pipeline is irrelevant.
    """
    written_ids: list[str] = []
    errors: list[str] = []
    fixed_embedding = [0.1] * 384  # ChromaDB default model dims
    try:
        # Imports inside the worker so the spawned child does its own
        # one-time module init — avoids races on shared module state.
        from mempalace.backends.chroma import ChromaBackend

        backend = ChromaBackend()
        col = backend.get_collection(palace_path, "mempalace_drawers", create=True)

        for i in range(n_writes):
            op = op_mix[i % len(op_mix)]
            drawer_id = f"stress_w{worker_id}_n{i}"
            try:
                if op == "add_drawer":
                    # Mirror the mcp_server.tool_add_drawer critical
                    # section (lock + refresh + upsert).
                    with palace_write_lock(palace_path, timeout=lock_timeout):
                        col.refresh_for_write()
                        col.upsert(
                            ids=[drawer_id],
                            documents=[
                                f"stress drawer from worker {worker_id} write {i} "
                                "with enough words to exercise the embedding"
                            ],
                            metadatas=[
                                {
                                    "wing": f"stress_w{worker_id}",
                                    "room": "concurrent",
                                    "added_by": "stress_test",
                                    "worker_id": worker_id,
                                    "write_index": i,
                                }
                            ],
                            embeddings=[fixed_embedding],
                        )
                    written_ids.append(drawer_id)
                elif op == "diary":
                    with palace_write_lock(palace_path, timeout=lock_timeout):
                        col.refresh_for_write()
                        col.add(
                            ids=[drawer_id],
                            documents=[f"diary entry worker {worker_id} write {i}"],
                            metadatas=[
                                {
                                    "wing": f"stress_w{worker_id}",
                                    "room": "diary",
                                    "added_by": "stress_diary",
                                }
                            ],
                            embeddings=[fixed_embedding],
                        )
                    written_ids.append(drawer_id)
                elif op == "tunnel":
                    # Use upsert (idempotent) to simulate a tunnel-style
                    # write that may collide with a sibling worker's id.
                    with palace_write_lock(palace_path, timeout=lock_timeout):
                        col.refresh_for_write()
                        col.upsert(
                            ids=[drawer_id],
                            documents=[f"tunnel-style write {worker_id}/{i}"],
                            metadatas=[
                                {
                                    "wing": "shared_tunnel_wing",
                                    "room": "shared_room",
                                    "added_by": "stress_tunnel",
                                }
                            ],
                            embeddings=[fixed_embedding],
                        )
                    written_ids.append(drawer_id)
            except PalaceWriteLockTimeout:
                # 60s budget against the other writers — should not happen
                # for legitimate writes, but record it instead of crashing.
                errors.append(f"timeout op={op} i={i}")
            except Exception as exc:
                errors.append(f"{type(exc).__name__} op={op} i={i}: {exc}")
    finally:
        Path(result_path).write_text(
            json.dumps(
                {
                    "worker_id": worker_id,
                    "written_ids": written_ids,
                    "errors": errors,
                    "pid": os.getpid(),
                }
            )
        )


def _writer_unique_ids(
    palace_path: str,
    worker_id: int,
    n_writes: int,
    result_path: str,
    lock_timeout: float = 30.0,
    max_retries: int = 5,
) -> None:
    """Like _writer_loop but each id is globally unique across workers.

    Used by ``test_no_data_loss_under_contention`` — the assertion is that
    every emitted id is queryable after all workers finish, with zero
    duplicates.

    The "no data loss" contract reflects production behaviour: a writer
    that times out on the lock retries (in production: the next mine run
    or next save tick) instead of dropping the write. We model that here
    by retrying ``max_retries`` times on ``PalaceWriteLockTimeout``. A
    write that survives every retry is logged as a hard error.
    """
    written_ids: list[str] = []
    errors: list[str] = []
    fixed_embedding = [0.1] * 384  # bypass embedding download
    try:
        from mempalace.backends.chroma import ChromaBackend

        backend = ChromaBackend()
        col = backend.get_collection(palace_path, "mempalace_drawers", create=True)
        for i in range(n_writes):
            drawer_id = f"unique_w{worker_id}_n{i}"
            attempts = 0
            while True:
                attempts += 1
                try:
                    with palace_write_lock(palace_path, timeout=lock_timeout):
                        col.refresh_for_write()
                        col.upsert(
                            ids=[drawer_id],
                            documents=[f"unique drawer worker {worker_id} index {i}"],
                            metadatas=[
                                {
                                    "wing": f"unique_w{worker_id}",
                                    "room": "data_loss_test",
                                    "worker_id": worker_id,
                                    "write_index": i,
                                }
                            ],
                            embeddings=[fixed_embedding],
                        )
                    written_ids.append(drawer_id)
                    break
                except PalaceWriteLockTimeout:
                    if attempts >= max_retries:
                        errors.append(f"timeout i={i} after {attempts} retries")
                        break
                    # Exponential backoff with worker-id jitter so all
                    # workers don't retry in lockstep.
                    time.sleep(0.05 * attempts + 0.01 * worker_id)
                except Exception as exc:
                    errors.append(f"{type(exc).__name__} i={i}: {exc}")
                    break
    finally:
        Path(result_path).write_text(json.dumps({"written_ids": written_ids, "errors": errors}))


def _reader_loop(
    palace_path: str,
    reader_id: int,
    duration_seconds: float,
    result_path: str,
) -> None:
    """Continuously query the collection. Used to assert no torn reads.

    Records (a) any exception raised by query/get and (b) any time a
    document came back partially populated — both are signals of a torn
    read.
    """
    errors: list[str] = []
    queries_run = 0
    torn_reads = 0
    try:
        from mempalace.backends.chroma import ChromaBackend

        backend = ChromaBackend()
        col = backend.get_collection(palace_path, "mempalace_drawers", create=True)
        deadline = time.monotonic() + duration_seconds
        while time.monotonic() < deadline:
            try:
                # Plain get is enough — we're verifying that returned rows
                # are internally consistent. If a row exists in the result
                # set it must have a non-empty document.
                got = col.get(limit=50, include=["documents", "metadatas"])
                queries_run += 1
                ids = got.get("ids", [])
                docs = got.get("documents", [])
                metas = got.get("metadatas", [])
                # Length parity is the cheapest torn-read signal.
                if len(docs) != len(ids) or len(metas) != len(ids):
                    torn_reads += 1
                    continue
                for did, doc, meta in zip(ids, docs, metas):
                    # An id with no document or no metadata is a torn read.
                    if did and (not doc or not meta):
                        torn_reads += 1
                        break
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
            time.sleep(0.001)
    finally:
        Path(result_path).write_text(
            json.dumps(
                {
                    "reader_id": reader_id,
                    "errors": errors,
                    "queries_run": queries_run,
                    "torn_reads": torn_reads,
                }
            )
        )


def _hold_lock_then_block(
    palace_path: str,
    ready_path: str,
    release_path: str,
) -> None:
    """Hold the palace_write_lock until ``release_path`` appears.

    Used by ``test_lock_timeout_triggers_recovery_wal`` to keep the lock
    busy while another process tries to acquire.
    """
    with palace_write_lock(palace_path, timeout=10.0):
        Path(ready_path).touch()
        deadline = time.monotonic() + 30.0
        while not Path(release_path).exists() and time.monotonic() < deadline:
            time.sleep(0.05)


# ── Helpers ───────────────────────────────────────────────────────────


def _wait_for(path: Path, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


def _ensure_palace_initialised(palace_path: str) -> None:
    """Pre-create the chromadb collection in the parent before spawning workers.

    Realistic production setup: by the time six MCP servers attach to the
    palace, ``~/.mempalace/palace/chroma.sqlite3`` already exists from a
    prior ``mempalace init`` or from the first server's startup. Letting
    every subprocess race the initial ``PersistentClient`` open exposes a
    different race (chromadb's internal ``CREATE TABLE collections`` is
    not idempotent across concurrent first-opens) which lives below the
    palace_write_lock layer and is documented as a finding in the Wave 3
    review. Pre-creating the palace lets these tests focus on the write
    paths that the lock IS supposed to protect.

    Initialised with NO embedding function so the workers (which also
    skip the embedding pipeline to avoid downloading the ONNX model on
    every spawn) can attach without a dimensionality mismatch.
    """
    from mempalace.backends.chroma import ChromaBackend

    backend = ChromaBackend()
    col = backend.get_collection(palace_path, "mempalace_drawers", create=True)
    # Touch the collection so the underlying segments exist on disk
    # before any subprocess tries to open them.
    _ = col.count()


def _join_all(procs, timeout=60.0):
    """Join every process; SIGTERM stragglers to keep CI from hanging."""
    deadline = time.monotonic() + timeout
    for p in procs:
        remaining = max(0.1, deadline - time.monotonic())
        p.join(timeout=remaining)
    for p in procs:
        if p.is_alive():
            p.terminate()
            p.join(timeout=2.0)


def _read_results(result_paths):
    """Decode the JSON outcome dropped by each worker."""
    results = []
    for path in result_paths:
        if not path.exists():
            results.append({"missing": True})
            continue
        try:
            results.append(json.loads(path.read_text()))
        except json.JSONDecodeError as exc:
            results.append({"decode_error": str(exc), "raw": path.read_text()})
    return results


# ── Tests ─────────────────────────────────────────────────────────────


@pytest.mark.stress
def test_six_concurrent_writers_no_corruption(tmp_path):
    """Six subprocesses write ~50 entries each — assert no corruption.

    Mirrors the production failure mode: 6 concurrent MCP servers all
    writing to the same palace at the same time.
    """
    palace = tmp_path / "palace"
    palace.mkdir()
    _ensure_palace_initialised(str(palace))

    n_workers = 6
    n_writes_per_worker = 50
    op_mix = ["add_drawer", "diary", "tunnel"]

    procs = []
    result_paths = []
    for worker_id in range(n_workers):
        result_path = tmp_path / f"worker_{worker_id}_result.json"
        result_paths.append(result_path)
        proc = _MP_CTX.Process(
            target=_writer_loop,
            args=(
                str(palace),
                worker_id,
                n_writes_per_worker,
                op_mix,
                str(result_path),
            ),
        )
        proc.start()
        procs.append(proc)

    _join_all(procs, timeout=120.0)

    # 1) Every subprocess exited cleanly.
    for i, p in enumerate(procs):
        assert not p.is_alive(), f"worker {i} did not exit"
        assert p.exitcode == 0, f"worker {i} exit code = {p.exitcode}"

    # 2) Every subprocess produced an outcome with zero errors.
    results = _read_results(result_paths)
    all_written: list[str] = []
    for i, res in enumerate(results):
        assert "missing" not in res, f"worker {i} produced no result file"
        assert res.get("errors", []) == [], f"worker {i} reported errors: {res['errors']}"
        all_written.extend(res["written_ids"])

    expected_unique = n_workers * n_writes_per_worker
    assert len(all_written) == expected_unique, (
        f"expected {expected_unique} writes, got {len(all_written)}"
    )

    # 3) Final collection count matches expectation. ``add_drawer`` and
    #    ``tunnel`` use upsert so distinct ids never collide; ``diary``
    #    uses add. Each id is unique by construction.
    from mempalace.palace import get_collection

    col = get_collection(str(palace), create=False)
    assert col.count() == expected_unique, (
        f"collection.count() = {col.count()}, expected {expected_unique}"
    )

    # 4) Health check passes — no segment corruption, no count drift.
    report = check_palace_health(palace)
    assert report.status == "ok", (
        f"palace health = {report.status}; issues: {[i.code for i in report.issues]}"
    )

    # 5) Every written id is actually retrievable. This is the strongest
    #    assertion we can make: it proves the lock didn't just prevent
    #    crashes, it prevented lost writes too.
    sample = random.Random(0).sample(all_written, min(60, len(all_written)))
    found = col.get(ids=sample)
    assert set(found["ids"]) == set(sample), (
        f"missing ids after stress run: {set(sample) - set(found['ids'])}"
    )


@pytest.mark.stress
def test_concurrent_writers_with_random_kill(tmp_path):
    """Randomly SIGKILL writers — survivors keep working, no deadlock.

    Verifies that fcntl-released locks are picked up by the next acquirer
    so a killed writer cannot wedge the palace.
    """
    if os.name == "nt":  # pragma: no cover
        pytest.skip("SIGKILL semantics differ on Windows")

    palace = tmp_path / "palace"
    palace.mkdir()
    _ensure_palace_initialised(str(palace))

    # Run for long enough that surviving workers actually do work. Each
    # subprocess spends ~500ms-1s on chromadb client init before its
    # first write — so a 5-second window with kills every 200ms would
    # never let any worker get past init. 8 seconds with 800ms between
    # kills gives both healthy work AND the kill-during-write race we
    # want to exercise.
    duration_seconds = 8.0
    kill_interval_seconds = 0.8
    base_workers = 4
    op_mix = ["add_drawer", "diary"]

    rng = random.Random(123)
    procs: list = []
    next_worker_id = 0
    result_paths: list = []

    def _spawn_worker():
        nonlocal next_worker_id
        wid = next_worker_id
        next_worker_id += 1
        rp = tmp_path / f"kill_worker_{wid}_result.json"
        result_paths.append(rp)
        # Use a real subprocess (not multiprocessing) so SIGKILL is clean.
        # Pass HOME through so the worker shares pytest's tmp HOME — the
        # palace_write_lock file lives under HOME, and a different HOME
        # in the child would mean a different lock file, defeating the
        # whole test.
        cmd = [
            sys.executable,
            "-c",
            (
                "import sys; "
                f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r}); "
                "from tests.test_palace_concurrent_stress import _writer_loop; "
                "_writer_loop("
                f"{str(palace)!r}, {wid}, 1000, {op_mix!r}, {str(rp)!r}, 60.0"
                ")"
            ),
        ]
        env = os.environ.copy()
        proc = subprocess.Popen(cmd, env=env)
        return proc

    for _ in range(base_workers):
        procs.append(_spawn_worker())

    deadline = time.monotonic() + duration_seconds
    while time.monotonic() < deadline:
        time.sleep(kill_interval_seconds)
        # Pick a victim and SIGKILL it. Spawn a replacement immediately.
        if procs:
            victim_idx = rng.randrange(len(procs))
            victim = procs.pop(victim_idx)
            try:
                victim.send_signal(signal.SIGKILL)
                victim.wait(timeout=2.0)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
            procs.append(_spawn_worker())

    # Give in-flight writers a moment to finish their current loop
    # iteration, then stop everyone with SIGKILL — the whole point of
    # this test is that the kernel-released flock lets the next acquirer
    # in. SIGKILL'd workers won't drop a result file, so we'll check the
    # palace contents directly to prove writes happened.
    time.sleep(1.5)  # let surviving workers do at least a few iterations
    for p in procs:
        if p.poll() is None:
            try:
                p.send_signal(signal.SIGKILL)
            except ProcessLookupError:
                pass

    for p in procs:
        try:
            p.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=2.0)

    # The palace itself must remain healthy — that's the load-bearing
    # assertion. Individual SIGKILL'd writers will of course report
    # nothing in their result files, but the palace must be intact.
    report = check_palace_health(palace)
    assert report.status == "ok", (
        f"palace health after random-kill stress = {report.status}; "
        f"issues: {[i.code for i in report.issues]}"
    )

    # The palace must contain SOME writes — proves the killed-writer
    # locks were released cleanly so subsequent writers could proceed.
    # We use the palace contents (not subprocess result files) because
    # SIGKILL'd workers never get to flush their JSON.
    from mempalace.palace import get_collection

    col = get_collection(str(palace), create=False)
    final_count = col.count()
    assert final_count > 0, "no writes landed in the palace — possible deadlock from leaked locks"


def _try_async_save_under_held_lock(palace_path: str, result_path: str) -> None:
    """Subprocess target: invoke _async_save_worker against a held lock."""
    from unittest.mock import patch

    # Stub the LLM path so the worker proceeds straight to the write
    # phase. The fake response intentionally produces a tiny payload so
    # the recovery WAL file is small and easy to verify.
    fake_payload = {
        "diary": (
            "Diary line that should land in recovery WAL when the lock times out. "
            "Long enough to clear the 20-char minimum filter."
        ),
        "drawers": [],
        "kg": [],
        "tunnels": [],
    }

    with (
        patch(
            "mempalace.recall_llm._get_llm_config",
            return_value={"endpoint": "stub", "model": "stub"},
        ),
        patch(
            "mempalace.recall_llm._call_llm",
            return_value=json.dumps(fake_payload),
        ),
        patch(
            "mempalace.hooks_cli._build_palace_context",
            return_value="",
        ),
        patch.dict(os.environ, {"MEMPAL_PALACE_PATH": palace_path}),
        patch(
            "mempalace.hooks_cli._PALACE_WRITE_LOCK_TIMEOUT_S",
            0.5,  # tiny budget so the timeout path fires fast
        ),
    ):
        try:
            from mempalace.hooks_cli import _async_save_worker

            _async_save_worker(
                transcript_text="user said something interesting",
                session_id="stress-session",
                cwd="/tmp/stress",
            )
            outcome = "ok"
        except SystemExit as exc:
            # _async_save_worker exits 0 on the recovery-WAL path; that's
            # the contract.
            outcome = f"exit:{exc.code}"
        except Exception as exc:
            outcome = f"crash:{type(exc).__name__}:{exc}"
    Path(result_path).write_text(outcome)


@pytest.mark.stress
def test_lock_timeout_triggers_recovery_wal(tmp_path):
    """When _async_save_worker can't acquire the lock, payload hits the WAL."""
    palace = tmp_path / "palace"
    palace.mkdir()
    _ensure_palace_initialised(str(palace))

    # Drain any pre-existing recovery files for this palace from a prior
    # test run on the same HOME tmpdir — the assertion below expects the
    # WAL contents we wrote to be the only ones present.
    rec_dir = recovery_dir_for_palace(str(palace))
    if rec_dir.exists():
        for child in rec_dir.iterdir():
            child.unlink(missing_ok=True)

    ready = tmp_path / "ready"
    release = tmp_path / "release"

    holder = _MP_CTX.Process(
        target=_hold_lock_then_block,
        args=(str(palace), str(ready), str(release)),
    )
    holder.start()
    try:
        assert _wait_for(ready, timeout=10.0), "lock holder failed to acquire"

        result_path = tmp_path / "async_save_outcome.txt"
        worker = _MP_CTX.Process(
            target=_try_async_save_under_held_lock,
            args=(str(palace), str(result_path)),
        )
        worker.start()
        worker.join(timeout=30.0)

        assert not worker.is_alive(), "async_save worker hung waiting on lock"

        # Worker must have exited cleanly — either ok (already drained)
        # or exit:0 (recovery-WAL path). Crashes are not allowed.
        outcome = result_path.read_text()
        assert outcome in ("ok", "exit:0"), (
            f"async_save worker did not handle lock timeout cleanly: {outcome}"
        )
    finally:
        release.touch()
        holder.join(timeout=10.0)
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=2.0)

    # Recovery WAL must exist and contain the diary payload verbatim.
    assert rec_dir.exists(), f"recovery dir not created at {rec_dir}"
    wal_files = list(rec_dir.glob("*.jsonl"))
    assert wal_files, f"no WAL files in {rec_dir}"

    # Concatenate every line of every wal file and assert our payload
    # text appears verbatim — the "verbatim always" promise.
    found = False
    for wal in wal_files:
        for line in wal.read_text().splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("op") == "diary":
                args = rec.get("args", {})
                if "recovery WAL" in args.get("document", ""):
                    found = True
                    break
        if found:
            break
    assert found, "diary payload missing from recovery WAL"


@pytest.mark.stress
def test_no_data_loss_under_contention(tmp_path):
    """10 writers, 100 ids each, every id queryable afterward.

    The strongest end-to-end correctness assertion in this file: it would
    fail loudly if a single write got dropped by a lost wakeup, a
    partial commit, or any race.
    """
    palace = tmp_path / "palace"
    palace.mkdir()
    _ensure_palace_initialised(str(palace))

    n_workers = 10
    n_writes_per_worker = 100

    procs = []
    result_paths = []
    for worker_id in range(n_workers):
        rp = tmp_path / f"data_loss_w{worker_id}_result.json"
        result_paths.append(rp)
        proc = _MP_CTX.Process(
            target=_writer_unique_ids,
            args=(str(palace), worker_id, n_writes_per_worker, str(rp)),
        )
        proc.start()
        procs.append(proc)

    _join_all(procs, timeout=180.0)

    for i, p in enumerate(procs):
        assert not p.is_alive(), f"writer {i} hung"
        assert p.exitcode == 0, f"writer {i} exit code = {p.exitcode}"

    results = _read_results(result_paths)
    expected_ids: set[str] = set()
    for i, res in enumerate(results):
        assert "missing" not in res, f"worker {i} produced no result file"
        assert res.get("errors", []) == [], f"worker {i} errors: {res['errors']}"
        ids = res["written_ids"]
        assert len(ids) == n_writes_per_worker, (
            f"worker {i} wrote {len(ids)} ids, expected {n_writes_per_worker}"
        )
        expected_ids.update(ids)

    assert len(expected_ids) == n_workers * n_writes_per_worker, (
        "duplicate id reported across workers"
    )

    from mempalace.palace import get_collection

    col = get_collection(str(palace), create=False)
    assert col.count() == len(expected_ids), (
        f"collection.count() = {col.count()}, expected {len(expected_ids)}"
    )

    # Re-fetch every id in chunks. ChromaDB's get() with explicit ids is
    # the most direct correctness oracle — if it can't find the id we
    # think we wrote, the lock failed to provide its guarantee.
    expected_list = sorted(expected_ids)
    chunk = 200
    found_ids: set[str] = set()
    for i in range(0, len(expected_list), chunk):
        batch = expected_list[i : i + chunk]
        got = col.get(ids=batch)
        found_ids.update(got["ids"])
    missing = set(expected_list) - found_ids
    assert not missing, f"{len(missing)} ids are missing after writes"

    # Health check confirms no torn segments.
    report = check_palace_health(palace)
    assert report.status == "ok", (
        f"palace health = {report.status}; issues: {[i.code for i in report.issues]}"
    )


@pytest.mark.stress
def test_mixed_read_write_no_torn_reads(tmp_path):
    """4 writers + 4 readers — readers never see a half-written document.

    The lock + refresh_for_write contract means an external reader can
    open a stale snapshot, but every individual document it sees must be
    fully populated (id + document + metadata) — or absent entirely.
    """
    palace = tmp_path / "palace"
    palace.mkdir()
    _ensure_palace_initialised(str(palace))

    # Pre-seed so readers have something to read from the first iteration.
    # Skip embedding fn for the same reasons as the worker functions.
    from mempalace.backends.chroma import ChromaBackend

    backend = ChromaBackend()
    seed_col = backend.get_collection(str(palace), "mempalace_drawers", create=True)
    fixed_embedding = [0.1] * 384
    with palace_write_lock(str(palace), timeout=10.0):
        seed_col.refresh_for_write()
        seed_col.upsert(
            ids=[f"seed_{i}" for i in range(20)],
            documents=[f"seed document number {i}" for i in range(20)],
            metadatas=[{"wing": "seed", "room": "warmup", "added_by": "seed"} for _ in range(20)],
            embeddings=[fixed_embedding for _ in range(20)],
        )

    duration_seconds = 4.0
    n_writers = 4
    n_readers = 4

    writer_results = []
    reader_results = []
    procs = []

    for worker_id in range(n_writers):
        rp = tmp_path / f"mixed_w{worker_id}.json"
        writer_results.append(rp)
        # Bound writers by total writes; the test ends when readers stop.
        proc = _MP_CTX.Process(
            target=_writer_loop,
            args=(
                str(palace),
                worker_id,
                200,
                ["add_drawer"],
                str(rp),
            ),
        )
        proc.start()
        procs.append(proc)

    for reader_id in range(n_readers):
        rp = tmp_path / f"mixed_r{reader_id}.json"
        reader_results.append(rp)
        proc = _MP_CTX.Process(
            target=_reader_loop,
            args=(str(palace), reader_id, duration_seconds, str(rp)),
        )
        proc.start()
        procs.append(proc)

    _join_all(procs, timeout=120.0)

    for i, p in enumerate(procs):
        assert not p.is_alive(), f"process {i} hung"
        assert p.exitcode == 0, f"process {i} exit code = {p.exitcode}"

    # Readers must report zero torn reads and zero exceptions.
    for rp in reader_results:
        res = json.loads(rp.read_text())
        assert res["errors"] == [], f"reader {res['reader_id']} errors: {res['errors']}"
        assert res["torn_reads"] == 0, (
            f"reader {res['reader_id']} saw {res['torn_reads']} torn reads"
        )
        # Sanity: at least one query ran in the duration window.
        assert res["queries_run"] > 0, f"reader {res['reader_id']} ran no queries"

    # Writers should also have completed cleanly.
    for rp in writer_results:
        res = json.loads(rp.read_text())
        assert res["errors"] == [], f"writer {res['worker_id']} errors: {res['errors']}"

    # Final palace health.
    report = check_palace_health(palace)
    assert report.status == "ok", (
        f"palace health = {report.status}; issues: {[i.code for i in report.issues]}"
    )


# ── Sanity: lock-file naming matches the implementation ───────────────


def test_stress_helper_lock_path_matches_implementation(tmp_path):
    """Make sure the recovery-dir helper agrees with the lock layout.

    Sanity check that prevents future drift: the recovery-dir hash and
    the lock-file hash MUST share the same scheme so operators can map
    one to the other by eye.
    """
    palace = tmp_path / "palace_xyz"
    palace.mkdir()

    rec_dir = recovery_dir_for_palace(str(palace))
    expected_hash = hashlib.sha256(str(palace.resolve()).encode()).hexdigest()[:16]
    assert rec_dir.name == expected_hash, (
        f"recovery dir hash {rec_dir.name} != expected {expected_hash}"
    )
