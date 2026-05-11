"""Tests for mempalace.embedding — local ONNX + proxy embedding support."""

import json
import logging
from unittest.mock import MagicMock

import pytest

import mempalace.embedding as embedding


@pytest.fixture(autouse=True)
def isolate_embedding_state(monkeypatch):
    monkeypatch.setattr(embedding, "_EF_CACHE", {})
    monkeypatch.setattr(embedding, "_PROXY_EF_CACHE", "UNSET")
    monkeypatch.setattr(embedding, "_WARNED", set())
    for key in (
        "MEMPAL_EMBEDDING_MODEL",
        "MEMPAL_EMBEDDING_ENDPOINT",
        "MEMPAL_EMBEDDING_KEY",
        "MEMPAL_EMBEDDING_DIMS",
    ):
        monkeypatch.delenv(key, raising=False)
    yield
    embedding.reset_cache()


class DummyEF:
    def __init__(self, preferred_providers=None):
        self.preferred_providers = preferred_providers or []


# --- upstream local ONNX provider selection ---


def test_auto_picks_cuda(monkeypatch):
    monkeypatch.setattr(
        "onnxruntime.get_available_providers",
        lambda: ["CUDAExecutionProvider", "CPUExecutionProvider"],
    )

    assert embedding._resolve_providers("auto") == (
        ["CUDAExecutionProvider", "CPUExecutionProvider"],
        "cuda",
    )


def test_auto_falls_to_cpu(monkeypatch):
    monkeypatch.setattr("onnxruntime.get_available_providers", lambda: ["CPUExecutionProvider"])

    assert embedding._resolve_providers("auto") == (["CPUExecutionProvider"], "cpu")


def test_cuda_missing_warns_with_gpu_extra(monkeypatch, caplog):
    monkeypatch.setattr("onnxruntime.get_available_providers", lambda: ["CPUExecutionProvider"])

    assert embedding._resolve_providers("cuda") == (["CPUExecutionProvider"], "cpu")
    assert "mempalace[gpu]" in caplog.text


def test_coreml_missing_warns_with_coreml_extra(monkeypatch, caplog):
    monkeypatch.setattr("onnxruntime.get_available_providers", lambda: ["CPUExecutionProvider"])

    assert embedding._resolve_providers("coreml") == (["CPUExecutionProvider"], "cpu")
    assert "mempalace[coreml]" in caplog.text


def test_dml_missing_warns_with_dml_extra(monkeypatch, caplog):
    monkeypatch.setattr("onnxruntime.get_available_providers", lambda: ["CPUExecutionProvider"])

    assert embedding._resolve_providers("dml") == (["CPUExecutionProvider"], "cpu")
    assert "mempalace[dml]" in caplog.text


def test_unknown_device_warns_once(monkeypatch, caplog):
    monkeypatch.setattr("onnxruntime.get_available_providers", lambda: ["CPUExecutionProvider"])

    assert embedding._resolve_providers("bogus") == (["CPUExecutionProvider"], "cpu")
    assert embedding._resolve_providers("bogus") == (["CPUExecutionProvider"], "cpu")
    assert caplog.text.count("Unknown embedding_device") == 1


