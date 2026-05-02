#!/usr/bin/env python3
"""
Optimisation walk-forward des paramètres de détection des zones S/R.

Patche dynamiquement les constantes de `core/zones.py` :
  - ZONE_TOLERANCE_PCT  : tolérance de clustering des pivots (% prix médian)
  - ZONE_MAX_WIDTH_PCT  : largeur maximale d'une zone (% mid)
  - ZONE_RECENCY_THRESHOLD : seuil "is_recent" pour les pivots du groupe

Critère de validation OOS : OOS PF ≥ 1.2 ET n_trades OOS ≥ 8 ET P&L OOS > 0.
Score = OOS PF × OOS P&L (cohérent avec optimize_opr.py).

Usage :
    python optimize_zones.py --csv-dir ./data
    python optimize_zones.py --csv-dir ./data --ticker NQ1
"""

import argparse
from itertools import product
from pathlib import Path

import pandas as pd

import config as cfg
from config import INSTRUMENTS, YM1_ENABLED
from core.data import load_csv, build_timeframes
from core import zones as _zones
from backtest import run_backtest


IS_END = "2025-09-30"

# Grilles compactes — on optimise autour des valeurs courantes
TOLERANCE_GRID = [0.0005, 0.001, 0.0015, 0.002]
MAX_WIDTH_GRID = [0.003, 0.004, 0.005, 0.006]
RECENCY_GRID   = [0.50, 0.66, 0.75]
# = 4 × 4 × 3 = 48 combinaisons par actif


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
        "n": int(len(f)),
        "wr": float(len(wins) / len(f) * 100),
        "pf": float(gp / gl) if gl > 0 else 0.0,
        "pnl": float(f["pnl"].sum()),
        "dd": float((cum - cum.cummax()).min()),
    }


def _split_trades(df: pd.DataFrame, is_end: str):
    if len(df) == 0 or "date" not in df.columns:
        return df, df
    is_mask = df["date"] <= is_end
    return df[is_mask], df[~is_mask]


def _set_zone_params(tol: float, mw: float, rec: float):
    """Patch dynamique de cfg + namespace core.zones."""
    cfg.ZONE_TOLERANCE_PCT = tol
    cfg.ZONE_MAX_WIDTH_PCT = mw
    cfg.ZONE_RECENCY_THRESHOLD = rec
    _zones.ZONE_TOLERANCE_PCT = tol
    _zones.ZONE_MAX_WIDTH_PCT = mw
    _zones.ZONE_RECENCY_THRESHOLD = rec


