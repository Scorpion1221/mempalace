"""Focused tests for LLM-backed recall decision, rewrite, and rerank."""

import json

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


class TestDecideRecall:
    def test_decide_recall_uses_current_message_as_primary_signal_and_parses_json(
        self, monkeypatch
    ):
        captured = {}

        def fake_call_llm(config, prompt, max_tokens, timeout, **kwargs):
            captured["prompt"] = prompt
            captured["max_tokens"] = max_tokens
            captured["timeout"] = timeout
            captured["kwargs"] = kwargs
            return (
                "```json\n"
                '{"should_recall":true,"reason":"short_followup_depends_on_previous_assistant",'
                '"query":"dbeaver mysql test env","after":"2026-04-17"}\n```'
            )

        monkeypatch.setattr(recall_llm, "_call_llm", fake_call_llm)

        result = recall_llm.decide_recall(
            "How do I open the test MySQL DB in DBeaver?",
            config={"backend": "stub"},
            previous_assistant_context={
                "assistant_message_tail": "Earlier I mentioned the test env MySQL connection info."
            },
            active_context="/Users/scorpion/git/mempalace",
        )

        assert result == {
            "should_recall": True,
            "reason": "short_followup_depends_on_previous_assistant",
            "query": "dbeaver mysql test env",
            "after": "2026-04-17",
            "filters": {},
        }
        assert "Decide whether memory recall is needed for this turn" in captured["prompt"]
        assert "CURRENT USER MESSAGE — this is the primary signal." in captured["prompt"]
        assert "/Users/scorpion/git/mempalace" in captured["prompt"]
        assert "Earlier I mentioned the test env MySQL connection info." in captured["prompt"]
        assert captured["max_tokens"] == recall_llm.REWRITE_MAX_TOKENS
        assert captured["timeout"] == recall_llm.REWRITE_TIMEOUT_S

    def test_decide_recall_supports_false_decision(self, monkeypatch):
        monkeypatch.setattr(
            recall_llm,
            "_call_llm",
            lambda *_, **__: (
                '{"should_recall":false,'
                '"reason":"direct_local_task_no_memory_needed",'
                '"query":null,"after":null}'
            ),
        )

        result = recall_llm.decide_recall(
            "Format this JSON",
            config={"backend": "stub"},
            previous_assistant_context="We were discussing hooks.",
        )

        assert result == {
            "should_recall": False,
            "reason": "direct_local_task_no_memory_needed",
            "query": None,
            "after": None,
            "filters": {},
        }

    def test_decide_recall_falls_back_to_plain_query_string(self, monkeypatch):
        monkeypatch.setattr(
            recall_llm,
            "_call_llm",
            lambda *_, **__: '"mysql host dbeaver test env"',
        )

        result = recall_llm.decide_recall(
            "How do I open it?",
            config={"backend": "stub"},
            previous_assistant_context="We were just discussing the test env MySQL host.",
        )

        assert result == {
            "should_recall": True,
            "reason": "query_string_fallback",
            "query": "mysql host dbeaver test env",
            "after": None,
            "filters": {},
        }

    def test_rewrite_query_wrapper_returns_none_when_decision_is_false(self, monkeypatch):
        monkeypatch.setattr(
            recall_llm,
            "decide_recall",
            lambda *args, **kwargs: {
                "should_recall": False,
                "reason": "direct_local_task_no_memory_needed",
                "query": None,
                "after": None,
            },
        )

        assert recall_llm.rewrite_query("Format this JSON", config={"backend": "stub"}) is None


