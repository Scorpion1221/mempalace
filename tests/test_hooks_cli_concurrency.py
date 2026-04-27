"""test_hooks_cli_concurrency.py — Wave 2 concurrency-fix tests for hooks_cli.

Verifies that ``_async_save_worker`` (the background process spawned by stop
hooks) honours the palace_write_lock contract, and that on lock-timeout the
unwritten payload is persisted to the recovery WAL instead of being silently
dropped.

Why this matters: each Cursor / Claude Code session can trigger an
``_async_save_worker`` subprocess on stop. Two simultaneous stops to the
same palace race the HNSW writer and corrupt segments. Layer 1
(``palace_write_lock``) closes the race; the recovery WAL closes the
"100% recall" gap when the lock times out.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mempalace import hooks_cli, recovery_wal
from mempalace.palace import PalaceWriteLockTimeout


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def isolated_palace(tmp_path, monkeypatch):
    """Point MempalaceConfig.palace_path at a fresh temp dir."""
    palace = tmp_path / "palace"
    palace.mkdir()
    # MEMPAL_PALACE_PATH is read by MempalaceConfig.palace_path, no config
    # file required.
    monkeypatch.setenv("MEMPAL_PALACE_PATH", str(palace))
    return str(palace)


@pytest.fixture
def llm_response_payload():
    """A minimal but realistic LLM response with diary + drawer + kg + tunnel."""
    return {
        "diary": (
            "Worked on concurrency fix for MemPalace. Added palace_write_lock "
            "around the async_save_worker writes so two stop hooks racing the "
            "HNSW writer can no longer corrupt segments."
        ),
        "drawers": [
            {
                "wing": "mempalace",
                "room": "concurrency",
                "content": (
                    "palace_write_lock contract: hold the lock for the duration "
                    "of all chroma writes for one logical save. Wave 2."
                ),
            },
            {
                "wing": "other_wing",
                "room": "concurrency",
                "content": (
                    "Auto-tunnels link concurrency rooms across wings. The "
                    "auto-link pass runs INSIDE the same lock so it sees the "
                    "rooms we just upserted."
                ),
            },
        ],
        "kg": [
            {
                "subject": "_async_save_worker",
                "predicate": "uses",
                "object": "palace_write_lock",
            }
        ],
        "tunnels": [
            {
                "source_wing": "mempalace",
                "source_room": "concurrency",
                "target_wing": "other_wing",
                "target_room": "concurrency",
                "label": "shared concurrency notes",
            }
        ],
    }


@pytest.fixture
def patched_llm(monkeypatch, llm_response_payload):
    """Stub recall_llm so tests don't hit the network."""
    fake_config = {"endpoint": "http://stub", "model": "stub-model"}

    monkeypatch.setattr(
        "mempalace.recall_llm._get_llm_config",
        lambda: fake_config,
    )
    monkeypatch.setattr(
        "mempalace.recall_llm._call_llm",
        lambda *a, **kw: json.dumps(llm_response_payload),
    )
    # _build_palace_context queries the real palace; short-circuit it so we
    # don't accidentally exercise unrelated code paths.
    monkeypatch.setattr(hooks_cli, "_build_palace_context", lambda: "")
    return llm_response_payload


# ── 1. Lock acquisition ────────────────────────────────────────────────


class TestAcquiresPalaceWriteLock:
    def test_async_save_worker_acquires_palace_write_lock(
        self, monkeypatch, isolated_palace, patched_llm
    ):
        """_async_save_worker MUST acquire palace_write_lock before any chroma write."""
        events: list[tuple[str, str | None]] = []

        @contextlib.contextmanager
        def _spy_lock(palace_path, timeout=30.0):
            events.append(("lock_in", str(palace_path)))
            yield
            events.append(("lock_out", str(palace_path)))

        # The lock symbol is imported INSIDE _async_save_worker, so patch
        # at the source.
        monkeypatch.setattr("mempalace.palace.palace_write_lock", _spy_lock)

        hooks_cli._async_save_worker(
            transcript_text="some transcript content for the LLM",
            session_id="test-session-1",
            cwd="/tmp/some_project",
        )

        lock_events = [e for e in events if e[0] in ("lock_in", "lock_out")]
        assert len(lock_events) >= 2, f"expected at least one lock acquire+release, got {events}"
        assert lock_events[0][0] == "lock_in"
        # Must be acquired with the real palace path (resolved via config).
        assert lock_events[0][1] == isolated_palace
        assert lock_events[-1][0] == "lock_out"


# ── 2. refresh_for_write ordering ──────────────────────────────────────


