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
        import socket

        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.5)
        try:
            s.connect(str(SOCKET_PATH))
            s.close()
            print("reachable: yes")
        except OSError as exc:
            print(f"reachable: no ({exc})")


# ---------------------------------------------------------------------------
# CLI entry points (wired from mempalace/cli.py)
# ---------------------------------------------------------------------------


def cmd_install(args) -> None:
    _dispatch("install")
    if getattr(args, "start", False):
        cmd_start(args)


def cmd_start(args) -> None:
    _dispatch("start")
    # Allow a short grace period for the socket to appear.
    for _ in range(20):
        if SOCKET_PATH.exists():
            return
        time.sleep(0.1)
    print("  ⚠ socket did not appear within 2s — check logs under ~/.mempalace/logs/")


def cmd_stop(args) -> None:
    _dispatch("stop")


def cmd_uninstall(args) -> None:
    _dispatch("uninstall")


def cmd_status(args) -> None:
    _status()
