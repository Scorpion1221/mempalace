#!/usr/bin/env python3
"""
MemPalace Production × LoCoMo
=============================

Drives LoCoMo retrieval through the *actual* MemPalace production code path —
`mempalace.miner.add_drawer` for ingest, `mempalace.searcher.search_memories`
for retrieval — so we can honestly measure whether recent optimizations (BM25
hybrid rerank, keyword recall, closet boost, entity tagging, production
embedding) improve over raw ChromaDB on the LoCoMo benchmark.

Mirrors the 2×2 matrix from `lme.py` (LongMemEval):

    --embed {baseline, prod}     ChromaDB all-MiniLM  vs. our LiteLLM proxy
    --search {raw, prod}         col.query()          vs. searcher.search_memories()

LoCoMo differences vs. LongMemEval:
  * 10 conversations × ~200 QA pairs each (vs. LME's 500 independent questions).
  * QA pairs within a conversation share a haystack — we build one palace per
    conversation, not per question.
  * Evidence is a list of dialog IDs like ["D1:3", "D2:8"]. Recall is measured
    as "fraction of evidence IDs found in top-k", mapped to either dialog IDs
    (granularity=dialog) or session IDs (granularity=session).
  * 5 question categories: Single-hop, Temporal, Temporal-inference,
    Open-domain, Adversarial.

Data loading, corpus building, and recall computation are imported verbatim
from `benchmarks.locomo_bench` — we do not re-implement them.

Usage:
    python locomo.py /tmp/locomo/data/locomo10.json \\
        --embed prod --search prod

    python locomo.py /tmp/locomo/data/locomo10.json \\
        --embed baseline --search raw --granularity session --top-k 10
"""

import argparse
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# Make sibling `mempalace` and `benchmarks` packages importable when run from
# any cwd. `lme.py` already does this for repo root; we additionally need the
# benchmarks/ directory so we can reuse `locomo_bench`'s helpers.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_BENCH_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_BENCH_DIR))


# =============================================================================
# IMPORT LOCOMO BENCH HELPERS
# =============================================================================
#
# `benchmarks/` has no __init__.py, so a simple `from benchmarks.locomo_bench
# import ...` depends on implicit namespace packaging and can silently fall
# back to cached bytecode in some setups. We load the module explicitly from
# its file path to guarantee we're pulling from the same source tree.


