#!/usr/bin/env bash
# Lance la grille (ticker × order) pour le script research_pivot_nq1.py.
# Usage : bash scripts/run_pivot_grid.sh <TICKER> [orders...]
# Exemple : bash scripts/run_pivot_grid.sh MES1 2 5 10 15 20
set -e
cd "$(dirname "$0")/.."

TICKER="${1:?ticker requis}"
shift
ORDERS=("$@")
if [ ${#ORDERS[@]} -eq 0 ]; then
  ORDERS=(2 5 10 15 20)
fi

# Limite les threads par process pour éviter la contention quand on lance
# plusieurs tickers en parallèle.
export OMP_NUM_THREADS=2
export OPENBLAS_NUM_THREADS=2
export MKL_NUM_THREADS=2

echo "▸ [$TICKER] orders : ${ORDERS[*]}"
for O in "${ORDERS[@]}"; do
  echo "──── $TICKER order=$O ────"
  python scripts/research_pivot_nq1.py --ticker "$TICKER" --order "$O" 2>&1 \
    | tail -20
done
echo "▸ [$TICKER] terminé."
