"""Tests for ChromaBackend write-path cache invalidation (Layer 2 of the concurrency fix).

The bug under test: when two MCP server processes share a palace, each holds its
own in-memory HNSW state in cached ``chromadb.PersistentClient`` instances. After
process A writes (landing on disk), process B's cached client doesn't know — so
process B's next write persists its own (stale) memory state and overwrites A's
work. ``ChromaBackend._client_for_write`` re-stats ``chroma.sqlite3`` on every
write-path call so the cache is rebuilt when an external writer changed the file.

These tests cover the in-process cache mechanics. The cross-process race
between processes is enforced by Layer 1 (``palace_write_lock``) and exercised
by ``tests/test_palace_write_lock.py``.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import chromadb

from mempalace.backends import PalaceRef
from mempalace.backends.chroma import ChromaBackend, ChromaCollection


# ── Helpers ────────────────────────────────────────────────────────────


def _seed_palace(palace_path: Path) -> None:
    """Create a real chromadb palace with the standard mempalace collection.

    Uses a throwaway ``PersistentClient`` so the on-disk layout matches what
    the production code expects (``chroma.sqlite3`` + segment dirs).
    """
    palace_path.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(palace_path))
    client.get_or_create_collection("mempalace_drawers", metadata={"hnsw:space": "cosine"})
    # Drop the throwaway client; it is not part of any backend cache.
    del client


def _bump_db_mtime(db_path: Path, *, seconds: float = 2.0) -> None:
    """Force the chroma.sqlite3 stat tuple to differ from any prior reading.

    Bumping mtime a couple of seconds beyond ``time.time()`` guarantees the
    ``(size, mtime_ns, inode)`` tuple changes — even on filesystems with
    coarse mtime granularity (HFS+, FAT) and even within the same nanosecond
    bucket as the previous stat.
    """
    future = time.time() + seconds
    os.utime(db_path, (future, future))


# ── Tests ──────────────────────────────────────────────────────────────


def test_write_client_rebuilt_after_external_mtime_change(tmp_path):
    """An external mtime bump must force ``_client_for_write`` to rebuild."""
    palace = tmp_path / "palace"
    _seed_palace(palace)
    db_file = palace / "chroma.sqlite3"
    assert db_file.is_file()

    backend = ChromaBackend()
    first_client = backend._client_for_write(str(palace))
    first_stat = backend._write_freshness[str(palace)]
    assert first_stat is not None

    # Simulate another process modifying the on-disk DB while we were idle.
    _bump_db_mtime(db_file, seconds=2.0)
    new_stat_on_disk = ChromaBackend._db_stat_full(str(palace))
    assert new_stat_on_disk != first_stat, "test setup failed: stat did not change on disk"

    second_client = backend._client_for_write(str(palace))

    # Cache must have been re-evaluated against the new stat. The client
    # identity may or may not differ (chromadb may return the same singleton
    # internally), but the freshness tuple MUST reflect the post-rebuild stat,
    # and any cached state must have been replaced.
    assert backend._write_freshness[str(palace)] != first_stat
    assert backend._write_freshness[str(palace)] == ChromaBackend._db_stat_full(str(palace))
    # Strong assertion: a different client object was constructed (the cached
    # one was evicted before rebuild).
    assert second_client is not first_client


def test_write_client_reused_when_no_change(tmp_path):
    """Two consecutive write-path calls with no on-disk change reuse the client."""
    palace = tmp_path / "palace"
    _seed_palace(palace)

    backend = ChromaBackend()
    first = backend._client_for_write(str(palace))
    first_stat = backend._write_freshness[str(palace)]

    # No external change between calls — ``_client_for_write`` must return
    # the same cached PersistentClient instance.
    second = backend._client_for_write(str(palace))

    assert second is first, "client should be reused when on-disk stat is unchanged"
    assert backend._write_freshness[str(palace)] == first_stat


def test_read_client_unchanged_by_write_path(tmp_path):
    """A read-path ``_client()`` call must not perturb write-path freshness tracking."""
    palace = tmp_path / "palace"
    _seed_palace(palace)

    backend = ChromaBackend()

    # Prime the write path so ``_write_freshness`` is populated.
    backend._client_for_write(str(palace))
    write_stat_before_read = backend._write_freshness[str(palace)]
    assert write_stat_before_read is not None

    # The read path should not touch ``_write_freshness`` regardless of how
    # many times it is invoked.
    for _ in range(3):
        backend._client(str(palace))

    assert backend._write_freshness[str(palace)] == write_stat_before_read, (
        "read path mutated write-freshness tracking"
    )


def test_refresh_for_write_picks_up_external_writes(tmp_path):
    """Backend A must observe a write made by Backend B after refresh_for_write.

    This simulates two processes sharing the same palace: Backend B writes
    a doc through its own cached client. Backend A then calls
    ``refresh_for_write`` on the collection it obtained earlier — the next
    read on the underlying chromadb collection must include B's doc.
    """
    palace = tmp_path / "palace"
    _seed_palace(palace)

    backend_a = ChromaBackend()
    backend_b = ChromaBackend()

    palace_ref = PalaceRef(id=str(palace), local_path=str(palace))

    # A and B each obtain their own ChromaCollection wrapping their own
    # cached client.
    collection_a = backend_a.get_collection(
        palace=palace_ref, collection_name="mempalace_drawers", create=True
    )
    collection_b = backend_b.get_collection(
        palace=palace_ref, collection_name="mempalace_drawers", create=True
    )

    # Baseline — both empty.
    assert collection_a.count() == 0
    assert collection_b.count() == 0

    # B writes a doc through its own backend's cached client.
    collection_b.add(
        documents=["hello from B"],
        ids=["doc-b-1"],
        metadatas=[{"wing": "test", "room": "b"}],
        embeddings=[[0.1, 0.2, 0.3]],
    )

    # Force chroma to flush by closing B's backend (drops B's strong refs to
    # the client/collection, which triggers ChromaDB's HNSW persistence on
    # garbage collection).
    backend_b.close()

    # Bump mtime so A's stat-based invalidation definitely fires even on
    # filesystems that round mtime to coarser units than chromadb's writes.
    _bump_db_mtime(palace / "chroma.sqlite3", seconds=1.0)

    # A's cached collection has not seen B's write yet (its underlying client
    # has stale in-memory state).  Refresh, then count.
    collection_a.refresh_for_write()
    assert collection_a.count() == 1, (
        "after refresh_for_write, backend A should see backend B's write"
    )

    # Sanity: the doc retrieved through A is exactly B's payload.
    result = collection_a.get(ids=["doc-b-1"])
    assert result.ids == ["doc-b-1"]
    assert result.documents == ["hello from B"]


def test_within_process_thread_safety(tmp_path):
    """Hammer ``_client_for_write`` from many threads — no exceptions, dicts intact."""
    palace = tmp_path / "palace"
    _seed_palace(palace)

    backend = ChromaBackend()
    palace_str = str(palace)

    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker():
        try:
            barrier.wait()
            for _ in range(20):
                client = backend._client_for_write(palace_str)
                assert client is not None
        except BaseException as exc:  # pragma: no cover - surfaced via errors list
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
        assert not t.is_alive(), "thread hung"

    assert errors == [], f"thread workers raised: {errors!r}"

    # Caches must remain self-consistent: exactly one entry per palace, and
    # the cached client matches the one ``_client_for_write`` returns now.
    assert list(backend._clients.keys()) == [palace_str]
    assert palace_str in backend._write_freshness
    assert backend._client_for_write(palace_str) is backend._clients[palace_str]


def test_client_for_write_creates_first_open(tmp_path):
    """``_client_for_write`` must successfully create a client on first call.

    Covers the cold-cache path where no prior client exists and the on-disk
    DB may or may not exist yet (chromadb creates it during PersistentClient
    construction).
    """
    palace = tmp_path / "palace"
    palace.mkdir()
    # Note: no _seed_palace — chroma.sqlite3 does not yet exist.

    backend = ChromaBackend()
    client = backend._client_for_write(str(palace))
    assert client is not None

    # After construction chromadb has created the DB file.
    assert (palace / "chroma.sqlite3").is_file()
    # And freshness is populated.
    assert backend._write_freshness[str(palace)] is not None


def test_client_for_write_after_close_raises(tmp_path):
    """Calling ``_client_for_write`` after ``close()`` raises ``BackendClosedError``."""
    from mempalace.backends.base import BackendClosedError

    palace = tmp_path / "palace"
    _seed_palace(palace)

    backend = ChromaBackend()
    backend._client_for_write(str(palace))
    backend.close()

    try:
        backend._client_for_write(str(palace))
    except BackendClosedError:
        pass
    else:  # pragma: no cover - assertion path
        raise AssertionError("expected BackendClosedError after backend.close()")


def test_refresh_for_write_no_op_when_no_backend_context(tmp_path):
    """``refresh_for_write`` is a silent no-op when collection lacks backend wiring.

    Unit tests construct ``ChromaCollection`` directly with a fake collection;
    those callers should not crash when invoking refresh_for_write.
    """

    class _FakeCollection:
        def count(self):
            return 0

    collection = ChromaCollection(_FakeCollection())
    # Should not raise.
    collection.refresh_for_write()
    assert collection.count() == 0


def test_db_stat_full_returns_none_when_missing(tmp_path):
    """``_db_stat_full`` returns ``None`` rather than raising when the DB is absent."""
    palace = tmp_path / "no_palace_here"
    palace.mkdir()
    assert ChromaBackend._db_stat_full(str(palace)) is None


def test_close_palace_clears_write_freshness(tmp_path):
    """``close_palace`` must drop the write-freshness tuple along with other caches."""
    palace = tmp_path / "palace"
    _seed_palace(palace)

    backend = ChromaBackend()
    backend._client_for_write(str(palace))
    assert str(palace) in backend._write_freshness

    backend.close_palace(PalaceRef(id=str(palace), local_path=str(palace)))

    assert str(palace) not in backend._write_freshness
    assert str(palace) not in backend._clients
    assert str(palace) not in backend._freshness