class TestLocalRecallDecision:
    def test_session_local_continue_is_skipped(self):
        result = recall_llm.local_recall_decision(
            "继续推进，直到完全修复完成",
            previous_assistant_context={"tail": "Earlier I outlined the task plan."},
        )
        assert result == {
            "should_recall": False,
            "reason": "session_local_continue_no_memory_needed",
            "query": None,
            "after": None,
        }

    def test_history_referencing_continue_is_not_skipped(self):
        result = recall_llm.local_recall_decision(
            "按之前那个方案继续推进",
            previous_assistant_context={"tail": "Earlier I outlined the task plan."},
        )
        assert result is None

    def test_english_continue_is_skipped(self):
        result = recall_llm.local_recall_decision(
            "keep going until it's fixed",
            previous_assistant_context={"tail": "Earlier I outlined the task plan."},
        )
        assert result == {
            "should_recall": False,
            "reason": "session_local_continue_no_memory_needed",
            "query": None,
            "after": None,
        }


class TestRerank:
    def test_rerank_uses_optional_previous_assistant_tail_and_preserves_order(self, monkeypatch):
        hits = [
            {"text": "Prod Postgres credentials", "wing": "infra", "room": "db"},
            {"text": "Test MySQL connection details for DBeaver", "wing": "infra", "room": "db"},
            {"text": "Weekly diary entry", "wing": "me", "room": "diary"},
        ]
        captured = {}

        def fake_call_llm(config, prompt, max_tokens, timeout, **kwargs):
            captured["prompt"] = prompt
            captured["max_tokens"] = max_tokens
            captured["timeout"] = timeout
            captured["kwargs"] = kwargs
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
        assert "CURRENT USER MESSAGE — what the user just asked" in captured["prompt"]
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
        monkeypatch.setattr(recall_llm, "_call_llm", lambda *_, **__: "999")

        result = recall_llm.rerank(
            "How do I open the test DB in DBeaver?",
            hits,
            top_k=1,
            config={"backend": "stub"},
        )

        assert result is None


class _FakeResponse:
    """Stand-in for the context manager returned by urllib.request.urlopen."""

    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._body


def _capture_request(monkeypatch, body: bytes):
    """Patch urlopen to capture the outgoing Request and return body."""
    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _FakeResponse(body)

    monkeypatch.setattr(recall_llm.urllib.request, "urlopen", fake_urlopen)
    return captured


class TestJsonModeOpenAICompat:
    def test_call_openai_compat_passes_response_format_when_json_mode(self, monkeypatch):
        body = json.dumps({"choices": [{"message": {"content": '{"foo":"bar"}'}}]}).encode("utf-8")
        captured = _capture_request(monkeypatch, body)

        result = recall_llm._call_openai_compat(
            "https://example.com/v1",
            "gemini-pro",
            "k",
            "prompt",
            max_tokens=100,
            timeout=5,
            json_mode=True,
        )

        assert result == '{"foo":"bar"}'
        assert captured["payload"]["response_format"] == {"type": "json_object"}

    def test_call_openai_compat_omits_response_format_when_not_json_mode(self, monkeypatch):
        body = json.dumps({"choices": [{"message": {"content": "anything"}}]}).encode("utf-8")
        captured = _capture_request(monkeypatch, body)

        recall_llm._call_openai_compat(
            "https://example.com/v1",
            "gemini-pro",
            "k",
            "prompt",
            max_tokens=100,
            timeout=5,
        )

        assert "response_format" not in captured["payload"]


class TestJsonModeAnthropicPrefill:
    def test_call_anthropic_uses_assistant_prefill_when_json_mode(self, monkeypatch):
        # API returns only what comes AFTER the prefill — no leading "{".
        body = json.dumps({"content": [{"text": '"foo":"bar"}'}]}).encode("utf-8")
        captured = _capture_request(monkeypatch, body)

        result = recall_llm._call_anthropic(
            "key",
            "claude-haiku-4-5",
            "prompt",
            max_tokens=100,
            timeout=5,
            json_mode=True,
        )

        messages = captured["payload"]["messages"]
        assert messages[0] == {"role": "user", "content": "prompt"}
        assert messages[1] == {"role": "assistant", "content": "{"}
        # The function should re-prepend "{" so the caller sees a complete object.
        assert result == '{"foo":"bar"}'

    def test_call_anthropic_omits_prefill_when_not_json_mode(self, monkeypatch):
        body = json.dumps({"content": [{"text": "free text"}]}).encode("utf-8")
        captured = _capture_request(monkeypatch, body)

        result = recall_llm._call_anthropic(
            "key",
            "claude-haiku-4-5",
            "prompt",
            max_tokens=100,
            timeout=5,
        )

        messages = captured["payload"]["messages"]
        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        assert result == "free text"


