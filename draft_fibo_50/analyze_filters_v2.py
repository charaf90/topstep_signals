#!/usr/bin/env python3
"""
Analyse walk-forward des filtres trigger pour Fib niveau 38.2 % (fib-v2).

Lit les CSVs `output/backtest_{TICKER}_fib.csv` générés par
`python backtest.py --strategy fib --no-analysis-charts` avec la config
fib-v2 (FIB_LEVEL_PER_TICKER = 0.382).

Pour chaque feature candidate, balaye les percentiles 10→90 sur IS pour
trouver le seuil maximisant le PF IS (n ≥ OOS_N_MIN), dans les deux
directions (gt / lt). Évalue le seuil retenu sur OOS en aveugle.

Usage :
    python draft_fibo_50/analyze_filters_v2.py
    python draft_fibo_50/analyze_filters_v2.py --ticker MES1
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import INSTRUMENTS, TOPSTEP_ACCOUNT_SIZE


IS_END = "2025-09-30"
SHARPE_ANNUALIZATION = 252
ACCOUNT_SIZE = TOPSTEP_ACCOUNT_SIZE

CANDIDATE_FEATURES = [
    "bars_since_confirm",
    "adx_at_arm", "adx_slope_3",
    "ema_stack_atr", "price_extension_atr",
    "impulse_velocity_atr", "session_hour_utc",
    "recent_vol_atr", "impulse_size_atr", "impulse_bars",
]

OOS_SHARPE_MIN = 0.5
OOS_PF_MIN = 1.2
OOS_N_MIN = 8
OOS_PNL_MIN = 0.0


def _stats(trades_subset: pd.DataFrame) -> dict:
    if len(trades_subset) == 0:
        return {"n": 0, "wr": 0.0, "pf": 0.0, "pnl": 0.0, "sharpe": 0.0}
    wins = trades_subset[trades_subset["pnl"] > 0]
    losses = trades_subset[trades_subset["pnl"] <= 0]
    gp = float(wins["pnl"].sum()) if len(wins) > 0 else 0.0
    gl = abs(float(losses["pnl"].sum())) if len(losses) > 0 else 0.0
    pf = gp / gl if gl > 0 else (9.99 if gp > 0 else 0.0)
    rets = trades_subset["pnl"] / ACCOUNT_SIZE
    sharpe = (
        float(rets.mean() / rets.std() * np.sqrt(SHARPE_ANNUALIZATION))
        if rets.std() > 0 and len(rets) > 1 else 0.0
    )
    return {
        "n": int(len(trades_subset)),
        "wr": float(len(wins) / len(trades_subset) * 100),
        "pf": float(pf), "pnl": float(trades_subset["pnl"].sum()),
        "sharpe": sharpe,
    }


def _valid_oos(s: dict) -> bool:
    return (s["sharpe"] >= OOS_SHARPE_MIN
            and s["pf"] >= OOS_PF_MIN
            and s["n"] >= OOS_N_MIN
            and s["pnl"] > OOS_PNL_MIN)


def _find_best_threshold(df_is: pd.DataFrame, feature: str, direction: str):
    vals = df_is[feature].dropna()
    if len(vals) < 2 * OOS_N_MIN:
        return None, None
    best_thresh, best_pf = None, -np.inf
    for pct in range(10, 95, 5):
        thresh = float(np.percentile(vals, pct))
        if direction == "gt":
            mask = df_is[feature] > thresh
        else:
            mask = df_is[feature] < thresh
        sub = df_is[mask]
        if len(sub) < OOS_N_MIN:
            continue
        s = _stats(sub)
        if s["pf"] > best_pf:
            best_pf = s["pf"]
            best_thresh = thresh
    return best_thresh, best_pf


def _test_filter(df_is: pd.DataFrame, df_oos: pd.DataFrame, feature: str
                 ) -> dict:
    df_is_f = df_is.dropna(subset=[feature])
    df_oos_f = df_oos.dropna(subset=[feature])
    base_oos = _stats(df_oos_f)
    best = None
    for direction in ["gt", "lt"]:
        thresh, _ = _find_best_threshold(df_is_f, feature, direction)
        if thresh is None:
            continue
        if direction == "gt":
            sub_is = df_is_f[df_is_f[feature] > thresh]
            sub_oos = df_oos_f[df_oos_f[feature] > thresh]
        else:
            sub_is = df_is_f[df_is_f[feature] < thresh]
            sub_oos = df_oos_f[df_oos_f[feature] < thresh]
        s_is = _stats(sub_is)
        s_oos = _stats(sub_oos)
        valid = _valid_oos(s_oos)
        improvement_sharpe = s_oos["sharpe"] - base_oos["sharpe"]
        improvement_pf = s_oos["pf"] - base_oos["pf"]
        row = {
            "feature": feature, "direction": direction,
            "threshold": round(thresh, 4),
            "is_n": s_is["n"], "is_pf": round(s_is["pf"], 3),
            "is_sharpe": round(s_is["sharpe"], 2),
            "is_pnl": round(s_is["pnl"], 0),
            "oos_n": s_oos["n"], "oos_pf": round(s_oos["pf"], 3),
            "oos_sharpe": round(s_oos["sharpe"], 2),
            "oos_pnl": round(s_oos["pnl"], 0),
            "valid_oos": bool(valid),
            "delta_pf": round(improvement_pf, 3),
            "delta_sharpe": round(improvement_sharpe, 2),
            "base_oos_sharpe": round(base_oos["sharpe"], 2),
            "base_oos_pf": round(base_oos["pf"], 3),
        }
        if best is None or s_is["pf"] > best["is_pf"]:
            best = row
    return best


def analyze_ticker(ticker: str, csv_path: Path, out_dir: Path):
    if not csv_path.exists():
        print(f"[!] {csv_path} introuvable")
        return None
    df = pd.read_csv(csv_path)
    if "result" not in df.columns:
        return None
    df = df[df["result"] != "NOT_FILLED"].copy()
    if len(df) == 0:
        return None

    # Date de fill pour split IS/OOS
    if "fill_time" in df.columns and df["fill_time"].notna().any():
        df["date"] = df["fill_time"].astype(str).str.slice(0, 10)
    df_is = df[df["date"].astype(str) <= IS_END].copy()
    df_oos = df[df["date"].astype(str) > IS_END].copy()

    print(f"\n{'='*92}")
    print(f"  ANALYSE FILTRES Fib-v2 — {ticker} (niveau 38.2 %)")
    print(f"{'='*92}")
    base_is = _stats(df_is)
    base_oos = _stats(df_oos)
    print(f"  Baseline IS  : n={base_is['n']}  PF={base_is['pf']:.2f}  "
          f"Sharpe={base_is['sharpe']:.2f}  P&L=${base_is['pnl']:+.0f}")
    print(f"  Baseline OOS : n={base_oos['n']}  PF={base_oos['pf']:.2f}  "
          f"Sharpe={base_oos['sharpe']:.2f}  P&L=${base_oos['pnl']:+.0f}")
    print(f"\n  {'Feature':<24} {'Dir':>3} {'Seuil':>8}  "
          f"{'IS_n':>4} {'IS_PF':>5} {'IS_Sh':>6}  "
          f"{'OOS_n':>4} {'OOS_PF':>6} {'OOS_Sh':>7} {'OOS_P&L':>8}  "
          f"{'ΔPF':>5} {'ΔSh':>5}")
    print(f"  {'-'*108}")

    results = []
    for feat in CANDIDATE_FEATURES:
        if feat not in df.columns:
            continue
        row = _test_filter(df_is, df_oos, feat)
        if row is None:
            continue
        results.append(row)
        flag = "VALID" if row["valid_oos"] else "fail"
        sym = ">" if row["direction"] == "gt" else "<"
        marker = ("↑" if row["delta_sharpe"] > 0.1
                  else "↓" if row["delta_sharpe"] < -0.1 else "·")
        print(f"  {feat:<24} {sym:>3} {row['threshold']:>8.3f}  "
              f"{row['is_n']:>4} {row['is_pf']:>5.2f} {row['is_sharpe']:>6.2f}  "
              f"{row['oos_n']:>4} {row['oos_pf']:>6.3f} {row['oos_sharpe']:>7.2f} "
              f"${row['oos_pnl']:>+7.0f}  {row['delta_pf']:>+5.2f} "
              f"{row['delta_sharpe']:>+5.2f}  {marker} {flag}")

    df_res = pd.DataFrame(results)
    df_res.to_csv(out_dir / f"filters_v2_{ticker}.csv", index=False)
    valid = df_res[df_res["valid_oos"]].copy()
    if len(valid) > 0:
        improving = valid[valid["delta_sharpe"] > 0.1].sort_values(
            "delta_sharpe", ascending=False
        )
        print(f"\n  FILTRES VALIDÉS OOS *amélioration Sharpe ≥ 0.1* :")
        if len(improving) > 0:
            for _, row in improving.iterrows():
                sym = ">" if row["direction"] == "gt" else "<"
                print(f"    [{row['feature']} {sym} {row['threshold']:.3f}] "
                      f"OOS Sharpe={row['oos_sharpe']:.2f} "
                      f"(Δ{row['delta_sharpe']:+.2f})  "
                      f"PF={row['oos_pf']:.2f}  P&L=${row['oos_pnl']:+.0f}  "
                      f"n={row['oos_n']}")
            return improving.iloc[0].to_dict()
    print(f"\n  Aucun filtre n'améliore Sharpe OOS de plus de 0.1.")
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", type=str, default=None)
    parser.add_argument("--input-dir", type=str, default="../output")
    args = parser.parse_args()
    in_dir = Path(args.input_dir)
    out_dir = Path(__file__).parent / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    tickers = [args.ticker] if args.ticker else list(INSTRUMENTS.keys())
    summary = {}
    for ticker in tickers:
        csv_path = in_dir / f"backtest_{ticker}_fib.csv"
        b = analyze_ticker(ticker, csv_path, out_dir)
        if b:
            summary[ticker] = b
    print(f"\n{'='*92}")
    print(f"  RÉSUMÉ — Meilleurs filtres Fib-v2 par ticker (Δ Sharpe ≥ 0.1)")
    print(f"{'='*92}")
    if not summary:
        print("  Aucun ticker n'a de filtre validé.")
        return
    for t, b in summary.items():
        sym = ">" if b["direction"] == "gt" else "<"
        print(f"  {t}: {b['feature']} {sym} {b['threshold']:.3f}  "
              f"OOS Sharpe={b['oos_sharpe']:.2f} (Δ{b['delta_sharpe']:+.2f})  "
              f"OOS PF={b['oos_pf']:.2f}  P&L=${b['oos_pnl']:+.0f}")


if __name__ == "__main__":
    main()
