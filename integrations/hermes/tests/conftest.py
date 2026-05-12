"""Test isolation for Hermes MemPalace provider tests.

Two isolation goals:

1. Never hit real LLM / embedding APIs. Tests strip
   ``MEMPAL_EMBEDDING_MODEL`` / ``MEMPAL_RECALL_LLM`` so embedding falls
   back to ChromaDB's built-in MiniLM and the recall gate is disabled.

2. Never hit the real shared palace at ``~/.mempalace/``. After the
   parity work (mempalace commit 32dbd5a), ``resolve_paths`` defaults to
   ``mempalace.config.DEFAULT_PALACE_PATH`` / ``mempalace.knowledge_graph.DEFAULT_KG_PATH``
   so Claude Code, Codex, and Hermes all share one store on this machine.
   That's the right default at runtime, but it's fatal for tests — a test
   run would read (and in some cases corrupt) the user's real memory.
   The ``_isolated_shared_defaults`` autouse fixture re-points those two
   module constants at a per-test tmp directory so each test gets a
   fresh, empty palace and KG.
"""

import os

import pytest

for _var in (
    "MEMPAL_EMBEDDING_MODEL",
    "MEMPALACE_EMBEDDING_MODEL",
    "MEMPAL_LLM",
    "MEMPAL_LLM_ENDPOINT",
    "MEMPAL_LLM_MODEL",
    "MEMPAL_LLM_KEY",
    "MEMPAL_RECALL_LLM",
    "MEMPAL_RECALL_ENDPOINT",
    "MEMPAL_RECALL_MODEL",
    "MEMPAL_RECALL_KEY",
):
    os.environ.pop(_var, None)

try:
    from mempalace.embedding import reset_cache
    from mempalace.palace import _reset_embedding_cache

    reset_cache()
    _reset_embedding_cache()
except ImportError:
    pass


@pytest.fixture(autouse=True)
def _isolated_shared_defaults(tmp_path, monkeypatch):
    """Re-point the "shared" palace + KG defaults at a tmp dir per test.

    ``resolve_paths`` reads ``mempalace.config.DEFAULT_PALACE_PATH`` and
    ``mempalace.knowledge_graph.DEFAULT_KG_PATH`` on each call via a local
    import, so monkeypatching the module attributes before the call takes
    effect. This keeps the production default (shared palace) intact
    while isolating tests.

    Also drops any cached ``ChromaBackend`` clients between tests — once a
    PersistentClient is instantiated for a path, ChromaDB refuses a second
    instance with different settings, and the cache from one test would
    poison the next.
    """
    import mempalace.config as _cfg
    import mempalace.knowledge_graph as _kg

    isolated = tmp_path / "shared-mempalace"
    monkeypatch.setattr(_cfg, "DEFAULT_PALACE_PATH", str(isolated / "palace"))
    monkeypatch.setattr(_kg, "DEFAULT_KG_PATH", str(isolated / "knowledge_graph.sqlite3"))

    try:
        from mempalace.palace import _DEFAULT_BACKEND

        _DEFAULT_BACKEND._clients.clear()
        if hasattr(_DEFAULT_BACKEND, "_freshness"):
            _DEFAULT_BACKEND._freshness.clear()
        _DEFAULT_BACKEND._closed = False
    except Exception:
        pass
    try:
        from chromadb.api.shared_system_client import SharedSystemClient

        SharedSystemClient.clear_system_cache()
    except Exception:
        pass

    yield isolated

    try:
        from mempalace.palace import _DEFAULT_BACKEND

        _DEFAULT_BACKEND._clients.clear()
        if hasattr(_DEFAULT_BACKEND, "_freshness"):
            _DEFAULT_BACKEND._freshness.clear()
        _DEFAULT_BACKEND._closed = False
    except Exception:
        pass
    try:
        from chromadb.api.shared_system_client import SharedSystemClient

        SharedSystemClient.clear_system_cache()
    except Exception:
        pass
