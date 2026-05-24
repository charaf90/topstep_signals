#!/usr/bin/env bash
# Vérification locale avant merge sur main.
#
# À lancer manuellement :
#   ./check.sh
#
# Toute étape qui échoue stoppe le script (set -e). Lancer avant tout merge
# sur main pour garantir que la branche dev n'introduit aucune régression.

set -e

cd "$(dirname "$0")"

echo "▸ ruff check ."
ruff check .

echo "▸ black --check ."
black --check .

echo "▸ pytest -x"
pytest -x

echo
echo "✅ All checks passed"
