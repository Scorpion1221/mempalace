#!/usr/bin/env python3
"""Debug keyword_recall contribution on LoCoMo conv 0.

Builds the palace the same way locomo.py does, then for each QA pair:
  - Run pure cosine query, log the cosine IDs
  - Run _keyword_recall with exclude_ids=cosine_pool, log what's returned
  - Check whether the evidence session IDs are in cosine_pool / keyword / neither
"""

import importlib.util
import json
import os
import sys
import shutil
import tempfile
from collections import Counter
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BENCH_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_BENCH_DIR))

# Force baseline embedding (all-MiniLM) — no LiteLLM proxy.
os.environ["MEMPAL_EMBEDDING_MODEL"] = "default"
os.environ.pop("MEMPAL_EMBEDDING_ENDPOINT", None)
os.environ.pop("MEMPAL_EMBEDDING_KEY", None)


def _load_locomo_bench():
    bench_path = _BENCH_DIR / "locomo_bench.py"
    spec = importlib.util.spec_from_file_location("locomo_bench", bench_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["locomo_bench"] = mod
    spec.loader.exec_module(mod)
    return mod


_lb = _load_locomo_bench()

from mempalace.miner import add_drawer
from mempalace.palace import get_collection
from mempalace.searcher import (
    _first_or_empty,
    _keyword_recall,
    _tokenize,
)


def main():
    data_file = sys.argv[1] if len(sys.argv) > 1 else "/tmp/locomo/data/locomo10.json"
    conv_idx = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    sample_limit = int(sys.argv[3]) if len(sys.argv) > 3 else 0  # 0 = all conversations

    data = json.load(open(data_file))

    aggregate_counts = Counter()
    aggregate_kw_rescues = 0   # evidence found by keyword but not cosine top-30
    aggregate_kw_adds = 0      # total non-empty keyword_recall returns
    cosine_already_has_evidence = 0
    cosine_missing_evidence = 0
    total = 0

    conversations_to_run = [conv_idx] if sample_limit == 0 else list(range(min(sample_limit, len(data))))

    for ci in conversations_to_run:
        sample = data[ci]
        sample_id = sample.get("sample_id", f"conv-{ci}")
        conversation = sample["conversation"]
        qa_pairs = sample["qa"]
        session_summaries = sample.get("session_summary", {})
        sessions = _lb.load_conversation_sessions(conversation, session_summaries)
        corpus, corpus_ids, _timestamps = _lb.build_corpus_from_sessions(sessions, granularity="session")
        if not corpus:
            continue

        print(f"\n=== Conversation {sample_id}: {len(corpus)} sessions, {len(qa_pairs)} QA ===")

        palace_dir = tempfile.mkdtemp(prefix=f"dbg_{sample_id[:8]}_")
        try:
            col = get_collection(palace_dir, create=True)
            for content, cid in zip(corpus, corpus_ids):
                add_drawer(col, "bench", "bench", content, cid, 0, "bench")

            per_query_fetch = max(30 * 3, 50)   # mirror locomo.py's n_results=max(top_k*3, 50)

            for qa_idx, qa in enumerate(qa_pairs):
                total += 1
                query = qa["question"]
                evidence = qa.get("evidence", [])
                evidence_session_ids = _lb.evidence_to_session_ids(evidence)

                # Step 1: pure cosine
                qres = col.query(
                    query_texts=[query],
                    n_results=per_query_fetch,
                    include=["documents", "metadatas", "distances"],
                )
                cos_ids = _first_or_empty(qres, "ids")  # these are the internal chroma ids
                cos_metas = _first_or_empty(qres, "metadatas")
                cos_source_files = [(m or {}).get("source_file", "") for m in cos_metas]

                # Step 2: keyword_recall with exclude_ids = cosine chroma-ids
                kw_hits = _keyword_recall(col, query, where=None, exclude_ids=set(cos_ids), limit=30 * 3)
                kw_sources = [(m or {}).get("source_file", "") for _, m, _ in kw_hits]

                # Did cosine already have the evidence sessions?
                evidence_in_cosine_top30 = evidence_session_ids & set(cos_source_files[:30])
                evidence_in_cosine_pool = evidence_session_ids & set(cos_source_files)
                evidence_in_kw = evidence_session_ids & set(kw_sources)
                evidence_in_neither = evidence_session_ids - set(cos_source_files) - set(kw_sources)

                if kw_hits:
                    aggregate_kw_adds += 1
                    aggregate_counts["kw_returned_something"] += 1
                    # Is this a RESCUE? i.e. was evidence missing from cosine top-30?
                    missing_from_cosine_top30 = evidence_session_ids - set(cos_source_files[:30])
                    rescued = missing_from_cosine_top30 & set(kw_sources)
                    if rescued:
                        aggregate_kw_rescues += 1
                        aggregate_counts["kw_rescued_evidence"] += 1
                        print(
                            f"  [{qa_idx}] RESCUE — Q={query[:60]!r}\n"
                            f"       evidence={sorted(evidence_session_ids)}  "
                            f"rescued={sorted(rescued)}"
                        )

                # Track whether cosine already had the evidence
                if evidence_session_ids and evidence_in_cosine_top30:
                    cosine_already_has_evidence += 1
                elif evidence_session_ids and not evidence_in_cosine_top30:
                    cosine_missing_evidence += 1
                    # Peek: does keyword_recall even find any cosine-top30-miss?
                    # Note: the exclude_ids is the full cosine POOL (all 50), not top-30.
                    # So kw_hits can never include docs that cosine already knows about.
                    if not kw_hits:
                        aggregate_counts["cosine_miss_kw_empty"] += 1
                    else:
                        aggregate_counts["cosine_miss_kw_has_results_but_not_evidence"] += 1

        finally:
            shutil.rmtree(palace_dir, ignore_errors=True)

    print(f"\n=== SUMMARY (total={total}) ===")
    print(f"  keyword_recall returned non-empty:  {aggregate_kw_adds}")
    print(f"  keyword_recall rescued evidence:    {aggregate_kw_rescues}")
    print(f"  cosine top-30 already had evidence: {cosine_already_has_evidence}")
    print(f"  cosine top-30 MISSED evidence:      {cosine_missing_evidence}")
    print(f"\n  breakdown:")
    for k, v in aggregate_counts.most_common():
        print(f"    {k}: {v}")


if __name__ == "__main__":
    main()
