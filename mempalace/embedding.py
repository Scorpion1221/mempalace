"""Embedding function factory with hardware acceleration.

Returns a ChromaDB-compatible embedding function bound to a user-selected
ONNX Runtime execution provider. The same ``all-MiniLM-L6-v2`` model and
384-dim vectors ChromaDB ships by default are reused, so switching device
does not invalidate existing palaces.

Supported devices (env ``MEMPALACE_EMBEDDING_DEVICE`` or ``embedding_device``
in ``~/.mempalace/config.json``):

* ``auto`` — prefer CUDA ▸ CoreML ▸ DirectML, fall back to CPU
* ``cpu`` — force CPU (the historical default)
* ``cuda`` — NVIDIA GPU via ``onnxruntime-gpu`` (``pip install mempalace[gpu]``)
* ``coreml`` — Apple Neural Engine (macOS)
* ``dml`` — DirectML (Windows / AMD / Intel GPUs)

Requesting an unavailable accelerator emits a warning and falls back to CPU
rather than hard-failing — mining must still work on a laptop without CUDA.
"""

from __future__ import annotations

import logging
import json
import os
import time
import urllib.error
import urllib.request
from typing import Optional

import chromadb

logger = logging.getLogger(__name__)

DEFAULT_EMBEDDING_DIMS = 3072
MAX_RETRIES = 3
INITIAL_BACKOFF_S = 0.5

_PROVIDER_MAP = {
    "cpu": ["CPUExecutionProvider"],
    "cuda": ["CUDAExecutionProvider", "CPUExecutionProvider"],
    "coreml": ["CoreMLExecutionProvider", "CPUExecutionProvider"],
    "dml": ["DmlExecutionProvider", "CPUExecutionProvider"],
}

_DEVICE_EXTRA = {
    "cuda": "mempalace[gpu]",
    "coreml": "mempalace[coreml]",
    "dml": "mempalace[dml]",
}

_AUTO_ORDER = [
    ("CUDAExecutionProvider", "cuda"),
    ("CoreMLExecutionProvider", "coreml"),
    ("DmlExecutionProvider", "dml"),
]

_EF_CACHE: dict = {}
_PROXY_EF_CACHE: object = "UNSET"
_WARNED: set = set()


