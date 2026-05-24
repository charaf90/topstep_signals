#!/usr/bin/env bash
# Grille H1 (ticker × order). Orders adaptés à la rareté de H1.
set -e
cd "$(dirname "$0")/.."

TICKER="${1:?ticker requis}"
shift
ORDERS=("$@")
if [ ${#ORDERS[@]} -eq 0 ]; then
  ORDERS=(2 3 5 8 10)
fi

export OMP_NUM_THREADS=2
export OPENBLAS_NUM_THREADS=2
export MKL_NUM_THREADS=2

echo "▸ [$TICKER H1] orders : ${ORDERS[*]}"
for O in "${ORDERS[@]}"; do
  echo "──── $TICKER H1 order=$O ────"
  python scripts/research_pivot_h1.py --ticker "$TICKER" --order "$O" 2>&1 | tail -10
done
echo "▸ [$TICKER H1] terminé."
