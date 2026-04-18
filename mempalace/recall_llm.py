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
from datetime import date
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


def _extract_previous_assistant_tail(previous_assistant_context: str | Mapping | None) -> str | None:
    """Extract the previous assistant reply tail from a string or structured payload."""
    if previous_assistant_context is None:
        return None

    if isinstance(previous_assistant_context, str):
        tail = previous_assistant_context.strip()
        if not tail:
            return None
        if len(tail) > PREVIOUS_ASSISTANT_TAIL_MAX_CHARS:
            tail = tail[-PREVIOUS_ASSISTANT_TAIL_MAX_CHARS :]
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


# =========================================================================
# Stage 1: Query Rewrite
# =========================================================================

_REWRITE_PROMPT = """\
You are a search query optimizer for a personal memory database. The database stores notes, configs, decisions, and logs as text chunks with timestamps.

You will receive:
- CURRENT USER MESSAGE — this is the primary signal and should drive the query rewrite.
- PREVIOUS ASSISTANT MESSAGE TAIL — optional context only. Use it only if it helps clarify the current user message. Ignore it if irrelevant, stale, or conflicting.

Output JSON only — no explanation:
{{"query": "english keywords here", "after": "YYYY-MM-DD or null"}}

Rules:
- "query": ALWAYS English keywords, translate if needed. Preserve proper nouns exactly. Remove filler words. Max 200 chars.
- "after": extract temporal intent if present. Prefer the current user message. Use the previous assistant message tail only for disambiguation when clearly relevant.
- Time examples: "今天/today" → today's date, "昨天/yesterday" → yesterday, "上周/last week" → 7 days ago, "之前/before" → null (no time filter).

Today is {today}.

Current user message:
{user_message}

Previous assistant message tail (optional context only):
{previous_assistant_tail}

JSON:"""


def _build_rewrite_prompt(
    user_prompt: str,
    previous_assistant_context: str | Mapping | None = None,
    *,
    today: str | None = None,
) -> str:
    """Build the rewrite prompt with optional previous assistant context."""
    return _REWRITE_PROMPT.format(
        today=today or date.today().isoformat(),
        user_message=user_prompt,
        previous_assistant_tail=_render_previous_assistant_tail(previous_assistant_context),
    )


def rewrite_query(
    user_prompt: str,
    config: dict | None = None,
    previous_assistant_context: str | Mapping | None = None,
) -> dict | None:
    """Use LLM to rewrite a user prompt into an optimized search query.

    Args:
        user_prompt: Current user message.
        config: LLM config dict (from _get_llm_config).
        previous_assistant_context: Optional previous assistant reply tail,
            either as a raw string or structured mapping (for example
            ``{"tail": "..."}``). This is context-only and ignored when absent.

    Returns dict with keys:
        query (str): Rewritten English keyword query.
        after (str|None): ISO date string for time filtering, or None.
    Returns None if LLM is unavailable/fails.
    """
    if config is None:
        config = _get_llm_config()
    if config is None:
        return None

    prompt = _build_rewrite_prompt(user_prompt, previous_assistant_context)
    result = _call_llm(config, prompt, REWRITE_MAX_TOKENS, REWRITE_TIMEOUT_S)

    if not result:
        return None

    # Parse JSON response
    result = result.strip()
    # Strip markdown code fences if present
    if result.startswith("```"):
        result = re.sub(r"^```(?:json)?\s*", "", result)
        result = re.sub(r"\s*```$", "", result)

    try:
        parsed = json.loads(result)
    except json.JSONDecodeError:
        # Fallback: treat entire result as query string
        clean = result.strip().strip('"\'')
        if 3 <= len(clean) <= 300:
            return {"query": clean, "after": None}
        return None

    if isinstance(parsed, str):
        clean = parsed.strip().strip("\"'")
        if 3 <= len(clean) <= 300:
            return {"query": clean, "after": None}
        return None

    if not isinstance(parsed, dict):
        return None

    query = parsed.get("query", "")
    if not query or len(query) < 3:
        return None

    after = parsed.get("after")
    # Validate date format
    if after and after != "null":
        try:
            date.fromisoformat(after)
        except (ValueError, TypeError):
            after = None
    else:
        after = None

    return {"query": query, "after": after}


# =========================================================================
# Stage 2: Rerank
# =========================================================================

_RERANK_PROMPT = """\
You are a relevance judge for a personal memory search.

You will receive:
- CURRENT USER MESSAGE — this is the primary signal and should drive the relevance decision.
- PREVIOUS ASSISTANT MESSAGE TAIL — optional context only. Use it only if it helps clarify the current user message. Ignore it if irrelevant, stale, or conflicting.
- {n} candidate memory snippets.

Select ONLY the candidate memories that are actually relevant to the current user message. Return up to {k} results.

Rules:
- Only include candidates that would genuinely help answer the current user message.
- Use the previous assistant message tail only when it materially clarifies the current user message.
- If none are relevant, reply with: NONE
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
        text = hit.get("text", "")[:400].replace("\n", " ").strip()
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
