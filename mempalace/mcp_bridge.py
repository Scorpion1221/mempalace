#!/usr/bin/env python3
"""mempalace-mcp-bridge — stdio MCP bridge to the shared MemPalace UDS server.

Default behavior:
- If ~/.mempalace/mcp.sock is reachable, proxy JSON-RPC line requests over UDS.
- If not reachable, fall back to spawning a local ``mempalace-mcp`` subprocess
  and piping stdio through unchanged.

Opt-out switch:
- ``MEMPAL_NO_SINGLETON=1`` forces the subprocess fallback even when the UDS
  socket is reachable. Use this for debugging one-off runs without touching
  the singleton.

This lets agent hosts keep using stdio MCP while sharing one long-lived local
MemPalace server when available.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys


SOCKET_PATH = os.environ.get(
    "MEMPAL_MCP_SOCKET",
    os.path.join(os.path.expanduser("~"), ".mempalace", "mcp.sock"),
)
BUFFER_SIZE = 65536
CONNECT_TIMEOUT = float(os.environ.get("MEMPAL_MCP_BRIDGE_TIMEOUT", "0.5"))


def _uds_available(path: str) -> bool:
    if not os.path.exists(path):
        return False
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(CONNECT_TIMEOUT)
        sock.connect(path)
        sock.close()
        return True
    except OSError:
        return False


def _proxy_via_uds(path: str) -> int:
    while True:
        line = sys.stdin.readline()
        if not line:
            return 0
        line = line.strip()
        if not line:
            continue

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(path)
            sock.sendall((line + "\n").encode("utf-8"))
            data = b""
            while not data.endswith(b"\n"):
                chunk = sock.recv(BUFFER_SIZE)
                if not chunk:
                    break
                data += chunk
            if data:
                sys.stdout.write(data.decode("utf-8"))
                sys.stdout.flush()
        finally:
            sock.close()


def _proxy_via_subprocess(argv: list[str]) -> int:
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=sys.stderr,
        text=True,
        bufsize=1,
    )

    assert proc.stdin is not None
    assert proc.stdout is not None

    try:
        while True:
            line = sys.stdin.readline()
            if not line:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
                return proc.wait(timeout=5)
            proc.stdin.write(line)
            proc.stdin.flush()
            resp = proc.stdout.readline()
            if not resp:
                return proc.wait(timeout=5)
            sys.stdout.write(resp)
            sys.stdout.flush()
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    force_stdio = os.environ.get("MEMPAL_NO_SINGLETON") == "1"

    if not force_stdio and _uds_available(SOCKET_PATH):
        return _proxy_via_uds(SOCKET_PATH)

    return _proxy_via_subprocess(["mempalace-mcp", *argv])


if __name__ == "__main__":
    raise SystemExit(main())
