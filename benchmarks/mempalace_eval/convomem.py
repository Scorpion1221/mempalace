#!/usr/bin/env python3
"""
MemPalace Production × ConvoMem
================================

Drives ConvoMem retrieval through the *actual* MemPalace production code path —
`mempalace.miner.add_drawer` for ingest, `mempalace.searcher.search_memories`
for retrieval — so we can honestly measure whether recent optimizations
(BM25 hybrid rerank, keyword recall, closet boost, entity tagging, production
embedding) improve over the raw-ChromaDB baseline published in
`benchmarks/convomem_bench.py`.

Sister script to `lme.py`. Same 2×2 matrix, same flags, different benchmark.

Two independent axes:

    --embed {baseline, prod}     ChromaDB all-MiniLM  vs. our LiteLLM proxy
    --search {raw, prod}         col.query()          vs. searcher.search_memories()

ConvoMem has per-item conversation haystacks (one palace per evidence item).
Every message in the haystack is ingested as a single drawer; `source_file`
is a stable per-message id so we can map retrieval back to evidence messages
and compute per-item recall.

Scoring mirrors the raw-ChromaDB reference inside `convomem_bench.py`:
substring match between the evidence message text and each retrieved
drawer's text (case-insensitive), either direction. We report per-category
recall and overall recall@k.

Usage:
    python convomem.py --embed prod --search prod --limit 50
    python convomem.py --embed baseline --search raw --category user_evidence --limit 50
    python convomem.py --embed prod --search prod --bm25-weight 0.3 --top-k 10
"""

import argparse
import functools
import json
import os
import shutil
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# Make the repo root importable so we can reach both `mempalace` and the
# sibling `benchmarks.convomem_bench` helpers (data loaders, CATEGORIES).
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

# Reuse ConvoMem's data infrastructure — CATEGORIES map + HuggingFace loader.
# `load_evidence_items` internally uses `discover_files` and
# `download_evidence_file`, so we don't re-implement the download/cache pipeline.
from benchmarks.convomem_bench import CATEGORIES, load_evidence_items  # noqa: E402


# =============================================================================
# CORPUS BUILDER — one drawer per message, stable ids for retrieval mapping
# =============================================================================


def build_corpus(item):
    """Flatten an evidence item's haystack into (corpus, corpus_ids, speakers).

    One doc per message, mirroring `retrieve_for_item()` in convomem_bench.py.
    corpus_ids are stable strings ("msg_<i>") used as `source_file` so we can
    round-trip retrieval through MemPalace metadata.
    """
    corpus, corpus_ids, speakers = [], [], []
    for conv in item.get("conversations", []):
        for msg in conv.get("messages", []):
            corpus.append(msg["text"])
            corpus_ids.append(f"msg_{len(corpus) - 1}")
            speakers.append(msg.get("speaker", ""))
    return corpus, corpus_ids, speakers


# =============================================================================
# RETRIEVAL
# =============================================================================


