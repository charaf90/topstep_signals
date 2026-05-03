#!/usr/bin/env python3
"""
Optimisation walk-forward multi-niveaux Fib (38.2, 50, 61.8).

Pour chaque (niveau Fib, ticker), balaye la grille SL × TP × IMP via
walk-forward IS/OOS. Identifie la meilleure config par (niveau, ticker)
et exporte les CSVs nécessaires à la comparaison portefeuille.

Critère de validation OOS : Sharpe ≥ 0.5, PF ≥ 1.2, n ≥ 8, P&L > 0.
Score = IS Sharpe maximisé parmi les combos OOS-validés.

Pour comparer ensuite les 7 combinaisons de niveaux, voir
`compare_fib_levels.py`.

Usage :
    python optimize_fib_levels.py --csv-dir ../data
    python optimize_fib_levels.py --csv-dir ../data --ticker MES1
"""

import argparse
import sys
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import INSTRUMENTS, TOPSTEP_ACCOUNT_SIZE
from core.data import load_csv
from core.strategy_fib import run_fib_backtest


IS_END = "2025-09-30"
SHARPE_ANNUALIZATION = 252

FIB_LEVELS = [0.382, 0.500, 0.618]

# Grilles compactes — autour des optimaux connus pour fib=0.50
SL_GRID = [0.75, 1.0, 1.25, 1.5]
TP_GRID = [1.5, 2.0, 2.5, 3.0]
IMP_GRID = [1.0, 1.5, 2.0]
# = 48 combos par (niveau, ticker) ; 3 niveaux × 3 tickers = 432 backtests

OOS_SHARPE_MIN = 0.5
OOS_PF_MIN = 1.2
OOS_N_MIN = 8
OOS_PNL_MIN = 0.0
IS_N_MIN = 10


def _stats(trades: pd.DataFrame, account_size: float = TOPSTEP_ACCOUNT_SIZE) -> dict:
    if len(trades) == 0:
        return {"n": 0, "wr": 0.0, "pf": 0.0, "pnl": 0.0,
                "sharpe": 0.0, "dd": 0.0}
    f = trades[trades.get("result", "") != "NOT_FILLED"] if "result" in trades.columns else trades
    if len(f) == 0:
        return {"n": 0, "wr": 0.0, "pf": 0.0, "pnl": 0.0,
                "sharpe": 0.0, "dd": 0.0}
    wins = f[f["pnl"] > 0]
    losses = f[f["pnl"] <= 0]
    gp = float(wins["pnl"].sum()) if len(wins) > 0 else 0.0
    gl = abs(float(losses["pnl"].sum())) if len(losses) > 0 else 0.0
    pf = gp / gl if gl > 0 else (9.99 if gp > 0 else 0.0)
    pnl_total = float(f["pnl"].sum())
    cum = f["pnl"].cumsum()
    dd = float((cum - cum.cummax()).min())
    rets = f["pnl"] / account_size
    sharpe = (
        float(rets.mean() / rets.std() * np.sqrt(SHARPE_ANNUALIZATION))
        if rets.std() > 0 and len(rets) > 1 else 0.0
    )
    return {
        "n": int(len(f)), "wr": float(len(wins) / len(f) * 100),
        "pf": pf, "pnl": pnl_total, "sharpe": sharpe, "dd": dd,
    }


def _split_trades(df: pd.DataFrame, is_end: str = IS_END):
    if len(df) == 0 or "date" not in df.columns:
        return df, df
    is_mask = df["date"].astype(str) <= is_end
    return df[is_mask], df[~is_mask]