class TestJsonModeVertexPrefill:
    def test_call_vertex_uses_assistant_prefill_when_json_mode(self, monkeypatch):
        # API returns only what comes AFTER the prefill — no leading "{".
        body = json.dumps({"content": [{"text": '"foo":1}'}]}).encode("utf-8")
        captured = _capture_request(monkeypatch, body)

        result = recall_llm._call_vertex(
            "proj",
            "us-east5",
            "claude-haiku-4-5",
            "tok",
            "prompt",
            max_tokens=100,
            timeout=5,
            json_mode=True,
        )

        messages = captured["payload"]["messages"]
        assert messages[0] == {"role": "user", "content": "prompt"}
        assert messages[1] == {"role": "assistant", "content": "{"}
        assert result == '{"foo":1}'

    def test_call_vertex_omits_prefill_when_not_json_mode(self, monkeypatch):
        body = json.dumps({"content": [{"text": "ok"}]}).encode("utf-8")
        captured = _capture_request(monkeypatch, body)

        recall_llm._call_vertex(
            "proj",
            "us-east5",
            "claude-haiku-4-5",
            "tok",
            "prompt",
            max_tokens=100,
            timeout=5,
        )

        messages = captured["payload"]["messages"]
        assert len(messages) == 1


class TestDecideRecallPassesJsonMode:
    def test_decide_recall_passes_json_mode(self, monkeypatch):
        captured: dict = {}

        def fake_call_llm(config, prompt, max_tokens, timeout, **kwargs):
            captured["kwargs"] = kwargs
            return '{"should_recall":false,"reason":"x","query":null,"after":null}'

        monkeypatch.setattr(recall_llm, "_call_llm", fake_call_llm)

        recall_llm.decide_recall("hello", config={"backend": "stub"})

        assert captured["kwargs"].get("json_mode") is True


class TestCallLlmDispatchPassesJsonMode:
    def test_dispatch_to_openai_compat_forwards_json_mode(self, monkeypatch):
        captured: dict = {}

        def fake(*args, **kwargs):
            captured["kwargs"] = kwargs
            return "ok"

        monkeypatch.setattr(recall_llm, "_call_openai_compat", fake)

        recall_llm._call_llm(
            {"backend": "openai_compat", "endpoint": "x", "model": "m"},
            "p",
            10,
            5,
            json_mode=True,
        )

        assert captured["kwargs"].get("json_mode") is True

    def test_dispatch_to_anthropic_forwards_json_mode(self, monkeypatch):
        captured: dict = {}

        def fake(*args, **kwargs):
            captured["kwargs"] = kwargs
            return "ok"

        monkeypatch.setattr(recall_llm, "_call_anthropic", fake)

        recall_llm._call_llm(
            {"backend": "anthropic", "api_key": "k", "model": "m"},
            "p",
            10,
            5,
            json_mode=True,
        )

        assert captured["kwargs"].get("json_mode") is True

    def test_dispatch_to_vertex_forwards_json_mode(self, monkeypatch):
        captured: dict = {}

        def fake(*args, **kwargs):
            captured["kwargs"] = kwargs
            return "ok"

        monkeypatch.setattr(recall_llm, "_call_vertex", fake)

        recall_llm._call_llm(
            {
                "backend": "vertex",
                "project": "p",
                "location": "l",
                "model": "m",
                "token": "t",
            },
            "p",
            10,
            5,
            json_mode=True,
        )

        assert captured["kwargs"].get("json_mode") is True
