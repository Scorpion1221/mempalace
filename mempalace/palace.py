"""
palace.py — Shared palace operations.

Consolidates collection access patterns used by both miners and the MCP server.
"""

import contextlib
import hashlib
import os
import re
import time
from pathlib import Path

from .backends.chroma import ChromaBackend

SKIP_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    "dist",
    "build",
    ".next",
    "coverage",
    ".mempalace",
    ".ruff_cache",
    ".mypy_cache",
    ".pytest_cache",
    ".cache",
    ".tox",
    ".nox",
    ".idea",
    ".vscode",
    ".ipynb_checkpoints",
    ".eggs",
    "htmlcov",
    "target",
    "tool-results",
}

_DEFAULT_BACKEND = ChromaBackend()

_embedding_fn_cache: object = "UNSET"


def _get_embedding_fn():
    """Lazily resolve and cache the configured embedding function.

    Only successful EFs are cached. A ``None`` return (no MEMPAL_EMBEDDING_*
    set, or transient lookup failure) does NOT pollute the cache, so the
    next call after env stabilises can still resolve a real EF instead of
    serving stale ``None`` for the whole process lifetime.
    """
    global _embedding_fn_cache
    if _embedding_fn_cache != "UNSET":
        return _embedding_fn_cache
    from .embedding import get_embedding_function

    ef = get_embedding_function()
    if ef is not None:
        _embedding_fn_cache = ef
    return ef


def _reset_embedding_cache():
    """Reset the embedding function cache. Used in tests."""
    global _embedding_fn_cache
    _embedding_fn_cache = "UNSET"


# Schema version for drawer normalization. Bump when the normalization
# pipeline changes in a way that existing drawers should be rebuilt to pick up
# (e.g., new noise-stripping rules). `file_already_mined` treats drawers with
# a missing or stale `normalize_version` as "not mined", so the next mine pass
# silently rebuilds them — users don't need to manually erase + re-mine.
#
# v2 (2026-04): introduced strip_noise() for Claude Code JSONL; previous
#               drawers stored system tags / hook chrome verbatim.
# v3 (2026-04): added is_noise_content() chunk filter + strip_noise in miner.
NORMALIZE_VERSION = 3


def get_collection(
    palace_path: str,
    collection_name: str = "mempalace_drawers",
    create: bool = True,
):
    """Get the palace collection through the backend layer."""
    return _DEFAULT_BACKEND.get_collection(
        palace_path,
        collection_name=collection_name,
        create=create,
        embedding_function=_get_embedding_fn(),
    )


def get_closets_collection(palace_path: str, create: bool = True):
    """Get the closets collection — the searchable index layer."""
    return get_collection(palace_path, collection_name="mempalace_closets", create=create)


CLOSET_CHAR_LIMIT = 1500  # fill closet until ~1500 chars, then start a new one
CLOSET_EXTRACT_WINDOW = 5000  # how many chars of source content to scan for entities/topics

# Common capitalized words that look like proper nouns but are usually
# sentence-starters or filler. Filtered out of entity extraction.
_ENTITY_STOPLIST = frozenset(
    {
        "The",
        "This",
        "That",
        "These",
        "Those",
        "When",
        "Where",
        "What",
        "Why",
        "Who",
        "Which",
        "How",
        "After",
        "Before",
        "Then",
        "Now",
        "Here",
        "There",
        "And",
        "But",
        "Or",
        "Yet",
        "So",
        "If",
        "Else",
        "Yes",
        "No",
        "Maybe",
        "Okay",
        "User",
        "Assistant",
        "System",
        "Tool",
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    }
)


_CANDIDATE_RX_CACHE = None


def _candidate_entity_words(text: str) -> list:
    """Find entity candidate words using i18n-aware patterns.

    Uses the same candidate_patterns as entity_detector (loaded from locale
    JSON files via get_entity_patterns), so non-Latin names (Cyrillic,
    accented Latin, etc.) are detected alongside ASCII names.
    """
    global _CANDIDATE_RX_CACHE
    if _CANDIDATE_RX_CACHE is None:
        from .config import MempalaceConfig
        from .i18n import get_entity_patterns

        patterns = get_entity_patterns(MempalaceConfig().entity_languages)
        rxs = []
        for pat in patterns["candidate_patterns"]:
            try:
                rxs.append(re.compile(pat))
            except re.error:
                continue
        _CANDIDATE_RX_CACHE = rxs
    words = []
    for rx in _CANDIDATE_RX_CACHE:
        words.extend(rx.findall(text))
    return words


