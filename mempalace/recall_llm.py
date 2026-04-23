"""
recall_llm.py — LLM-powered recall enhancement for MemPalace hooks.

Two-stage pipeline that improves memory retrieval precision:

Stage 1 — Query Rewrite:
    Transforms natural-language user prompts into keyword-rich search queries
    that better match stored memory content. Bridges the semantic gap between
    how users ask ("测试环境怎么用DBeaver查看mysql数据库？") and how memories
    are stored ("MySQL 数据库连接信息 Host Port Database").

Stage 2 — Rerank/Filter:
    Takes a pool of candidates from vector search and uses the LLM to select
    the most relevant subset, filtering noise from high-frequency but
    irrelevant entries (diary, task logs).

Supports three API backends (in priority order):
    1. MEMPAL_RECALL_ENDPOINT (explicit — LiteLLM proxy, Ollama, etc.)
    2. Vertex AI (CLAUDE_CODE_USE_VERTEX=1 + gcloud credentials)
    3. Anthropic native API (ANTHROPIC_API_KEY)

All stages gracefully degrade on failure — the caller falls back to the
original query / original ranking.

Opt-in via environment variable:
    MEMPAL_RECALL_LLM=1   — enable LLM-enhanced recall (default: off)
"""

from collections.abc import Mapping
from datetime import date, timedelta
import json
import logging
import os
import re
import subprocess
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

# --- Configuration ---

# Env var priority: MEMPAL_RECALL_ENDPOINT > Vertex AI > ANTHROPIC_API_KEY
DEFAULT_ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_VERTEX_MODEL = "claude-haiku-4-5"
VERTEX_LOCATION = "us-east5"
REWRITE_TIMEOUT_S = 8
RERANK_TIMEOUT_S = 8
REWRITE_MAX_TOKENS = 200
RERANK_MAX_TOKENS = 50
PREVIOUS_ASSISTANT_TAIL_MAX_CHARS = 1200
_PREVIOUS_ASSISTANT_TAIL_KEYS = (
    "previous_assistant_message_tail",
    "assistant_message_tail",
    "previous_assistant_tail",
    "assistant_tail",
    "message_tail",
    "tail",
    "text",
    "content",
)
_PREVIOUS_ASSISTANT_CONTAINER_KEYS = (
    "previous_assistant_message",
    "previous_assistant",
    "assistant_message",
    "assistant",
)
_SESSION_LOCAL_CONTINUE_PREFIXES = (
    "继续",
    "继续推进",
    "继续做",
    "继续修",
    "继续改",
    "继续处理",
    "继续完成",
    "接着做",
    "接着推进",
    "往下做",
    "往下推进",
    "把剩下的做完",
    "把剩下的修完",
    "推进下去",
    "go ahead",
    "keep going",
    "keep working",
    "finish it",
    "finish this",
    "continue",
    "continue fixing",
    "continue implementing",
    "continue working",
    "proceed",
)
_HISTORY_REFERENCE_HINTS = (
    "之前",
    "上次",
    "上回",
    " earlier ",
    " last time",
    " before ",
    " previous ",
    " prior ",
    " history ",
    " remember ",
    " earlier",
    "last time",
    "before",
    "previous",
    "prior",
)


def is_enabled() -> bool:
    """Check if LLM-enhanced recall is enabled. Opt-in via MEMPAL_RECALL_LLM=1."""
    return os.environ.get("MEMPAL_RECALL_LLM", "") == "1"


# Cache gcloud access token within a single hook invocation
_vertex_token_cache: dict = {}


