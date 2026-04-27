"""
MemPalace configuration system.

Priority: env vars > config file (~/.mempalace/config.json) > defaults
"""

import json
import os
import re
from pathlib import Path

try:
    from dotenv import load_dotenv as _load_dotenv
except (
    ImportError
):  # dotenv is a runtime dependency; guard for source checkouts without it installed
    _load_dotenv = None


def _load_env_file() -> None:
    """Load ~/.mempalace/env into os.environ without overriding existing values.

    Why: interactive shells source .zshenv / .bashrc, but launchd and systemd
    services do not. A single user-facing env file keeps config in one place
    for terminal and service contexts. Process env always wins, so explicit
    overrides from launchd plist / systemd unit / docker env still take effect.
    """
    env_file = Path(os.path.expanduser("~/.mempalace/env"))
    if _load_dotenv is None or not env_file.is_file():
        return
    _load_dotenv(dotenv_path=env_file, override=False)


_load_env_file()


# ── Input validation ──────────────────────────────────────────────────────────
# Shared sanitizers for wing/room/entity names. Prevents path traversal,
# excessively long strings, and special characters that could cause issues
# in file paths, SQLite, or ChromaDB metadata.

MAX_NAME_LENGTH = 128
_SAFE_NAME_RE = re.compile(r"^(?:[^\W_]|[^\W_][\w .'-]{0,126}[^\W_])$")


def sanitize_name(value: str, field_name: str = "name") -> str:
    """Validate and sanitize a wing/room/entity name.

    Raises ValueError if the name is invalid.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")

    value = value.strip()

    if len(value) > MAX_NAME_LENGTH:
        raise ValueError(f"{field_name} exceeds maximum length of {MAX_NAME_LENGTH} characters")

    # Block path traversal
    if ".." in value or "/" in value or "\\" in value:
        raise ValueError(f"{field_name} contains invalid path characters")

    # Block null bytes
    if "\x00" in value:
        raise ValueError(f"{field_name} contains null bytes")

    # Enforce safe character set
    if not _SAFE_NAME_RE.match(value):
        raise ValueError(f"{field_name} contains invalid characters")

    return value


def sanitize_kg_value(value: str, field_name: str = "value") -> str:
    """Validate a knowledge-graph entity name (subject or object).

    More permissive than sanitize_name — allows punctuation like commas,
    colons, and parentheses that are common in natural-language KG values.
    Only blocks null bytes and over-length strings.

    Not used for wing/room names (which have filesystem constraints) or
    predicates (which should be simple relationship identifiers).
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")

    value = value.strip()

    if len(value) > MAX_NAME_LENGTH:
        raise ValueError(f"{field_name} exceeds maximum length of {MAX_NAME_LENGTH} characters")

    if "\x00" in value:
        raise ValueError(f"{field_name} contains null bytes")

    return value


