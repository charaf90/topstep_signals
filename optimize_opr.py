#!/usr/bin/env python3
"""
Optimisation des distances Stop / Take-Profit (en points) de la stratégie
OPR `opr-v2`, par actif, en walk-forward IS / OOS.

La logique OPR est désormais à distance fixe (PineScript) — on balaye
donc directement (SL_pts, TP_pts) plutôt qu'un RR. Le sizing reste à
risque fixe ($100 / trade).

Usage :
    python optimize_opr.py --csv-dir ./data
    python optimize_opr.py --csv-dir ./data --ticker NQ1
    python optimize_opr.py --csv-dir ./data \
        --sl-mes 6,8,10,12,15 --tp-mes 12,16,20,25,30

Pour limiter la combinatoire, on fixe une grille par actif. Par défaut
on balaye autour des valeurs initiales de config.py. Le critère de
sélection est cohérent avec optimize.py Phase C :
   OOS PF ≥ 1.2 ET n_trades OOS ≥ 8 ET P&L OOS > 0.

Le script imprime un tableau IS / OOS par actif et propose la meilleure
combinaison validée. Il NE modifie pas config.py — la valeur retenue est
à reporter à la main.
"""

import argparse
from pathlib import Path

import pandas as pd

from config import (
    INSTRUMENTS, OPR_SL_POINTS, OPR_TP_POINTS,
)
from core.data import load_csv, build_timeframes
import config as cfg
from backtest import run_opr_backtest


# Split walk-forward (cohérent avec optimize.py Phase C)
IS_END = "2025-09-30"

# Grilles par défaut (autour des valeurs config.py)
DEFAULT_SL = {
    "MES1": [6.0, 8.0, 10.0, 12.0, 15.0],
    "NQ1": [15.0, 20.0, 25.0, 30.0, 40.0],
    "YM1": [30.0, 40.0, 50.0, 60.0, 80.0],
}
DEFAULT_TP = {
    "MES1": [10.0, 15.0, 20.0, 25.0, 35.0, 45.0],
    "NQ1": [25.0, 40.0, 50.0, 60.0, 80.0, 100.0],
    "YM1": [60.0, 80.0, 100.0, 130.0, 160.0],
}


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


def optimize_ticker(df_15m, tf, ticker: str,
                    sl_grid, tp_grid, is_end: str = IS_END):
    print(f"\n{'='*78}")
    print(f"  OPR optimisation — {ticker} "
          f"({len(sl_grid)} SL × {len(tp_grid)} TP)")
    print(f"{'='*78}")
    print(f"  {'SL':>5} {'TP':>5} {'RR':>5}  "
          f"{'IS_n':>5} {'IS_PF':>6} {'IS_PnL':>9}   "
          f"{'OOS_n':>5} {'OOS_PF':>7} {'OOS_PnL':>9}  {'OOS_DD':>9}")

    # Sauvegarde des valeurs config courantes pour les restaurer ensuite.
    sl_backup = dict(OPR_SL_POINTS)
    tp_backup = dict(OPR_TP_POINTS)

    best = None
    rows = []
    try:
        for sl in sl_grid:
            for tp in tp_grid:
                if tp <= sl:
                    continue  # RR < 1, pas pertinent
                cfg.OPR_SL_POINTS[ticker] = float(sl)
                cfg.OPR_TP_POINTS[ticker] = float(tp)
                # Le module core/opr.py a importé OPR_SL_POINTS/OPR_TP_POINTS
                # par référence dict — on patche aussi son namespace local.
                from core import opr as _opr
                _opr.OPR_SL_POINTS[ticker] = float(sl)
                _opr.OPR_TP_POINTS[ticker] = float(tp)

                df_trades = run_opr_backtest(df_15m, tf, ticker,
                                             analysis_chart_dir=None)
                is_t, oos_t = _split_trades(df_trades, is_end)
                is_s = _stats(is_t)
                oos_s = _stats(oos_t)
                rr = tp / sl
                rows.append((sl, tp, is_s, oos_s))

                valid = oos_s["pf"] >= 1.2 and oos_s["n"] >= 8 and oos_s["pnl"] > 0
                flag = "✅" if valid else "  "
                print(f"  {sl:>5.1f} {tp:>5.1f} {rr:>5.2f}  "
                      f"{is_s['n']:>5} {is_s['pf']:>6.2f} ${is_s['pnl']:>+8.0f}   "
                      f"{oos_s['n']:>5} {oos_s['pf']:>7.2f} "
                      f"${oos_s['pnl']:>+8.0f}  ${oos_s['dd']:>+8.0f} {flag}")

                if valid:
                    score = oos_s["pf"] * oos_s["pnl"]
                    if best is None or score > best[0]:
                        best = (score, sl, tp, is_s, oos_s)
    finally:
        # Restaure config
        cfg.OPR_SL_POINTS.update(sl_backup)
        cfg.OPR_TP_POINTS.update(tp_backup)
        from core import opr as _opr
        _opr.OPR_SL_POINTS.update(sl_backup)
        _opr.OPR_TP_POINTS.update(tp_backup)

    if best:
        _, sl, tp, is_s, oos_s = best
        print(f"\n  ➜ Meilleure combinaison validée OOS : "
              f"SL={sl:.1f}  TP={tp:.1f}  (RR={tp/sl:.2f})")
        print(f"     IS  PF={is_s['pf']:.2f}  P&L=${is_s['pnl']:+.0f}  n={is_s['n']}")
        print(f"     OOS PF={oos_s['pf']:.2f}  P&L=${oos_s['pnl']:+.0f}  "
              f"n={oos_s['n']}  DD=${oos_s['dd']:+.0f}")
    else:
        print(f"\n  ⚠ Aucune combinaison ne valide IS/OOS "
              f"(PF≥1.2, n≥8, PnL>0). Conserver les valeurs config.py.")

    return best, rows


