import pytest

import mempalace.embedding as embedding


@pytest.fixture(autouse=True)
def isolate_embedding_state(monkeypatch):
    monkeypatch.setattr(embedding, "_EF_CACHE", {})
    monkeypatch.setattr(embedding, "_PROXY_EF_CACHE", "UNSET")
    monkeypatch.setattr(embedding, "_WARNED", set())
    for name in (
        "MEMPAL_EMBEDDING_MODEL",
        "MEMPAL_EMBEDDING_ENDPOINT",
        "MEMPAL_EMBEDDING_KEY",
        "MEMPAL_EMBEDDING_DIMS",
    ):
        monkeypatch.delenv(name, raising=False)


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
    class DummyEF:
        def __init__(self, preferred_providers):
            self.preferred_providers = preferred_providers

    monkeypatch.setattr(embedding, "_build_ef_class", lambda: DummyEF)
    monkeypatch.setattr(
        embedding, "_resolve_providers", lambda device: (["CPUExecutionProvider"], "cpu")
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


def test_proxy_embedding_takes_precedence_when_model_is_set(monkeypatch):
    monkeypatch.setenv("MEMPAL_EMBEDDING_MODEL", "gemini-embedding-2")
    monkeypatch.setenv("MEMPAL_EMBEDDING_ENDPOINT", "http://localhost:4000")
    monkeypatch.setenv("MEMPAL_EMBEDDING_KEY", "sk-test")

    result = embedding.get_embedding_function("cpu")

    assert isinstance(result, embedding.ProxyEmbeddingFunction)
    assert result._model == "gemini-embedding-2"
    assert result._dimensions == embedding.DEFAULT_EMBEDDING_DIMS
    assert result._url == "http://localhost:4000/v1/embeddings"


def test_proxy_embedding_custom_dimensions(monkeypatch):
    monkeypatch.setenv("MEMPAL_EMBEDDING_MODEL", "gemini-embedding-2")
    monkeypatch.setenv("MEMPAL_EMBEDDING_ENDPOINT", "http://localhost:4000")
    monkeypatch.setenv("MEMPAL_EMBEDDING_KEY", "sk-test")
    monkeypatch.setenv("MEMPAL_EMBEDDING_DIMS", "768")

    result = embedding.get_embedding_function("cpu")

    assert isinstance(result, embedding.ProxyEmbeddingFunction)
    assert result._dimensions == 768


def test_proxy_incomplete_env_falls_back_to_onnx(monkeypatch, caplog):
    class DummyEF:
        def __init__(self, preferred_providers):
            self.preferred_providers = preferred_providers

    monkeypatch.setenv("MEMPAL_EMBEDDING_MODEL", "gemini-embedding-2")
    monkeypatch.setattr(embedding, "_build_ef_class", lambda: DummyEF)
    monkeypatch.setattr(
        embedding,
        "_resolve_providers",
        lambda device: (["CPUExecutionProvider"], "cpu"),
    )

    result = embedding.get_embedding_function("cpu")

    assert isinstance(result, DummyEF)
    assert "MEMPAL_EMBEDDING_ENDPOINT" in caplog.text


def test_proxy_cache_does_not_store_missing_env(monkeypatch):
    class DummyEF:
        def __init__(self, preferred_providers):
            self.preferred_providers = preferred_providers

    monkeypatch.setenv("MEMPAL_EMBEDDING_MODEL", "gemini-embedding-2")
    monkeypatch.setattr(embedding, "_build_ef_class", lambda: DummyEF)
    monkeypatch.setattr(
        embedding,
        "_resolve_providers",
        lambda device: (["CPUExecutionProvider"], "cpu"),
    )

    assert isinstance(embedding.get_embedding_function("cpu"), DummyEF)
    monkeypatch.setenv("MEMPAL_EMBEDDING_ENDPOINT", "http://localhost:4000")
    monkeypatch.setenv("MEMPAL_EMBEDDING_KEY", "sk-test")
    embedding.reset_cache()
    assert isinstance(embedding.get_embedding_function("cpu"), embedding.ProxyEmbeddingFunction)


def test_proxy_embed_batch_openai_shape(monkeypatch):
    import json
    from unittest.mock import MagicMock

    response = {"data": [{"embedding": [0.1, 0.2]}, {"embedding": [0.3, 0.4]}]}
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
        api_key="k", model="m", dimensions=2, endpoint="http://localhost:4000"
    )

    assert ef._embed_batch(["hello", "world"]) == [[0.1, 0.2], [0.3, 0.4]]
    assert captured["url"] == "http://localhost:4000/v1/embeddings"
    assert captured["body"] == {"model": "m", "input": ["hello", "world"], "dimensions": 2}
    headers = {k.lower(): v for k, v in captured["headers"].items()}
    assert headers["authorization"] == "Bearer k"
