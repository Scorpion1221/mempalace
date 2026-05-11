"""test_mcp_server_concurrency.py — Wave 2 concurrency-fix tests for the MCP server.

These tests verify that the four MCP write tools
(``tool_add_drawer``, ``tool_delete_drawer``, ``tool_update_drawer``,
``tool_diary_write``) honor ``palace_write_lock`` and call
``refresh_for_write`` *inside* the lock — so racing writers cannot
corrupt the HNSW segment via stale in-memory state.

The startup health-check test verifies that a corrupt palace at boot
emits a stderr warning but does NOT prevent the server from starting.
"""

from __future__ import annotations

import contextlib
import threading
from unittest.mock import MagicMock

from mempalace.palace import PalaceWriteLockTimeout


def _patch_mcp_server(monkeypatch, config, kg):
    """Patch the mcp_server module globals to use test fixtures."""
    from mempalace import mcp_server

    monkeypatch.setattr(mcp_server, "_config", config)
    monkeypatch.setattr(mcp_server, "_get_kg", lambda: kg)
    monkeypatch.setattr(mcp_server, "_client_cache", None)
    monkeypatch.setattr(mcp_server, "_collection_cache", None)
    monkeypatch.setattr(mcp_server, "_collection_has_ef", False)
    monkeypatch.setattr(mcp_server, "_palace_db_inode", 0)
    monkeypatch.setattr(mcp_server, "_palace_db_mtime", 0.0)


def _force_create_palace(palace_path):
    """Bootstrap an empty chromadb palace so ``_get_collection`` succeeds."""
    import chromadb

    client = chromadb.PersistentClient(path=palace_path)
    client.get_or_create_collection("mempalace_drawers", metadata={"hnsw:space": "cosine"})
    del client


# ── 1. lock acquired with the right path ──────────────────────────────


