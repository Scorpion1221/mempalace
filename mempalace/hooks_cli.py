"""
Hook logic for MemPalace — Python implementation of session-start, stop, and precompact hooks.

Reads JSON from stdin, outputs JSON to stdout.
Supported hooks: session-start, stop, precompact
Supported harnesses: claude-code, codex (extensible to cursor, gemini, etc.)
"""

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

SAVE_INTERVAL = 15
STATE_DIR = Path.home() / ".mempalace" / "hook_state"
PALACE_ROOT = Path.home() / ".mempalace"

# Matches any CJK character (Chinese, Japanese kana, Korean hangul syllables).
# Used so the KG recall path keeps 2-char CJK bigrams from ``_tokenize``.
_CJK_CHAR_RE = re.compile(r"[一-鿿぀-ヿ가-힯]")

# UserPromptSubmit recall settings
USERPROMPT_RECALL_LIMIT = 5
USERPROMPT_RECALL_POOL = 10
USERPROMPT_MAX_SNIPPET_CHARS = 400
USERPROMPT_MAX_DISTANCE = 1.5
USERPROMPT_MIN_QUERY_LEN = 6
USERPROMPT_PREVIOUS_ASSISTANT_TAIL_CHARS = 500
USERPROMPT_BUDGET_SECONDS = 15

USERPROMPT_SKIP_PHRASES = frozenset(
    {
        "hi",
        "hello",
        "hey",
        "嗨",
        "你好",
        "ok",
        "okay",
        "好",
        "好的",
        "行",
        "嗯",
        "对",
        "cool",
        "nice",
        "great",
        "sounds good",
        "yes",
        "no",
        "是",
        "是的",
        "不",
        "不是",
        "continue",
        "go",
        "go on",
        "next",
        "继续",
        "thanks",
        "thank you",
        "thx",
        "谢谢",
        "done",
        "完成",
        "搞定",
        "stop",
        "quit",
        "exit",
    }
)
USERPROMPT_CONTEXTUAL_FOLLOWUP_PHRASES = frozenset({"continue", "go", "go on", "next", "继续"})
USERPROMPT_HARD_SKIP_PHRASES = USERPROMPT_SKIP_PHRASES - USERPROMPT_CONTEXTUAL_FOLLOWUP_PHRASES


def _detached_popen_kwargs() -> dict:
    """Kwargs that fully detach a Popen child so the hook process can exit.

    Without these, Windows holds the parent open until the child closes the
    inherited stdout/stderr handles — manifesting as "Stop hook hangs" at
    session end (#1268). On POSIX the parent can already exit (orphan
    reparents to init), but ``start_new_session`` makes the boundary
    explicit so signals to the hook don't propagate to the background mine.
    """
    kwargs: dict = {"stdin": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        flags = 0
        for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP", "CREATE_BREAKAWAY_FROM_JOB"):
            flags |= getattr(subprocess, name, 0)
        if flags:
            kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True
    return kwargs


def _palace_root_exists() -> bool:
    """User-removable kill-switch.

    If ~/.mempalace/ does not exist, the user has explicitly cleared it.
    All hook side effects (logging, state dir creation, mining, ingestion)
    must respect this and short-circuit BEFORE touching disk — including
    before logging the short-circuit itself.

    Uses ``is_dir()`` rather than ``exists()`` so a stray regular file at
    ``~/.mempalace`` (or a broken symlink) is treated as absent — otherwise
    the kill-switch would be bypassed and ``STATE_DIR.mkdir()`` would later
    crash on ``NotADirectoryError``.
    """
    # STATE_DIR is the operative root used by tests and by installed hooks
    # (`~/.mempalace/hook_state` by default). Checking its parent preserves the
    # user-removable kill switch while letting tests redirect state safely.
    return STATE_DIR.parent.is_dir()


def _mempalace_python() -> str:
    """Return the python interpreter that has mempalace installed.

    When hooks are invoked by Claude Code, sys.executable may be the system
    python which lacks chromadb and other deps.  Resolution order:
    1. MEMPALACE_PYTHON env var (explicit override)
    2. Venv python from package install path
    3. Editable install: venv/ sibling to mempalace/
    4. sys.executable fallback
    """
    # Honor explicit override (used by shell hook wrappers)
    env_python = os.environ.get("MEMPALACE_PYTHON", "")
    if env_python and os.path.isfile(env_python) and os.access(env_python, os.X_OK):
        return env_python
    # This file lives at <venv>/lib/pythonX.Y/site-packages/mempalace/hooks_cli.py
    # or <project>/mempalace/hooks_cli.py (editable install).
    venv_bin = Path(__file__).resolve().parents[3] / "bin" / "python"
    if venv_bin.is_file():
        return str(venv_bin)
    # Editable install: assumes project root has a venv/ sibling to mempalace/
    project_venv = Path(__file__).resolve().parents[1] / "venv" / "bin" / "python"
    if project_venv.is_file():
        return str(project_venv)
    return sys.executable


_RECENT_MSG_COUNT = 30  # how many recent user messages to summarize

STOP_BLOCK_REASON = (
    "AUTO-SAVE checkpoint (MemPalace). Save this session's key content:\n"
    "1. mempalace_diary_write — session summary (what was discussed, "
    "key decisions, current state of work)\n"
    "2. mempalace_add_drawer — verbatim quotes, decisions, code snippets "
    "(place in appropriate wing and room)\n"
    "3. mempalace_kg_add — entity relationships (optional)\n"
    "For THIS save, use MemPalace MCP tools only (not auto-memory .md files). "
    "Use verbatim quotes where possible. Continue conversation after saving."
)

PRECOMPACT_BLOCK_REASON = (
    "COMPACTION IMMINENT (MemPalace). Save ALL session content before context is lost:\n"
    "1. mempalace_diary_write — thorough session summary\n"
    "2. mempalace_add_drawer — ALL verbatim quotes, decisions, code, context "
    "(place each in appropriate wing and room)\n"
    "3. mempalace_kg_add — entity relationships (optional)\n"
    "For THIS save, use MemPalace MCP tools only (not auto-memory .md files). "
    "Be thorough — after compaction this is all that survives. "
    "Save everything to MemPalace, then allow compaction to proceed."
)


def _sanitize_session_id(session_id: str) -> str:
    """Only allow alnum, dash, underscore to prevent path traversal."""
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "", session_id)
    return sanitized or "unknown"


def _session_state_path(session_id: str, key: str) -> Path:
    """Return the per-session hook state file for ``key``.

    Session ids are sanitized before path construction and keys are restricted
    to simple identifier characters so hook-side state never becomes a path
    traversal primitive.
    """
    safe_session = _sanitize_session_id(str(session_id))
    safe_key = re.sub(r"[^a-zA-Z0-9_-]", "", str(key)) or "state"
    return STATE_DIR / f"{safe_session}_{safe_key}"


def _read_session_state_text(session_id: str, key: str, *, max_chars: int = 20_000) -> str:
    """Best-effort read of a small per-session hook state file.

    The UserPrompt hook uses this to recover the last assistant reply tail for
    short follow-up prompts. It must never create ``STATE_DIR`` or raise — hook
    recall is opportunistic and should silently degrade when state is missing.
    """
    try:
        path = _session_state_path(session_id, key)
        if not path.is_file():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if max_chars > 0 and len(text) > max_chars:
        return text[-max_chars:]
    return text


def _tail_chars(text: str, limit: int) -> str:
    """Return the last ``limit`` characters of ``text`` after stripping whitespace."""
    if not text or limit <= 0:
        return ""
    text = str(text).strip()
    if len(text) <= limit:
        return text
    return text[-limit:]


