"""Tests for the singleton manager UDS socket readiness probe."""

from __future__ import annotations

import socket
import threading
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from mempalace import singleton_manager


@pytest.fixture
def short_sockdir(tmp_path):
    """macOS caps AF_UNIX paths at 104 bytes; pytest's tmp_path is too long.

    Use /private/tmp/<uuid> so binds succeed on Darwin too. In the sandboxed
    test runner, /tmp may reject AF_UNIX bind() even though /private/tmp is
    writable.
    """
    base = Path("/private/tmp") / f"mp-singleton-{uuid.uuid4().hex[:12]}"
    base.mkdir(parents=True, exist_ok=True)
    yield base
    for p in base.glob("*"):
        try:
            p.unlink()
        except OSError:
            pass
    try:
        base.rmdir()
    except OSError:
        pass


def _bind_uds_server(path: Path) -> socket.socket:
    """Bind a real AF_UNIX SOCK_STREAM server at ``path`` and return the socket."""
    if path.exists():
        path.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(path))
    except PermissionError as exc:
        server.close()
        pytest.skip(f"AF_UNIX bind is not permitted in this sandbox: {exc}")
    server.listen(4)
    return server


def test_socket_reachable_returns_false_when_path_missing(short_sockdir):
    sock_path = short_sockdir / "missing.sock"
    with patch.object(singleton_manager, "SOCKET_PATH", sock_path):
        ready, err = singleton_manager._socket_reachable()
    assert ready is False
    assert err == "socket path does not exist"


def test_socket_reachable_returns_false_for_stale_unbound_file(short_sockdir):
    """A leftover socket file that nothing is listening on must not look ready."""
    sock_path = short_sockdir / "stale.sock"
    sock_path.write_bytes(b"")  # plain file at the path, no listener bound

    with patch.object(singleton_manager, "SOCKET_PATH", sock_path):
        ready, err = singleton_manager._socket_reachable()
    assert ready is False
    assert err is not None


def test_socket_reachable_returns_true_when_listener_bound(short_sockdir):
    sock_path = short_sockdir / "live.sock"
    server = _bind_uds_server(sock_path)
    try:
        with patch.object(singleton_manager, "SOCKET_PATH", sock_path):
            ready, err = singleton_manager._socket_reachable()
        assert ready is True
        assert err is None
    finally:
        server.close()
        if sock_path.exists():
            sock_path.unlink()


def test_wait_for_socket_returns_promptly_on_ready(short_sockdir):
    sock_path = short_sockdir / "ready.sock"
    server = _bind_uds_server(sock_path)
    try:
        with patch.object(singleton_manager, "SOCKET_PATH", sock_path):
            ready, elapsed = singleton_manager._wait_for_socket(timeout_s=5.0)
        assert ready is True
        # First probe runs before any sleep — should be near-instant.
        assert elapsed < 1.0
    finally:
        server.close()
        if sock_path.exists():
            sock_path.unlink()


def test_wait_for_socket_succeeds_after_delayed_bind(short_sockdir):
    """Simulates the cold-start race: socket binds ~0.5s after we start probing."""
    sock_path = short_sockdir / "delayed.sock"
    probe_path = short_sockdir / "probe.sock"
    probe = _bind_uds_server(probe_path)
    probe.close()
    if probe_path.exists():
        probe_path.unlink()
    server_holder: dict = {}

    def _bind_after_delay():
        time.sleep(0.5)
        server_holder["server"] = _bind_uds_server(sock_path)

    binder = threading.Thread(target=_bind_after_delay, daemon=True)
    binder.start()
    try:
        with patch.object(singleton_manager, "SOCKET_PATH", sock_path):
            ready, elapsed = singleton_manager._wait_for_socket(timeout_s=5.0)
        assert ready is True
        assert 0.4 <= elapsed < 5.0
    finally:
        binder.join(timeout=2.0)
        srv = server_holder.get("server")
        if srv is not None:
            srv.close()
        if sock_path.exists():
            sock_path.unlink()


def test_wait_for_socket_times_out_when_never_ready(short_sockdir):
    sock_path = short_sockdir / "never.sock"
    with patch.object(singleton_manager, "SOCKET_PATH", sock_path):
        ready, elapsed = singleton_manager._wait_for_socket(timeout_s=0.4)
    assert ready is False
    assert elapsed >= 0.4


def test_cmd_start_reports_ready_with_elapsed_time(short_sockdir, capsys):
    sock_path = short_sockdir / "started.sock"
    server = _bind_uds_server(sock_path)
    try:
        with patch.object(singleton_manager, "SOCKET_PATH", sock_path):
            with patch.object(singleton_manager, "_dispatch") as mock_dispatch:
                singleton_manager.cmd_start(args=None)
        out = capsys.readouterr().out
        mock_dispatch.assert_called_once_with("start")
        assert "singleton ready" in out
        assert "did not" not in out
    finally:
        server.close()
        if sock_path.exists():
            sock_path.unlink()


def test_cmd_start_reports_warning_when_singleton_not_ready(short_sockdir, capsys, monkeypatch):
    sock_path = short_sockdir / "ghost.sock"
    monkeypatch.setenv("MEMPAL_SINGLETON_READY_TIMEOUT", "0.3")
    with patch.object(singleton_manager, "SOCKET_PATH", sock_path):
        with patch.object(singleton_manager, "_dispatch"):
            singleton_manager.cmd_start(args=None)
    out = capsys.readouterr().out
    assert "not ready" in out
    assert "mcp.err.log" in out


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
