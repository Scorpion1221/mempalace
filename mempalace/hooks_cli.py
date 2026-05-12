"""
Hook logic for MemPalace — Python implementation of session-start, stop, precompact,
and userprompt hooks.

Reads JSON from stdin, outputs JSON to stdout.
Supported hooks: session-start, stop, precompact, userprompt
Supported harnesses: claude-code, codex (extensible to cursor, gemini, etc.)
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional

# Budget for the palace-wide write lock when the async save worker writes to
# ChromaDB. Mirrors the value used by mcp_server.py and miner.py so all
# writers share the same waiting budget on a contended palace.
_PALACE_WRITE_LOCK_TIMEOUT_S = 30.0

SAVE_INTERVAL = int(os.environ.get("MEMPAL_SAVE_INTERVAL", "3"))
SAVE_MIN_MESSAGES = int(os.environ.get("MEMPAL_SAVE_MIN_MESSAGES", "3"))
STATE_DIR = Path.home() / ".mempalace" / "hook_state"
PALACE_ROOT = Path.home() / ".mempalace"


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
    return PALACE_ROOT.is_dir()


# Metadata tag for async LLM-driven save (diary/drawer writes from the recall
# LLM). Kept model-agnostic — the actual model name is recorded separately on
# the `agent` field. ``ASYNC_SAVE_TAG_LEGACY`` matches pre-rename drawers so
# `_build_palace_context` still sees them during the transition.
ASYNC_SAVE_TAG = "async_llm_save"
ASYNC_SAVE_TAG_LEGACY = "haiku_async_save"
ASYNC_SAVE_TAG_OFFLINE = "async_offline_save"
_RECENT_MSG_COUNT = 30  # how many recent user messages to summarize


def _mempalace_python() -> str:
    """Return the python interpreter that has mempalace installed."""
    env_python = os.environ.get("MEMPALACE_PYTHON", "")
    if env_python and os.path.isfile(env_python) and os.access(env_python, os.X_OK):
        return env_python
    venv_bin = Path(__file__).resolve().parents[3] / "bin" / "python"
    if venv_bin.is_file():
        return str(venv_bin)
    project_venv = Path(__file__).resolve().parents[1] / "venv" / "bin" / "python"
    if project_venv.is_file():
        return str(project_venv)
    return sys.executable


# Matches any CJK character (Chinese, Japanese kana, Korean hangul syllables).
# Used so the KG recall path keeps 2-char CJK bigrams from ``_tokenize``,
# which would otherwise be dropped by a plain ``len(t) >= 3`` filter.
_CJK_CHAR_RE = re.compile(r"[一-鿿぀-ヿ가-힯]")

# UserPromptSubmit recall settings
USERPROMPT_RECALL_LIMIT = 5
USERPROMPT_RECALL_POOL = 10  # over-fetch for LLM reranking
USERPROMPT_MAX_SNIPPET_CHARS = 400
USERPROMPT_MAX_DISTANCE = 1.5
USERPROMPT_MIN_QUERY_LEN = 6  # skip very short prompts
USERPROMPT_PREVIOUS_ASSISTANT_TAIL_CHARS = 500
USERPROMPT_BUDGET_SECONDS = 15  # internal timeout — bail before harness kills us

# Short phrases that don't need memory recall
# Keep in sync with TRIVIAL_USER_MESSAGES in hermes-mempalace-plugin
USERPROMPT_SKIP_PHRASES = frozenset(
    {
        # Greetings
        "hi",
        "hello",
        "hey",
        "嗨",
        "你好",
        # Acknowledgement
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
        # Affirmation / negation
        "yes",
        "no",
        "是",
        "是的",
        "不",
        "不是",
        # Continuation
        "continue",
        "go",
        "go on",
        "next",
        "继续",
        # Gratitude
        "thanks",
        "thank you",
        "thx",
        "谢谢",
        # Completion / exit
        "done",
        "完成",
        "搞定",
        "stop",
        "quit",
        "exit",
    }
)

# Short prompts that are only meaningful with prior assistant context.
USERPROMPT_CONTEXTUAL_FOLLOWUP_PHRASES = frozenset(
    {
        "continue",
        "go",
        "go on",
        "next",
        "继续",
    }
)
USERPROMPT_HARD_SKIP_PHRASES = USERPROMPT_SKIP_PHRASES - USERPROMPT_CONTEXTUAL_FOLLOWUP_PHRASES

STOP_BLOCK_REASON = (
    "AUTO-SAVE checkpoint (MemPalace). Save this session's key content:\n"
    "1. mempalace_diary_write — detailed natural language session summary "
    "(write in the SAME LANGUAGE the user used during this session; "
    "include specific decisions, file paths, commands, technical details — "
    "not just a brief overview)\n"
    "2. mempalace_add_drawer — save EACH key piece of content as a separate drawer:\n"
    "   - Decisions and their reasoning (wing=project, room=decisions)\n"
    "   - Code changes and file paths (wing=project, room=code)\n"
    "   - Configuration changes (wing=project, room=configuration)\n"
    "   - Bug findings and root causes (wing=project, room=bugs)\n"
    "   For long sessions, save multiple drawers to capture all important content. "
    "Do not compress — store verbatim text.\n"
    "3. mempalace_kg_add — entity relationships (optional)\n"
    "Use mempalace_list_wings first if unsure which wing to use. "
    "Continue conversation after saving."
)

PRECOMPACT_BLOCK_REASON = (
    "COMPACTION IMMINENT (MemPalace). Save ALL session content before context is lost:\n"
    "1. mempalace_diary_write — thorough natural language session summary "
    "(write in the SAME LANGUAGE the user used during this session; "
    "cover ALL topics, decisions, technical details, outcomes)\n"
    "2. mempalace_add_drawer — save EVERY key piece of content as separate drawers:\n"
    "   - Each decision and its reasoning\n"
    "   - Each code change with file paths\n"
    "   - Each bug finding and root cause\n"
    "   - Each configuration change\n"
    "   - Any verbatim quotes or commands worth preserving\n"
    "   This is your LAST CHANCE — after compaction, detailed context is gone forever. "
    "Save as many drawers as needed. Do not compress — store verbatim.\n"
    "3. mempalace_kg_add — entity relationships (optional)\n"
    "Save everything to MemPalace, then allow compaction to proceed."
)

_MCP_SOCKET_PATH = os.path.join(os.path.expanduser("~"), ".mempalace", "mcp.sock")
_MCP_SOCKET_TIMEOUT = 3.0


def _search_via_mcp_socket(query, wing=None, n_results=5, max_distance=0.0, preferred_wing=None):
    """Try searching via the MCP server's Unix socket (hot HNSW cache).

    Returns the search result dict on success, or None on failure.
    """
    import socket as sock_mod

    if not os.path.exists(_MCP_SOCKET_PATH):
        return None
    try:
        s = sock_mod.socket(sock_mod.AF_UNIX, sock_mod.SOCK_STREAM)
        s.settimeout(_MCP_SOCKET_TIMEOUT)
        s.connect(_MCP_SOCKET_PATH)
        args = {"query": query, "limit": n_results}
        if wing:
            args["wing"] = wing
        if max_distance > 0:
            args["max_distance"] = max_distance
        if preferred_wing:
            args["preferred_wing"] = preferred_wing
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "mempalace_search", "arguments": args},
        }
        s.sendall(json.dumps(request).encode("utf-8") + b"\n")
        data = b""
        while True:
            chunk = s.recv(8192)
            if not chunk:
                break
            data += chunk
            if b"\n" in data:
                break
        s.close()
        response = json.loads(data.decode("utf-8").strip())
        if "error" in response:
            return None
        result = response.get("result", {})
        content = result.get("content", [])
        if content and isinstance(content[0], dict):
            text = content[0].get("text", "{}")
            return json.loads(text)
        return None
    except Exception:
        return None


def _sanitize_session_id(session_id: str) -> str:
    """Only allow alnum, dash, underscore to prevent path traversal."""
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "", session_id)
    return sanitized or "unknown"


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


def _get_session_state_path(session_id: str, key: str) -> Path:
    """Return the state file path for a session-scoped hook cache entry."""
    return STATE_DIR / f"{session_id}_{key}"


def _count_human_messages(transcript_path: str) -> int:
    """Count human messages in a JSONL transcript, skipping tool calls and command-messages.

    Supports three transcript formats:
    - Claude Code: {"type": "user", "content": "..."} (skip "tool_use", "tool_result")
    - Legacy/other: {"message": {"role": "user", "content": ...}}
    - Codex CLI: {"type": "event_msg", "payload": {"type": "user_message", ...}}
    """
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

                    # --- Claude Code transcript format ---
                    # {"type": "user"} can be:
                    #   - real user message (no toolUseResult, no isMeta)
                    #   - tool result (has "toolUseResult" key) — skip
                    #   - meta/system (has "isMeta" key) — skip
                    entry_type = entry.get("type", "")
                    if entry_type == "user":
                        # Skip tool results and meta messages
                        if "toolUseResult" in entry or entry.get("isMeta"):
                            continue
                        content = entry.get("content", "")
                        if isinstance(content, str) and "<command-message>" in content:
                            continue
                        count += 1
                        continue
                    if entry_type in (
                        "tool_use",
                        "tool_result",
                        "assistant",
                        "permission-mode",
                        "attachment",
                        "system",
                        "file-history-snapshot",
                        "last-prompt",
                    ):
                        continue

                    # --- Legacy format: {"message": {"role": "user"}} ---
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
                        continue

                    # --- Codex CLI transcript format ---
                    # {"type": "event_msg", "payload": {"type": "user_message", ...}}
                    if entry_type == "event_msg":
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


def _extract_assistant_text(content) -> str:
    """Extract plain assistant text, ignoring tool blocks."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                text = item.strip()
                if text:
                    parts.append(text)
                continue
            if not isinstance(item, dict):
                continue
            block_type = item.get("type")
            if block_type in ("tool_use", "tool_result"):
                continue
            text = item.get("text")
            if isinstance(text, str):
                text = text.strip()
                if text:
                    parts.append(text)
        return "\n".join(parts).strip()
    if isinstance(content, dict):
        text = content.get("text", "")
        return text.strip() if isinstance(text, str) else ""
    return ""


