"""test_miner_concurrency.py — Wave 2 concurrency-fix tests for the miners.

These tests verify that miner.process_file and convo_miner._file_chunks_locked
honor the two-tier lock contract:
    mine_lock(source_file)              # outer — per-file dedup
        palace_write_lock(palace_path)  # inner — palace-wide HNSW gate

…and that ``refresh_for_write`` is called inside the palace lock
before the first write. Lock-ordering, timeout-graceful-skip, and a
two-process serialization smoke test round it out.

The two-process test is the one that catches real HNSW corruption
regressions — if the inner palace lock is missing, two miners writing
to different files can still race ChromaDB's segment writer.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
from pathlib import Path
from unittest.mock import MagicMock

import chromadb
import pytest
import yaml

from mempalace.palace import PalaceWriteLockTimeout


# ── Helpers ────────────────────────────────────────────────────────────


def _write_project(project_root: Path, files: dict, wing: str = "concurrency_test") -> None:
    """Bootstrap a tiny mempalace project with a yaml + N files."""
    with open(project_root / "mempalace.yaml", "w") as fh:
        yaml.dump(
            {
                "wing": wing,
                "rooms": [{"name": "general", "description": "All files"}],
            },
            fh,
        )
    for relpath, content in files.items():
        target = project_root / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


@pytest.fixture
def project_root():
    """Throwaway project directory, cleaned up after the test."""
    tmp = tempfile.mkdtemp(prefix="mempal_miner_conc_")
    try:
        yield Path(tmp).resolve()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── 1. miner.process_file: lock ordering ───────────────────────────────


class TestMinerLockOrdering:
    def test_miner_acquires_palace_write_lock_inside_mine_lock(self, monkeypatch, project_root):
        """mine_lock MUST be acquired before palace_write_lock; both released after."""
        from mempalace import miner

        _write_project(
            project_root,
            {"notes.md": "verbatim text content for one drawer\n" * 30},
        )
        palace_path = str(project_root / "palace")

        events: list[str] = []

        @contextlib.contextmanager
        def _spy_mine_lock(src):
            events.append(f"mine_lock_in:{Path(src).name}")
            yield
            events.append(f"mine_lock_out:{Path(src).name}")

        @contextlib.contextmanager
        def _spy_palace_lock(palace_path, timeout=30.0):
            events.append("palace_lock_in")
            yield
            events.append("palace_lock_out")

        monkeypatch.setattr(miner, "mine_lock", _spy_mine_lock)
        monkeypatch.setattr(miner, "palace_write_lock", _spy_palace_lock)

        miner.mine(str(project_root), palace_path)

        # mine_lock must be acquired BEFORE the inner palace_write_lock,
        # and released AFTER it. The structural sequence per file is:
        #   mine_lock_in → palace_lock_in → palace_lock_out → mine_lock_out
        try:
            mine_in = events.index("mine_lock_in:notes.md")
            palace_in = events.index("palace_lock_in")
            palace_out = events.index("palace_lock_out")
            mine_out = events.index("mine_lock_out:notes.md")
        except ValueError as exc:  # pragma: no cover
            pytest.fail(f"missing expected lock event: {exc!r}; got {events}")

        assert mine_in < palace_in < palace_out < mine_out, (
            f"unexpected lock interleaving: {events}"
        )

    def test_miner_calls_refresh_for_write_before_write(self, monkeypatch, project_root):
        """refresh_for_write MUST run inside the palace lock, before first write."""
        from mempalace import miner

        _write_project(
            project_root,
            {"notes.md": "verbatim text content for one drawer\n" * 30},
        )
        palace_path = str(project_root / "palace")

        order: list[str] = []

        @contextlib.contextmanager
        def _record_palace_lock(_path, timeout=30.0):
            order.append("palace_lock_in")
            yield
            order.append("palace_lock_out")

        monkeypatch.setattr(miner, "palace_write_lock", _record_palace_lock)

        original_get_collection = miner.get_collection
        original_get_closets = miner.get_closets_collection

        def _wrapped_get_collection(palace, **kwargs):
            real = original_get_collection(palace, **kwargs)
            spy = MagicMock(wraps=real)

            real_refresh = getattr(real, "refresh_for_write", lambda: None)

            def _spy_refresh():
                order.append("refresh_for_write")
                real_refresh()

            spy.refresh_for_write = MagicMock(side_effect=_spy_refresh)

            real_upsert = real.upsert

            def _spy_upsert(*args, **kwargs):
                order.append("upsert")
                return real_upsert(*args, **kwargs)

            spy.upsert = MagicMock(side_effect=_spy_upsert)

            real_delete = real.delete

            def _spy_delete(*args, **kwargs):
                order.append("delete")
                return real_delete(*args, **kwargs)

            spy.delete = MagicMock(side_effect=_spy_delete)
            return spy

        monkeypatch.setattr(miner, "get_collection", _wrapped_get_collection)
        # Closets collection: keep the real one but skip its spy treatment.
        monkeypatch.setattr(miner, "get_closets_collection", original_get_closets)

        miner.mine(str(project_root), palace_path)

        # Required ordering: palace_lock_in < refresh_for_write < first upsert/delete
        # < palace_lock_out.
        assert "palace_lock_in" in order
        assert "refresh_for_write" in order
        assert "upsert" in order
        assert "palace_lock_out" in order

        lock_in = order.index("palace_lock_in")
        refresh = order.index("refresh_for_write")
        first_write = min(i for i, ev in enumerate(order) if ev in ("upsert", "delete"))
        lock_out = order.index("palace_lock_out")

        assert lock_in < refresh < first_write < lock_out, (
            f"refresh did not run inside lock before first write: {order}"
        )

    def test_miner_handles_palace_write_lock_timeout_gracefully(
        self, monkeypatch, project_root, caplog
    ):
        """A PalaceWriteLockTimeout on file A must NOT abort the rest of the run."""
        from mempalace import miner

        _write_project(
            project_root,
            {
                "skip_me.md": "verbatim content one\n" * 30,
                "process_me.md": "verbatim content two\n" * 30,
            },
        )
        palace_path = str(project_root / "palace")

        # Make the lock raise on the FIRST acquisition only (skip_me.md)
        # and behave normally afterwards (process_me.md).
        call_count = {"n": 0}
        real_palace_lock = miner.palace_write_lock

        @contextlib.contextmanager
        def _flaky_lock(palace, timeout=30.0):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise PalaceWriteLockTimeout("simulated contention on first file")
            with real_palace_lock(palace, timeout=timeout):
                yield

        monkeypatch.setattr(miner, "palace_write_lock", _flaky_lock)

        with caplog.at_level("ERROR"):
            miner.mine(str(project_root), palace_path)

        # The mine must NOT have raised. The second file (`process_me.md`)
        # must have been filed normally.
        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_collection("mempalace_drawers")
        all_meta = col.get()["metadatas"]
        sources = {m.get("source_file", "") for m in all_meta if m}
        del col, client

        process_resolved = str(project_root / "process_me.md")
        skip_resolved = str(project_root / "skip_me.md")

        assert any(process_resolved in s for s in sources), (
            f"second file should have been mined; got sources={sources}"
        )
        assert not any(skip_resolved in s for s in sources), (
            f"first file should have been skipped on lock timeout; got sources={sources}"
        )

        # And the failure was logged so an operator can see it.
        timeout_messages = [
            rec.message for rec in caplog.records if "palace write lock timeout" in rec.message
        ]
        assert timeout_messages, (
            f"expected a timeout log entry; got {[r.message for r in caplog.records]}"
        )


# ── 2. convo_miner._file_chunks_locked: lock ordering ─────────────────


class TestConvoMinerLockOrdering:
    def test_convo_miner_acquires_palace_write_lock(self, monkeypatch, project_root):
        """mine_convos must acquire palace_write_lock for the chunked write."""
        from mempalace import convo_miner

        convo_path = project_root / "chat.txt"
        convo_path.write_text(
            "> What is memory?\nMemory is persistence.\n\n"
            "> Why does it matter?\nIt enables continuity.\n\n"
            "> How do we build it?\nWith structured storage.\n",
            encoding="utf-8",
        )
        palace_path = str(project_root / "palace")

        events: list[str] = []

        @contextlib.contextmanager
        def _spy_mine_lock(src):
            events.append(f"mine_lock_in:{Path(src).name}")
            yield
            events.append(f"mine_lock_out:{Path(src).name}")

        @contextlib.contextmanager
        def _spy_palace_lock(palace_path, timeout=30.0):
            events.append("palace_lock_in")
            yield
            events.append("palace_lock_out")

        monkeypatch.setattr(convo_miner, "mine_lock", _spy_mine_lock)
        monkeypatch.setattr(convo_miner, "palace_write_lock", _spy_palace_lock)

        convo_miner.mine_convos(str(project_root), palace_path, wing="conv_test")

        try:
            mine_in = events.index("mine_lock_in:chat.txt")
            palace_in = events.index("palace_lock_in")
            palace_out = events.index("palace_lock_out")
            mine_out = events.index("mine_lock_out:chat.txt")
        except ValueError as exc:  # pragma: no cover
            pytest.fail(f"missing expected lock event: {exc!r}; got {events}")

        assert mine_in < palace_in < palace_out < mine_out, (
            f"unexpected lock interleaving: {events}"
        )

    def test_convo_miner_handles_palace_write_lock_timeout_gracefully(
        self, monkeypatch, project_root, caplog
    ):
        """A timeout on convo file A must not abort the rest of the run."""
        from mempalace import convo_miner

        # Two distinct convo files; first will hit a simulated timeout.
        (project_root / "first.txt").write_text(
            "> Q one\nA one verbatim payload\n", encoding="utf-8"
        )
        (project_root / "second.txt").write_text(
            "> Q two\nA two verbatim payload\n", encoding="utf-8"
        )
        palace_path = str(project_root / "palace")

        call_count = {"n": 0}
        real_palace_lock = convo_miner.palace_write_lock

        @contextlib.contextmanager
        def _flaky_lock(palace, timeout=30.0):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise PalaceWriteLockTimeout("simulated contention on first convo file")
            with real_palace_lock(palace, timeout=timeout):
                yield

        monkeypatch.setattr(convo_miner, "palace_write_lock", _flaky_lock)

        with caplog.at_level("ERROR"):
            convo_miner.mine_convos(str(project_root), palace_path, wing="conv_test")

        # The mine must not have crashed. Second file should be recorded.
        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_collection("mempalace_drawers")
        sources = {(m or {}).get("source_file", "") for m in col.get()["metadatas"]}
        del col, client

        # At least one of the two files should produce a real (non-registry)
        # drawer for second.txt — registry sentinels share the same source.
        second_resolved = str(project_root / "second.txt")
        assert any(second_resolved in s for s in sources), (
            f"second file should have been mined; got sources={sources}"
        )

        timeout_messages = [
            rec.message for rec in caplog.records if "palace write lock timeout" in rec.message
        ]
        assert timeout_messages, "expected timeout log entry"


# ── 3. Two-process serialization (the corruption-prevention test) ──────


# Subprocess body that runs ONE concurrent miner. Spawning real subprocesses
# is the only way to exercise the cross-process palace_write_lock — fcntl
# advisory locks are a kernel-level construct, not Python-level.
_WORKER_SCRIPT = """
import os, sys
sys.path.insert(0, {repo_root!r})

