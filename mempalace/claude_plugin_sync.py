"""Helpers for keeping Claude Code plugin cache metadata aligned to runtime version."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path


_PLUGIN_KEY = "mempalace@mempalace"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_version_tuple(name: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in name.split("."))
    except ValueError:
        return tuple()


def _highest_cache_dir(cache_root: Path) -> Path | None:
    dirs = [p for p in cache_root.iterdir() if p.is_dir()]
    versioned = [p for p in dirs if _parse_version_tuple(p.name)]
    if versioned:
        return sorted(versioned, key=lambda p: _parse_version_tuple(p.name))[-1]
    return dirs[0] if dirs else None


def _load_registry(installed_plugins_path: Path) -> dict:
    if not installed_plugins_path.exists():
        return {"plugins": {}}
    return json.loads(installed_plugins_path.read_text())


def _save_registry(installed_plugins_path: Path, data: dict) -> None:
    installed_plugins_path.parent.mkdir(parents=True, exist_ok=True)
    installed_plugins_path.write_text(json.dumps(data, indent=2) + "\n")


def sync_claude_cache_metadata(
    *,
    installed_plugins_path: Path,
    cache_root: Path,
    runtime_version: str,
) -> Path | None:
    """Ensure Claude's installed_plugins.json points at a runtime-versioned cache dir.

    Behavior:
    1. Read the current mempalace installPath from installed_plugins.json if present.
    2. If that dir is missing, fall back to the highest versioned cache dir.
    3. Materialize cache_root/<runtime_version> by copying the source dir when needed.
    4. Rewrite plugin.json.version in the target dir.
    5. Rewrite installed_plugins.json installPath/version/lastUpdated.

    Returns the target cache dir, or None when no source cache exists yet.
    """
    cache_root = cache_root.expanduser()
    installed_plugins_path = installed_plugins_path.expanduser()
    target_dir = cache_root / runtime_version

    registry = _load_registry(installed_plugins_path)
    plugins = registry.setdefault("plugins", {})
    entries = plugins.get(_PLUGIN_KEY, [])

    source_dir: Path | None = None
    if entries:
        install_path = entries[0].get("installPath", "")
        if install_path:
            candidate = Path(install_path).expanduser()
            if candidate.is_dir():
                source_dir = candidate

    if source_dir is None and cache_root.is_dir():
        source_dir = _highest_cache_dir(cache_root)

    if source_dir is None:
        return None

    if target_dir.exists() and target_dir != source_dir:
        shutil.rmtree(target_dir)
    if not target_dir.exists():
        shutil.copytree(source_dir, target_dir)

    plugin_json = target_dir / "plugin.json"
    if plugin_json.exists():
        plugin = json.loads(plugin_json.read_text())
        plugin["version"] = runtime_version
        plugin_json.write_text(json.dumps(plugin, indent=2) + "\n")

    now = _utc_now_iso()
    if not entries:
        entries = [{"scope": "user", "installedAt": now}]
        plugins[_PLUGIN_KEY] = entries
    entry = entries[0]
    entry["installPath"] = str(target_dir)
    entry["version"] = runtime_version
    entry["lastUpdated"] = now
    entry.setdefault("scope", "user")
    entry.setdefault("installedAt", now)

    _save_registry(installed_plugins_path, registry)
    return target_dir