def optimize_ticker(df_15m: pd.DataFrame, tf: dict, ticker: str,
                    is_end: str = IS_END):
    print(f"\n{'='*92}")
    print(f"  ZONE OPTIMIZATION — {ticker}  "
          f"({len(TOLERANCE_GRID)} TOL × {len(MAX_WIDTH_GRID)} MW × "
          f"{len(RECENCY_GRID)} REC)")
    print(f"{'='*92}")
    print(f"  {'TOL':>7} {'MW':>6} {'REC':>5}  "
          f"{'IS_n':>5} {'IS_PF':>6} {'IS_PnL':>9}   "
          f"{'OOS_n':>5} {'OOS_PF':>7} {'OOS_PnL':>9}  {'OOS_DD':>8}")

    # Backup des valeurs courantes
    backup = (cfg.ZONE_TOLERANCE_PCT,
              cfg.ZONE_MAX_WIDTH_PCT,
              cfg.ZONE_RECENCY_THRESHOLD)

    rows = []
    best = None
    try:
        for tol, mw, rec in product(TOLERANCE_GRID, MAX_WIDTH_GRID, RECENCY_GRID):
            _set_zone_params(tol, mw, rec)
            df_trades = run_backtest(df_15m, tf, ticker, analysis_chart_dir=None)
            # run_backtest (composite) inclut déjà une colonne "date" (str YYYY-MM-DD)
            is_t, oos_t = _split_trades(df_trades, is_end)
            is_s = _stats(is_t)
            oos_s = _stats(oos_t)

            valid = (oos_s["pf"] >= 1.2 and oos_s["n"] >= 8 and oos_s["pnl"] > 0)
            flag = "OK" if valid else "  "
            print(f"  {tol:>7.4f} {mw:>6.4f} {rec:>5.2f}  "
                  f"{is_s['n']:>5} {is_s['pf']:>6.2f} ${is_s['pnl']:>+8.0f}   "
                  f"{oos_s['n']:>5} {oos_s['pf']:>7.2f} "
                  f"${oos_s['pnl']:>+8.0f}  ${oos_s['dd']:>+7.0f} {flag}")
            rows.append({
                "tol": tol, "mw": mw, "rec": rec,
                "is_n": is_s["n"], "is_pf": is_s["pf"], "is_pnl": is_s["pnl"],
                "oos_n": oos_s["n"], "oos_pf": oos_s["pf"], "oos_pnl": oos_s["pnl"],
                "oos_dd": oos_s["dd"], "valid_oos": valid,
            })
            if valid:
                score = oos_s["pf"] * oos_s["pnl"]
                if best is None or score > best[0]:
                    best = (score, tol, mw, rec, is_s, oos_s)
    finally:
        cfg.ZONE_TOLERANCE_PCT, cfg.ZONE_MAX_WIDTH_PCT, cfg.ZONE_RECENCY_THRESHOLD = backup
        _zones.ZONE_TOLERANCE_PCT = backup[0]
        _zones.ZONE_MAX_WIDTH_PCT = backup[1]
        _zones.ZONE_RECENCY_THRESHOLD = backup[2]

    if best:
        _, tol, mw, rec, is_s, oos_s = best
        print(f"\n  ➜ Meilleure combinaison validée OOS :")
        print(f"     ZONE_TOLERANCE_PCT  = {tol:.4f}")
        print(f"     ZONE_MAX_WIDTH_PCT  = {mw:.4f}")
        print(f"     ZONE_RECENCY_THRESHOLD = {rec:.2f}")
        print(f"     IS  PF={is_s['pf']:.2f}  P&L=${is_s['pnl']:+.0f}  n={is_s['n']}")
        print(f"     OOS PF={oos_s['pf']:.2f}  P&L=${oos_s['pnl']:+.0f}  "
              f"n={oos_s['n']}  DD=${oos_s['dd']:+.0f}")
    else:
        print(f"\n  /!\\ Aucune combinaison ne valide OOS.")

    return rows, best


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv-dir", type=str, required=True)
    parser.add_argument("--ticker", type=str, default=None)
    args = parser.parse_args()

    if args.ticker:
        tickers = [args.ticker]
    else:
        # YM1 désactivé par config → on n'optimise que MES1 + NQ1
        tickers = [t for t in INSTRUMENTS.keys()
                   if t != "YM1" or YM1_ENABLED]

    out_dir = Path("output")
    out_dir.mkdir(parents=True, exist_ok=True)

    summary = {}
    for ticker in tickers:
        csv_path = Path(args.csv_dir) / f"{ticker}_data_m15.csv"
        if not csv_path.exists():
            print(f"[!] {csv_path} introuvable")
            continue
        df_15m = load_csv(str(csv_path))
        tf = build_timeframes(df_15m)
        rows, best = optimize_ticker(df_15m, tf, ticker)
        if rows:
            pd.DataFrame(rows).to_csv(out_dir / f"zone_opt_{ticker}.csv", index=False)
        if best:
            summary[ticker] = best

    print(f"\n{'='*92}")
    print(f"  RÉSUMÉ — Zone params à reporter dans config.py")
    print(f"{'='*92}")
    for t, b in summary.items():
        _, tol, mw, rec, _, oos_s = b
        print(f"  {t}: TOL={tol:.4f}  MW={mw:.4f}  REC={rec:.2f}  "
              f"OOS PF={oos_s['pf']:.2f}  P&L=${oos_s['pnl']:+.0f}")


if __name__ == "__main__":
    main()
