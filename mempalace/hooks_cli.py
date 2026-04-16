"""
Hook logic for MemPalace — Python implementation of session-start, stop, precompact,
and userprompt hooks.

Reads JSON from stdin, outputs JSON to stdout.
Supported hooks: session-start, stop, precompact, userprompt
Supported harnesses: claude-code, codex (extensible to cursor, gemini, etc.)
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SAVE_INTERVAL = int(os.environ.get("MEMPAL_SAVE_INTERVAL", "3"))
SAVE_MIN_MESSAGES = int(os.environ.get("MEMPAL_SAVE_MIN_MESSAGES", "3"))
STATE_DIR = Path.home() / ".mempalace" / "hook_state"

# UserPromptSubmit recall settings
USERPROMPT_RECALL_LIMIT = 5
USERPROMPT_RECALL_POOL = 10  # over-fetch for LLM reranking
USERPROMPT_MAX_SNIPPET_CHARS = 400
USERPROMPT_MAX_DISTANCE = 1.5
USERPROMPT_MIN_QUERY_LEN = 6  # skip very short prompts

# Short phrases that don't need memory recall
# Keep in sync with TRIVIAL_USER_MESSAGES in hermes-mempalace-plugin
USERPROMPT_SKIP_PHRASES = frozenset({
    # Greetings
    "hi", "hello", "hey", "嗨", "你好",
    # Acknowledgement
    "ok", "okay", "好", "好的", "行", "嗯", "对",
    "cool", "nice", "great", "sounds good",
    # Affirmation / negation
    "yes", "no", "是", "是的", "不", "不是",
    # Continuation
    "continue", "go", "go on", "next", "继续",
    # Gratitude
    "thanks", "thank you", "thx", "谢谢",
    # Completion / exit
    "done", "完成", "搞定", "stop", "quit", "exit",
})

STOP_BLOCK_REASON = (
    "AUTO-SAVE checkpoint (MemPalace). Save this session's key content:\n"
    "1. mempalace_diary_write — AAAK-compressed session summary\n"
    "2. mempalace_add_drawer — verbatim quotes, decisions, code snippets "
    "(params: wing=project name, room=topic, content=verbatim text)\n"
    "3. mempalace_kg_add — entity relationships (optional)\n"
    "Use mempalace_list_wings first if unsure which wing to use. "
    "Do NOT write to Claude Code's native auto-memory (.md files). "
    "Continue conversation after saving."
)

PRECOMPACT_BLOCK_REASON = (
    "COMPACTION IMMINENT (MemPalace). Save ALL session content before context is lost:\n"
    "1. mempalace_diary_write — thorough AAAK-compressed session summary\n"
    "2. mempalace_add_drawer — ALL verbatim quotes, decisions, code, context "
    "(params: wing=project name, room=topic, content=verbatim text)\n"
    "3. mempalace_kg_add — entity relationships (optional)\n"
    "Be thorough \u2014 after compaction, detailed context will be lost. "
    "Do NOT write to Claude Code's native auto-memory (.md files). "
    "Save everything to MemPalace, then allow compaction to proceed."
)


def _sanitize_session_id(session_id: str) -> str:
    """Only allow alnum, dash, underscore to prevent path traversal."""
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "", session_id)
    return sanitized or "unknown"


def _count_human_messages(transcript_path: str) -> int:
    """Count human messages in a JSONL transcript, skipping tool calls and command-messages.

    Supports three transcript formats:
    - Claude Code: {"type": "user", "content": "..."} (skip "tool_use", "tool_result")
    - Legacy/other: {"message": {"role": "user", "content": ...}}
    - Codex CLI: {"type": "event_msg", "payload": {"type": "user_message", ...}}
    """
    path = Path(transcript_path).expanduser()
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
                    if entry_type in ("tool_use", "tool_result", "assistant",
                                      "permission-mode", "attachment", "system",
                                      "file-history-snapshot", "last-prompt"):
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


def _log(message: str):
    """Append to hook state log file."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        log_path = STATE_DIR / "hook.log"
        timestamp = datetime.now().strftime("%H:%M:%S")
        with open(log_path, "a") as f:
            f.write(f"[{timestamp}] {message}\n")
    except OSError:
        pass


def _output(data: dict):
    """Print JSON to stdout with consistent formatting (pretty-printed)."""
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _maybe_auto_ingest():
    """If MEMPAL_DIR is set and exists, run mempalace mine in background."""
    mempal_dir = os.environ.get("MEMPAL_DIR", "")
    if mempal_dir and os.path.isdir(mempal_dir):
        try:
            log_path = STATE_DIR / "hook.log"
            with open(log_path, "a") as log_f:
                subprocess.Popen(
                    [sys.executable, "-m", "mempalace", "mine", mempal_dir],
                    stdout=log_f,
                    stderr=log_f,
                )
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


