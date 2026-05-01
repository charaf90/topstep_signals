#!/usr/bin/env python3
"""
Optimisation walk-forward des paramètres clés (SL_ATR_MULT × TP_ATR_MULT × MIN_IMPULSE_ATR).

Calibration sur IS uniquement (≤ 2025-09-30), évaluation aveugle sur OOS.
Critère de validation OOS : Sharpe ≥ 0.5, PF ≥ 1.2, n_trades ≥ 8, P&L > 0.
Score de sélection : IS Sharpe maximal parmi les combos OOS-validés.

Usage :
    python optimize.py --csv-dir ../data
    python optimize.py --csv-dir ../data --ticker MES1
"""

import argparse
import sys
from itertools import product
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from config import INSTRUMENTS, IS_END
from backtest import run_backtest, stats, load_csv


# ─────────────────────────────────────────────────────────────────────────────
# Grille de recherche par défaut (compromis exhaustivité / temps de calcul)
# ─────────────────────────────────────────────────────────────────────────────

SL_GRID = [0.75, 1.0, 1.25, 1.5]
TP_GRID = [1.5, 2.0, 2.5, 3.0]
IMPULSE_GRID = [1.0, 1.5, 2.0]

# Critères de validation OOS
OOS_SHARPE_MIN = 0.5
OOS_PF_MIN = 1.2
OOS_N_MIN = 8
OOS_PNL_MIN = 0.0
IS_N_MIN = 10


def optimize_ticker(df: pd.DataFrame, ticker: str) -> dict:
    """
    Balaye la grille (SL × TP × IMP) pour un ticker.
    Retourne {"results": [...], "best": dict|None}.
    """
    print(f"\n{'='*92}")
    print(f"  OPTIMISATION FIB-50 — {ticker}  "
          f"({len(SL_GRID)} SL × {len(TP_GRID)} TP × {len(IMPULSE_GRID)} IMP)")
    print(f"{'='*92}")
    print(f"  {'SL':>5} {'TP':>5} {'IMP':>5} {'RR':>5}  "
          f"{'IS_n':>5} {'IS_PF':>6} {'IS_Sh':>6} {'IS_PnL':>9}   "
          f"{'OOS_n':>5} {'OOS_PF':>7} {'OOS_Sh':>7} {'OOS_PnL':>9}")

    results = []
    best = None

    for sl, tp, imp in product(SL_GRID, TP_GRID, IMPULSE_GRID):
        if tp / sl < 1.0:  # filtre RR < 1 (pas pertinent en pullback)
            continue

        overrides = {
            "SL_ATR_MULT": float(sl),
            "TP_ATR_MULT": float(tp),
            "MIN_IMPULSE_ATR": float(imp),
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

        valid = (
            s_oos["sharpe"] >= OOS_SHARPE_MIN
            and s_oos["pf"] >= OOS_PF_MIN
            and s_oos["n"] >= OOS_N_MIN
            and s_oos["pnl_total"] > OOS_PNL_MIN
        )
        flag = "OK" if valid else "  "

        print(f"  {sl:>5.2f} {tp:>5.2f} {imp:>5.2f} {tp/sl:>5.2f}  "
              f"{s_is['n']:>5} {s_is['pf']:>6.2f} {s_is['sharpe']:>6.2f} "
              f"${s_is['pnl_total']:>+8.0f}   "
              f"{s_oos['n']:>5} {s_oos['pf']:>7.2f} {s_oos['sharpe']:>7.2f} "
              f"${s_oos['pnl_total']:>+8.0f}  {flag}")

        row = {
            "sl": float(sl), "tp": float(tp), "imp": float(imp), "rr": float(tp / sl),
            "is_n": s_is["n"], "is_pf": s_is["pf"], "is_sharpe": s_is["sharpe"],
            "is_pnl": s_is["pnl_total"], "is_dd": s_is["max_dd"],
            "oos_n": s_oos["n"], "oos_pf": s_oos["pf"], "oos_sharpe": s_oos["sharpe"],
            "oos_pnl": s_oos["pnl_total"], "oos_dd": s_oos["max_dd"],
            "valid_oos": bool(valid),
        }
        results.append(row)

        if valid:
            score = s_is["sharpe"]   # maximise IS Sharpe parmi validés OOS
            if best is None or score > best["score"]:
                best = {"score": score, **row}

    if best:
        print(f"\n  ➜ Meilleure combinaison validée OOS : "
              f"SL={best['sl']:.2f}  TP={best['tp']:.2f}  IMP={best['imp']:.2f}  "
              f"(RR={best['rr']:.2f})")
        print(f"     IS  Sharpe={best['is_sharpe']:.2f}  PF={best['is_pf']:.2f}  "
              f"P&L=${best['is_pnl']:+.0f}  n={best['is_n']}")
        print(f"     OOS Sharpe={best['oos_sharpe']:.2f}  PF={best['oos_pf']:.2f}  "
              f"P&L=${best['oos_pnl']:+.0f}  n={best['oos_n']}  "
              f"DD=${best['oos_dd']:+.0f}")
    else:
        print(f"\n  /!\\ Aucune combinaison ne valide OOS "
              f"(Sharpe ≥ {OOS_SHARPE_MIN}, PF ≥ {OOS_PF_MIN}, "
              f"n ≥ {OOS_N_MIN}, P&L > {OOS_PNL_MIN}).")

    return {"results": results, "best": best}


def main():
    parser = argparse.ArgumentParser(
        description="Optimisation walk-forward Fibonacci 50%"
    )
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
            print(f"[!] {csv_path} introuvable")
            continue
        df = load_csv(str(csv_path))
        result = optimize_ticker(df, ticker)
        if result["best"]:
            summary[ticker] = result["best"]
        # Export grille complète
        if result["results"]:
            pd.DataFrame(result["results"]).to_csv(
                out_dir / f"optimization_{ticker}.csv", index=False
            )

    # ── Résumé final ────────────────────────────────────────────────────────
    print(f"\n{'='*92}")
    print(f"  RÉSUMÉ — Meilleurs paramètres (à reporter dans config.py)")
    print(f"{'='*92}")
    if not summary:
        print(f"  Aucun ticker n'a de combinaison validée OOS.")
        return

    for t, b in summary.items():
        print(f"  {t}: SL_ATR_MULT={b['sl']:.2f}  TP_ATR_MULT={b['tp']:.2f}  "
              f"MIN_IMPULSE_ATR={b['imp']:.2f}  (RR={b['rr']:.2f})")
        print(f"        IS Sharpe={b['is_sharpe']:.2f}  "
              f"OOS Sharpe={b['oos_sharpe']:.2f}  "
              f"OOS P&L=${b['oos_pnl']:+.0f}")


if __name__ == "__main__":
    main()
