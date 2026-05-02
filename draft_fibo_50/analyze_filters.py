#!/usr/bin/env python3
"""
Analyse des features au moment de l'armement et test walk-forward de filtres.

Approche identique à analyse/03_filter_backtest.py du projet OPR :
  - Pour chaque feature candidate, balaye les percentiles 10→90 sur IS pour
    trouver le seuil maximisant le PF IS (n ≥ OOS_N_MIN), dans les deux
    directions (gt / lt).
  - Évalue le seuil retenu sur OOS en aveugle.
  - Critères de validation OOS : Sharpe ≥ 0.5, PF ≥ 1.2, n ≥ 8, P&L > 0.

Usage :
    python analyze_filters.py
    python analyze_filters.py --ticker MES1
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from config import INSTRUMENTS, IS_END, ACCOUNT_SIZE, SHARPE_ANNUALIZATION


# ─────────────────────────────────────────────────────────────────────────────
# Features candidates (calculables AU MOMENT DE L'ARMEMENT — pas de leak)
# ─────────────────────────────────────────────────────────────────────────────

CANDIDATE_FEATURES = [
    "bars_since_confirm",      # délai entre confirmation pivot et armement
    "adx_at_arm",              # force de tendance ADX
    "adx_slope_3",             # pente ADX (renforcement vs faiblissement)
    "ema_stack_atr",           # distance EMA50-EMA200 / ATR (signed)
    "price_extension_atr",     # distance prix-fib_50 / ATR (signed dans direction)
    "impulse_velocity_atr",    # ATR par bougie de l'impulse
    "session_hour_utc",        # heure de session
    "recent_vol_atr",          # std(10 closes) / ATR — volatilité récente
    "impulse_size_atr",        # taille impulse / ATR_au_pivot
    "impulse_bars",            # durée impulse en bougies
]

# Critères validation OOS (cohérent avec optimize.py)
OOS_SHARPE_MIN = 0.5
OOS_PF_MIN = 1.2
OOS_N_MIN = 8
OOS_PNL_MIN = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Helpers stats
# ─────────────────────────────────────────────────────────────────────────────

def _stats(trades_subset: pd.DataFrame) -> dict:
    """Stats sur un sous-ensemble de trades — mêmes formules que backtest.stats()."""
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
        "pf": float(pf),
        "pnl": float(trades_subset["pnl"].sum()),
        "sharpe": sharpe,
    }


def _valid_oos(s: dict) -> bool:
    return (s["sharpe"] >= OOS_SHARPE_MIN
            and s["pf"] >= OOS_PF_MIN
            and s["n"] >= OOS_N_MIN
            and s["pnl"] > OOS_PNL_MIN)


# ─────────────────────────────────────────────────────────────────────────────
# Optimisation seuil sur IS
# ─────────────────────────────────────────────────────────────────────────────

def _find_best_threshold(df_is: pd.DataFrame, feature: str, direction: str
                         ) -> tuple:
    """
    Balaye les percentiles 10→90 (step 5) du feature sur IS.
    Retourne (seuil, IS PF) maximisant PF IS avec n ≥ OOS_N_MIN.
    """
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
    """Optimise le seuil sur IS dans les 2 directions, évalue sur OOS."""
    df_is_f = df_is.dropna(subset=[feature])
    df_oos_f = df_oos.dropna(subset=[feature])
    base_is = _stats(df_is_f)
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
        improvement_pf = s_oos["pf"] - base_oos["pf"]
        improvement_sharpe = s_oos["sharpe"] - base_oos["sharpe"]
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
            "base_oos_pf": round(base_oos["pf"], 3),
            "base_oos_sharpe": round(base_oos["sharpe"], 2),
        }
        if best is None or s_is["pf"] > best["is_pf"]:
            best = row
    return best


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def analyze_ticker(ticker: str, trades_path: Path, out_dir: Path):
    if not trades_path.exists():
        print(f"[!] {trades_path} manquant.")
        return None

    df = pd.read_csv(trades_path)
    df["date"] = df["fill_time"].astype(str).str[:10]
    df_is = df[df["date"] <= IS_END].copy()
    df_oos = df[df["date"] > IS_END].copy()

    print(f"\n{'='*88}")
    print(f"  ANALYSE FILTRES — {ticker}")
    print(f"{'='*88}")
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
        improvement = (
            "↑" if row["delta_sharpe"] > 0.1 else
            "↓" if row["delta_sharpe"] < -0.1 else "·"
        )
        sym = ">" if row["direction"] == "gt" else "<"
        print(f"  {feat:<24} {sym:>3} {row['threshold']:>8.3f}  "
              f"{row['is_n']:>4} {row['is_pf']:>5.2f} {row['is_sharpe']:>6.2f}  "
              f"{row['oos_n']:>4} {row['oos_pf']:>6.3f} {row['oos_sharpe']:>7.2f} "
              f"${row['oos_pnl']:>+7.0f}  {row['delta_pf']:>+5.2f} "
              f"{row['delta_sharpe']:>+5.2f}  {improvement} {flag}")

    df_res = pd.DataFrame(results)
    df_res.to_csv(out_dir / f"filters_{ticker}.csv", index=False)

    # Validés OOS, triés par delta_sharpe
    valid = df_res[df_res["valid_oos"]].copy()
    if len(valid) > 0:
        # On veut une amélioration vs baseline (pas juste passer les seuils)
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
            return {
                "ticker": ticker,
                "best": improving.iloc[0].to_dict(),
                "all_validated": improving.to_dict(orient="records"),
            }
        else:
            print(f"    Aucun filtre n'améliore Sharpe OOS de plus de 0.1.")
    else:
        print(f"\n  Aucun filtre ne valide OOS.")

    return {"ticker": ticker, "best": None, "all_validated": []}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", type=str, default=None)
    args = parser.parse_args()

    out_dir = Path(__file__).parent / "output"
    out_dir.mkdir(parents=True, exist_ok=True)

    tickers = [args.ticker] if args.ticker else list(INSTRUMENTS.keys())
    summary = {}
    for ticker in tickers:
        trades_path = out_dir / f"trades_{ticker}.csv"
        result = analyze_ticker(ticker, trades_path, out_dir)
        if result and result["best"]:
            summary[ticker] = result["best"]

    print(f"\n{'='*88}")
    print(f"  RÉSUMÉ — Meilleurs filtres validés OOS (Δ Sharpe ≥ 0.1)")
    print(f"{'='*88}")
    if not summary:
        print(f"  Aucun ticker n'a de filtre robuste validé OOS.")
        return
    for t, b in summary.items():
        sym = ">" if b["direction"] == "gt" else "<"
        print(f"  {t}: {b['feature']} {sym} {b['threshold']:.3f}  "
              f"OOS Sharpe={b['oos_sharpe']:.2f} (Δ{b['delta_sharpe']:+.2f})  "
              f"OOS PF={b['oos_pf']:.2f}  P&L=${b['oos_pnl']:+.0f}")


if __name__ == "__main__":
    main()
