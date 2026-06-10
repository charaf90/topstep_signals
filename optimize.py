#!/usr/bin/env python3
"""
Optimisation walk-forward standardisée — toutes stratégies (plug-and-play).

Toute stratégie placée dans `strategies/<nom>.py` est automatiquement
disponible via `--strategy <nom>` (cf. core/registry.py).

Usage :
  python optimize.py --strategy opr --csv-dir ./data
  python optimize.py --strategy fib --csv-dir ./data --ticker NQ1
  python optimize.py --strategy all --csv-dir ./data
  python optimize.py --list
"""

import argparse
from pathlib import Path

from config import INSTRUMENTS
from core.data import build_timeframes, load_csv
from core.optimizer import HOLDOUT_START, IS_END, OOS_START, optimize
from core.registry import discover_strategies, load_strategy
from core.walkforward import run_multifold


def main():
    parser = argparse.ArgumentParser(description="Optimisation walk-forward (plug-and-play)")
    parser.add_argument(
        "--list", action="store_true", help="Liste les stratégies disponibles et quitte"
    )
    parser.add_argument("--csv-dir", type=str, default=None)
    parser.add_argument(
        "--strategy", type=str, default=None, help="Stratégie à optimiser (ou 'all')"
    )
    parser.add_argument("--ticker", type=str, default=None)
    parser.add_argument(
        "--is-end", type=str, default=IS_END, help=f"Fin de la période IS (défaut {IS_END})"
    )
    parser.add_argument(
        "--oos-start",
        type=str,
        default=OOS_START,
        help=f"Début de la période OOS (défaut {OOS_START})",
    )
    parser.add_argument(
        "--oos-end",
        type=str,
        default=HOLDOUT_START,
        help=f"Fin EXCLUSIVE de l'OOS (défaut {HOLDOUT_START} = hold-out exclu ; "
        "'none' = OOS jusqu'au bout, ancien comportement)",
    )
    parser.add_argument(
        "--holdout",
        action="store_true",
        help="Évalue UNE fois les params retenus sur le hold-out terminal "
        "(pré-promotion uniquement — chaque consultation le consomme)",
    )
    parser.add_argument(
        "--multifold",
        action="store_true",
        help="Walk-forward multi-folds ancré (stabilité inter-folds + OOS recousu) "
        "au lieu du split unique",
    )
    parser.add_argument(
        "--no-robustness", action="store_true", help="Ne pas générer le rapport de robustesse"
    )
    parser.add_argument("--output-dir", type=str, default="output")
    args = parser.parse_args()

    available = discover_strategies()

    if args.list:
        print("Stratégies disponibles :")
        for n in sorted(available):
            print(f"  • {n:<10}  →  {available[n]}")
        return

    if not args.strategy:
        parser.error("--strategy est requis (ou utilise --list)")
    if not args.csv_dir:
        parser.error("--csv-dir est requis")

    if args.strategy == "all":
        strategy_names = sorted(available)
    elif args.strategy in available:
        strategy_names = [args.strategy]
    else:
        parser.error(
            f"Stratégie inconnue : '{args.strategy}'. "
            f"Disponibles : {', '.join(sorted(available)) or '(aucune)'} ou 'all'"
        )
        return

    tickers = [args.ticker] if args.ticker else list(INSTRUMENTS)

    for strat_name in strategy_names:
        module = load_strategy(strat_name)
        data = {}

        for ticker in tickers:
            if ticker not in getattr(module, "TICKERS", tickers):
                continue
            tf_suffix = getattr(module, "CSV_TIMEFRAME", "m15")
            csv_path = Path(args.csv_dir) / f"{ticker}_data_{tf_suffix}.csv"
            if not csv_path.exists():
                print(f"  [!] {csv_path} introuvable")
                continue
            df_15m = load_csv(str(csv_path))
            tf = build_timeframes(df_15m)
            data[ticker] = (df_15m, tf)

        if not data:
            print(f"  [!] Aucune donnée disponible pour {strat_name}")
            continue

        if args.multifold:
            run_multifold(module, data, output_dir=args.output_dir)
            continue

        oos_end = None if str(args.oos_end).lower() == "none" else args.oos_end
        optimize(
            module,
            data,
            is_end=args.is_end,
            oos_start=args.oos_start,
            oos_end=oos_end,
            evaluate_holdout=args.holdout,
            robustness_report=not args.no_robustness,
            output_dir=args.output_dir,
        )


if __name__ == "__main__":
    main()
