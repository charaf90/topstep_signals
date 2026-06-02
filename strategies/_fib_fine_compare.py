"""
Harnais de comparaison M1 vs M5 pour fib-fine-v1 (PHASES 3→5).

NON enregistré au REGISTRY — script de recherche dédié à la mission de comparaison.
Lance la MÊME logique (strategies.fib_fine.run_backtest) sur M1 et M5 pour les
tickers communs NQ1/MES1/YM1, à périmètre temporel ÉGAL (M5 resamplé depuis M1),
walk-forward IS_END=2025-09-30 / OOS_START=2025-10-01, métriques NET.

Sorties :
  output/fib-fine-v1/full/trades/<TICKER>_<TF>.csv
  output/fib-fine-v1/full/comparison.json
  output/fib-fine-v1/full/robustness.json

Usage : python -m strategies._fib_fine_compare
"""

from __future__ import annotations

import json
import os

import numpy as np
import pandas as pd

from config import FIB_FINE_IS_END, FIB_FINE_OOS_START
from core.metrics import compute_stats
from core.robustness import (
    block_bootstrap,
    bonferroni_threshold,
    monte_carlo_drawdown,
    probabilistic_sharpe_ratio,
    regime_stress_test,
    worst_case_clustering,
)
from strategies.fib_fine import run_backtest

np.random.seed(42)

OUT = "output/fib-fine-v1"
TRADES_DIR = f"{OUT}/full/trades"
TICKERS = ["NQ1", "MES1", "YM1"]
TFS = ["m1", "m5"]
IS_END = pd.Timestamp(FIB_FINE_IS_END)
OOS_START = pd.Timestamp(FIB_FINE_OOS_START)


def _split(df: pd.DataFrame):
    if df.empty:
        return df, df
    d = pd.to_datetime(df["date"])
    is_df = df[d <= IS_END].reset_index(drop=True)
    oos_df = df[d >= OOS_START].reset_index(drop=True)
    return is_df, oos_df


def _metrics(df: pd.DataFrame) -> dict:
    s = compute_stats(df)
    return {
        "n": int(s["n"]),
        "pf": round(float(s["pf"]), 3),
        "pnl": round(float(s["pnl"]), 0),
        "dd": round(float(s["dd"]), 0),
        "wr": round(float(s["wr"]), 1),
        "sharpe": round(float(s["sharpe"]), 2),
    }


def _bootstrap(df: pd.DataFrame) -> float:
    if df is None or len(df) < 10:
        return 0.0
    try:
        r = block_bootstrap(df, n_iterations=1000, metric="profit_factor", threshold=1.0, seed=42)
        # p_above_threshold est DÉJÀ un pourcentage (0–100).
        return round(float(r.get("p_above_threshold", 0.0)), 1)
    except Exception as e:  # pragma: no cover
        print("  bootstrap err:", e)
        return 0.0


def run_comparison():
    os.makedirs(TRADES_DIR, exist_ok=True)
    results = {}  # (ticker, tf) -> dict
    all_trades = {}  # (ticker, tf) -> DataFrame
    portfolio = {tf: [] for tf in TFS}

    for tf in TFS:
        for ticker in TICKERS:
            df = run_backtest(None, ticker, params={"timeframe": tf, "source": "resampled"})
            all_trades[(ticker, tf)] = df
            df.to_csv(f"{TRADES_DIR}/{ticker}_{tf}.csv", index=False)
            is_df, oos_df = _split(df)
            results[f"{ticker}_{tf}"] = {
                "ticker": ticker,
                "tf": tf,
                "full": _metrics(df),
                "is": _metrics(is_df),
                "oos": _metrics(oos_df),
                "oos_bootstrap": _bootstrap(oos_df),
            }
            portfolio[tf].append(oos_df)
            o = results[f"{ticker}_{tf}"]["oos"]
            print(
                f"[{tf}] {ticker}: OOS PF={o['pf']} pnl=${o['pnl']:.0f} n={o['n']} "
                f"bs={results[f'{ticker}_{tf}']['oos_bootstrap']}%"
            )

    # Portefeuille par TF (OOS).
    port_results = {}
    for tf in TFS:
        parts = [d for d in portfolio[tf] if len(d)]
        pdf = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
        if len(pdf):
            pdf = pdf.sort_values("fill_time").reset_index(drop=True)
        port_results[tf] = {
            "oos": _metrics(pdf),
            "oos_bootstrap": _bootstrap(pdf),
        }
        # Sauvegarde du portefeuille OOS pour robustesse.
        pdf.to_csv(f"{TRADES_DIR}/PORTFOLIO_{tf}_oos.csv", index=False)
        m = port_results[tf]["oos"]
        print(
            f"[{tf}] PORTFOLIO OOS: PF={m['pf']} pnl=${m['pnl']:.0f} n={m['n']} "
            f"DD=${m['dd']:.0f} bs={port_results[tf]['oos_bootstrap']}%"
        )

    comparison = {
        "is_end": str(IS_END.date()),
        "oos_start": str(OOS_START.date()),
        "per_cell": results,
        "portfolio": port_results,
    }
    with open(f"{OUT}/full/comparison.json", "w") as f:
        json.dump(comparison, f, indent=2)
    return comparison, all_trades


