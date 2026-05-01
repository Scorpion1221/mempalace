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
    # `mempalace update` now delegates to install.sh (which internally calls
    # sync-plugins.sh). Keep both present so _find_repo() accepts the fake repo.
    (repo / "install.sh").write_text("#!/bin/bash\necho install ok\n")
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
    # `mempalace update` now invokes install.sh directly (install.sh itself
    # delegates to sync-plugins.sh). Assert on install.sh, not sync-plugins.sh.
    install_calls = [c for c in calls if "install.sh" in str(c)]
    assert len(install_calls) == 1
    assert "--singleton" in install_calls[0]


def test_check_does_not_modify(tmp_path, capsys):
    repo = _make_fake_repo(tmp_path)
    with patch.object(updater, "_find_repo", return_value=repo):
        with patch.object(updater, "_commits_behind_ahead", return_value=(0, 0)):
            with patch.object(updater, "_resolved_cli_path", return_value="/tmp/fake-mempalace"):
                with patch.object(updater, "_runtime_package_version", return_value="3.3.311"):
                    updater.check(repo=repo)
    out = capsys.readouterr().out
    assert "Already up to date" in out
    assert "CLI on PATH:" in out
    assert "/tmp/fake-mempalace" in out
    assert "Runtime version: 3.3.311" in out
    assert updater._is_clean(repo) is True


def test_detect_agents_returns_dict():
    agents = updater._detect_agents()
    assert isinstance(agents, dict)
    assert set(agents.keys()) == {"claude", "codex", "hermes", "cursor"}
    for v in agents.values():
        assert isinstance(v, bool)


def test_runtime_package_version_none_when_missing():
    missing = Path("/tmp/definitely-not-a-real-mempal-runtime-python")
    assert updater._runtime_package_version(missing) is None


def test_check_flags_drift_when_cli_resolves_elsewhere(tmp_path, capsys):
    repo = _make_fake_repo(tmp_path)
    # Point the runtime CLI at a fake path, and simulate a stale shim from a
    # different venv on PATH (NOT a symlink into the runtime).
    fake_runtime = tmp_path / "runtime" / "bin" / "mempalace"
    fake_runtime.parent.mkdir(parents=True)
    fake_runtime.write_text("#!/bin/sh\necho runtime\n")
    fake_runtime.chmod(0o755)

    stale_cli = tmp_path / "stale" / "venv" / "bin" / "mempalace"
    stale_cli.parent.mkdir(parents=True)
    stale_cli.write_text("#!/bin/sh\necho stale\n")
    stale_cli.chmod(0o755)

    with patch.object(updater, "_find_repo", return_value=repo), \
         patch.object(updater, "_commits_behind_ahead", return_value=(0, 0)), \
         patch.object(updater, "_resolved_cli_path", return_value=str(stale_cli)), \
         patch.object(updater, "_runtime_package_version", return_value="3.3.311"), \
         patch.object(updater, "DEFAULT_RUNTIME_DIR", tmp_path / "runtime"):
        updater.check(repo=repo)
    out = capsys.readouterr().out
    assert "drift: resolves to" in out
    assert str(stale_cli) in out


def test_check_treats_system_bin_symlink_as_non_drift(tmp_path, capsys):
    repo = _make_fake_repo(tmp_path)
    # /opt/homebrew/bin/mempalace -> ~/.mempalace/venv/bin/mempalace is the
    # blessed install shape; check() must NOT flag it as drift.
    runtime_cli = tmp_path / "runtime" / "bin" / "mempalace"
    runtime_cli.parent.mkdir(parents=True)
    runtime_cli.write_text("#!/bin/sh\necho runtime\n")
    runtime_cli.chmod(0o755)

    system_bin = tmp_path / "sysbin" / "mempalace"
    system_bin.parent.mkdir(parents=True)
    system_bin.symlink_to(runtime_cli)

    with patch.object(updater, "_find_repo", return_value=repo), \
         patch.object(updater, "_commits_behind_ahead", return_value=(0, 0)), \
         patch.object(updater, "_resolved_cli_path", return_value=str(system_bin)), \
         patch.object(updater, "_runtime_package_version", return_value="3.3.311"), \
         patch.object(updater, "DEFAULT_RUNTIME_DIR", tmp_path / "runtime"):
        updater.check(repo=repo)
    out = capsys.readouterr().out
    assert "drift" not in out
    assert f"CLI on PATH:    {system_bin}" in out
