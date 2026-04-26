#!/usr/bin/env python3
"""Print the 2x2 evaluation matrix from four lme.py result files.

    python matrix.py \
        --A results_baseline_raw_*.jsonl \
        --B results_baseline_prod_*.jsonl \
        --C results_prod_raw_*.jsonl \
        --D results_prod_prod_*.jsonl
"""

import argparse
import json
from pathlib import Path


def load(path):
    if not path or not Path(path).exists():
        return None
    rows = []
    with open(path) as f:
        for line in f:
            rows.append(json.loads(line))
    return rows


def metric(rows, key):
    if not rows:
        return None
    vals = [r["metrics"].get(key, 0.0) for r in rows]
    return sum(vals) / len(vals) if vals else 0.0


def fmt(v):
    return f"{v:.4f}" if v is not None else "  --  "


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--A", help="baseline embed + raw search")
    p.add_argument("--B", help="baseline embed + prod search")
    p.add_argument("--C", help="prod embed + raw search")
    p.add_argument("--D", help="prod embed + prod search")
    p.add_argument("--k", type=int, default=5)
    args = p.parse_args()

    cells = {"A": load(args.A), "B": load(args.B), "C": load(args.C), "D": load(args.D)}
    counts = {k: (len(v) if v else 0) for k, v in cells.items()}
    print(f"\n  n: A={counts['A']}  B={counts['B']}  C={counts['C']}  D={counts['D']}\n")

    for label, key in [
        (f"R@{args.k}", f"r@{args.k}"),
        ("R@10", "r@10"),
        (f"NDCG@{args.k}", f"ndcg@{args.k}"),
    ]:
        a, b = metric(cells["A"], key), metric(cells["B"], key)
        c, d = metric(cells["C"], key), metric(cells["D"], key)
        print(f"  ── {label} " + "─" * 50)
        print("                       │  raw search    │  prod search")
        print("  ─────────────────────┼────────────────┼───────────────")
        print(f"   baseline embed      │   {fmt(a)}       │   {fmt(b)}")
        print(f"   prod embed          │   {fmt(c)}       │   {fmt(d)}")
        if a is not None:
            for src, val, name in [(b, b, "B"), (c, c, "C"), (d, d, "D")]:
                if val is not None:
                    print(f"      Δ vs A ({name}): {val - a:+.4f}")
        print()


if __name__ == "__main__":
    main()
