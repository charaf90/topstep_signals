#!/usr/bin/env python3
"""
Optimisation focalisée du paramètre PIVOT (left = right) par ticker.

Utilise les meilleurs (SL, TP, IMP) déjà calibrés via optimize.py et sweep
PIVOT_LEFT/RIGHT ∈ {5, 6, 8, 10, 12} pour identifier la résolution optimale
de détection des swings par ticker.

Usage :
    python optimize_pivot.py --csv-dir ../data
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from config import (
    INSTRUMENTS, IS_END,
    SL_ATR_MULT_PER_TICKER, TP_ATR_MULT_PER_TICKER, MIN_IMPULSE_ATR_PER_TICKER,
)
from backtest import run_backtest, stats, load_csv

PIVOT_GRID = [5, 6, 8, 10, 12]

OOS_SHARPE_MIN = 0.5
OOS_PF_MIN = 1.2
OOS_N_MIN = 8
IS_N_MIN = 10


def optimize_pivot_for_ticker(df: pd.DataFrame, ticker: str) -> dict:
    """Sweep PIVOT_LEFT=PIVOT_RIGHT pour un ticker, SL/TP/IMP fixés."""
    sl = SL_ATR_MULT_PER_TICKER[ticker]
    tp = TP_ATR_MULT_PER_TICKER[ticker]
    imp = MIN_IMPULSE_ATR_PER_TICKER[ticker]

    print(f"\n{'='*92}")
    print(f"  PIVOT SWEEP — {ticker}  "
          f"(SL={sl}  TP={tp}  IMP={imp})")
    print(f"{'='*92}")
    print(f"  {'PIVOT':>6}  "
          f"{'IS_n':>5} {'IS_PF':>6} {'IS_Sh':>6} {'IS_PnL':>9}   "
          f"{'OOS_n':>5} {'OOS_PF':>7} {'OOS_Sh':>7} {'OOS_PnL':>9}")

    rows = []
    best = None
    for piv in PIVOT_GRID:
        overrides = {
            "SL_ATR_MULT": sl, "TP_ATR_MULT": tp, "MIN_IMPULSE_ATR": imp,
            "PIVOT_LEFT": piv, "PIVOT_RIGHT": piv,
        }
        trades = run_backtest(df, ticker, overrides)
        if len(trades) == 0:
            continue

        trades["date"] = trades["fill_time"].astype(str).str[:10]
        is_t = trades[trades["date"] <= IS_END]
        oos_t = trades[trades["date"] > IS_END]
        if len(is_t) < IS_N_MIN:
            continue

        s_is = stats(is_t)
        s_oos = stats(oos_t)
        valid = (s_oos["sharpe"] >= OOS_SHARPE_MIN
                 and s_oos["pf"] >= OOS_PF_MIN
                 and s_oos["n"] >= OOS_N_MIN
                 and s_oos["pnl_total"] > 0)
        flag = "OK" if valid else "  "
        print(f"  {piv:>6d}  "
              f"{s_is['n']:>5} {s_is['pf']:>6.2f} {s_is['sharpe']:>6.2f} "
              f"${s_is['pnl_total']:>+8.0f}   "
              f"{s_oos['n']:>5} {s_oos['pf']:>7.2f} {s_oos['sharpe']:>7.2f} "
              f"${s_oos['pnl_total']:>+8.0f}  {flag}")

        row = {
            "pivot": piv,
            "is_n": s_is["n"], "is_pf": s_is["pf"], "is_sharpe": s_is["sharpe"],
            "is_pnl": s_is["pnl_total"],
            "oos_n": s_oos["n"], "oos_pf": s_oos["pf"], "oos_sharpe": s_oos["sharpe"],
            "oos_pnl": s_oos["pnl_total"], "oos_dd": s_oos["max_dd"],
            "valid_oos": bool(valid),
        }
        rows.append(row)
        if valid and (best is None or s_is["sharpe"] > best["is_sharpe"]):
            best = row

    if best:
        print(f"\n  ➜ Meilleur PIVOT validé OOS : {best['pivot']}")
        print(f"     IS Sharpe={best['is_sharpe']:.2f}  "
              f"OOS Sharpe={best['oos_sharpe']:.2f}  "
              f"OOS PF={best['oos_pf']:.2f}  "
              f"OOS P&L=${best['oos_pnl']:+.0f}")
    else:
        print(f"\n  /!\\ Aucun PIVOT ne valide OOS.")

    return {"results": rows, "best": best}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv-dir", type=str, default="../data")
    parser.add_argument("--ticker", type=str, default=None)
    args = parser.parse_args()

    out_dir = Path(__file__).parent / "output"
    out_dir.mkdir(parents=True, exist_ok=True)

    tickers = [args.ticker] if args.ticker else list(INSTRUMENTS.keys())
    summary = {}
    for ticker in tickers:
        csv_path = Path(args.csv_dir) / f"{ticker}_data_m15.csv"
        if not csv_path.exists():
            continue
        df = load_csv(str(csv_path))
        result = optimize_pivot_for_ticker(df, ticker)
        if result["best"]:
            summary[ticker] = result["best"]
        if result["results"]:
            pd.DataFrame(result["results"]).to_csv(
                out_dir / f"pivot_sweep_{ticker}.csv", index=False
            )

    print(f"\n{'='*92}")
    print(f"  RÉSUMÉ — Meilleur PIVOT par ticker")
    print(f"{'='*92}")
    for t, b in summary.items():
        print(f"  {t}: PIVOT={b['pivot']}  "
              f"IS Sharpe={b['is_sharpe']:.2f}  "
              f"OOS Sharpe={b['oos_sharpe']:.2f}  "
              f"OOS P&L=${b['oos_pnl']:+.0f}")


if __name__ == "__main__":
    main()
