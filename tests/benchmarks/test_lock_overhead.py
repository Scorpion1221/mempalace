"""Lock overhead benchmark for ``palace_write_lock``.

Measures the cost of the Wave 1+2 concurrency fix so we know what we're
paying. The benchmark runs four scenarios and prints a single table at
the end. None of these scenarios fail on regression — this is a
benchmark, not a regression gate.

Scenarios:

1. Single-writer baseline      — 1000 writes WITHOUT the lock (raw ChromaDB).
2. Single-writer with lock     — 1000 writes WITH the lock (uncontended).
                                  Reports overhead percent.
3. 6-way contended throughput  — 6 writers x 1000 writes each, all
                                  contending for the same palace lock.
                                  Reports total throughput and per-writer
                                  p50/p99 latency.
4. Per-write overhead          — Lock acquisition + refresh_for_write
                                  cost on a hot palace, p50 / p99.

Run command:

    python -m pytest tests/benchmarks/test_lock_overhead.py -v -s

The ``-s`` flag is important — pytest captures stdout by default, but
this benchmark prints its summary table at the end and you need it on
your terminal.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import statistics
import time
from pathlib import Path

import pytest


pytestmark = pytest.mark.benchmark


# ── Fixed-embedding helper ────────────────────────────────────────────
# Bypassing the embedding function avoids a 79 MB ONNX model download on
# fresh palaces and keeps the per-write cost dominated by the operations
# under measurement (lock, refresh, upsert).

_FIXED_EMBED = [0.1] * 384

_MP_CTX = mp.get_context("spawn")


def _make_collection(palace_path: str):
    """Build a chromadb collection with no embedding fn for fast bench."""
    from mempalace.backends.chroma import ChromaBackend

    backend = ChromaBackend()
    return backend.get_collection(palace_path, "mempalace_drawers", create=True)


def _build_record(idx: int) -> dict:
    return {
        "ids": [f"bench_{idx}"],
        "documents": [f"benchmark drawer index {idx} with stable text"],
        "metadatas": [
            {
                "wing": "bench",
                "room": "lock_overhead",
                "added_by": "benchmark",
                "write_index": idx,
            }
        ],
        "embeddings": [_FIXED_EMBED],
    }


# ── Subprocess target for contended benchmark ─────────────────────────


def _contended_writer(
    palace_path: str,
    worker_id: int,
    n_writes: int,
    result_path: str,
) -> None:
    """One worker for the 6-way contended throughput scenario.

    Records per-write wall-clock latency (lock acquisition + write) so
    we can compute p50 / p99 across the whole population.
    """
    from mempalace.palace import palace_write_lock

    col = _make_collection(palace_path)
    latencies_ms: list[float] = []
    started = time.monotonic()
    for i in range(n_writes):
        rec = _build_record(worker_id * n_writes + i)
        wall_start = time.perf_counter()
        with palace_write_lock(palace_path, timeout=120.0):
            col.refresh_for_write()
            col.upsert(**rec)
        latencies_ms.append((time.perf_counter() - wall_start) * 1000.0)
    duration_s = time.monotonic() - started
    Path(result_path).write_text(
        json.dumps(
            {
                "worker_id": worker_id,
                "duration_s": duration_s,
                "latencies_ms": latencies_ms,
            }
        )
    )


# ── Reporting helpers ─────────────────────────────────────────────────


def _percentile(samples: list[float], p: float) -> float:
    if not samples:
        return 0.0
    s = sorted(samples)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return s[k]


def _format_table(rows: list[tuple[str, str]]) -> str:
    """Emit a two-column table with right-aligned values."""
    label_w = max(len(label) for label, _ in rows)
    value_w = max(len(value) for _, value in rows)
    out_lines = []
    sep = "─" * (label_w + value_w + 7)
    out_lines.append(sep)
    out_lines.append(f"  {'metric':<{label_w}}  │  {'value':>{value_w}}")
    out_lines.append(sep)
    for label, value in rows:
        out_lines.append(f"  {label:<{label_w}}  │  {value:>{value_w}}")
    out_lines.append(sep)
    return "\n".join(out_lines)


# ── Tests ─────────────────────────────────────────────────────────────


N_WRITES_BASELINE = 1000
N_WRITES_CONTENDED = 1000
N_CONTENDED_WORKERS = 6
N_PER_WRITE_OVERHEAD_SAMPLES = 500


def test_single_writer_baseline_no_lock(tmp_path, bench_results):
    """1000 writes WITHOUT palace_write_lock — raw chromadb throughput."""
    palace = tmp_path / "palace_baseline"
    palace.mkdir()

    col = _make_collection(str(palace))

    started = time.monotonic()
    for i in range(N_WRITES_BASELINE):
        col.upsert(**_build_record(i))
    duration_s = time.monotonic() - started

    throughput = N_WRITES_BASELINE / duration_s
    bench_results.record(
        "lock_overhead",
        "baseline_no_lock_throughput_per_s",
        round(throughput, 2),
    )
    bench_results.record(
        "lock_overhead",
        "baseline_no_lock_duration_s",
        round(duration_s, 3),
    )


def test_single_writer_uncontended_with_lock(tmp_path, bench_results):
    """1000 writes WITH the lock — uncontended overhead."""
    from mempalace.palace import palace_write_lock

    palace = tmp_path / "palace_with_lock"
    palace.mkdir()

    col = _make_collection(str(palace))

    started = time.monotonic()
    for i in range(N_WRITES_BASELINE):
        with palace_write_lock(str(palace), timeout=10.0):
            col.refresh_for_write()
            col.upsert(**_build_record(i))
    duration_s = time.monotonic() - started

    throughput = N_WRITES_BASELINE / duration_s
    baseline_throughput = bench_results.results.get("lock_overhead", {}).get(
        "baseline_no_lock_throughput_per_s"
    )
    overhead_pct = None
    if baseline_throughput:
        overhead_pct = (1.0 - throughput / baseline_throughput) * 100.0

    bench_results.record(
        "lock_overhead",
        "uncontended_with_lock_throughput_per_s",
        round(throughput, 2),
    )
    bench_results.record(
        "lock_overhead",
        "uncontended_with_lock_duration_s",
        round(duration_s, 3),
    )
    if overhead_pct is not None:
        bench_results.record(
            "lock_overhead",
            "lock_overhead_percent_uncontended",
            round(overhead_pct, 2),
        )


def test_six_way_contended_throughput(tmp_path, bench_results):
    """6 subprocess writers, 1000 writes each, all contending one lock."""
    palace = tmp_path / "palace_contended"
    palace.mkdir()
    # Pre-create the palace so workers don't race the initial chromadb
    # CREATE TABLE on first open (a known issue documented in the Wave 3
    # review). This benchmark measures lock overhead, not init races.
    _make_collection(str(palace))

    procs = []
    result_paths: list[Path] = []
    overall_started = time.monotonic()
    for worker_id in range(N_CONTENDED_WORKERS):
        rp = tmp_path / f"contended_{worker_id}.json"
        result_paths.append(rp)
        proc = _MP_CTX.Process(
            target=_contended_writer,
            args=(str(palace), worker_id, N_WRITES_CONTENDED, str(rp)),
        )
        proc.start()
        procs.append(proc)

    for p in procs:
        p.join(timeout=600.0)
        assert not p.is_alive(), "contended writer hung"
        assert p.exitcode == 0, f"contended writer exit {p.exitcode}"

    overall_duration_s = time.monotonic() - overall_started

    # Collate per-write latency across all workers.
    all_latencies: list[float] = []
    per_worker_durations: list[float] = []
    for rp in result_paths:
        data = json.loads(rp.read_text())
        all_latencies.extend(data["latencies_ms"])
        per_worker_durations.append(data["duration_s"])

    total_writes = N_WRITES_CONTENDED * N_CONTENDED_WORKERS
    total_throughput = total_writes / overall_duration_s

    p50 = _percentile(all_latencies, 50)
    p99 = _percentile(all_latencies, 99)
    p999 = _percentile(all_latencies, 99.9)
    mean = statistics.fmean(all_latencies) if all_latencies else 0.0

    bench_results.record(
        "lock_overhead",
        "contended6_total_throughput_per_s",
        round(total_throughput, 2),
    )
    bench_results.record(
        "lock_overhead",
        "contended6_overall_duration_s",
        round(overall_duration_s, 3),
    )
    bench_results.record(
        "lock_overhead",
        "contended6_per_writer_duration_s_max",
        round(max(per_worker_durations), 3),
    )
    bench_results.record(
        "lock_overhead",
        "contended6_per_writer_duration_s_min",
        round(min(per_worker_durations), 3),
    )
    bench_results.record(
        "lock_overhead",
        "contended6_per_write_latency_ms_p50",
        round(p50, 3),
    )
    bench_results.record(
        "lock_overhead",
        "contended6_per_write_latency_ms_p99",
        round(p99, 3),
    )
    bench_results.record(
        "lock_overhead",
        "contended6_per_write_latency_ms_p999",
        round(p999, 3),
    )
    bench_results.record(
        "lock_overhead",
        "contended6_per_write_latency_ms_mean",
        round(mean, 3),
    )


def test_per_write_overhead_only(tmp_path, bench_results):
    """Cost of acquiring + releasing the lock + refresh_for_write only.

    Subtract the no-lock per-write cost from the with-lock per-write cost
    to isolate the lock + refresh overhead per individual write. Reports
    p50 and p99 of the OVERHEAD distribution (not the total per-write
    distribution).

    Target: < 5 ms p50 on a modern Mac (fcntl.flock + collection.upsert
    of a fixed-embedding row should both be sub-millisecond; the dominant
    cost is the gc.collect() invoked by refresh_for_write on cache
    eviction).
    """
    from mempalace.palace import palace_write_lock

    palace = tmp_path / "palace_overhead"
    palace.mkdir()
    col = _make_collection(str(palace))

    # Warm up: discard the first dozen samples to skip JIT/warmup noise.
    for i in range(20):
        col.upsert(**_build_record(i))

    no_lock_samples_ms: list[float] = []
    for i in range(20, 20 + N_PER_WRITE_OVERHEAD_SAMPLES):
        wall_start = time.perf_counter()
        col.upsert(**_build_record(i))
        no_lock_samples_ms.append((time.perf_counter() - wall_start) * 1000.0)

    base_idx = 20 + N_PER_WRITE_OVERHEAD_SAMPLES
    with_lock_samples_ms: list[float] = []
    for i in range(base_idx, base_idx + N_PER_WRITE_OVERHEAD_SAMPLES):
        wall_start = time.perf_counter()
        with palace_write_lock(str(palace), timeout=10.0):
            col.refresh_for_write()
            col.upsert(**_build_record(i))
        with_lock_samples_ms.append((time.perf_counter() - wall_start) * 1000.0)

    no_lock_p50 = _percentile(no_lock_samples_ms, 50)
    with_lock_p50 = _percentile(with_lock_samples_ms, 50)
    with_lock_p99 = _percentile(with_lock_samples_ms, 99)

    # Naive overhead estimate: difference between p50s. We use p50s
    # rather than per-sample subtraction because the two populations
    # were measured separately.
    overhead_p50 = max(0.0, with_lock_p50 - no_lock_p50)

    bench_results.record(
        "lock_overhead",
        "per_write_no_lock_ms_p50",
        round(no_lock_p50, 3),
    )
    bench_results.record(
        "lock_overhead",
        "per_write_with_lock_ms_p50",
        round(with_lock_p50, 3),
    )
    bench_results.record(
        "lock_overhead",
        "per_write_with_lock_ms_p99",
        round(with_lock_p99, 3),
    )
    bench_results.record(
        "lock_overhead",
        "per_write_lock_overhead_ms_p50",
        round(overhead_p50, 3),
    )


# ── End-of-suite report ───────────────────────────────────────────────


def test_zzz_print_lock_overhead_summary(bench_results):
    """Final test in the file (alphabetically last) — print the table.

    Pytest discovers tests in source order by default but we name this
    one ``zzz_`` so any reordering tooling still puts it at the end. It
    intentionally has no asserts: a benchmark that's allowed to print
    even when one of the underlying measurements is missing.
    """
    overhead = bench_results.results.get("lock_overhead", {})
    if not overhead:
        pytest.skip("no lock overhead measurements collected")

    rows: list[tuple[str, str]] = []

    def _add(label: str, value, suffix: str = ""):
        if value is None:
            rows.append((label, "n/a"))
        else:
            rows.append((label, f"{value}{suffix}"))

    _add(
        "baseline no-lock throughput",
        overhead.get("baseline_no_lock_throughput_per_s"),
        " writes/s",
    )
    _add(
        "uncontended w/lock throughput",
        overhead.get("uncontended_with_lock_throughput_per_s"),
        " writes/s",
    )
    _add(
        "uncontended overhead",
        overhead.get("lock_overhead_percent_uncontended"),
        " %",
    )
    _add(
        "6-way contended throughput",
        overhead.get("contended6_total_throughput_per_s"),
        " writes/s",
    )
    _add(
        "6-way per-write latency p50",
        overhead.get("contended6_per_write_latency_ms_p50"),
        " ms",
    )
    _add(
        "6-way per-write latency p99",
        overhead.get("contended6_per_write_latency_ms_p99"),
        " ms",
    )
    _add(
        "per-write no-lock p50",
        overhead.get("per_write_no_lock_ms_p50"),
        " ms",
    )
    _add(
        "per-write w/lock p50",
        overhead.get("per_write_with_lock_ms_p50"),
        " ms",
    )
    _add(
        "per-write w/lock p99",
        overhead.get("per_write_with_lock_ms_p99"),
        " ms",
    )
    _add(
        "per-write lock overhead p50",
        overhead.get("per_write_lock_overhead_ms_p50"),
        " ms",
    )

    table = _format_table(rows)
    print()
    print("Lock overhead benchmark summary (palace_write_lock)")
    print()
    print(table)
    print()
    # No asserts — this is a benchmark, not a regression gate.
    assert True


# ── Regression guard ──────────────────────────────────────────────────

# Hard ceiling on per-write lock overhead. Set with headroom: the H-2 fix
# (drop ``gc.collect()``, track post-write stat) brought the measured p50
# from 17.7 ms down to ~2.4 ms on Apple Silicon. We allow up to 8 ms p50
# before failing — that absorbs CI noise and slower hardware while still
# catching any future regression that would push us back into the
# pre-fix neighbourhood (16+ ms).
_PER_WRITE_OVERHEAD_REGRESSION_GUARD_MS_P50 = 8.0


def test_zzz_per_write_overhead_regression_guard(bench_results):
    """Fail the suite if per-write lock overhead p50 regresses past the ceiling.

    This is the only assertion-bearing test in this file. It runs after
    ``test_per_write_overhead_only`` populates the metric (alphabetical
    ``zzz_`` ordering keeps it last). When the H-2 perf fix is intact the
    measured p50 sits comfortably under the ceiling. A failure here means
    something landed on top of the ``_client_for_write`` /
    ``_note_post_write`` / ``ChromaCollection`` write paths that
    re-introduces the per-write client rebuild — almost always a
    regression worth investigating before merging.
    """
    overhead = bench_results.results.get("lock_overhead", {})
    p50 = overhead.get("per_write_lock_overhead_ms_p50")
    if p50 is None:
        pytest.skip("per-write overhead not measured this run")
    assert p50 < _PER_WRITE_OVERHEAD_REGRESSION_GUARD_MS_P50, (
        f"per-write lock overhead regressed: p50={p50}ms exceeds "
        f"{_PER_WRITE_OVERHEAD_REGRESSION_GUARD_MS_P50}ms ceiling. "
        "Likely cause: a change to ChromaBackend._client_for_write, "
        "_note_post_write, or ChromaCollection's write methods that "
        "re-introduces per-write client rebuilds. See H-2 in "
        "docs/CONCURRENCY_FIX_REVIEW.md for context."
    )


# ── Sanity checks ─────────────────────────────────────────────────────


def test_constants_are_sane():
    """Cheap test so the file is non-empty even without -m benchmark."""
    assert N_WRITES_BASELINE > 0
    assert N_WRITES_CONTENDED > 0
    assert N_CONTENDED_WORKERS > 0
    assert N_PER_WRITE_OVERHEAD_SAMPLES > 0
    # Make sure os/path import paths are wired right.
    assert os.path.isdir(os.path.dirname(os.path.abspath(__file__)))
