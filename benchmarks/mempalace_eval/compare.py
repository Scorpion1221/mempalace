#!/usr/bin/env python3
"""
Diff two lme.py result files.

    python compare.py results_baseline_raw_*.jsonl results_prod_prod_*.jsonl

Prints side-by-side summary metrics, per-question-type deltas, and per-question
wins/losses (rows where one file got R@5=1 and the other got R@5=0).
"""

import argparse
import json
from collections import defaultdict
from pathlib import Path


def load(path):
    rows = {}
    with open(path) as f:
        for line in f:
            r = json.loads(line)
            rows[r["question_id"]] = r
    return rows


def summary(rows):
    ks = [1, 3, 5, 10, 30, 50]
    out = {}
    n = len(rows)
    for k in ks:
        rs = [r["metrics"].get(f"r@{k}", 0.0) for r in rows.values()]
        nds = [r["metrics"].get(f"ndcg@{k}", 0.0) for r in rows.values()]
        out[f"r@{k}"] = sum(rs) / n if n else 0.0
        out[f"ndcg@{k}"] = sum(nds) / n if n else 0.0
    return out


def per_type(rows, k=5):
    d = defaultdict(list)
    for r in rows.values():
        d[r["question_type"]].append(r["metrics"].get(f"r@{k}", 0.0))
    return {qt: sum(vs) / len(vs) for qt, vs in d.items()}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("base", help="Baseline jsonl (typically results_baseline_raw_*.jsonl)")
    p.add_argument("cand", help="Candidate jsonl (typically results_prod_prod_*.jsonl)")
    p.add_argument("--k", type=int, default=5, help="Recall@k for win/loss scan (default 5)")
    args = p.parse_args()

    base = load(args.base)
    cand = load(args.cand)
    shared = set(base) & set(cand)
    print(f"\n  Base:   {Path(args.base).name}  (n={len(base)})")
    print(f"  Cand:   {Path(args.cand).name}  (n={len(cand)})")
    print(f"  Shared: {len(shared)}\n")

    sb, sc = summary({k: base[k] for k in shared}), summary({k: cand[k] for k in shared})
    print(f"  {'metric':<10} {'base':>8}  {'cand':>8}  {'delta':>8}")
    print(f"  {'-' * 10} {'-' * 8}  {'-' * 8}  {'-' * 8}")
    for m in sb:
        d = sc[m] - sb[m]
        arrow = "↑" if d > 1e-6 else ("↓" if d < -1e-6 else " ")
        print(f"  {m:<10} {sb[m]:>8.4f}  {sc[m]:>8.4f}  {d:>+7.4f} {arrow}")

    print(f"\n  Per-type R@{args.k}:")
    tb, tc = per_type({k: base[k] for k in shared}, args.k), per_type(
        {k: cand[k] for k in shared}, args.k
    )
    print(f"  {'type':<35} {'base':>7} {'cand':>7} {'delta':>7}")
    for qt in sorted(set(tb) | set(tc)):
        a, b = tb.get(qt, 0.0), tc.get(qt, 0.0)
        print(f"  {qt:<35} {a:>7.3f} {b:>7.3f} {b - a:>+7.3f}")

    wins, losses = [], []
    for qid in shared:
        rb = base[qid]["metrics"].get(f"r@{args.k}", 0.0)
        rc = cand[qid]["metrics"].get(f"r@{args.k}", 0.0)
        if rc > rb:
            wins.append(qid)
        elif rc < rb:
            losses.append(qid)
    print(f"\n  Cand beat base on R@{args.k}:  {len(wins):>3} questions")
    print(f"  Cand lost to base on R@{args.k}: {len(losses):>3} questions")

    if losses:
        print(f"\n  Regressions (R@{args.k} dropped):")
        for qid in losses[:10]:
            q = base[qid]["question"][:80]
            print(f"    {qid}  {base[qid]['question_type']:<30}  {q}")
        if len(losses) > 10:
            print(f"    ... +{len(losses) - 10} more")


if __name__ == "__main__":
    main()