def test_onnxruntime_import_error_falls_back_to_cpu(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "onnxruntime":
            raise ImportError("missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    assert embedding._resolve_providers("cuda") == (["CPUExecutionProvider"], "cpu")


def test_get_embedding_function_caches_by_resolved_provider_tuple(monkeypatch):
    monkeypatch.setattr(embedding, "_build_ef_class", lambda: DummyEF)
    monkeypatch.setattr(
        embedding,
        "_resolve_providers",
        lambda device: (["CPUExecutionProvider"], "cpu"),
    )

    first = embedding.get_embedding_function("cpu")
    second = embedding.get_embedding_function("auto")

    assert first is second
    assert first.preferred_providers == ["CPUExecutionProvider"]


def test_describe_device_uses_resolved_effective_device(monkeypatch):
    monkeypatch.setattr(
        embedding,
        "_resolve_providers",
        lambda device: (["CUDAExecutionProvider", "CPUExecutionProvider"], "cuda"),
    )

    assert embedding.describe_device("auto") == "cuda"


# --- fork proxy embedding path ---


def test_returns_proxy_when_configured(monkeypatch):
    monkeypatch.setenv("MEMPAL_EMBEDDING_MODEL", "gemini-embedding-2")
    monkeypatch.setenv("MEMPAL_EMBEDDING_ENDPOINT", "http://localhost:4000")
    monkeypatch.setenv("MEMPAL_EMBEDDING_KEY", "sk-test")
    result = embedding.get_embedding_function()
    assert isinstance(result, embedding.ProxyEmbeddingFunction)
    assert result._model == "gemini-embedding-2"
    assert result._dimensions == 3072
    assert result._url == "http://localhost:4000/v1/embeddings"


def test_custom_dimensions(monkeypatch):
    monkeypatch.setenv("MEMPAL_EMBEDDING_MODEL", "gemini-embedding-2")
    monkeypatch.setenv("MEMPAL_EMBEDDING_ENDPOINT", "http://localhost:4000")
    monkeypatch.setenv("MEMPAL_EMBEDDING_KEY", "sk-test")
    monkeypatch.setenv("MEMPAL_EMBEDDING_DIMS", "768")
    result = embedding.get_embedding_function()
    assert result._dimensions == 768


def test_missing_proxy_endpoint_or_key_falls_back_to_local(monkeypatch, caplog):
    monkeypatch.setenv("MEMPAL_EMBEDDING_MODEL", "gemini-embedding-2")
    monkeypatch.setenv("MEMPAL_EMBEDDING_KEY", "sk-test")
    monkeypatch.setattr(embedding, "_build_ef_class", lambda: DummyEF)
    monkeypatch.setattr(embedding, "_resolve_providers", lambda device: (["CPUExecutionProvider"], "cpu"))
    with caplog.at_level(logging.WARNING, logger="mempalace.embedding"):
        result = embedding.get_embedding_function("cpu")
    assert isinstance(result, DummyEF)
    assert "MEMPAL_EMBEDDING_ENDPOINT" in caplog.text


def test_proxy_cache_does_not_block_later_valid_config(monkeypatch):
    monkeypatch.setenv("MEMPAL_EMBEDDING_MODEL", "gemini-embedding-2")
    monkeypatch.setattr(embedding, "_build_ef_class", lambda: DummyEF)
    monkeypatch.setattr(embedding, "_resolve_providers", lambda device: (["CPUExecutionProvider"], "cpu"))
    assert isinstance(embedding.get_embedding_function("cpu"), DummyEF)

    monkeypatch.setenv("MEMPAL_EMBEDDING_ENDPOINT", "http://localhost:4000")
    monkeypatch.setenv("MEMPAL_EMBEDDING_KEY", "sk-test")
    result = embedding.get_embedding_function("cpu")
    assert isinstance(result, embedding.ProxyEmbeddingFunction)


def test_describe_device_reports_proxy(monkeypatch):
    monkeypatch.setenv("MEMPAL_EMBEDDING_MODEL", "gemini-embedding-2")
    assert embedding.describe_device("auto") == "proxy"


class TestBackwardCompatAlias:
    """The deprecated GeminiEmbeddingFunction name must still resolve."""

    def test_alias_is_proxy_class(self):
        assert embedding.GeminiEmbeddingFunction is embedding.ProxyEmbeddingFunction

    def test_alias_can_instantiate(self):
        ef = embedding.GeminiEmbeddingFunction(
            api_key="k", model="m", dimensions=4, endpoint="http://localhost:4000"
        )
        assert isinstance(ef, embedding.ProxyEmbeddingFunction)


class TestProxyEmbeddingFunction:
    def test_requires_endpoint(self):
        with pytest.raises(ValueError, match="MEMPAL_EMBEDDING_ENDPOINT"):
            embedding.ProxyEmbeddingFunction(api_key="k", model="m", dimensions=4)

    def test_embed_batch_openai_shape(self, monkeypatch):
        response = {
            "data": [
                {"embedding": [0.1, 0.2, 0.3, 0.4]},
                {"embedding": [0.5, 0.6, 0.7, 0.8]},
            ]
        }

        captured = {}

        def mock_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["headers"] = dict(req.header_items())
            captured["body"] = json.loads(req.data)
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(response).encode()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            return mock_resp

        monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)
        ef = embedding.ProxyEmbeddingFunction(
            api_key="k", model="m", dimensions=4, endpoint="http://localhost:4000"
        )
        result = ef._embed_batch(["hello", "world"])
        assert result == [[0.1, 0.2, 0.3, 0.4], [0.5, 0.6, 0.7, 0.8]]
        assert captured["url"] == "http://localhost:4000/v1/embeddings"
        assert captured["body"]["model"] == "m"
        assert captured["body"]["input"] == ["hello", "world"]
        assert captured["body"]["dimensions"] == 4
        auth_header = {k.lower(): v for k, v in captured["headers"].items()}
        assert auth_header["authorization"] == "Bearer k"

    def test_concurrent_call_returns_one_per_text(self, monkeypatch):
        def mock_urlopen(req, timeout=None):
            body = json.loads(req.data)
            n = len(body["input"])
            response = {"data": [{"embedding": [1.0]} for _ in range(n)]}
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(response).encode()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            return mock_resp

        monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)
        ef = embedding.ProxyEmbeddingFunction(
            api_key="k", model="m", dimensions=1, endpoint="http://localhost:4000"
        )
        texts = [f"text_{i}" for i in range(25)]
        result = ef(texts)
        assert len(result) == 25
        assert all(r == [1.0] for r in result)