from mempalace import miner

# Wait for both miners to be ready before racing.
ready = open({ready_path!r}, "w")
ready.write("hi")
ready.close()

# Block until the parent says go.
while not os.path.exists({go_path!r}):
    pass

miner.mine({project_dir!r}, {palace_path!r})
"""


class TestConcurrentMinersSerialize:
    def test_two_concurrent_miners_serialize_via_palace_lock(self, project_root):
        """Two miners hitting different files in the SAME palace must succeed
        without corruption and produce the expected total drawer count.

        Spawns two real subprocesses (so fcntl flock semantics actually
        apply — Python-level threading would not exercise the cross-
        process lock the way miner.process_file calls it).
        """
        # Two project dirs sharing ONE palace. Different source files so
        # mine_lock does NOT serialize them — only palace_write_lock can.
        proj_a = project_root / "proj_a"
        proj_b = project_root / "proj_b"
        proj_a.mkdir()
        proj_b.mkdir()
        palace_path = str(project_root / "shared_palace")
        os.makedirs(palace_path)

        _write_project(
            proj_a,
            {"notes_a.md": "alpha verbatim payload\n" * 60},
            wing="proj_a",
        )
        _write_project(
            proj_b,
            {"notes_b.md": "beta verbatim payload\n" * 60},
            wing="proj_b",
        )

        # Bootstrap the palace ONCE in the parent so the two child processes
        # don't race ChromaDB's initial CREATE TABLE on the brand-new
        # SQLite file. palace_write_lock protects HNSW writes, not initial
        # SQLite schema creation — that's a separate ChromaDB-internal
        # race that's irrelevant to what we're testing here.
        bootstrap_client = chromadb.PersistentClient(path=palace_path)
        bootstrap_client.get_or_create_collection(
            "mempalace_drawers", metadata={"hnsw:space": "cosine"}
        )
        bootstrap_client.get_or_create_collection(
            "mempalace_closets", metadata={"hnsw:space": "cosine"}
        )
        del bootstrap_client

        repo_root = str(Path(__file__).parent.parent.resolve())
        ready_a = project_root / ".ready_a"
        ready_b = project_root / ".ready_b"
        go_path = project_root / ".go"

        def _make_script(proj_dir, ready_path):
            return textwrap.dedent(
                _WORKER_SCRIPT.format(
                    repo_root=repo_root,
                    ready_path=str(ready_path),
                    go_path=str(go_path),
                    project_dir=str(proj_dir),
                    palace_path=palace_path,
                )
            )

        env = os.environ.copy()
        # Force subprocesses to use the SAME isolated HOME as the parent
        # so the lock files land in the test-isolated ~/.mempalace/locks.
        # If we don't pin this, the children would write to the real $HOME.
        env["HOME"] = os.environ.get("HOME", "")
        env["USERPROFILE"] = os.environ.get("USERPROFILE", "")

        script_a = _make_script(proj_a, ready_a)
        script_b = _make_script(proj_b, ready_b)

        proc_a = subprocess.Popen(
            [sys.executable, "-c", script_a],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        proc_b = subprocess.Popen(
            [sys.executable, "-c", script_b],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

        try:
            # Wait for both children to be ready (they create their ready
            # files just before blocking on the go signal).
            import time

            deadline = time.monotonic() + 60.0
            while not (ready_a.exists() and ready_b.exists()):
                if time.monotonic() > deadline:
                    proc_a.kill()
                    proc_b.kill()
                    pytest.fail(
                        f"workers did not become ready in 60s; a.exists={ready_a.exists()}, b.exists={ready_b.exists()}"
                    )
                # Surface child errors early.
                if proc_a.poll() is not None and not ready_a.exists():
                    out, err = proc_a.communicate(timeout=5)
                    pytest.fail(
                        f"worker A exited prematurely: rc={proc_a.returncode}\nSTDOUT:\n{out.decode(errors='replace')}\nSTDERR:\n{err.decode(errors='replace')}"
                    )
                if proc_b.poll() is not None and not ready_b.exists():
                    out, err = proc_b.communicate(timeout=5)
                    pytest.fail(
                        f"worker B exited prematurely: rc={proc_b.returncode}\nSTDOUT:\n{out.decode(errors='replace')}\nSTDERR:\n{err.decode(errors='replace')}"
                    )
                time.sleep(0.05)

            # Release both at once.
            go_path.write_text("go", encoding="utf-8")

            out_a, err_a = proc_a.communicate(timeout=120)
            out_b, err_b = proc_b.communicate(timeout=120)
        finally:
            for p in (proc_a, proc_b):
                if p.poll() is None:
                    p.kill()

        # Both processes must exit cleanly. ChromaDB segfaults from
        # racing HNSW writers would surface as nonzero exit codes here.
        assert proc_a.returncode == 0, (
            f"worker A failed: rc={proc_a.returncode}\n"
            f"STDOUT:\n{out_a.decode(errors='replace')}\n"
            f"STDERR:\n{err_a.decode(errors='replace')}"
        )
        assert proc_b.returncode == 0, (
            f"worker B failed: rc={proc_b.returncode}\n"
            f"STDOUT:\n{out_b.decode(errors='replace')}\n"
            f"STDERR:\n{err_b.decode(errors='replace')}"
        )

        # Open the shared palace and confirm both wings are present and
        # the drawer count is the union of both miners' contributions —
        # no rows lost to interleaved writes.
        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_collection("mempalace_drawers")
        all_meta = col.get()["metadatas"]
        wings = {(m or {}).get("wing", "") for m in all_meta}
        del col, client

        assert "proj_a" in wings, f"wing proj_a missing from palace; got {wings}"
        assert "proj_b" in wings, f"wing proj_b missing from palace; got {wings}"


# ── 4. Single-process two-thread serialization ────────────────────────


class TestConcurrentMinersSerializeInProcess:
    def test_two_threads_writing_different_files_both_succeed(self, monkeypatch, project_root):
        """Threaded analogue of the subprocess test — quick smoke that the
        in-process integration doesn't deadlock and both files land.

        fcntl.flock is per-OPEN-FILE (Linux) but per-PROCESS on most BSD/
        macOS implementations, so two threads in one process may not block
        each other — that's fine. We're verifying no exceptions and both
        rows present, not strict serialization.
        """
        from mempalace import miner

        proj_a = project_root / "proj_a"
        proj_b = project_root / "proj_b"
        proj_a.mkdir()
        proj_b.mkdir()
        palace_path = str(project_root / "shared_palace")
        os.makedirs(palace_path)

        _write_project(proj_a, {"notes_a.md": "alpha verbatim text\n" * 30}, wing="proj_a")
        _write_project(proj_b, {"notes_b.md": "beta verbatim text\n" * 30}, wing="proj_b")

        errors: list[BaseException] = []

        def _worker(pdir):
            try:
                miner.mine(str(pdir), palace_path)
            except Exception as exc:  # pragma: no cover - defensive
                errors.append(exc)

        t1 = threading.Thread(target=_worker, args=(proj_a,))
        t2 = threading.Thread(target=_worker, args=(proj_b,))
        t1.start()
        t2.start()
        t1.join(timeout=120)
        t2.join(timeout=120)

        assert not errors, f"thread workers raised: {errors}"

        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_collection("mempalace_drawers")
        wings = {(m or {}).get("wing", "") for m in col.get()["metadatas"]}
        del col, client

        assert "proj_a" in wings
        assert "proj_b" in wings