def build_closet_lines(source_file, drawer_ids, content, wing, room):
    """Build compact closet pointer lines from drawer content.

    Returns a LIST of lines (not joined). Each line is one complete topic
    pointer — never split across closets.

    Format: topic|entities|→drawer_ids
    """
    import re
    from pathlib import Path

    drawer_ref = ",".join(drawer_ids[:3])
    window = content[:CLOSET_EXTRACT_WINDOW]

    # Extract proper nouns (2+ occurrences). Uses i18n-aware patterns so
    # non-Latin names (Cyrillic, accented Latin, etc.) are also detected.
    words = _candidate_entity_words(window)
    word_freq = {}
    for w in words:
        if w in _ENTITY_STOPLIST:
            continue
        word_freq[w] = word_freq.get(w, 0) + 1
    entities = sorted(
        [w for w, c in word_freq.items() if c >= 2],
        key=lambda w: -word_freq[w],
    )[:5]
    entity_str = ";".join(entities) if entities else ""

    # Extract key phrases — action verbs + context
    topics = []
    for pattern in [
        r"(?:built|fixed|wrote|added|pushed|tested|created|decided|migrated|reviewed|deployed|configured|removed|updated)\s+[\w\s]{3,40}",
    ]:
        topics.extend(re.findall(pattern, window, re.IGNORECASE))
    # Also grab section headers if present
    for header in re.findall(r"^#{1,3}\s+(.{5,60})$", window, re.MULTILINE):
        topics.append(header.strip())
    # Dedupe preserving order
    topics = list(dict.fromkeys(t.strip().lower() for t in topics))[:12]

    # Extract quotes
    quotes = re.findall(r'"([^"]{15,150})"', window)

    # Build pointer lines — each one is atomic, never split
    lines = []
    for topic in topics:
        lines.append(f"{topic}|{entity_str}|→{drawer_ref}")
    for quote in quotes[:3]:
        lines.append(f'"{quote}"|{entity_str}|→{drawer_ref}')

    # Always have at least one line
    if not lines:
        name = Path(source_file).stem[:40]
        lines.append(f"{wing}/{room}/{name}|{entity_str}|→{drawer_ref}")

    return lines


def purge_file_closets(closets_col, source_file: str) -> None:
    """Delete every closet associated with ``source_file``.

    Call this before ``upsert_closet_lines`` on a re-mine so stale topics
    from a prior schema/version don't survive in the closet collection.
    Mirrors the drawer-purge step in process_file().
    """
    try:
        closets_col.delete(where={"source_file": source_file})
    except Exception:
        pass


def upsert_closet_lines(closets_col, closet_id_base, lines, metadata):
    """Write topic lines to closets, packed greedily without splitting a line.

    Closets are deterministically numbered (``..._01``, ``..._02``, …) and
    each ``upsert`` fully overwrites the prior content at that ID. Callers
    are expected to ``purge_file_closets`` first when re-mining a source
    file so stale-numbered closets from larger prior runs don't leak.

    Returns the number of closets written.
    """
    closet_num = 1
    current_lines: list = []
    current_chars = 0
    closets_written = 0

    def _flush():
        nonlocal closets_written
        if not current_lines:
            return
        closet_id = f"{closet_id_base}_{closet_num:02d}"
        text = "\n".join(current_lines)
        closets_col.upsert(documents=[text], ids=[closet_id], metadatas=[metadata])
        closets_written += 1

    for line in lines:
        line_len = len(line)
        # Would this line fit whole in the current closet?
        if current_chars > 0 and current_chars + line_len + 1 > CLOSET_CHAR_LIMIT:
            _flush()
            closet_num += 1
            current_lines = []
            current_chars = 0

        current_lines.append(line)
        current_chars += line_len + 1  # +1 for newline

    _flush()
    return closets_written


