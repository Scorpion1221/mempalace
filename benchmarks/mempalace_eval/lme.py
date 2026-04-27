#!/usr/bin/env python3
"""
MemPalace Production × LongMemEval
===================================

Drives LongMemEval retrieval through the *actual* MemPalace production code
path — `mempalace.miner.add_drawer` for ingest, `mempalace.searcher.search_memories`
for retrieval — so we can honestly measure whether recent optimizations
(BM25 hybrid rerank, keyword recall, closet boost, entity tagging, production
embedding) improve over the 96.6% raw-ChromaDB baseline.

Two independent axes:

    --embed {baseline, prod}     ChromaDB all-MiniLM  vs. our LiteLLM proxy
    --search {raw, prod}         col.query()          vs. searcher.search_memories()

Four combinations reproduce the 2×2 matrix in README.md. Each run is one
subprocess — we don't try to switch embedding functions mid-process because
`palace._embedding_fn_cache` is module-level.

Usage:
    python lme.py /tmp/longmemeval-data/longmemeval_s_cleaned.json \
        --embed prod --search prod --limit 20

    python lme.py /tmp/longmemeval-data/longmemeval_s_cleaned.json \
        --embed baseline --search raw            # full 500
"""

import argparse
import json
import math
import os
import shutil
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# Make sibling `mempalace` package importable when run from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


# =============================================================================
# METRICS — same formulas as benchmarks/longmemeval_bench.py
# =============================================================================


def dcg(relevances, k):
    score = 0.0
    for i, rel in enumerate(relevances[:k]):
        score += rel / math.log2(i + 2)
    return score


def ndcg(rankings, correct_ids, corpus_ids, k):
    relevances = [1.0 if corpus_ids[idx] in correct_ids else 0.0 for idx in rankings[:k]]
    ideal = sorted(relevances, reverse=True)
    idcg = dcg(ideal, k)
    if idcg == 0:
        return 0.0
    return dcg(relevances, k) / idcg


def evaluate_retrieval(rankings, correct_ids, corpus_ids, k):
    top_k_ids = set(corpus_ids[idx] for idx in rankings[:k])
    recall_any = float(any(cid in top_k_ids for cid in correct_ids))
    recall_all = float(all(cid in top_k_ids for cid in correct_ids))
    ndcg_score = ndcg(rankings, correct_ids, corpus_ids, k)
    return recall_any, recall_all, ndcg_score


# =============================================================================
# CORPUS BUILDER — one doc per haystack session (session granularity)
# =============================================================================


def build_corpus(entry):
    """Flatten haystack sessions into (corpus, corpus_ids, timestamps).

    Mirrors the session-granularity path of `build_palace_and_retrieve()`
    in the original bench: one document per session = concatenated user
    turns. Assistant turns are dropped (LongMemEval convention).
    """
    corpus, corpus_ids, timestamps = [], [], []
    for session, sess_id, date in zip(
        entry["haystack_sessions"],
        entry["haystack_session_ids"],
        entry["haystack_dates"],
    ):
        user_turns = [t["content"] for t in session if t["role"] == "user"]
        if not user_turns:
            continue
        corpus.append("\n".join(user_turns))
        corpus_ids.append(sess_id)
        timestamps.append(date)
    return corpus, corpus_ids, timestamps


# =============================================================================
# RETRIEVAL
# =============================================================================


def retrieve_raw(palace_path, query, n_results, corpus_ids):
    """Direct ChromaDB query — bypasses all MemPalace search logic."""
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
# INGEST — one drawer per session, source_file = sess_id so we can map back
# =============================================================================


def ingest(palace_path, corpus, corpus_ids):
    """Batch-upsert all sessions into the palace in a single call.

    This matters a lot with prod embedding (LiteLLM → Gemini): ProxyEmbeddingFunction
    fans the batch out to 10 worker threads, so ingest time drops ~10x vs.
    calling miner.add_drawer once per session (which upserts one doc at a time
    and never engages the worker pool). Produces identical drawer metadata —
    we just inline what miner.add_drawer was doing, minus the one-at-a-time
    collection.upsert call.

    Drawer ids use the corpus index (not a hash of source_file) because some
    LongMemEval haystacks contain duplicate session_ids; hashing those would
    collide in a single upsert and ChromaDB rejects duplicate ids.
    """
    from datetime import datetime

    from mempalace.miner import (
        NORMALIZE_VERSION,
        _extract_entities_for_metadata,
        detect_hall,
    )
    from mempalace.palace import get_collection

    col = get_collection(palace_path, create=True)
    docs, ids, metas = [], [], []
    now = datetime.now().isoformat()
    for i, (content, sess_id) in enumerate(zip(corpus, corpus_ids)):
        drawer_id = f"drawer_bench_{i:04d}"
        metadata = {
            "wing": "bench",
            "room": "bench",
            "source_file": sess_id,
            "chunk_index": 0,
            "added_by": "bench",
            "filed_at": now,
            "normalize_version": NORMALIZE_VERSION,
            "hall": detect_hall(content),
        }
        entities = _extract_entities_for_metadata(content)
        if entities:
            metadata["entities"] = entities
        docs.append(content)
        ids.append(drawer_id)
        metas.append(metadata)
    col.upsert(documents=docs, ids=ids, metadatas=metas)
    return col


