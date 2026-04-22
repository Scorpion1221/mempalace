"""
Pluggable embedding function support for MemPalace.

Allows switching from ChromaDB's default all-MiniLM-L6-v2 to a custom
embedding model (e.g. gemini-embedding-2) via environment variable.

Config:
    MEMPAL_EMBEDDING_MODEL  — "default" (or unset) for ChromaDB built-in,
                              or a Gemini model name like "gemini-embedding-2"
    GEMINI_API_KEY          — required when using a Gemini model
    MEMPAL_EMBEDDING_DIMS   — output dimensionality (default: 3072 for Gemini)
"""

import json
import logging
import math
import os
import time
import urllib.error
import urllib.request

import chromadb

logger = logging.getLogger(__name__)

GEMINI_BATCH_LIMIT = 100
GEMINI_DEFAULT_DIMS = 3072
GEMINI_DEFAULT_MODEL = "gemini-embedding-2-preview"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
MAX_RETRIES = 3
INITIAL_BACKOFF_S = 0.5


class GeminiEmbeddingFunction(chromadb.EmbeddingFunction):
    """ChromaDB embedding function using Google's Gemini API.

    Inherits from chromadb.EmbeddingFunction to ensure full interface
    compatibility across ChromaDB versions. The base class provides
    embed_query, embed_documents, embed_with_retries, default_space, etc.
    We only need to override __call__ (the core embedding logic).

    Uses raw urllib (no SDK dependency), consistent with recall_llm.py.
    """

    def __init__(
        self,
        api_key: str,
        model: str = GEMINI_DEFAULT_MODEL,
        dimensions: int = GEMINI_DEFAULT_DIMS,
        endpoint: str = "",
    ):
        self._api_key = api_key
        self._model = model
        self._dimensions = dimensions
        self._endpoint = endpoint.rstrip("/") if endpoint else ""
        if self._endpoint:
            self._url = f"{self._endpoint}/v1/embeddings"
            self._mode = "openai"
        else:
            self._url = (
                f"{GEMINI_API_BASE}/models/{model}:batchEmbedContents"
                f"?key={api_key}"
            )
            self._mode = "gemini"
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
            proxy_handler = urllib.request.ProxyHandler({
                "https": proxy,
                "http": proxy,
            })
            return urllib.request.build_opener(proxy_handler)
        return None

    def __call__(self, input):
        """Embed a list of texts. Handles batching for large inputs."""
        if isinstance(input, str):
            input = [input]
        if not input:
            return []

        all_embeddings = []
        n_batches = math.ceil(len(input) / GEMINI_BATCH_LIMIT)

        for i in range(n_batches):
            batch = input[i * GEMINI_BATCH_LIMIT : (i + 1) * GEMINI_BATCH_LIMIT]
            embeddings = self._embed_batch(batch)
            all_embeddings.extend(embeddings)

        return all_embeddings

    @staticmethod
    def name() -> str:
        return "gemini"

    def default_space(self):
        return "cosine"

    def _embed_batch(self, texts):
        """Embed a single batch (up to GEMINI_BATCH_LIMIT texts)."""
        if self._mode == "openai":
            payload = json.dumps({
                "model": self._model,
                "input": texts,
                "dimensions": self._dimensions,
            }).encode("utf-8")
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            }
        else:
            payload = json.dumps({
                "requests": [
                    {
                        "model": f"models/{self._model}",
                        "content": {"parts": [{"text": t}]},
                        "outputDimensionality": self._dimensions,
                    }
                    for t in texts
                ]
            }).encode("utf-8")
            headers = {"Content-Type": "application/json"}

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
                if self._mode == "openai":
                    return [item["embedding"] for item in result["data"]]
                return [e["values"] for e in result["embeddings"]]
            except urllib.error.HTTPError as e:
                code = getattr(e, "code", 0)
                body = ""
                try:
                    body = e.read().decode("utf-8", errors="replace")[:200]
                except Exception:
                    pass
                if code in (429, 500, 503) and attempt < MAX_RETRIES - 1:
                    backoff = INITIAL_BACKOFF_S * (2 ** attempt)
                    logger.warning(
                        "Gemini embedding API %d, retry %d/%d in %.1fs",
                        code, attempt + 1, MAX_RETRIES, backoff,
                    )
                    time.sleep(backoff)
                    continue
                logger.error("Gemini embedding API failed (HTTP %d): %s", code, body)
                raise
            except Exception as e:
                err_str = str(e)
                is_ssl = "CERTIFICATE_VERIFY_FAILED" in err_str or "SSL" in err_str
                if is_ssl:
                    logger.error(
                        "Gemini embedding SSL error: %s. "
                        "Fix: set SSL_CERT_FILE env var to your cert.pem path. "
                        "On macOS with homebrew: SSL_CERT_FILE=/opt/homebrew/etc/openssl@3/cert.pem",
                        e,
                    )
                    raise
                if attempt < MAX_RETRIES - 1:
                    backoff = INITIAL_BACKOFF_S * (2 ** attempt)
                    logger.warning(
                        "Gemini embedding error (%s), retry %d/%d in %.1fs",
                        e, attempt + 1, MAX_RETRIES, backoff,
                    )
                    time.sleep(backoff)
                    continue
                raise

        raise RuntimeError("Gemini embedding: all retries exhausted")