def _get_vertex_token() -> str | None:
    """Get a GCP access token via application-default credentials. Cached per process."""
    if "token" in _vertex_token_cache:
        return _vertex_token_cache["token"]
    try:
        # Use application-default credentials (service account key via
        # GOOGLE_APPLICATION_CREDENTIALS), not the user's gcloud login.
        # Pass GOOGLE_APPLICATION_CREDENTIALS explicitly in env so it works
        # even when the parent process (e.g. launchd) doesn't inherit shell vars.
        env = dict(os.environ)
        gac = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
        if gac:
            env["GOOGLE_APPLICATION_CREDENTIALS"] = gac
        result = subprocess.run(
            ["gcloud", "auth", "application-default", "print-access-token"],
            capture_output=True,
            text=True,
            timeout=5,
            env=env,
        )
        token = result.stdout.strip()
        if token and result.returncode == 0:
            _vertex_token_cache["token"] = token
            return token
    except Exception as e:
        logger.debug("gcloud ADC token fetch failed: %s", e)
    _vertex_token_cache["token"] = None
    return None


def _get_llm_config() -> dict | None:
    """Resolve LLM config for recall enhancement.

    Returns dict with keys: backend ("vertex" | "anthropic" | "openai_compat"),
    plus backend-specific fields, or None if no API is configured.
    """
    # Priority 1: MEMPAL_RECALL_ENDPOINT (explicit override — LiteLLM, Ollama, etc.)
    # When set, always use this regardless of Vertex or Anthropic config.
    endpoint = os.environ.get("MEMPAL_RECALL_ENDPOINT", "")
    llm_model = os.environ.get("MEMPAL_RECALL_MODEL", "")
    if endpoint and llm_model:
        return {
            "backend": "openai_compat",
            "endpoint": endpoint.rstrip("/"),
            "model": llm_model,
            "key": os.environ.get("MEMPAL_RECALL_KEY", ""),
        }

    # Priority 2: Vertex AI (Claude Code Vertex mode)
    if os.environ.get("CLAUDE_CODE_USE_VERTEX") == "1":
        project = os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID", "")
        if project:
            token = _get_vertex_token()
            if token:
                model = os.environ.get("MEMPAL_RECALL_MODEL", DEFAULT_VERTEX_MODEL)
                location = os.environ.get("MEMPAL_VERTEX_LOCATION", "")
                if not location:
                    cloud_region = os.environ.get("CLOUD_ML_REGION", "")
                    location = (
                        cloud_region
                        if cloud_region and cloud_region != "global"
                        else VERTEX_LOCATION
                    )
                return {
                    "backend": "vertex",
                    "project": project,
                    "location": location,
                    "model": model,
                    "token": token,
                }

    # Priority 3: Anthropic native API
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if api_key:
        model = os.environ.get("MEMPAL_RECALL_MODEL", DEFAULT_ANTHROPIC_MODEL)
        return {"backend": "anthropic", "api_key": api_key, "model": model}

    return None


