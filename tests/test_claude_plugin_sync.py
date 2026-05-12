"""Tests for Claude plugin cache/version metadata sync helpers."""

import json
from pathlib import Path

from mempalace.claude_plugin_sync import sync_claude_cache_metadata


def _write_plugin_json(path: Path, version: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"name": "mempalace", "version": version}, indent=2) + "\n")


def _write_registry(path: Path, install_path: Path, version: str = "3.3.3"):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "plugins": {
            "mempalace@mempalace": [
                {
                    "scope": "user",
                    "installPath": str(install_path),
                    "version": version,
                    "installedAt": "2026-04-10T11:51:42Z",
                    "lastUpdated": "2026-04-25T18:08:47Z",
                }
            ]
        }
    }
    path.write_text(json.dumps(data, indent=2) + "\n")


def test_sync_claude_cache_metadata_copies_installed_cache_and_updates_registry(tmp_path):
    cache_root = tmp_path / ".claude" / "plugins" / "cache" / "mempalace" / "mempalace"
    old_cache = cache_root / "3.3.3"
    _write_plugin_json(old_cache / "plugin.json", "3.3.310")
    (old_cache / "skills" / "mempalace").mkdir(parents=True)
    (old_cache / "skills" / "mempalace" / "SKILL.md").write_text("skill")

    registry = tmp_path / ".claude" / "plugins" / "installed_plugins.json"
    _write_registry(registry, old_cache, version="3.3.3")

    target = sync_claude_cache_metadata(
        installed_plugins_path=registry,
        cache_root=cache_root,
        runtime_version="3.3.311",
    )

    assert target == cache_root / "3.3.311"
    assert target.is_dir()
    assert (target / "skills" / "mempalace" / "SKILL.md").read_text() == "skill"
    plugin = json.loads((target / "plugin.json").read_text())
    assert plugin["version"] == "3.3.311"

    data = json.loads(registry.read_text())
    entry = data["plugins"]["mempalace@mempalace"][0]
    assert entry["installPath"] == str(target)
    assert entry["version"] == "3.3.311"
    assert entry["lastUpdated"] != "2026-04-25T18:08:47Z"


def test_sync_claude_cache_metadata_falls_back_to_highest_cache_when_registry_path_missing(
    tmp_path,
):
    cache_root = tmp_path / ".claude" / "plugins" / "cache" / "mempalace" / "mempalace"
    _write_plugin_json(cache_root / "3.3.3" / "plugin.json", "3.3.310")
    _write_plugin_json(cache_root / "3.3.310" / "plugin.json", "3.3.310")

    registry = tmp_path / ".claude" / "plugins" / "installed_plugins.json"
    _write_registry(registry, cache_root / "missing", version="3.3.3")

    target = sync_claude_cache_metadata(
        installed_plugins_path=registry,
        cache_root=cache_root,
        runtime_version="3.3.311",
    )

    assert target == cache_root / "3.3.311"
    plugin = json.loads((target / "plugin.json").read_text())
    assert plugin["version"] == "3.3.311"

    data = json.loads(registry.read_text())
    entry = data["plugins"]["mempalace@mempalace"][0]
    assert entry["installPath"] == str(target)
    assert entry["version"] == "3.3.311"
