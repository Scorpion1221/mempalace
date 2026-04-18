"""Focused tests for LLM-backed recall prompt construction and parsing."""

from mempalace import recall_llm


class TestPreviousAssistantContext:
    def test_extracts_nested_previous_assistant_tail(self):
        context = {
            "previous_assistant_message": {
                "tail": "  Use the read-only DBeaver connection for the test env.  "
            }
        }

        assert (
            recall_llm._extract_previous_assistant_tail(context)
            == "Use the read-only DBeaver connection for the test env."
        )


class TestRewriteQuery:
    def test_rewrite_query_uses_current_message_as_primary_signal_and_parses_json(
        self, monkeypatch
    ):
        captured = {}

        def fake_call_llm(config, prompt, max_tokens, timeout):
            captured["prompt"] = prompt
            captured["max_tokens"] = max_tokens
            captured["timeout"] = timeout
            return '```json\n{"query":"dbeaver mysql test env","after":"2026-04-17"}\n```'

        monkeypatch.setattr(recall_llm, "_call_llm", fake_call_llm)

        result = recall_llm.rewrite_query(
            "How do I open the test MySQL DB in DBeaver?",
            config={"backend": "stub"},
            previous_assistant_context={
                "assistant_message_tail": "Earlier I mentioned the test env MySQL connection info."
            },
        )

        assert result == {"query": "dbeaver mysql test env", "after": "2026-04-17"}
        assert "CURRENT USER MESSAGE — this is the primary signal" in captured["prompt"]
        assert "optional context only" in captured["prompt"]
        assert "How do I open the test MySQL DB in DBeaver?" in captured["prompt"]
        assert "Earlier I mentioned the test env MySQL connection info." in captured["prompt"]
        assert captured["max_tokens"] == recall_llm.REWRITE_MAX_TOKENS
        assert captured["timeout"] == recall_llm.REWRITE_TIMEOUT_S

    def test_rewrite_query_falls_back_to_plain_query_string(self, monkeypatch):
        monkeypatch.setattr(
            recall_llm,
            "_call_llm",
            lambda *_: '"mysql host dbeaver test env"',
        )

        result = recall_llm.rewrite_query(
            "How do I open it?",
            config={"backend": "stub"},
            previous_assistant_context="We were just discussing the test env MySQL host.",
        )

        assert result == {"query": "mysql host dbeaver test env", "after": None}


class TestRerank:
    def test_rerank_uses_optional_previous_assistant_tail_and_preserves_order(self, monkeypatch):
        hits = [
            {"text": "Prod Postgres credentials", "wing": "infra", "room": "db"},
            {"text": "Test MySQL connection details for DBeaver", "wing": "infra", "room": "db"},
            {"text": "Weekly diary entry", "wing": "me", "room": "diary"},
        ]
        captured = {}

        def fake_call_llm(config, prompt, max_tokens, timeout):
            captured["prompt"] = prompt
            captured["max_tokens"] = max_tokens
            captured["timeout"] = timeout
            return "2,1,2,999"

        monkeypatch.setattr(recall_llm, "_call_llm", fake_call_llm)

        reranked = recall_llm.rerank(
            "How do I open the test DB in DBeaver?",
            hits,
            top_k=2,
            config={"backend": "stub"},
            previous_assistant_context={
                "previous_assistant_message": {
                    "tail": "I previously pointed you to the test MySQL DB connection."
                }
            },
        )

        assert reranked == [hits[1], hits[0]]
        assert "CURRENT USER MESSAGE — this is the primary signal" in captured["prompt"]
        assert "I previously pointed you to the test MySQL DB connection." in captured["prompt"]
        assert "1. [infra/db] Prod Postgres credentials" in captured["prompt"]
        assert "2. [infra/db] Test MySQL connection details for DBeaver" in captured["prompt"]
        assert captured["max_tokens"] == recall_llm.RERANK_MAX_TOKENS
        assert captured["timeout"] == recall_llm.RERANK_TIMEOUT_S

    def test_rerank_returns_none_when_llm_response_has_only_invalid_indices(self, monkeypatch):
        hits = [
            {"text": "Test MySQL connection details for DBeaver", "wing": "infra", "room": "db"},
            {"text": "Weekly diary entry", "wing": "me", "room": "diary"},
        ]
        monkeypatch.setattr(recall_llm, "_call_llm", lambda *_: "999")

        result = recall_llm.rerank(
            "How do I open the test DB in DBeaver?",
            hits,
            top_k=1,
            config={"backend": "stub"},
        )

        assert result is None
