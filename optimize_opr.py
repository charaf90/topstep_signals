#!/usr/bin/env python3
"""
Optimisation rapide du Risk-Reward de la stratégie OPR par actif.

Sweep linéaire de OPR_RR sur la période complète disponible (split walk-forward
IS / OOS pour limiter l'overfit). Les paramètres SL ne sont pas balayés ici :
le SL est structurellement déterminé par la zone OPR (autre côté de la zone
+ buffer + min asset). Seul le TP (= sl_dist × RR) est libre.

Usage :
    python optimize_opr.py --csv-dir ./data
    python optimize_opr.py --csv-dir ./data --ticker NQ1
    python optimize_opr.py --csv-dir ./data --rr 0.75,1.0,1.25,1.5,2.0,2.5

Le script imprime un tableau IS / OOS par actif et propose la meilleure
valeur de RR (validée si OOS PF ≥ 1.2 et n_trades OOS ≥ 8). Il NE modifie
pas config.py — vous reportez la valeur retenue à la main.
"""

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from config import INSTRUMENTS, OPR_RR
from core.data import load_csv, build_timeframes
from backtest import run_opr_backtest


# Split walk-forward (cohérent avec optimize.py)
IS_END = "2025-09-30"


def _stats(df_trades: pd.DataFrame) -> dict:
    if len(df_trades) == 0 or "result" not in df_trades.columns:
        return {"n": 0, "wr": 0.0, "pf": 0.0, "pnl": 0.0, "dd": 0.0}
    f = df_trades[df_trades["result"] != "NOT_FILLED"]
    if len(f) == 0:
        return {"n": 0, "wr": 0.0, "pf": 0.0, "pnl": 0.0, "dd": 0.0}
    wins = f[f["pnl"] > 0]
    losses = f[f["pnl"] <= 0]
    gp = wins["pnl"].sum() if len(wins) else 0
    gl = abs(losses["pnl"].sum()) if len(losses) else 1
    cum = f["pnl"].cumsum()
    return {
        "n": len(f),
        "wr": len(wins) / len(f) * 100,
        "pf": gp / gl if gl > 0 else 0,
        "pnl": float(f["pnl"].sum()),
        "dd": float((cum - cum.cummax()).min()),
    }


def _split_trades(df_trades: pd.DataFrame, is_end: str):
    if len(df_trades) == 0 or "date" not in df_trades.columns:
        return df_trades, df_trades
    is_mask = df_trades["date"] <= is_end
    return df_trades[is_mask], df_trades[~is_mask]


def optimize_ticker(df_15m, tf, ticker: str, rr_grid, is_end: str = IS_END):
    print(f"\n{'='*70}")
    print(f"  OPR optimisation — {ticker} ({len(rr_grid)} valeurs RR)")
    print(f"{'='*70}")
    print(f"  {'RR':>6}  {'IS_n':>5} {'IS_PF':>6} {'IS_PnL':>9}   "
          f"{'OOS_n':>5} {'OOS_PF':>7} {'OOS_PnL':>9}  {'OOS_DD':>9}")

    best = None
    rows = []
    for rr in rr_grid:
        df_trades = run_opr_backtest(df_15m, tf, ticker, rr=rr,
                                     analysis_chart_dir=None)
        is_t, oos_t = _split_trades(df_trades, is_end)
        is_s = _stats(is_t)
        oos_s = _stats(oos_t)
        rows.append((rr, is_s, oos_s))

        valid = oos_s["pf"] >= 1.2 and oos_s["n"] >= 8 and oos_s["pnl"] > 0
        flag = "✅" if valid else "  "
        print(f"  {rr:>6.2f}  {is_s['n']:>5} {is_s['pf']:>6.2f} ${is_s['pnl']:>+8.0f}   "
              f"{oos_s['n']:>5} {oos_s['pf']:>7.2f} ${oos_s['pnl']:>+8.0f}  ${oos_s['dd']:>+8.0f} {flag}")

        if valid:
            score = oos_s["pf"] * oos_s["pnl"]  # privilégie PF + PnL OOS
            if best is None or score > best[0]:
                best = (score, rr, is_s, oos_s)

    if best:
        _, rr, is_s, oos_s = best
        print(f"\n  ➜ Meilleur RR validé OOS : {rr:.2f}  "
              f"(IS PF={is_s['pf']:.2f} P&L=${is_s['pnl']:+.0f}  |  "
              f"OOS PF={oos_s['pf']:.2f} P&L=${oos_s['pnl']:+.0f}, n={oos_s['n']})")
    else:
        print(f"\n  ⚠ Aucun RR ne valide IS/OOS (PF≥1.2, n≥8, PnL>0). "
              f"Conserver la valeur par défaut OPR_RR={OPR_RR}.")

    return best, rows


def main():
    parser = argparse.ArgumentParser(description="Optimisation OPR_RR par actif")
    parser.add_argument("--csv-dir", type=str, required=True)
    parser.add_argument("--ticker", type=str, default=None)
    parser.add_argument("--rr", type=str,
                        default="0.75,1.0,1.25,1.5,1.75,2.0,2.5,3.0",
                        help="Liste de valeurs RR séparées par virgule")
    parser.add_argument("--is-end", type=str, default=IS_END,
                        help="Date de fin de la période IS (YYYY-MM-DD)")
    args = parser.parse_args()

    rr_grid = [float(x.strip()) for x in args.rr.split(",")]
    tickers = [args.ticker] if args.ticker else list(INSTRUMENTS.keys())

    summary = {}
    for ticker in tickers:
        csv_path = Path(args.csv_dir) / f"{ticker}_data_m15.csv"
        if not csv_path.exists():
            print(f"  [!] {csv_path} introuvable")
            continue
        df_15m = load_csv(str(csv_path))
        tf = build_timeframes(df_15m)
        best, _ = optimize_ticker(df_15m, tf, ticker, rr_grid, is_end=args.is_end)
        if best:
            summary[ticker] = best[1]

    if summary:
        print(f"\n{'='*70}")
        print(f"  RÉSUMÉ — RR retenu par actif (à reporter dans config.py)")
        print(f"{'='*70}")
        for t, rr in summary.items():
            print(f"  {t}: OPR_RR_{t} = {rr}")


if __name__ == "__main__":
    main()
