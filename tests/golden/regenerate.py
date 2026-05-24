#!/usr/bin/env python3
"""Régénère les fichiers golden_master JSON.

À lancer manuellement uniquement quand un changement de stratégie est
volontaire et que les golden masters doivent être mis à jour :

    python tests/golden/regenerate.py

Toujours commit les baselines régénérés dans un commit dédié, séparé
des changements de stratégie, pour faciliter la review du diff.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Permettre l'import depuis la racine du projet
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests._golden_helpers import build_baseline  # noqa: E402

GOLDEN_DIR = Path(__file__).resolve().parent

# Liste des stratégies à figer (toutes celles utilisées en prod ou prêtes à l'être).
# Ne pas réordonner : l'ordre n'a pas d'impact, mais on garde l'historique stable.
STRATEGIES = ["opr", "opr_v5_1", "fib_v4"]


def main() -> None:
    print(f"Régénération des golden masters dans {GOLDEN_DIR}")
    for name in STRATEGIES:
        baseline = build_baseline(name)
        out_path = GOLDEN_DIR / f"{name}_baseline.json"
        out_path.write_text(json.dumps(baseline, sort_keys=True, indent=2) + "\n")
        n_total = sum(t.get("n_total", 0) for t in baseline["tickers"].values())
        sum_pnl = sum(t.get("sum_pnl", 0.0) for t in baseline["tickers"].values())
        print(f"  ✓ {name:<12} → {out_path.name}  ({n_total} trades, P&L=${sum_pnl:+,.2f})")


if __name__ == "__main__":
    main()