# ---------------------------------------------------------------------------
# Singleton factory
# ---------------------------------------------------------------------------

_cached_embedding_fn: object = "UNSET"


def get_embedding_function():
    """Return the configured embedding function, or None for ChromaDB default.

    Reads MEMPAL_EMBEDDING_MODEL (or MEMPALACE_EMBEDDING_MODEL) env var.
    Returns None when unset/default, or a GeminiEmbeddingFunction instance.
    Result is cached at module level.
    """
    global _cached_embedding_fn
    if _cached_embedding_fn != "UNSET":
        return _cached_embedding_fn

    model = (
        os.environ.get("MEMPAL_EMBEDDING_MODEL")
        or os.environ.get("MEMPALACE_EMBEDDING_MODEL")
        or ""
    ).strip()

    if not model or model.lower() == "default":
        _cached_embedding_fn = None
        return None

    if model.startswith("gemini"):
        endpoint = (
            os.environ.get("MEMPAL_EMBEDDING_ENDPOINT")
            or os.environ.get("MEMPALACE_EMBEDDING_ENDPOINT")
            or ""
        ).strip()
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()

        if endpoint:
            key = (
                os.environ.get("MEMPAL_EMBEDDING_KEY")
                or api_key
                or os.environ.get("LITELLM_KEY", "")
                or "sk-litellm-local"
            ).strip()
            logger.info("Using Gemini embedding via proxy: endpoint=%s, model=%s", endpoint, model)
        else:
            key = api_key
            if not key:
                logger.warning(
                    "MEMPAL_EMBEDDING_MODEL=%s but no GEMINI_API_KEY or MEMPAL_EMBEDDING_ENDPOINT set. "
                    "Falling back to default embedding.",
                    model,
                )
                _cached_embedding_fn = None
                return None

        dims_str = os.environ.get("MEMPAL_EMBEDDING_DIMS", "")
        dims = int(dims_str) if dims_str.strip().isdigit() else GEMINI_DEFAULT_DIMS

        logger.info(
            "Using Gemini embedding: model=%s, dims=%d", model, dims,
        )
        ef = GeminiEmbeddingFunction(
            api_key=key, model=model, dimensions=dims, endpoint=endpoint,
        )
        # Startup probe: full round-trip through ChromaDB's embed_query path
        try:
            ef.embed_query(input=["mempalace startup probe"])
            logger.info("Gemini embedding probe OK")
        except Exception as e:
            err_str = str(e)
            if "CERTIFICATE_VERIFY_FAILED" in err_str or "SSL" in err_str:
                logger.error(
                    "Gemini embedding FAILED: SSL certificate error. "
                    "Set SSL_CERT_FILE env var. On macOS: "
                    "SSL_CERT_FILE=/opt/homebrew/etc/openssl@3/cert.pem"
                )
            elif "location is not supported" in err_str:
                logger.error(
                    "Gemini embedding FAILED: region not supported. "
                    "Set HTTPS_PROXY to a US/EU proxy."
                )
            else:
                logger.warning("Gemini embedding probe failed: %s (will retry on use)", e)
        _cached_embedding_fn = ef
        return _cached_embedding_fn

    logger.warning("Unknown embedding model %r, falling back to default.", model)
    _cached_embedding_fn = None
    return None


def reset_cache():
    """Reset the singleton cache. Used in tests."""
    global _cached_embedding_fn
    _cached_embedding_fn = "UNSET"
