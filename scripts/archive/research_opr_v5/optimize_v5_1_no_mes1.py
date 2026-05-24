#!/usr/bin/env python3
"""
Re-optimisation walk-forward d'opr-v5.1 sur NQ1+YM1 uniquement (MES1 exclu).

Motivation :
  Le rapport opr-v5.1 (verdict 🟡) identifie MES1 comme le seul ticker
  empêchant 🟢 : Bootstrap Topstep MES1 = 0 % (n=29 OOS, p=0.23 ML).
  Recommandation explicite §7.2 : promotion sélective NQ1+YM1, MES1 sur v4.

But du script :
  Tester si, en excluant MES1, le portfolio NQ1+YM1 passe BS Topstep ≥ 80 %
  per-ticker ET portfolio agrégé — ce qui débloquerait le verdict 🟢.

Méthode :
  - Charge uniquement NQ1 et YM1
  - Patche temporairement strategies.opr_v5_1.TICKERS = ["NQ1", "YM1"]
  - Lance optimize() standard
  - Outputs dans output/no_mes1/ pour préserver les artefacts v5.1 actuels

Usage :
    python -m scripts.optimize_v5_1_no_mes1 --csv-dir ./data
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np

np.random.seed(42)

from core.data import build_timeframes, load_csv
from core.optimizer import optimize
from core.registry import load_strategy

SELECTED_TICKERS = ["NQ1", "YM1"]


def main():
    parser = argparse.ArgumentParser(
        description="Optimisation v5.1 sans MES1 (promotion sélective)"
    )
    parser.add_argument("--csv-dir", type=str, required=True)
    parser.add_argument("--is-end", type=str, default="2025-09-30")
    parser.add_argument("--oos-start", type=str, default="2025-10-01")
    parser.add_argument(
        "--output-dir",
        type=str,
        default="output/no_mes1",
        help="Dossier séparé pour ne pas écraser les artefacts v5.1 actuels",
    )
    args = parser.parse_args()

    # Charger module v5.1 et patcher sa liste de tickers
    module = load_strategy("opr_v5_1")
    module.TICKERS = list(SELECTED_TICKERS)

    # Charger uniquement NQ1 + YM1
    data = {}
    for ticker in SELECTED_TICKERS:
        csv_path = Path(args.csv_dir) / f"{ticker}_data_m15.csv"
        if not csv_path.exists():
            print(f"  [!] {csv_path} introuvable")
            continue
        df_15m = load_csv(str(csv_path))
        tf = build_timeframes(df_15m)
        data[ticker] = (df_15m, tf)

    if not data:
        print("  [!] Aucune donnée chargée — abandon")
        return

    print(f"\n  Tickers retenus : {list(data.keys())} (MES1 exclu volontairement)")
    print(f"  Output dir      : {args.output_dir}")
    print(f"  Strategy_id     : {module.STRATEGY_ID}")

    Path(args.output_dir).mkdir(parents=True, exist_ok=True)

    optimize(
        module,
        data,
        is_end=args.is_end,
        oos_start=args.oos_start,
        robustness_report=True,
        output_dir=args.output_dir,
    )

    print("\n  ✓ Artefacts générés dans", args.output_dir)
    print("  → robustness_opr-v5.1.json + .md")


if __name__ == "__main__":
    main()
