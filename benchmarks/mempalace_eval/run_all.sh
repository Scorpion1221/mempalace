#!/usr/bin/env bash
# Run the 2x2 LongMemEval evaluation matrix and print the summary.
#
# Usage:
#   ./run_all.sh /tmp/longmemeval-data/longmemeval_s_cleaned.json [--limit N]
#
# Produces 4 result files in the script dir, plus a final matrix print.
# A and B (baseline embed) run in parallel; C and D (prod embed) run sequentially
# to avoid hammering the LiteLLM proxy.
set -euo pipefail

DATA="${1:-/tmp/longmemeval-data/longmemeval_s_cleaned.json}"
shift || true
EXTRA_ARGS="$*"

DIR="$(cd "$(dirname "$0")" && pwd)"
TS="$(date +%Y%m%d_%H%M%S)"
A="$DIR/results_baseline_raw_${TS}.jsonl"
B="$DIR/results_baseline_prod_${TS}.jsonl"
C="$DIR/results_prod_raw_${TS}.jsonl"
D="$DIR/results_prod_prod_${TS}.jsonl"

cd "$DIR/../.."

echo ">> Cell A + B (baseline embed, runs in parallel) ..."
python -u benchmarks/mempalace_eval/lme.py "$DATA" --embed baseline --search raw  --out "$A" $EXTRA_ARGS > "$DIR/log_A_${TS}.log" 2>&1 &
PID_A=$!
python -u benchmarks/mempalace_eval/lme.py "$DATA" --embed baseline --search prod --out "$B" $EXTRA_ARGS > "$DIR/log_B_${TS}.log" 2>&1 &
PID_B=$!
wait $PID_A; echo "   A done."
wait $PID_B; echo "   B done."

# Source env so MEMPAL_EMBEDDING_KEY is available.
if [ -f "$HOME/.mempalace/env" ]; then
    set +u; source "$HOME/.mempalace/env"; set -u
fi

echo ">> Cell C (prod embed + raw search) — ~25s/q ..."
python -u benchmarks/mempalace_eval/lme.py "$DATA" --embed prod --search raw --out "$C" $EXTRA_ARGS > "$DIR/log_C_${TS}.log" 2>&1
echo "   C done."

echo ">> Cell D (prod embed + prod search, full production) ..."
python -u benchmarks/mempalace_eval/lme.py "$DATA" --embed prod --search prod --out "$D" $EXTRA_ARGS > "$DIR/log_D_${TS}.log" 2>&1
echo "   D done."

echo
python benchmarks/mempalace_eval/matrix.py --A "$A" --B "$B" --C "$C" --D "$D"

echo
echo "Result files:"
echo "  A: $A"
echo "  B: $B"
echo "  C: $C"
echo "  D: $D"
