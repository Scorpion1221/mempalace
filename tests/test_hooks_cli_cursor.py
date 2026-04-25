"""Tests for the Cursor harness adapter in mempalace.hooks_cli.

Cursor IDE has different hook input/output shapes from Claude Code and Codex:
- Input: ``workspace_roots`` (list) instead of ``cwd`` (string);
  ``conversation_id`` instead of ``session_id``.
- Output: top-level ``{"additional_context": "..."}`` instead of the nested
  ``{"hookSpecificOutput": {"additionalContext": "..."}}`` shape.

These tests verify the adapter handles both differences without breaking
existing Claude Code / Codex behaviour.
"""

import contextlib
import io
import json
from unittest.mock import patch

import pytest

from mempalace.hooks_cli import (
    SUPPORTED_HARNESSES,
    _output_additional_context,
    _parse_harness_input,
    hook_session_start,
)


# --- _parse_harness_input ---


def test_cursor_in_supported_harnesses():
    assert "cursor" in SUPPORTED_HARNESSES


def test_parse_cursor_workspace_roots_becomes_cwd():
    parsed = _parse_harness_input(
        {"workspace_roots": ["/Users/scorpion/git/mempalace"], "conversation_id": "abc"},
        harness="cursor",
    )
    assert parsed["cwd"] == "/Users/scorpion/git/mempalace"
    assert parsed["session_id"] == "abc"
    assert parsed["harness"] == "cursor"


def test_parse_cursor_empty_workspace_roots():
    parsed = _parse_harness_input({"workspace_roots": []}, harness="cursor")
    assert parsed["cwd"] == ""


def test_parse_cursor_missing_workspace_roots():
    parsed = _parse_harness_input({}, harness="cursor")
    assert parsed["cwd"] == ""


def test_parse_cursor_cwd_takes_precedence_over_workspace_roots():
    # If both are present (defensive — Cursor docs only list workspace_roots),
    # the explicit ``cwd`` wins so other harnesses' field semantics stay
    # predictable.
    parsed = _parse_harness_input(
        {"cwd": "/explicit", "workspace_roots": ["/roots"]},
        harness="cursor",
    )
    assert parsed["cwd"] == "/explicit"


def test_parse_claude_code_unchanged():
    parsed = _parse_harness_input(
        {"cwd": "/proj", "session_id": "s1", "transcript_path": "/t"},
        harness="claude-code",
    )
    assert parsed["cwd"] == "/proj"
    assert parsed["session_id"] == "s1"
    assert parsed["transcript_path"] == "/t"
    assert parsed["harness"] == "claude-code"


def test_parse_rejects_unknown_harness():
    with pytest.raises(SystemExit):
        _parse_harness_input({}, harness="garbage-harness")


# --- _output_additional_context ---


def _capture_output():
    """Patch _output and return (context_manager, buf)."""
    buf = io.StringIO()
    return (
        patch("mempalace.hooks_cli._output", side_effect=lambda d: buf.write(json.dumps(d))),
        buf,
    )


def test_output_additional_context_cursor_sessionstart_top_level():
    cm, buf = _capture_output()
    with cm:
        _output_additional_context("hello world", harness="cursor", event="SessionStart")
    result = json.loads(buf.getvalue())
    assert result == {"additional_context": "hello world"}
    # Cursor must NOT see the claude-style nested shape.
    assert "hookSpecificOutput" not in result
    assert "continue" not in result


def test_output_additional_context_cursor_userpromptsubmit_uses_user_message():
    # beforeSubmitPrompt in Cursor only injects ``user_message`` into the
    # prompt context (not ``additional_context``). Verified against the
    # plastic-labs cursor-honcho plugin's output helper — the docs are
    # incomplete on this point.
    cm, buf = _capture_output()
    with cm:
        _output_additional_context("recall payload", harness="cursor", event="UserPromptSubmit")
    result = json.loads(buf.getvalue())
    assert result == {"continue": True, "user_message": "recall payload"}
    assert "additional_context" not in result
    assert "hookSpecificOutput" not in result


