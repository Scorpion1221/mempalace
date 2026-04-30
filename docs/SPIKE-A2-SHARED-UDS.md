# A2 Spike — Shared UDS MCP Singleton

**Status**: Phase 0 (verification) + Phase 1 (bridge PoC) complete on 2026-04-30.

## Why this spike

Four independent `mempalace-mcp` stdio subprocesses (Claude Code, Codex,
Cursor, Hermes) were getting out of sync whenever `~/.mempalace/env`
changed, producing silent 400 responses from shells that still had the old
embedding model pinned.

Reading `mempalace/mcp_server.py` revealed that the server already starts a
Unix-domain socket listener at `~/.mempalace/mcp.sock` alongside stdio.
That makes it possible to have **one** server process per machine and have
every agent talk to it — the architecture Sir originally expected ("local
singleton, shared across agents").

This document captures what the spike actually proved, what it uncovered,
and what the portable-install migration must handle.

## Verified

1. **UDS entrypoint works end-to-end.**
   - Start `mempalace-mcp`.
   - `~/.mempalace/mcp.sock` appears (AF_UNIX, SOCK_STREAM).
   - Sending JSON-RPC `initialize`, `tools/list`, and
     `tools/call mempalace_status` over the socket returns the same
     payloads as the stdio path.
2. **Concurrent clients are fine.**
   - Two simultaneous requests (`mempalace_status` and
     `mempalace_search`) each got their own response; neither blocked the
     other and there was no cross-talk.
3. **Restart is self-healing (but dirty).**
   - After killing the process group the socket file was **not removed**
     (atexit did not fire under SIGTERM in a wrapper-shell launch). A
     subsequent `mempalace-mcp` start happily unlinked the stale socket
     and rebound it. So the system recovers without manual intervention,
     it just leaves stale files around between runs.
4. **The bridge prototype works.**
   - `bin/mempalace-mcp-bridge` proxies stdio to the UDS server when
     `mcp.sock` is reachable.
   - With `MEMPAL_NO_SINGLETON=1` (or when the socket is missing) it
     falls back to spawning a local `mempalace-mcp` subprocess and
     piping stdio through untouched.
   - Both paths returned byte-identical `initialize` responses during
     the spike.

## Known caveats we must design for

- **Stale socket after abrupt exits**: `_start_socket_listener`'s
  `atexit` cleanup is not reached under `kill -TERM`. Launchd/systemd
  wrappers should assume stale sockets are normal after a crash and
  refuse to promote them into "server is healthy" signals.
- **Socket cleanup relies on next start**: `mempalace-mcp.main()` already
  handles "file exists but nobody listening" by `os.unlink()` before
  `bind()`. That code path is spike-verified, so the bridge's
  `_uds_available()` probe is the right gatekeeper for clients: if a
  connect fails with `ECONNREFUSED`, fall back instead of silently
  returning errors.
- **Launchd sends SIGTERM to wrapper shells, not Python**: during the
  spike, the bash wrapper swallowed the signal and the Python process
  kept running. Any managed-service plist/unit must `exec` the Python
  entry-point directly (no login-shell wrappers). See the launchd
  template shipped under `integrations/launchd/` for the correct shape.
- **No protocol-level heartbeat**: there is currently no way to ask the
  UDS listener "are you fully booted, palace health is good"?
  `_run_startup_health_check` logs to stderr but does not expose
  readiness over the socket. The doctor subcommand we're adding in
  Phase 4 needs its own probe (accept a small JSON-RPC `initialize`,
  measure round-trip, and optionally inspect the sidecar PID file).
- **Cross-platform**: UDS is supported on both macOS and Linux, so the
  bridge is platform-agnostic. The singleton *manager* is not — macOS
  uses launchd, Linux uses systemd `--user`. The install layer must
  pick one per host. Neither is required for the bridge itself, which
  can always fall back to the local stdio path.

## Deliverables Phase 0 + 1

| File | Purpose |
|---|---|
| `bin/mempalace-mcp-bridge` | stdio↔UDS proxy with graceful fallback |
| `docs/SPIKE-A2-SHARED-UDS.md` | this document |
| `mempalace/mcp_server.py` | added `MEMPAL_MCP_DISABLE_SOCKET=1` escape hatch |

## Next phases (not yet done)

- **Phase 2**: ship managed-service templates (launchd + systemd user),
  plus a helper `mempalace singleton install|start|stop|status`.
- **Phase 3**: switch `sync-plugins.sh` to register `mempalace-mcp-bridge`
  as the `command` in every agent's MCP config. Preserve
  `MEMPAL_NO_SINGLETON=1` as a documented opt-out.
- **Phase 4**: extend `mempalace doctor` to probe the UDS socket, detect
  stale socket files, and warn when agents are still wired to the old
  `mempalace-mcp` command instead of the bridge.
- **Phase 5**: rewrite `INSTALL.md` and `docs/INSTALL-FOR-AGENTS.md` to
  describe the singleton-by-default architecture, including the Linux
  + macOS split for the managed-service layer.
