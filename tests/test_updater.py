"""Tests for mempalace.updater — self-update command."""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from mempalace import updater


def _make_fake_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "mempalace"
    repo.mkdir()
    (repo / "scripts").mkdir()
    (repo / "scripts" / "sync-plugins.sh").write_text("#!/bin/bash\necho ok\n")
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init", "--allow-empty"],
        cwd=str(repo),
        capture_output=True,
        env={
            **__import__("os").environ,
            "GIT_AUTHOR_NAME": "test",
            "GIT_COMMITTER_NAME": "test",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )
    return repo


def test_is_clean_on_clean_repo(tmp_path):
    repo = _make_fake_repo(tmp_path)
    assert updater._is_clean(repo) is True


def test_is_clean_on_dirty_repo(tmp_path):
    repo = _make_fake_repo(tmp_path)
    (repo / "dirty.txt").write_text("x")
    assert updater._is_clean(repo) is False


def test_update_refuses_dirty_tree(tmp_path):
    repo = _make_fake_repo(tmp_path)
    (repo / "dirty.txt").write_text("x")
    with pytest.raises(SystemExit):
        updater.update(repo=repo, pull=True)


def test_update_no_pull_skips_git(tmp_path):
    repo = _make_fake_repo(tmp_path)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch("subprocess.run", side_effect=fake_run):
        updater.update(repo=repo, pull=False)

    git_calls = [c for c in calls if c[0] == "git"]
    assert git_calls == [], "No git commands should run with --no-pull"
    bash_calls = [c for c in calls if "sync-plugins.sh" in str(c)]
    assert len(bash_calls) == 1


def test_check_does_not_modify(tmp_path, capsys):
    repo = _make_fake_repo(tmp_path)
    with patch.object(updater, "_find_repo", return_value=repo):
        with patch.object(updater, "_commits_behind_ahead", return_value=(0, 0)):
            updater.check(repo=repo)
    out = capsys.readouterr().out
    assert "Already up to date" in out
    assert updater._is_clean(repo) is True


def test_detect_agents_returns_dict():
    agents = updater._detect_agents()
    assert isinstance(agents, dict)
    assert set(agents.keys()) == {"claude", "codex", "hermes", "cursor"}
    for v in agents.values():
        assert isinstance(v, bool)