def _get_last_assistant_message(transcript_path: str) -> str:
    """Return the last assistant/agent reply from a transcript.

    Skips save-checkpoint responses (diary_write, add_drawer confirmations)
    to avoid polluting the previous-assistant cache with MCP bookkeeping noise.
    """
    path = _validate_transcript_path(transcript_path)
    if path is None or not path.is_file():
        return ""

    _SAVE_NOISE_MARKERS = (
        "checkpoint",
        "diary_write",
        "add_drawer",
        "mempalace_diary_write",
        "mempalace_add_drawer",
        "mempalace_kg_add",
        "已保存",
        "saved",
    )

    last_message = ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if not isinstance(entry, dict):
                    continue

                entry_type = entry.get("type", "")

                # Claude Code JSONL: {"type": "assistant", "message": {"content": ...}}
                if entry_type == "assistant":
                    message = entry.get("message", {})
                    content = (
                        message.get("content")
                        if isinstance(message, dict)
                        else entry.get("content")
                    )
                    text = _extract_assistant_text(content)
                    if text:
                        text_lower = text[:200].lower()
                        if not any(m in text_lower for m in _SAVE_NOISE_MARKERS):
                            last_message = text
                    continue

                # Codex JSONL: {"type": "event_msg", "payload": {"type": "agent_message", ...}}
                if entry_type == "event_msg":
                    payload = entry.get("payload", {})
                    if (
                        isinstance(payload, dict)
                        and payload.get("type") == "agent_message"
                        and isinstance(payload.get("message"), str)
                    ):
                        text = payload["message"].strip()
                        if text:
                            text_lower = text[:200].lower()
                            if not any(m in text_lower for m in _SAVE_NOISE_MARKERS):
                                last_message = text
                    continue

                # Legacy fallback: {"message": {"role": "assistant", "content": ...}}
                message = entry.get("message", {})
                if isinstance(message, dict) and message.get("role") == "assistant":
                    text = _extract_assistant_text(message.get("content", ""))
                    if text:
                        text_lower = text[:200].lower()
                        if not any(m in text_lower for m in _SAVE_NOISE_MARKERS):
                            last_message = text
    except OSError:
        return ""

    return last_message


def _write_session_state_text(session_id: str, key: str, value: str):
    """Persist session-scoped hook state as plaintext."""
    if value is None or session_id == "unknown":
        return
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        path = _get_session_state_path(session_id, key)
        path.write_text(value, encoding="utf-8")
    except OSError:
        pass


def _read_session_state_text(session_id: str, key: str) -> str:
    """Read plaintext session-scoped hook state."""
    if session_id == "unknown":
        return ""
    path = _get_session_state_path(session_id, key)
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _clear_session_state_text(session_id: str, key: str):
    """Remove plaintext session-scoped hook state."""
    if session_id == "unknown":
        return
    path = _get_session_state_path(session_id, key)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _tail_chars(text: str, limit: int) -> str:
    """Return the last ``limit`` characters from text."""
    if not text or limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[-limit:]


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
    """Print JSON to stdout with consistent formatting (pretty-printed)."""
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _output_additional_context(context: str, harness: str, event: str) -> None:
    """Emit a hook response that injects ``context`` into the agent's context
    window, in the shape the named harness expects.

    Claude Code / Codex: wrapped under ``hookSpecificOutput.additionalContext``
    with the legacy ``continue`` / ``suppressOutput`` siblings.

    Cursor: field depends on the event. ``sessionStart`` accepts top-level
    ``additional_context`` (per cursor.com/cn/docs/hooks). ``beforeSubmitPrompt``
    looks like it only accepts ``{permission, user_message, agent_message}``
    per the docs, but in practice (verified against the plastic-labs
    cursor-honcho plugin) it ALSO injects ``user_message`` into the prompt
    context when ``continue: true`` — the docs are incomplete. Use that
    undocumented-but-working shape so per-prompt recall actually lands.

    ``event`` is the Claude Code / Codex hookEventName (``"UserPromptSubmit"``,
    ``"SessionStart"``) — we switch on it here to pick the right Cursor
    field.
    """
    if harness == "cursor":
        if event == "UserPromptSubmit":
            # beforeSubmitPrompt path — ``user_message`` is the only field
            # Cursor actually feeds into the agent's prompt context for
            # this event.
            _output({"continue": True, "user_message": context})
        else:
            # SessionStart (and any other event that documents
            # ``additional_context``) — top-level key.
            _output({"additional_context": context})
        return
    _output(
        {
            "continue": True,
            "suppressOutput": True,
            "hookSpecificOutput": {
                "hookEventName": event,
                "additionalContext": context,
            },
        }
    )


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
_MINE_PID_FILE = _MINE_PID_DIR / "mine.pid"

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