# =============================================================================
# PER-QUESTION PIPELINE
# =============================================================================


def build_and_retrieve(entry, search_mode, n_results=50):
    corpus, corpus_ids, timestamps = build_corpus(entry)
    if not corpus:
        return [], corpus, corpus_ids, timestamps

    palace_dir = tempfile.mkdtemp(prefix=f"lme_{entry['question_id'][:8]}_")
    try:
        ingest(palace_dir, corpus, corpus_ids)
        query = entry["question"]
        if search_mode == "prod":
            rankings = retrieve_prod(palace_dir, query, n_results, corpus_ids)
        else:
            rankings = retrieve_raw(palace_dir, query, n_results, corpus_ids)
    finally:
        shutil.rmtree(palace_dir, ignore_errors=True)

    return rankings, corpus, corpus_ids, timestamps


# =============================================================================
# DRIVER
# =============================================================================


def apply_embed_mode(embed_mode):
    """Set MEMPAL_EMBEDDING_MODEL before any mempalace import. Caller should
    invoke this before importing mempalace modules."""
    if embed_mode == "baseline":
        # Force ChromaDB built-in all-MiniLM-L6-v2.
        os.environ["MEMPAL_EMBEDDING_MODEL"] = "default"
        # Clear the proxy config so no network calls are attempted.
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
    """Import llm_rerank from the legacy bench so both A and B share one
    apples-to-apples LLM reranker implementation. Done lazily so callers
    not using --llm-rerank pay no import cost."""
    import importlib.util

    bench_path = Path(__file__).resolve().parents[1] / "longmemeval_bench.py"
    spec = importlib.util.spec_from_file_location("_legacy_bench", bench_path)
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

    with open(args.data_file) as f:
        data = json.load(f)

    if args.split_file and (args.dev_only or args.held_out):
        with open(args.split_file) as f:
            split = json.load(f)
        subset_key = "dev" if args.dev_only else "held_out"
        wanted = set(split[subset_key])
        before = len(data)
        data = [e for e in data if e["question_id"] in wanted]
        print(f"  Split filter ({subset_key}): {before} → {len(data)} questions")

    if args.skip > 0:
        data = data[args.skip :]
        print(f"  Skipped first {args.skip} (resume mode)")

    if args.limit > 0:
        data = data[: args.limit]

    cell = f"{args.embed}+{args.search}"
    out_file = args.out or (
        Path(__file__).parent
        / f"results_{args.embed}_{args.search}_{datetime.now():%Y%m%d_%H%M%S}.jsonl"
    )

    print(f"\n{'=' * 60}")
    print("  MemPalace Production × LongMemEval")
    print(f"{'=' * 60}")
    print(f"  Data:    {Path(args.data_file).name}")
    print(f"  Cell:    {cell}")
    print(f"  N:       {len(data)}")
    print(f"  Out:     {out_file}")
    print(f"{'─' * 60}\n")

    ks = [1, 3, 5, 10, 30, 50]
    metrics = {f"recall_any@{k}": [] for k in ks}
    metrics.update({f"ndcg@{k}": [] for k in ks})
    per_type = defaultdict(lambda: defaultdict(list))

    logs = []
    t0 = time.time()
    errors = 0

    # Open output file in line-buffered append mode so partial runs survive.
    out_fp = open(out_file, "w", buffering=1)

    for i, entry in enumerate(data):
        qid = entry["question_id"]
        qtype = entry["question_type"]
        try:
            rankings, corpus, corpus_ids, _ = build_and_retrieve(
                entry, search_mode=args.search, n_results=max(ks)
            )
        except Exception as e:
            errors += 1
            print(f"  [{i + 1:4}/{len(data)}] {qid[:12]} ERROR: {e}")
            continue

        if not rankings:
            print(f"  [{i + 1:4}/{len(data)}] {qid[:12]} SKIP (empty corpus)")
            continue

        if llm_rerank_fn is not None:
            try:
                rankings = llm_rerank_fn(
                    entry["question"],
                    rankings,
                    corpus,
                    corpus_ids,
                    api_key=_llm_bearer,
                    top_k=args.llm_top_k,
                    model=args.llm_model,
                    backend=args.llm_backend,
                    base_url=args.llm_base_url,
                )
            except Exception as e:
                errors += 1
                print(f"  [{i + 1:4}/{len(data)}] {qid[:12]} LLM RERANK ERROR: {e}")
                # Fall through with the original (pre-rerank) rankings.

        answer_sids = set(entry["answer_session_ids"])
        entry_m = {}
        for k in ks:
            ra, _, nd = evaluate_retrieval(rankings, answer_sids, corpus_ids, k)
            metrics[f"recall_any@{k}"].append(ra)
            metrics[f"ndcg@{k}"].append(nd)
            entry_m[f"r@{k}"] = ra
            entry_m[f"ndcg@{k}"] = nd

        per_type[qtype]["r@5"].append(metrics["recall_any@5"][-1])
        per_type[qtype]["r@10"].append(metrics["recall_any@10"][-1])

        logs.append(
            {
                "question_id": qid,
                "question_type": qtype,
                "question": entry["question"],
                "answer_session_ids": list(answer_sids),
                "top_ids": [corpus_ids[idx] for idx in rankings[:10]],
                "metrics": entry_m,
            }
        )
        out_fp.write(json.dumps(logs[-1]) + "\n")
        out_fp.flush()

        if (i + 1) % 10 == 0 or i == len(data) - 1:
            done = len(metrics["recall_any@5"])
            r5 = sum(metrics["recall_any@5"]) / done if done else 0.0
            r10 = sum(metrics["recall_any@10"]) / done if done else 0.0
            dt = time.time() - t0
            rate = (i + 1) / dt if dt > 0 else 0
            print(
                f"  [{i + 1:4}/{len(data)}] {qid[:12]} | "
                f"R@5={r5:.3f} R@10={r10:.3f} | {rate:.2f} q/s"
            )

    dt = time.time() - t0

    out_fp.close()

    print(f"\n{'=' * 60}")
    print(f"  Results — {cell}")
    print(f"{'=' * 60}")
    n = len(metrics["recall_any@5"])
    if n:
        for k in ks:
            r = sum(metrics[f"recall_any@{k}"]) / n
            nd = sum(metrics[f"ndcg@{k}"]) / n
            print(f"  R@{k:<3}  {r:.4f}   NDCG@{k:<3} {nd:.4f}")
        print(f"\n  Per question type (R@5 / R@10):")
        for qt, vals in sorted(per_type.items()):
            qn = len(vals["r@5"])
            print(
                f"    {qt:35} n={qn:4}  "
                f"R@5={sum(vals['r@5']) / qn:.3f}  R@10={sum(vals['r@10']) / qn:.3f}"
            )
    print(f"\n  Errors:  {errors}")
    print(f"  Time:    {dt:.1f}s  ({n / dt:.2f} q/s)" if dt else "")
    print(f"  Written: {out_file}")


