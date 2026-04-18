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

logger = logging.getLogger(__name__)

GEMINI_BATCH_LIMIT = 100
GEMINI_DEFAULT_DIMS = 3072
GEMINI_DEFAULT_MODEL = "gemini-embedding-2-preview"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
MAX_RETRIES = 3
INITIAL_BACKOFF_S = 0.5


class GeminiEmbeddingFunction:
    """ChromaDB-compatible embedding function using Google's Gemini API.

    Implements the ChromaDB EmbeddingFunction protocol: __call__ takes a
    list of strings, returns a list of float lists.

    Uses raw urllib (no SDK dependency), consistent with recall_llm.py.
    """

    def __init__(
        self,
        api_key: str,
        model: str = GEMINI_DEFAULT_MODEL,
        dimensions: int = GEMINI_DEFAULT_DIMS,
    ):
        self._api_key = api_key
        self._model = model
        self._dimensions = dimensions
        self._url = (
            f"{GEMINI_API_BASE}/models/{model}:batchEmbedContents"
            f"?key={api_key}"
        )

    def __call__(self, input: list[str]) -> list[list[float]]:
        """Embed a list of texts. Handles batching for large inputs."""
        if not input:
            return []

        all_embeddings: list[list[float]] = []
        n_batches = math.ceil(len(input) / GEMINI_BATCH_LIMIT)

        for i in range(n_batches):
            batch = input[i * GEMINI_BATCH_LIMIT : (i + 1) * GEMINI_BATCH_LIMIT]
            embeddings = self._embed_batch(batch)
            all_embeddings.extend(embeddings)

        return all_embeddings

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a single batch (up to GEMINI_BATCH_LIMIT texts)."""
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

        req = urllib.request.Request(
            self._url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        for attempt in range(MAX_RETRIES):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    result = json.loads(resp.read())
                return [e["values"] for e in result["embeddings"]]
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 503) and attempt < MAX_RETRIES - 1:
                    backoff = INITIAL_BACKOFF_S * (2 ** attempt)
                    logger.warning(
                        "Gemini embedding API %d, retry %d/%d in %.1fs",
                        e.code, attempt + 1, MAX_RETRIES, backoff,
                    )
                    time.sleep(backoff)
                    continue
                logger.error("Gemini embedding API failed: %s", e)
                raise
            except Exception as e:
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
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not api_key:
            logger.warning(
                "MEMPAL_EMBEDDING_MODEL=%s but GEMINI_API_KEY is not set. "
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
        _cached_embedding_fn = GeminiEmbeddingFunction(
            api_key=api_key, model=model, dimensions=dims,
        )
        return _cached_embedding_fn

    logger.warning("Unknown embedding model %r, falling back to default.", model)
    _cached_embedding_fn = None
    return None


def reset_cache():
    """Reset the singleton cache. Used in tests."""
    global _cached_embedding_fn
    _cached_embedding_fn = "UNSET"