def test_output_additional_context_claude_code_nested():
    cm, buf = _capture_output()
    with cm:
        _output_additional_context("hello", harness="claude-code", event="UserPromptSubmit")
    result = json.loads(buf.getvalue())
    assert result["continue"] is True
    assert result["suppressOutput"] is True
    assert result["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert result["hookSpecificOutput"]["additionalContext"] == "hello"


def test_output_additional_context_codex_matches_claude_code():
    cm1, buf1 = _capture_output()
    cm2, buf2 = _capture_output()
    with cm1:
        _output_additional_context("x", harness="claude-code", event="UserPromptSubmit")
    with cm2:
        _output_additional_context("x", harness="codex", event="UserPromptSubmit")
    assert json.loads(buf1.getvalue()) == json.loads(buf2.getvalue())


# --- hook_session_start cursor branch ---


def test_session_start_cursor_emits_palace_summary_when_available(tmp_path):
    buf = io.StringIO()
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch("mempalace.hooks_cli._output", side_effect=lambda d: buf.write(json.dumps(d)))
        )
        stack.enter_context(patch("mempalace.hooks_cli.STATE_DIR", tmp_path))
        stack.enter_context(
            patch("mempalace.hooks_cli._build_palace_context", return_value="Wings: mempalace (bugs, decisions)")
        )
        hook_session_start(
            {"workspace_roots": [str(tmp_path)], "conversation_id": "c1"},
            harness="cursor",
        )
    result = json.loads(buf.getvalue())
    assert "additional_context" in result
    assert "<mempalace-recall>" in result["additional_context"]
    assert "Wings: mempalace" in result["additional_context"]
    # Cursor-specific: NOT nested under hookSpecificOutput.
    assert "hookSpecificOutput" not in result


def test_session_start_cursor_empty_palace_emits_empty_context(tmp_path):
    buf = io.StringIO()
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch("mempalace.hooks_cli._output", side_effect=lambda d: buf.write(json.dumps(d)))
        )
        stack.enter_context(patch("mempalace.hooks_cli.STATE_DIR", tmp_path))
        stack.enter_context(
            patch("mempalace.hooks_cli._build_palace_context", return_value="")
        )
        hook_session_start(
            {"workspace_roots": [str(tmp_path)], "conversation_id": "c2"},
            harness="cursor",
        )
    result = json.loads(buf.getvalue())
    # Even on empty palace we keep the Cursor-shaped key — emitting {} would
    # be a legacy claude-code pass-through shape that Cursor wouldn't honour.
    assert result == {"additional_context": ""}


def test_session_start_claude_code_still_passthrough(tmp_path):
    buf = io.StringIO()
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch("mempalace.hooks_cli._output", side_effect=lambda d: buf.write(json.dumps(d)))
        )
        stack.enter_context(patch("mempalace.hooks_cli.STATE_DIR", tmp_path))
        # Should NOT be called for claude-code — regression check.
        stack.enter_context(
            patch("mempalace.hooks_cli._build_palace_context", side_effect=AssertionError("should not be called"))
        )
        hook_session_start(
            {"cwd": str(tmp_path), "session_id": "s1"},
            harness="claude-code",
        )
    assert json.loads(buf.getvalue()) == {}


def test_session_start_codex_still_passthrough(tmp_path):
    buf = io.StringIO()
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch("mempalace.hooks_cli._output", side_effect=lambda d: buf.write(json.dumps(d)))
        )
        stack.enter_context(patch("mempalace.hooks_cli.STATE_DIR", tmp_path))
        stack.enter_context(
            patch("mempalace.hooks_cli._build_palace_context", side_effect=AssertionError("should not be called"))
        )
        hook_session_start({"cwd": str(tmp_path)}, harness="codex")
    assert json.loads(buf.getvalue()) == {}
