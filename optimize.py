#!/usr/bin/env python3
"""
Optimisation walk-forward standardisée — toutes stratégies.

Usage :
  python optimize.py --strategy opr --csv-dir ./data
  python optimize.py --strategy fib --csv-dir ./data --ticker NQ1
  python optimize.py --strategy all --csv-dir ./data
"""

import argparse
import importlib
from pathlib import Path

from config import INSTRUMENTS
from core.data import load_csv, build_timeframes
from core.optimizer import optimize

# ── Registre (identique à backtest.py) ──────────────────────────────────────
REGISTRY = {
    "opr":         "strategies.opr",
    "fib":         "strategies.fib",
    "smc":         "strategies.smc",
    "fib_explore": "strategies.fib_explore",
}


def main():
    parser = argparse.ArgumentParser(description="Optimisation walk-forward")
    parser.add_argument("--csv-dir",  type=str, required=True)
    parser.add_argument("--strategy", type=str, required=True,
                        choices=list(REGISTRY) + ["all"])
    parser.add_argument("--ticker",   type=str, default=None)
    parser.add_argument("--is-end",   type=str, default="2025-09-30",
                        help="Fin de la période IS (YYYY-MM-DD)")
    parser.add_argument("--oos-start",type=str, default="2025-10-01",
                        help="Début de la période OOS (YYYY-MM-DD)")
    args = parser.parse_args()

    strategies = list(REGISTRY) if args.strategy == "all" else [args.strategy]
    tickers    = [args.ticker]  if args.ticker    else list(INSTRUMENTS)

    for strat_name in strategies:
        module = importlib.import_module(REGISTRY[strat_name])
        data   = {}

        for ticker in tickers:
            if ticker not in getattr(module, "TICKERS", tickers):
                continue
            csv_path = Path(args.csv_dir) / f"{ticker}_data_m15.csv"
            if not csv_path.exists():
                print(f"  [!] {csv_path} introuvable")
                continue
            df_15m = load_csv(str(csv_path))
            tf     = build_timeframes(df_15m)
            data[ticker] = (df_15m, tf)

        if not data:
            print(f"  [!] Aucune donnée disponible pour {strat_name}")
            continue

        optimize(module, data, is_end=args.is_end, oos_start=args.oos_start)


if __name__ == "__main__":
    main()