def _validate_transcript_path(transcript_path: str) -> Path:
    """Validate and resolve a transcript path, rejecting paths outside expected roots.

    Returns a resolved Path if valid, or None if the path should be rejected.
    Accepted paths must:
    - Have a .jsonl or .json extension
    - Not contain '..' after resolution (path traversal prevention)
    """
    if not transcript_path:
        return None
    path = Path(transcript_path).expanduser().resolve()
    if path.suffix not in (".jsonl", ".json"):
        return None
    # Reject if the original input contained '..' traversal components
    if ".." in Path(transcript_path).parts:
        return None
    return path


def _count_human_messages(transcript_path: str) -> int:
    """Count human messages in a JSONL transcript, skipping command-messages."""
    path = _validate_transcript_path(transcript_path)
    if path is None:
        if transcript_path:
            _log(f"WARNING: transcript_path rejected by validator: {transcript_path!r}")
        return 0
    if not path.is_file():
        return 0
    count = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    msg = entry.get("message", {})
                    if isinstance(msg, dict) and msg.get("role") == "user":
                        content = msg.get("content", "")
                        if isinstance(content, str):
                            if "<command-message>" in content:
                                continue
                        elif isinstance(content, list):
                            text = " ".join(
                                b.get("text", "") for b in content if isinstance(b, dict)
                            )
                            if "<command-message>" in text:
                                continue
                        count += 1
                    # Also handle Codex CLI transcript format
                    # {"type": "event_msg", "payload": {"type": "user_message", "message": "..."}}
                    elif entry.get("type") == "event_msg":
                        payload = entry.get("payload", {})
                        if isinstance(payload, dict) and payload.get("type") == "user_message":
                            msg_text = payload.get("message", "")
                            if isinstance(msg_text, str) and "<command-message>" not in msg_text:
                                count += 1
                except (json.JSONDecodeError, AttributeError):
                    pass
    except OSError:
        return 0
    return count


_state_dir_initialized = False


def _log(message: str):
    """Append to hook state log file."""
    if not _palace_root_exists():
        return  # User removed the palace; do not recreate by logging
    global _state_dir_initialized
    try:
        if not _state_dir_initialized:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            try:
                STATE_DIR.chmod(0o700)
            except (OSError, NotImplementedError):
                pass
            _state_dir_initialized = True
        log_path = STATE_DIR / "hook.log"
        is_new = not log_path.exists()
        timestamp = datetime.now().strftime("%H:%M:%S")
        with open(log_path, "a") as f:
            f.write(f"[{timestamp}] {message}\n")
        if is_new:
            try:
                log_path.chmod(0o600)
            except (OSError, NotImplementedError):
                pass
    except OSError:
        pass


def _output(data: dict):
    """Print JSON to stdout without importing modules that may redirect streams.

    If mempalace.mcp_server is already loaded, reuse its saved real stdout fd.
    Otherwise, write directly to fd 1 so hook responses still go to stdout even
    if sys.stdout has been redirected elsewhere.
    """
    payload = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")

    real_stdout_fd: int | None = None
    mcp_mod = sys.modules.get("mempalace.mcp_server") or sys.modules.get(
        f"{__package__}.mcp_server" if __package__ else "mcp_server"
    )
    if mcp_mod is not None:
        real_stdout_fd = getattr(mcp_mod, "_REAL_STDOUT_FD", None)

    fd = real_stdout_fd if real_stdout_fd is not None else 1
    offset = 0
    try:
        while offset < len(payload):
            try:
                offset += os.write(fd, payload[offset:])
            except InterruptedError:
                continue
        return
    except OSError:
        pass

    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()


def _output_additional_context(additional_context: str, harness: str, hook_event_name: str) -> None:
    """Emit hook output that injects additional context for the active harness."""
    if harness == "claude-code":
        _output(
            {
                "hookSpecificOutput": {
                    "hookEventName": hook_event_name,
                    "additionalContext": additional_context,
                }
            }
        )
        return

    # Codex and other harnesses currently accept the same top-level shape used
    # by generic hook runners. Keep the exact context verbatim.
    _output({"additionalContext": additional_context})