def _call_vertex(
    project: str,
    location: str,
    model: str,
    token: str,
    prompt: str,
    max_tokens: int,
    timeout: int,
) -> str | None:
    """Call Vertex AI Claude endpoint. Returns response text or None."""
    # Vertex AI uses the Anthropic Messages API format
    url = (
        f"https://{location}-aiplatform.googleapis.com/v1/"
        f"projects/{project}/locations/{location}/"
        f"publishers/anthropic/models/{model}:rawPredict"
    )
    payload = json.dumps(
        {
            "anthropic_version": "vertex-2023-10-16",
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read())
        return result["content"][0]["text"].strip()
    except Exception as e:
        logger.debug("Vertex AI call failed: %s", e)
        return None


def _call_anthropic(
    api_key: str,
    model: str,
    prompt: str,
    max_tokens: int,
    timeout: int,
) -> str | None:
    """Call Anthropic Messages API. Returns response text or None."""
    payload = json.dumps(
        {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read())
        return result["content"][0]["text"].strip()
    except Exception as e:
        logger.debug("Anthropic API call failed: %s", e)
        return None


def _call_openai_compat(
    endpoint: str,
    model: str,
    key: str,
    prompt: str,
    max_tokens: int,
    timeout: int,
) -> str | None:
    """Call OpenAI-compatible /chat/completions. Returns response text or None."""
    payload = json.dumps(
        {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode("utf-8")

    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    req = urllib.request.Request(
        f"{endpoint}/chat/completions",
        data=payload,
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read())
        return result["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.debug("OpenAI-compat API call failed: %s", e)
        return None


def _call_llm(config: dict, prompt: str, max_tokens: int, timeout: int) -> str | None:
    """Dispatch LLM call to the configured backend."""
    backend = config["backend"]
    if backend == "vertex":
        return _call_vertex(
            config["project"],
            config["location"],
            config["model"],
            config["token"],
            prompt,
            max_tokens,
            timeout,
        )
    if backend == "anthropic":
        return _call_anthropic(
            config["api_key"],
            config["model"],
            prompt,
            max_tokens,
            timeout,
        )
    return _call_openai_compat(
        config["endpoint"],
        config["model"],
        config.get("key", ""),
        prompt,
        max_tokens,
        timeout,
    )


# =========================================================================
# Shared prompt helpers
# =========================================================================


def _extract_previous_assistant_tail(
    previous_assistant_context: str | Mapping | None,
) -> str | None:
    """Extract the previous assistant reply tail from a string or structured payload."""
    if previous_assistant_context is None:
        return None

    if isinstance(previous_assistant_context, str):
        tail = previous_assistant_context.strip()
        if not tail:
            return None
        if len(tail) > PREVIOUS_ASSISTANT_TAIL_MAX_CHARS:
            tail = tail[-PREVIOUS_ASSISTANT_TAIL_MAX_CHARS:]
        return tail

    if not isinstance(previous_assistant_context, Mapping):
        return None

    for key in _PREVIOUS_ASSISTANT_TAIL_KEYS:
        value = previous_assistant_context.get(key)
        if isinstance(value, str) and value.strip():
            return _extract_previous_assistant_tail(value)

    for key in _PREVIOUS_ASSISTANT_CONTAINER_KEYS:
        value = previous_assistant_context.get(key)
        if isinstance(value, Mapping):
            tail = _extract_previous_assistant_tail(value)
            if tail:
                return tail

    return None


def _render_previous_assistant_tail(previous_assistant_context: str | Mapping | None) -> str:
    """Render previous assistant context for prompts."""
    tail = _extract_previous_assistant_tail(previous_assistant_context)
    return tail if tail else "(none provided)"


def _normalize_message(text: str) -> str:
    """Normalize a user message for local rule checks."""
    return " ".join((text or "").strip().lower().split())


def _starts_with_any_phrase(text: str, phrases: tuple[str, ...]) -> bool:
    """Check whether normalized text starts with any continuation phrase."""
    for phrase in phrases:
        if (
            text == phrase
            or text.startswith(f"{phrase} ")
            or text.startswith(f"{phrase},")
            or text.startswith(f"{phrase}:")
            or text.startswith(f"{phrase}，")
            or text.startswith(f"{phrase}：")
        ):
            return True
    return False


def local_recall_decision(
    user_prompt: str,
    previous_assistant_context: str | Mapping | None = None,
    active_context: str | Mapping | None = None,
) -> dict | None:
    """Return a local no-recall decision for obvious session-local cases."""
    del previous_assistant_context, active_context  # reserved for future tuning

    normalized = _normalize_message(user_prompt)
    if not normalized:
        return None

    # Keep recall enabled when the user explicitly references past state.
    for hint in _HISTORY_REFERENCE_HINTS:
        if hint in normalized:
            return None

    if _starts_with_any_phrase(normalized, _SESSION_LOCAL_CONTINUE_PREFIXES):
        return {
            "should_recall": False,
            "reason": "session_local_continue_no_memory_needed",
            "query": None,
            "after": None,
        }

    return None


# =========================================================================
# Stage 1: Decide whether recall is needed + rewrite query
# =========================================================================

_DECIDE_RECALL_PROMPT = """\
You are a recall gate and search query optimizer for a personal memory database. The database stores notes, configs, decisions, and logs as text chunks with timestamps.

You will receive:
- CURRENT USER MESSAGE — this is the primary signal.
- PREVIOUS ASSISTANT MESSAGE TAIL — optional context only. Use it only if it helps clarify the current user message. Ignore it if irrelevant, stale, or conflicting.
- ACTIVE CONTEXT — optional project/workdir hint.

Decide whether memory recall is needed for this turn.

Output JSON only — no explanation:
{{"should_recall": true, "reason": "short_machine_label", "query": "english keywords here or null", "after": "YYYY-MM-DD or null"}}

The key question: does the user need information from MEMORY (past sessions) to handle this turn, or is the current conversation thread sufficient?

Rules (in priority order — earlier rules override later ones):
- RULE 1 (highest priority) — "should_recall": false for session-local messages:
  - 1a. Continuation / execution-control: any form of "继续" + optional verb/clause (继续, 继续补, 继续做, 继续推进 直到完成, etc.), "keep going", "go ahead", "finish it", "continue", "proceed". Includes continuations with conditions or goals ("继续推进，直到完全修复完成").
  - 1b. Confirmations and acknowledgements: "好的/ok/行/可以/做吧/是的/yes/嗯/got it/sounds good"
  - 1c. Session-local topic redirects: "先不管这个，帮我看看X" / "换个方向" / "skip that, do X instead" — the user is redirecting within the current thread, not asking for memory.
  - 1d. Proximal references to current-thread content: "这个报错怎么修", "那个函数怎么改" — when there is no history keyword, "这个/那个/this/that" refers to something in the current thread.
  - These ALL refer to the CURRENT conversation thread. Shortness or ambiguity is NOT a reason to recall — it means the user expects the assistant to use in-thread context.
  - EXCEPTION: override to should_recall=true if the message contains EXPLICIT history-referencing words: "之前", "上次", "上回", "以前", "当时", "那时候", "还记得", "earlier", "last time", "previous", "remember", "prior", "history", "historically"
- RULE 2 — "should_recall": false for self-contained tasks:
  - direct code execution, inspection, file operations ("run the tests", "帮我把这个函数改成async")
  - text transformation, formatting, translation, summarization ("翻译成英文")
  - mechanical edits, greetings
- RULE 3 — "should_recall": true ONLY when the answer requires information from PAST SESSIONS or OTHER PROJECTS that is not in the current thread or current codebase:
  - 3a. Explicit history references: messages containing any of the history-referencing words listed in RULE 1 EXCEPTION.
  - 3b. Cross-project queries: asking about a project/system NOT in the current working directory. Infer the current project name from the last segment of ACTIVE CONTEXT path. If the user names a different project/system, recall is likely needed.
  - 3c. Past decisions or preferences: "我们怎么决定的", "用什么方案", deployment procedures, architectural choices from earlier conversations.
  - 3d. Temporal queries about past work: "昨天那个bug", "上周的进展"
  - The test: if the assistant can handle this turn using ONLY the current conversation + current codebase, recall is NOT needed.

Query rewrite rules (only when should_recall is true):
- "query": rewrite into a SHORT PHRASE that captures the search intent. Keep the SAME LANGUAGE as the user's message — if the user writes Chinese, output Chinese; if English, output English. The downstream search uses vector embeddings that work best with same-language matching.
- Preserve proper nouns EXACTLY (project names, tool names, people names) in their original form.
- For mixed-language content, keep the dominant language and preserve technical terms as-is (e.g. "recall gate 中文继续消息误判" is fine — don't translate to pure English or pure Chinese).
- If the PREVIOUS ASSISTANT MESSAGE TAIL contains entity names or specifics that the user's message references implicitly, include them in the query. E.g. user says "那个bug修了吗", assistant tail mentions "auth middleware session leak" → query should be "auth middleware session leak bug修复状态", mixing languages as needed for best retrieval.
- Max 200 chars. Remove filler words but keep semantic structure.
- "query": if should_recall is false, output null.

Other fields:
- "reason" must be a short snake_case label.
- "after": extract temporal intent if present. Prefer the current user message. Use the previous assistant message tail only for disambiguation when clearly relevant.
- Time reference mapping: "今天/today" → {today}, "昨天/yesterday" → {yesterday}, "上周/last week" → 7 days before today, "之前/before" → null (too vague for date filter).

Examples (user message → expected output):

"继续补" → {{"should_recall":false,"reason":"continuation_directive","query":null,"after":null}}
"继续推进，直到完全修复完成" → {{"should_recall":false,"reason":"continuation_with_goal","query":null,"after":null}}
"好的，做吧" → {{"should_recall":false,"reason":"confirmation","query":null,"after":null}}
"帮我把这个函数改成async" → {{"should_recall":false,"reason":"direct_code_task","query":null,"after":null}}
"run the tests" → {{"should_recall":false,"reason":"direct_execution","query":null,"after":null}}
"这个报错怎么修" → {{"should_recall":false,"reason":"proximal_reference_current_thread","query":null,"after":null}}
"那个文件有什么问题" → {{"should_recall":false,"reason":"proximal_reference_current_thread","query":null,"after":null}}
"翻译成英文" → {{"should_recall":false,"reason":"text_transformation","query":null,"after":null}}
"先不管这个，帮我看看那个文件" → {{"should_recall":false,"reason":"session_local_redirect","query":null,"after":null}}
"上次那个部署脚本放哪了" → {{"should_recall":true,"reason":"past_session_reference","query":"上次部署脚本位置","after":null}}
"我们之前决定用什么方案来做缓存的" → {{"should_recall":true,"reason":"past_decision_reference","query":"之前缓存方案决策","after":null}}
"昨天那个bug修了吗" → {{"should_recall":true,"reason":"past_work_status","query":"昨天bug修复状态","after":"{yesterday}"}}
"Hermes的飞书网关是怎么实现的" → {{"should_recall":true,"reason":"cross_project_query","query":"Hermes飞书网关实现","after":null}}
"按之前那个方案继续推进" → {{"should_recall":true,"reason":"continuation_referencing_past_decision","query":"之前的实现方案","after":null}}
"where did we put the deploy script last time" → {{"should_recall":true,"reason":"past_session_reference","query":"deploy script location last time","after":null}}

Today is {today}.

Current user message:
{user_message}

Previous assistant message tail (optional context only):
{previous_assistant_tail}

Active context:
{active_context}

JSON:"""


def _render_active_context(active_context: str | Mapping | None) -> str:
    """Render optional active context for prompts."""
    if active_context is None:
        return "(none provided)"
    if isinstance(active_context, str):
        text = active_context.strip()
        return text if text else "(none provided)"
    if isinstance(active_context, Mapping):
        try:
            rendered = json.dumps(active_context, ensure_ascii=False, sort_keys=True)
        except TypeError:
            rendered = str(active_context)
        return rendered if rendered else "(none provided)"
    return str(active_context)


def _build_decide_recall_prompt(
    user_prompt: str,
    previous_assistant_context: str | Mapping | None = None,
    active_context: str | Mapping | None = None,
    *,
    today: str | None = None,
) -> str:
    """Build the decide+rewrite prompt with optional previous assistant context."""
    today_str = today or date.today().isoformat()
    yesterday_str = (date.fromisoformat(today_str) - timedelta(days=1)).isoformat()
    return _DECIDE_RECALL_PROMPT.format(
        today=today_str,
        yesterday=yesterday_str,
        user_message=user_prompt,
        previous_assistant_tail=_render_previous_assistant_tail(previous_assistant_context),
        active_context=_render_active_context(active_context),
    )


def _parse_bool(value: object) -> bool | None:
    """Parse a bool or bool-like string, returning None when invalid."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1"}:
            return True
        if normalized in {"false", "no", "0"}:
            return False
    return None


def _normalize_after(after: object) -> str | None:
    """Validate and normalize an ISO date or null-like value."""
    if after and after != "null":
        try:
            date.fromisoformat(str(after))
            return str(after)
        except (ValueError, TypeError):
            return None
    return None


def _parse_decide_recall_result(result: str) -> dict | None:
    """Parse the LLM decide+rewrite response into a normalized dict."""
    if not result:
        return None

    result = result.strip()
    if result.startswith("```"):
        result = re.sub(r"^```(?:json)?\s*", "", result)
        result = re.sub(r"\s*```$", "", result)

    try:
        parsed = json.loads(result)
    except json.JSONDecodeError:
        clean = result.strip().strip("\"'")
        if 3 <= len(clean) <= 300:
            return {
                "should_recall": True,
                "reason": "query_string_fallback",
                "query": clean,
                "after": None,
            }
        return None

    if isinstance(parsed, str):
        clean = parsed.strip().strip("\"'")
        if 3 <= len(clean) <= 300:
            return {
                "should_recall": True,
                "reason": "query_string_fallback",
                "query": clean,
                "after": None,
            }
        return None

    if not isinstance(parsed, dict):
        return None

    should_recall = _parse_bool(parsed.get("should_recall"))
    query = parsed.get("query")
    if query == "null":
        query = None
    if isinstance(query, str):
        query = query.strip() or None
    elif query is not None:
        query = str(query).strip() or None

    # Backward compatibility: older rewrite-only responses may omit should_recall.
    if should_recall is None:
        should_recall = bool(query)

    if not should_recall:
        return {
            "should_recall": False,
            "reason": str(parsed.get("reason") or "llm_decided_no_recall"),
            "query": None,
            "after": None,
        }

    if not query or len(query) < 3:
        return None

    return {
        "should_recall": True,
        "reason": str(parsed.get("reason") or "llm_decided_recall"),
        "query": query,
        "after": _normalize_after(parsed.get("after")),
    }


def decide_recall(
    user_prompt: str,
    config: dict | None = None,
    previous_assistant_context: str | Mapping | None = None,
    active_context: str | Mapping | None = None,
) -> dict | None:
    """Use one LLM call to decide whether recall is needed and rewrite query.

    Args:
        user_prompt: Current user message.
        config: LLM config dict (from _get_llm_config).
        previous_assistant_context: Optional previous assistant reply tail,
            either as a raw string or structured mapping (for example
            ``{"tail": "..."}``). This is context-only and ignored when absent.
        active_context: Optional current project/workdir or other local context.

    Returns dict with keys:
        should_recall (bool): Whether memory recall should be attempted.
        reason (str): Short machine label describing the decision.
        query (str): Rewritten English keyword query.
        after (str|None): ISO date string for time filtering, or None.
    Returns None if LLM is unavailable/fails.
    """
    if config is None:
        config = _get_llm_config()
    if config is None:
        return None

    prompt = _build_decide_recall_prompt(
        user_prompt,
        previous_assistant_context,
        active_context,
    )
    result = _call_llm(config, prompt, REWRITE_MAX_TOKENS, REWRITE_TIMEOUT_S)
    return _parse_decide_recall_result(result)


def rewrite_query(
    user_prompt: str,
    config: dict | None = None,
    previous_assistant_context: str | Mapping | None = None,
    active_context: str | Mapping | None = None,
) -> dict | None:
    """Backward-compatible wrapper returning only rewrite fields when recall is needed."""
    decision = decide_recall(
        user_prompt,
        config=config,
        previous_assistant_context=previous_assistant_context,
        active_context=active_context,
    )
    if not decision or not decision.get("should_recall"):
        return None
    return {
        "query": decision["query"],
        "after": decision.get("after"),
    }


# =========================================================================
# Stage 2: Rerank
# =========================================================================

_RERANK_PROMPT = """\
You are a relevance judge for a personal memory search.

You will receive:
- CURRENT USER MESSAGE — this is the primary signal and should drive the relevance decision.
- PREVIOUS ASSISTANT MESSAGE TAIL — optional context only. Use it only if it helps clarify the current user message. Ignore it if irrelevant, stale, or conflicting.
- {n} candidate memory snippets.

Select the candidate memories that are relevant to the current user message. Return up to {k} results.

Rules:
- Include candidates that contain information the user is looking for, even if only partially relevant.
- A candidate mentioning the same project, system, or topic as the user's question is likely relevant — include it.
- Prefer to INCLUDE borderline candidates rather than exclude them — false negatives (missing a relevant memory) are worse than false positives (including a marginally relevant one).
- Use the previous assistant message tail only when it materially clarifies the current user message.
- Reply NONE only if the candidates are clearly about completely unrelated topics.
- Otherwise reply with ONLY the numbers of relevant candidates, separated by commas, in order of relevance
- Example (some relevant): 3,1,7
- Example (none relevant): NONE

Current user message:
{user_message}

Previous assistant message tail (optional context only):
{previous_assistant_tail}

Candidates:
{candidates}

Relevant candidates:"""


def _build_rerank_prompt(
    user_prompt: str,
    hits: list,
    top_k: int,
    previous_assistant_context: str | Mapping | None = None,
) -> str:
    """Build the rerank prompt with optional previous assistant context."""
    candidate_blocks = []
    for i, hit in enumerate(hits):
        text = hit.get("text", "")[:800].replace("\n", " ").strip()
        wing = hit.get("wing", "?")
        room = hit.get("room", "?")
        candidate_blocks.append(f"{i + 1}. [{wing}/{room}] {text}")

    return _RERANK_PROMPT.format(
        n=len(hits),
        k=top_k,
        user_message=user_prompt,
        previous_assistant_tail=_render_previous_assistant_tail(previous_assistant_context),
        candidates="\n\n".join(candidate_blocks),
    )


def rerank(
    user_prompt: str,
    hits: list,
    top_k: int = 5,
    config: dict | None = None,
    previous_assistant_context: str | Mapping | None = None,
) -> list | None:
    """Use LLM to rerank search hits by relevance, filtering irrelevant ones.

    Args:
        user_prompt: Current user message.
        hits: List of search result dicts (must have "text" key).
        top_k: Maximum number of results to select.
        config: LLM config dict (from _get_llm_config).
        previous_assistant_context: Optional previous assistant reply tail,
            either as a raw string or structured mapping (for example
            ``{"tail": "..."}``). This is context-only and ignored when absent.

    Returns:
        Reordered hits list (0 to top_k items), or None if LLM call fails.
        Returns empty list if LLM determines no candidates are relevant.
    """
    if config is None:
        config = _get_llm_config()
    if config is None:
        return None

    if len(hits) <= top_k:
        return hits

    prompt = _build_rerank_prompt(
        user_prompt,
        hits,
        top_k,
        previous_assistant_context=previous_assistant_context,
    )

    result = _call_llm(config, prompt, RERANK_MAX_TOKENS, RERANK_TIMEOUT_S)
    if not result:
        return None

    # LLM says nothing is relevant — fallback to top BM25 hit if any has
    # a nonzero keyword score (avoids losing exact keyword matches).
    if "NONE" in result.upper():
        bm25_fallback = [h for h in hits if h.get("bm25_score", 0) > 0]
        if bm25_fallback:
            bm25_fallback.sort(key=lambda h: h.get("bm25_score", 0), reverse=True)
            return bm25_fallback[:top_k]
        return []

    # Parse comma-separated numbers
    numbers = re.findall(r"\d+", result)
    seen = set()
    reranked = []
    for num_str in numbers:
        idx = int(num_str) - 1  # 1-indexed to 0-indexed
        if 0 <= idx < len(hits) and idx not in seen:
            seen.add(idx)
            reranked.append(hits[idx])
        if len(reranked) >= top_k:
            break

    # Empty list after parsing = LLM returned numbers but all were invalid.
    # Treat as LLM failure (return None → caller falls back to BM25 order),
    # distinct from explicit "NONE" response (return [] → no relevant results).
    if not reranked:
        return None
    return reranked
