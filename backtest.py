#!/usr/bin/env python3
"""
Backtest standardisé — toutes stratégies (plug-and-play).

Toute stratégie placée dans `strategies/<nom>.py` est automatiquement
disponible via `--strategy <nom>` (cf. core/registry.py).

Usage :
  python backtest.py --strategy opr --csv-dir ./data
  python backtest.py --strategy fib --csv-dir ./data --ticker NQ1
  python backtest.py --strategy all --csv-dir ./data --plot
  python backtest.py --strategy opr --csv-dir ./data --portfolio-charts
  python backtest.py --strategy opr --live --bars 15000
  python backtest.py --list                    # liste les stratégies trouvées
"""

import argparse
from pathlib import Path

import pandas as pd

from config import INSTRUMENTS
from core.data import load_csv, build_timeframes
try:
    from core.data import fetch_live
except ImportError:
    fetch_live = None
from core import backtester, metrics as m
from core.registry import discover_strategies, load_strategy, list_strategy_names


def main():
    parser = argparse.ArgumentParser(description="Backtest standardisé (plug-and-play)")
    parser.add_argument("--list", action="store_true",
                        help="Liste les stratégies disponibles et quitte")

    src = parser.add_mutually_exclusive_group()
    src.add_argument("--csv-dir", type=str, help="Répertoire CSV 15m")
    src.add_argument("--live",    action="store_true",
                     help="Données live TradingView")
    parser.add_argument("--bars",     type=int,  default=10_000,
                        help="Nombre de barres live (défaut 10 000)")
    parser.add_argument("--strategy", type=str,  default=None,
                        help="Stratégie à backtester (ou 'all')")
    parser.add_argument("--ticker",   type=str,  default=None,
                        help="Actif unique (défaut: tous)")
    parser.add_argument("--plot",     action="store_true",
                        help="Générer N charts par jour aléatoire (plot_day)")
    parser.add_argument("--n-charts", type=int, default=10,
                        help="Nombre de charts à générer (défaut 10)")
    parser.add_argument("--portfolio-charts", action="store_true",
                        help="Générer les charts portefeuille (equity, DD, heatmap, hourly)")
    parser.add_argument("--output-dir", type=str, default="./output")
    args = parser.parse_args()

    available = discover_strategies()

    if args.list:
        print("Stratégies disponibles :")
        for n in sorted(available):
            print(f"  • {n:<10}  →  {available[n]}")
        return

    if not args.strategy:
        parser.error("--strategy est requis (ou utilise --list)")
    if not args.csv_dir and not args.live:
        parser.error("--csv-dir ou --live est requis")

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

    tickers    = [args.ticker]  if args.ticker    else list(INSTRUMENTS)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    results_by_strategy = {}

    for strat_name in strategy_names:
        module  = load_strategy(strat_name)
        results = []

        for ticker in tickers:
            if ticker not in getattr(module, "TICKERS", tickers):
                continue

            # Chargement des données
            if args.live:
                if fetch_live is None:
                    print(f"  [!] fetch_live indisponible — skip {ticker}")
                    continue
                df_15m = fetch_live(ticker, args.bars)
                if df_15m is None or len(df_15m) == 0:
                    print(f"  [!] {ticker}: données live indisponibles")
                    continue
            else:
                tf_suffix = getattr(module, "CSV_TIMEFRAME", "m15")
                csv_path = Path(args.csv_dir) / f"{ticker}_data_{tf_suffix}.csv"
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

    # Charts portefeuille mutualisés (--portfolio-charts)
    if args.portfolio_charts:
        for sid, res in results_by_strategy.items():
            parts = []
            for r in res:
                df = r["df_trades"]
                if df is None or len(df) == 0:
                    continue
                df = df.copy()
                if "ticker" not in df.columns:
                    df["ticker"] = r["ticker"]
                parts.append(df)
            if not parts:
                continue
            combined = pd.concat(parts, ignore_index=True)
            paths = backtester.generate_portfolio_charts(
                trades_df = combined,
                strategy_id = sid,
                output_dir  = output_dir,
            )
            print(f"\n  ▸ Charts portefeuille [{sid}] :")
            for k, p in paths.items():
                print(f"    • {k:<12} → {p}")

    print(f"\n{'='*60}")
    print(f"  ✅ BACKTEST TERMINÉ")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