def _search_via_mcp_socket(
    *,
    query: str,
    wing: str | None = None,
    n_results: int = USERPROMPT_RECALL_LIMIT,
    max_distance: float = USERPROMPT_MAX_DISTANCE,
    preferred_wing: str | None = None,
) -> dict | None:
    """Best-effort hot-path search through the singleton MCP UDS.

    Returns ``None`` when the socket is unavailable, disabled, times out, or
    returns a malformed response so callers can fall back to in-process search.
    ``preferred_wing`` is accepted for call-site compatibility; the MCP tool
    schema does not expose that ranking hint, so filtered/project-sensitive
    paths should use direct ``search_memories`` instead.
    """
    del preferred_wing
    if os.environ.get("MEMPAL_MCP_DISABLE_SOCKET") == "1":
        return None
    socket_path = os.environ.get(
        "MEMPAL_MCP_SOCKET",
        os.path.join(os.path.expanduser("~"), ".mempalace", "mcp.sock"),
    )
    if not os.path.exists(socket_path):
        return None
    try:
        import socket

        request = {
            "jsonrpc": "2.0",
            "id": "hook-search",
            "method": "tools/call",
            "params": {
                "name": "mempalace_search",
                "arguments": {
                    "query": query,
                    "limit": n_results,
                    "wing": wing,
                    "max_distance": max_distance,
                },
            },
        }
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(float(os.environ.get("MEMPAL_MCP_SOCKET_TIMEOUT", "0.25")))
            sock.connect(socket_path)
            sock.sendall((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
            data = b""
            while not data.endswith(b"\n"):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
        if not data:
            return None
        response = json.loads(data.decode("utf-8").strip())
        if response.get("error"):
            return None
        content = (response.get("result") or {}).get("content") or []
        if not content:
            return None
        text = content[0].get("text") if isinstance(content[0], dict) else None
        parsed = json.loads(text) if isinstance(text, str) else None
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _get_mine_targets() -> list[tuple[str, str]]:
    """Return the list of ``(dir, mode)`` targets for auto-ingest.

    MEMPAL_DIR (when set and resolvable) contributes a ``"projects"``
    target. Transcript ingestion is handled separately by
    ``_ingest_transcript`` — emitting it here too would double-mine the
    same JSONL into a different wing on every hook fire (#1231 review).

    An empty list means no MEMPAL_DIR ingest should run.
    """
    targets: list[tuple[str, str]] = []
    mempal_dir = os.environ.get("MEMPAL_DIR", "")
    if mempal_dir:
        resolved = Path(mempal_dir).expanduser().resolve()
        if resolved.is_dir():
            targets.append((str(resolved), "projects"))
    return targets


# Per-target PID guard.
#
# Hook fires ingest mines in the background. If a previous fire's child is
# still running for the *same* target (same source dir, mode, wing), the new
# fire should skip rather than pile up — multiple concurrent mines against the
# same source corrupt the HNSW index and exhaust disk via duplicate upserts
# (#1212, #1206). But mines targeting *different* sources / modes must remain
# independent so the user can have e.g. project-mining and transcript-ingest
# running in parallel.
#
# The single ``mine.pid`` global file used previously failed both ways: the
# guard was rebuilt every spawn (so two near-simultaneous fires both passed
# the check before either wrote), and the file was unconditionally overwritten
# (so the second spawn lost the first PID, orphaning it). The replacement is
# a directory of per-target slots, claimed via ``O_CREAT | O_EXCL`` so the
# claim is atomic and per-target.
_MINE_PID_DIR = STATE_DIR / "mine_pids"

# The per-process PID file path is communicated to the mine subprocess via
# this env var so the child's cleanup hook (in miner.py) can remove its
# own slot on exit without scanning the whole directory.
_MINE_PID_FILE_ENV = "MEMPALACE_MINE_PID_FILE"


def _pid_file_for_cmd(cmd: list[str]) -> Path:
    """Return the per-target PID file path for a mine subcommand.

    The key is derived from the mine arguments (everything after ``mine``)
    so different (dir, mode, wing) combinations get independent slots.
    Two fires with the same arguments collapse to the same slot — which is
    exactly the dedup we want.
    """
    try:
        idx = cmd.index("mine")
        key = " ".join(cmd[idx:])
    except ValueError:
        key = " ".join(cmd)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return _MINE_PID_DIR / f"mine_{digest}.pid"


def _pid_alive(pid: int) -> bool:
    """Cross-platform existence check for a PID.

    On POSIX, ``os.kill(pid, 0)`` is the well-known no-op existence probe.
    On Windows, ``os.kill`` maps to ``TerminateProcess(handle, sig)`` and
    would *terminate* the target process with exit code ``sig`` — using
    it here would kill our own mine child (or worse, the caller itself).
    Use ``OpenProcess`` + ``GetExitCodeProcess`` via ctypes instead.
    """
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def _mine_already_running(cmd: list[str]) -> bool:
    """Return True if a previous mine for ``cmd``'s target is still alive."""
    pid_file = _pid_file_for_cmd(cmd)
    try:
        recorded = pid_file.read_text().strip()
    except OSError:
        return False
    if not recorded.isdigit():
        return False
    return _pid_alive(int(recorded))


def _claim_mine_slot(cmd: list[str]) -> Optional[Path]:
    """Atomically reserve the per-target PID slot for ``cmd``.

    Returns the slot path on success, or ``None`` if the target is
    already being mined by a live process. The reservation is done via
    ``O_CREAT | O_EXCL`` so two simultaneous hook fires can never both
    pass the check; one wins, the other returns None.

    A stale slot (file exists but the recorded PID is dead) is reclaimed
    transparently — orphan miners that crashed without cleanup do not
    block future hook fires forever.
    """
    pid_file = _pid_file_for_cmd(cmd)
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(pid_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        return pid_file
    except FileExistsError:
        pass
    # Slot exists. If the holder is alive, defer.
    if _mine_already_running(cmd):
        return None
    # Stale entry; reclaim. The unlink+create is racy against another hook
    # firing right now, but the second create's O_EXCL will fail and that
    # caller will see the live PID via the next round.
    try:
        pid_file.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        return None
    try:
        fd = os.open(str(pid_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        return pid_file
    except FileExistsError:
        return None


def _spawn_mine(cmd: list) -> None:
    """Spawn a mine subprocess if no live mine is already targeting it.

    The PID slot is claimed atomically *before* the spawn, so two near-
    simultaneous hook fires can't both proceed — the second sees the
    claimed slot and silently skips. The spawned process inherits a
    ``MEMPALACE_MINE_PID_FILE`` env var so its cleanup hook can remove
    the slot on exit without scanning the directory.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log_path = STATE_DIR / "hook.log"
    pid_file = _claim_mine_slot(cmd)
    if pid_file is None:
        _log(f"Skipping mine: target already running ({' '.join(cmd[-3:])})")
        return
    child_env = os.environ.copy()
    child_env[_MINE_PID_FILE_ENV] = str(pid_file)
    with open(log_path, "a") as log_f:
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=log_f,
                stderr=log_f,
                env=child_env,
                **_detached_popen_kwargs(),
            )
        except OSError:
            # Spawn failed; release the slot we just claimed so the next
            # hook fire can try again rather than skipping forever.
            try:
                pid_file.unlink()
            except OSError:
                pass
            raise
    try:
        pid_file.write_text(str(proc.pid))
    except OSError:
        pass


def _maybe_auto_ingest():
    """Background-mine MEMPAL_DIR (project files) if set.

    Hooks deliberately do not auto-mine raw conversation transcripts. The
    AI-facing save checkpoint writes structured/verbatim memories through MCP;
    ``MEMPAL_DIR`` is the only optional background mining target here.

    Per-target dedup is done by ``_spawn_mine`` itself: each (dir, mode)
    target gets its own PID slot, so distinct targets never block each
    other but a re-fire of the same target while the previous one is
    still running is silently skipped.
    """
    targets = _get_mine_targets()
    if not targets:
        return
    for mine_dir, mode in targets:
        try:
            _spawn_mine([_mempalace_python(), "-m", "mempalace", "mine", mine_dir, "--mode", mode])
        except OSError:
            pass


def _mine_sync():
    """Synchronously mine MEMPAL_DIR (precompact path).

    Hooks deliberately do not auto-mine raw conversation transcripts, so the
    precompact path only handles the optional project directory.
    """
    targets = _get_mine_targets()
    if not targets:
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log_path = STATE_DIR / "hook.log"
    for mine_dir, mode in targets:
        try:
            with open(log_path, "a") as log_f:
                subprocess.run(
                    [
                        _mempalace_python(),
                        "-m",
                        "mempalace",
                        "mine",
                        mine_dir,
                        "--mode",
                        mode,
                    ],
                    stdout=log_f,
                    stderr=log_f,
                    timeout=60,
                )
        except (OSError, subprocess.TimeoutExpired):
            pass


def _desktop_toast(body: str, title: str = "MemPalace"):
    """Send a desktop notification via notify-send. Fails silently."""
    try:
        subprocess.Popen(
            ["notify-send", "--app-name=MemPalace", "--icon=brain", title, body],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **_detached_popen_kwargs(),
        )
    except OSError:
        pass


def _extract_recent_messages(transcript_path: str, count: int = _RECENT_MSG_COUNT) -> list[str]:
    """Extract the last N user messages from a JSONL transcript."""
    path = Path(transcript_path).expanduser()
    if not path.is_file():
        return []
    messages = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    # Claude Code format
                    msg = entry.get("message") or entry.get("event_message") or {}
                    if isinstance(msg, dict) and msg.get("role") == "user":
                        content = msg.get("content", "")
                        if isinstance(content, list):
                            content = " ".join(
                                b.get("text", "") for b in content if isinstance(b, dict)
                            )
                        if not isinstance(content, str) or not content.strip():
                            continue
                        if "<command-message>" in content or "<system-reminder>" in content:
                            continue
                        messages.append(content.strip()[:200])
                    # Codex CLI format
                    elif entry.get("type") == "event_msg":
                        payload = entry.get("payload", {})
                        if isinstance(payload, dict) and payload.get("type") == "user_message":
                            text = payload.get("message", "")
                            if isinstance(text, str) and text.strip():
                                if "<command-message>" not in text:
                                    messages.append(text.strip()[:200])
                except (json.JSONDecodeError, AttributeError):
                    pass
    except OSError:
        return []
    return messages[-count:]


_THEME_STOPWORDS = frozenset(
    "the a an and or but in on at to for of is it i me my you your we our "
    "this that with from by was were be been are not no yes can do did dont "
    "will would should could have has had lets let just also like so if then "
    "ok okay sure yeah hey hi here there what when where how why which some "
    "all any each every about into out up down over after before between "
    "get got make made need want use used using check look see run try "
    "know think right now still already really very much more most too "
    "file files code one two new first last next thing things way well".split()
)


def _extract_themes(messages: list[str], max_themes: int = 3) -> list[str]:
    """Pull 2-3 distinctive topic words from recent messages.

    Note: stopword list is English-only; non-English corpora will produce noisy themes.
    """
    from collections import Counter

    words: Counter[str] = Counter()
    for msg in messages:
        for word in msg.lower().split():
            # Strip punctuation, keep words 4+ chars
            clean = word.strip(".,;:!?\"'`()[]{}#<>/\\-_=+@$%^&*~")
            if len(clean) >= 4 and clean not in _THEME_STOPWORDS and clean.isalpha():
                words[clean] += 1
    return [w for w, _ in words.most_common(max_themes)]


def _save_diary_direct(
    transcript_path: str,
    session_id: str,
    wing: str = "",
    toast: bool = False,
) -> dict:
    """Write a diary checkpoint by calling the tool function directly (no MCP roundtrip).

    If `wing` is set, the entry lands in that wing (typically the project wing
    derived from the transcript path). Otherwise falls back to `tool_diary_write`'s
    default of `wing_session-hook`.

    Returns {"count": N, "themes": [...]} on success, {"count": 0} on failure.
    """
    messages = _extract_recent_messages(transcript_path)
    if not messages:
        _log("No recent messages to save")
        return {"count": 0}

    themes = _extract_themes(messages)

    # Build a compressed diary entry from recent conversation
    now = datetime.now()
    topics = "|".join(m[:80] for m in messages[-10:])
    entry = (
        f"CHECKPOINT:{now.strftime('%Y-%m-%d')}|session:{session_id}"
        f"|msgs:{len(messages)}|recent:{topics}"
    )

    try:
        from .mcp_server import tool_diary_write

        result = tool_diary_write(
            agent_name="session-hook",
            entry=entry,
            topic="checkpoint",
            wing=wing,
        )
        if result.get("success"):
            _log(f"Diary checkpoint saved: {result.get('entry_id', '?')}")
            # Write state for ack tool to read
            try:
                ack_file = STATE_DIR / "last_checkpoint"
                ack_file.write_text(
                    json.dumps({"msgs": len(messages), "ts": now.isoformat()}),
                    encoding="utf-8",
                )
            except OSError:
                pass
            if toast:
                _desktop_toast(f"Checkpoint saved \u2014 {len(messages)} messages archived")
            return {"count": len(messages), "themes": themes}
        else:
            _log(f"Diary checkpoint failed: {result.get('error', 'unknown')}")
    except Exception as e:
        _log(f"Diary checkpoint error: {e}")
    return {"count": 0}


def _ingest_transcript(transcript_path: str):
    """Mine a Claude Code session transcript into the palace as a conversation."""
    path = Path(transcript_path).expanduser()
    if not path.is_file() or path.stat().st_size < 100:
        return

    from .config import MempalaceConfig

    try:
        MempalaceConfig()  # validate config loads
    except Exception:
        return

    try:
        # Route through ``_spawn_mine`` so the per-target PID guard kicks
        # in here too — repeated Stop/PreCompact fires for the same
        # transcript should not stack up parallel ingest mines.
        _spawn_mine(
            [
                _mempalace_python(),
                "-m",
                "mempalace",
                "mine",
                str(path.parent),
                "--mode",
                "convos",
                "--wing",
                "sessions",
            ]
        )
        _log(f"Transcript ingest started: {path.name}")
    except OSError:
        pass


SUPPORTED_HARNESSES = {"claude-code", "codex"}


def _parse_harness_input(data: dict, harness: str) -> dict:
    """Parse stdin JSON according to the harness type."""
    if harness not in SUPPORTED_HARNESSES:
        print(f"Unknown harness: {harness}", file=sys.stderr)
        sys.exit(1)
    return {
        "session_id": _sanitize_session_id(str(data.get("session_id", "unknown"))),
        "stop_hook_active": data.get("stop_hook_active", False),
        "transcript_path": str(data.get("transcript_path", "")),
    }


def _wing_from_transcript_path(transcript_path: str) -> str:
    """Derive a project wing name from a Claude Code transcript path.

    Claude Code encodes the project's source directory by replacing path
    separators with dashes, producing folders like:
        ~/.claude/projects/-home-<user>-Projects-<project>/session.jsonl
        ~/.claude/projects/-home-<user>-dev-<parent>-<project>/session.jsonl
        ~/.claude/projects/-Users-<user>-<folder>-<project>/session.jsonl

    The project directory name is the final dash-separated token of the
    encoded folder. Returns ``wing_<project>`` (lowercased, spaces → ``_``).
    Falls back to ``wing_sessions`` if the path does not match a Claude Code
    project-folder layout.
    """
    # Normalize path separators for cross-platform (Windows backslashes)
    normalized = transcript_path.replace("\\", "/")
    # Primary: pull the encoded project folder out of ``.claude/projects/``
    # and take its last dash-separated token.
    match = re.search(r"/\.claude/projects/-([^/]+)", normalized)
    if match:
        encoded = match.group(1)
        project = encoded.rsplit("-", 1)[-1]
        if project:
            return f"wing_{project.lower().replace(' ', '_')}"
    # Legacy fallback: explicit ``-Projects-<name>`` segment, useful for
    # transcripts not under the standard Claude Code projects dir.
    match = re.search(r"-Projects-([^/]+?)(?:/|$)", normalized)
    if match:
        project = match.group(1).lower().replace(" ", "_")
        return f"wing_{project}"
    return "wing_sessions"


def hook_stop(data: dict, harness: str):
    """Stop hook: block every N messages for auto-save."""
    if not _palace_root_exists():
        _output({})
        return
    parsed = _parse_harness_input(data, harness)
    session_id = parsed["session_id"]
    stop_hook_active = parsed["stop_hook_active"]
    transcript_path = parsed["transcript_path"]

    # If already in a block-mode save cycle, let through (infinite-loop prevention).
    # Silent mode saves directly without returning {"decision":"block"}, so there's
    # no loop to prevent — and Claude Code's plugin dispatch sets this flag on every
    # fire after the first, which would otherwise suppress all subsequent auto-saves.
    if str(stop_hook_active).lower() in ("true", "1", "yes"):
        # Safe default: assume silent mode on any config-read failure so saves
        # proceed rather than being silently dropped. Silent mode is the default
        # (v3.3.0+), so if we can't read config, behave as if it's still on.
        silent_guard = True
        try:
            from .config import MempalaceConfig
        except ImportError as exc:
            _log(
                f"WARNING: could not import MempalaceConfig for stop guard: {exc}; defaulting to silent mode"
            )
        else:
            try:
                silent_guard = MempalaceConfig().hook_silent_save
            except AttributeError as exc:
                _log(f"WARNING: could not read hook_silent_save: {exc}; defaulting to silent mode")
        if not silent_guard:
            _output({})
            return

    # Count human messages
    exchange_count = _count_human_messages(transcript_path)

    # Track last save point
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    last_save_file = STATE_DIR / f"{session_id}_last_save"
    last_save = 0
    if last_save_file.is_file():
        try:
            last_save = int(last_save_file.read_text().strip())
        except (ValueError, OSError):
            last_save = 0

    since_last = exchange_count - last_save

    _log(f"Session {session_id}: {exchange_count} exchanges, {since_last} since last save")

    if since_last >= SAVE_INTERVAL and exchange_count > 0:
        _log(f"TRIGGERING SAVE at exchange {exchange_count}")

        # Read hook settings from config
        from .config import MempalaceConfig

        try:
            config = MempalaceConfig()
            silent = config.hook_silent_save
            toast = config.hook_desktop_toast
        except Exception:
            silent = True
            toast = False

        project_wing = _wing_from_transcript_path(transcript_path)

        if silent:
            # Save directly via Python API — systemMessage renders in terminal
            result = {"count": 0}
            if transcript_path:
                result = _save_diary_direct(
                    transcript_path, session_id, wing=project_wing, toast=toast
                )
            _maybe_auto_ingest()
            # Only advance save marker after successful save
            count = result.get("count", 0)
            if count > 0:
                try:
                    last_save_file.write_text(str(exchange_count), encoding="utf-8")
                except OSError:
                    pass
                themes = result.get("themes", [])
                if themes:
                    tag = " \u2014 " + ", ".join(themes)
                else:
                    tag = ""
                _output(
                    {
                        "systemMessage": f"\u2726 {count} memories woven into the palace{tag}",
                    }
                )
            else:
                _output({})
        else:
            # Legacy: block and ask Claude to save via MCP tools.
            # Marker advances before confirmed save — best-effort; if Claude
            # fails to save, the checkpoint is lost but won't retry endlessly.
            try:
                last_save_file.write_text(str(exchange_count), encoding="utf-8")
            except OSError:
                pass
            _maybe_auto_ingest()
            reason = STOP_BLOCK_REASON + f" Write diary entry to wing={project_wing}."
            _output({"decision": "block", "reason": reason})
    else:
        _output({})


def hook_session_start(data: dict, harness: str):
    """Session start hook: initialize session tracking state."""
    if not _palace_root_exists():
        _output({})
        return
    parsed = _parse_harness_input(data, harness)
    session_id = parsed["session_id"]

    _log(f"SESSION START for session {session_id}")

    # Initialize session state directory
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    # Pass through — no blocking on session start
    _output({})


def hook_precompact(data: dict, harness: str):
    """Precompact hook: save via checkpoint policy, then allow compaction."""
    if not _palace_root_exists():
        _output({})
        return
    parsed = _parse_harness_input(data, harness)
    session_id = parsed["session_id"]

    _log(f"PRE-COMPACT triggered for session {session_id}")

    # Mine only MEMPAL_DIR synchronously so project data lands before
    # compaction proceeds. Raw transcripts are not auto-mined by policy.
    _mine_sync()

    _output({})


def _infer_wing_from_cwd(cwd: str) -> str:
    """Infer a MemPalace wing name from the working directory.

    Maps directory basenames to known wings, e.g. 'solvely-web' -> 'solvely_web'.
    Returns None if no mapping is found.
    """
    if not cwd:
        return None
    basename = os.path.basename(cwd.rstrip("/"))
    # Normalize: dashes to underscores, lowercase
    candidate = basename.replace("-", "_").lower()
    return candidate or None


def _get_palace_taxonomy(palace_path: str) -> dict:
    """Snapshot the current palace taxonomy for the LLM gate.

    Returns {"rooms": [...top by count], "halls": [...top by count],
    "wings": [...top by count]}, all sorted by descending drawer count and
    capped at 12 entries each. Used as the source of truth so the rewrite
    LLM only suggests filter values that actually exist in the palace right
    now, and so the hook can validate LLM-suggested wing names.

    Returns {} on any failure — callers treat it as no taxonomy hint.
    """
    try:
        from collections import Counter

        import chromadb

        client = chromadb.PersistentClient(path=palace_path)
        try:
            col = client.get_collection("mempalace_drawers")
        except Exception:
            return {}
        r = col.get(include=["metadatas"])
        metas = r.get("metadatas") or []
        rooms = Counter(m.get("room") for m in metas if m and m.get("room"))
        halls = Counter(m.get("hall") for m in metas if m and m.get("hall"))
        wings = Counter(m.get("wing") for m in metas if m and m.get("wing"))
        return {
            "rooms": [r for r, _ in rooms.most_common(12)],
            "halls": [h for h, _ in halls.most_common(12)],
            "wings": [w for w, _ in wings.most_common(12)],
        }
    except Exception:
        return {}


def _collect_kg_candidates(query: str = "") -> list[dict]:
    """Return KG triples relevant to ``query`` as rerank-ready candidate dicts.

    Each dict mirrors a drawer-hit's shape so the candidate can be merged
    into the rerank pool. The LLM then judges KG triples against drawer
    hits using the same relevance bar instead of letting them bypass rerank.

    Strategy:
      1. Load all KG entity names.
      2. Match any entity whose name appears as a substring of the raw query
         (case-insensitive). Works for CJK where bigram tokenization would
         otherwise lose entity references ("猫" is 1 char, "Scorpion" is 8).
      3. Fall back to long-token matching (>=3 chars, or any CJK bigram)
         so short/proper-noun queries still get coverage when no entity
         name appears verbatim.
      4. For each matched entity, fetch current facts (valid_to IS NULL).
      5. Wrap each triple as a hit dict: text="subj → pred → obj",
         wing="kg", room="triple", matched_via="kg".

    We intentionally do NOT pull entities from hit wings — doing so used to
    contaminate recall, dragging in every fact about a project just because
    one of its drawers surfaced unrelated to the user's question.
    """
    if not query:
        return []
    try:
        from .knowledge_graph import KnowledgeGraph

        kg = KnowledgeGraph()
    except Exception:
        return []

    try:
        entity_names = kg.list_entity_names()
    except Exception:
        entity_names = []

    query_lower = query.lower()
    entities: list = []
    seen_entities: set = set()
    for name in entity_names:
        if not name:
            continue
        if name.lower() in query_lower:
            key = name.lower()
            if key not in seen_entities:
                seen_entities.add(key)
                entities.append(name)
        if len(entities) >= 6:
            break

    # Fallback: long-token match (primarily helps English queries where the
    # user typed a word that isn't yet an entity — we still try a lookup).
    # CJK queries: keep any token containing a CJK char even if it's only
    # 2 chars (bigram), so Chinese queries don't fall through with zero
    # entities. Without this check, the tokenizer's CJK bigrams (all len=2)
    # would be silently dropped by the len>=3 filter.
    if not entities:
        from .searcher import _tokenize

        tokens = _tokenize(query)
        for t in tokens:
            if (_CJK_CHAR_RE.search(t) or len(t) >= 3) and t.lower() not in seen_entities:
                seen_entities.add(t.lower())
                entities.append(t)
            if len(entities) >= 6:
                break

    if not entities:
        kg.close()
        return []

    try:
        candidates: list[dict] = []
        seen: set = set()
        for entity in entities[:6]:
            try:
                facts = kg.query_entity(entity, direction="both")
            except Exception:
                continue
            for f in facts:
                if f.get("valid_to") is not None:
                    continue
                subj = f.get("subject", "")
                pred = f.get("predicate", "")
                obj = f.get("object", "")
                if subj and pred and obj:
                    key = (subj, pred, obj)
                    if key not in seen:
                        seen.add(key)
                        candidates.append(
                            {
                                "text": f"{subj} → {pred} → {obj}",
                                "wing": "kg",
                                "room": "triple",
                                "matched_via": "kg",
                                # Neutral signal — rerank judges relevance.
                                "similarity": 0.5,
                                "distance": 1.0,
                                "created_at": "",
                                "source_file": "kg",
                            }
                        )
            if len(candidates) >= 8:
                break
        kg.close()
        return candidates[:8]
    except Exception:
        try:
            kg.close()
        except Exception:
            pass
        return []


def _truncate_snippet(text: str, max_chars: int = USERPROMPT_MAX_SNIPPET_CHARS) -> str:
    """Truncate text to max_chars, appending ellipsis if needed."""
    if not text or len(text) <= max_chars:
        return text
    return text[:max_chars] + "…"


def _get_palace_kg_entities(limit: int = 60) -> list[str]:
    """Return the top KG entity names by triple count (most "important" first).

    Used to feed the recall gate so it can rewrite user queries to include
    canonical entity names already in the palace, boosting both vector and
    KG recall (e.g. user mentions "the prod gateway" → query is rewritten to
    include "auth-gateway-prod" verbatim when that's the canonical entity name).

    Returns empty list on any failure — this is best-effort context, never
    a hard requirement.
    """
    if limit <= 0:
        return []
    try:
        import sqlite3

        from .knowledge_graph import KnowledgeGraph

        kg = KnowledgeGraph()
        try:
            conn = sqlite3.connect(kg.db_path, timeout=5)
            try:
                # Rank entities by participation in CURRENT (non-expired)
                # triples. Schema: triples(subject, object) → entities(id);
                # name is the human-readable form we want to expose to the LLM.
                rows = conn.execute(
                    """
                    SELECT e.name, COUNT(t.id) AS c
                    FROM entities e
                    LEFT JOIN triples t
                      ON (t.subject = e.id OR t.object = e.id)
                      AND t.valid_to IS NULL
                    GROUP BY e.id
                    HAVING c > 0
                    ORDER BY c DESC, e.name ASC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            finally:
                conn.close()
            names: list[str] = []
            seen: set[str] = set()
            for row in rows:
                name = row[0]
                if not isinstance(name, str):
                    continue
                name = name.strip()
                if name and name not in seen:
                    seen.add(name)
                    names.append(name)
            return names
        finally:
            kg.close()
    except Exception:
        return []


def _build_active_context(
    cwd: str, palace_path: str = None, preferred_wing: str | None = None
) -> object:
    """Build active_context payload for the recall gate.

    Includes the workdir (project hint), palace taxonomy (rooms/halls for
    valid filter values), top KG entity names (so the gate can rewrite
    queries to echo canonical entity names), and the inferred preferred_wing
    (so the gate can default filters.wing to the active project for
    project-scoped queries). Falls back to a plain cwd string when no extras
    are available, keeping the shape backward compatible with older gate
    prompts.
    """
    ctx: dict = {"cwd": cwd}
    if palace_path:
        taxonomy = _get_palace_taxonomy(palace_path)
        if taxonomy:
            ctx["palace"] = taxonomy
    entities = _get_palace_kg_entities(limit=60)
    if entities:
        ctx["entities"] = entities
    if preferred_wing:
        ctx["preferred_wing"] = preferred_wing
    if len(ctx) == 1:  # only cwd — nothing extra to expose
        return cwd
    return ctx


def hook_userprompt(data: dict, harness: str):
    """UserPromptSubmit hook: search MemPalace and inject relevant memories.

    Pipeline (with LLM enhancement when API key is available):
      1. LLM query rewrite — transform user prompt into optimized search terms
      2. Vector search — fetch candidate pool (RECALL_POOL size)
      3. BM25 hybrid rank — initial ranking
      4. LLM rerank — select top RECALL_LIMIT from the pool

    Each LLM stage degrades gracefully: if no API key or call fails,
    falls back to the original query / BM25-only ranking.
    """
    parsed = _parse_harness_input(data, harness)
    session_id = parsed["session_id"]
    user_prompt = data.get("user_prompt", "") or data.get("prompt", "")
    cwd = parsed.get("cwd", "") or data.get("cwd", "")
    previous_assistant_message = _read_session_state_text(session_id, "last_assistant")
    previous_assistant_tail = _tail_chars(
        previous_assistant_message, USERPROMPT_PREVIOUS_ASSISTANT_TAIL_CHARS
    )

    prompt_stripped = user_prompt.strip() if user_prompt else ""
    if not prompt_stripped:
        _output({})
        return

    prompt_normalized = prompt_stripped.lower()

    # Skip system/internal prompts (e.g. Codex title generation, hook errors)
    _SYSTEM_PROMPT_MARKERS = (
        "you are a helpful assistant",
        "you will be presented with a user prompt",
        "generate a short title",
        "hook timed out",
        "hook (failed)",
        "hook (completed)",
    )
    prompt_lower_head = prompt_normalized[:200]
    for marker in _SYSTEM_PROMPT_MARKERS:
        if marker in prompt_lower_head:
            _log(f"UserPrompt recall: skipped system/internal prompt ({marker!r})")
            _output({})
            return

    # Skip trivial prompts: too short or common filler phrases
    if prompt_normalized in USERPROMPT_HARD_SKIP_PHRASES:
        _log(f"UserPrompt recall: skipped trivial prompt {prompt_stripped!r}")
        _output({})
        return

    if len(prompt_stripped) < USERPROMPT_MIN_QUERY_LEN and not previous_assistant_tail:
        _log(f"UserPrompt recall: skipped trivial prompt {prompt_stripped!r}")
        _output({})
        return

    try:
        from .recall_llm import local_recall_decision

        local_decision = local_recall_decision(
            prompt_stripped,
            previous_assistant_context={"tail": previous_assistant_tail},
            active_context=cwd,
        )
        if local_decision and not local_decision.get("should_recall"):
            _log(f"UserPrompt recall: local skip reason={local_decision.get('reason', 'unknown')}")
            _output({})
            return
    except Exception as e:
        _log(f"UserPrompt recall: local gate failed ({e}), continuing")

    # Lazy import to avoid startup cost when other hooks run
    try:
        from .config import MempalaceConfig
        from .searcher import search_memories
    except ImportError:
        _log("WARNING: Could not import mempalace searcher — skipping recall")
        _output({})
        return

    config = MempalaceConfig()
    palace_path = config.palace_path

    if not os.path.isdir(palace_path):
        _log(f"Palace path not found: {palace_path}")
        _output({})
        return

    preferred_wing = _infer_wing_from_cwd(cwd)
    _log(
        "UserPrompt recall: "
        f"query={user_prompt[:80]!r}, wing={preferred_wing}, "
        f"session={session_id}, prev_assistant_chars={len(previous_assistant_tail)}"
    )

    import time as _time

    _budget_start = _time.monotonic()

    def _budget_exceeded():
        return (_time.monotonic() - _budget_start) > USERPROMPT_BUDGET_SECONDS

    search_query = user_prompt
    original_query = user_prompt
    if previous_assistant_tail:
        search_query = f"{previous_assistant_tail}\n\n{user_prompt}"
        original_query = search_query

    # --- Stage 1: LLM query rewrite + filter selection (default-on when an
    # LLM endpoint is configured; opt out via MEMPAL_LLM=0).
    #
    # NOTE: we deliberately ignore decide_recall's `should_recall` field. The
    # gate sees only the prompt; rerank can judge against actual candidates,
    # and the rerank prompt already returns NONE when nothing helps. Letting
    # rerank decide eliminates the "should-have-recalled but didn't" failure
    # mode where the gate over-prunes prompts that lack explicit history
    # markers. A failed/None LLM decide just falls back to the original query.
    llm_config = None
    time_after = None
    rewrite_filters: dict = {}
    try:
        from .recall_llm import is_enabled, _get_llm_config, decide_recall, rerank

        if is_enabled():
            llm_config = _get_llm_config()
        else:
            _log("UserPrompt recall: LLM disabled, running pure vector search (offline mode)")
        if llm_config:
            # Build active context: cwd + palace taxonomy (rooms/halls) + top
            # KG entities, so the LLM can pick valid filter values AND rewrite
            # the query to echo canonical entity names when the user implicitly
            # references one.
            active_ctx = _build_active_context(cwd, palace_path, preferred_wing=preferred_wing)
            taxonomy = active_ctx.get("palace") if isinstance(active_ctx, dict) else {}
            recall_decision = decide_recall(
                user_prompt,
                config=llm_config,
                previous_assistant_context={"tail": previous_assistant_tail},
                active_context=active_ctx,
            )
            if recall_decision and recall_decision.get("query"):
                search_query = recall_decision["query"]
                time_after = recall_decision.get("after")
                # Validate LLM-suggested filters against real palace taxonomy.
                # Drop any value the LLM hallucinated so we never filter to an
                # empty result set over a bogus hall/room name.
                raw_filters = recall_decision.get("filters") or {}
                valid_rooms = set(taxonomy.get("rooms", [])) if taxonomy else set()
                valid_halls = set(taxonomy.get("halls", [])) if taxonomy else set()
                valid_wings = set(taxonomy.get("wings", [])) if taxonomy else set()
                if raw_filters.get("room") and (
                    not valid_rooms or raw_filters["room"] in valid_rooms
                ):
                    rewrite_filters["room"] = raw_filters["room"]
                if raw_filters.get("hall") and (
                    not valid_halls or raw_filters["hall"] in valid_halls
                ):
                    rewrite_filters["hall"] = raw_filters["hall"]
                if raw_filters.get("wing"):
                    # Accept the LLM's wing only when it's real — either it
                    # matches preferred_wing (inferred from CWD) or it's
                    # present in the palace's known wings. This prevents
                    # hallucinated wing names from filtering to an empty
                    # result set.
                    candidate_wing = raw_filters["wing"]
                    if candidate_wing == preferred_wing or (
                        valid_wings and candidate_wing in valid_wings
                    ):
                        rewrite_filters["wing"] = candidate_wing
                    else:
                        _log(
                            "UserPrompt recall: dropping unknown wing "
                            f"{candidate_wing!r} (preferred={preferred_wing!r}, "
                            f"known={sorted(valid_wings)})"
                        )
                _log(
                    "UserPrompt recall: "
                    f"LLM rewrite reason={recall_decision.get('reason', 'unknown')}, "
                    f"query={search_query[:80]!r}, after={time_after}, "
                    f"filters={rewrite_filters or 'none'}, "
                    f"gate_should_recall={recall_decision.get('should_recall')}"
                )
            else:
                # LLM returned None / no query — fall through with the
                # original prompt as the query. Rerank is the final filter.
                _log("UserPrompt recall: no LLM rewrite available, using original query")
    except Exception as e:
        _log(f"UserPrompt recall: decide+rewrite failed ({e}), using original query")

    # Check budget before search
    if _budget_exceeded():
        _log("UserPrompt recall: budget exceeded after LLM decide, bailing")
        _output({})
        return

    # --- Stage 2: Vector search + BM25 hybrid rank ---
    # Fetch a larger pool when LLM rerank is available
    pool_size = USERPROMPT_RECALL_POOL if llm_config else USERPROMPT_RECALL_LIMIT
    extra = [original_query] if search_query != original_query else []
    result = None
    # MCP socket path doesn't support room/hall filter — skip it when filtered.
    if not extra and not rewrite_filters:
        result = _search_via_mcp_socket(
            query=search_query,
            wing=None,
            n_results=pool_size,
            max_distance=USERPROMPT_MAX_DISTANCE,
            preferred_wing=preferred_wing,
        )
        if result is not None:
            _log("UserPrompt recall: used MCP socket (hot path)")
    if result is None:
        try:
            result = search_memories(
                query=search_query,
                palace_path=palace_path,
                wing=rewrite_filters.get("wing"),
                room=rewrite_filters.get("room"),
                hall=rewrite_filters.get("hall"),
                preferred_wing=preferred_wing,
                n_results=pool_size,
                max_distance=USERPROMPT_MAX_DISTANCE,
                after=time_after,
                extra_queries=extra,
            )
            # Two-stage fallback when the filtered search returns 0 hits.
            #
            # `wing` is a project-scoping SIGNAL, not a convenience: if the
            # gate decided the query is about the active project and the
            # palace has nothing for it, the truthful recall is empty. We
            # must NOT abandon `wing` and drop back into a global search —
            # doing so drags in drawers from unrelated projects as noise.
            #
            # `room`/`hall` are narrower hints and can be relaxed: the gate
            # may have mis-classified the topic room, so a wing-only retry
            # is a safe widening. Wing itself is never widened here.
            if (
                rewrite_filters
                and isinstance(result, dict)
                and not result.get("error")
                and len(result.get("results") or []) == 0
            ):
                wing_filter = rewrite_filters.get("wing")
                narrower = {k: v for k, v in rewrite_filters.items() if k != "wing"}
                if wing_filter and narrower:
                    _log(
                        "UserPrompt recall: filtered search returned 0 hits, "
                        f"widening within wing={wing_filter!r} (dropping {sorted(narrower)})"
                    )
                    result = search_memories(
                        query=search_query,
                        palace_path=palace_path,
                        wing=wing_filter,
                        preferred_wing=preferred_wing,
                        n_results=pool_size,
                        max_distance=USERPROMPT_MAX_DISTANCE,
                        after=time_after,
                        extra_queries=extra,
                    )
                    if (
                        isinstance(result, dict)
                        and not result.get("error")
                        and len(result.get("results") or []) == 0
                    ):
                        _log(
                            "UserPrompt recall: no hits for "
                            f"wing={wing_filter!r}; returning empty recall "
                            "(refusing to cross-project contaminate)"
                        )
                        _output({})
                        return
                elif wing_filter:
                    # wing was the only filter and had 0 hits — stop here.
                    _log(
                        f"UserPrompt recall: no hits for wing={wing_filter!r}; "
                        "returning empty recall"
                    )
                    _output({})
                    return
                else:
                    # No wing filter — safe to fully widen (likely a
                    # hallucinated room/hall label).
                    _log(
                        "UserPrompt recall: filtered search returned 0 hits, "
                        f"retrying without filters={rewrite_filters}"
                    )
                    result = search_memories(
                        query=search_query,
                        palace_path=palace_path,
                        wing=None,
                        preferred_wing=preferred_wing,
                        n_results=pool_size,
                        max_distance=USERPROMPT_MAX_DISTANCE,
                        after=time_after,
                        extra_queries=extra,
                    )
        except Exception as e:
            _log(f"WARNING: search_memories failed: {e}")
            _output({})
            return

    # Distinguish "no results" from "search error" (e.g. embedding failure)
    if isinstance(result, dict) and "error" in result:
        _log(f"WARNING: search returned error: {result['error']}")
        _output({})
        return

    hits = result.get("results", []) if isinstance(result, dict) else []

    # Note: diary entries are NOT filtered out. Previously we filtered them
    # as "session logs", but the async save now stores valuable personal
    # facts (e.g. "user has three cats") as diary entries too. Let the
    # reranker decide — it correctly identifies relevance.

    # Collect KG triples that match the query's named entities. Previously
    # these went straight to the output unfiltered, dragging in topically-
    # related-but-irrelevant facts (e.g. "LiteLLM → endpoints → 127.0.0.1:4000"
    # surfaced for "configure Azure gpt-image-2"). Now they enter the rerank
    # pool alongside drawer hits and get judged against the same relevance bar.
    kg_candidates = _collect_kg_candidates(query=user_prompt)

    if not hits and not kg_candidates:
        _log("UserPrompt recall: no hits")
        _output({})
        return

    if not hits:
        # Pure-KG recall: skip tunnel expansion (nothing to expand from)
        # and feed KG candidates directly into the rerank pool.
        result = {"results": list(kg_candidates)}
        hits = result["results"]
        _log(f"UserPrompt recall: no drawer hits; rerank pool is {len(hits)} KG triples")

    # Tunnel expansion: for each unique (wing, room) in the current pool,
    # follow explicit tunnels to surface connected drawers in other wings.
    # Rerank will filter irrelevant additions; we're just broadening candidates.
    try:
        from .palace import get_collection
        from .palace_graph import follow_tunnels as _follow_tunnels

        # Load the drawers collection once so follow_tunnels can populate
        # `drawer_preview` (first 300 chars of connected drawer content).
        # Without this, follow_tunnels returns only the tunnel label — rerank
        # then gets a one-sentence candidate it almost always rejects, and the
        # expansion does nothing.
        try:
            _tunnel_col = get_collection(palace_path, create=False)
        except Exception as e:
            _log(f"UserPrompt recall: tunnel expansion collection load failed ({e})")
            _tunnel_col = None

        # Hits from search_memories don't carry a stable drawer_id; tunnel
        # hits do (drawer_id of the connected endpoint). Dedup is therefore
        # only meaningful among tunnel additions themselves — search hits
        # live in different wings/rooms by construction (tunnels cross wings).
        existing_tunnel_ids = set()
        seen_pairs = set()
        tunnel_added = 0
        MAX_TUNNEL_EXPANSION = 5

        for hit in list(result.get("results") or []):
            if tunnel_added >= MAX_TUNNEL_EXPANSION:
                break
            w = (hit.get("wing") or "").strip()
            r = (hit.get("room") or "").strip()
            if not w or not r or (w, r) in seen_pairs:
                continue
            seen_pairs.add((w, r))
            try:
                connected = _follow_tunnels(w, r, col=_tunnel_col)
            except Exception as e:
                _log(f"UserPrompt recall: follow_tunnels({w!r}, {r!r}) failed: {e}")
                continue
            # Backfill drawer_preview when a tunnel was created without an
            # explicit drawer_id (the dominant case — auto-save binds tunnels
            # to (wing, room) only). Fetch one representative drawer from the
            # connected location so rerank has real content to evaluate
            # instead of just the tunnel's one-sentence label.
            if _tunnel_col is not None and connected:
                for c in connected:
                    if c.get("drawer_preview"):
                        continue
                    cw = c.get("connected_wing")
                    cr = c.get("connected_room")
                    if not (cw and cr):
                        continue
                    try:
                        sample = _tunnel_col.get(
                            where={"$and": [{"wing": cw}, {"room": cr}]},
                            limit=1,
                            include=["documents"],
                        )
                        docs = (
                            sample.get("documents")
                            if isinstance(sample, dict)
                            else getattr(sample, "documents", None)
                        )
                        if docs and docs[0]:
                            c["drawer_preview"] = docs[0][:300]
                    except Exception as e:
                        _log(
                            "UserPrompt recall: tunnel preview backfill "
                            f"({cw!r}, {cr!r}) failed: {e}"
                        )
            for c in connected or []:
                if tunnel_added >= MAX_TUNNEL_EXPANSION:
                    break
                cid = c.get("drawer_id") or c.get("tunnel_id") or id(c)
                if cid in existing_tunnel_ids:
                    continue
                existing_tunnel_ids.add(cid)
                # follow_tunnels returns connection records. When col= was
                # passed above, `drawer_preview` holds the first 300 chars
                # of the connected drawer content — real substance for the
                # reranker to judge. Fall back to label only if the drawer
                # was deleted or never had an id.
                tunnel_wing = c.get("connected_wing", "")
                tunnel_room = c.get("connected_room", "")
                tunnel_text = c.get("drawer_preview") or c.get("label", "")
                if not tunnel_text:
                    continue  # no content to rerank against — skip silently
                result.setdefault("results", []).append(
                    {
                        "text": tunnel_text,
                        "wing": tunnel_wing,
                        "room": tunnel_room,
                        "created_at": c.get("filed_at") or c.get("created_at", ""),
                        "matched_via": "tunnel",
                        "similarity": 0.5,  # neutral — rerank decides
                        "distance": 1.0,
                        "source_file": c.get("source_file", "?"),
                    }
                )
                tunnel_added += 1

        if tunnel_added:
            # Refresh hits view so the rerank stage below sees the additions.
            hits = result.get("results", []) if isinstance(result, dict) else hits
            _log(
                f"UserPrompt recall: tunnel expansion added {tunnel_added} drawers "
                f"(pool now {len(result.get('results') or [])})"
            )
    except Exception as e:
        _log(f"UserPrompt recall: tunnel expansion skipped ({e})")

    # Merge KG candidates into the rerank pool. Done after tunnel expansion
    # so KG triples sit alongside drawer hits + tunnel-expanded drawers as
    # peer candidates judged by the same rerank pass.
    if kg_candidates:
        already_kg = any(h.get("matched_via") == "kg" for h in hits)
        if not already_kg:
            result.setdefault("results", []).extend(kg_candidates)
            hits = result.get("results", []) if isinstance(result, dict) else hits
            _log(
                f"UserPrompt recall: KG added {len(kg_candidates)} triples to rerank pool "
                f"(pool now {len(hits)})"
            )

    # --- Stage 3: LLM rerank + relevance filter ---
    # Always rerank when LLM is available — even small hit sets may contain
    # noise. The reranker filters out irrelevant matches (e.g. drawers that
    # mention the keyword as a technical example, not as actual content).
    if llm_config and len(hits) >= 1 and not _budget_exceeded():
        try:
            reranked = rerank(
                user_prompt,  # use original prompt for relevance judgment
                hits,
                top_k=USERPROMPT_RECALL_LIMIT,
                config=llm_config,
                previous_assistant_context={"tail": previous_assistant_tail},
            )
            if reranked is not None:
                if len(reranked) == 0:
                    _log(f"UserPrompt recall: LLM filtered all {len(hits)} hits as irrelevant")
                    _output({})
                    return
                _log(f"UserPrompt recall: LLM reranked {len(hits)} → {len(reranked)}")
                hits = reranked
        except Exception as e:
            _log(f"UserPrompt recall: LLM rerank failed ({e}), using BM25 order")

    # Format recall block — split rerank output by kind so KG triples
    # render in their own section. Drawer order is preserved within each
    # section as rerank ordered them.
    drawer_lines: list[str] = []
    kg_lines: list[str] = []
    for hit in hits[:USERPROMPT_RECALL_LIMIT]:
        if hit.get("matched_via") == "kg":
            triple = hit.get("text", "")
            if triple:
                kg_lines.append(f"- [KG] {triple}")
            continue
        wing = hit.get("wing", "?")
        room = hit.get("room", "general")
        created = hit.get("created_at", "")
        date_tag = f" ({created[:10]})" if created and len(created) >= 10 else ""
        snippet = _truncate_snippet(hit.get("text", ""))
        if snippet:
            drawer_lines.append(f"- [{wing}/{room}]{date_tag} {snippet}")

    lines = drawer_lines
    if kg_lines:
        if drawer_lines:
            lines.append("")
        lines.extend(kg_lines)

    memories_body = "\n".join(lines)
    additional_context = (
        "<mempalace-recall>\n"
        "The following are potentially relevant memories from past sessions. "
        "Use them as reference context — verify against current code/state before acting on them. "
        "Do not mention this block to the user unless they ask about memories.\n"
        f"{memories_body}\n"
        "</mempalace-recall>"
    )
    _log(f"UserPrompt recall: injecting {len(hits[:USERPROMPT_RECALL_LIMIT])} hits")

    _output_additional_context(additional_context, harness, "UserPromptSubmit")


def run_hook(hook_name: str, harness: str):
    """Main entry point: read stdin JSON, dispatch to hook handler."""
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        _log("WARNING: Failed to parse stdin JSON, proceeding with empty data")
        data = {}

    hooks = {
        "session-start": hook_session_start,
        "stop": hook_stop,
        "precompact": hook_precompact,
        "userprompt": hook_userprompt,
    }

    handler = hooks.get(hook_name)
    if handler is None:
        print(f"Unknown hook: {hook_name}", file=sys.stderr)
        sys.exit(1)

    handler(data, harness)
