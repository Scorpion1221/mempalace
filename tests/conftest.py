"""
conftest.py — Shared fixtures for MemPalace tests.

Provides isolated palace and knowledge graph instances so tests never
touch the user's real data or leak temp files on failure.

HOME is redirected to a temp directory at module load time — before any
mempalace imports — so that module-level initialisations (e.g.
``_kg = KnowledgeGraph()`` in mcp_server) write to a throwaway location
instead of the real user profile.
"""

import os
import shutil
import tempfile

# ── Isolate HOME before any mempalace imports ──────────────────────────
_original_env = {}
_session_tmp = tempfile.mkdtemp(prefix="mempalace_session_")

for _var in ("HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH"):
    _original_env[_var] = os.environ.get(_var)

# Ensure tests use the default (local) embedding and no LLM recall,
# not any API-based config from the user's shell environment. The probe
# in recall_llm.is_enabled() reads endpoint+model from MEMPAL_LLM_* (or
# legacy MEMPAL_RECALL_*) — strip both forms so probe-based default-on
# does not accidentally trigger LLM calls in tests.
for _embed_var in (
    "MEMPAL_EMBEDDING_MODEL",
    "MEMPAL_EMBEDDING_ENDPOINT",
    "MEMPAL_EMBEDDING_KEY",
    "MEMPAL_EMBEDDING_DIMS",
    "MEMPAL_LLM",
    "MEMPAL_LLM_ENDPOINT",
    "MEMPAL_LLM_MODEL",
    "MEMPAL_LLM_KEY",
    "MEMPAL_RECALL_LLM",
    "MEMPAL_RECALL_ENDPOINT",
    "MEMPAL_RECALL_MODEL",
    "MEMPAL_RECALL_KEY",
):
    _original_env[_embed_var] = os.environ.pop(_embed_var, None)

os.environ["MEMPAL_EMBEDDING_MODEL"] = "hash"
os.environ["MEMPAL_LLM"] = "0"

os.environ["HOME"] = _session_tmp
os.environ["USERPROFILE"] = _session_tmp
os.environ["HOMEDRIVE"] = os.path.splitdrive(_session_tmp)[0] or "C:"
os.environ["HOMEPATH"] = os.path.splitdrive(_session_tmp)[1] or _session_tmp

# Now it is safe to import mempalace modules that trigger initialisation.
import chromadb  # noqa: E402
import pytest  # noqa: E402
import chromadb.api.types as _chroma_types  # noqa: E402
import chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 as _chroma_onnx  # noqa: E402

# Reset embedding caches so they pick up the cleaned env vars above.
import mempalace.embedding as _embedding_mod  # noqa: E402


class _DeterministicTestEmbeddingFunction:
    def __init__(self, preferred_providers=None, dimensions=384):
        self.preferred_providers = preferred_providers or []
        self.dimensions = dimensions

    def name(self):
        return "default"

    def __call__(self, input):
        return self.embed_documents(input)

    def embed_query(self, input):
        return self.embed_documents([input])

    def embed_documents(self, input):
        vectors = []
        for text in input:
            vec = [0.0] * self.dimensions
            tokens = str(text).lower().replace(".", " ").replace(",", " ").split()
            for token in tokens or [str(text)]:
                bucket = sum(ord(ch) for ch in token) % len(vec)
                vec[bucket] += 1.0
            vectors.append(vec)
        return vectors


_embedding_mod._build_ef_class = lambda: _DeterministicTestEmbeddingFunction
_reset_embedding_cache = _embedding_mod.reset_cache

_reset_embedding_cache()


def _deterministic_chroma_default_call(self, input):
    """Replace Chroma's network-backed default EF in tests.

    Several tests intentionally instantiate raw Chroma collections rather
    than going through mempalace.embedding. Chroma's default embedding
    function downloads ONNX model assets on first use, which is both slow and
    unavailable in the sandboxed test environment. Keep those tests local by
    making Chroma's own default EF deterministic too.
    """

    return _DeterministicTestEmbeddingFunction()(input)


_chroma_types.DefaultEmbeddingFunction.__call__ = _deterministic_chroma_default_call
_chroma_onnx.ONNXMiniLM_L6_V2.__call__ = _deterministic_chroma_default_call
try:
    from mempalace.palace import _reset_embedding_cache as _reset_palace_cache  # noqa: E402

    _reset_palace_cache()
except ImportError:
    pass

from mempalace.config import MempalaceConfig  # noqa: E402
from mempalace.knowledge_graph import KnowledgeGraph  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_mcp_cache():
    """Reset cached MCP state between tests without importing mcp_server.

    If mempalace.mcp_server is already imported, close/clear its KG cache and
    Chroma client cache. If it has not been imported, leave it unloaded so
    fork/spawn-based tests do not inherit extra Chroma/SQLite state.
    """

    def _clear_cache():
        try:
            import sys

            mcp_server = sys.modules.get("mempalace.mcp_server")
            if mcp_server is not None:
                for kg in list(getattr(mcp_server, "_kg_by_path", {}).values()):
                    close = getattr(kg, "close", None)
                    if close is not None:
                        try:
                            close()
                        except Exception:
                            pass

                if hasattr(mcp_server, "_kg_by_path"):
                    mcp_server._kg_by_path.clear()

                mcp_server._client_cache = None
                mcp_server._collection_cache = None
                mcp_server._collection_has_ef = False
        except AttributeError:
            pass

        try:
            # Reset the per-process quarantine gate so tests don't leak
            # state through ChromaBackend._quarantined_paths.
            from mempalace.backends.chroma import ChromaBackend

            ChromaBackend._quarantined_paths.clear()
        except (ImportError, AttributeError):
            pass

    _clear_cache()
    yield
    _clear_cache()


