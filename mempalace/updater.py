"""Self-update: git pull + sync-plugins.sh in one command."""

import os
import subprocess
import sys
from pathlib import Path


DEFAULT_REPO = Path.home() / "git" / "mempalace"
SYNC_SCRIPT = "scripts/sync-plugins.sh"

_AGENT_FLAGS = {
    "claude": "--claude",
    "codex": "--codex",
    "hermes": "--hermes",
    "cursor": "--cursor",
}

_AGENT_MARKERS = {
    "claude": ("~/.claude/plugins", Path.home() / ".claude" / "plugins"),
    "codex": ("~/.codex/config.toml", Path.home() / ".codex" / "config.toml"),
    "hermes": ("~/.hermes/hermes-agent", Path.home() / ".hermes" / "hermes-agent"),
    "cursor": ("~/.cursor", Path.home() / ".cursor"),
}


def _find_repo() -> Path:
    env = os.environ.get("MEMPAL_REPO", "")
    if env:
        p = Path(env).expanduser().resolve()
        if (p / SYNC_SCRIPT).is_file():
            return p
        print(f"MEMPAL_REPO={env} does not contain {SYNC_SCRIPT}", file=sys.stderr)
        sys.exit(1)
    if (DEFAULT_REPO / SYNC_SCRIPT).is_file():
        return DEFAULT_REPO
    print(
        f"Cannot find mempalace repo at {DEFAULT_REPO}.\n"
        f"Set MEMPAL_REPO=/path/to/mempalace or cd into the repo.",
        file=sys.stderr,
    )
    sys.exit(1)


def _detect_agents() -> dict:
    found = {}
    for name, (label, path) in _AGENT_MARKERS.items():
        found[name] = path.exists()
    return found


def _git(*cmd, repo: Path, capture=False):
    full = ["git", "-C", str(repo)] + list(cmd)
    if capture:
        r = subprocess.run(full, capture_output=True, text=True)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    return subprocess.run(full).returncode


def _is_clean(repo: Path) -> bool:
    rc, out, _ = _git("status", "--porcelain", repo=repo, capture=True)
    return rc == 0 and out == ""


def _current_branch(repo: Path) -> str:
    _, out, _ = _git("symbolic-ref", "--short", "HEAD", repo=repo, capture=True)
    return out


def _commits_behind_ahead(repo: Path) -> tuple:
    _git("fetch", "--quiet", "origin", repo=repo)
    _, out, _ = _git("rev-list", "--left-right", "--count", "HEAD...@{u}", repo=repo, capture=True)
    if out:
        parts = out.split()
        if len(parts) == 2:
            return int(parts[1]), int(parts[0])
    return 0, 0


def check(repo: Path | None = None):
    repo = repo or _find_repo()
    branch = _current_branch(repo)
    clean = _is_clean(repo)
    behind, ahead = _commits_behind_ahead(repo)
    agents = _detect_agents()

    from .version import __version__

    print(f"MemPalace {__version__}  repo: {repo}")
    print(f"Branch: {branch}  {'clean' if clean else 'DIRTY (uncommitted changes)'}")
    print(f"Commits: {behind} behind, {ahead} ahead of origin/{branch}")
    print()

    will_sync = [n for n, ok in agents.items() if ok]
    will_skip = [n for n, ok in agents.items() if not ok]
    if will_sync:
        markers = {n: _AGENT_MARKERS[n][0] for n in will_sync}
        print("Will sync: " + ", ".join(f"{n} ({markers[n]})" for n in will_sync))
    if will_skip:
        markers = {n: _AGENT_MARKERS[n][0] for n in will_skip}
        print("Will skip: " + ", ".join(f"{n} (no {markers[n]})" for n in will_skip))

    if behind == 0:
        print("\nAlready up to date.")
    elif not clean:
        print("\nWorking tree is dirty — commit or stash before running `mempalace update`.")
    else:
        print(f"\nRun `mempalace update` to pull {behind} commit(s) and sync plugins.")


def update(
    *,
    repo: Path | None = None,
    agents: list[str] | None = None,
    tag: str | None = None,
    pull: bool = True,
):
    repo = repo or _find_repo()
    sync_script = repo / SYNC_SCRIPT

    if pull:
        if not _is_clean(repo):
            print(
                "Aborting: working tree has uncommitted changes.\n"
                "Commit or stash them first, or use --no-pull to skip git pull.",
                file=sys.stderr,
            )
            sys.exit(1)

        if tag:
            print(f"Fetching and checking out tag {tag}...")
            rc = _git("fetch", "--tags", "origin", repo=repo)
            if rc != 0:
                sys.exit(rc)
            rc = _git("checkout", tag, repo=repo)
            if rc != 0:
                print(f"Tag {tag} not found.", file=sys.stderr)
                sys.exit(1)
        else:
            branch = _current_branch(repo)
            print(f"Pulling origin/{branch}...")
            rc = _git("pull", "--rebase", "origin", branch, repo=repo)
            if rc != 0:
                print(
                    "git pull failed. Resolve conflicts manually, then re-run.",
                    file=sys.stderr,
                )
                sys.exit(rc)
    else:
        print("Skipping git pull (--no-pull).")

    cmd = ["bash", str(sync_script)]
    if agents:
        for a in agents:
            flag = _AGENT_FLAGS.get(a)
            if flag:
                cmd.append(flag)
    print(f"Running: {' '.join(cmd)}")
    rc = subprocess.run(cmd, cwd=str(repo)).returncode
    if rc != 0:
        print(f"sync-plugins.sh exited with code {rc}", file=sys.stderr)
        sys.exit(rc)

    from .version import __version__

    print(f"\nMemPalace {__version__} — update complete.")
