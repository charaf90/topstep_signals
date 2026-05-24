#!/usr/bin/env bash
# Lance la grille walk-forward (ticker × order).
# Usage : bash scripts/run_pivot_wf_grid.sh <TICKER> [orders...]
set -e
cd "$(dirname "$0")/.."

TICKER="${1:?ticker requis}"
shift
ORDERS=("$@")
if [ ${#ORDERS[@]} -eq 0 ]; then
  ORDERS=(2 5 10 15 20)
fi

export OMP_NUM_THREADS=2
export OPENBLAS_NUM_THREADS=2
export MKL_NUM_THREADS=2

echo "▸ [$TICKER WF] orders : ${ORDERS[*]}"
for O in "${ORDERS[@]}"; do
  echo "──── $TICKER order=$O ────"
  python scripts/research_pivot_wf.py --ticker "$TICKER" --order "$O" 2>&1 | tail -10
done
echo "▸ [$TICKER WF] terminé."