@contextlib.contextmanager
def mine_lock(source_file: str):
    """Cross-platform file lock for mine operations.

    Prevents multiple agents from mining the same file simultaneously,
    which causes duplicate drawers when the delete+insert cycle interleaves.
    """
    lock_dir = os.path.join(os.path.expanduser("~"), ".mempalace", "locks")
    os.makedirs(lock_dir, exist_ok=True)
    lock_path = os.path.join(
        lock_dir, hashlib.sha256(source_file.encode()).hexdigest()[:16] + ".lock"
    )

    lf = open(lock_path, "w")
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lf.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(lf, fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lf.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lf, fcntl.LOCK_UN)
        except Exception:
            pass
        lf.close()


class PalaceWriteLockTimeout(TimeoutError):
    """Raised when palace_write_lock cannot be acquired within timeout."""


# Polling interval for non-blocking lock acquisition. Kept tight (50ms) so
# legitimate writers aren't penalised for waiting on a contended palace.
_PALACE_LOCK_POLL_INTERVAL_S = 0.05


@contextlib.contextmanager
def palace_write_lock(palace_path: str | Path, timeout: float = 30.0):
    """Palace-wide exclusive write lock. All ChromaDB write paths MUST hold this.

    Why: ChromaDB's HNSW writer is not multi-process safe. Concurrent writes
    from different MCP servers / mine subprocesses / async_save_worker
    processes can corrupt segment files (the palace then fails to load).
    ``mine_lock`` only serialises writes to the SAME source file — it does
    not stop two unrelated mines from hitting the same ChromaDB at once.
    This lock closes that gap by providing a single palace-wide gate.

    Crash-safe: ``fcntl.flock`` (Unix) and ``msvcrt.locking`` (Windows) are
    advisory kernel-level locks. They are released automatically when the
    holding process exits, even on SIGKILL — so a crashed writer cannot
    permanently wedge the palace.

    Per-palace: the lock file name is derived from ``Path.resolve()`` of
    ``palace_path``, so two different palaces NEVER block each other, while
    symlinks and relative paths to the SAME palace map to the same lock.

    Scope — what this lock does NOT cover:
        This lock guards ChromaDB HNSW segment writes only. Other palace
        state has its own locking and does NOT participate in
        ``palace_write_lock``:

          - Tunnel JSON (``~/.mempalace/tunnels.json``): protected by
            ``mine_lock(_TUNNEL_FILE)`` — see ``palace_graph.create_tunnel``
            and ``palace_graph.delete_tunnel``.
          - Knowledge graph SQLite (``~/.mempalace/knowledge_graph.sqlite3``):
            protected by ``KnowledgeGraph``'s internal ``threading.Lock``
            plus ``PRAGMA journal_mode=WAL`` for cross-process readers —
            see ``knowledge_graph.py``. Note: the threading.Lock only
            protects in-process concurrent threads; cross-process safety
            relies on SQLite's WAL + ``timeout=10`` retry, not on this lock.
          - Verbatim source files held by the miner: protected by
            ``mine_lock(source_file)`` — see ``miner.process_file``.

        Callers writing to those stores do NOT need to acquire
        ``palace_write_lock``. Callers writing to ChromaDB MUST acquire it.

    Args:
        palace_path: Path to the palace directory (any form — absolute,
            relative, or symlink — is normalised via ``Path.resolve()``).
        timeout: Maximum seconds to wait for the lock. Acquisition uses
            non-blocking probes with a 50ms backoff; a real timeout raises
            ``PalaceWriteLockTimeout`` instead of blocking the caller.

    Raises:
        PalaceWriteLockTimeout: If the lock could not be acquired within
            ``timeout`` seconds.
    """
    resolved = str(Path(palace_path).resolve())
    palace_hash = hashlib.sha256(resolved.encode()).hexdigest()[:16]

    lock_dir = os.path.join(os.path.expanduser("~"), ".mempalace", "locks")
    os.makedirs(lock_dir, exist_ok=True)
    lock_path = os.path.join(lock_dir, f"palace_write_{palace_hash}.lock")

    lf = open(lock_path, "w")
    acquired = False
    deadline = time.monotonic() + timeout
    try:
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    msvcrt.locking(lf.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise PalaceWriteLockTimeout(
                            f"Could not acquire palace write lock for {resolved} within {timeout}s"
                        ) from None
                    time.sleep(_PALACE_LOCK_POLL_INTERVAL_S)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise PalaceWriteLockTimeout(
                            f"Could not acquire palace write lock for {resolved} within {timeout}s"
                        ) from None
                    time.sleep(_PALACE_LOCK_POLL_INTERVAL_S)
        yield
    finally:
        if acquired:
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(lf.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
        try:
            lf.close()
        except Exception:
            pass


def ensure_palace_initialized(palace_path: str | Path, timeout: float = 30.0) -> None:
    """Idempotently ensure the ChromaDB palace schema exists. Safe for concurrent callers.

    Why: ChromaDB's ``PersistentClient.__init__`` runs internal ``CREATE TABLE``
    statements that race when N processes initialize a brand-new palace
    simultaneously (the loser raises
    ``InternalError: table collections already exists``). This helper
    serializes the first-open via ``palace_write_lock`` — after the first
    successful call, subsequent calls are cheap no-ops because
    ``chroma.sqlite3`` already exists and ChromaDB's idempotency suffices.

    MUST be called at writer entry points (MCP server startup, miner
    ``main()``, ``async_save_worker`` startup) BEFORE any other code attempts
    to open the palace.

    Idempotent: safe to call multiple times, by multiple processes, in any
    order.

    Args:
        palace_path: Directory that holds (or will hold) ``chroma.sqlite3``.
        timeout: Maximum seconds to wait for ``palace_write_lock`` on the
            slow path. The fast path (sqlite already present and non-empty)
            never takes the lock, so contention is bounded to the very
            first race per palace.

    Raises:
        PalaceWriteLockTimeout: If the slow-path lock could not be
            acquired within ``timeout`` seconds. Callers SHOULD log and
            continue rather than crash — the next invocation will retry.
    """
    palace_path = Path(palace_path).resolve()

    # Fast path: a non-empty chroma.sqlite3 means the schema is initialized.
    # Skip the lock entirely so this helper is essentially free after the
    # first successful run on any given palace.
    sqlite_path = palace_path / "chroma.sqlite3"
    if sqlite_path.exists() and sqlite_path.stat().st_size > 0:
        return

    # Slow path: take the cross-process lock and bootstrap exactly once.
    palace_path.mkdir(parents=True, exist_ok=True)
    with palace_write_lock(palace_path, timeout=timeout):
        # Re-check inside the lock — another process may have just
        # bootstrapped while we were waiting on the lock.
        if sqlite_path.exists() and sqlite_path.stat().st_size > 0:
            return
        # Force ChromaDB to create the schema. ``list_collections`` is the
        # cheapest call that triggers full schema initialization without
        # creating any user-visible collections.
        import chromadb

        client = chromadb.PersistentClient(path=str(palace_path))
        client.list_collections()
        # Drop the strong reference so ChromaDB releases its memory map
        # before this function returns. Python's refcount-based GC reclaims
        # the client immediately on the ``del`` (no cycles, see the
        # ``_client_for_write`` docstring on why ``gc.collect()`` is
        # unnecessary here).
        del client


def file_already_mined(collection, source_file: str, check_mtime: bool = False) -> bool:
    """Check if a file has already been filed in the palace.

    Returns False (so the file gets re-mined) when:
      - no drawers exist for this source_file
      - the stored `normalize_version` is missing or older than the current
        schema (triggers silent rebuild after a normalization upgrade)
      - `check_mtime=True` and the file's mtime differs from the stored one

    When check_mtime=True (used by project miner), also re-mines on content
    change. When check_mtime=False (used by convo miner), transcripts are
    assumed immutable, so only the version gate triggers a rebuild.
    """
    try:
        results = collection.get(where={"source_file": source_file}, limit=1)
        if not results.get("ids"):
            return False
        stored_meta = results.get("metadatas", [{}])[0] or {}
        # Pre-v2 drawers have no version field — treat them as stale.
        stored_version = stored_meta.get("normalize_version", 1)
        if stored_version < NORMALIZE_VERSION:
            return False
        if check_mtime:
            stored_mtime = stored_meta.get("source_mtime")
            if stored_mtime is None:
                return False
            current_mtime = os.path.getmtime(source_file)
            return abs(float(stored_mtime) - current_mtime) < 0.001
        return True
    except Exception:
        return False
