"""
Pluggable embedding function for ChromaDB.

Routes embedding calls through any OpenAI-compatible ``/v1/embeddings``
endpoint (LiteLLM proxy, Ollama, vLLM, etc.). The Google
``generativelanguage.googleapis.com`` direct-API path has been removed —
LiteLLM (or any equivalent OpenAI-compat proxy) is the only supported
transport.

Config (all three required when overriding the ChromaDB built-in):
    MEMPAL_EMBEDDING_MODEL    — model name passed through to the proxy
                                (e.g. "gemini-embedding-2"). Set to
                                "default" or leave unset to use ChromaDB's
                                built-in all-MiniLM-L6-v2.
    MEMPAL_EMBEDDING_ENDPOINT — base URL of the OpenAI-compat proxy
                                (e.g. "http://localhost:4000").
    MEMPAL_EMBEDDING_KEY      — bearer token for the proxy
                                (e.g. "sk-litellm-local").
    MEMPAL_EMBEDDING_DIMS     — optional output dimensionality
                                (default: 3072).

Example:
    export MEMPAL_EMBEDDING_MODEL=gemini-embedding-2
    export MEMPAL_EMBEDDING_ENDPOINT=http://localhost:4000
    export MEMPAL_EMBEDDING_KEY=sk-litellm-local
"""

import json
import logging
import os
import time
import urllib.error
import urllib.request

import chromadb

logger = logging.getLogger(__name__)

DEFAULT_EMBEDDING_DIMS = 3072
MAX_RETRIES = 3
INITIAL_BACKOFF_S = 0.5


class ProxyEmbeddingFunction(chromadb.EmbeddingFunction):
    """ChromaDB embedding function backed by an OpenAI-compatible proxy.

    Sends requests to ``{endpoint}/v1/embeddings`` with the standard
    OpenAI request/response shape. Designed for LiteLLM, Ollama, vLLM,
    and any other proxy that speaks the OpenAI embeddings protocol.

    Inherits from chromadb.EmbeddingFunction to ensure full interface
    compatibility across ChromaDB versions. The base class provides
    embed_query, embed_documents, embed_with_retries, default_space, etc.
    We only need to override __call__ (the core embedding logic).

    Uses raw urllib (no SDK dependency), consistent with recall_llm.py.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        dimensions: int = DEFAULT_EMBEDDING_DIMS,
        endpoint: str = "",
    ):
        if not endpoint:
            raise ValueError("MEMPAL_EMBEDDING_ENDPOINT is required")
        self._api_key = api_key
        self._model = model
        self._dimensions = dimensions
        self._endpoint = endpoint.rstrip("/")
        self._url = f"{self._endpoint}/v1/embeddings"
        self._opener = self._build_opener()

    @staticmethod
    def _build_opener():
        """Build a urllib opener with proxy support if configured."""
        proxy = (
            os.environ.get("HTTPS_PROXY")
            or os.environ.get("https_proxy")
            or os.environ.get("ALL_PROXY")
            or os.environ.get("all_proxy")
            or ""
        )
        if proxy:
            proxy_handler = urllib.request.ProxyHandler(
                {
                    "https": proxy,
                    "http": proxy,
                }
            )
            return urllib.request.build_opener(proxy_handler)
        return None

    def __call__(self, input):
        """Embed a list of texts via the OpenAI-compat proxy."""
        if isinstance(input, str):
            input = [input]
        if not input:
            return []
        return self._embed_concurrent(input)

    def _embed_concurrent(self, texts):
        """Embed via OpenAI-compat endpoint with concurrent single-text calls.

        LiteLLM's Vertex AI proxy doesn't support batch input — it returns
        only 1 embedding regardless of input size. Work around by sending
        one text per request with a thread pool for parallelism.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        results = [None] * len(texts)
        max_workers = min(10, len(texts))

        def _embed_one(idx, text):
            emb = self._embed_batch([text])
            return idx, emb[0]

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_embed_one, i, t): i for i, t in enumerate(texts)}
            for future in as_completed(futures):
                idx, embedding = future.result()
                results[idx] = embedding

        return results

    @staticmethod
    def name() -> str:
        return "proxy"

    def default_space(self):
        return "cosine"

    def _embed_batch(self, texts):
        """Embed a batch of texts via a single OpenAI-compat request."""
        payload = json.dumps(
            {
                "model": self._model,
                "input": texts,
                "dimensions": self._dimensions,
            }
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }

        req = urllib.request.Request(
            self._url,
            data=payload,
            headers=headers,
            method="POST",
        )

        for attempt in range(MAX_RETRIES):
            try:
                if self._opener:
                    with self._opener.open(req, timeout=30) as resp:
                        result = json.loads(resp.read())
                else:
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        result = json.loads(resp.read())
                return [item["embedding"] for item in result["data"]]
            except urllib.error.HTTPError as e:
                code = getattr(e, "code", 0)
                body = ""
                try:
                    body = e.read().decode("utf-8", errors="replace")[:200]
                except Exception:
                    pass
                if code in (429, 500, 503) and attempt < MAX_RETRIES - 1:
                    backoff = INITIAL_BACKOFF_S * (2**attempt)
                    logger.warning(
                        "Proxy embedding API %d, retry %d/%d in %.1fs",
                        code,
                        attempt + 1,
                        MAX_RETRIES,
                        backoff,
                    )
                    time.sleep(backoff)
                    continue
                logger.error("Proxy embedding API failed (HTTP %d): %s", code, body)
                raise
            except Exception as e:
                if attempt < MAX_RETRIES - 1:
                    backoff = INITIAL_BACKOFF_S * (2**attempt)
                    logger.warning(
                        "Proxy embedding error (%s), retry %d/%d in %.1fs",
                        e,
                        attempt + 1,
                        MAX_RETRIES,
                        backoff,
                    )
                    time.sleep(backoff)
                    continue
                raise

        raise RuntimeError("Proxy embedding: all retries exhausted")


