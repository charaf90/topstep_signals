#!/usr/bin/env python3
"""
Backtest standardisé — toutes stratégies.

Usage :
  python backtest.py --strategy opr --csv-dir ./data
  python backtest.py --strategy fib --csv-dir ./data --ticker NQ1
  python backtest.py --strategy all --csv-dir ./data --plot
  python backtest.py --strategy opr --live --bars 15000
"""

import argparse
import importlib
from pathlib import Path

from config import INSTRUMENTS
from core.data import load_csv, build_timeframes
try:
    from core.data import fetch_live
except ImportError:
    fetch_live = None
from core import backtester, metrics as m

# ── Registre des stratégies ──────────────────────────────────────────────────
REGISTRY = {
    "opr":         "strategies.opr",
    "fib":         "strategies.fib",
    "smc":         "strategies.smc",
    "fib_explore": "strategies.fib_explore",
}


def main():
    parser = argparse.ArgumentParser(description="Backtest standardisé")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv-dir", type=str, help="Répertoire CSV 15m")
    src.add_argument("--live",    action="store_true",
                     help="Données live TradingView")
    parser.add_argument("--bars",     type=int,  default=10_000,
                        help="Nombre de barres live (défaut 10 000)")
    parser.add_argument("--strategy", type=str,  required=True,
                        choices=list(REGISTRY) + ["all"],
                        help="Stratégie à backtester")
    parser.add_argument("--ticker",   type=str,  default=None,
                        help="Actif unique (défaut: tous)")
    parser.add_argument("--plot",     action="store_true",
                        help="Générer 10 charts sur des jours aléatoires")
    parser.add_argument("--n-charts", type=int, default=10,
                        help="Nombre de charts à générer (défaut 10)")
    parser.add_argument("--output-dir", type=str, default="./output")
    args = parser.parse_args()

    strategies = list(REGISTRY) if args.strategy == "all" else [args.strategy]
    tickers    = [args.ticker]  if args.ticker    else list(INSTRUMENTS)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    results_by_strategy = {}

    for strat_name in strategies:
        module  = importlib.import_module(REGISTRY[strat_name])
        results = []

        for ticker in tickers:
            if ticker not in getattr(module, "TICKERS", tickers):
                continue

            # Chargement des données
            if args.live:
                df_15m = fetch_live(ticker, args.bars)
                if df_15m is None or len(df_15m) == 0:
                    print(f"  [!] {ticker}: données live indisponibles")
                    continue
            else:
                csv_path = Path(args.csv_dir) / f"{ticker}_data_m15.csv"
                if not csv_path.exists():
                    print(f"  [!] Fichier introuvable: {csv_path}")
                    continue
                df_15m = load_csv(str(csv_path))

            tf = build_timeframes(df_15m)

            res = backtester.run_for_ticker(
                module, df_15m, ticker, tf=tf,
                plot=args.plot,
                n_sample_charts=args.n_charts,
                output_dir=output_dir,
            )
            results.append(res)

        results_by_strategy[module.STRATEGY_ID] = results

    # Rapport portefeuille
    portfolio_input = {
        sid: [{"df_trades": r["df_trades"]} for r in res]
        for sid, res in results_by_strategy.items()
    }
    m.print_portfolio_report(portfolio_input)

    print(f"\n{'='*60}")
    print(f"  ✅ BACKTEST TERMINÉ")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