def optimize_level_for_ticker(df_15m, ticker: str, fib_level: float
                               ) -> dict:
    """Optimise SL × TP × IMP pour un (niveau, ticker) donné."""
    print(f"\n  ─── {ticker}  fib_level={fib_level:.3f} ───")
    print(f"    {'SL':>4} {'TP':>4} {'IMP':>4} {'RR':>4}  "
          f"{'IS_n':>4} {'IS_PF':>5} {'IS_Sh':>6} {'IS_PnL':>9}   "
          f"{'OOS_n':>5} {'OOS_PF':>6} {'OOS_Sh':>7} {'OOS_PnL':>9}")
    rows = []
    best = None
    for sl, tp, imp in product(SL_GRID, TP_GRID, IMP_GRID):
        if tp / sl < 1.0:
            continue
        # Pas de filtre trigger durant l'optimisation : on cherche la
        # performance brute du niveau (filtre potentiellement appliqué après).
        trades = run_fib_backtest(
            df_15m, ticker,
            fib_level=fib_level, sl_mult=sl, tp_mult=tp, min_imp=imp,
            apply_filter=False,
        )
        if len(trades) == 0:
            continue
        is_t, oos_t = _split_trades(trades)
        if len(is_t) < IS_N_MIN:
            continue
        s_is = _stats(is_t)
        s_oos = _stats(oos_t)
        valid = (s_oos["sharpe"] >= OOS_SHARPE_MIN
                 and s_oos["pf"] >= OOS_PF_MIN
                 and s_oos["n"] >= OOS_N_MIN
                 and s_oos["pnl"] > OOS_PNL_MIN)
        flag = "OK" if valid else "  "
        print(f"    {sl:>4.2f} {tp:>4.2f} {imp:>4.2f} {tp/sl:>4.2f}  "
              f"{s_is['n']:>4} {s_is['pf']:>5.2f} {s_is['sharpe']:>6.2f} "
              f"${s_is['pnl']:>+8.0f}   "
              f"{s_oos['n']:>5} {s_oos['pf']:>6.2f} {s_oos['sharpe']:>7.2f} "
              f"${s_oos['pnl']:>+8.0f}  {flag}")
        row = {
            "ticker": ticker, "fib_level": fib_level,
            "sl": sl, "tp": tp, "imp": imp, "rr": tp / sl,
            "is_n": s_is["n"], "is_pf": s_is["pf"], "is_sharpe": s_is["sharpe"],
            "is_pnl": s_is["pnl"],
            "oos_n": s_oos["n"], "oos_pf": s_oos["pf"], "oos_sharpe": s_oos["sharpe"],
            "oos_pnl": s_oos["pnl"], "oos_dd": s_oos["dd"],
            "valid_oos": bool(valid),
        }
        rows.append(row)
        if valid:
            score = s_is["sharpe"]   # max IS Sharpe parmi validés OOS
            if best is None or score > best["score"]:
                best = {"score": score, **row}
    if best:
        print(f"    ➜ Best : SL={best['sl']:.2f}  TP={best['tp']:.2f}  "
              f"IMP={best['imp']:.2f}  IS Sh={best['is_sharpe']:.2f}  "
              f"OOS Sh={best['oos_sharpe']:.2f}  P&L=${best['oos_pnl']:+.0f}")
    else:
        print(f"    /!\\ Aucune combinaison ne valide OOS pour ce niveau.")
    return {"rows": rows, "best": best}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv-dir", type=str, default="../data")
    parser.add_argument("--ticker", type=str, default=None)
    args = parser.parse_args()

    out_dir = Path(__file__).parent / "output"
    out_dir.mkdir(parents=True, exist_ok=True)

    tickers = [args.ticker] if args.ticker else list(INSTRUMENTS.keys())

    all_rows = []
    summary = {}   # {(ticker, fib_level): best_dict}
    for ticker in tickers:
        csv_path = Path(args.csv_dir) / f"{ticker}_data_m15.csv"
        if not csv_path.exists():
            print(f"  [!] {csv_path} introuvable")
            continue
        df_15m = load_csv(str(csv_path))
        print(f"\n{'='*92}")
        print(f"  OPTIMISATION FIB MULTI-NIVEAUX — {ticker}")
        print(f"{'='*92}")

        for fib_level in FIB_LEVELS:
            result = optimize_level_for_ticker(df_15m, ticker, fib_level)
            all_rows.extend(result["rows"])
            if result["best"]:
                summary[(ticker, fib_level)] = result["best"]

    # Export grille complète
    if all_rows:
        df_grid = pd.DataFrame(all_rows)
        grid_path = out_dir / "fib_levels_grid.csv"
        df_grid.to_csv(grid_path, index=False)
        print(f"\n  ✓ {grid_path}")

    # ── Résumé final ────────────────────────────────────────────────────
    print(f"\n{'='*92}")
    print(f"  RÉSUMÉ — Meilleure config par (ticker, niveau Fib)")
    print(f"{'='*92}")
    print(f"  {'Ticker':<6} {'Level':>6}  {'SL':>5} {'TP':>5} {'IMP':>5} {'RR':>5}  "
          f"{'IS Sh':>6} {'OOS Sh':>7} {'OOS PF':>7} {'OOS PnL':>9} {'OOS n':>6}")
    print(f"  {'-'*88}")
    for (t, lvl), b in sorted(summary.items()):
        print(f"  {t:<6} {lvl:>6.3f}  {b['sl']:>5.2f} {b['tp']:>5.2f} "
              f"{b['imp']:>5.2f} {b['rr']:>5.2f}  "
              f"{b['is_sharpe']:>6.2f} {b['oos_sharpe']:>7.2f} "
              f"{b['oos_pf']:>7.2f} ${b['oos_pnl']:>+8.0f} {b['oos_n']:>6}")

    # Export résumé
    if summary:
        summary_rows = [
            {"ticker": t, "fib_level": lvl, **{k: v for k, v in b.items()
                                                if k != "score"}}
            for (t, lvl), b in sorted(summary.items())
        ]
        summary_path = out_dir / "fib_levels_best.csv"
        pd.DataFrame(summary_rows).to_csv(summary_path, index=False)
        print(f"\n  ✓ {summary_path}")


if __name__ == "__main__":
    main()