def hook_stop(data: dict, harness: str):
    """Stop hook: block every N messages for auto-save."""
    parsed = _parse_harness_input(data, harness)
    session_id = parsed["session_id"]
    stop_hook_active = parsed["stop_hook_active"]
    transcript_path = parsed["transcript_path"]

    # If already in a save cycle, let through (infinite-loop prevention)
    if str(stop_hook_active).lower() in ("true", "1", "yes"):
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

    if since_last >= SAVE_INTERVAL and exchange_count >= SAVE_MIN_MESSAGES:
        # Update last save point
        try:
            last_save_file.write_text(str(exchange_count), encoding="utf-8")
        except OSError:
            pass

        _log(f"TRIGGERING SAVE at exchange {exchange_count}")

        # Optional: auto-ingest if MEMPAL_DIR is set
        _maybe_auto_ingest()

        _output({"decision": "block", "reason": STOP_BLOCK_REASON})
    else:
        _output({})


def hook_session_start(data: dict, harness: str):
    """Session start hook: initialize session tracking state."""
    parsed = _parse_harness_input(data, harness)
    session_id = parsed["session_id"]

    _log(f"SESSION START for session {session_id}")

    # Initialize session state directory
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    # Pass through — no blocking on session start
    _output({})


def hook_precompact(data: dict, harness: str):
    """Precompact hook: always block with comprehensive save instruction."""
    parsed = _parse_harness_input(data, harness)
    session_id = parsed["session_id"]

    _log(f"PRE-COMPACT triggered for session {session_id}")

    # Optional: auto-ingest synchronously before compaction (so memories land first)
    mempal_dir = os.environ.get("MEMPAL_DIR", "")
    if mempal_dir and os.path.isdir(mempal_dir):
        try:
            log_path = STATE_DIR / "hook.log"
            with open(log_path, "a") as log_f:
                subprocess.run(
                    [sys.executable, "-m", "mempalace", "mine", mempal_dir],
                    stdout=log_f,
                    stderr=log_f,
                    timeout=60,
                )
        except OSError:
            pass

    # Always block -- compaction = save everything
    _output({"decision": "block", "reason": PRECOMPACT_BLOCK_REASON})


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


def _truncate_snippet(text: str, max_chars: int = USERPROMPT_MAX_SNIPPET_CHARS) -> str:
    """Truncate text to max_chars, appending ellipsis if needed."""
    if not text or len(text) <= max_chars:
        return text
    return text[:max_chars] + "…"


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
    user_prompt = data.get("user_prompt", "") or data.get("prompt", "")
    cwd = data.get("cwd", "")

    prompt_stripped = user_prompt.strip() if user_prompt else ""
    if not prompt_stripped:
        _output({})
        return

    # Skip trivial prompts: too short or common filler phrases
    if (len(prompt_stripped) < USERPROMPT_MIN_QUERY_LEN
            or prompt_stripped.lower() in USERPROMPT_SKIP_PHRASES):
        _log(f"UserPrompt recall: skipped trivial prompt {prompt_stripped!r}")
        _output({})
        return

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
    _log(f"UserPrompt recall: query={user_prompt[:80]!r}, wing={preferred_wing}")

    # --- Stage 1: LLM query rewrite (opt-in via MEMPAL_RECALL_LLM=1) ---
    llm_config = None
    search_query = user_prompt
    time_after = None
    try:
        from .recall_llm import is_enabled, _get_llm_config, rewrite_query, rerank
        if is_enabled():
            llm_config = _get_llm_config()
        if llm_config:
            rewrite_result = rewrite_query(user_prompt, config=llm_config)
            if rewrite_result:
                search_query = rewrite_result["query"]
                time_after = rewrite_result.get("after")
                _log(f"UserPrompt recall: query rewritten to {search_query[:80]!r}, after={time_after}")
    except Exception as e:
        _log(f"UserPrompt recall: query rewrite failed ({e}), using original")

    # --- Stage 2: Vector search + BM25 hybrid rank ---
    # Fetch a larger pool when LLM rerank is available
    pool_size = USERPROMPT_RECALL_POOL if llm_config else USERPROMPT_RECALL_LIMIT
    try:
        result = search_memories(
            query=search_query,
            palace_path=palace_path,
            wing=None,  # search all wings
            preferred_wing=preferred_wing,
            n_results=pool_size,
            max_distance=USERPROMPT_MAX_DISTANCE,
            after=time_after,
        )
    except Exception as e:
        _log(f"WARNING: search_memories failed: {e}")
        _output({})
        return

    hits = result.get("results", []) if isinstance(result, dict) else []

    # Filter out diary entries — AAAK-compressed session logs are not
    # human-readable and pollute auto-recall results. Agents can still
    # find diary content via explicit mempalace_search tool calls.
    hits = [h for h in hits if h.get("room") != "diary"]

    if not hits:
        _log("UserPrompt recall: no hits")
        _output({})
        return

    # --- Stage 3: LLM rerank + relevance filter ---
    if llm_config and len(hits) > USERPROMPT_RECALL_LIMIT:
        try:
            reranked = rerank(
                user_prompt,  # use original prompt for relevance judgment
                hits,
                top_k=USERPROMPT_RECALL_LIMIT,
                config=llm_config,
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

    # Format recall block
    lines = []
    for hit in hits[:USERPROMPT_RECALL_LIMIT]:
        wing = hit.get("wing", "?")
        room = hit.get("room", "general")
        snippet = _truncate_snippet(hit.get("text", ""))
        if snippet:
            lines.append(f"- [{wing}/{room}] {snippet}")

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

    _output({
        "continue": True,
        "suppressOutput": True,
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": additional_context,
        },
    })


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