class TestRefreshForWriteOrdering:
    def test_async_save_worker_calls_refresh_for_write_before_writes(
        self, monkeypatch, isolated_palace, patched_llm
    ):
        """refresh_for_write MUST be called inside the lock, before the first add/upsert."""
        order: list[str] = []

        @contextlib.contextmanager
        def _spy_lock(palace_path, timeout=30.0):
            order.append("lock_in")
            yield
            order.append("lock_out")

        monkeypatch.setattr("mempalace.palace.palace_write_lock", _spy_lock)

        # Wrap get_collection so we can spy on the returned collection.
        from mempalace import palace as palace_mod

        original_get_collection = palace_mod.get_collection

        def _wrapped_get_collection(palace_path, **kwargs):
            real = original_get_collection(palace_path, **kwargs)
            spy = MagicMock(wraps=real)

            real_refresh = getattr(real, "refresh_for_write", lambda: None)

            def _spy_refresh():
                order.append("refresh_for_write")
                return real_refresh()

            spy.refresh_for_write = MagicMock(side_effect=_spy_refresh)

            real_add = real.add
            real_upsert = real.upsert

            def _spy_add(*args, **kwargs):
                order.append("add")
                return real_add(*args, **kwargs)

            def _spy_upsert(*args, **kwargs):
                order.append("upsert")
                return real_upsert(*args, **kwargs)

            spy.add = MagicMock(side_effect=_spy_add)
            spy.upsert = MagicMock(side_effect=_spy_upsert)
            return spy

        monkeypatch.setattr("mempalace.palace.get_collection", _wrapped_get_collection)

        hooks_cli._async_save_worker(
            transcript_text="some transcript content for the LLM",
            session_id="test-session-2",
            cwd="/tmp/some_project",
        )

        assert "lock_in" in order, f"lock not acquired; order={order}"
        assert "refresh_for_write" in order, f"refresh_for_write not called; order={order}"
        assert any(ev in ("add", "upsert") for ev in order), (
            f"no chroma write recorded; order={order}"
        )

        lock_in = order.index("lock_in")
        refresh = order.index("refresh_for_write")
        first_write = min(i for i, ev in enumerate(order) if ev in ("add", "upsert"))
        lock_out = order.index("lock_out")

        assert lock_in < refresh < first_write < lock_out, (
            f"refresh did not run inside lock before first write: {order}"
        )


# ── 3. Recovery WAL on timeout ─────────────────────────────────────────


class TestRecoveryWalOnTimeout:
    def test_async_save_worker_persists_to_recovery_wal_on_timeout(
        self, monkeypatch, isolated_palace, patched_llm
    ):
        """When palace_write_lock times out, the unwritten payload must be persisted."""

        @contextlib.contextmanager
        def _timeout_lock(palace_path, timeout=30.0):
            raise PalaceWriteLockTimeout("simulated contention")
            yield  # pragma: no cover

        monkeypatch.setattr("mempalace.palace.palace_write_lock", _timeout_lock)

        with pytest.raises(SystemExit) as excinfo:
            hooks_cli._async_save_worker(
                transcript_text="some transcript content for the LLM",
                session_id="test-session-timeout",
                cwd="/tmp/some_project",
            )
        # Clean exit so the hook chain is not disrupted.
        assert excinfo.value.code == 0

        # A recovery file should now exist for this palace.
        recovery_dir = recovery_wal.recovery_dir_for_palace(isolated_palace)
        assert recovery_dir.is_dir(), f"recovery dir not created at {recovery_dir}"
        files = list(recovery_dir.glob("*.jsonl"))
        assert len(files) == 1, f"expected one recovery file, got {files}"

        # And the file must contain the diary + drawer + kg + tunnel records.
        records = [json.loads(line) for line in files[0].read_text().splitlines() if line.strip()]
        ops = [r["op"] for r in records]
        assert "context" in ops, f"missing context record; got ops={ops}"
        assert "diary" in ops, f"missing diary record; got ops={ops}"
        assert ops.count("drawer") == 2, f"expected 2 drawer records; got ops={ops}"
        assert "kg_triple" in ops, f"missing kg_triple record; got ops={ops}"
        assert "tunnel" in ops, f"missing tunnel record; got ops={ops}"


# ── 4. Recovery WAL path format ───────────────────────────────────────


class TestRecoveryWalPathFormat:
    def test_recovery_wal_path_format(self, tmp_path):
        """Recovery files land at ~/.mempalace/recovery/<palace_id>/<timestamp>_<pid>.jsonl."""
        palace = tmp_path / "fmt_palace"
        palace.mkdir()
        path = recovery_wal.persist_async_save_payload(
            str(palace),
            diary={"id": "x", "document": "verbatim", "metadata": {}},
            pid=99999,
        )
        # Parent must be ~/.mempalace/recovery/<palace_id>/
        expected_parent = Path(os.path.expanduser("~")) / ".mempalace" / "recovery"
        assert expected_parent in path.parents, (
            f"recovery file should live under {expected_parent}, got {path}"
        )
        # The directly-enclosing dir name is the palace_id (16 hex chars).
        palace_id_dir = path.parent.name
        assert len(palace_id_dir) == 16, f"palace_id should be 16 chars, got {palace_id_dir!r}"
        assert all(c in "0123456789abcdef" for c in palace_id_dir), (
            f"palace_id should be hex, got {palace_id_dir!r}"
        )
        # Filename: <timestamp>_<pid>.jsonl
        assert path.name.endswith(f"_{99999}.jsonl"), (
            f"filename should end with _<pid>.jsonl; got {path.name!r}"
        )


