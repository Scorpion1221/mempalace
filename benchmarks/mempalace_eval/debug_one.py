#!/usr/bin/env python3
"""Debug one preference regression.

Replicates lme.py's pipeline for ONE question, then prints every candidate
with its vector / BM25 / final score so we can see who displaced the answer.
"""

import json
import os
import sys
import shutil
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Force baseline (all-MiniLM) for parity with cell B.
os.environ["MEMPAL_EMBEDDING_MODEL"] = "default"
os.environ.pop("MEMPAL_EMBEDDING_ENDPOINT", None)
os.environ.pop("MEMPAL_EMBEDDING_KEY", None)

from mempalace.miner import add_drawer
from mempalace.palace import get_collection
from mempalace.searcher import (
    _bm25_scores,
    _first_or_empty,
    _keyword_recall,
    _tokenize,
)


def find_entry(data, qid):
    for e in data:
        if e["question_id"] == qid:
            return e
    raise SystemExit(f"qid {qid} not found")


def main():
    qid = sys.argv[1] if len(sys.argv) > 1 else "38146c39"  # default: chocolate chip cookies
    data = json.load(open("/tmp/longmemeval-data/longmemeval_s_cleaned.json"))
    entry = find_entry(data, qid)
    answer_ids = set(entry["answer_session_ids"])
    print(f"Q: {entry['question']}")
    print(f"Answer session: {answer_ids}")
    print()

    # Build corpus
    corpus, corpus_ids, _ = [], [], []
    for session, sid, _ in zip(
        entry["haystack_sessions"], entry["haystack_session_ids"], entry["haystack_dates"]
    ):
        user_turns = [t["content"] for t in session if t["role"] == "user"]
        if not user_turns:
            continue
        corpus.append("\n".join(user_turns))
        corpus_ids.append(sid)

    palace_dir = tempfile.mkdtemp(prefix=f"dbg_{qid}_")
    try:
        col = get_collection(palace_dir, create=True)
        for content, sid in zip(corpus, corpus_ids):
            add_drawer(col, "bench", "bench", content, sid, 0, "bench")

        query = entry["question"]
        per_query_fetch = 50 * 3

        # 1. Pure cosine query
        qres = col.query(
            query_texts=[query],
            n_results=per_query_fetch,
            include=["documents", "metadatas", "distances"],
        )
        cos_ids = _first_or_empty(qres, "ids")
        cos_metas = _first_or_empty(qres, "metadatas")
        cos_docs = _first_or_empty(qres, "documents")
        cos_dists = _first_or_empty(qres, "distances")
        print("=" * 70)
        print(f"PURE COSINE — top 15 of {len(cos_ids)} cosine candidates")
        print(f"{'rank':<5} {'cid':<25} {'distance':>8}  {'mark'}")
        for i, (m, d) in enumerate(zip(cos_metas[:15], cos_dists[:15])):
            sid = (m or {}).get("source_file", "?")
            mark = "✓ ANSWER" if sid in answer_ids else ""
            print(f"  {i + 1:<3} {sid:<25} {d:>8.4f}  {mark}")
        print()

        # 2. Keyword recall (returns extra candidates not in cosine top)
        merged_ids = set(cos_ids)
        kw_hits = _keyword_recall(col, query, where=None, exclude_ids=merged_ids, limit=50 * 3)
        print("=" * 70)
        print(f"KEYWORD RECALL — added {len(kw_hits)} candidates not in cosine pool")
        print(f"  (these only exist because of literal-keyword match via $contains)")
        for h in kw_hits[:15]:
            sid = (h.get("_source_file_full") or h.get("source_file", "?"))
            mark = "✓ ANSWER" if sid in answer_ids else ""
            print(f"  {sid:<35} dist={h['distance']:>8.4f}  {mark}")
        print()

        # 3. Build full candidate pool with bm25, replicate hybrid_rank
        all_cands = []
        for sid_doc, m, d, doc in zip(cos_ids, cos_metas, cos_dists, cos_docs):
            cid = (m or {}).get("source_file", "?")
            all_cands.append({"corpus_id": cid, "text": doc, "distance": d, "via": "cosine"})
        for h in kw_hits:
            cid = h.get("_source_file_full") or h.get("source_file", "?")
            all_cands.append({"corpus_id": cid, "text": h.get("text", ""),
                              "distance": h.get("distance", 1.0), "via": "keyword"})

        docs = [c["text"] for c in all_cands]
        bm25_raw = _bm25_scores(query, docs)
        max_bm25 = max(bm25_raw) if bm25_raw else 0.0
        bm25_norm = [s / max_bm25 if max_bm25 > 0 else 0.0 for s in bm25_raw]
        for c, raw, norm in zip(all_cands, bm25_raw, bm25_norm):
            vec_sim = max(0.0, 1 - c["distance"])
            c["vec_sim"] = vec_sim
            c["bm25_raw"] = raw
            c["bm25_norm"] = norm
            c["final"] = 0.6 * vec_sim + 0.4 * norm

        ranked = sorted(all_cands, key=lambda c: c["final"], reverse=True)
        print("=" * 70)
        print(f"HYBRID RANK (vec_w=0.6, bm25_w=0.4) — top 15 of {len(ranked)}")
        print(f"  {'rank':<3} {'cid':<25} {'vec_sim':>8} {'bm25_n':>8} {'final':>8}  {'via':<8} {'mark'}")
        for i, c in enumerate(ranked[:15]):
            mark = "✓ ANSWER" if c["corpus_id"] in answer_ids else ""
            print(f"  {i + 1:<3} {c['corpus_id']:<25} "
                  f"{c['vec_sim']:>8.3f} {c['bm25_norm']:>8.3f} {c['final']:>8.3f}  "
                  f"{c['via']:<8} {mark}")
        print()

        # 4. Find where answer landed in each ranking
        print("=" * 70)
        print("ANSWER POSITION ACROSS STAGES")
        for stage_name, stage_list in [
            ("pure cosine", [(m or {}).get("source_file") for m in cos_metas]),
            ("hybrid", [c["corpus_id"] for c in ranked]),
        ]:
            for i, sid in enumerate(stage_list):
                if sid in answer_ids:
                    print(f"  {stage_name:<15} → rank {i + 1}")
                    break
            else:
                print(f"  {stage_name:<15} → NOT FOUND in {len(stage_list)}")
        print()

        # 5. Sweep: what if bm25_weight were lower?
        print("=" * 70)
        print("WEIGHT SWEEP — answer's rank under different bm25_weight")
        print("  (vec_weight = 1 - bm25_weight)")
        print(f"  {'bm25_w':<8} {'answer_rank':<12} {'top-1 cid':<25}")
        for bw in [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6]:
            vw = 1 - bw
            rescored = sorted(
                all_cands, key=lambda c: vw * c["vec_sim"] + bw * c["bm25_norm"], reverse=True
            )
            ans_rank = next((i + 1 for i, c in enumerate(rescored) if c["corpus_id"] in answer_ids), -1)
            top1 = rescored[0]["corpus_id"] if rescored else "?"
            print(f"  {bw:<8.2f} {ans_rank:<12} {top1:<25}")

    finally:
        shutil.rmtree(palace_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
