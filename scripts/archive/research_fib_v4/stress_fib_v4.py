"""
Phase 6 — Stress tests fib-v4.

Pour chacune des 3 cellules retenues 🟢 (MES1/NQ1/MGC1), évalue :
  • Décomposition par régime (trend BULL/BEAR, ATR haute/basse, macro days)
  • Monte Carlo permutation sur le DD attendu (1000 itérations)
  • Worst-case clustering : profil des 20 pires trades vs la moyenne
  • Sensibilité paramètres : wick_max ±20%, pivot_buffer ∈ {0, 0.1, 0.2}

Sortie :
  • output/stress_fib_v4.md

Usage :
  python scripts/stress_fib_v4.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import (
    COMMISSION_RT_PER_CONTRACT,
    FIB_V4_PIVOT_BREAK_BUFFER_ATR_PER_TICKER,
    FIB_V4_WICK_THROUGH_MAX_ATR_PER_TICKER,
    INSTRUMENTS,
    SLIPPAGE_TICKS_PER_TICKER,
)
from core.data import load_csv
from core.strategy_fib_v4 import run_fib_v4_backtest

OUTPUT_DIR = PROJECT_ROOT / "output"
DATA_DIR = PROJECT_ROOT / "data"

# Cellules 🟢 retenues Phase 4
PROD_CELLS = [
    ("MES1", "m15", 0.382),
    ("NQ1", "m15", 0.382),
    ("MGC1", "m15", 0.500),
]


def cost_rt(ticker: str) -> float:
    slip = SLIPPAGE_TICKS_PER_TICKER.get(ticker, 1)
    tick = INSTRUMENTS[ticker]["tick_size"]
    dpp = INSTRUMENTS[ticker]["dollar_per_point"]
    return 2.0 * slip * tick * dpp + COMMISSION_RT_PER_CONTRACT


def pf(arr: np.ndarray) -> float:
    gp = arr[arr > 0].sum()
    gl = -arr[arr < 0].sum()
    if gl <= 0:
        return float("inf") if gp > 0 else float("nan")
    return float(gp / gl)


def metrics(trades: pd.DataFrame) -> dict:
    if len(trades) == 0:
        return dict(n=0, pf=float("nan"), pnl=0.0, dd=0.0, wr=float("nan"))
    pnl = trades["pnl"].to_numpy()
    eq = trades.sort_values("pending_time")["pnl"].cumsum()
    dd = float((eq - eq.cummax()).min()) if len(eq) else 0.0
    return dict(
        n=len(trades),
        pf=pf(pnl),
        pnl=float(pnl.sum()),
        dd=dd,
        wr=float((pnl > 0).mean()),
    )


def run_cell(
    ticker: str, tf: str, fib_level: float, wick_max: float = None, pivot_buffer: float = None
) -> pd.DataFrame:
    df = load_csv(str(DATA_DIR / f"{ticker}_data_{tf}.csv"))
    trades = run_fib_v4_backtest(
        df,
        ticker,
        fib_level=fib_level,
        wick_max_atr=(
            wick_max if wick_max is not None else FIB_V4_WICK_THROUGH_MAX_ATR_PER_TICKER[ticker]
        ),
        pivot_break_buffer_atr=(
            pivot_buffer
            if pivot_buffer is not None
            else FIB_V4_PIVOT_BREAK_BUFFER_ATR_PER_TICKER[ticker]
        ),
    )
    if len(trades) == 0:
        return trades
    trades = trades[trades["result"].isin(["TP", "SL", "TE"])].copy()
    rt = cost_rt(ticker)
    trades["pnl_gross"] = trades["pnl"]
    trades["pnl"] = trades["pnl_gross"] - rt * trades["n_ct"]
    trades["pending_time"] = pd.to_datetime(trades["pending_time"])
    return trades


def regime_breakdown(trades: pd.DataFrame) -> dict:
    if "trend" not in trades.columns or len(trades) == 0:
        return {}
    out = {}
    # Par direction
    for direction in ["long", "short"]:
        sub = trades[trades["direction"] == direction]
        if len(sub) >= 3:
            out[f"direction={direction}"] = metrics(sub)
    # Par tendance (BULL/BEAR)
    for trend_val in ["BULL", "BEAR"]:
        sub = trades[trades["trend"] == trend_val]
        if len(sub) >= 3:
            out[f"trend={trend_val}"] = metrics(sub)
    # ATR (utilise atr stocké) — quintile haut/bas
    if "atr" in trades.columns and len(trades) >= 10:
        q33, q66 = trades["atr"].quantile([0.33, 0.66])
        low = trades[trades["atr"] <= q33]
        mid = trades[(trades["atr"] > q33) & (trades["atr"] <= q66)]
        high = trades[trades["atr"] > q66]
        if len(low) >= 3:
            out["atr=low"] = metrics(low)
        if len(mid) >= 3:
            out["atr=mid"] = metrics(mid)
        if len(high) >= 3:
            out["atr=high"] = metrics(high)
    # Macro days
    if "is_macro_day" in trades.columns:
        for val in [True, False]:
            sub = trades[trades["is_macro_day"] == val]
            if len(sub) >= 3:
                out[f"is_macro_day={val}"] = metrics(sub)
    return out


def monte_carlo_dd(trades: pd.DataFrame, n_iter: int = 1000, seed: int = 42) -> dict:
    """Permutation random de l'ordre des trades → distribution du DD attendu."""
    if len(trades) < 10:
        return {}
    pnls = trades["pnl"].to_numpy()
    rng = np.random.default_rng(seed)
    dds = np.empty(n_iter)
    for k in range(n_iter):
        perm = rng.permutation(pnls)
        eq = np.cumsum(perm)
        dds[k] = (eq - np.maximum.accumulate(eq)).min()
    return dict(
        observed_dd=float(
            (
                trades.sort_values("pending_time")["pnl"].cumsum()
                - trades.sort_values("pending_time")["pnl"].cumsum().cummax()
            ).min()
        ),
        mean_dd=float(dds.mean()),
        p5_dd=float(np.percentile(dds, 5)),
        p50_dd=float(np.percentile(dds, 50)),
        p95_dd=float(np.percentile(dds, 95)),
        p99_dd=float(np.percentile(dds, 99)),
    )