# deprecated: kept as a backward-compat alias for one release cycle.
# Remove in the next major version.
GeminiEmbeddingFunction = ProxyEmbeddingFunction


# ---------------------------------------------------------------------------
# Singleton factory
# ---------------------------------------------------------------------------

_cached_embedding_fn: object = "UNSET"


def get_embedding_function():
    """Return the configured embedding function, or None for ChromaDB default.

    Reads ``MEMPAL_EMBEDDING_MODEL``. When unset, empty, or "default",
    returns ``None`` (caller should fall back to the ChromaDB built-in).
    Otherwise requires ``MEMPAL_EMBEDDING_ENDPOINT`` and
    ``MEMPAL_EMBEDDING_KEY``; if either is missing, logs a warning and
    falls back to ``None`` (conservative — never crashes on misconfig).
    Result is cached at module level.
    """
    global _cached_embedding_fn
    if _cached_embedding_fn != "UNSET":
        return _cached_embedding_fn

    model = os.environ.get("MEMPAL_EMBEDDING_MODEL", "").strip()

    if not model or model.lower() == "default":
        _cached_embedding_fn = None
        return None

    endpoint = os.environ.get("MEMPAL_EMBEDDING_ENDPOINT", "").strip()
    key = os.environ.get("MEMPAL_EMBEDDING_KEY", "").strip()

    if not endpoint or not key:
        logger.warning(
            "MEMPAL_EMBEDDING_MODEL=%s but MEMPAL_EMBEDDING_ENDPOINT and/or "
            "MEMPAL_EMBEDDING_KEY is not set. Falling back to default embedding.",
            model,
        )
        _cached_embedding_fn = None
        return None

    dims_str = os.environ.get("MEMPAL_EMBEDDING_DIMS", "")
    dims = int(dims_str) if dims_str.strip().isdigit() else DEFAULT_EMBEDDING_DIMS

    logger.info(
        "Proxy embedding configured: endpoint=%s, model=%s, dims=%d",
        endpoint,
        model,
        dims,
    )
    ef = ProxyEmbeddingFunction(
        api_key=key,
        model=model,
        dimensions=dims,
        endpoint=endpoint,
    )
    # Startup probe: full round-trip through ChromaDB's embed_query path.
    try:
        ef.embed_query(input=["mempalace startup probe"])
        logger.info("Proxy embedding probe OK")
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        logger.info(
            "Proxy embedding probe could not reach %s (%s). "
            "Is LiteLLM running? Try: cd ~/.litellm && docker compose up -d",
            endpoint,
            reason,
        )
    except urllib.error.HTTPError as e:
        code = getattr(e, "code", 0)
        if code in (401, 403):
            logger.error(
                "Proxy embedding probe auth failed (HTTP %d). Check MEMPAL_EMBEDDING_KEY.",
                code,
            )
        else:
            logger.warning(
                "Proxy embedding probe failed (HTTP %d): %s (will retry on use)",
                code,
                e,
            )
    except Exception as e:
        logger.warning("Proxy embedding probe failed: %s (will retry on use)", e)
    _cached_embedding_fn = ef
    return _cached_embedding_fn


def reset_cache():
    """Reset the singleton cache. Used in tests."""
    global _cached_embedding_fn
    _cached_embedding_fn = "UNSET"