class TestLockAcquisition:
    def test_add_drawer_acquires_lock(self, monkeypatch, config, palace_path, kg):
        """tool_add_drawer must call palace_write_lock with cfg.palace_path."""
        _patch_mcp_server(monkeypatch, config, kg)
        _force_create_palace(palace_path)

        from mempalace import mcp_server

        observed = {}

        @contextlib.contextmanager
        def _spy_lock(path, timeout=30.0):
            observed["path"] = path
            observed["timeout"] = timeout
            yield

        monkeypatch.setattr(mcp_server, "palace_write_lock", _spy_lock)

        result = mcp_server.tool_add_drawer(
            wing="lockwing", room="lockroom", content="locked content one"
        )
        assert result["success"] is True
        assert observed["path"] == config.palace_path
        # Default timeout from the constant
        assert observed["timeout"] == mcp_server._PALACE_WRITE_LOCK_TIMEOUT_S

    def test_delete_drawer_acquires_lock(
        self, monkeypatch, config, palace_path, seeded_collection, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        observed = {}

        @contextlib.contextmanager
        def _spy_lock(path, timeout=30.0):
            observed["path"] = path
            yield

        monkeypatch.setattr(mcp_server, "palace_write_lock", _spy_lock)

        result = mcp_server.tool_delete_drawer("drawer_proj_backend_aaa")
        assert result["success"] is True
        assert observed["path"] == config.palace_path

    def test_update_drawer_acquires_lock(
        self, monkeypatch, config, palace_path, seeded_collection, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        observed = {}

        @contextlib.contextmanager
        def _spy_lock(path, timeout=30.0):
            observed["path"] = path
            yield

        monkeypatch.setattr(mcp_server, "palace_write_lock", _spy_lock)

        result = mcp_server.tool_update_drawer(
            "drawer_proj_backend_aaa", content="rewritten verbatim text"
        )
        assert result["success"] is True
        assert observed["path"] == config.palace_path

    def test_diary_write_acquires_lock(self, monkeypatch, config, palace_path, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        _force_create_palace(palace_path)
        from mempalace import mcp_server

        observed = {}

        @contextlib.contextmanager
        def _spy_lock(path, timeout=30.0):
            observed["path"] = path
            yield

        monkeypatch.setattr(mcp_server, "palace_write_lock", _spy_lock)

        result = mcp_server.tool_diary_write(agent_name="LockTester", entry="diary inside lock")
        assert result["success"] is True
        assert observed["path"] == config.palace_path


# ── 2. lock timeout is graceful, not fatal ────────────────────────────


class TestLockTimeout:
    def test_add_drawer_timeout_returns_error_not_crash(self, monkeypatch, config, palace_path, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        _force_create_palace(palace_path)
        from mempalace import mcp_server

        @contextlib.contextmanager
        def _always_timeout(path, timeout=30.0):
            raise PalaceWriteLockTimeout("simulated contention")
            yield  # pragma: no cover

        monkeypatch.setattr(mcp_server, "palace_write_lock", _always_timeout)

        result = mcp_server.tool_add_drawer(
            wing="timeoutwing", room="timeoutroom", content="never makes it in"
        )
        # The handler caught the exception and returned a structured error.
        assert result["success"] is False
        assert "timeout" in result["error"].lower()

    def test_delete_drawer_timeout_returns_error_not_crash(
        self, monkeypatch, config, palace_path, seeded_collection, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        @contextlib.contextmanager
        def _always_timeout(path, timeout=30.0):
            raise PalaceWriteLockTimeout("simulated contention")
            yield  # pragma: no cover

        monkeypatch.setattr(mcp_server, "palace_write_lock", _always_timeout)

        result = mcp_server.tool_delete_drawer("drawer_proj_backend_aaa")
        assert result["success"] is False
        assert "timeout" in result["error"].lower()
        # The drawer must still be present — the delete never executed.
        assert seeded_collection.count() == 4

    def test_update_drawer_timeout_returns_error_not_crash(
        self, monkeypatch, config, palace_path, seeded_collection, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        @contextlib.contextmanager
        def _always_timeout(path, timeout=30.0):
            raise PalaceWriteLockTimeout("simulated contention")
            yield  # pragma: no cover

        monkeypatch.setattr(mcp_server, "palace_write_lock", _always_timeout)

        result = mcp_server.tool_update_drawer("drawer_proj_backend_aaa", content="new content")
        assert result["success"] is False
        assert "timeout" in result["error"].lower()
        # Original content unchanged.
        existing = seeded_collection.get(ids=["drawer_proj_backend_aaa"])
        assert "JWT" in existing["documents"][0]

    def test_diary_write_timeout_returns_error_not_crash(
        self, monkeypatch, config, palace_path, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        _force_create_palace(palace_path)
        from mempalace import mcp_server

        @contextlib.contextmanager
        def _always_timeout(path, timeout=30.0):
            raise PalaceWriteLockTimeout("simulated contention")
            yield  # pragma: no cover

        monkeypatch.setattr(mcp_server, "palace_write_lock", _always_timeout)

        result = mcp_server.tool_diary_write(agent_name="ToWho", entry="lost diary entry")
        assert result["success"] is False
        assert "timeout" in result["error"].lower()


# ── 3. refresh_for_write is called INSIDE the lock ────────────────────


class TestRefreshOrdering:
    def test_refresh_for_write_called_inside_lock_for_add(
        self, monkeypatch, config, palace_path, kg
    ):
        """``refresh_for_write`` must be called after lock entry, before write."""
        _patch_mcp_server(monkeypatch, config, kg)
        _force_create_palace(palace_path)
        from mempalace import mcp_server

        order = []

        @contextlib.contextmanager
        def _record_lock(path, timeout=30.0):
            order.append("lock_acquired")
            yield
            order.append("lock_released")

        monkeypatch.setattr(mcp_server, "palace_write_lock", _record_lock)

        # Wrap the *real* collection so we can observe call ordering without
        # losing functionality. We patch the module-level ``_get_collection``
        # to return our spy wrapper exactly once for this call.
        real_col = mcp_server._get_collection(create=True)
        assert real_col is not None

        spy = MagicMock(wraps=real_col)
        spy.refresh_for_write = MagicMock(side_effect=lambda: order.append("refresh_for_write"))
        original_upsert = real_col.upsert

        def _spy_upsert(*args, **kwargs):
            order.append("upsert")
            return original_upsert(*args, **kwargs)

        spy.upsert = MagicMock(side_effect=_spy_upsert)

        monkeypatch.setattr(mcp_server, "_get_collection", lambda create=False: spy)

        result = mcp_server.tool_add_drawer(
            wing="orderwing", room="orderroom", content="ordering matters here"
        )
        assert result["success"] is True

        assert "lock_acquired" in order
        assert "refresh_for_write" in order
        assert "upsert" in order
        assert "lock_released" in order

        # Strict ordering: refresh and write must both fall between
        # lock_acquired and lock_released.
        ack = order.index("lock_acquired")
        rel = order.index("lock_released")
        rfw = order.index("refresh_for_write")
        ups = order.index("upsert")
        assert ack < rfw < rel
        assert ack < ups < rel
        # And refresh must precede the actual write.
        assert rfw < ups

    def test_refresh_for_write_called_for_delete(
        self, monkeypatch, config, palace_path, seeded_collection, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        called = {"refresh": False}

        @contextlib.contextmanager
        def _enter_lock(path, timeout=30.0):
            yield

        monkeypatch.setattr(mcp_server, "palace_write_lock", _enter_lock)

        real_col = mcp_server._get_collection()
        assert real_col is not None
        spy = MagicMock(wraps=real_col)
        spy.refresh_for_write = MagicMock(side_effect=lambda: called.__setitem__("refresh", True))
        spy.delete = real_col.delete  # let the real delete run

        # _get_collection is called twice in tool_delete_drawer (the read
        # probe for existing, then the spy for the lock body). We swap only
        # after the probe so the existing-row check doesn't go through the
        # spy. The simplest approach: route both calls through the spy and
        # just ensure refresh+delete are observable.
        monkeypatch.setattr(mcp_server, "_get_collection", lambda create=False: spy)

        result = mcp_server.tool_delete_drawer("drawer_proj_backend_aaa")
        assert result["success"] is True
        assert called["refresh"] is True


# ── 4. concurrent writers serialize correctly ─────────────────────────


class TestConcurrentSerialization:
    def test_concurrent_add_drawer_serializes(self, monkeypatch, config, palace_path, kg):
        """Two threads racing tool_add_drawer must both succeed and both be present.

        The real ``palace_write_lock`` enforces cross-process exclusion via
        fcntl/msvcrt; within a single process two threads still observe it
        (``flock`` semantics on Linux/macOS allow re-acquisition only after
        release). We rely on chromadb's own internal thread safety in
        addition to the lock — the assertion is "no exceptions, both rows
        land, count is exactly two".
        """
        _patch_mcp_server(monkeypatch, config, kg)
        _force_create_palace(palace_path)
        from mempalace import mcp_server

        # Pre-warm the cached collection so both threads see the same instance
        # and don't race on cache initialisation (which is unrelated to the
        # behaviour under test).
        col = mcp_server._get_collection(create=True)
        assert col is not None

        results = {}
        errors = []
        barrier = threading.Barrier(2)

        def _writer(idx, content):
            try:
                barrier.wait(timeout=10.0)
                res = mcp_server.tool_add_drawer(
                    wing="racewing",
                    room="raceroom",
                    content=content,
                )
                results[idx] = res
            except Exception as exc:  # pragma: no cover - defensive
                errors.append((idx, repr(exc)))

        t1 = threading.Thread(
            target=_writer, args=(1, "first racer payload — deterministic content one")
        )
        t2 = threading.Thread(
            target=_writer, args=(2, "second racer payload — deterministic content two")
        )
        t1.start()
        t2.start()
        t1.join(timeout=30.0)
        t2.join(timeout=30.0)

        assert not errors, f"writers raised: {errors}"
        assert results[1]["success"] is True
        assert results[2]["success"] is True
        assert results[1]["drawer_id"] != results[2]["drawer_id"]

        # Both rows must be present in the persisted collection.
        ids_in_col = set(col.get()["ids"])
        assert results[1]["drawer_id"] in ids_in_col
        assert results[2]["drawer_id"] in ids_in_col


# ── 5. startup health check is non-fatal on corruption ────────────────


class TestStartupHealthCheck:
    def test_startup_health_check_does_not_block_serve_on_corrupt(
        self, monkeypatch, config, palace_path, kg, capsys
    ):
        """Corrupt palace at boot must emit a stderr warning, then return cleanly.

        We invoke the helper directly instead of ``main()`` (which would
        block on stdin). The contract is: never raise, never sys.exit, just
        log to stderr.
        """
        _patch_mcp_server(monkeypatch, config, kg)
        # Bootstrap a real palace, then maul chroma.sqlite3 so health
        # reports ``corrupt``.
        _force_create_palace(palace_path)
        import os

        db_path = os.path.join(palace_path, "chroma.sqlite3")
        with open(db_path, "wb") as fh:
            fh.write(b"\x00\xffNOT A VALID SQLITE FILE\x00" * 200)

        from mempalace import mcp_server

        # Should not raise.
        mcp_server._run_startup_health_check()

        captured = capsys.readouterr()
        assert "[mempalace]" in captured.err
        # The corrupt branch mentions the recovery commands.
        assert "doctor" in captured.err or "repair" in captured.err

    def test_startup_health_check_swallows_unexpected_exceptions(
        self, monkeypatch, config, palace_path, kg, capsys
    ):
        """If check_palace_health raises something unexpected, we still come up."""
        _patch_mcp_server(monkeypatch, config, kg)
        _force_create_palace(palace_path)
        from mempalace import mcp_server
        from mempalace import health as health_mod

        def _boom(*_args, **_kwargs):
            raise RuntimeError("synthetic health-check explosion")

        monkeypatch.setattr(health_mod, "check_palace_health", _boom)

        # Must not raise.
        mcp_server._run_startup_health_check()

        captured = capsys.readouterr()
        assert "non-fatal" in captured.err

    def test_startup_health_check_skips_when_palace_dir_missing(
        self, monkeypatch, config, kg, tmp_dir, capsys
    ):
        """Fresh-install case: no palace dir yet → silent no-op, no scary warning."""
        import os

        missing_path = os.path.join(tmp_dir, "this_dir_does_not_exist")
        # Override the config so palace_path points at the missing dir.
        config_mock = MagicMock()
        config_mock.palace_path = missing_path

        from mempalace import mcp_server

        monkeypatch.setattr(mcp_server, "_config", config_mock)

        mcp_server._run_startup_health_check()

        captured = capsys.readouterr()
        assert "WARNING" not in captured.err
        assert "non-fatal" not in captured.err

    def test_startup_health_check_quiet_for_ok_palace(
        self, monkeypatch, config, palace_path, kg, capsys
    ):
        """A clean palace must produce no stderr output at all."""
        _patch_mcp_server(monkeypatch, config, kg)
        _force_create_palace(palace_path)
        # Add at least one drawer so SQLite count > 0 and cross-checks run.
        import chromadb

        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_or_create_collection(
            "mempalace_drawers", metadata={"hnsw:space": "cosine"}
        )
        col.add(
            ids=["drawer_quiet_check_aaa"],
            documents=["clean palace single drawer"],
            metadatas=[{"wing": "quiet", "room": "check"}],
        )
        del col
        del client

        from mempalace import mcp_server

        mcp_server._run_startup_health_check()

        captured = capsys.readouterr()
        assert captured.err == ""


# ── 6. lock timeout error is structured + replayable ──────────────────


class TestErrorShape:
    def test_timeout_error_message_actionable(self, monkeypatch, config, palace_path, kg):
        """The error string should hint at lock contention so operators can act."""
        _patch_mcp_server(monkeypatch, config, kg)
        _force_create_palace(palace_path)
        from mempalace import mcp_server

        @contextlib.contextmanager
        def _always_timeout(path, timeout=30.0):
            raise PalaceWriteLockTimeout("simulated")
            yield  # pragma: no cover

        monkeypatch.setattr(mcp_server, "palace_write_lock", _always_timeout)

        result = mcp_server.tool_add_drawer(wing="w", room="r", content="hint please")
        assert result["success"] is False
        assert any(token in result["error"].lower() for token in ("lock", "timeout"))
