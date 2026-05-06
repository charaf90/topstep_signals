"""
Runner universel : exécute n'importe quelle stratégie du dossier strategies/.

Contrat attendu de chaque module stratégie :
    STRATEGY_ID : str                    — tag de version (ex. "opr-v4")
    TICKERS     : list[str]              — actifs supportés
    run_backtest(df_15m, ticker,
                 tf=None, params=None,
                 topstep_guard=True) -> pd.DataFrame

Optionnel (chart) :
    plot_day(df_15m, ticker, date_str,
             day_trades, output_path)    — chart d'une journée
"""

import random
from pathlib import Path
from typing import Optional

import pandas as pd

from config import INSTRUMENTS
from core import metrics as m


# ══════════════════════════════════════════════════════════════════════════════
# Runner principal
# ══════════════════════════════════════════════════════════════════════════════

def run_for_ticker(
    strategy,
    df_15m: pd.DataFrame,
    ticker: str,
    tf: Optional[dict] = None,
    params: Optional[dict] = None,
    topstep_guard: bool = True,
    plot: bool = False,
    n_sample_charts: int = 10,
    output_dir: Optional[Path] = None,
    verbose: bool = True,
) -> dict:
    """
    Exécute une stratégie sur un ticker et retourne ses métriques.

    Args:
        strategy        : module strategies/xxx.py
        df_15m          : DataFrame OHLCV 15m
        ticker          : "MES1" | "NQ1" | "YM1"
        tf              : dict de timeframes pré-construits (optionnel)
        params          : dict de params pour override config (optimisation)
        topstep_guard   : activer le garde-fou Topstep dans la boucle jour
        plot            : générer des graphiques sur N jours aléatoires
        n_sample_charts : nombre de jours à tracer (défaut 10)
        output_dir      : répertoire de sortie (CSV + charts)
        verbose         : afficher le rapport

    Returns:
        {"df_trades": pd.DataFrame, "stats": dict, "topstep": dict,
         "ticker": str, "strategy_id": str}
    """
    strategy_id = getattr(strategy, "STRATEGY_ID", "unknown")

    if verbose:
        print(f"\n{'='*60}")
        print(f"  BACKTEST — {ticker}  [{strategy_id}]")
        print(f"{'='*60}")
        print(f"  {len(df_15m):,} bougies "
              f"[{df_15m.index.min()} → {df_15m.index.max()}]")

    df_trades = strategy.run_backtest(
        df_15m, ticker, tf=tf, params=params, topstep_guard=topstep_guard
    )

    stats   = m.compute_stats(df_trades)
    topstep = m.compute_topstep(df_trades, n_bootstrap=1000)

    if verbose:
        m.print_stats_report(stats, ticker, strategy_id)
        m.print_topstep_report(topstep, ticker)

    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        suffix = getattr(strategy, "CSV_SUFFIX", f"_{strategy_id.replace('-','_')}")
        csv_path = output_dir / f"backtest_{ticker}{suffix}.csv"
        df_trades.to_csv(csv_path, index=False)
        if verbose:
            print(f"  ✓ {csv_path}")

    if plot and len(df_trades) > 0:
        _generate_sample_charts(
            strategy, df_15m, ticker, df_trades,
            output_dir=output_dir,
            n=n_sample_charts,
            verbose=verbose,
        )

    return {
        "df_trades":   df_trades,
        "stats":       stats,
        "topstep":     topstep,
        "ticker":      ticker,
        "strategy_id": strategy_id,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Charts sur N jours aléatoires
# ══════════════════════════════════════════════════════════════════════════════

def _generate_sample_charts(
    strategy,
    df_15m: pd.DataFrame,
    ticker: str,
    df_trades: pd.DataFrame,
    output_dir: Optional[Path],
    n: int = 10,
    verbose: bool = True,
):
    """Génère des charts sur N jours tirés aléatoirement parmi les jours avec fills."""
    if not hasattr(strategy, "plot_day"):
        return

    filled = df_trades[df_trades["result"] != "NOT_FILLED"] \
             if "result" in df_trades.columns else df_trades
    if len(filled) == 0:
        return

    filled_dates = sorted(filled["date"].unique())
    sample_dates = random.sample(filled_dates, min(n, len(filled_dates)))
    sample_dates.sort()

    chart_dir = (output_dir or Path("output")) / "charts" / ticker
    chart_dir.mkdir(parents=True, exist_ok=True)

    if verbose:
        print(f"\n  ▸ Génération de {len(sample_dates)} chart(s) "
              f"[{ticker}] → {chart_dir}")

    for date_str in sample_dates:
        day_trades = df_trades[df_trades["date"] == date_str].to_dict("records")
        out_path   = str(chart_dir / f"{date_str}.png")
        try:
            strategy.plot_day(df_15m, ticker, date_str, day_trades, out_path)
        except Exception as e:
            if verbose:
                print(f"  [!] chart {date_str}: {e}")

    if verbose:
        print(f"  ✓ {len(sample_dates)} chart(s) → {chart_dir}")
