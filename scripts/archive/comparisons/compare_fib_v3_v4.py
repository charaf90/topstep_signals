"""
Phase 7 — Comparaison apples-to-apples fib-v3 vs fib-v4.

Sur la même période (totale puis OOS 2025-10-01 → 2026-05-31), pour
chaque cellule retenue, compare :
  • ΔPF, ΔP&L, ΔDD, ΔSharpe, ΔWR par ticker
  • Corrélation P&L daily v3 ↔ v4 (cible [0.5, 0.95])
  • Sélectivité : trades v3 refusés par v4 — leur P&L cumulé

Sortie :
  • output/compare_fib_v3_v4.md
  • output/fib_v4_compare_pnl.png  (cumulative P&L curves)

Note : fib-v3 a un filtre trigger par ticker (FIB_TRIGGER_FILTERS_PER_TICKER).
Pour une comparaison juste, on compare le fib-v3 ACTUEL (avec filtres trigger
en place, équivalent live) vs fib-v4 (avec filtres pivot break + wick).

Usage :
  python scripts/compare_fib_v3_v4.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import matplotlib

matplotlib.use("Agg")
import core.strategy_fib as sf
import matplotlib.pyplot as plt

from config import (
    COMMISSION_RT_PER_CONTRACT,
    FIB_LEVEL_PER_TICKER,
    FIB_V4_LEVEL_PER_TICKER,
    INSTRUMENTS,
    SLIPPAGE_TICKS_PER_TICKER,
)
from core.data import load_csv
from core.strategy_fib_v4 import run_fib_v4_backtest

DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = PROJECT_ROOT / "output"
OOS_START = pd.Timestamp("2025-10-01")
OOS_END = pd.Timestamp("2026-05-31 23:59:59")

PROD_CELLS = [
    ("MES1", "m15"),
    ("NQ1", "m15"),
    ("MGC1", "m15"),
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


def apply_frictions(trades: pd.DataFrame, ticker: str) -> pd.DataFrame:
    if len(trades) == 0:
        return trades
    rt = cost_rt(ticker)
    trades = trades.copy()
    trades["pnl_gross"] = trades["pnl"]
    trades["pnl"] = trades["pnl_gross"] - rt * trades["n_ct"]
    return trades


def metrics(trades: pd.DataFrame) -> dict:
    if len(trades) == 0:
        return dict(n=0, pf=float("nan"), pnl=0.0, dd=0.0, sharpe=float("nan"), wr=float("nan"))
    eq = trades.sort_values("pending_time")["pnl"].cumsum()
    dd = float((eq - eq.cummax()).min())
    std = trades["pnl"].std(ddof=1)
    sharpe = (trades["pnl"].mean() / std) if std and std > 0 else float("nan")
    return dict(
        n=len(trades),
        pf=pf(trades["pnl"].to_numpy()),
        pnl=float(trades["pnl"].sum()),
        dd=dd,
        sharpe=float(sharpe) if not np.isnan(sharpe) else float("nan"),
        wr=float((trades["pnl"] > 0).mean()),
    )


def run_v3(ticker: str, fib_level: float) -> pd.DataFrame:
    """fib-v3 actuel — avec ses filtres trigger calibrés (équivalent live)."""
    df = load_csv(str(DATA_DIR / f"{ticker}_data_m15.csv"))
    trades = sf.run_fib_backtest(df, ticker, fib_level=fib_level, apply_filter=True)
    if len(trades) == 0:
        return trades
    trades = trades[trades["result"].isin(["TP", "SL", "TE"])].copy()
    trades = apply_frictions(trades, ticker)
    trades["pending_time"] = pd.to_datetime(trades["pending_time"])
    return trades


def run_v4(ticker: str, fib_level: float) -> pd.DataFrame:
    """fib-v4 — nouvelle config (pivot break + wick excess data-driven)."""
    df = load_csv(str(DATA_DIR / f"{ticker}_data_m15.csv"))
    trades = run_fib_v4_backtest(df, ticker, fib_level=fib_level)
    if len(trades) == 0:
        return trades
    trades = trades[trades["result"].isin(["TP", "SL", "TE"])].copy()
    trades = apply_frictions(trades, ticker)
    trades["pending_time"] = pd.to_datetime(trades["pending_time"])
    return trades


def slice_oos(trades: pd.DataFrame) -> pd.DataFrame:
    return trades[
        (trades["pending_time"] >= OOS_START) & (trades["pending_time"] <= OOS_END)
    ].copy()


def daily_pnl(trades: pd.DataFrame) -> pd.Series:
    if len(trades) == 0:
        return pd.Series(dtype=float)
    trades = trades.copy()
    trades["day"] = trades["pending_time"].dt.date
    return trades.groupby("day")["pnl"].sum()


def main():
    lines = []
    lines.append("# Phase 7 — Comparaison apples-to-apples fib-v3 vs fib-v4\n")
    lines.append("**Périmètre** : 3 tickers retenus production (MES1/NQ1/MGC1).\n")
    lines.append(
        "- **fib-v3** : configuration LIVE actuelle "
        "(`FIB_LEVEL_PER_TICKER=0.382` + filtres trigger calibrés)."
    )
    lines.append(
        "- **fib-v4** : `FIB_V4_LEVEL_PER_TICKER` + invalidation pivot "
        "break + wick excess data-driven.\n"
    )
    lines.append(
        "Frictions appliquées : slippage `config.SLIPPAGE_TICKS_PER_TICKER` "
        "(2 côtés) + commission RT $1.40.\n"
    )

    portfolio_v3, portfolio_v4 = [], []

    lines.append("\n## Comparaison TOTAL (toute l'historique sept 2024 → mai 2026)\n")
    lines.append(
        "| Ticker | v3 n | v3 PF | v3 P&L | v3 DD | v4 n | v4 PF | v4 P&L | v4 DD | Sélect. |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for ticker, tf in PROD_CELLS:
        fib_lvl_v3 = FIB_LEVEL_PER_TICKER[ticker]  # actuel : 0.382
        fib_lvl_v4 = FIB_V4_LEVEL_PER_TICKER[ticker]
        print(f"  ── {ticker} (v3 fib={fib_lvl_v3}, v4 fib={fib_lvl_v4}) ──")
        trades_v3 = run_v3(ticker, fib_lvl_v3)
        trades_v4 = run_v4(ticker, fib_lvl_v4)
        portfolio_v3.append(trades_v3)
        portfolio_v4.append(trades_v4)

        m3, m4 = metrics(trades_v3), metrics(trades_v4)
        select = 1.0 - (len(trades_v4) / max(1, len(trades_v3)))
        lines.append(
            f"| {ticker} | {m3['n']} | {m3['pf']:.2f} | ${m3['pnl']:+,.0f} "
            f"| ${m3['dd']:+,.0f} | {m4['n']} | {m4['pf']:.2f} "
            f"| ${m4['pnl']:+,.0f} | ${m4['dd']:+,.0f} "
            f"| {select:.0%} |"
        )

    # Portefeuille agrégé
    all_v3 = pd.concat(portfolio_v3, ignore_index=True) if portfolio_v3 else pd.DataFrame()
    all_v4 = pd.concat(portfolio_v4, ignore_index=True) if portfolio_v4 else pd.DataFrame()
    mp3, mp4 = metrics(all_v3), metrics(all_v4)
    lines.append(
        f"| **PORTEFEUILLE** | **{mp3['n']}** | **{mp3['pf']:.2f}** "
        f"| **${mp3['pnl']:+,.0f}** | **${mp3['dd']:+,.0f}** "
        f"| **{mp4['n']}** | **{mp4['pf']:.2f}** "
        f"| **${mp4['pnl']:+,.0f}** | **${mp4['dd']:+,.0f}** "
        f"| **{1.0 - mp4['n']/max(1, mp3['n']):.0%}** |"
    )

    # ── OOS ──
    lines.append("\n\n## Comparaison OOS (2025-10-01 → 2026-05-31)\n")
    lines.append("| Ticker | v3 n | v3 PF | v3 P&L | v4 n | v4 PF | v4 P&L | ΔP&L | Sélect. |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    portfolio_v3_oos, portfolio_v4_oos = [], []
    for ticker, tf in PROD_CELLS:
        # Re-load (déjà calculés au-dessus mais on garde simple)
        fib_lvl_v3 = FIB_LEVEL_PER_TICKER[ticker]
        fib_lvl_v4 = FIB_V4_LEVEL_PER_TICKER[ticker]
        t3 = slice_oos(run_v3(ticker, fib_lvl_v3))
        t4 = slice_oos(run_v4(ticker, fib_lvl_v4))
        portfolio_v3_oos.append(t3)
        portfolio_v4_oos.append(t4)
        m3, m4 = metrics(t3), metrics(t4)
        select = 1.0 - (len(t4) / max(1, len(t3)))
        delta_pnl = m4["pnl"] - m3["pnl"]
        lines.append(
            f"| {ticker} | {m3['n']} | {m3['pf']:.2f} | ${m3['pnl']:+,.0f} "
            f"| {m4['n']} | {m4['pf']:.2f} | ${m4['pnl']:+,.0f} "
            f"| ${delta_pnl:+,.0f} | {select:.0%} |"
        )

    pf_v3_oos = pd.concat(portfolio_v3_oos, ignore_index=True)
    pf_v4_oos = pd.concat(portfolio_v4_oos, ignore_index=True)
    mp3_oos, mp4_oos = metrics(pf_v3_oos), metrics(pf_v4_oos)
    lines.append(
        f"| **PORTEFEUILLE OOS** | **{mp3_oos['n']}** | "
        f"**{mp3_oos['pf']:.2f}** | **${mp3_oos['pnl']:+,.0f}** "
        f"| **{mp4_oos['n']}** | **{mp4_oos['pf']:.2f}** "
        f"| **${mp4_oos['pnl']:+,.0f}** | **${mp4_oos['pnl']-mp3_oos['pnl']:+,.0f}** "
        f"| **{1.0 - mp4_oos['n']/max(1, mp3_oos['n']):.0%}** |"
    )

    # ── Corrélation P&L daily ──
    lines.append("\n\n## Corrélation P&L daily v3 ↔ v4 (OOS)\n")
    lines.append("Cible saine : `[0.5, 0.95]` (ni redondance pure, ni dérive).\n")
    lines.append("| Ticker | Corrélation Pearson | n jours en commun | Verdict |")
    lines.append("|---|---|---|---|")
    for ticker, t3, t4 in zip([c[0] for c in PROD_CELLS], portfolio_v3_oos, portfolio_v4_oos):
        d3 = daily_pnl(t3)
        d4 = daily_pnl(t4)
        joined = pd.DataFrame({"v3": d3, "v4": d4}).fillna(0)
        n_common = (joined.abs().sum(axis=1) > 0).sum()
        if n_common < 5:
            lines.append(f"| {ticker} | — | {n_common} | n trop faible |")
            continue
        r = joined["v3"].corr(joined["v4"])
        if 0.5 <= r <= 0.95:
            verdict = "✅ cible"
        elif r > 0.95:
            verdict = "⚠️ redondance"
        else:
            verdict = "⚠️ divergence"
        lines.append(f"| {ticker} | {r:.3f} | {n_common} | {verdict} |")

    # ── Sélectivité : trades v3 supprimés par v4 — bons ou mauvais ? ──
    lines.append("\n\n## Sélectivité : trades v3 refusés par v4 (OOS)\n")
    lines.append(
        "Si la sélectivité est intelligente, les trades supprimés doivent "
        "avoir un P&L cumulé majoritairement négatif (= on a éliminé les pertes).\n"
    )
    lines.append(
        "| Ticker | Trades v3 OOS | Refusés par v4 | P&L cumulé refusés | " "PF des refusés |"
    )
    lines.append("|---|---|---|---|---|")
    for ticker, t3, t4 in zip([c[0] for c in PROD_CELLS], portfolio_v3_oos, portfolio_v4_oos):
        if len(t3) == 0:
            continue
        # Trades v3 fillés à des moments où v4 n'a pas de fill correspondant
        # (approximation : on identifie les fill_time uniques)
        fills_v4 = set(t4["fill_time"].astype(str)) if len(t4) else set()
        refused = t3[~t3["fill_time"].astype(str).isin(fills_v4)]
        pf_refused = pf(refused["pnl"].to_numpy()) if len(refused) else float("nan")
        lines.append(
            f"| {ticker} | {len(t3)} | {len(refused)} "
            f"| ${refused['pnl'].sum():+,.0f} "
            f"| {pf_refused:.2f} |"
        )

    # ── Equity curves ──
    print("\n  Génération courbes equity OOS...")
    fig, axes = plt.subplots(1, 3, figsize=(18, 5), squeeze=False)
    for ax, (ticker, _), t3, t4 in zip(axes[0], PROD_CELLS, portfolio_v3_oos, portfolio_v4_oos):
        if len(t3):
            t3 = t3.sort_values("pending_time")
            ax.plot(
                t3["pending_time"], t3["pnl"].cumsum(), label=f"v3 (n={len(t3)})", color="steelblue"
            )
        if len(t4):
            t4 = t4.sort_values("pending_time")
            ax.plot(
                t4["pending_time"],
                t4["pnl"].cumsum(),
                label=f"v4 (n={len(t4)})",
                color="darkorange",
            )
        ax.set_title(f"{ticker} — Equity OOS")
        ax.set_ylabel("P&L cumulé ($)")
        ax.legend()
        ax.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(OUTPUT_DIR / "fib_v4_compare_pnl.png", dpi=110)
    plt.close()
    lines.append("\n\n## Visuels\n")
    lines.append("- `output/fib_v4_compare_pnl.png` — courbes equity OOS par ticker")

    out_path = OUTPUT_DIR / "compare_fib_v3_v4.md"
    out_path.write_text("\n".join(lines))
    print(f"  ✅ Rapport : {out_path}")


if __name__ == "__main__":
    main()