def _parse_grid(s: str):
    if not s:
        return None
    return [float(x.strip()) for x in s.split(",") if x.strip()]


def main():
    parser = argparse.ArgumentParser(
        description="Optimisation OPR (SL/TP en points) par actif"
    )
    parser.add_argument("--csv-dir", type=str, required=True)
    parser.add_argument("--ticker", type=str, default=None)
    parser.add_argument("--sl-mes", type=str, default=None)
    parser.add_argument("--tp-mes", type=str, default=None)
    parser.add_argument("--sl-nq", type=str, default=None)
    parser.add_argument("--tp-nq", type=str, default=None)
    parser.add_argument("--sl-ym", type=str, default=None)
    parser.add_argument("--tp-ym", type=str, default=None)
    parser.add_argument("--is-end", type=str, default=IS_END,
                        help="Date de fin de la période IS (YYYY-MM-DD)")
    args = parser.parse_args()

    grids = {
        "MES1": (_parse_grid(args.sl_mes) or DEFAULT_SL["MES1"],
                 _parse_grid(args.tp_mes) or DEFAULT_TP["MES1"]),
        "NQ1":  (_parse_grid(args.sl_nq)  or DEFAULT_SL["NQ1"],
                 _parse_grid(args.tp_nq)  or DEFAULT_TP["NQ1"]),
        "YM1":  (_parse_grid(args.sl_ym)  or DEFAULT_SL["YM1"],
                 _parse_grid(args.tp_ym)  or DEFAULT_TP["YM1"]),
    }
    tickers = [args.ticker] if args.ticker else list(INSTRUMENTS.keys())

    summary = {}
    for ticker in tickers:
        csv_path = Path(args.csv_dir) / f"{ticker}_data_m15.csv"
        if not csv_path.exists():
            print(f"  [!] {csv_path} introuvable")
            continue
        df_15m = load_csv(str(csv_path))
        tf = build_timeframes(df_15m)
        sl_grid, tp_grid = grids[ticker]
        best, _ = optimize_ticker(df_15m, tf, ticker, sl_grid, tp_grid,
                                  is_end=args.is_end)
        if best:
            summary[ticker] = (best[1], best[2])

    if summary:
        print(f"\n{'='*78}")
        print(f"  RÉSUMÉ — SL/TP retenus (à reporter dans config.py)")
        print(f"{'='*78}")
        for t, (sl, tp) in summary.items():
            print(f"  {t}:  OPR_SL_POINTS={sl}  OPR_TP_POINTS={tp}  "
                  f"(RR={tp/sl:.2f})")


if __name__ == "__main__":
    main()