def sanitize_content(value: str, max_length: int = 100_000) -> str:
    """Validate and clean drawer/diary content for safe storage and embedding.

    - Strips leading/trailing whitespace
    - Removes null bytes and other control characters (except newline/tab)
    - Normalizes excessive whitespace runs
    - Truncates to max_length with a marker if exceeded
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("content must be a non-empty string")
    value = value.strip()
    # Remove null bytes and non-printable control characters (keep \n \t)
    value = "".join(c for c in value if c in ("\n", "\t") or (ord(c) >= 32) or (ord(c) > 127))
    # Collapse runs of 3+ blank lines to 2
    import re

    value = re.sub(r"\n{4,}", "\n\n\n", value)
    if len(value) > max_length:
        value = value[: max_length - 20] + "\n[truncated at limit]"
    if not value.strip():
        raise ValueError("content is empty after sanitization")
    return value


DEFAULT_PALACE_PATH = os.path.expanduser("~/.mempalace/palace")
DEFAULT_COLLECTION_NAME = "mempalace_drawers"

DEFAULT_TOPIC_WINGS = [
    "emotions",
    "consciousness",
    "memory",
    "technical",
    "identity",
    "family",
    "creative",
]

# Hall taxonomy — aligned with the official MemPalace doc
# (https://mempalaceofficial.com/concepts/the-palace.html). Halls describe
# how a memory connects to other memories within a wing, NOT what topic
# the memory is about (room handles topic). Valid hall values:
#
#   hall_facts        — decisions made, choices locked in, configurations
#   hall_events       — sessions, milestones, debugging, deployments, runs
#   hall_discoveries  — breakthroughs, new insights, surprising findings
#   hall_preferences  — habits, likes/dislikes, opinions, style choices
#   hall_advice       — recommendations, approvals, suggestions, solutions
#   hall_diary        — auto-save diary entries (one per save cycle)
#
# Auto-save prefers the LLM's hall judgment per drawer; detect_hall() in
# miner.py is a bilingual keyword-stem fallback for when the LLM omits or
# supplies an invalid value. Default fallback: hall_events (the most
# common shape of a conversation chunk — something happened).
VALID_HALLS = frozenset(
    {
        "hall_facts",
        "hall_events",
        "hall_discoveries",
        "hall_preferences",
        "hall_advice",
        "hall_diary",
    }
)
DEFAULT_HALL_FALLBACK = "hall_events"

DEFAULT_HALL_KEYWORDS = {
    "hall_facts": [
        "decided",
        "chose",
        "locked in",
        "agreed",
        "selected",
        "committed",
        "决定",
        "选择",
        "确定",
        "采用",
        "敲定",
        "同意",
    ],
    "hall_events": [
        "debugged",
        "fixed",
        "ran",
        "deployed",
        "encountered",
        "merged",
        "shipped",
        "rolled out",
        "executed",
        "triggered",
        "修复",
        "部署",
        "运行",
        "排查",
        "出现",
        "上线",
        "执行",
        "触发",
        "合并",
    ],
    "hall_discoveries": [
        "realized",
        "found out",
        "discovered",
        "breakthrough",
        "learned",
        "turns out",
        "noticed",
        "spotted",
        "发现",
        "原来",
        "意识到",
        "突破",
        "察觉",
        "注意到",
    ],
    "hall_preferences": [
        "prefer",
        "like",
        "hate",
        "always",
        "never",
        "favor",
        "dislike",
        "偏好",
        "喜欢",
        "讨厌",
        "总是",
        "从不",
        "习惯",
        "倾向",
        "更愿意",
    ],
    "hall_advice": [
        "recommend",
        "should",
        "suggest",
        "approved",
        "rejected",
        "advise",
        "propose",
        "best practice",
        "建议",
        "推荐",
        "应该",
        "批准",
        "驳回",
        "提议",
        "最佳实践",
    ],
}


class MempalaceConfig:
    """Configuration manager for MemPalace.

    Load order: env vars > config file > defaults.
    """

    def __init__(self, config_dir=None):
        """Initialize config.

        Args:
            config_dir: Override config directory (useful for testing).
                        Defaults to ~/.mempalace.
        """
        self._config_dir = (
            Path(config_dir) if config_dir else Path(os.path.expanduser("~/.mempalace"))
        )
        self._config_file = self._config_dir / "config.json"
        self._people_map_file = self._config_dir / "people_map.json"
        self._file_config = {}

        if self._config_file.exists():
            try:
                with open(self._config_file, "r") as f:
                    self._file_config = json.load(f)
            except (json.JSONDecodeError, OSError):
                self._file_config = {}

    @property
    def palace_path(self):
        """Path to the memory palace data directory."""
        env_val = os.environ.get("MEMPALACE_PALACE_PATH") or os.environ.get("MEMPAL_PALACE_PATH")
        if env_val:
            # Normalize: expand ~ and collapse .. to match the CLI --palace
            # code path (mcp_server.py:62) and prevent surprise redirection
            # when the env var contains unresolved components.
            return os.path.abspath(os.path.expanduser(env_val))
        return self._file_config.get("palace_path", DEFAULT_PALACE_PATH)

    @property
    def collection_name(self):
        """ChromaDB collection name."""
        return self._file_config.get("collection_name", DEFAULT_COLLECTION_NAME)

    @property
    def people_map(self):
        """Mapping of name variants to canonical names."""
        if self._people_map_file.exists():
            try:
                with open(self._people_map_file, "r") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        return self._file_config.get("people_map", {})

    @property
    def topic_wings(self):
        """List of topic wing names."""
        return self._file_config.get("topic_wings", DEFAULT_TOPIC_WINGS)

    @property
    def hall_keywords(self):
        """Mapping of hall names to keyword lists.

        Auto-migration: if the on-disk config still has the legacy
        taxonomy (emotions/consciousness/technical/etc.), return the
        new doc-aligned defaults instead. The legacy mapping never
        produces hall_facts/events/discoveries/preferences/advice and
        leaving it active would silently keep new-installs on the old
        labels. Disk file is left untouched — `mempalace init` rewrites
        it on the next explicit init call.
        """
        stored = self._file_config.get("hall_keywords")
        if not stored:
            return DEFAULT_HALL_KEYWORDS
        if any(key not in VALID_HALLS for key in stored):
            # Legacy taxonomy — silently swap to current defaults.
            return DEFAULT_HALL_KEYWORDS
        return stored

    @property
    def entity_languages(self):
        """Languages whose entity-detection patterns should be applied.

        Reads from env var ``MEMPALACE_ENTITY_LANGUAGES`` (comma-separated)
        first, then the ``entity_languages`` field in ``config.json``,
        defaulting to ``["en"]``.
        """
        env_val = os.environ.get("MEMPALACE_ENTITY_LANGUAGES") or os.environ.get(
            "MEMPAL_ENTITY_LANGUAGES"
        )
        if env_val:
            return [s.strip() for s in env_val.split(",") if s.strip()] or ["en"]
        cfg = self._file_config.get("entity_languages")
        if isinstance(cfg, list) and cfg:
            return [str(s) for s in cfg]
        return ["en"]

    def set_entity_languages(self, languages):
        """Persist the entity-detection language list to ``config.json``."""
        normalized = [s.strip() for s in languages if s and s.strip()]
        if not normalized:
            normalized = ["en"]
        self._file_config["entity_languages"] = normalized
        self._config_dir.mkdir(parents=True, exist_ok=True)
        try:
            with open(self._config_file, "w", encoding="utf-8") as f:
                json.dump(self._file_config, f, indent=2, ensure_ascii=False)
        except OSError:
            pass
        try:
            self._config_file.chmod(0o600)
        except (OSError, NotImplementedError):
            pass
        return normalized

    @property
    def hook_silent_save(self):
        """Whether the stop hook saves directly (True) or blocks for MCP calls (False)."""
        return self._file_config.get("hooks", {}).get("silent_save", True)

    @property
    def hook_desktop_toast(self):
        """Whether the stop hook shows a desktop notification via notify-send."""
        return self._file_config.get("hooks", {}).get("desktop_toast", False)

    def set_hook_setting(self, key: str, value: bool):
        """Update a hook setting and write config to disk."""
        if "hooks" not in self._file_config:
            self._file_config["hooks"] = {}
        self._file_config["hooks"][key] = value
        try:
            with open(self._config_file, "w", encoding="utf-8") as f:
                json.dump(self._file_config, f, indent=2, ensure_ascii=False)
        except OSError:
            pass

    def init(self):
        """Create config directory and write default config.json if it doesn't exist."""
        self._config_dir.mkdir(parents=True, exist_ok=True)
        # Restrict directory permissions to owner only (Unix)
        try:
            self._config_dir.chmod(0o700)
        except (OSError, NotImplementedError):
            pass  # Windows doesn't support Unix permissions
        if not self._config_file.exists():
            default_config = {
                "palace_path": DEFAULT_PALACE_PATH,
                "collection_name": DEFAULT_COLLECTION_NAME,
                "topic_wings": DEFAULT_TOPIC_WINGS,
                "hall_keywords": DEFAULT_HALL_KEYWORDS,
            }
            with open(self._config_file, "w") as f:
                json.dump(default_config, f, indent=2)
            # Restrict config file to owner read/write only
            try:
                self._config_file.chmod(0o600)
            except (OSError, NotImplementedError):
                pass
        return self._config_file

    def save_people_map(self, people_map):
        """Write people_map.json to config directory.

        Args:
            people_map: Dict mapping name variants to canonical names.
        """
        self._config_dir.mkdir(parents=True, exist_ok=True)
        with open(self._people_map_file, "w") as f:
            json.dump(people_map, f, indent=2)
        try:
            self._people_map_file.chmod(0o600)
        except (OSError, NotImplementedError):
            pass
        return self._people_map_file
