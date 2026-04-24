"""Tests for mempalace.embedding — pluggable embedding function support."""

import json
import logging
from unittest.mock import MagicMock

import pytest

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
        assert embedding.get_embedding_function() is None

    def test_returns_none_for_default(self, monkeypatch):
        monkeypatch.setenv("MEMPAL_EMBEDDING_MODEL", "default")
        assert embedding.get_embedding_function() is None

    def test_returns_none_for_empty(self, monkeypatch):
        monkeypatch.setenv("MEMPAL_EMBEDDING_MODEL", "")
        assert embedding.get_embedding_function() is None

    def test_returns_proxy_when_configured(self, monkeypatch):
        monkeypatch.setenv("MEMPAL_EMBEDDING_MODEL", "gemini-embedding-2")
        monkeypatch.setenv("MEMPAL_EMBEDDING_ENDPOINT", "http://localhost:4000")
        monkeypatch.setenv("MEMPAL_EMBEDDING_KEY", "sk-test")
        result = embedding.get_embedding_function()
        assert isinstance(result, embedding.ProxyEmbeddingFunction)
        assert result._model == "gemini-embedding-2"
        assert result._dimensions == 3072
        assert result._url == "http://localhost:4000/v1/embeddings"

    def test_custom_dimensions(self, monkeypatch):
        monkeypatch.setenv("MEMPAL_EMBEDDING_MODEL", "gemini-embedding-2")
        monkeypatch.setenv("MEMPAL_EMBEDDING_ENDPOINT", "http://localhost:4000")
        monkeypatch.setenv("MEMPAL_EMBEDDING_KEY", "sk-test")
        monkeypatch.setenv("MEMPAL_EMBEDDING_DIMS", "768")
        result = embedding.get_embedding_function()
        assert result._dimensions == 768

    def test_falls_back_when_endpoint_missing(self, monkeypatch, caplog):
        monkeypatch.setenv("MEMPAL_EMBEDDING_MODEL", "gemini-embedding-2")
        monkeypatch.delenv("MEMPAL_EMBEDDING_ENDPOINT", raising=False)
        monkeypatch.setenv("MEMPAL_EMBEDDING_KEY", "sk-test")
        with caplog.at_level(logging.WARNING, logger="mempalace.embedding"):
            assert embedding.get_embedding_function() is None
        assert any("MEMPAL_EMBEDDING_ENDPOINT" in r.message for r in caplog.records)

    def test_falls_back_when_key_missing(self, monkeypatch, caplog):
        monkeypatch.setenv("MEMPAL_EMBEDDING_MODEL", "gemini-embedding-2")
        monkeypatch.setenv("MEMPAL_EMBEDDING_ENDPOINT", "http://localhost:4000")
        monkeypatch.delenv("MEMPAL_EMBEDDING_KEY", raising=False)
        with caplog.at_level(logging.WARNING, logger="mempalace.embedding"):
            assert embedding.get_embedding_function() is None
        assert any("MEMPAL_EMBEDDING_KEY" in r.message for r in caplog.records)

    def test_caches_result(self, monkeypatch):
        monkeypatch.setenv("MEMPAL_EMBEDDING_MODEL", "gemini-embedding-2")
        monkeypatch.setenv("MEMPAL_EMBEDDING_ENDPOINT", "http://localhost:4000")
        monkeypatch.setenv("MEMPAL_EMBEDDING_KEY", "sk-test")
        a = embedding.get_embedding_function()
        b = embedding.get_embedding_function()
        assert a is b


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
        # Header keys are lowercased by urllib
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