@pytest.fixture(scope="session", autouse=True)
def _isolate_home():
    """Ensure HOME points to a temp dir for the entire test session.

    The env vars were already set at module level (above) so that
    module-level initialisations are captured.  This fixture simply
    restores the originals on teardown and cleans up the temp dir.
    """
    yield
    for var, orig in _original_env.items():
        if orig is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = orig
    shutil.rmtree(_session_tmp, ignore_errors=True)


@pytest.fixture
def tmp_dir():
    """Create and auto-cleanup a temporary directory."""
    d = tempfile.mkdtemp(prefix="mempalace_test_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def palace_path(tmp_dir):
    """Path to an empty palace directory inside tmp_dir."""
    p = os.path.join(tmp_dir, "palace")
    os.makedirs(p)
    return p


@pytest.fixture
def config(tmp_dir, palace_path):
    """A MempalaceConfig pointing at the temp palace."""
    cfg_dir = os.path.join(tmp_dir, "config")
    os.makedirs(cfg_dir)
    import json

    with open(os.path.join(cfg_dir, "config.json"), "w") as f:
        json.dump({"palace_path": palace_path}, f)
    return MempalaceConfig(config_dir=cfg_dir)


@pytest.fixture
def collection(palace_path):
    """A ChromaDB collection pre-seeded in the temp palace."""
    client = chromadb.PersistentClient(path=palace_path)
    col = client.get_or_create_collection(
        "mempalace_drawers",
        metadata={"hnsw:space": "cosine"},
        embedding_function=_embedding_mod.get_embedding_function("cpu"),
    )
    yield col
    client.delete_collection("mempalace_drawers")
    del client


@pytest.fixture
def seeded_collection(collection):
    """Collection with a handful of representative drawers."""
    collection.add(
        ids=[
            "drawer_proj_backend_aaa",
            "drawer_proj_backend_bbb",
            "drawer_proj_frontend_ccc",
            "drawer_notes_planning_ddd",
        ],
        documents=[
            "The authentication module uses JWT tokens for session management. "
            "Tokens expire after 24 hours. Refresh tokens are stored in HttpOnly cookies.",
            "Database migrations are handled by Alembic. We use PostgreSQL 15 "
            "with connection pooling via pgbouncer.",
            "The React frontend uses TanStack Query for server state management. "
            "All API calls go through a centralized fetch wrapper.",
            "Sprint planning: migrate auth to passkeys by Q3. "
            "Evaluate ChromaDB alternatives for vector search.",
        ],
        metadatas=[
            {
                "wing": "project",
                "room": "backend",
                "source_file": "auth.py",
                "chunk_index": 0,
                "added_by": "miner",
                "filed_at": "2026-01-01T00:00:00",
            },
            {
                "wing": "project",
                "room": "backend",
                "source_file": "db.py",
                "chunk_index": 0,
                "added_by": "miner",
                "filed_at": "2026-01-02T00:00:00",
            },
            {
                "wing": "project",
                "room": "frontend",
                "source_file": "App.tsx",
                "chunk_index": 0,
                "added_by": "miner",
                "filed_at": "2026-01-03T00:00:00",
            },
            {
                "wing": "notes",
                "room": "planning",
                "source_file": "sprint.md",
                "chunk_index": 0,
                "added_by": "miner",
                "filed_at": "2026-01-04T00:00:00",
            },
        ],
    )
    return collection


@pytest.fixture
def kg(tmp_dir):
    """An isolated KnowledgeGraph using a temp SQLite file."""
    db_path = os.path.join(tmp_dir, "test_kg.sqlite3")
    graph = KnowledgeGraph(db_path=db_path)
    yield graph
    graph.close()


@pytest.fixture
def seeded_kg(kg):
    """KnowledgeGraph pre-loaded with sample triples."""
    kg.add_entity("Alice", entity_type="person")
    kg.add_entity("Max", entity_type="person")
    kg.add_entity("swimming", entity_type="activity")
    kg.add_entity("chess", entity_type="activity")

    kg.add_triple("Alice", "parent_of", "Max", valid_from="2015-04-01")
    kg.add_triple("Max", "does", "swimming", valid_from="2025-01-01")
    kg.add_triple("Max", "does", "chess", valid_from="2024-06-01")
    kg.add_triple("Alice", "works_at", "Acme Corp", valid_from="2020-01-01", valid_to="2024-12-31")
    kg.add_triple("Alice", "works_at", "NewCo", valid_from="2025-01-01")

    return kg