def _mine_already_running(cmd: Optional[list[str]] = None) -> bool:
    """Return True if a previous mine for ``cmd``'s target is still alive."""
    pid_file = _pid_file_for_cmd(cmd) if cmd is not None else _MINE_PID_FILE
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

    Transcript convos are ingested separately via ``_ingest_transcript``
    in the hook handlers — this function does not handle them, to avoid
    asymmetric interpreter handling and PID-file overwrite when both
    targets fire from a single hook call (#1231 review).

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

    Transcript convos are ingested separately via ``_ingest_transcript``
    in ``hook_precompact`` — keeping them out of this function avoids
    timeout stacking against the harness 30s ceiling (#1231 review).
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


SUPPORTED_HARNESSES = {"claude-code", "codex", "cursor"}


def _parse_harness_input(data: dict, harness: str) -> dict:
    """Parse stdin JSON according to the harness type."""
    if harness not in SUPPORTED_HARNESSES:
        print(f"Unknown harness: {harness}", file=sys.stderr)
        sys.exit(1)
    # cwd field name differs across harnesses:
    #   Claude Code: "cwd"
    #   Codex:       "cwd" or "workdir" (Codex internal tool-call payloads use "workdir")
    #   Cursor:      "workspace_roots" — a list, first element is the workspace root
    # Falling back to "" means wing → "general" in downstream code, which
    # fragments a project's memory across a bogus wing. Extract once here.
    workspace_roots = data.get("workspace_roots") or []
    workspace_root = workspace_roots[0] if workspace_roots else None
    cwd = str(
        data.get("cwd")
        or data.get("workdir")
        or data.get("workspace_path")
        or data.get("working_directory")
        or workspace_root
        or ""
    )
    # Cursor's session id is "conversation_id"; fall back to session_id for
    # Claude Code / Codex.
    session_id = data.get("session_id") or data.get("conversation_id") or "unknown"
    # Cursor doesn't emit a transcript_path for every hook — it's nullable in
    # the docs — so str() on None would give "None"; normalise to empty.
    transcript_path = data.get("transcript_path") or ""
    return {
        "session_id": _sanitize_session_id(str(session_id)),
        "stop_hook_active": data.get("stop_hook_active", False),
        "transcript_path": str(transcript_path),
        "cwd": cwd,
        "harness": harness,
    }


_ASYNC_SAVE_PROMPT = """\
You are a memory librarian for MemPalace. Extract key content from this conversation and return structured JSON.
Write in the SAME LANGUAGE as the conversation (Chinese→Chinese, English→English).

## Palace Structure
- **wing**: project or domain name, lowercase with underscores (e.g. "backend_api", "infra_deploy"). Use "{wing}" as default. **IMPORTANT**: if the conversation is clearly about a project that already exists in the "Current Palace State" wings list, override the default and use that existing wing — this lets memories from different agents on this machine pool into the same project wing.
- **room**: topic category, lowercase (e.g. "decisions", "code", "configuration", "bugs", "architecture", "general", "issues", "operations", or any fitting short name)
- **diary**: natural language summary of the session segment — include specific decisions, file paths, commands, technical details. Not just "discussed X", but WHAT was decided/changed/found.
- **drawers**: discrete pieces of knowledge worth remembering in future sessions. Each drawer should be self-contained — readable without the conversation context.

## Output Format
- All `"` inside JSON string values MUST be escaped as `\\"`. All newlines inside string values MUST be escaped as `\\n`. Never emit a literal newline inside a JSON string.
Return ONLY valid JSON:
{{"diary": "<session summary>", "drawers": [{{"wing": "<project>", "room": "<topic>", "content": "<verbatim knowledge>"}}], "kg": [{{"subject": "<entity>", "predicate": "<relationship>", "object": "<entity>"}}], "tunnels": [{{"source_wing": "<wing>", "source_room": "<room>", "target_wing": "<wing>", "target_room": "<room>", "label": "<why linked>"}}]}}

## Rules
- diary: 2-5 sentences, include WHY not just WHAT
- drawers: each a standalone fact/decision/config worth recalling later. Typically 0-5, but use more for rich conversations
- Skip trivial exchanges (greetings, confirmations, "OK", "继续")
- If nothing worth saving: {{"diary": "", "drawers": [], "kg": [], "tunnels": []}}
- DEDUP: check the "Current Palace State" section above. Do NOT re-store facts/decisions/configs that already exist in the listed wings/rooms. Only store genuinely NEW information from this conversation segment.
- If the AI's response is just recalling/repeating previously stored memories, there is nothing new to save.
- Drawer content should be specific and actionable, not vague summaries
- Include file paths, URLs, command examples, config values when mentioned
- kg: 0-5 entity-relationship facts. Subject and object are entities (people, projects, tools, services).
  Predicate should be a short, reusable verb or noun (1-2 words). Prefer common terms so facts can be queried later.
  **Language consistency (critical)**: the predicate's language MUST match the subject/object language. Do NOT mix.
    - English subject/object → English predicate (snake_case): "uses", "owns", "depends_on", "fixed_by", "deployed_to"
    - Chinese subject/object → Chinese predicate: "使用", "拥有", "依赖", "修复", "部署到", "养有", "喜欢", "属于", "位于", "状态"
    - Japanese subject/object → Japanese predicate; Korean → Korean predicate; etc.
  Good English predicates: uses, depends_on, works_on, owns, decided, prefers, hosted_at, tech_stack, role, name, status, fixed_by, deployed_to, config_value, endpoint, blocked_by, migrated_to, has_pet, reports_to
  Good Chinese predicates: 使用, 依赖, 工作于, 拥有, 决定, 偏好, 托管于, 技术栈, 角色, 名字, 状态, 修复, 部署到, 配置, 端点, 阻塞于, 迁移到, 养有, 隶属于, 喜欢, 位于
  Bad predicates: found_no_direct_public_private_data_access_evidence (too specific, never queryable). Mixed-language predicates like {{"张三", "loves", "下棋"}} — always wrong.
  Only include facts explicitly STATED in the conversation. Skip if no clear entity relationships.
- tunnels: 0-3 cross-wing edges connecting related palace locations. Strict rules:
  - **Cross-wing only**: source_wing MUST differ from target_wing. Never emit a tunnel where both endpoints are in the same wing — those connections are already implicit.
  - **Both endpoints must exist in "Current Palace State"**: pick source_wing/source_room and target_wing/target_room from the wings list shown above. Do NOT invent wings or rooms that are not already present. If unsure both exist, skip the tunnel.
  - **Real causal/constraint linkage**: a decision or fact in one wing must actually constrain or shape content in the other. "Both mention auth" is not enough. Example of a good link: an "API rate limit" decision in `wing_backend_api` forces "DB pool sizing" in `wing_backend_db` — the first choice shapes the second.
  - **Prefer 0**: default to no tunnels unless a genuine cross-wing link was surfaced in this conversation. Over-creating tunnels pollutes palace traversal. Max 2-3 per save.
  - **Label**: one short sentence naming the causal/constraint story — the "why are these linked" in plain words (SAME LANGUAGE as the conversation).

## Examples

Input: User asks how to connect to the staging database, assistant provides connection string requiring VPN.
Output:
{{"diary": "Provided staging database connection details. Requires VPN access on port 5432.", "drawers": [{{"wing": "backend_api", "room": "configuration", "content": "Staging DB connection: postgres://readonly@staging-db.internal:5432/app_staging (requires VPN, read-only credentials)"}}], "kg": [{{"subject": "backend_api", "predicate": "endpoint", "object": "staging-db.internal:5432/app_staging"}}]}}

Input: 用户报告搜索接口返回504超时，助手排查发现是缺少索引导致全表扫描，添加了复合索引修复。
Output:
{{"diary": "修复了搜索接口504超时问题。根因是 orders 表缺少 (user_id, created_at) 复合索引导致全表扫描，添加索引后响应时间从12s降到50ms。", "drawers": [{{"wing": "backend_api", "room": "bugs", "content": "搜索接口504超时：orders 表缺少 (user_id, created_at) 复合索引，添加后响应从12s→50ms。migration: 20260423_add_orders_search_index.sql"}}, {{"wing": "backend_api", "room": "decisions", "content": "决定对所有按 user_id 查询的表添加 (user_id, created_at) 复合索引作为默认规范"}}], "kg": [{{"subject": "backend_api", "predicate": "修复", "object": "复合索引_user_id_created_at"}}]}}

Input: Team decides to switch from REST to GraphQL for the mobile app API, with a 2-week migration plan.
Output:
{{"diary": "Architecture decision: mobile API switching from REST to GraphQL. Migration plan is 2 weeks, starting with read-only queries. Apollo Server chosen over Yoga for better caching.", "drawers": [{{"wing": "mobile_app", "room": "architecture", "content": "Mobile API migration: REST → GraphQL. Apollo Server (not Yoga) for caching. Step 1: read-only queries (week 1), Step 2: mutations (week 2). Existing REST endpoints kept until v3.0."}}, {{"wing": "mobile_app", "room": "decisions", "content": "Chose Apollo Server over GraphQL Yoga for mobile API — better built-in response caching and dataloader integration"}}], "kg": [{{"subject": "mobile_app", "predicate": "uses", "object": "GraphQL"}}, {{"subject": "mobile_app", "predicate": "uses", "object": "Apollo Server"}}]}}

Input: 助手帮用户重构了认证模块，从 JWT 改成了 session-based，修改了 src/auth/middleware.ts 和 src/auth/session.ts。
Output:
{{"diary": "重构认证模块：JWT → session-based auth。修改了 middleware.ts 和新建了 session.ts，session 存储在 Redis 中，TTL 24小时。", "drawers": [{{"wing": "{wing}", "room": "code", "content": "认证重构 JWT→session: 修改 src/auth/middleware.ts（移除 JWT 验证，改用 session cookie），新建 src/auth/session.ts（Redis session store, TTL=24h）"}}, {{"wing": "{wing}", "room": "decisions", "content": "认证从 JWT 改为 session-based：原因是需要支持即时吊销（JWT 无法做到），session 存 Redis，cookie httpOnly+secure"}}], "kg": [{{"subject": "{wing}", "predicate": "migrated_to", "object": "session_based_auth"}}, {{"subject": "{wing}", "predicate": "uses", "object": "Redis"}}]}}

Input: User configures CI/CD pipeline, sets up GitHub Actions with Docker build and deploy to AWS ECS.
Output:
{{"diary": "Set up CI/CD: GitHub Actions workflow builds Docker image, pushes to ECR, deploys to ECS Fargate. Added .github/workflows/deploy.yml with staging and production environments.", "drawers": [{{"wing": "{wing}", "room": "operations", "content": "CI/CD pipeline: .github/workflows/deploy.yml — build Docker → push to ECR (123456.dkr.ecr.us-east-1) → deploy ECS Fargate. Staging auto-deploys on push to develop, production requires manual approval."}}, {{"wing": "{wing}", "room": "configuration", "content": "ECS Fargate config: task def in infra/ecs-task.json, 512 CPU / 1024 MB, health check /api/health, min 2 / max 8 tasks"}}], "kg": [{{"subject": "{wing}", "predicate": "deployed_to", "object": "AWS ECS Fargate"}}, {{"subject": "{wing}", "predicate": "uses", "object": "GitHub Actions"}}]}}

Input: 用户和助手讨论了项目的技术选型，最终选择了 Next.js + tRPC + Prisma 的技术栈。
Output:
{{"diary": "完成技术选型讨论。最终确定：Next.js 14 (App Router) + tRPC v11 + Prisma ORM + PostgreSQL。选择 tRPC 而非 REST 是因为端到端类型安全。", "drawers": [{{"wing": "{wing}", "room": "architecture", "content": "技术栈选型：Next.js 14 (App Router) + tRPC v11 + Prisma ORM + PostgreSQL。前端 Tailwind CSS + shadcn/ui。部署 Vercel (frontend) + Railway (database)。"}}, {{"wing": "{wing}", "room": "decisions", "content": "选择 tRPC 而非 REST/GraphQL：端到端类型安全，无需手写 schema，和 Next.js Server Components 集成好。trade-off: 仅限 TypeScript 客户端"}}], "kg": [{{"subject": "{wing}", "predicate": "tech_stack", "object": "Next.js + tRPC + Prisma + PostgreSQL"}}]}}

Input: 用户说自己在北京生活了五年，目前在一家叫 Acme 的公司做高级工程师。
Output:
{{"diary": "用户提供个人信息：在北京生活 5 年，在 Acme 公司任高级工程师。", "drawers": [{{"wing": "{wing}", "room": "diary", "content": "用户现居北京，已 5 年；就职于 Acme 公司，职位高级工程师"}}], "kg": [{{"subject": "用户", "predicate": "居住于", "object": "北京"}}, {{"subject": "用户", "predicate": "就职于", "object": "Acme"}}, {{"subject": "用户", "predicate": "职位", "object": "高级工程师"}}]}}

Input: User says "ok" / "继续" / "sounds good" with no new information.
Output:
{{"diary": "", "drawers": [], "kg": [], "tunnels": []}}

Input: User asks assistant to run tests and they all pass. No bugs found, no decisions made.
Output:
{{"diary": "Ran test suite, all tests passed.", "drawers": [], "kg": [], "tunnels": []}}

Input: Team lowers the public API rate limit from 1000 to 100 req/min to protect the shared PostgreSQL pool. Palace already has wings `backend_api` (with rooms decisions, configuration) and `backend_db` (with rooms configuration, architecture).
Output:
{{"diary": "Lowered public API rate limit from 1000 → 100 req/min. The previous 1000 limit was saturating the PostgreSQL connection pool (max 200), causing backend_db to queue. New limit is sized to stay under the DB pool ceiling.", "drawers": [{{"wing": "backend_api", "room": "decisions", "content": "Public API rate limit: 1000 → 100 req/min. Reason: 1000 was saturating the shared PostgreSQL pool (max 200 connections) and causing request queueing in backend_db. 100 req/min keeps us safely below pool capacity."}}, {{"wing": "backend_db", "room": "configuration", "content": "PostgreSQL pool size: 200 connections (shared with backend_api). Do not raise without coordinating a matching change to the API rate limit — the limit is calibrated to this pool ceiling."}}], "kg": [{{"subject": "backend_api", "predicate": "config_value", "object": "rate_limit=100/min"}}, {{"subject": "backend_db", "predicate": "config_value", "object": "pg_pool=200"}}], "tunnels": [{{"source_wing": "backend_api", "source_room": "decisions", "target_wing": "backend_db", "target_room": "configuration", "label": "API rate limit of 100 req/min is calibrated to the backend_db PostgreSQL pool size of 200 — raising either in isolation will break the other"}}]}}

Input: 用户决定把移动端 App 从 Firebase Auth 迁到自建认证服务。Palace 已有 wings `mobile_app`（rooms: decisions, architecture）和 `auth_service`（rooms: architecture, configuration）。
Output:
{{"diary": "决定移动端认证从 Firebase Auth 迁移到自建 auth_service。迁移原因是 Firebase 定价在用户量增长后变得不可控，以及需要在 auth_service 统一多端的会话策略。auth_service 需新增 /mobile/token 端点并支持刷新令牌 30 天。", "drawers": [{{"wing": "mobile_app", "room": "decisions", "content": "移动端认证迁移：Firebase Auth → 自建 auth_service。理由：Firebase 按 MAU 计费在高增长下不可控；统一多端会话策略；自主控制登录风控逻辑"}}, {{"wing": "auth_service", "room": "architecture", "content": "为移动端新增端点 POST /mobile/token（access token 1h / refresh token 30d），需在 auth_service 的 OAuth 流程基础上扩展 device_id 绑定"}}], "kg": [{{"subject": "mobile_app", "predicate": "migrated_to", "object": "auth_service"}}, {{"subject": "auth_service", "predicate": "endpoint", "object": "/mobile/token"}}], "tunnels": [{{"source_wing": "mobile_app", "source_room": "decisions", "target_wing": "auth_service", "target_room": "architecture", "label": "移动端迁移到自建认证，直接要求 auth_service 新增 /mobile/token 端点与 device_id 绑定"}}]}}

## Conversation to process:
{transcript}"""


def _extract_recent_exchanges(transcript_path, since_exchange=0, max_chars=100000):
    """Read recent user+assistant exchanges from a JSONL transcript."""
    path = _validate_transcript_path(transcript_path)
    if not path or not path.is_file():
        return ""
    exchanges = []
    human_count = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                msg_type = entry.get("type", "")
                message = entry.get("message", {})
                if not isinstance(message, dict):
                    message = {}
                content = message.get("content", "")
                if msg_type in ("human", "user"):
                    if isinstance(content, list):
                        if all(
                            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
                        ):
                            continue
                    human_count += 1
                    if human_count <= since_exchange:
                        continue
                    text = (
                        _extract_assistant_text(content)
                        if isinstance(content, list)
                        else str(content)
                    )
                    if text and "<command-message>" not in text:
                        exchanges.append(f"> {text[:2000]}")
                elif msg_type == "assistant" and human_count > since_exchange:
                    text = (
                        _extract_assistant_text(content)
                        if isinstance(content, list)
                        else str(content)
                    )
                    if text:
                        exchanges.append(text[:4000])
    except OSError:
        return ""
    result = "\n\n".join(exchanges)
    if len(result) > max_chars:
        result = result[-max_chars:]
    return result


def _build_palace_context():
    """Build a compact summary of existing palace structure for the recall LLM context."""
    try:
        from .config import MempalaceConfig
        from .palace import get_collection

        cfg = MempalaceConfig()
        col = get_collection(cfg.palace_path, create=False)
        total = col.count() if col else 0
        if total == 0:
            return ""

        lines = []

        batch = col.get(limit=min(total, 5000), include=["metadatas"])
        wing_rooms = {}
        for meta in batch["metadatas"]:
            if not meta:
                continue
            w = meta.get("wing", "")
            r = meta.get("room", "")
            if w:
                wing_rooms.setdefault(w, set())
                if r:
                    wing_rooms[w].add(r)

        if wing_rooms:
            wings_str = ", ".join(
                f"{w} ({', '.join(sorted(rs)[:5])})" if rs else w
                for w, rs in sorted(wing_rooms.items())[:10]
            )
            lines.append(f"Wings: {wings_str}")

        recent = col.get(
            limit=10,
            include=["documents", "metadatas"],
            where={"added_by": {"$in": [ASYNC_SAVE_TAG, ASYNC_SAVE_TAG_LEGACY]}},
        )
        if recent and recent.get("documents"):
            previews = []
            for doc, meta in zip(recent["documents"], recent["metadatas"] or []):
                w = meta.get("wing", "?") if meta else "?"
                r = meta.get("room", "?") if meta else "?"
                previews.append(f"  [{w}/{r}] {doc[:100]}")
            if previews:
                lines.append("Recent saves (do NOT re-store these):")
                lines.extend(previews[:8])

        try:
            from .knowledge_graph import KnowledgeGraph

            kg = KnowledgeGraph()
            import sqlite3

            conn = sqlite3.connect(kg.db_path)
            cur = conn.cursor()
            cur.execute(
                "SELECT DISTINCT subject FROM triples WHERE valid_to IS NULL "
                "ORDER BY subject LIMIT 15"
            )
            entities = [r[0] for r in cur.fetchall()]
            conn.close()
            kg.close()
            if entities:
                lines.append(f"KG entities: {', '.join(entities)}")
        except Exception:
            pass

        return "\n".join(lines)
    except Exception:
        return ""


def _extract_first_json_object(text: str) -> str | None:
    """Return the first complete top-level JSON object substring in text.

    Scans for matching braces while respecting string literals (so a ``}``
    inside a quoted string doesn't close the object). Returns None if no
    balanced object is found.
    """
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _async_save_worker(transcript_text, session_id, cwd):
    """Background worker: call the recall LLM to extract memories, write to palace.

    Concurrency contract (Wave 2):
        All ChromaDB / KG / tunnel writes happen inside ONE
        ``palace_write_lock`` acquisition so they commit as one logical
        save unit. CPU-heavy work (LLM call, JSON parse, sanitize, hash,
        embed) stays OUTSIDE the lock so contended palaces don't pile up
        on a tight critical section.

        On ``PalaceWriteLockTimeout``, the unwritten payload is persisted
        to ``~/.mempalace/recovery/<palace_id>/`` so the next successful
        run can drain it. We exit cleanly (rc=0) so the stop hook chain
        is not disrupted.

    Drainer (Wave 3):
        Before processing the new payload, the worker drains any pending
        recovery WAL files for this palace via ``drain_recovery_wal``.
        Both the drain and the new payload run inside the same
        ``palace_write_lock`` acquisition so writers see them as one
        logical save unit. If the drain pre-flight times out we silently
        skip it — the new payload's own try/except will then send
        everything (drained or not) to the WAL for the next worker.
    """
    try:
        # Bootstrap ChromaDB schema BEFORE any code opens the palace.
        # Prevents the first-open CREATE TABLE race when N async save
        # workers fire against a brand-new palace concurrently. Idempotent
        # fast no-op once chroma.sqlite3 exists — the slow path runs at
        # most once per palace.
        try:
            from .config import MempalaceConfig as _BootstrapConfig
            from .palace import (
                PalaceWriteLockTimeout as _BootstrapTimeout,
                ensure_palace_initialized,
            )

            ensure_palace_initialized(_BootstrapConfig().palace_path)
        except _BootstrapTimeout as bootstrap_exc:
            _log(
                f"async save: palace bootstrap timed out ({bootstrap_exc}) — "
                "continuing; the first write will retry"
            )
        except Exception as bootstrap_exc:
            _log(f"async save: palace bootstrap failed ({bootstrap_exc}) — continuing")

        from .recall_llm import _get_llm_config, _call_llm

        config = _get_llm_config()
        if not config:
            _log("async save: no LLM configured, skipping")
            return

        wing = Path(cwd).name.lower().replace(" ", "_").replace("-", "_") if cwd else "general"

        palace_context = _build_palace_context()
        prompt = _ASYNC_SAVE_PROMPT.format(wing=wing, transcript=transcript_text)
        if palace_context:
            prompt = prompt.replace(
                "## Conversation to process:",
                f"## Current Palace State (reuse existing wings/rooms/entities when possible):\n{palace_context}\n\n## Conversation to process:",
            )

        response = _call_llm(config, prompt, max_tokens=16000, timeout=30, json_mode=True)
        if not response:
            _log("async save: LLM returned empty response")
            return

        try:
            candidate = _extract_first_json_object(response)
            if candidate is None:
                raise ValueError("no balanced JSON object in LLM response")
            data = json.loads(candidate)
        except (ValueError, json.JSONDecodeError) as e:
            dump_dir = STATE_DIR
            try:
                dump_dir.mkdir(parents=True, exist_ok=True)
                ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                dump_path = dump_dir / f"async_save_fail_{ts}.txt"
                dump_path.write_text(
                    (
                        f"ERROR: {e}\n\n"
                        f"=== PROMPT (first 2KB) ===\n{prompt[:2048]}\n\n"
                        f"=== RAW RESPONSE ===\n{response}"
                    ),
                    encoding="utf-8",
                )
                _log(f"async save: JSON parse failed ({e}); raw dumped to {dump_path}")
            except OSError:
                _log(f"async save: JSON parse failed ({e}); could not write dump")
            return

        # ── Build write batch OUTSIDE the lock (CPU-heavy: hash + sanitize). ──
        from .config import MempalaceConfig, sanitize_content, sanitize_name

        cfg = MempalaceConfig()
        now = datetime.now()
        import hashlib  # local — used by id derivation below

        diary_record: dict | None = None
        diary_text = data.get("diary", "")
        if diary_text and len(diary_text.strip()) > 20:
            entry_id = (
                f"diary_{wing}_{now.strftime('%Y%m%d_%H%M%S%f')}"
                f"_{hashlib.sha256(diary_text.encode()).hexdigest()[:12]}"
            )
            diary_record = {
                "id": entry_id,
                "document": sanitize_content(diary_text),
                "metadata": {
                    "wing": wing,
                    "room": "diary",
                    "hall": "hall_diary",
                    "topic": "auto-save",
                    "type": "diary_entry",
                    "added_by": ASYNC_SAVE_TAG,
                    "filed_at": now.isoformat(),
                    "date": now.strftime("%Y-%m-%d"),
                },
            }

        drawer_records: list[dict] = []
        for drawer in data.get("drawers", []):
            content = drawer.get("content", "")
            if not content or len(content.strip()) < 20:
                continue
            d_wing = sanitize_name(drawer.get("wing", wing))
            d_room = sanitize_name(drawer.get("room", "general"))
            from .miner import detect_hall

            d_hall = detect_hall(content)
            d_id = (
                f"drawer_{d_wing}_{d_room}"
                f"_{hashlib.sha256((d_wing + d_room + content).encode()).hexdigest()[:24]}"
            )
            drawer_records.append(
                {
                    "id": d_id,
                    "document": sanitize_content(content),
                    "metadata": {
                        "wing": d_wing,
                        "room": d_room,
                        "hall": d_hall,
                        "added_by": ASYNC_SAVE_TAG,
                        "filed_at": now.isoformat(),
                    },
                    "_pair": (d_wing, d_room),
                }
            )

        kg_facts_raw = data.get("kg", []) or []
        kg_facts: list[dict] = []
        for fact in kg_facts_raw:
            subj = fact.get("subject", "")
            pred = fact.get("predicate", "")
            obj = fact.get("object", "")
            if subj and pred and obj:
                kg_facts.append({"subject": subj, "predicate": pred, "object": obj})

        tunnels_raw = data.get("tunnels", []) or []
        tunnels: list[dict] = []
        for t in tunnels_raw:
            sw = (t.get("source_wing") or "").strip()
            sr = (t.get("source_room") or "").strip()
            tw = (t.get("target_wing") or "").strip()
            tr = (t.get("target_room") or "").strip()
            label = (t.get("label") or "").strip()
            if not (sw and sr and tw and tr) or sw == tw:
                continue  # skip malformed or same-wing
            tunnels.append(
                {
                    "source_wing": sw,
                    "source_room": sr,
                    "target_wing": tw,
                    "target_room": tr,
                    "label": label,
                }
            )

        # Nothing extracted at all — bail out before grabbing the lock.
        if not (diary_record or drawer_records or kg_facts or tunnels):
            _log("async save: LLM returned no actionable payload")
            return

        # ── Critical section: refresh + write under the per-palace lock. ──
        from .palace import PalaceWriteLockTimeout, get_collection, palace_write_lock

        try:
            with palace_write_lock(cfg.palace_path, timeout=_PALACE_WRITE_LOCK_TIMEOUT_S):
                # get_collection inside the lock so client/refresh state
                # is bound to the locked palace, not a stale snapshot.
                col = get_collection(cfg.palace_path, create=True)
                try:
                    col.refresh_for_write()
                except AttributeError:
                    # Older backend / test fake without refresh_for_write —
                    # safe to skip; the lock alone still serialises writers.
                    pass

                # Drain orphaned recovery WAL files first so they land in
                # the palace before the new payload is processed. Drain +
                # new payload share this single lock acquisition so other
                # workers cannot interleave between the two.
                _drain_recovery_wal_safely(cfg.palace_path, col, now)

                stats = _async_save_apply_writes(
                    col=col,
                    now=now,
                    diary_record=diary_record,
                    drawer_records=drawer_records,
                    kg_facts=kg_facts,
                    tunnels=tunnels,
                )
        except PalaceWriteLockTimeout:
            # Persist the unwritten payload to the recovery WAL so the next
            # successful save (or a manual replay) can pick it up. Losing
            # this data silently would violate the "verbatim always /
            # 100% recall" promise.
            _persist_async_save_to_recovery(
                cfg.palace_path,
                session_id=session_id,
                wing=wing,
                diary_record=diary_record,
                drawer_records=drawer_records,
                kg_facts=kg_facts,
                tunnels=tunnels,
            )
            sys.exit(0)

        _log(
            f"async save: wrote {stats['written']} entries "
            f"(diary + {len(data.get('drawers', []))} drawers + "
            f"{stats['kg_written']} kg facts + "
            f"{stats['tunnels_written']} llm tunnels + "
            f"{stats['auto_tunnels_written']} auto tunnels)"
        )
    except Exception as e:
        _log(f"async save error: {e}\n{traceback.format_exc()}")


def _async_save_apply_writes(
    *,
    col,
    now,
    diary_record,
    drawer_records,
    kg_facts,
    tunnels,
) -> dict:
    """Apply all async-save writes against an already-locked, refreshed collection.

    Caller MUST hold ``palace_write_lock`` for ``col``'s palace and have
    already invoked ``refresh_for_write``. Splitting this out keeps
    ``_async_save_worker`` simple enough that ruff's complexity budget
    stays satisfied.

    Returns counters: ``{"written", "kg_written", "tunnels_written",
    "auto_tunnels_written"}``.
    """
    written = 0

    if diary_record is not None:
        col.add(
            ids=[diary_record["id"]],
            documents=[diary_record["document"]],
            metadatas=[diary_record["metadata"]],
        )
        written += 1

    saved_pairs: list = []
    for rec in drawer_records:
        col.upsert(
            ids=[rec["id"]],
            documents=[rec["document"]],
            metadatas=[rec["metadata"]],
        )
        saved_pairs.append(rec["_pair"])
        written += 1

    kg_written = _async_save_apply_kg(kg_facts, now) if kg_facts else 0
    tunnels_written = _async_save_apply_tunnels(tunnels) if tunnels else 0
    auto_tunnels_written = _async_save_apply_auto_tunnels(saved_pairs, col) if saved_pairs else 0

    return {
        "written": written,
        "kg_written": kg_written,
        "tunnels_written": tunnels_written,
        "auto_tunnels_written": auto_tunnels_written,
    }


def _async_save_apply_kg(kg_facts: list[dict], now) -> int:
    """Write KG triples (with prior-fact invalidation). Returns count written."""
    try:
        from .knowledge_graph import KnowledgeGraph

        kg = KnowledgeGraph()
        try:
            kg_written = 0
            for fact in kg_facts:
                subj = fact["subject"]
                pred = fact["predicate"]
                obj = fact["object"]
                existing = kg.query_entity(subj, direction="outgoing")
                for old in existing:
                    if (
                        old.get("predicate") == pred
                        and old.get("object") != obj
                        and old.get("valid_to") is None
                    ):
                        kg.invalidate(subj, pred, old["object"], ended=now.strftime("%Y-%m-%d"))
                kg.add_triple(subj, pred, obj, valid_from=now.strftime("%Y-%m-%d"))
                kg_written += 1
            return kg_written
        finally:
            kg.close()
    except Exception as e:
        _log(f"async save: KG write error: {e}")
        return 0


def _async_save_apply_tunnels(tunnels: list[dict]) -> int:
    """Create explicit cross-wing tunnels. Returns count written."""
    try:
        from .palace_graph import create_tunnel

        tunnels_written = 0
        for t in tunnels:
            try:
                create_tunnel(
                    source_wing=t["source_wing"],
                    source_room=t["source_room"],
                    target_wing=t["target_wing"],
                    target_room=t["target_room"],
                    label=t["label"],
                )
                tunnels_written += 1
            except Exception as e:
                _log(
                    "async save: tunnel write error "
                    f"({t['source_wing']}/{t['source_room']} -> "
                    f"{t['target_wing']}/{t['target_room']}): {e}"
                )
        return tunnels_written
    except Exception as e:
        _log(f"async save: tunnel block failed: {e}")
        return 0


def _async_save_apply_auto_tunnels(saved_pairs: list, col) -> int:
    """Deterministic auto-tunnel pass for same-room cross-wing pairs.

    Independent of the LLM's tunnel output. Stays inside the caller's
    palace_write_lock so it sees the rooms we just upserted.
    """
    try:
        from .palace_graph import auto_link_shared_rooms, invalidate_graph_cache

        invalidate_graph_cache()
        auto_links = auto_link_shared_rooms(saved_pairs, col=col, max_per_save=5)
        return len(auto_links)
    except Exception as e:
        _log(f"async save: auto-link tunnel block failed: {e}")
        return 0


def _drain_recovery_wal_safely(palace_path, col, now) -> None:
    """Drain pending recovery WAL files into ``col``, swallowing per-file errors.

    Called from inside ``_async_save_worker``'s ``palace_write_lock``
    block. ``apply_records`` writes via the shared ``_async_save_apply_*``
    helpers, so the drained payload commits under the same lock as the
    new payload. Failures are logged and the offending file is left on
    disk for retry by a future worker (the WAL is the recovery
    boundary — losing data here would defeat its purpose).
    """
    try:
        from .recovery_wal import drain_recovery_wal
    except Exception as exc:  # pragma: no cover - defensive
        _log(f"async save: drainer unavailable, skipping: {exc}")
        return

    try:
        result = drain_recovery_wal(
            palace_path,
            apply_records=lambda recs: _replay_recovery_records(recs, col, now),
        )
    except Exception as exc:  # pragma: no cover - defensive
        _log(f"async save: recovery WAL drain raised unexpectedly: {exc}")
        return

    if result.records_replayed > 0 or result.files_processed > 0:
        msg = (
            f"[mempalace.async_save] drained {result.files_processed} "
            f"recovery file(s) ({result.records_replayed} record(s)) "
            f"in {result.duration_seconds:.2f}s"
        )
        try:
            sys.stderr.write(msg + "\n")
        except Exception:
            pass
        _log(msg)
    if result.files_failed > 0:
        for path, err in result.failures:
            _log(f"async save: recovery drain left {path} on disk: {err}")


def _replay_recovery_records(records: list[dict], col, now) -> None:
    """Apply a flat list of WAL records against an already-locked collection.

    Splits the records by ``op`` into the four buckets the existing
    ``_async_save_apply_*`` helpers expect, then dispatches to them.
    Reuses the live writers so a future schema change to the writers
    is automatically inherited by the drainer.

    Records with unrecognised ``op`` (e.g. ``"context"`` metadata,
    ``"empty"`` markers) are skipped — they were always informational.
    """
    diary_record: dict | None = None
    drawer_records: list[dict] = []
    kg_facts: list[dict] = []
    tunnels: list[dict] = []

    for rec in records:
        op = rec.get("op")
        args = rec.get("args") or {}
        if op == "diary":
            # First diary wins; subsequent diaries inside the same WAL
            # file are extremely rare (one save → one diary by design),
            # so a single record per save is the realistic shape.
            diary_record = diary_record or args
        elif op == "drawer":
            drawer_records.append(args)
        elif op == "kg_triple":
            kg_facts.append(args)
        elif op == "tunnel":
            tunnels.append(args)
        # other ops (context, empty, unknown) are intentionally skipped

    # Compute saved_pairs the same way _async_save_apply_writes does so
    # the auto-tunnel pass sees the rooms we're about to upsert.
    saved_pairs: list = []

    if diary_record is not None:
        col.add(
            ids=[diary_record["id"]],
            documents=[diary_record["document"]],
            metadatas=[diary_record.get("metadata", {})],
        )

    for rec in drawer_records:
        col.upsert(
            ids=[rec["id"]],
            documents=[rec["document"]],
            metadatas=[rec.get("metadata", {})],
        )
        meta = rec.get("metadata") or {}
        wing = meta.get("wing")
        room = meta.get("room")
        if wing and room:
            saved_pairs.append((wing, room))

    if kg_facts:
        _async_save_apply_kg(kg_facts, now)
    if tunnels:
        _async_save_apply_tunnels(tunnels)
    if saved_pairs:
        _async_save_apply_auto_tunnels(saved_pairs, col)


def _persist_async_save_to_recovery(
    palace_path,
    *,
    session_id,
    wing,
    diary_record,
    drawer_records,
    kg_facts,
    tunnels,
):
    """Persist an async-save payload to the recovery WAL on lock timeout.

    Strips internal-only fields (``_pair``) so the on-disk record is
    portable. Logs to stderr (visible in the spawning hook chain) and to
    the hook log so operators can find orphaned payloads. The WAL is
    drained on the next successful ``_async_save_worker`` run, or
    manually via ``mempal drain-recovery``.
    """
    from .recovery_wal import persist_async_save_payload

    # Sanitised copies — caller's dicts may include internal-only keys.
    drawers_for_disk = [
        {k: v for k, v in rec.items() if not k.startswith("_")} for rec in (drawer_records or [])
    ]

    record_count = sum(
        1 for x in (diary_record, *(drawers_for_disk), *(kg_facts or []), *(tunnels or [])) if x
    )

    try:
        path = persist_async_save_payload(
            palace_path,
            diary=diary_record,
            drawers=drawers_for_disk,
            kg_facts=kg_facts,
            tunnels=tunnels,
            context={
                "session_id": session_id,
                "wing": wing,
                "filed_at": datetime.now().isoformat(),
                "reason": "palace_write_lock_timeout",
            },
        )
    except Exception as exc:
        # Last-ditch: even WAL persistence failed. Log loudly so the
        # operator knows data was lost (this should be very rare —
        # writing one short JSONL file usually cannot fail unless the
        # disk is full or permissions are broken).
        msg = (
            f"[mempalace.async_save] palace_write_lock timeout AND "
            f"recovery WAL persistence failed: {exc!r}. "
            f"Lost {record_count} records.\n"
        )
        try:
            sys.stderr.write(msg)
        except Exception:
            pass
        _log(msg)
        return

    msg = (
        f"[mempalace.async_save] palace_write_lock timeout. "
        f"Persisted {record_count} records to recovery WAL: {path}\n"
    )
    try:
        sys.stderr.write(msg)
    except Exception:
        pass
    _log(msg)


def _async_save_worker_offline(transcript_text, session_id, cwd):
    """Background worker for offline mode: write transcript verbatim, no LLM.

    Honors the verbatim-storage design principle: stores the exact transcript
    chunk in a single drawer under ``room=raw_transcript`` so it remains
    searchable via vector + BM25 even without an LLM stack. Future runs with
    an LLM configured can re-classify these drawers into structured ones.
    """
    try:
        if len(transcript_text.strip()) < 80:
            _log("offline save: transcript too short, skipping")
            return

        from .config import MempalaceConfig, sanitize_content
        from .miner import detect_hall
        from .palace import get_collection

        wing = Path(cwd).name.lower().replace(" ", "_").replace("-", "_") if cwd else "general"
        cfg = MempalaceConfig()
        col = get_collection(cfg.palace_path, create=True)
        now = datetime.now()

        import hashlib

        digest = hashlib.sha256(transcript_text.encode()).hexdigest()[:24]
        drawer_id = f"raw_{wing}_{now.strftime('%Y%m%d_%H%M%S%f')}_{digest}"
        col.upsert(
            ids=[drawer_id],
            documents=[sanitize_content(transcript_text)],
            metadatas=[
                {
                    "wing": wing,
                    "room": "raw_transcript",
                    "hall": detect_hall(transcript_text),
                    "added_by": ASYNC_SAVE_TAG_OFFLINE,
                    "filed_at": now.isoformat(),
                    "date": now.strftime("%Y-%m-%d"),
                    "session_id": session_id,
                }
            ],
        )
        _log(f"offline save: wrote 1 raw drawer wing={wing} chars={len(transcript_text)}")
    except Exception as e:
        _log(f"offline save error: {e}\n{traceback.format_exc()}")


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

    # If already in a save cycle, let through (infinite-loop prevention)
    if str(stop_hook_active).lower() in ("true", "1", "yes"):
        _output({})
        return

    last_assistant_message = _get_last_assistant_message(transcript_path)
    if last_assistant_message:
        _write_session_state_text(session_id, "last_assistant", last_assistant_message)
        _log(
            f"Session {session_id}: cached last assistant reply "
            f"({len(last_assistant_message)} chars)"
        )
    else:
        _clear_session_state_text(session_id, "last_assistant")

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

    if since_last > 0 and exchange_count >= SAVE_MIN_MESSAGES:
        try:
            last_save_file.write_text(str(exchange_count), encoding="utf-8")
        except OSError:
            pass

        _log(f"TRIGGERING SAVE at exchange {exchange_count}")

        if os.environ.get("MEMPAL_VERBOSE", "") in ("true", "1"):
            _output({"decision": "block", "reason": STOP_BLOCK_REASON})
            return

        transcript_text = _extract_recent_exchanges(transcript_path, since_exchange=last_save)
        # Auto-save and recall share the same LLM gate: enabled by default
        # whenever an endpoint+model is configured. Set MEMPAL_LLM=0 to
        # opt out. See recall_llm.is_enabled() for the full probe rules.
        # Offline mode (no LLM) falls back to a verbatim raw-transcript
        # writer so the palace keeps growing; opt out via MEMPAL_OFFLINE_SAVE=0.
        from .recall_llm import is_enabled as _llm_is_enabled

        if transcript_text:
            if _llm_is_enabled():
                worker = "_async_save_worker"
            elif os.environ.get("MEMPAL_OFFLINE_SAVE", "1") in ("0", "false", "False"):
                worker = ""
                _log("async save: offline mode but MEMPAL_OFFLINE_SAVE=0, skipping")
            else:
                worker = "_async_save_worker_offline"
            if worker:
                cwd = parsed.get("cwd", "") or data.get("cwd", "")
                try:
                    popen_kwargs = _detached_popen_kwargs()
                    popen_kwargs.update(
                        {
                            "stdin": subprocess.PIPE,
                            "stdout": subprocess.DEVNULL,
                            "stderr": subprocess.DEVNULL,
                        }
                    )
                    proc = subprocess.Popen(
                        [
                            sys.executable,
                            "-c",
                            "import sys, json; "
                            "d = json.load(sys.stdin); "
                            f"from mempalace.hooks_cli import {worker}; "
                            f"{worker}(d['text'], d['session'], d['cwd'])",
                        ],
                        **popen_kwargs,
                    )
                    payload = json.dumps(
                        {"text": transcript_text, "session": session_id, "cwd": cwd or ""}
                    )
                    proc.stdin.write(payload.encode("utf-8"))
                    proc.stdin.close()
                    _log(f"async save: spawned {worker} background process")
                except Exception as e:
                    _log(f"async save: failed to spawn ({e})")

        _output({})
    else:
        _output({})


def hook_session_start(data: dict, harness: str):
    """Session start hook: initialize session tracking state.

    For Cursor, also injects a one-shot palace context summary (wings,
    recent saves, KG entities) via the ``additional_context`` channel.
    Cursor lacks a per-prompt context-injection hook, so sessionStart is
    the only chance to seed the agent with memory map awareness — actual
    per-query recall happens via the agent calling the ``mempalace_search``
    MCP tool, which is encouraged by the ``mempalace-recall.mdc`` rule
    shipped in ``.cursor-plugin/rules/``.

    Claude Code / Codex have full per-prompt recall via ``hook_userprompt``,
    so their session-start path stays a no-op pass-through.
    """
    if not _palace_root_exists():
        _output({})
        return
    parsed = _parse_harness_input(data, harness)
    session_id = parsed["session_id"]

    _log(f"SESSION START for session {session_id} (harness={harness})")

    # Initialize session state directory
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    if harness == "cursor":
        try:
            palace_summary = _build_palace_context()
        except Exception as exc:
            _log(f"SessionStart: palace context build failed: {exc}")
            palace_summary = ""
        if palace_summary:
            # This is the ONLY injection point that Cursor 3.1.17 actually
            # feeds into the agent context. beforeSubmitPrompt's user_message
            # is documented but silent-drops in practice, so we can't do
            # per-prompt recall the way Claude Code / Codex do. Compensate
            # by giving the agent a strong, specific instruction to pull
            # memory on-demand via the MCP tool.
            additional_context = (
                "<mempalace-recall>\n"
                "You have access to a MemPalace memory system via the "
                "`mempalace_search` and `mempalace_kg_query` MCP tools. "
                "This block is a map of what's stored — use the tools to "
                "fetch specifics.\n\n"
                "BEHAVIOUR RULES — these override any 'I don't know' reflex:\n"
                "• If the user asks about personal facts (pets, family, "
                "preferences, plans, projects they work on) → call "
                "`mempalace_search` BEFORE answering. Do not say 'I have "
                "no way to know' — the answer is likely in memory.\n"
                "• If the user references past work ('之前', '上次', "
                "'remember', 'last time', 'we decided', '我们') → call "
                "`mempalace_search` BEFORE answering.\n"
                "• If the user names a project/person/tool you're uncertain "
                "about → call `mempalace_kg_query` with that entity.\n"
                "• Skip mempalace for fresh code-writing questions with no "
                "historical reference — don't waste latency.\n\n"
                "Palace map (for orientation; do NOT treat as the full answer):\n"
                f"{palace_summary}\n"
                "</mempalace-recall>"
            )
            _output_additional_context(additional_context, harness, "SessionStart")
            return
        # Empty palace — emit empty additional_context shape for Cursor so
        # we don't accidentally fall into the legacy {} pass-through that
        # Cursor wouldn't recognise.
        _output({"additional_context": ""})
        return

    # Pass through — no blocking on session start
    _output({})


def hook_precompact(data: dict, harness: str):
    """Precompact hook: mine transcript synchronously, then allow compaction."""
    if not _palace_root_exists():
        _output({})
        return
    parsed = _parse_harness_input(data, harness)
    session_id = parsed["session_id"]

    _log(f"PRE-COMPACT triggered for session {session_id}")

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


def hook_userprompt(data: dict, harness: str):  # noqa: C901
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
