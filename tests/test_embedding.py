"""Tests for mempalace.embedding — pluggable embedding function support."""

import json
from unittest.mock import patch, MagicMock

from mempalace import embedding


class TestGetEmbeddingFunction:
    def setup_method(self):
        embedding.reset_cache()

    def teardown_method(self):
        embedding.reset_cache()
        from mempalace.palace import _reset_embedding_cache
        _reset_embedding_cache()

    def test_returns_none_when_unset(self, monkeypatch):
        monkeypatch.delenv("MEMPAL_EMBEDDING_MODEL", raising=False)
        monkeypatch.delenv("MEMPALACE_EMBEDDING_MODEL", raising=False)
        assert embedding.get_embedding_function() is None

    def test_returns_none_for_default(self, monkeypatch):
        monkeypatch.setenv("MEMPAL_EMBEDDING_MODEL", "default")
        assert embedding.get_embedding_function() is None

    def test_returns_none_for_empty(self, monkeypatch):
        monkeypatch.setenv("MEMPAL_EMBEDDING_MODEL", "")
        assert embedding.get_embedding_function() is None

    def test_returns_gemini_when_configured(self, monkeypatch):
        monkeypatch.setenv("MEMPAL_EMBEDDING_MODEL", "gemini-embedding-2")
        monkeypatch.setenv("GEMINI_API_KEY", "test-key-123")
        result = embedding.get_embedding_function()
        assert isinstance(result, embedding.GeminiEmbeddingFunction)
        assert result._model == "gemini-embedding-2"
        assert result._dimensions == 3072

    def test_custom_dimensions(self, monkeypatch):
        monkeypatch.setenv("MEMPAL_EMBEDDING_MODEL", "gemini-embedding-2")
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setenv("MEMPAL_EMBEDDING_DIMS", "768")
        result = embedding.get_embedding_function()
        assert result._dimensions == 768

    def test_falls_back_when_no_api_key_and_no_endpoint(self, monkeypatch):
        monkeypatch.setenv("MEMPAL_EMBEDDING_MODEL", "gemini-embedding-2")
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("MEMPAL_EMBEDDING_ENDPOINT", raising=False)
        monkeypatch.delenv("MEMPALACE_EMBEDDING_ENDPOINT", raising=False)
        assert embedding.get_embedding_function() is None

    def test_unknown_model_falls_back(self, monkeypatch):
        monkeypatch.setenv("MEMPAL_EMBEDDING_MODEL", "unknown-model")
        assert embedding.get_embedding_function() is None

    def test_caches_result(self, monkeypatch):
        monkeypatch.setenv("MEMPAL_EMBEDDING_MODEL", "gemini-embedding-2")
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        a = embedding.get_embedding_function()
        b = embedding.get_embedding_function()
        assert a is b

    def test_mempalace_env_var_also_works(self, monkeypatch):
        monkeypatch.delenv("MEMPAL_EMBEDDING_MODEL", raising=False)
        monkeypatch.setenv("MEMPALACE_EMBEDDING_MODEL", "gemini-embedding-2")
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        result = embedding.get_embedding_function()
        assert isinstance(result, embedding.GeminiEmbeddingFunction)


class TestGeminiEmbeddingFunction:
    def test_embed_batch_single(self, monkeypatch):
        response = {
            "embeddings": [
                {"values": [0.1, 0.2, 0.3, 0.4]},
                {"values": [0.5, 0.6, 0.7, 0.8]},
            ]
        }

        def mock_urlopen(req, timeout=None):
            body = json.loads(req.data)
            assert len(body["requests"]) == 2
            assert body["requests"][0]["outputDimensionality"] == 4
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(response).encode()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            return mock_resp

        monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)
        ef = embedding.GeminiEmbeddingFunction(api_key="k", model="m", dimensions=4)
        result = ef._embed_batch(["hello", "world"])
        assert result == [[0.1, 0.2, 0.3, 0.4], [0.5, 0.6, 0.7, 0.8]]

    def test_batching_splits_large_input(self, monkeypatch):
        call_count = {"n": 0}

        def mock_urlopen(req, timeout=None):
            call_count["n"] += 1
            body = json.loads(req.data)
            n = len(body["requests"])
            response = {"embeddings": [{"values": [1.0]} for _ in range(n)]}
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(response).encode()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            return mock_resp

        monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)
        ef = embedding.GeminiEmbeddingFunction(api_key="k", model="m", dimensions=1)
        texts = [f"text_{i}" for i in range(250)]
        result = ef(texts)
        assert len(result) == 250
        assert call_count["n"] == 3  # 100 + 100 + 50
