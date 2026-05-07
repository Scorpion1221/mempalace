"""Singleton manager for the shared MemPalace MCP server.

Wraps platform-specific service managers (launchd on macOS, systemd --user
on Linux) behind a uniform CLI. Users never need to edit plist/unit files
directly — `mempalace singleton install` stages them, `start/stop/status`
drives them, and `uninstall` tears them down.

On unsupported platforms the commands degrade to advisory output so the
bridge still works via its subprocess fallback path.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable


REPO_DIR = Path(__file__).resolve().parent.parent
LAUNCHD_TEMPLATE = REPO_DIR / "integrations" / "launchd" / "ai.mempalace.server.plist.template"
SYSTEMD_TEMPLATE = REPO_DIR / "integrations" / "systemd" / "mempalace-server.service.template"

LAUNCHD_LABEL = "ai.mempalace.server"
LAUNCHD_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"

SYSTEMD_UNIT_NAME = "mempalace-server.service"
SYSTEMD_UNIT_DIR = Path.home() / ".config" / "systemd" / "user"
SYSTEMD_UNIT_PATH = SYSTEMD_UNIT_DIR / SYSTEMD_UNIT_NAME

SOCKET_PATH = Path.home() / ".mempalace" / "mcp.sock"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _find_mempalace_mcp() -> str:
    """Resolve the absolute path of the ``mempalace-mcp`` entry script.

    launchd has no shell; it ignores ~/.zshrc and pyenv shims, so the plist
    MUST point at an absolute path. We prefer the currently active Python's
    Scripts directory (matching whatever ran `mempalace singleton install`),
    and fall back to PATH lookup.
    """
    candidate = Path(sys.executable).parent / "mempalace-mcp"
    if candidate.exists():
        return str(candidate)
    on_path = shutil.which("mempalace-mcp")
    if on_path:
        return on_path
    raise SystemExit(
        "Cannot find mempalace-mcp binary. Install MemPalace first with `pip install -e ~/git/mempalace`."
    )


def _render(template: Path, replacements: dict) -> str:
    text = template.read_text()
    for key, value in replacements.items():
        text = text.replace(key, value)
    return text


def _write_if_changed(path: Path, content: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text() == content:
        return False
    path.write_text(content)
    return True


def _run(cmd: list[str], *, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True, check=check)


# ---------------------------------------------------------------------------
# macOS (launchd)
# ---------------------------------------------------------------------------


def _macos_install() -> None:
    if not LAUNCHD_TEMPLATE.exists():
        raise SystemExit(f"launchd template missing: {LAUNCHD_TEMPLATE}")
    rendered = _render(
        LAUNCHD_TEMPLATE,
        {
            "@MEMPAL_MCP_PATH@": _find_mempalace_mcp(),
            "@HOME@": str(Path.home()),
            "@PATH@": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        },
    )
    changed = _write_if_changed(LAUNCHD_PLIST, rendered)
    (Path.home() / ".mempalace" / "logs").mkdir(parents=True, exist_ok=True)
    if changed:
        print(f"  ✓ wrote {LAUNCHD_PLIST}")
    else:
        print(f"  ✓ {LAUNCHD_PLIST} already up to date")


def _macos_bootstrap_args(verb: str) -> list[str]:
    # launchctl bootstrap/bootout need the GUI domain for a login session.
    uid = os.getuid()
    return ["launchctl", verb, f"gui/{uid}", str(LAUNCHD_PLIST)]


def _macos_start() -> None:
    _run(_macos_bootstrap_args("bootout"))  # best-effort
    result = _run(_macos_bootstrap_args("bootstrap"))
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        raise SystemExit(result.returncode)
    print("  ✓ launchd bootstrapped ai.mempalace.server")


def _macos_stop() -> None:
    result = _run(_macos_bootstrap_args("bootout"))
    if result.returncode not in (0, 36):  # 36 = not loaded
        sys.stderr.write(result.stderr)
        raise SystemExit(result.returncode)
    print("  ✓ launchd bootout complete")


def _macos_status() -> str:
    uid = os.getuid()
    result = _run(["launchctl", "print", f"gui/{uid}/{LAUNCHD_LABEL}"])
    return result.stdout + result.stderr


def _macos_uninstall() -> None:
    _macos_stop()
    if LAUNCHD_PLIST.exists():
        LAUNCHD_PLIST.unlink()
        print(f"  ✓ removed {LAUNCHD_PLIST}")
    else:
        print(f"  {LAUNCHD_PLIST} already absent")


# ---------------------------------------------------------------------------
# Linux (systemd --user)
# ---------------------------------------------------------------------------


def _linux_install() -> None:
    if not SYSTEMD_TEMPLATE.exists():
        raise SystemExit(f"systemd template missing: {SYSTEMD_TEMPLATE}")
    rendered = _render(
        SYSTEMD_TEMPLATE,
        {
            "@MEMPAL_MCP_PATH@": _find_mempalace_mcp(),
            "@HOME@": str(Path.home()),
            "@PATH@": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        },
    )
    changed = _write_if_changed(SYSTEMD_UNIT_PATH, rendered)
    (Path.home() / ".mempalace" / "logs").mkdir(parents=True, exist_ok=True)
    _run(["systemctl", "--user", "daemon-reload"])
    if changed:
        print(f"  ✓ wrote {SYSTEMD_UNIT_PATH}")
    else:
        print(f"  ✓ {SYSTEMD_UNIT_PATH} already up to date")


def _linux_start() -> None:
    _run(["systemctl", "--user", "enable", SYSTEMD_UNIT_NAME])
    result = _run(["systemctl", "--user", "restart", SYSTEMD_UNIT_NAME])
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        raise SystemExit(result.returncode)
    print("  ✓ systemd --user enabled + (re)started mempalace-server")


def _linux_stop() -> None:
    _run(["systemctl", "--user", "stop", SYSTEMD_UNIT_NAME])
    _run(["systemctl", "--user", "disable", SYSTEMD_UNIT_NAME])
    print("  ✓ systemd --user stopped + disabled mempalace-server")


def _linux_status() -> str:
    result = _run(["systemctl", "--user", "status", SYSTEMD_UNIT_NAME])
    return result.stdout + result.stderr


def _linux_uninstall() -> None:
    _linux_stop()
    if SYSTEMD_UNIT_PATH.exists():
        SYSTEMD_UNIT_PATH.unlink()
        _run(["systemctl", "--user", "daemon-reload"])
        print(f"  ✓ removed {SYSTEMD_UNIT_PATH}")
    else:
        print(f"  {SYSTEMD_UNIT_PATH} already absent")


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def _dispatch(action: str) -> None:
    system = platform.system()

    if system == "Darwin":
        {
            "install": _macos_install,
            "start": _macos_start,
            "stop": _macos_stop,
            "uninstall": _macos_uninstall,
        }[action]()
    elif system == "Linux":
        {
            "install": _linux_install,
            "start": _linux_start,
            "stop": _linux_stop,
            "uninstall": _linux_uninstall,
        }[action]()
    else:
        raise SystemExit(
            f"Unsupported platform for singleton manager: {system}. "
            "The bridge will still fall back to per-agent stdio subprocesses."
        )


def _socket_reachable(timeout_s: float = 0.5) -> tuple[bool, str | None]:
    """Probe the singleton UDS socket via connect().

    File existence alone is not a reliable readiness signal — a stale socket
    file may linger from a previous crashed process while a new launchd-spawned
    interpreter is still importing chromadb (cold start ~1-2s). connect() only
    succeeds once the listener thread has rebound the socket and is accepting.
    """
    if not SOCKET_PATH.exists():
        return False, "socket path does not exist"
    import socket as _socket

    s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    s.settimeout(timeout_s)
    try:
        s.connect(str(SOCKET_PATH))
        return True, None
    except OSError as exc:
        return False, str(exc)
    finally:
        try:
            s.close()
        except OSError:
            pass


def _wait_for_socket(timeout_s: float = 15.0) -> tuple[bool, float]:
    """Poll the singleton UDS socket until it accepts a connection or we time out.

    Returns (ready, elapsed_seconds). Backs off gently from 100ms to 500ms so
    we don't busy-loop while the interpreter cold-starts.
    """
    start = time.monotonic()
    delay = 0.1
    while True:
        ready, _ = _socket_reachable()
        if ready:
            return True, time.monotonic() - start
        elapsed = time.monotonic() - start
        if elapsed >= timeout_s:
            return False, elapsed
        time.sleep(delay)
        delay = min(delay + 0.1, 0.5)


def _status() -> None:
    system = platform.system()
    if system == "Darwin":
        print(_macos_status())
    elif system == "Linux":
        print(_linux_status())
    else:
        print(f"(singleton manager not supported on {system})")

    print("\n--- Socket check ---")
    print(f"path: {SOCKET_PATH}")
    print(f"exists: {SOCKET_PATH.exists()}")
    if SOCKET_PATH.exists():
        ready, err = _socket_reachable()
        if ready:
            print("reachable: yes")
        else:
            print(f"reachable: no ({err})")


# ---------------------------------------------------------------------------
# CLI entry points (wired from mempalace/cli.py)
# ---------------------------------------------------------------------------


def cmd_install(args) -> None:
    _dispatch("install")
    if getattr(args, "start", False):
        cmd_start(args)


def cmd_start(args) -> None:
    _dispatch("start")
    # 2s was too tight: launchd `bootstrap` returns once the job is registered,
    # but the Python interpreter still has to import chromadb (~1-2s cold) before
    # the singleton listener thread binds the UDS socket. Wait up to 15s with a
    # connect probe — only stale-socket-file scenarios get a false "ready".
    timeout = float(os.environ.get("MEMPAL_SINGLETON_READY_TIMEOUT", "15"))
    ready, elapsed = _wait_for_socket(timeout_s=timeout)
    if ready:
        print(f"  ✓ singleton ready ({elapsed:.1f}s)")
    else:
        print(f"  ⚠ singleton not ready after {elapsed:.1f}s — check ~/.mempalace/logs/mcp.err.log")


def cmd_stop(args) -> None:
    _dispatch("stop")


def cmd_uninstall(args) -> None:
    _dispatch("uninstall")


def cmd_status(args) -> None:
    _status()
