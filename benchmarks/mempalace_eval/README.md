# MemPalace Production Eval

Evaluates MemPalace's **actual production code path** against the published
"raw ChromaDB" baseline to answer one question:

> Do our recent optimizations (BM25 hybrid rerank, multi-query, keyword recall,
> closet boost, entity tagging, production embedding) actually improve retrieval
> over the library's 96.6% raw baseline — and where does the gain come from?

## Why a separate eval

`benchmarks/longmemeval_bench.py` implements every mode (`raw`, `hybrid_v4`,
`palace`, etc.) inline inside the benchmark script. It imports `mempalace.dialect`
and nothing else. None of its modes exercise `mempalace.searcher.search_memories()`
— the code our users actually run. So the published 96.6% → 100% progression
tells us nothing about whether our dev-branch changes to `searcher.py`, `miner.py`,
or `palace.py` help or hurt.

This folder fixes that: one script, one code path, reads the same LongMemEval
questions, but routes every query through the production searcher.

## The 2×2 matrix

Two orthogonal changes vs. the published baseline:

| | Embedding: `all-MiniLM` (ChromaDB default, baseline) | Embedding: `gemini-embedding-2` (our prod default) |
|---|---|---|
| **Search: raw ChromaDB `col.query()`** | **A** — reproduces published 96.6% | **B** — embedding-only gain |
| **Search: `searcher.search_memories()`** | **C** — algorithm-only gain | **D** — full production stack |

- A is the reference point we're measuring against.
- D is what users get today.
- B and C decompose the gain.

## Running

```bash
# Prereq — data
mkdir -p /tmp/longmemeval-data
curl -fsSL -o /tmp/longmemeval-data/longmemeval_s_cleaned.json \
  https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json

# Prereq — embedding proxy (only needed for --embed prod)
# LiteLLM proxy must be running at MEMPAL_EMBEDDING_ENDPOINT (default :4000)

# Smoke test (20 questions, all 4 cells)
python lme.py /tmp/longmemeval-data/longmemeval_s_cleaned.json --embed baseline --search raw  --limit 20
python lme.py /tmp/longmemeval-data/longmemeval_s_cleaned.json --embed baseline --search prod --limit 20
python lme.py /tmp/longmemeval-data/longmemeval_s_cleaned.json --embed prod     --search raw  --limit 20
python lme.py /tmp/longmemeval-data/longmemeval_s_cleaned.json --embed prod     --search prod --limit 20

# Full run
python lme.py /tmp/longmemeval-data/longmemeval_s_cleaned.json --embed prod --search prod
```

## Output

Each run writes `results_<cell>_<timestamp>.jsonl` with per-question rankings
and metrics, plus a console summary (R@1/3/5/10, NDCG@10, per-question-type).

`compare.py` diffs two result files and flags per-question wins/losses.

## ConvoMem (`convomem.py`)

Sister script that runs the same 2×2 matrix against the ConvoMem benchmark
(Salesforce/ConvoMem on HuggingFace), so we can honestly measure whether the
production search path (`searcher.search_memories`) beats raw ChromaDB on a
second, independent retrieval benchmark. ConvoMem uses per-item conversation
haystacks instead of one shared corpus: every message in an item becomes a
drawer, and recall is scored by substring match against the item's
`message_evidences` — mirroring the reference raw-mode path inside
`benchmarks/convomem_bench.py`. Data is auto-downloaded from HuggingFace and
cached at `/tmp/convomem-data` on first run (no preliminary `curl` needed).

```bash
# Smoke test — 10 items, one category, all four cells
python convomem.py --embed baseline --search raw  --category user_evidence --limit 10
python convomem.py --embed baseline --search prod --category user_evidence --limit 10
python convomem.py --embed prod     --search raw  --category user_evidence --limit 10
python convomem.py --embed prod     --search prod --category user_evidence --limit 10

# Full run — 50 items × 6 categories (matches the published 92.9% number)
python convomem.py --embed prod --search prod --limit 50
```

Takes the same `--embed`, `--search`, and `--bm25-weight` flags as `lme.py`,
plus `--category {all, user_evidence, assistant_facts_evidence, changing_evidence,
abstention_evidence, preference_evidence, implicit_connection_evidence}`,
`--limit` (items per category, default 50), and `--top-k` (default 10). Output
is `convomem_results_<cell>_<timestamp>.jsonl` (line-buffered, flushed per row
so partial runs survive process death) plus a console summary with R@1/3/5/10
and per-category breakdown. Published reference numbers for raw ChromaDB:
`assistant_facts_evidence=1.00`, `user_evidence=0.98`, `abstention=0.91`,
`implicit_connection=0.89`, `preference=0.86`.

## LoCoMo (`locomo.py`)

Same 2×2-matrix harness against the [LoCoMo](https://snap-stanford.github.io/locomo/)
benchmark — 10 long-form conversations × ~200 QA pairs each, five categories
(Single-hop, Temporal, Temporal-inference, Open-domain, Adversarial). Reuses
`benchmarks/locomo_bench.py`'s data loader, corpus builder, and recall
computation verbatim, so numbers are directly comparable to the legacy modes
in that file. QA pairs within a conversation share a haystack — we build one
fresh palace per conversation (not per QA), ingest each corpus item via
`miner.add_drawer`, then route every question through either raw ChromaDB or
`searcher.search_memories`. `--limit` and `--skip` operate on conversations,
not QA pairs. Data is expected at `/tmp/locomo/data/locomo10.json` (already
downloaded for this repo).

```bash
# Smoke test — first conversation only (~200 QA), all 4 cells
python locomo.py /tmp/locomo/data/locomo10.json --embed baseline --search raw  --limit 1
python locomo.py /tmp/locomo/data/locomo10.json --embed baseline --search prod --limit 1
python locomo.py /tmp/locomo/data/locomo10.json --embed prod     --search raw  --limit 1
python locomo.py /tmp/locomo/data/locomo10.json --embed prod     --search prod --limit 1

# Full run — all 10 conversations (~2000 QA)
python locomo.py /tmp/locomo/data/locomo10.json --embed prod --search prod
```

Takes the same `--embed`, `--search`, and `--bm25-weight` flags as `lme.py`,
plus `--granularity {session, dialog}` (default `session` — what LoCoMo's
published headline numbers use; `dialog` matches the evidence-ID format
exactly) and `--top-k` (default 10). Output is
`results_locomo_<embed>_<search>_<granularity>_top<k>_<timestamp>.jsonl`
(line-buffered, flushed per row so partial runs survive process death) plus a
console summary with avg recall, per-category recall, and the
perfect/partial/zero distribution.