def run_robustness(all_trades: dict):
    """Robustesse complète sur le portefeuille du MEILLEUR TF (décidé par PF OOS)."""
    with open(f"{OUT}/full/comparison.json") as f:
        comp = json.load(f)
    # Choix du TF gagnant : PF OOS portefeuille le plus haut avec n_oos >= 20 et pnl > 0.
    rank = []
    for tf in TFS:
        m = comp["portfolio"][tf]["oos"]
        rank.append((tf, m["pf"], m["n"], m["pnl"]))
    valid = [r for r in rank if r[2] >= 20 and r[3] > 0]
    pool = valid if valid else rank
    best_tf = max(pool, key=lambda r: r[1])[0]

    # Portefeuille OOS du best_tf.
    parts = [all_trades[(t, best_tf)] for t in TICKERS]
    parts = [d for d in parts if len(d)]
    pdf = pd.concat(parts, ignore_index=True)
    d = pd.to_datetime(pdf["date"])
    oos = pdf[d >= OOS_START].sort_values("fill_time").reset_index(drop=True)

    # Nombre de configs testées (grille) pour Bonferroni : 3 niveaux × 4 wick × 3 sl
    n_tested = 3 * 4 * 3
    bf = bonferroni_threshold(alpha=0.05, n_tests=n_tested)
    boot = block_bootstrap(oos, n_iterations=1000, metric="profit_factor", threshold=1.0, seed=42)
    psr = probabilistic_sharpe_ratio(oos["pnl"].values, sr_target=0.0)
    mc = monte_carlo_drawdown(oos, n_iterations=1000, topstep_dd_remaining=2000.0, seed=42)
    stress = regime_stress_test(oos)
    wcc = worst_case_clustering(oos, n_worst=20)

    # p_above_threshold est en % (0–100).
    boot_pct = float(boot.get("p_above_threshold", 0.0))
    bf_p = float(bf.get("p_corrected", 0.05 / n_tested))
    bf_threshold_pct = float(bf.get("bootstrap_threshold_pct", 100.0 * (1.0 - bf_p)))
    # Bonferroni OK si le bootstrap (PF>1) dépasse le seuil corrigé.
    bonferroni_ok = boot_pct >= bf_threshold_pct
    boot_p = boot_pct / 100.0

    rob = {
        "best_tf": best_tf,
        "n_configs_tested": n_tested,
        "bonferroni": {
            "p_corrected": bf_p,
            "bootstrap_threshold_pct": round(bf_threshold_pct, 2),
            "bootstrap_p_pct": round(boot_pct, 2),
            "ok": bool(bonferroni_ok),
        },
        "psr_0": round(float(psr.get("psr", 0.0)), 4),
        "monte_carlo": {
            "dd_p95": round(float(mc.get("dd_p95_worst", 0.0)), 0),
            "dd_p99": round(float(mc.get("dd_p99_worst", 0.0)), 0),
            "dd_median": round(float(mc.get("dd_median", 0.0)), 0),
            "dd_topstep_breach_pct": round(float(mc.get("dd_topstep_breach_pct", 0.0)), 1),
        },
        "stress": stress.to_dict(orient="records") if hasattr(stress, "to_dict") else str(stress),
        "worst_case_clustering": wcc if isinstance(wcc, dict) else str(wcc),
    }
    with open(f"{OUT}/full/robustness.json", "w") as f:
        json.dump(rob, f, indent=2, default=str)
    print("\n=== ROBUSTESSE (best_tf =", best_tf, ") ===")
    print(json.dumps({k: v for k, v in rob.items() if k != "stress"}, indent=2, default=str))
    print("STRESS:")
    print(stress.to_string() if hasattr(stress, "to_string") else stress)
    return rob


if __name__ == "__main__":
    comp, trades = run_comparison()
    run_robustness(trades)