def worst_case_profile(trades: pd.DataFrame, k: int = 20) -> dict:
    """Compare le profil moyen des k pires trades vs la population."""
    if len(trades) < k * 2:
        return {}
    worst = trades.nsmallest(k, "pnl")
    rest = trades[~trades.index.isin(worst.index)]
    feats = [
        "wick_through_atr",
        "pivot_break_atr",
        "mae_pending_atr",
        "bars_to_fill",
        "adx_at_arm",
        "bars_since_confirm",
    ]
    out = {}
    for f in feats:
        if f not in trades.columns:
            continue
        out[f] = dict(
            worst_mean=float(worst[f].mean()),
            worst_median=float(worst[f].median()),
            rest_mean=float(rest[f].mean()),
            rest_median=float(rest[f].median()),
        )
    return out


def sensitivity_analysis(ticker: str, tf: str, fib_level: float) -> list:
    """Sensibilité aux paramètres : varie wick_max et pivot_buffer."""
    base_wmax = FIB_V4_WICK_THROUGH_MAX_ATR_PER_TICKER[ticker]
    rows = []
    # Variation wick (±20%)
    for delta_pct in [-30, -20, -10, 0, 10, 20, 30]:
        wmax = base_wmax * (1 + delta_pct / 100)
        for pbuf in [0.0, 0.10, 0.20]:
            trades = run_cell(ticker, tf, fib_level, wick_max=wmax, pivot_buffer=pbuf)
            m = metrics(trades)
            rows.append(
                dict(
                    ticker=ticker,
                    wick_max=round(wmax, 4),
                    wick_delta_pct=delta_pct,
                    pivot_buffer=pbuf,
                    **m,
                )
            )
    return rows