class ProxyEmbeddingFunction(chromadb.EmbeddingFunction):
    """ChromaDB embedding function backed by an OpenAI-compatible proxy.

    Enabled only when ``MEMPAL_EMBEDDING_MODEL`` is set. Requests are sent to
    ``{MEMPAL_EMBEDDING_ENDPOINT}/v1/embeddings`` using the standard OpenAI
    request/response shape (LiteLLM, Ollama, vLLM, etc.).
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
        proxy = (
            os.environ.get("HTTPS_PROXY")
            or os.environ.get("https_proxy")
            or os.environ.get("ALL_PROXY")
            or os.environ.get("all_proxy")
            or ""
        )
        if proxy:
            return urllib.request.build_opener(
                urllib.request.ProxyHandler({"https": proxy, "http": proxy})
            )
        return None

    @staticmethod
    def name() -> str:
        return "proxy"

    def default_space(self):
        return "cosine"

    def __call__(self, input):
        if isinstance(input, str):
            input = [input]
        if not input:
            return []
        return self._embed_concurrent(input)

    def _embed_concurrent(self, texts):
        """Embed via concurrent single-text calls.

        Some OpenAI-compatible proxies (notably Vertex via LiteLLM) accept
        batch input but return a single embedding. Per-text calls preserve
        cardinality while still keeping mining throughput reasonable.
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

    def _embed_batch(self, texts):
        payload = json.dumps(
            {"model": self._model, "input": texts, "dimensions": self._dimensions}
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        req = urllib.request.Request(self._url, data=payload, headers=headers, method="POST")

        for attempt in range(MAX_RETRIES):
            try:
                opener = self._opener.open if self._opener else urllib.request.urlopen
                with opener(req, timeout=30) as resp:
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


# Deprecated backward-compatible alias for one release cycle.
GeminiEmbeddingFunction = ProxyEmbeddingFunction


def _resolve_providers(device: str) -> tuple[list, str]:
    """Return ``(provider_list, effective_device)`` for ``device``.

    Falls back to CPU (with a one-shot warning) when the requested
    accelerator is not compiled into the installed ``onnxruntime``.
    """
    device = (device or "auto").strip().lower()

    try:
        import onnxruntime as ort

        available = set(ort.get_available_providers())
    except ImportError:
        return (["CPUExecutionProvider"], "cpu")

    if device == "auto":
        for provider, name in _AUTO_ORDER:
            if provider in available:
                return ([provider, "CPUExecutionProvider"], name)
        return (["CPUExecutionProvider"], "cpu")

    requested = _PROVIDER_MAP.get(device)
    if requested is None:
        if device not in _WARNED:
            logger.warning("Unknown embedding_device %r — falling back to cpu", device)
            _WARNED.add(device)
        return (["CPUExecutionProvider"], "cpu")

    preferred = requested[0]
    if preferred == "CPUExecutionProvider":
        return (requested, "cpu")

    if preferred not in available:
        if device not in _WARNED:
            extra = _DEVICE_EXTRA.get(device, "the matching mempalace extra for your device")
            logger.warning(
                "embedding_device=%r requested but %s is not installed — "
                "falling back to CPU. Install %s.",
                device,
                preferred,
                extra,
            )
            _WARNED.add(device)
        return (["CPUExecutionProvider"], "cpu")

    return (requested, device)


def _build_ef_class():
    """Subclass ``ONNXMiniLM_L6_V2`` with name ``"default"``.

    Why the rename: ChromaDB 1.5 persists the EF identity on the collection
    and rejects reads that pass a differently-named EF (``onnx_mini_lm_l6_v2``
    vs ``default``). The vectors and model are identical — only the
    ``name()`` tag differs — so spoofing the name lets one EF class serve
    palaces created with ``DefaultEmbeddingFunction`` *and* palaces we
    create ourselves, with the same GPU-capable ``preferred_providers``.
    """
    from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2

    class _MempalaceONNX(ONNXMiniLM_L6_V2):
        @staticmethod
        def name() -> str:
            return "default"

    return _MempalaceONNX


def _proxy_model_from_env() -> str:
    """Return configured proxy model, or ``""`` when local ONNX should be used."""
    model = os.environ.get("MEMPAL_EMBEDDING_MODEL", "").strip()
    if not model or model.lower() == "default":
        return ""
    return model


def _get_proxy_embedding_function():
    """Return a cached proxy EF when MEMPAL_EMBEDDING_MODEL is configured.

    Incomplete proxy env never poisons the cache; callers fall through to the
    upstream ONNX embedding function so local-first operation still works.
    """
    global _PROXY_EF_CACHE
    model = _proxy_model_from_env()
    if not model:
        return None
    if _PROXY_EF_CACHE != "UNSET":
        return _PROXY_EF_CACHE

    endpoint = os.environ.get("MEMPAL_EMBEDDING_ENDPOINT", "").strip()
    key = os.environ.get("MEMPAL_EMBEDDING_KEY", "").strip()
    if not endpoint or not key:
        logger.warning(
            "MEMPAL_EMBEDDING_MODEL=%s but MEMPAL_EMBEDDING_ENDPOINT and/or "
            "MEMPAL_EMBEDDING_KEY is not set. Falling back to local ONNX embedding.",
            model,
        )
        return None

    dims_str = os.environ.get("MEMPAL_EMBEDDING_DIMS", "")
    dims = int(dims_str) if dims_str.strip().isdigit() else DEFAULT_EMBEDDING_DIMS
    _PROXY_EF_CACHE = ProxyEmbeddingFunction(
        api_key=key,
        model=model,
        dimensions=dims,
        endpoint=endpoint,
    )
    logger.info(
        "Proxy embedding configured: endpoint=%s, model=%s, dims=%d",
        endpoint,
        model,
        dims,
    )
    return _PROXY_EF_CACHE


def get_embedding_function(device: Optional[str] = None):
    """Return a cached embedding function.

    Policy:
    * If ``MEMPAL_EMBEDDING_MODEL`` is set (and not ``default``), use Sir's
      OpenAI-compatible proxy embedding function.
    * Otherwise use upstream's local ONNX MiniLM embedding function with the
      requested hardware provider.

    ``device=None`` reads from :class:`MempalaceConfig.embedding_device` for
    the local ONNX path. EF instances are cached per provider/proxy config.
    """
    proxy_ef = _get_proxy_embedding_function()
    if proxy_ef is not None:
        return proxy_ef

    if device is None:
        from .config import MempalaceConfig

        device = MempalaceConfig().embedding_device

    providers, effective = _resolve_providers(device)
    cache_key = tuple(providers)
    cached = _EF_CACHE.get(cache_key)
    if cached is not None:
        return cached

    ef_cls = _build_ef_class()
    ef = ef_cls(preferred_providers=providers)
    _EF_CACHE[cache_key] = ef
    logger.info("Embedding function initialized (device=%s providers=%s)", effective, providers)
    return ef


def describe_device(device: Optional[str] = None) -> str:
    """Return a short human-readable label for the resolved device.

    Used by the miner CLI header so users can see at a glance whether GPU
    acceleration actually engaged.
    """
    if _proxy_model_from_env():
        return "proxy"
    if device is None:
        from .config import MempalaceConfig

        device = MempalaceConfig().embedding_device
    _, effective = _resolve_providers(device)
    return effective


def reset_cache():
    """Reset embedding caches. Used in tests and reconnect/repair flows."""
    global _EF_CACHE, _PROXY_EF_CACHE
    _EF_CACHE = {}
    _PROXY_EF_CACHE = "UNSET"
