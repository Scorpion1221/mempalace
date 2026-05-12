"""Self-update: git pull + stable reinstall + sync in one command."""

import os
import shutil
import subprocess
import sys
from pathlib import Path


DEFAULT_REPO = Path.home() / "git" / "mempalace"
SYNC_SCRIPT = "scripts/sync-plugins.sh"
INSTALL_SCRIPT = "install.sh"
DEFAULT_RUNTIME_DIR = Path.home() / ".mempalace" / "venv"
RUNTIME_MANIFEST = Path.home() / ".mempalace" / "runtime.json"

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
        if (p / SYNC_SCRIPT).is_file() and (p / INSTALL_SCRIPT).is_file():
            return p
        print(
            f"MEMPAL_REPO={env} does not contain {SYNC_SCRIPT} and {INSTALL_SCRIPT}",
            file=sys.stderr,
        )
        sys.exit(1)
    if (DEFAULT_REPO / SYNC_SCRIPT).is_file() and (DEFAULT_REPO / INSTALL_SCRIPT).is_file():
        return DEFAULT_REPO
    print(
        f"Cannot find mempalace repo at {DEFAULT_REPO}.\n"
        f"Set MEMPAL_REPO=/path/to/mempalace or cd into the repo.",
        file=sys.stderr,
    )
    sys.exit(1)


def _detect_agents() -> dict:
    found = {}
    for name, (_label, path) in _AGENT_MARKERS.items():
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


def _runtime_python() -> Path:
    env = os.environ.get("MEMPAL_RUNTIME_PYTHON", "")
    if env:
        return Path(env).expanduser().resolve()
    return (DEFAULT_RUNTIME_DIR / "bin" / "python3").resolve()


def _runtime_package_version(runtime_python: Path) -> str | None:
    if not runtime_python.exists():
        return None
    try:
        result = subprocess.run(
            [str(runtime_python), "-c", "import mempalace; print(mempalace.__version__)"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    version = result.stdout.strip()
    return version or None


def _resolved_cli_path() -> str:
    cli = shutil.which("mempalace")
    return cli or "NOT ON PATH"


def check(repo: Path | None = None):
    repo = repo or _find_repo()
    branch = _current_branch(repo)
    clean = _is_clean(repo)
    behind, ahead = _commits_behind_ahead(repo)
    agents = _detect_agents()
    runtime_python = _runtime_python()
    runtime_version = _runtime_package_version(runtime_python)

    from .version import __version__

    print(f"MemPalace {__version__}  repo: {repo}")
    print(f"Branch: {branch}  {'clean' if clean else 'DIRTY (uncommitted changes)'}")
    print(f"Commits: {behind} behind, {ahead} ahead of origin/{branch}")
    print(f"Runtime python: {runtime_python}  {'exists' if runtime_python.exists() else 'MISSING'}")
    print(f"Runtime version: {runtime_version or 'UNKNOWN'}")
    print(
        f"Runtime manifest: {RUNTIME_MANIFEST}  {'exists' if RUNTIME_MANIFEST.exists() else 'MISSING'}"
    )

    # Resolved CLI path — surface drift when `mempalace` on PATH does not
    # ultimately resolve to the dedicated runtime's CLI. System bin symlinks
    # (e.g. /opt/homebrew/bin/mempalace -> ~/.mempalace/venv/bin/mempalace)
    # are the blessed setup and are NOT drift — compare realpaths, not the
    # raw lookup result. Only flag when the final target disagrees.
    cli_path = _resolved_cli_path()
    runtime_cli = (DEFAULT_RUNTIME_DIR / "bin" / "mempalace").resolve()
    resolved_cli = Path(cli_path).resolve() if cli_path != "NOT ON PATH" else None
    drift = resolved_cli is not None and resolved_cli != runtime_cli
    drift_note = f"  (drift: resolves to {resolved_cli}, expected {runtime_cli})" if drift else ""
    print(f"CLI on PATH:    {cli_path}{drift_note}")
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
        print(f"\nRun `mempalace update` to pull {behind} commit(s) and reinstall/sync plugins.")


def update(
    *,
    repo: Path | None = None,
    agents: list[str] | None = None,
    tag: str | None = None,
    pull: bool = True,
):
    repo = repo or _find_repo()
    install_script = repo / INSTALL_SCRIPT

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
            requested = tag.strip()
            candidates = []
            for candidate in (
                requested,
                f"v{requested}" if not requested.startswith("v") else requested.removeprefix("v"),
            ):
                if candidate and candidate not in candidates:
                    candidates.append(candidate)

            checked_out = None
            for candidate in candidates:
                rc = _git(
                    "fetch",
                    "--force",
                    "origin",
                    f"refs/tags/{candidate}:refs/tags/{candidate}",
                    repo=repo,
                )
                if rc != 0:
                    continue
                rc = _git("checkout", candidate, repo=repo)
                if rc == 0:
                    checked_out = candidate
                    break

            if checked_out is None:
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

    cmd = ["bash", str(install_script)]
    if agents:
        for a in agents:
            flag = _AGENT_FLAGS.get(a)
            if flag:
                cmd.append(flag)
    else:
        cmd.append("--auto")
    cmd.append("--singleton")

    env = os.environ.copy()
    env.setdefault("MEMPAL_REPO", str(repo))
    env.setdefault("MEMPAL_RUNTIME_DIR", str(DEFAULT_RUNTIME_DIR))
    env.setdefault("MEMPAL_RUNTIME_PYTHON", str(_runtime_python()))
    env.setdefault("MEMPAL_RUNTIME_MANIFEST", str(RUNTIME_MANIFEST))

    print(f"Running: {' '.join(cmd)}")
    rc = subprocess.run(cmd, cwd=str(repo), env=env).returncode
    if rc != 0:
        print(f"install.sh exited with code {rc}", file=sys.stderr)
        sys.exit(rc)

    runtime_python = _runtime_python()
    runtime_version = _runtime_package_version(runtime_python)
    reported_version = runtime_version or "unknown-runtime-version"

    print(f"\nMemPalace {reported_version} — update complete.")