def _load_locomo_bench():
    bench_path = _BENCH_DIR / "locomo_bench.py"
    spec = importlib.util.spec_from_file_location("locomo_bench", bench_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load {bench_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["locomo_bench"] = mod
    spec.loader.exec_module(mod)
    return mod


_lb = _load_locomo_bench()
load_conversation_sessions = _lb.load_conversation_sessions
build_corpus_from_sessions = _lb.build_corpus_from_sessions
compute_retrieval_recall = _lb.compute_retrieval_recall
evidence_to_dialog_ids = _lb.evidence_to_dialog_ids
evidence_to_session_ids = _lb.evidence_to_session_ids
CATEGORIES = _lb.CATEGORIES


# =============================================================================
# RETRIEVAL
# =============================================================================


def retrieve_raw(palace_path, query, n_results, corpus_ids):
    """Direct ChromaDB query — bypasses all MemPalace search logic.

    Mirrors `lme.py::retrieve_raw`. Returns a ranked list of indices into
    `corpus_ids`, with unretrieved indices appended at the tail so downstream
    recall computation can walk the full list if desired.
    """
    from mempalace.palace import get_collection

    col = get_collection(palace_path, create=False)
    results = col.query(
        query_texts=[query],
        n_results=min(n_results, col.count()),
        include=["metadatas", "distances"],
    )
    metas = (results.get("metadatas") or [[]])[0]
    id_to_idx = {cid: i for i, cid in enumerate(corpus_ids)}
    ranked = []
    seen = set()
    for m in metas:
        cid = (m or {}).get("source_file", "")
        idx = id_to_idx.get(cid)
        if idx is not None and idx not in seen:
            ranked.append(idx)
            seen.add(idx)
    for i in range(len(corpus_ids)):
        if i not in seen:
            ranked.append(i)
    return ranked


def retrieve_prod(palace_path, query, n_results, corpus_ids):
    """Route through `mempalace.searcher.search_memories()` — the code MCP calls."""
    from mempalace.searcher import search_memories

    out = search_memories(
        query=query,
        palace_path=palace_path,
        n_results=n_results,
    )
    if "error" in out:
        raise RuntimeError(f"search_memories error: {out['error']}")

    id_to_idx = {cid: i for i, cid in enumerate(corpus_ids)}
    ranked, seen = [], set()
    for hit in out.get("results", []):
        cid = hit.get("source_file", "")
        idx = id_to_idx.get(cid)
        if idx is not None and idx not in seen:
            ranked.append(idx)
            seen.add(idx)
    for i in range(len(corpus_ids)):
        if i not in seen:
            ranked.append(i)
    return ranked


# =============================================================================
# INGEST — one drawer per corpus item, source_file = corpus_id so we can map back
# =============================================================================


def ingest(palace_path, corpus, corpus_ids):
    from mempalace.miner import add_drawer
    from mempalace.palace import get_collection

    col = get_collection(palace_path, create=True)
    for content, cid in zip(corpus, corpus_ids):
        add_drawer(
            col,
            wing="bench",
            room="bench",
            content=content,
            source_file=cid,
            chunk_index=0,
            agent="bench",
        )
    return col


# =============================================================================
# PER-CONVERSATION PIPELINE
# =============================================================================
#
# Unlike LongMemEval (one palace per QA), LoCoMo QA pairs within a conversation
# share the haystack. We build the palace once per conversation and reuse it
# across all ~200 QA pairs for that conversation.


def run_conversation(
    sample, search_mode, granularity, top_k, n_results, llm_rerank_fn=None, llm_args=None
):
    """Run all QA pairs against one LoCoMo conversation. Yields per-QA rows."""
    sample_id = sample.get("sample_id", "conv-?")
    conversation = sample["conversation"]
    qa_pairs = sample["qa"]

    session_summaries = sample.get("session_summary", {})
    sessions = load_conversation_sessions(conversation, session_summaries)
    corpus, corpus_ids, _timestamps = build_corpus_from_sessions(sessions, granularity=granularity)

    if not corpus:
        for qa in qa_pairs:
            yield {
                "sample_id": sample_id,
                "skipped": "empty_corpus",
                "question": qa.get("question", ""),
                "category": qa["category"],
            }
        return

    palace_dir = tempfile.mkdtemp(prefix=f"locomo_{sample_id[:12]}_")
    try:
        ingest(palace_dir, corpus, corpus_ids)

        for qa in qa_pairs:
            question = qa["question"]
            answer = qa.get("answer", qa.get("adversarial_answer", ""))
            category = qa["category"]
            evidence = qa.get("evidence", [])

            if search_mode == "prod":
                ranked = retrieve_prod(palace_dir, question, n_results, corpus_ids)
            else:
                ranked = retrieve_raw(palace_dir, question, n_results, corpus_ids)

            if llm_rerank_fn is not None and ranked:
                try:
                    ranked = llm_rerank_fn(
                        question,
                        ranked,
                        corpus,
                        corpus_ids,
                        api_key=(llm_args or {}).get("api_key", ""),
                        top_k=(llm_args or {}).get("top_k", 10),
                        model=(llm_args or {}).get("model", ""),
                        backend=(llm_args or {}).get("backend", "ollama"),
                        base_url=(llm_args or {}).get("base_url", ""),
                    )
                except Exception as e:
                    print(f"    LLM RERANK ERROR ({sample_id}): {e}", flush=True)
                    # Fall through with the original (pre-rerank) rankings.

            retrieved_ids = [corpus_ids[idx] for idx in ranked[:top_k]]

            if granularity == "dialog":
                evidence_set = evidence_to_dialog_ids(evidence)
            else:
                evidence_set = evidence_to_session_ids(evidence)

            recall = compute_retrieval_recall(retrieved_ids, evidence_set)

            yield {
                "sample_id": sample_id,
                "question": question,
                "answer": answer,
                "category": category,
                "evidence": evidence,
                "evidence_set": sorted(evidence_set),
                "retrieved_ids": retrieved_ids,
                "recall": recall,
            }
    finally:
        shutil.rmtree(palace_dir, ignore_errors=True)


# =============================================================================
# DRIVER — mirrors lme.py's apply_embed_mode / patch_hybrid_weights / run
# =============================================================================


def apply_embed_mode(embed_mode):
    """Set MEMPAL_EMBEDDING_MODEL before any mempalace import. Must be called
    before importing `mempalace.searcher` or `mempalace.palace`, because
    `palace._embedding_fn_cache` is module-level."""
    if embed_mode == "baseline":
        os.environ["MEMPAL_EMBEDDING_MODEL"] = "default"
        os.environ.pop("MEMPAL_EMBEDDING_ENDPOINT", None)
        os.environ.pop("MEMPAL_EMBEDDING_KEY", None)
    # embed_mode == "prod" → leave env alone, use whatever config is set


def patch_hybrid_weights(bm25_weight):
    """Override the default bm25_weight / vector_weight in searcher._hybrid_rank
    so we can sweep the weight at benchmark time without mutating production code."""
    if bm25_weight is None:
        return
    import functools

    from mempalace import searcher

    orig = searcher._hybrid_rank

    @functools.wraps(orig)
    def patched(results, query, **kwargs):
        kwargs.setdefault("bm25_weight", bm25_weight)
        kwargs.setdefault("vector_weight", 1.0 - bm25_weight)
        return orig(results, query, **kwargs)

    searcher._hybrid_rank = patched
    print(f"  Patched _hybrid_rank: bm25_weight={bm25_weight}, vector_weight={1 - bm25_weight:.2f}")


def _load_llm_rerank():
    """Import llm_rerank from the legacy bench so all eval scripts share one
    apples-to-apples LLM reranker implementation. Done lazily so callers
    not using --llm-rerank pay no import cost."""
    bench_path = _BENCH_DIR / "longmemeval_bench.py"
    spec = importlib.util.spec_from_file_location("_legacy_bench", bench_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load {bench_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.llm_rerank


def run(args):
    # Capture LLM bearer token BEFORE apply_embed_mode — baseline mode pops
    # MEMPAL_EMBEDDING_KEY from env, which would strip the LLM proxy auth.
    _llm_bearer = args.llm_key or os.environ.get("MEMPAL_EMBEDDING_KEY", "")

    apply_embed_mode(args.embed)
    patch_hybrid_weights(args.bm25_weight)

    llm_rerank_fn = _load_llm_rerank() if args.llm_rerank else None
    llm_args = (
        {
            "api_key": _llm_bearer,
            "top_k": args.llm_top_k,
            "model": args.llm_model,
            "backend": args.llm_backend,
            "base_url": args.llm_base_url,
        }
        if args.llm_rerank
        else None
    )

    with open(args.data_file) as f:
        data = json.load(f)

    if args.skip > 0:
        data = data[args.skip :]
        print(f"  Skipped first {args.skip} conversations (resume mode)")

    if args.limit > 0:
        data = data[: args.limit]

    cell = f"{args.embed}+{args.search}"
    out_file = args.out or (
        Path(__file__).parent / f"results_locomo_{args.embed}_{args.search}"
        f"_{args.granularity}_top{args.top_k}"
        f"_{datetime.now():%Y%m%d_%H%M%S}.jsonl"
    )

    total_qa = sum(len(s.get("qa", [])) for s in data)

    print(f"\n{'=' * 60}")
    print("  MemPalace Production × LoCoMo")
    print(f"{'=' * 60}")
    print(f"  Data:         {Path(args.data_file).name}")
    print(f"  Cell:         {cell}")
    print(f"  Conversations:{len(data)}")
    print(f"  QA pairs:     {total_qa}")
    print(f"  Granularity:  {args.granularity}")
    print(f"  Top-k:        {args.top_k}")
    if args.llm_rerank:
        print(f"  LLM rerank:   {args.llm_backend}:{args.llm_model} (top-{args.llm_top_k})")
    print(f"  Out:          {out_file}")
    print(f"{'─' * 60}\n")

    all_recall = []
    per_category = defaultdict(list)
    errors = 0
    skipped = 0

    t0 = time.time()
    # Line-buffered output so partial runs survive process death.
    out_fp = open(out_file, "w", buffering=1)

    # Over-fetch for retrieval — we fetch more than top_k so the ranking
    # has headroom. search_memories internally over-fetches too, but this
    # way both raw and prod paths see the same ceiling.
    n_results = max(args.top_k * 3, 50)

    for conv_idx, sample in enumerate(data):
        sample_id = sample.get("sample_id", f"conv-{conv_idx}")
        qa_pairs = sample.get("qa", [])
        print(f"  [{conv_idx + 1}/{len(data)}] {sample_id}: {len(qa_pairs)} questions")

        try:
            rows = list(
                run_conversation(
                    sample,
                    search_mode=args.search,
                    granularity=args.granularity,
                    top_k=args.top_k,
                    n_results=n_results,
                    llm_rerank_fn=llm_rerank_fn,
                    llm_args=llm_args,
                )
            )
        except Exception as e:
            errors += 1
            print(f"    ERROR: {e}")
            continue

        conv_recall = []
        for row in rows:
            if row.get("skipped"):
                skipped += 1
                out_fp.write(json.dumps(row) + "\n")
                out_fp.flush()
                continue

            all_recall.append(row["recall"])
            per_category[row["category"]].append(row["recall"])
            conv_recall.append(row["recall"])

            out_fp.write(json.dumps(row) + "\n")
            out_fp.flush()

        dt = time.time() - t0
        rate = (len(all_recall) / dt) if dt > 0 else 0.0
        avg_so_far = sum(all_recall) / len(all_recall) if all_recall else 0.0
        conv_avg = sum(conv_recall) / len(conv_recall) if conv_recall else 0.0
        print(f"    conv R={conv_avg:.3f}  |  running R={avg_so_far:.3f}  |  {rate:.1f} q/s")

    dt = time.time() - t0
    out_fp.close()

    avg_recall = sum(all_recall) / len(all_recall) if all_recall else 0.0

    print(f"\n{'=' * 60}")
    print(f"  RESULTS — {cell} ({args.granularity}, top-{args.top_k})")
    print(f"{'=' * 60}")
    print(f"  Time:        {dt:.1f}s ({dt / max(len(all_recall), 1):.3f}s/q)")
    print(f"  Questions:   {len(all_recall)}")
    if skipped:
        print(f"  Skipped:     {skipped} (empty corpus)")
    print(f"  Avg Recall:  {avg_recall:.4f}")

    print("\n  PER-CATEGORY RECALL:")
    for cat in sorted(per_category.keys()):
        vals = per_category[cat]
        name = CATEGORIES.get(cat, f"Cat-{cat}")
        print(f"    {name:20} R={sum(vals) / len(vals):.4f}  (n={len(vals)})")

    perfect = sum(1 for r in all_recall if r >= 1.0)
    partial = sum(1 for r in all_recall if 0 < r < 1.0)
    zero = sum(1 for r in all_recall if r == 0)
    n = max(len(all_recall), 1)
    print("\n  RECALL DISTRIBUTION:")
    print(f"    Perfect (1.0):  {perfect:4} ({perfect / n * 100:.1f}%)")
    print(f"    Partial (0-1):  {partial:4} ({partial / n * 100:.1f}%)")
    print(f"    Zero (0.0):     {zero:4} ({zero / n * 100:.1f}%)")

    print(f"\n  Errors:  {errors}")
    print(f"  Written: {out_file}")


def main():
    p = argparse.ArgumentParser(description="MemPalace production × LoCoMo")
    p.add_argument("data_file", help="Path to locomo10.json")
    p.add_argument(
        "--embed",
        choices=["baseline", "prod"],
        default="prod",
        help="baseline = ChromaDB all-MiniLM-L6-v2. "
        "prod = whatever MEMPAL_EMBEDDING_* env points to (LiteLLM proxy).",
    )
    p.add_argument(
        "--search",
        choices=["raw", "prod"],
        default="prod",
        help="raw = vanilla chromadb collection.query(). "
        "prod = mempalace.searcher.search_memories() with BM25 hybrid rerank + keyword recall.",
    )
    p.add_argument(
        "--granularity",
        choices=["session", "dialog"],
        default="session",
        help="Corpus granularity. 'session' = one doc per whole session "
        "(what LoCoMo's published headline numbers use). "
        "'dialog' = one doc per utterance (matches evidence-ID format).",
    )
    p.add_argument("--top-k", type=int, default=10, help="Retrieval top-k (default: 10)")
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Limit to first N conversations (0 = all 10). Each conversation has ~200 QA pairs.",
    )
    p.add_argument(
        "--skip",
        type=int,
        default=0,
        help="Skip first N conversations (resume mode, applied before --limit).",
    )
    p.add_argument("--out", default=None, help="JSONL output path (default: auto-named)")
    p.add_argument(
        "--bm25-weight",
        type=float,
        default=None,
        help="Override searcher._hybrid_rank's bm25_weight. "
        "vector_weight is set to 1 - bm25_weight. Only applies to --search prod.",
    )
    p.add_argument(
        "--llm-rerank",
        action="store_true",
        default=False,
        help="After backbone retrieval, ask an LLM to promote the best of the top-k "
        "to rank 1. Apples-to-apples comparison: pair with --search raw vs prod to "
        "measure which backbone hands the LLM a better candidate pool.",
    )
    p.add_argument("--llm-model", default="gemini-3.1-flash-lite-preview")
    p.add_argument(
        "--llm-backend",
        choices=["anthropic", "ollama"],
        default="ollama",
        help="ollama backend hits {base_url}/v1/chat/completions — works with the "
        "LiteLLM proxy on port 4000 (the default for prod embedding here).",
    )
    p.add_argument("--llm-base-url", default="http://127.0.0.1:4000")
    p.add_argument("--llm-key", default="", help="Bearer token; falls back to MEMPAL_EMBEDDING_KEY.")
    p.add_argument("--llm-top-k", type=int, default=10, help="Top-k pool sent to the LLM reranker.")
    args = p.parse_args()
    run(args)


if __name__ == "__main__":
    main()
