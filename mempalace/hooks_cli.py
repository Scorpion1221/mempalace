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


def _get_mine_dir(transcript_path: str = "") -> str:
    """Determine directory to mine from MEMPAL_DIR or transcript path."""
    mempal_dir = os.environ.get("MEMPAL_DIR", "")
    if mempal_dir and os.path.isdir(mempal_dir):
        return mempal_dir
    if transcript_path:
        path = Path(transcript_path).expanduser()
        if path.is_file():
            return str(path.parent)
    return ""


_MINE_PID_FILE = STATE_DIR / "mine.pid"


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


def _mine_already_running() -> bool:
    """Return True if a background mine process from a previous hook fire is still alive."""
    try:
        pid = int(_MINE_PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return False
    return _pid_alive(pid)


def _spawn_mine(cmd: list) -> None:
    """Spawn a mine subprocess, write its PID to the lock file, log to hook.log."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log_path = STATE_DIR / "hook.log"
    with open(log_path, "a") as log_f:
        proc = subprocess.Popen(cmd, stdout=log_f, stderr=log_f)
    _MINE_PID_FILE.write_text(str(proc.pid))


def _maybe_auto_ingest(transcript_path: str = ""):
    """Run mempalace mine in background if a mine directory is available."""
    mine_dir = _get_mine_dir(transcript_path)
    if not mine_dir:
        return
    if _mine_already_running():
        _log("Skipping auto-ingest: mine already running")
        return
    try:
        _spawn_mine([sys.executable, "-m", "mempalace", "mine", mine_dir])
    except OSError:
        pass


def _mine_sync(transcript_path: str = ""):
    """Run mempalace mine synchronously (for precompact -- data must land first)."""
    mine_dir = _get_mine_dir(transcript_path)
    if not mine_dir:
        return
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        log_path = STATE_DIR / "hook.log"
        with open(log_path, "a") as log_f:
            subprocess.run(
                [sys.executable, "-m", "mempalace", "mine", mine_dir],
                stdout=log_f,
                stderr=log_f,
                timeout=60,
            )
    except (OSError, subprocess.TimeoutExpired):
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


_ASYNC_SAVE_PROMPT = """\
You are a memory librarian for MemPalace. Extract key content from this conversation and return structured JSON.
Write in the SAME LANGUAGE as the conversation (Chinese→Chinese, English→English).

## Palace Structure
- **wing**: project or domain name, lowercase with underscores (e.g. "backend_api", "infra_deploy"). Use "{wing}" as default.
- **room**: topic category, lowercase (e.g. "decisions", "code", "configuration", "bugs", "architecture", "general", "issues", "operations", or any fitting short name)
- **diary**: natural language summary of the session segment — include specific decisions, file paths, commands, technical details. Not just "discussed X", but WHAT was decided/changed/found.
- **drawers**: discrete pieces of knowledge worth remembering in future sessions. Each drawer should be self-contained — readable without the conversation context.

## Output Format
Return ONLY valid JSON:
{{"diary": "<session summary>", "drawers": [{{"wing": "<project>", "room": "<topic>", "content": "<verbatim knowledge>"}}], "kg": [{{"subject": "<entity>", "predicate": "<relationship>", "object": "<entity>"}}]}}

## Rules
- diary: 2-5 sentences, include WHY not just WHAT
- drawers: each a standalone fact/decision/config worth recalling later. Typically 0-5, but use more for rich conversations
- Skip trivial exchanges (greetings, confirmations, "OK", "继续")
- If nothing worth saving: {{"diary": "", "drawers": [], "kg": []}}
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

## Examples

Input: User asks how to connect to the staging database, assistant provides connection string requiring VPN.
Output:
{{"diary": "Provided staging database connection details. Requires VPN access on port 5432.", "drawers": [{{"wing": "backend_api", "room": "configuration", "content": "Staging DB connection: postgres://readonly@staging-db.internal:5432/app_staging (requires VPN, read-only credentials)"}}], "kg": [{{"subject": "backend_api", "predicate": "endpoint", "object": "staging-db.internal:5432/app_staging"}}]}}

Input: 用户报告搜索接口返回504超时，助手排查发现是缺少索引导致全表扫描，添加了复合索引修复。
Output:
{{"diary": "修复了搜索接口504超时问题。根因是 orders 表缺少 (user_id, created_at) 复合索引导致全表扫描，添加索引后响应时间从12s降到50ms。", "drawers": [{{"wing": "backend_api", "room": "bugs", "content": "搜索接口504超时：orders 表缺少 (user_id, created_at) 复合索引，添加后响应从12s→50ms。migration: 20260423_add_orders_search_index.sql"}}, {{"wing": "backend_api", "room": "decisions", "content": "决定对所有按 user_id 查询的表添加 (user_id, created_at) 复合索引作为默认规范"}}], "kg": [{{"subject": "backend_api", "predicate": "修复", "object": "复合索引_user_id_created_at"}}]}}

Input: Team decides to switch from REST to GraphQL for the mobile app API, with a 2-week migration plan.
Output:
{{"diary": "Architecture decision: mobile API switching from REST to GraphQL. Migration plan is 2 weeks, starting with read-only queries. Apollo Server chosen over Yoga for better caching.", "drawers": [{{"wing": "mobile_app", "room": "architecture", "content": "Mobile API migration: REST → GraphQL. Apollo Server (not Yoga) for caching. Phase 1: read-only queries (week 1), Phase 2: mutations (week 2). Existing REST endpoints kept until v3.0."}}, {{"wing": "mobile_app", "room": "decisions", "content": "Chose Apollo Server over GraphQL Yoga for mobile API — better built-in response caching and dataloader integration"}}], "kg": [{{"subject": "mobile_app", "predicate": "uses", "object": "GraphQL"}}, {{"subject": "mobile_app", "predicate": "uses", "object": "Apollo Server"}}]}}

Input: 助手帮用户重构了认证模块，从 JWT 改成了 session-based，修改了 src/auth/middleware.ts 和 src/auth/session.ts。
Output:
{{"diary": "重构认证模块：JWT → session-based auth。修改了 middleware.ts 和新建了 session.ts，session 存储在 Redis 中，TTL 24小时。", "drawers": [{{"wing": "{wing}", "room": "code", "content": "认证重构 JWT→session: 修改 src/auth/middleware.ts（移除 JWT 验证，改用 session cookie），新建 src/auth/session.ts（Redis session store, TTL=24h）"}}, {{"wing": "{wing}", "room": "decisions", "content": "认证从 JWT 改为 session-based：原因是需要支持即时吊销（JWT 无法做到），session 存 Redis，cookie httpOnly+secure"}}], "kg": [{{"subject": "{wing}", "predicate": "migrated_to", "object": "session_based_auth"}}, {{"subject": "{wing}", "predicate": "uses", "object": "Redis"}}]}}

Input: User configures CI/CD pipeline, sets up GitHub Actions with Docker build and deploy to AWS ECS.
Output:
{{"diary": "Set up CI/CD: GitHub Actions workflow builds Docker image, pushes to ECR, deploys to ECS Fargate. Added .github/workflows/deploy.yml with staging and production environments.", "drawers": [{{"wing": "{wing}", "room": "operations", "content": "CI/CD pipeline: .github/workflows/deploy.yml — build Docker → push to ECR (123456.dkr.ecr.us-east-1) → deploy ECS Fargate. Staging auto-deploys on push to develop, production requires manual approval."}}, {{"wing": "{wing}", "room": "configuration", "content": "ECS Fargate config: task def in infra/ecs-task.json, 512 CPU / 1024 MB, health check /api/health, min 2 / max 8 tasks"}}], "kg": [{{"subject": "{wing}", "predicate": "deployed_to", "object": "AWS ECS Fargate"}}, {{"subject": "{wing}", "predicate": "uses", "object": "GitHub Actions"}}]}}

Input: 用户和助手讨论了项目的技术选型，最终选择了 Next.js + tRPC + Prisma 的技术栈。
Output:
{{"diary": "完成技术选型讨论。最终确定：Next.js 14 (App Router) + tRPC v11 + Prisma ORM + PostgreSQL。选择 tRPC 而非 REST 是因为端到端类型安全。", "drawers": [{{"wing": "{wing}", "room": "architecture", "content": "技术栈选型：Next.js 14 (App Router) + tRPC v11 + Prisma ORM + PostgreSQL。前端 Tailwind CSS + shadcn/ui。部署 Vercel (frontend) + Railway (database)。"}}, {{"wing": "{wing}", "room": "decisions", "content": "选择 tRPC 而非 REST/GraphQL：端到端类型安全，无需手写 schema，和 Next.js Server Components 集成好。trade-off: 仅限 TypeScript 客户端"}}], "kg": [{{"subject": "{wing}", "predicate": "tech_stack", "object": "Next.js + tRPC + Prisma + PostgreSQL"}}]}}

Input: 用户说自己有三只猫，名字叫小柒、小布、小包。
Output:
{{"diary": "用户提供个人信息：拥有三只猫，名字分别是小柒、小布、小包。", "drawers": [{{"wing": "{wing}", "room": "diary", "content": "用户养有三只猫：小柒、小布、小包"}}], "kg": [{{"subject": "用户", "predicate": "养有", "object": "三只猫"}}, {{"subject": "用户", "predicate": "拥有", "object": "小柒"}}, {{"subject": "用户", "predicate": "拥有", "object": "小布"}}, {{"subject": "用户", "predicate": "拥有", "object": "小包"}}]}}

Input: User says "ok" / "继续" / "sounds good" with no new information.
Output:
{{"diary": "", "drawers": [], "kg": []}}

Input: User asks assistant to run tests and they all pass. No bugs found, no decisions made.
Output:
{{"diary": "Ran test suite, all tests passed.", "drawers": [], "kg": []}}

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
    """Build a compact summary of existing palace structure for Haiku context."""
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
            where={"added_by": "haiku_async_save"},
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


def _async_save_worker(transcript_text, session_id, cwd):
    """Background worker: call Haiku to extract memories, write to palace."""
    try:
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

        response = _call_llm(config, prompt, max_tokens=16000, timeout=30)
        if not response:
            _log("async save: LLM returned empty response")
            return

        start = response.find("{")
        end = response.rfind("}") + 1
        if start < 0 or end <= start:
            _log("async save: no JSON in LLM response")
            return
        data = json.loads(response[start:end])

        from .config import MempalaceConfig, sanitize_content, sanitize_name
        from .palace import get_collection

        cfg = MempalaceConfig()
        col = get_collection(cfg.palace_path, create=True)
        now = datetime.now()
        written = 0

        diary = data.get("diary", "")
        if diary and len(diary.strip()) > 20:
            import hashlib

            entry_id = (
                f"diary_{wing}_{now.strftime('%Y%m%d_%H%M%S%f')}"
                f"_{hashlib.sha256(diary.encode()).hexdigest()[:12]}"
            )
            col.add(
                ids=[entry_id],
                documents=[sanitize_content(diary)],
                metadatas=[
                    {
                        "wing": wing,
                        "room": "diary",
                        "hall": "hall_diary",
                        "topic": "auto-save",
                        "type": "diary_entry",
                        "agent": "haiku",
                        "filed_at": now.isoformat(),
                        "date": now.strftime("%Y-%m-%d"),
                    }
                ],
            )
            written += 1

        for drawer in data.get("drawers", []):
            content = drawer.get("content", "")
            if not content or len(content.strip()) < 20:
                continue
            d_wing = sanitize_name(drawer.get("wing", wing))
            d_room = sanitize_name(drawer.get("room", "general"))
            import hashlib

            from .miner import detect_hall

            d_hall = detect_hall(content)

            d_id = (
                f"drawer_{d_wing}_{d_room}"
                f"_{hashlib.sha256((d_wing + d_room + content).encode()).hexdigest()[:24]}"
            )
            col.upsert(
                ids=[d_id],
                documents=[sanitize_content(content)],
                metadatas=[
                    {
                        "wing": d_wing,
                        "room": d_room,
                        "hall": d_hall,
                        "added_by": "haiku_async_save",
                        "filed_at": now.isoformat(),
                    }
                ],
            )
            written += 1

        kg_facts = data.get("kg", [])
        kg_written = 0
        if kg_facts:
            try:
                from .knowledge_graph import KnowledgeGraph

                kg = KnowledgeGraph()
                for fact in kg_facts:
                    subj = fact.get("subject", "")
                    pred = fact.get("predicate", "")
                    obj = fact.get("object", "")
                    if subj and pred and obj:
                        existing = kg.query_entity(subj, direction="outgoing")
                        for old in existing:
                            if (
                                old.get("predicate") == pred
                                and old.get("object") != obj
                                and old.get("valid_to") is None
                            ):
                                kg.invalidate(
                                    subj, pred, old["object"], ended=now.strftime("%Y-%m-%d")
                                )
                        kg.add_triple(subj, pred, obj, valid_from=now.strftime("%Y-%m-%d"))
                        kg_written += 1
                kg.close()
            except Exception as e:
                _log(f"async save: KG write error: {e}")

        _log(
            f"async save: wrote {written} entries "
            f"(diary + {len(data.get('drawers', []))} drawers + {kg_written} kg facts)"
        )
    except Exception as e:
        _log(f"async save error: {e}")


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
        if transcript_text and os.environ.get("MEMPAL_RECALL_LLM", "") == "1":
            cwd = data.get("cwd", "")
            try:
                proc = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        "import sys, json; "
                        "d = json.load(sys.stdin); "
                        "from mempalace.hooks_cli import _async_save_worker; "
                        "_async_save_worker(d['text'], d['session'], d['cwd'])",
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                payload = json.dumps(
                    {"text": transcript_text, "session": session_id, "cwd": cwd or ""}
                )
                proc.stdin.write(payload.encode("utf-8"))
                proc.stdin.close()
                _log("async save: spawned background process")
            except Exception as e:
                _log(f"async save: failed to spawn ({e})")

        _output({})
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
    """Precompact hook: mine transcript synchronously, then allow compaction."""
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

    Returns {"rooms": [...top by count], "halls": [...top by count]}, both
    sorted by descending drawer count and capped at 12 entries each. Used as
    the source of truth so the rewrite LLM only suggests filter values that
    actually exist in the palace right now.

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
        return {
            "rooms": [r for r, _ in rooms.most_common(12)],
            "halls": [h for h, _ in halls.most_common(12)],
        }
    except Exception:
        return {}


def _get_kg_context_for_recall(hits, query=""):
    """Query knowledge graph for entities named in the user's QUERY.

    Strategy:
      1. Load all KG entity names.
      2. Match any entity whose name appears as a substring of the raw query
         (case-insensitive). Works for CJK where bigram tokenization would
         otherwise lose entity references ("猫" is 1 char, "Scorpion" is 8).
      3. Fall back to long-token matching (>=3 chars) so short/proper-noun
         queries still get some coverage when no entity name appears
         verbatim.
      4. For each matched entity, fetch current facts (valid_to IS NULL).

    We intentionally do NOT pull entities from hit wings — doing so used to
    contaminate recall, dragging in every fact about a project just because
    one of its drawers surfaced unrelated to the user's question.
    """
    del hits  # kept for caller compatibility; wing-based extraction was noisy
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
        lines = []
        seen = set()
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
                        lines.append(f"- [KG] {subj} → {pred} → {obj}")
            if len(lines) >= 8:
                break
        kg.close()
        return lines[:8]
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
    KG recall (e.g. "小柒怎么样" → "小柒 状态" when "小柒" is a known entity).

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


def _build_active_context(cwd: str, palace_path: str = None) -> object:
    """Build active_context payload for the recall gate.

    Includes the workdir (project hint), palace taxonomy (rooms/halls for
    valid filter values), and top KG entity names (so the gate can rewrite
    queries to echo canonical entity names). Falls back to a plain cwd string
    when no taxonomy/entities are available, keeping the shape backward
    compatible with older gate prompts.
    """
    ctx: dict = {"cwd": cwd}
    if palace_path:
        taxonomy = _get_palace_taxonomy(palace_path)
        if taxonomy:
            ctx["palace"] = taxonomy
    entities = _get_palace_kg_entities(limit=60)
    if entities:
        ctx["entities"] = entities
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
    cwd = data.get("cwd", "")
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

    # --- Stage 1: LLM query rewrite (opt-in via MEMPAL_RECALL_LLM=1) ---
    llm_config = None
    time_after = None
    rewrite_filters: dict = {}
    try:
        from .recall_llm import is_enabled, _get_llm_config, decide_recall, rerank

        if is_enabled():
            llm_config = _get_llm_config()
        if llm_config:
            # Build active context: cwd + palace taxonomy (rooms/halls) + top
            # KG entities, so the gate can pick valid filter values AND rewrite
            # the query to echo canonical entity names when the user implicitly
            # references one ("小柒怎么样" → include "小柒").
            active_ctx = _build_active_context(cwd, palace_path)
            taxonomy = active_ctx.get("palace") if isinstance(active_ctx, dict) else {}
            recall_decision = decide_recall(
                user_prompt,
                config=llm_config,
                previous_assistant_context={"tail": previous_assistant_tail},
                active_context=active_ctx,
            )
            if recall_decision:
                if not recall_decision.get("should_recall"):
                    _log(
                        "UserPrompt recall: LLM skipped recall "
                        f"reason={recall_decision.get('reason', 'unknown')}"
                    )
                    _output({})
                    return
                search_query = recall_decision["query"]
                time_after = recall_decision.get("after")
                # Validate LLM-suggested filters against real palace taxonomy.
                # Drop any value the LLM hallucinated so we never filter to an
                # empty result set over a bogus hall/room name.
                raw_filters = recall_decision.get("filters") or {}
                valid_rooms = set(taxonomy.get("rooms", [])) if taxonomy else set()
                valid_halls = set(taxonomy.get("halls", [])) if taxonomy else set()
                if raw_filters.get("room") and (not valid_rooms or raw_filters["room"] in valid_rooms):
                    rewrite_filters["room"] = raw_filters["room"]
                if raw_filters.get("hall") and (not valid_halls or raw_filters["hall"] in valid_halls):
                    rewrite_filters["hall"] = raw_filters["hall"]
                if raw_filters.get("wing"):
                    rewrite_filters["wing"] = raw_filters["wing"]
                _log(
                    "UserPrompt recall: "
                    f"LLM decided recall reason={recall_decision.get('reason', 'unknown')}, "
                    f"query={search_query[:80]!r}, after={time_after}, "
                    f"filters={rewrite_filters or 'none'}"
                )
            else:
                # LLM returned None (API failure / parse error).
                # If the user explicitly references history, fallback to search.
                # Otherwise fail closed — don't waste time on generic queries.
                from .recall_llm import _HISTORY_REFERENCE_HINTS

                prompt_lower = user_prompt.lower()
                has_history_ref = any(h in prompt_lower for h in _HISTORY_REFERENCE_HINTS)
                if has_history_ref:
                    _log(
                        "UserPrompt recall: LLM decide returned None, but history ref detected — fallback to search"
                    )
                else:
                    _log("UserPrompt recall: LLM decide returned None, fail closed")
                    _output({})
                    return
    except Exception as e:
        from .recall_llm import _HISTORY_REFERENCE_HINTS

        prompt_lower = user_prompt.lower()
        has_history_ref = any(h in prompt_lower for h in _HISTORY_REFERENCE_HINTS)
        if has_history_ref:
            _log(
                f"UserPrompt recall: decide+rewrite failed ({e}), but history ref detected — fallback to search"
            )
        else:
            _log(f"UserPrompt recall: decide+rewrite failed ({e}), fail closed")
            _output({})
            return

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
            # If the filter was too tight (empty pool), retry without filters
            # so we don't fail closed on a hallucinated-but-valid label.
            if (
                rewrite_filters
                and isinstance(result, dict)
                and not result.get("error")
                and len(result.get("results") or []) == 0
            ):
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
    # as "session logs", but Haiku async save now stores valuable personal
    # facts (e.g. "user has three cats") as diary entries too. Let the
    # reranker decide — it correctly identifies relevance.

    if not hits:
        _log("UserPrompt recall: no hits")
        _output({})
        return

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

    # Format recall block
    lines = []
    for hit in hits[:USERPROMPT_RECALL_LIMIT]:
        wing = hit.get("wing", "?")
        room = hit.get("room", "general")
        created = hit.get("created_at", "")
        date_tag = f" ({created[:10]})" if created and len(created) >= 10 else ""
        snippet = _truncate_snippet(hit.get("text", ""))
        if snippet:
            lines.append(f"- [{wing}/{room}]{date_tag} {snippet}")

    kg_lines = _get_kg_context_for_recall(hits, query=user_prompt)
    if kg_lines:
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

    _output(
        {
            "continue": True,
            "suppressOutput": True,
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": additional_context,
            },
        }
    )


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
