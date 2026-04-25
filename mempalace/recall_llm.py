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
# `MEMPAL_RECALL_MODEL` is required for every backend — no implicit fallback.
# When unset (or empty), the corresponding backend is treated as unconfigured
# and skipped. This avoids silently locking users into a vendor default.
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
        model = os.environ.get("MEMPAL_RECALL_MODEL", "").strip()
        if project and model:
            token = _get_vertex_token()
            if token:
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
    model = os.environ.get("MEMPAL_RECALL_MODEL", "").strip()
    if api_key and model:
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
    *,
    json_mode: bool = False,
) -> str | None:
    """Call Vertex AI Claude endpoint. Returns response text or None.

    When ``json_mode`` is True, uses an assistant-prefill trick: appends an
    assistant message containing ``{`` so the model must continue with JSON.
    The returned text is prefixed with ``{`` to form a complete JSON document
    (the API only returns what comes AFTER the prefill).
    """
    # Vertex AI uses the Anthropic Messages API format
    url = (
        f"https://{location}-aiplatform.googleapis.com/v1/"
        f"projects/{project}/locations/{location}/"
        f"publishers/anthropic/models/{model}:rawPredict"
    )
    messages: list[dict] = [{"role": "user", "content": prompt}]
    if json_mode:
        messages.append({"role": "assistant", "content": "{"})
    payload = json.dumps(
        {
            "anthropic_version": "vertex-2023-10-16",
            "max_tokens": max_tokens,
            "messages": messages,
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
        text = result["content"][0]["text"].strip()
        if json_mode and not text.startswith("{"):
            text = "{" + text
        return text
    except Exception as e:
        logger.debug("Vertex AI call failed: %s", e)
        return None


def _call_anthropic(
    api_key: str,
    model: str,
    prompt: str,
    max_tokens: int,
    timeout: int,
    *,
    json_mode: bool = False,
) -> str | None:
    """Call Anthropic Messages API. Returns response text or None.

    When ``json_mode`` is True, uses an assistant-prefill trick: appends an
    assistant message containing ``{`` so the model must continue with JSON.
    The returned text is prefixed with ``{`` to form a complete JSON document
    (the API only returns what comes AFTER the prefill).
    """
    messages: list[dict] = [{"role": "user", "content": prompt}]
    if json_mode:
        messages.append({"role": "assistant", "content": "{"})
    payload = json.dumps(
        {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
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
        text = result["content"][0]["text"].strip()
        if json_mode and not text.startswith("{"):
            text = "{" + text
        return text
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
    *,
    json_mode: bool = False,
) -> str | None:
    """Call OpenAI-compatible /chat/completions. Returns response text or None.

    When ``json_mode`` is True, sets ``response_format={"type": "json_object"}``
    on the request. LiteLLM proxies forward this to Vertex/Anthropic/OpenAI
    as appropriate and most providers honor it.
    """
    body: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    payload = json.dumps(body).encode("utf-8")

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


def _call_llm(
    config: dict,
    prompt: str,
    max_tokens: int,
    timeout: int,
    *,
    json_mode: bool = False,
) -> str | None:
    """Dispatch LLM call to the configured backend.

    When ``json_mode`` is True, the backend is instructed (via
    ``response_format`` for OpenAI-compat or an assistant prefill for
    Anthropic/Vertex) to emit a valid JSON object. The returned string is
    guaranteed to start with ``{`` in that case.
    """
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
            json_mode=json_mode,
        )
    if backend == "anthropic":
        return _call_anthropic(
            config["api_key"],
            config["model"],
            prompt,
            max_tokens,
            timeout,
            json_mode=json_mode,
        )
    return _call_openai_compat(
        config["endpoint"],
        config["model"],
        config.get("key", ""),
        prompt,
        max_tokens,
        timeout,
        json_mode=json_mode,
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
You are a recall gate and search query optimizer for a personal memory database. The database stores notes, configs, decisions, and logs as text chunks with timestamps, organized by wing (project/topic), room (aspect), and hall (category).

You will receive:
- CURRENT USER MESSAGE — this is the primary signal.
- PREVIOUS ASSISTANT MESSAGE TAIL — optional context only. Use it only if it helps clarify the current user message. Ignore it if irrelevant, stale, or conflicting.
- ACTIVE CONTEXT — optional project/workdir hint, the current palace taxonomy (available rooms/halls), a list of KG entities currently in the palace, AND a `preferred_wing` naming the user's current project. Use the taxonomy to pick valid filter values; use the entity list to spot when the user's question implicitly references an entity already in memory; use preferred_wing as the DEFAULT wing scope for project-relative queries.

Decide whether memory recall is needed for this turn, and what search filter will most precisely locate the answer.

Output JSON only — no explanation:
{{"should_recall": true, "reason": "short_machine_label", "query": "keywords here or null", "after": "YYYY-MM-DD or null", "filters": {{"wing": null, "room": null, "hall": null}}}}

The key question: does the user need information from MEMORY (past sessions) to handle this turn, or is the current conversation thread sufficient?

Rules (in priority order — earlier rules override later ones):
- RULE 1 (highest priority) — "should_recall": false for session-local messages:
  - 1a. Continuation / execution-control: any form of "继续" + optional verb/clause (继续, 继续补, 继续做, 继续推进 直到完成, etc.), "keep going", "go ahead", "finish it", "continue", "proceed". Includes continuations with conditions or goals ("继续推进，直到完全修复完成").
  - 1b. Confirmations and acknowledgements: "好的/ok/行/可以/做吧/是的/yes/嗯/got it/sounds good"
  - 1c. Session-local topic redirects: "先不管这个，帮我看看X" / "换个方向" / "skip that, do X instead" — the user is redirecting within the current thread, not asking for memory.
  - 1d. Proximal references to current-thread content: "这个报错怎么修", "那个函数怎么改" — when there is no history keyword, "这个/那个/this/that" refers to something in the current thread.
  - These ALL refer to the CURRENT conversation thread. Shortness or ambiguity is NOT a reason to recall — it means the user expects the assistant to use in-thread context.
  - EXCEPTION A — override to should_recall=true if the message contains EXPLICIT history-referencing words: "之前", "上次", "上回", "以前", "当时", "那时候", "还记得", "earlier", "last time", "previous", "remember", "prior", "history", "historically"
  - EXCEPTION B — override to should_recall=true if the message is a PERSONAL-FACT QUERY about the user themselves or their stable context (identity, biography, relationships, possessions, preferences, habitual locations, roles). The defining trait is that the answer is a specific fact the palace would have stored, not reasoning the assistant can do from thread context. Questions of this shape almost always warrant a memory lookup — even when the same fact was discussed earlier in this thread, memory is the canonical source and the user may be verifying storage or expecting a stored-version answer.
- RULE 2 — "should_recall": false for self-contained tasks:
  - direct code execution, inspection, file operations ("run the tests", "帮我把这个函数改成async")
  - text transformation, formatting, translation, summarization ("翻译成英文")
  - mechanical edits, greetings
- RULE 3 — "should_recall": true ONLY when the answer requires information from PAST SESSIONS or OTHER PROJECTS that is not in the current thread or current codebase:
  - 3a. Explicit history references: messages containing any of the history-referencing words listed in RULE 1 EXCEPTION A.
  - 3b. Cross-project queries: asking about a project/system NOT in the current working directory. Infer the current project name from the last segment of ACTIVE CONTEXT path. If the user names a different project/system, recall is likely needed.
  - 3c. Past decisions or preferences: "我们怎么决定的", "用什么方案", deployment procedures, architectural choices from earlier conversations.
  - 3d. Temporal queries about past work: "昨天那个bug", "上周的进展"
  - 3e. Personal facts about the user themselves — see RULE 1 EXCEPTION B.
  - The test: if the assistant can handle this turn using ONLY the current conversation + current codebase, recall is NOT needed.

Query rewrite rules (only when should_recall is true):
- "query": rewrite into a SHORT PHRASE that captures the search intent. Keep the SAME LANGUAGE as the user's message — if the user writes Chinese, output Chinese; if English, output English. The downstream search uses vector embeddings that work best with same-language matching.
- Preserve proper nouns EXACTLY (project names, tool names, people names) in their original form.
- For mixed-language content, keep the dominant language and preserve technical terms as-is (e.g. "recall gate 中文继续消息误判" is fine — don't translate to pure English or pure Chinese).
- If the PREVIOUS ASSISTANT MESSAGE TAIL contains entity names or specifics that the user's message references implicitly, include them in the query. E.g. user says "那个bug修了吗", assistant tail mentions "auth middleware session leak" → query should be "auth middleware session leak bug修复状态", mixing languages as needed for best retrieval.
- Entity expansion: if the user's query mentions OR implicitly refers to any entity from the ACTIVE CONTEXT entity list, include that entity name VERBATIM in the rewritten query. This boosts both vector recall and KG matching. The entity list is the source of truth — only echo names that are in it; never invent. Pattern: paraphrase plus the canonical name (e.g. user "the staging api" + entity list contains "auth-gateway-staging" → query "auth-gateway-staging staging api").
- Max 200 chars. Remove filler words but keep semantic structure.
- "query": if should_recall is false, output null.

Filter selection rules (output goes in "filters"):
- Use "filters" to narrow the search BEFORE ranking. Values must come from ACTIVE CONTEXT's palace taxonomy — never invent room or hall names the palace doesn't actually have. If unsure, leave a field null.
- Strong mapping by question type:
  - Personal facts about the user (RULE 3e / EXCEPTION B): prefer hall="hall_diary" if that hall exists in the taxonomy; otherwise prefer room="diary" if present. Personal-fact halls/rooms isolate biographical content from technical drawers, sharply improving precision.
  - Past decisions / architecture / "what approach did we pick": prefer room="decisions" or room="architecture" if present.
  - Bug history / incidents / error recurrence: prefer room="bugs" if present.
  - Deployment / configuration / ops: prefer room="configuration" or room="operations" if present.
  - Code-level queries ("where is function X implemented"): prefer room="code" if present.
  - Cross-project / open exploratory / no strong hint: leave both null — rely on ranking.
- Only ONE of room or hall is usually enough. Use hall when it is a cleaner signal (personal-fact halls like hall_diary, hall_identity, hall_family), use room otherwise.
- "wing": DEFAULT to ACTIVE CONTEXT's `preferred_wing` for project-scoped queries — "our bugs", "how did we fix X", "当前测试失败", "recent decisions", or any question that implicitly refers to the user's current project. This is the most impactful filter for preventing cross-project noise.
  - Set wing=null ONLY when the user EXPLICITLY references a different project by name ("Hermes的飞书网关", "solvely-web 的路由问题"), or when the question is clearly cross-project / open-exploratory ("which projects use Redis").
  - For personal-fact queries about the user themselves (RULE 3e / EXCEPTION B), leave wing=null — personal facts cross project boundaries.
  - If preferred_wing is missing from ACTIVE CONTEXT, leave wing=null.
- If should_recall is false, output {{"wing": null, "room": null, "hall": null}}.

Other fields:
- "reason" must be a short snake_case label.
- "after": extract temporal intent if present. Prefer the current user message. Use the previous assistant message tail only for disambiguation when clearly relevant.
- Time reference mapping: "今天/today" → {today}, "昨天/yesterday" → {yesterday}, "上周/last week" → 7 days before today, "之前/before" → null (too vague for date filter).

Examples (user message → expected output):

In examples below, "<preferred_wing>" is a placeholder meaning "whatever preferred_wing is in ACTIVE CONTEXT" — echo that value into filters.wing when defaulting to the active project. If preferred_wing is missing, leave wing=null.

"继续补" → {{"should_recall":false,"reason":"continuation_directive","query":null,"after":null,"filters":{{"wing":null,"room":null,"hall":null}}}}
"好的，做吧" → {{"should_recall":false,"reason":"confirmation","query":null,"after":null,"filters":{{"wing":null,"room":null,"hall":null}}}}
"帮我把这个函数改成async" → {{"should_recall":false,"reason":"direct_code_task","query":null,"after":null,"filters":{{"wing":null,"room":null,"hall":null}}}}
"这个报错怎么修" → {{"should_recall":false,"reason":"proximal_reference_current_thread","query":null,"after":null,"filters":{{"wing":null,"room":null,"hall":null}}}}
"翻译成英文" → {{"should_recall":false,"reason":"text_transformation","query":null,"after":null,"filters":{{"wing":null,"room":null,"hall":null}}}}
"我老婆生日是哪天" → {{"should_recall":true,"reason":"personal_fact_query","query":"配偶 生日","after":null,"filters":{{"wing":null,"room":"diary","hall":"hall_diary"}}}}
"我现在住在哪个城市" → {{"should_recall":true,"reason":"personal_fact_query","query":"居住 城市","after":null,"filters":{{"wing":null,"room":"diary","hall":"hall_diary"}}}}
"what is my employee id" → {{"should_recall":true,"reason":"personal_fact_query","query":"employee id number","after":null,"filters":{{"wing":null,"room":"diary","hall":"hall_diary"}}}}
"我们之前决定用什么方案来做缓存的" → {{"should_recall":true,"reason":"past_decision_reference","query":"之前缓存方案决策","after":null,"filters":{{"wing":"<preferred_wing>","room":"decisions","hall":null}}}}
"昨天那个bug修了吗" → {{"should_recall":true,"reason":"past_work_status","query":"昨天bug修复状态","after":"{yesterday}","filters":{{"wing":"<preferred_wing>","room":"bugs","hall":null}}}}
"测试失败怎么查" → {{"should_recall":true,"reason":"project_bug_query","query":"测试失败 排查","after":null,"filters":{{"wing":"<preferred_wing>","room":"bugs","hall":null}}}}
"feishu-gateway 是怎么实现的" → {{"should_recall":true,"reason":"cross_project_query","query":"feishu-gateway 实现","after":null,"filters":{{"wing":null,"room":"architecture","hall":null}}}}
"the staging api 最近有什么变动" (with entity list including "auth-gateway-staging") → {{"should_recall":true,"reason":"entity_status_query","query":"auth-gateway-staging staging api 变动","after":null,"filters":{{"wing":"<preferred_wing>","room":null,"hall":null}}}}
"上次那个部署脚本放哪了" → {{"should_recall":true,"reason":"past_session_reference","query":"上次部署脚本位置","after":null,"filters":{{"wing":"<preferred_wing>","room":"operations","hall":null}}}}
"where did we put the deploy script last time" → {{"should_recall":true,"reason":"past_session_reference","query":"deploy script location last time","after":null,"filters":{{"wing":"<preferred_wing>","room":"operations","hall":null}}}}

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


_ALLOWED_FILTER_KEYS = ("wing", "room", "hall")


def _normalize_filters(raw: object) -> dict:
    """Extract {wing, room, hall} from the LLM filter object.

    Any null/"null"/empty string values are stripped. Non-string values are
    coerced or dropped. Unknown keys are ignored. Values are NOT validated
    against the actual palace taxonomy — callers are expected to cross-check
    against their current wings/rooms/halls before applying.
    """
    if not isinstance(raw, Mapping):
        return {}
    out: dict = {}
    for key in _ALLOWED_FILTER_KEYS:
        value = raw.get(key)
        if value is None or value == "null":
            continue
        if isinstance(value, str):
            v = value.strip()
            if v and v.lower() != "null":
                out[key] = v
    return out


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
                "filters": {},
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
                "filters": {},
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
            "filters": {},
        }

    if not query or len(query) < 3:
        return None

    return {
        "should_recall": True,
        "reason": str(parsed.get("reason") or "llm_decided_recall"),
        "query": query,
        "after": _normalize_after(parsed.get("after")),
        "filters": _normalize_filters(parsed.get("filters")),
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
        filters (dict): Optional {wing?, room?, hall?} narrowing the search.
            Caller must validate values against the current palace taxonomy
            before passing them to search_memories — the LLM may hallucinate
            or use stale labels.
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
    result = _call_llm(
        config,
        prompt,
        REWRITE_MAX_TOKENS,
        REWRITE_TIMEOUT_S,
        json_mode=True,
    )
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
- CURRENT USER MESSAGE — what the user just asked; this drives the decision.
- PREVIOUS ASSISTANT MESSAGE TAIL — optional context. Use only if it clarifies the user message; ignore if stale or irrelevant.
- {n} candidate memory snippets.

Your job: select candidates that would actually help the assistant ANSWER the user's question. Return up to {k} results, most useful first.

The test for each candidate:
  "If the assistant quoted this snippet in its reply, would it move the answer forward — or would it just look topically related?"
Topic overlap alone is NOT enough. A snippet must contain information the assistant would use in its answer.

Distinguish a FACT from META-DISCUSSION about that fact:
- STATES the fact ("I moved to Berlin in March 2024") → relevant to "when did I move?"
- DISCUSSES the topic in meta fashion — as an example in a bug report, a test fixture, a changelog entry, or a sample query used to demo the search system → NOT relevant, even when it repeats the user's exact words. For instance, a drawer logging "fixed wrong indexing for the query <user-question-here>" describes the search system; it does not answer the question.
- Drawers about the memory/search/indexing system itself are almost never the answer — exclude unless the user is asking about that system.

Question-type calibration:
- Factual / personal ("how many X", "what is my Y", "who is Z"): include only candidates that state the fact. Topic-adjacent commentary is noise.
- Decision / preference ("how did we decide X", "what approach for Y"): include candidates that record the decision or rationale. Status pings naming X don't qualify.
- Status / progress ("is X done", "state of Y"): include candidates that report status or recent activity on X.
- Open / exploratory ("tell me about Z", "what do I know about Z"): be more inclusive — anything substantively about Z qualifies.

When in doubt:
- Factual / personal questions → exclude borderline candidates.
- Exploratory questions → include them.

Reply NONE when no candidate would actually contribute to the answer — even if some repeat the user's words. Returning NONE is the correct call when the palace doesn't contain the answer; injecting topic-matched noise is worse than injecting nothing.

Output format:
- Comma-separated indices in relevance order, e.g. 3,1,7
- Or NONE if nothing helps.

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

    if not hits:
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

    # LLM says nothing is relevant
    if "NONE" in result.upper():
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
