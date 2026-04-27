"""test_recovery_wal_drainer.py — Wave 3 drainer tests for the recovery WAL.

The drainer replays JSONL WAL files (written by ``_async_save_worker`` on
lock timeout) back into the palace. These tests pin the contract:

- Empty / fresh palaces drain to a zero-result instead of raising.
- Files apply oldest-first so the temporal order of saves is preserved.
- Successful files are deleted; failed files are left on disk for retry.
- Per-file failures never cascade — the drainer keeps going.
- Malformed JSONL lines are skipped (documented behavior — losing 99
  valid records to one corrupt line would defeat the WAL's purpose).
- Idempotent: a re-drain after re-creating the same WAL files behaves
  identically (no state leaked across runs).
- ``_async_save_worker`` drains pending WAL files BEFORE processing the
  new payload, and a drain failure does not block the new payload.
- The CLI command lists / drains / dry-runs as documented.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mempalace import hooks_cli, recovery_wal


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def isolated_palace(tmp_path, monkeypatch):
    """Point MempalaceConfig.palace_path at a fresh temp dir."""
    palace = tmp_path / "palace"
    palace.mkdir()
    monkeypatch.setenv("MEMPAL_PALACE_PATH", str(palace))
    return str(palace)


def _write_wal_file(
    palace_path: str,
    records: list[dict],
    *,
    pid: int = 12345,
    mtime: float | None = None,
) -> Path:
    """Write one WAL file with ``records`` and optionally backdate its mtime."""
    path = recovery_wal.persist_records(palace_path, records, pid=pid)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _diary_record(text: str = "verbatim diary text") -> dict:
    return {
        "op": "diary",
        "args": {
            "id": f"diary_test_{abs(hash(text)) % 10_000_000}",
            "document": text,
            "metadata": {"wing": "test", "room": "diary", "type": "diary_entry"},
        },
    }


def _drawer_record(content: str, wing: str = "test", room: str = "general") -> dict:
    return {
        "op": "drawer",
        "args": {
            "id": f"drawer_{wing}_{room}_{abs(hash(content)) % 10_000_000}",
            "document": content,
            "metadata": {"wing": wing, "room": room, "added_by": "test"},
        },
    }


# ── Drainer unit tests ────────────────────────────────────────────────


def test_drain_empty_directory_returns_zero_processed(tmp_path):
    """A palace with no WAL files drains to a zero-result, never raises."""
    palace = tmp_path / "fresh_palace"
    palace.mkdir()
    apply_calls: list[list[dict]] = []

    result = recovery_wal.drain_recovery_wal(
        palace, apply_records=lambda recs: apply_calls.append(recs)
    )

    assert result.files_processed == 0
    assert result.files_failed == 0
    assert result.records_replayed == 0
    assert result.failures == []
    assert apply_calls == []
    assert result.duration_seconds >= 0.0


def test_drain_replays_records_in_order(tmp_path):
    """Multiple WAL files apply oldest-first (mtime ascending)."""
    palace = tmp_path / "ordered_palace"
    palace.mkdir()

    base = time.time() - 3600
    p_old = _write_wal_file(str(palace), [_diary_record("oldest")], pid=1, mtime=base)
    p_mid = _write_wal_file(str(palace), [_diary_record("middle")], pid=2, mtime=base + 60)
    p_new = _write_wal_file(str(palace), [_diary_record("newest")], pid=3, mtime=base + 120)

    seen_diary_ids: list[str] = []

    def _apply(records):
        for rec in records:
            if rec["op"] == "diary":
                seen_diary_ids.append(rec["args"]["document"])

    result = recovery_wal.drain_recovery_wal(palace, apply_records=_apply)

    assert seen_diary_ids == ["oldest", "middle", "newest"]
    assert result.files_processed == 3
    assert result.records_replayed == 3
    # All files removed.
    for p in (p_old, p_mid, p_new):
        assert not p.exists(), f"{p} should have been deleted after successful drain"


def test_drain_deletes_files_on_success(tmp_path):
    """Successful drain removes the WAL files."""
    palace = tmp_path / "delete_palace"
    palace.mkdir()
    p1 = _write_wal_file(str(palace), [_diary_record("a")], pid=10)
    p2 = _write_wal_file(str(palace), [_drawer_record("b")], pid=11)
    assert p1.exists() and p2.exists()

    recovery_wal.drain_recovery_wal(palace, apply_records=lambda recs: None)

    assert not p1.exists()
    assert not p2.exists()


def test_drain_leaves_files_on_failure(tmp_path):
    """When apply_records raises, the WAL file stays on disk and is recorded as failed."""
    palace = tmp_path / "fail_palace"
    palace.mkdir()
    p = _write_wal_file(str(palace), [_diary_record("not-applied")], pid=99)

    def _bad_apply(_recs):
        raise RuntimeError("simulated chroma down")

    result = recovery_wal.drain_recovery_wal(palace, apply_records=_bad_apply)

    assert p.exists(), "WAL file must NOT be deleted when apply_records fails"
    assert result.files_failed == 1
    assert result.files_processed == 0
    assert result.records_replayed == 0
    assert len(result.failures) == 1
    failing_path, err_msg = result.failures[0]
    assert failing_path == p
    assert "simulated chroma down" in err_msg


def test_drain_continues_after_per_file_failure(tmp_path):
    """One bad file does not block the rest."""
    palace = tmp_path / "mixed_palace"
    palace.mkdir()

    base = time.time() - 600
    p_first = _write_wal_file(str(palace), [_diary_record("first-applied")], pid=21, mtime=base)
    p_bad = _write_wal_file(str(palace), [_diary_record("blows-up")], pid=22, mtime=base + 30)
    p_third = _write_wal_file(
        str(palace), [_diary_record("third-applied")], pid=23, mtime=base + 60
    )

    applied_documents: list[str] = []

    def _apply(records):
        for rec in records:
            doc = rec["args"]["document"]
            if doc == "blows-up":
                raise RuntimeError("simulated mid-file error")
            applied_documents.append(doc)

    result = recovery_wal.drain_recovery_wal(palace, apply_records=_apply)

    assert applied_documents == ["first-applied", "third-applied"]
    assert result.files_processed == 2
    assert result.files_failed == 1
    assert not p_first.exists()
    assert p_bad.exists(), "failed file must remain on disk"
    assert not p_third.exists()


def test_drain_idempotent(tmp_path):
    """Running drain twice over equivalent WAL files behaves the same."""
    palace = tmp_path / "idem_palace"
    palace.mkdir()

    apply_log_pass1: list[str] = []
    apply_log_pass2: list[str] = []

    # Pass 1
    p1 = _write_wal_file(str(palace), [_diary_record("payload-1")], pid=31)
    p2 = _write_wal_file(str(palace), [_diary_record("payload-2")], pid=32)
    res1 = recovery_wal.drain_recovery_wal(
        palace,
        apply_records=lambda recs: apply_log_pass1.extend(
            r["args"]["document"] for r in recs if r["op"] == "diary"
        ),
    )
    assert res1.files_processed == 2
    assert not p1.exists() and not p2.exists()

    # Pass 2 — re-create the SAME WAL files
    _write_wal_file(str(palace), [_diary_record("payload-1")], pid=33)
    _write_wal_file(str(palace), [_diary_record("payload-2")], pid=34)
    res2 = recovery_wal.drain_recovery_wal(
        palace,
        apply_records=lambda recs: apply_log_pass2.extend(
            r["args"]["document"] for r in recs if r["op"] == "diary"
        ),
    )
    assert res2.files_processed == 2
    assert (
        sorted(apply_log_pass1)
        == sorted(apply_log_pass2)
        == [
            "payload-1",
            "payload-2",
        ]
    )


def test_drain_handles_corrupt_jsonl_gracefully(tmp_path):
    """Malformed lines are skipped; the rest of the file still drains.

    Behavior decision: skip the LINE, not the file. Losing 99 valid
    records to recover from one corrupt line would be a worse violation
    of the "100% recall" promise than the corrupt line itself.
    """
    palace = tmp_path / "corrupt_palace"
    palace.mkdir()
    rec_dir = recovery_wal.recovery_dir_for_palace(str(palace))
    rec_dir.mkdir(parents=True, exist_ok=True)
    target = rec_dir / "20260101-120000-000000_999.jsonl"
    good_a = json.dumps(_diary_record("good-line-1"), sort_keys=True)
    good_b = json.dumps(_drawer_record("good-line-2"), sort_keys=True)
    target.write_text(
        good_a + "\n" + "{this is not json}\n" + good_b + "\n",
        encoding="utf-8",
    )

    seen_ops: list[str] = []
    result = recovery_wal.drain_recovery_wal(
        palace,
        apply_records=lambda recs: seen_ops.extend(r["op"] for r in recs),
    )

    assert seen_ops == ["diary", "drawer"], f"corrupt line should be skipped; got {seen_ops}"
    assert result.files_processed == 1
    assert result.records_replayed == 2
    assert not target.exists()


def test_drain_without_callback_raises_typeerror(tmp_path):
    """Programmer errors (missing callback) are surfaced, not swallowed."""
    palace = tmp_path / "callback_palace"
    palace.mkdir()
    with pytest.raises(TypeError):
        recovery_wal.drain_recovery_wal(palace, apply_records=None)


def test_list_pending_returns_empty_for_missing_dir(tmp_path):
    """A palace that has never timed out has no recovery dir — must not raise."""
    palace = tmp_path / "never_timed_out"
    palace.mkdir()
    assert recovery_wal.list_pending(palace) == []


def test_list_pending_returns_oldest_first(tmp_path):
    """``list_pending`` orders by mtime ascending."""
    palace = tmp_path / "list_palace"
    palace.mkdir()
    base = time.time() - 7200
    p_oldest = _write_wal_file(str(palace), [_diary_record("oldest")], pid=41, mtime=base)
    p_newest = _write_wal_file(str(palace), [_diary_record("newest")], pid=42, mtime=base + 3600)
    p_middle = _write_wal_file(str(palace), [_diary_record("middle")], pid=43, mtime=base + 1800)

    files = recovery_wal.list_pending(str(palace))
    assert files == [p_oldest, p_middle, p_newest]


# ── Worker integration tests ──────────────────────────────────────────


@pytest.fixture
def patched_llm_no_payload(monkeypatch):
    """Stub the recall LLM to return an empty extraction.

    With no LLM payload, ``_async_save_worker`` bails out BEFORE its
    write block, but only after the drain pre-flight. We use this in
    tests that want the drain to run without pulling new payload writes
    into the picture.
    """
    monkeypatch.setattr(
        "mempalace.recall_llm._get_llm_config",
        lambda: {"endpoint": "http://stub", "model": "stub"},
    )
    monkeypatch.setattr(
        "mempalace.recall_llm._call_llm",
        lambda *a, **kw: json.dumps({"diary": "", "drawers": [], "kg": [], "tunnels": []}),
    )
    monkeypatch.setattr(hooks_cli, "_build_palace_context", lambda: "")


@pytest.fixture
def patched_llm_with_payload(monkeypatch):
    """Stub the LLM to return a small but actionable payload."""
    payload = {
        "diary": "session diary entry " + ("x" * 30),
        "drawers": [
            {
                "wing": "test_wing",
                "room": "test_room",
                "content": "drawer content " + ("y" * 30),
            }
        ],
        "kg": [],
        "tunnels": [],
    }
    monkeypatch.setattr(
        "mempalace.recall_llm._get_llm_config",
        lambda: {"endpoint": "http://stub", "model": "stub"},
    )
    monkeypatch.setattr(
        "mempalace.recall_llm._call_llm",
        lambda *a, **kw: json.dumps(payload),
    )
    monkeypatch.setattr(hooks_cli, "_build_palace_context", lambda: "")
    return payload


def test_async_save_worker_drains_before_processing_new_payload(
    monkeypatch, isolated_palace, patched_llm_with_payload
):
    """The worker must drain pending WAL records before applying its own payload."""
    # Plant a WAL file so the drain has something to do.
    pending_doc = "pending-recovery-drawer " + ("z" * 50)
    _write_wal_file(
        isolated_palace,
        [_drawer_record(pending_doc, wing="recovery_wing", room="recovery_room")],
        pid=55,
    )
    rec_dir = recovery_wal.recovery_dir_for_palace(isolated_palace)
    assert len(list(rec_dir.glob("*.jsonl"))) == 1

    hooks_cli._async_save_worker(
        transcript_text="session content for the LLM",
        session_id="drain-then-write",
        cwd="/tmp/drainproject",
    )

    # 1. The recovery file must be gone.
    assert list(rec_dir.glob("*.jsonl")) == [], "drainer should have removed the WAL file"

    # 2. Both the drained drawer AND the new payload's drawer must exist.
    from mempalace.palace import get_collection

    col = get_collection(isolated_palace, create=True)
    all_docs = col.get(include=["documents", "metadatas"])
    documents = all_docs["documents"]
    assert any(pending_doc in d for d in documents), (
        f"drained drawer missing from palace; have docs: {documents}"
    )
    assert any("drawer content" in d for d in documents), "new-payload drawer missing from palace"


def test_async_save_worker_drain_failure_does_not_block_new_payload(
    monkeypatch, isolated_palace, patched_llm_with_payload
):
    """If the drainer raises, the new payload must STILL get processed."""

    # Force the drainer to blow up.
    def _boom(*_a, **_kw):
        raise RuntimeError("simulated drainer failure")

    monkeypatch.setattr("mempalace.recovery_wal.drain_recovery_wal", _boom)

    hooks_cli._async_save_worker(
        transcript_text="session content for the LLM",
        session_id="drain-fails-but-payload-runs",
        cwd="/tmp/drainfailproject",
    )

    # The new-payload drawer must still have landed.
    from mempalace.palace import get_collection

    col = get_collection(isolated_palace, create=True)
    docs = col.get(include=["documents"])["documents"]
    assert any("drawer content" in d for d in docs), (
        f"new payload drawer missing despite drain failure; docs={docs}"
    )


def test_async_save_worker_drain_with_corrupt_wal_does_not_block_payload(
    monkeypatch, isolated_palace, patched_llm_with_payload
):
    """A corrupt WAL file must not stop the new payload from being written."""
    rec_dir = recovery_wal.recovery_dir_for_palace(isolated_palace)
    rec_dir.mkdir(parents=True, exist_ok=True)
    bad = rec_dir / "20260101-000000-000000_77.jsonl"
    bad.write_text("{not valid jsonl\n", encoding="utf-8")

    hooks_cli._async_save_worker(
        transcript_text="session content for the LLM",
        session_id="drain-corrupt-wal",
        cwd="/tmp/draincorruptproject",
    )

    # New payload must have landed.
    from mempalace.palace import get_collection

    col = get_collection(isolated_palace, create=True)
    docs = col.get(include=["documents"])["documents"]
    assert any("drawer content" in d for d in docs)


# ── CLI tests ─────────────────────────────────────────────────────────


def _run_cli(*cli_args, cwd=None, env_override=None) -> subprocess.CompletedProcess:
    """Invoke ``python -m mempalace.cli ...`` as a subprocess."""
    env = os.environ.copy()
    if env_override:
        env.update(env_override)
    return subprocess.run(
        [sys.executable, "-m", "mempalace.cli", *cli_args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_drain_recovery_cli_command(tmp_path):
    """`mempal drain-recovery --palace PATH` drains the WAL files."""
    palace = tmp_path / "cli_palace"
    palace.mkdir()
    p = _write_wal_file(
        str(palace),
        [
            _diary_record("cli-diary " + ("d" * 30)),
            _drawer_record("cli-drawer " + ("e" * 30), wing="cli_wing", room="cli_room"),
        ],
        pid=88,
    )
    assert p.exists()

    result = _run_cli(
        "--palace",
        str(palace),
        "drain-recovery",
        env_override={"MEMPAL_PALACE_PATH": str(palace)},
    )

    assert result.returncode == 0, f"CLI failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "MemPalace Recovery Drain" in result.stdout
    assert "Files processed: 1" in result.stdout
    assert "Records:         2" in result.stdout
    # File must be gone.
    assert not p.exists()


def test_drain_recovery_cli_dry_run(tmp_path):
    """`mempal drain-recovery --dry-run` lists files without applying them."""
    palace = tmp_path / "cli_dry_palace"
    palace.mkdir()
    p = _write_wal_file(
        str(palace),
        [
            _diary_record("dry-run-diary " + ("f" * 20)),
            _drawer_record("dry-run-drawer " + ("g" * 20), wing="dry_wing", room="dry_room"),
        ],
        pid=89,
    )

    result = _run_cli(
        "--palace",
        str(palace),
        "drain-recovery",
        "--dry-run",
        env_override={"MEMPAL_PALACE_PATH": str(palace)},
    )

    assert result.returncode == 0, f"CLI failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "DRY RUN" in result.stdout
    assert "diary=1" in result.stdout
    assert "drawer=1" in result.stdout
    # File MUST still be on disk.
    assert p.exists(), "dry run must not delete the WAL file"


def test_drain_recovery_cli_missing_palace(tmp_path):
    """Missing palace dir yields exit code 2."""
    missing = tmp_path / "no_such_palace"
    result = _run_cli(
        "--palace",
        str(missing),
        "drain-recovery",
        env_override={"MEMPAL_PALACE_PATH": str(missing)},
    )
    assert result.returncode == 2


def test_drain_recovery_cli_empty(tmp_path):
    """Empty palace returns 0 with 'Nothing to drain' message."""
    palace = tmp_path / "empty_cli_palace"
    palace.mkdir()
    result = _run_cli(
        "--palace",
        str(palace),
        "drain-recovery",
        env_override={"MEMPAL_PALACE_PATH": str(palace)},
    )
    assert result.returncode == 0
    assert "Nothing to drain" in result.stdout


# ── Misc internals ────────────────────────────────────────────────────


def test_drain_result_is_immutable(tmp_path):
    """``DrainResult`` is frozen so callers cannot mutate the report."""
    palace = tmp_path / "immutable_palace"
    palace.mkdir()
    res = recovery_wal.drain_recovery_wal(palace, apply_records=lambda _r: None)
    with pytest.raises(Exception):
        # frozen dataclass — assignment must fail
        res.files_processed = 999  # type: ignore[misc]


def test_replay_recovery_records_dispatches_by_op(tmp_path, isolated_palace):
    """``_replay_recovery_records`` splits records into the right buckets."""
    from datetime import datetime as _dt

    captured_kg: list[list[dict]] = []
    captured_tunnels: list[list[dict]] = []
    captured_pairs: list[list] = []

    def _fake_kg(facts, _now):
        captured_kg.append(facts)
        return len(facts)

    def _fake_tun(tuns):
        captured_tunnels.append(tuns)
        return len(tuns)

    def _fake_auto(pairs, _col):
        captured_pairs.append(pairs)
        return 0

    fake_col = MagicMock()

    with contextlib.ExitStack() as stack:
        import unittest.mock

        stack.enter_context(unittest.mock.patch.object(hooks_cli, "_async_save_apply_kg", _fake_kg))
        stack.enter_context(
            unittest.mock.patch.object(hooks_cli, "_async_save_apply_tunnels", _fake_tun)
        )
        stack.enter_context(
            unittest.mock.patch.object(hooks_cli, "_async_save_apply_auto_tunnels", _fake_auto)
        )

        records = [
            {"op": "context", "args": {"session_id": "s"}},
            _diary_record("hello world"),
            _drawer_record("draw 1", wing="w1", room="r1"),
            _drawer_record("draw 2", wing="w2", room="r2"),
            {"op": "kg_triple", "args": {"subject": "A", "predicate": "p", "object": "B"}},
            {
                "op": "tunnel",
                "args": {
                    "source_wing": "w1",
                    "source_room": "r1",
                    "target_wing": "w2",
                    "target_room": "r2",
                    "label": "L",
                },
            },
            {"op": "empty", "args": {}},
            {"op": "totally_unknown", "args": {}},
        ]

        hooks_cli._replay_recovery_records(records, fake_col, _dt.now())

    # Diary and drawers go straight to col.
    assert fake_col.add.called, "diary should have been added"
    assert fake_col.upsert.call_count == 2, "two drawers should have been upserted"
    # KG, tunnels, auto-tunnels routed through helpers.
    assert captured_kg == [[{"subject": "A", "predicate": "p", "object": "B"}]]
    assert len(captured_tunnels) == 1 and len(captured_tunnels[0]) == 1
    assert captured_pairs == [[("w1", "r1"), ("w2", "r2")]]