def main():
    p = argparse.ArgumentParser(description="MemPalace production × LongMemEval")
    p.add_argument("data_file", help="Path to longmemeval_s_cleaned.json")
    p.add_argument(
        "--embed",
        choices=["baseline", "prod"],
        default="prod",
        help="baseline = ChromaDB all-MiniLM-L6-v2 (reproduces 96.6% baseline). "
        "prod = whatever MEMPAL_EMBEDDING_* env points to (LiteLLM proxy).",
    )
    p.add_argument(
        "--search",
        choices=["raw", "prod"],
        default="prod",
        help="raw = vanilla chromadb collection.query(). "
        "prod = mempalace.searcher.search_memories() with BM25 hybrid rerank + keyword recall.",
    )
    p.add_argument("--limit", type=int, default=0, help="Limit to first N questions (0 = all)")
    p.add_argument(
        "--split-file",
        default=None,
        help="Path to benchmarks/lme_split_50_450.json. "
        "Used with --dev-only or --held-out to run a stratified subset.",
    )
    p.add_argument(
        "--dev-only", action="store_true", help="Run only the 50 dev questions (requires --split-file)"
    )
    p.add_argument(
        "--held-out",
        action="store_true",
        help="Run only the 450 held-out questions (requires --split-file)",
    )
    p.add_argument("--out", default=None, help="JSONL output path (default: auto-named)")
    p.add_argument("--skip", type=int, default=0, help="Skip first N questions (resume mode)")
    p.add_argument(
        "--bm25-weight",
        type=float,
        default=None,
        help="Override searcher._hybrid_rank's bm25_weight (default is 0.4 in prod). "
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