def main():
    lines = []
    lines.append("# Phase 6 — Stress tests fib-v4\n")
    lines.append("Analyse de robustesse des 3 cellules 🟢 retenues Phase 4.\n")

    sensitivity_all = []

    for ticker, tf, fib_level in PROD_CELLS:
        print(f"\n  ── {ticker} {tf} fib={fib_level} ──")
        trades = run_cell(ticker, tf, fib_level)
        m = metrics(trades)
        print(
            f"    Baseline : n={m['n']}, PF={m['pf']:.2f}, "
            f"P&L=${m['pnl']:+,.0f}, DD=${m['dd']:+,.0f}"
        )

        lines.append(
            f"\n## {ticker} (m15 fib={fib_level}, wick_max="
            f"{FIB_V4_WICK_THROUGH_MAX_ATR_PER_TICKER[ticker]})\n"
        )
        lines.append(
            f"\n**Baseline complet (toute l'historique)** : n={m['n']}, "
            f"PF={m['pf']:.2f}, P&L=${m['pnl']:+,.0f}, DD=${m['dd']:+,.0f}, "
            f"WR={m['wr']:.1%}\n"
        )

        # Régimes
        lines.append("\n### Décomposition par régime\n")
        lines.append("| Régime | n | PF | P&L | DD | WR |")
        lines.append("|---|---|---|---|---|---|")
        for regime_key, regime_m in regime_breakdown(trades).items():
            lines.append(
                f"| {regime_key} | {regime_m['n']} | {regime_m['pf']:.2f} "
                f"| ${regime_m['pnl']:+,.0f} | ${regime_m['dd']:+,.0f} "
                f"| {regime_m['wr']:.1%} |"
            )

        # Monte Carlo DD
        mc = monte_carlo_dd(trades)
        if mc:
            lines.append("\n### Monte Carlo DD (1000 permutations de l'ordre)\n")
            lines.append(f"- DD observé : ${mc['observed_dd']:+,.0f}")
            lines.append(f"- DD attendu (mean) : ${mc['mean_dd']:+,.0f}")
            lines.append(
                f"- P5 : ${mc['p5_dd']:+,.0f}  | P50 : "
                f"${mc['p50_dd']:+,.0f}  | P95 : ${mc['p95_dd']:+,.0f}  | "
                f"P99 : ${mc['p99_dd']:+,.0f}"
            )

        # Worst case
        wc = worst_case_profile(trades)
        if wc:
            lines.append("\n### Profil des 20 pires trades vs reste\n")
            lines.append(
                "| Feature | Worst mean | Worst median | Rest mean | Rest median | Δ médiane |"
            )
            lines.append("|---|---|---|---|---|---|")
            for f, vals in wc.items():
                delta = vals["worst_median"] - vals["rest_median"]
                lines.append(
                    f"| `{f}` | {vals['worst_mean']:.3f} | {vals['worst_median']:.3f} "
                    f"| {vals['rest_mean']:.3f} | {vals['rest_median']:.3f} "
                    f"| {delta:+.3f} |"
                )

        # Sensibilité
        sens = sensitivity_analysis(ticker, tf, fib_level)
        sensitivity_all.extend(sens)

        # Pour ce ticker, tableau de sensibilité résumé
        lines.append("\n### Sensibilité paramètres\n")
        lines.append("| wick_max | Δ% | pivot_buffer | n | PF | P&L | DD |")
        lines.append("|---|---|---|---|---|---|---|")
        for r in sens:
            lines.append(
                f"| {r['wick_max']} | {r['wick_delta_pct']:+d}% "
                f"| {r['pivot_buffer']} | {r['n']} | {r['pf']:.2f} "
                f"| ${r['pnl']:+,.0f} | ${r['dd']:+,.0f} |"
            )

    # CSV sensibilité
    df_sens = pd.DataFrame(sensitivity_all)
    df_sens.to_csv(OUTPUT_DIR / "fib_v4_sensitivity.csv", index=False)
    lines.append("\n\n## Annexe — Sensibilité globale\n")
    lines.append(
        f"Données complètes : `output/fib_v4_sensitivity.csv` " f"({len(df_sens)} configs)"
    )

    out_path = OUTPUT_DIR / "stress_fib_v4.md"
    out_path.write_text("\n".join(lines))
    print(f"\n  ✅ Rapport : {out_path}")
    print("  ✅ Sensibilité CSV : output/fib_v4_sensitivity.csv")


if __name__ == "__main__":
    main()