def retrieve_raw(palace_path, query, n_results, corpus_ids):
    """Direct ChromaDB query — bypasses all MemPalace search logic.

    Returns a list of corpus indices ranked best-first. Any corpus id not
    returned by ChromaDB is appended at the end so the ranking is a
    complete permutation, matching `lme.py`.
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
    ranked, seen = [], set()
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
# INGEST — one drawer per message, source_file = msg_<i> so we can map back
# =============================================================================


def ingest(palace_path, corpus, corpus_ids):
    from mempalace.miner import add_drawer
    from mempalace.palace import get_collection

    col = get_collection(palace_path, create=True)
    for content, mid in zip(corpus, corpus_ids):
        add_drawer(
            col,
            wing="bench",
            room="bench",
            content=content,
            source_file=mid,
            chunk_index=0,
            agent="bench",
        )
    return col


# =============================================================================
# SCORING — substring match to stay compatible with convomem_bench.py
# =============================================================================


def score_recall(rankings, corpus, item, k):
    """Compute per-item recall@k using ConvoMem's substring-match rule.

    For each evidence message text, check whether any of the top-k retrieved
    drawer texts contains it (or is contained in it), case-insensitive.
    Recall = (evidence messages found) / (evidence messages total).
    Returns 1.0 when there are no evidence messages (graceful default).
    """
    evidence_messages = item.get("message_evidences", [])
    evidence_texts = [e["text"].strip().lower() for e in evidence_messages if e.get("text")]
    if not evidence_texts:
        return 1.0, 0, 0

    top_indices = rankings[:k]
    retrieved_texts = [corpus[i].strip().lower() for i in top_indices if i < len(corpus)]

    found = 0
    for ev_text in evidence_texts:
        for ret_text in retrieved_texts:
            if ev_text in ret_text or ret_text in ev_text:
                found += 1
                break

    return found / len(evidence_texts), found, len(evidence_texts)


# =============================================================================
# PER-ITEM PIPELINE
# =============================================================================


def build_and_retrieve(item, search_mode, n_results):
    """Build a fresh palace for one evidence item, ingest, retrieve, clean up.

    Matches the per-question isolation pattern from `lme.py` — one
    `tempfile.mkdtemp` per item, `shutil.rmtree` in a `finally` block.
    """
    corpus, corpus_ids, _ = build_corpus(item)
    if not corpus:
        return [], corpus, corpus_ids

    # Short deterministic prefix for debuggability — ConvoMem items don't
    # have a single stable id, so use the question hash instead.
    tag = abs(hash(item.get("question", ""))) % (16**8)
    palace_dir = tempfile.mkdtemp(prefix=f"convomem_{tag:08x}_")
    try:
        ingest(palace_dir, corpus, corpus_ids)
        query = item["question"]
        if search_mode == "prod":
            rankings = retrieve_prod(palace_dir, query, n_results, corpus_ids)
        else:
            rankings = retrieve_raw(palace_dir, query, n_results, corpus_ids)
    finally:
        shutil.rmtree(palace_dir, ignore_errors=True)

    return rankings, corpus, corpus_ids


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
    import importlib.util

    bench_path = Path(__file__).resolve().parents[1] / "longmemeval_bench.py"
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

    # Resolve categories.
    if args.category == "all":
        categories = list(CATEGORIES.keys())
    else:
        categories = [args.category]

    os.makedirs(args.cache_dir, exist_ok=True)

    cell = f"{args.embed}+{args.search}"
    out_file = args.out or (
        Path(__file__).parent
        / f"convomem_results_{args.embed}_{args.search}_{datetime.now():%Y%m%d_%H%M%S}.jsonl"
    )

    print(f"\n{'=' * 60}")
    print("  MemPalace Production × ConvoMem")
    print(f"{'=' * 60}")
    print(f"  Cell:        {cell}")
    print(f"  Categories:  {len(categories)}")
    print(f"  Limit/cat:   {args.limit}")
    print(f"  Top-k:       {args.top_k}")
    print(f"  Cache dir:   {args.cache_dir}")
    print(f"  Out:         {out_file}")
    print(f"{'─' * 60}")
    print("\n  Loading data from HuggingFace...\n")

    items = load_evidence_items(categories, args.limit, args.cache_dir)

    print(f"\n  Total items: {len(items)}")
    print(f"{'─' * 60}\n")

    ks = sorted({1, 3, 5, 10, max(1, args.top_k)})
    recall_at_k = {k: [] for k in ks}
    per_category = defaultdict(lambda: defaultdict(list))  # cat -> k -> [recalls]

    t0 = time.time()
    errors = 0

    # Line-buffered so partial runs survive process death — same as lme.py.
    out_fp = open(out_file, "w", buffering=1)

    for i, item in enumerate(items):
        cat_key = item.get("_category_key", "unknown")
        question = item.get("question", "")
        try:
            rankings, corpus, corpus_ids = build_and_retrieve(
                item, search_mode=args.search, n_results=max(ks)
            )
        except Exception as e:
            errors += 1
            print(f"  [{i + 1:4}/{len(items)}] {cat_key[:20]:20} ERROR: {e}")
            continue

        if not rankings:
            print(f"  [{i + 1:4}/{len(items)}] {cat_key[:20]:20} SKIP (empty corpus)")
            continue

        if llm_rerank_fn is not None:
            try:
                rankings = llm_rerank_fn(
                    question,
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
                print(f"  [{i + 1:4}/{len(items)}] {cat_key[:20]:20} LLM RERANK ERROR: {e}")
                # Fall through with the original (pre-rerank) rankings.

        entry_m = {}
        for k in ks:
            r, found, total = score_recall(rankings, corpus, item, k)
            recall_at_k[k].append(r)
            per_category[cat_key][k].append(r)
            entry_m[f"recall@{k}"] = r
            if k == args.top_k:
                entry_m["found"] = found
                entry_m["evidence_count"] = total

        log_row = {
            "category": cat_key,
            "question": question,
            "answer": item.get("answer", ""),
            "top_ids": [corpus_ids[idx] for idx in rankings[: args.top_k]],
            "metrics": entry_m,
        }
        out_fp.write(json.dumps(log_row) + "\n")
        out_fp.flush()

        if (i + 1) % 10 == 0 or i == len(items) - 1:
            done = len(recall_at_k[args.top_k])
            avg = sum(recall_at_k[args.top_k]) / done if done else 0.0
            dt = time.time() - t0
            rate = (i + 1) / dt if dt > 0 else 0
            print(
                f"  [{i + 1:4}/{len(items)}] {cat_key[:20]:20} | "
                f"R@{args.top_k}={avg:.3f} | {rate:.2f} q/s"
            )

    dt = time.time() - t0
    out_fp.close()

    # --------------------------------------------------------------------- #
    # Summary
    # --------------------------------------------------------------------- #
    print(f"\n{'=' * 60}")
    print(f"  Results — {cell}")
    print(f"{'=' * 60}")
    n = len(recall_at_k[args.top_k])
    if n:
        for k in ks:
            avg = sum(recall_at_k[k]) / n
            print(f"  R@{k:<3}  {avg:.4f}")

        print(f"\n  Per category (R@{args.top_k}):")
        for cat_key in sorted(per_category.keys()):
            vals = per_category[cat_key][args.top_k]
            if not vals:
                continue
            avg = sum(vals) / len(vals)
            perfect = sum(1 for v in vals if v >= 1.0)
            name = CATEGORIES.get(cat_key, cat_key)
            print(
                f"    {name:25} n={len(vals):4}  "
                f"R@{args.top_k}={avg:.3f}  perfect={perfect}/{len(vals)}"
            )

        perfect_total = sum(1 for r in recall_at_k[args.top_k] if r >= 1.0)
        zero_total = sum(1 for r in recall_at_k[args.top_k] if r == 0)
        print("\n  Distribution:")
        print(f"    Perfect (1.0):  {perfect_total:4} ({perfect_total / n * 100:.1f}%)")
        print(f"    Zero (0.0):     {zero_total:4} ({zero_total / n * 100:.1f}%)")

    print(f"\n  Errors:  {errors}")
    if dt:
        print(f"  Time:    {dt:.1f}s  ({n / dt:.2f} q/s)")
    print(f"  Written: {out_file}")


def main():
    p = argparse.ArgumentParser(description="MemPalace production × ConvoMem")
    p.add_argument(
        "--embed",
        choices=["baseline", "prod"],
        default="prod",
        help="baseline = ChromaDB all-MiniLM-L6-v2 (reproduces raw baseline). "
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
        "--category",
        choices=list(CATEGORIES.keys()) + ["all"],
        default="all",
        help="ConvoMem evidence category (default: all six).",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Max items per category (default: 50 — matches the published 92.9%% number).",
    )
    p.add_argument("--top-k", type=int, default=10, help="Top-k retrieval (default: 10).")
    p.add_argument(
        "--cache-dir",
        default="/tmp/convomem-data",
        help="Where HuggingFace downloads are cached (default: /tmp/convomem-data).",
    )
    p.add_argument("--out", default=None, help="JSONL output path (default: auto-named).")
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