# ── 5. No data loss on timeout ────────────────────────────────────────


class TestNoDataLossOnTimeout:
    def test_async_save_worker_does_not_lose_data_on_lock_timeout(
        self, monkeypatch, isolated_palace, patched_llm, llm_response_payload
    ):
        """The payload landing in the WAL must contain the LLM-extracted content verbatim."""

        @contextlib.contextmanager
        def _timeout_lock(palace_path, timeout=30.0):
            raise PalaceWriteLockTimeout("simulated contention")
            yield  # pragma: no cover

        monkeypatch.setattr("mempalace.palace.palace_write_lock", _timeout_lock)

        with pytest.raises(SystemExit):
            hooks_cli._async_save_worker(
                transcript_text="some transcript content for the LLM",
                session_id="test-session-content",
                cwd="/tmp/some_project",
            )

        recovery_dir = recovery_wal.recovery_dir_for_palace(isolated_palace)
        files = list(recovery_dir.glob("*.jsonl"))
        assert len(files) == 1
        records = [json.loads(line) for line in files[0].read_text().splitlines() if line.strip()]

        # Diary text is preserved verbatim in the diary record.
        diary_records = [r for r in records if r["op"] == "diary"]
        assert len(diary_records) == 1
        assert llm_response_payload["diary"] in diary_records[0]["args"]["document"]

        # Drawer contents preserved verbatim.
        drawer_records = [r for r in records if r["op"] == "drawer"]
        assert len(drawer_records) == 2
        drawer_docs = [r["args"]["document"] for r in drawer_records]
        for original in llm_response_payload["drawers"]:
            assert any(original["content"] in doc for doc in drawer_docs), (
                f"original drawer content {original['content']!r} missing from {drawer_docs}"
            )

        # KG triple preserved.
        kg_records = [r for r in records if r["op"] == "kg_triple"]
        assert len(kg_records) == 1
        assert kg_records[0]["args"] == llm_response_payload["kg"][0]

        # Tunnel preserved.
        tunnel_records = [r for r in records if r["op"] == "tunnel"]
        assert len(tunnel_records) == 1
        assert tunnel_records[0]["args"]["source_wing"] == "mempalace"
        assert tunnel_records[0]["args"]["target_wing"] == "other_wing"


# ── 6. Idempotent recovery WAL writes ─────────────────────────────────


class TestRecoveryWalIdempotent:
    def test_recovery_wal_idempotent_writes(self, tmp_path):
        """Two timeouts in a row must produce two SEPARATE files (no clobber)."""
        palace = tmp_path / "idempotent_palace"
        palace.mkdir()

        path1 = recovery_wal.persist_async_save_payload(
            str(palace),
            diary={"id": "first", "document": "first payload verbatim", "metadata": {}},
        )
        path2 = recovery_wal.persist_async_save_payload(
            str(palace),
            diary={"id": "second", "document": "second payload verbatim", "metadata": {}},
        )

        assert path1 != path2, "two recovery writes produced the same path (clobber!)"
        assert path1.exists(), f"first recovery file missing: {path1}"
        assert path2.exists(), f"second recovery file missing: {path2}"

        recs1 = [json.loads(line) for line in path1.read_text().splitlines() if line.strip()]
        recs2 = [json.loads(line) for line in path2.read_text().splitlines() if line.strip()]

        assert any(r["args"].get("id") == "first" for r in recs1 if r["op"] == "diary")
        assert any(r["args"].get("id") == "second" for r in recs2 if r["op"] == "diary")


# ── 7. Clean exit on lock timeout ─────────────────────────────────────


class TestExitCleanOnLockTimeout:
    def test_async_save_worker_exits_clean_on_lock_timeout(
        self, monkeypatch, isolated_palace, patched_llm
    ):
        """On lock timeout, _async_save_worker must SystemExit with code 0.

        A non-zero exit would propagate up the stop hook chain and surface
        as a noisy error to the user — but losing the payload silently is
        what we're already preventing via the recovery WAL, so we exit
        clean and let the next save (or the drainer) replay the payload.
        """

        @contextlib.contextmanager
        def _timeout_lock(palace_path, timeout=30.0):
            raise PalaceWriteLockTimeout("simulated contention")
            yield  # pragma: no cover

        monkeypatch.setattr("mempalace.palace.palace_write_lock", _timeout_lock)

        with pytest.raises(SystemExit) as excinfo:
            hooks_cli._async_save_worker(
                transcript_text="some transcript content for the LLM",
                session_id="test-session-cleanexit",
                cwd="/tmp/some_project",
            )

        # SystemExit code 0 — explicitly. None or non-zero would be wrong.
        assert excinfo.value.code == 0, (
            f"expected exit code 0 on lock timeout, got {excinfo.value.code!r}"
        )
