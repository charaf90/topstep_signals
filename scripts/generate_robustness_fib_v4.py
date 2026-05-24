"""
Génère output/robustness_fib-v4.{json,md} au format standard FORGE.

Utilise core.robustness.run_full_robustness pour agréger :
  • bootstrap PF / PnL / Sharpe (stationnaire, 1000 itér.)
  • Monte Carlo DD (1000 itér., distribution P5/P50/P95/P99)
  • PSR (Bailey & López de Prado)
  • Bonferroni (5 tests = 5 seuils wick par cellule grid Phase 4)
  • Worst-case clustering (top 20)
  • Régime stress (si colonnes nécessaires présentes)

Périmètre : 3 cellules 🟢 retenues (MES1 m15 fib=0.382, NQ1 m15 fib=0.382,
MGC1 m15 fib=0.5 avec skip_macro=True).

Le rapport est restreint à l'**OOS** (2025-10-01 → 2026-05-31) — c'est sur
ces trades que la décision de promotion s'appuie.

Usage :
  python scripts/generate_robustness_fib_v4.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import (
    COMMISSION_RT_PER_CONTRACT,
    FIB_V4_LEVEL_PER_TICKER,
    FIB_V4_TICKERS,
    INSTRUMENTS,
    SLIPPAGE_TICKS_PER_TICKER,
)
from core.data import load_csv
from core.robustness import (
    format_summary_markdown,
    run_full_robustness,
)
from core.strategy_fib_v4 import run_fib_v4_backtest

DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = PROJECT_ROOT / "output"
OOS_START = pd.Timestamp("2025-10-01")
OOS_END = pd.Timestamp("2026-05-31 23:59:59")
STRATEGY_ID = "fib-v4"


def cost_rt(ticker: str) -> float:
    slip = SLIPPAGE_TICKS_PER_TICKER.get(ticker, 1)
    tick = INSTRUMENTS[ticker]["tick_size"]
    dpp = INSTRUMENTS[ticker]["dollar_per_point"]
    return 2.0 * slip * tick * dpp + COMMISSION_RT_PER_CONTRACT


def collect_oos_trades(ticker: str) -> pd.DataFrame:
    df = load_csv(str(DATA_DIR / f"{ticker}_data_m15.csv"))
    trades = run_fib_v4_backtest(
        df,
        ticker,
        fib_level=FIB_V4_LEVEL_PER_TICKER[ticker],
    )
    if len(trades) == 0:
        return trades
    trades = trades[trades["result"].isin(["TP", "SL", "TE"])].copy()
    # frictions
    rt = cost_rt(ticker)
    trades["pnl_gross"] = trades["pnl"]
    trades["pnl"] = trades["pnl_gross"] - rt * trades["n_ct"]
    # filtre OOS
    trades["pending_dt"] = pd.to_datetime(trades["pending_time"])
    trades = trades[(trades["pending_dt"] >= OOS_START) & (trades["pending_dt"] <= OOS_END)].copy()
    # colonnes attendues par regime_stress_test (peut nécessiter ajustements)
    if "date" not in trades.columns:
        trades["date"] = trades["pending_dt"].dt.strftime("%Y-%m-%d")
    return trades.reset_index(drop=True)


def main():
    print("  Collecte trades OOS sur les 3 cellules 🟢 fib-v4...")
    parts = []
    per_ticker = {}
    for ticker in FIB_V4_TICKERS:
        t = collect_oos_trades(ticker)
        parts.append(t)
        per_ticker[ticker] = dict(
            n=int(len(t)),
            pnl_total=float(t["pnl"].sum()) if len(t) else 0.0,
            wr=float((t["pnl"] > 0).mean()) if len(t) else float("nan"),
        )
        print(f"    {ticker}: {len(t)} trades OOS, P&L=${t['pnl'].sum():+,.0f}")

    portfolio = pd.concat(parts, ignore_index=True).sort_values("pending_dt")
    print(f"\n  Portefeuille OOS : {len(portfolio)} trades, P&L=${portfolio['pnl'].sum():+,.0f}")

    # n_tests Bonferroni : 5 seuils wick × 3 cellules = 15 tests environ ;
    # mais le test pertinent est la sélection par cellule = 5 par ticker, soit
    # 15 globalement. On utilise 15 (conservateur).
    n_tests_bonferroni = 15

    # Limite Topstep restante : on a TOPSTEP_DAILY_LOSS_MAX=1000 pour ref,
    # mais le DD réel doit comparer à la limite TRAILING (TOPSTEP_TRAILING_DD=2000).
    # On choisit la plus contraignante (trailing dd) pour MC DD probabilité.
    from config import TOPSTEP_TRAILING_DD

    topstep_limit = TOPSTEP_TRAILING_DD

    print(f"\n  Génération robustness portefeuille (PORTFOLIO, n={len(portfolio)})...")
    results = run_full_robustness(
        portfolio,
        n_strategies_tested=n_tests_bonferroni,
        topstep_dd_remaining=topstep_limit,
        seed=42,
    )

    # Détail par cellule (en complément, pas demandé par run_full_robustness)
    per_ticker_robustness = {}
    for ticker, sub in zip(FIB_V4_TICKERS, parts):
        if len(sub) < 10:
            per_ticker_robustness[ticker] = {"n": len(sub), "skip": "n<10"}
            continue
        r = run_full_robustness(
            sub,
            n_strategies_tested=n_tests_bonferroni,
            topstep_dd_remaining=topstep_limit,
            seed=42,
        )
        per_ticker_robustness[ticker] = dict(
            n=int(len(sub)),
            pnl=float(sub["pnl"].sum()),
            bootstrap_pf_p_above=float(r["bootstrap_pf"]["p_above_threshold"]),
            bootstrap_pnl_p_above=float(r["bootstrap_pnl"]["p_above_threshold"]),
            psr=float(r["psr"].get("psr", float("nan"))),
            mc_dd_p95=float(r["monte_carlo_dd"]["dd_p95_worst"]),
            mc_dd_p99=float(r["monte_carlo_dd"]["dd_p99_worst"]),
        )

    # Enrichir le résultat principal pour traçabilité
    payload = {
        "strategy_id": STRATEGY_ID,
        "periode_oos": f"{OOS_START.date()} → {OOS_END.date()}",
        "tickers_retenus": FIB_V4_TICKERS,
        "params_per_ticker": {
            t: {
                "fib_level": FIB_V4_LEVEL_PER_TICKER[t],
                **per_ticker[t],
            }
            for t in FIB_V4_TICKERS
        },
        "per_ticker_robustness": per_ticker_robustness,
        **{k: v for k, v in results.items() if k != "format_summary_markdown"},
    }

    # Sauvegarde JSON
    json_path = OUTPUT_DIR / f"robustness_{STRATEGY_ID}.json"
    json_path.write_text(json.dumps(payload, indent=2, default=str))
    print(f"  ✅ JSON : {json_path}")

    # Markdown via format_summary_markdown (standard projet)
    md_body = format_summary_markdown(results)

    md = []
    md.append(f"# Robustness — {STRATEGY_ID}\n")
    md.append(f"**Période OOS** : {OOS_START.date()} → {OOS_END.date()}")
    md.append(f"**Univers** : {', '.join(FIB_V4_TICKERS)} (M15)")
    md.append(
        "**Filtres data-driven** : wick_through_atr (seuil par ticker) "
        "+ pivot_break_atr ≥ 0 + skip macro days MGC1"
    )
    md.append(f"**Bonferroni** : {n_tests_bonferroni} tests (5 seuils wick × 3 cellules)\n")

    md.append("## Synthèse par ticker (OOS)\n")
    md.append("| Ticker | n | P&L | WR | Bootstrap PF P(>1) | PSR | DD P95 | DD P99 |")
    md.append("|---|---|---|---|---|---|---|---|")
    for t in FIB_V4_TICKERS:
        d = per_ticker_robustness.get(t, {})
        if d.get("skip"):
            md.append(f"| {t} | {d.get('n', 0)} | — | — | — | — | — | — |")
            continue
        md.append(
            f"| {t} | {d['n']} | ${d['pnl']:+,.0f} "
            f"| {per_ticker[t]['wr'] * 100:.1f}% "
            f"| {d['bootstrap_pf_p_above']:.1f}% "
            f"| {d['psr']:.1f}% "
            f"| ${d['mc_dd_p95']:+,.0f} | ${d['mc_dd_p99']:+,.0f} |"
        )

    md.append("\n## Portefeuille agrégé (OOS, 3 tickers)\n")
    md.append(md_body)

    md_path = OUTPUT_DIR / f"robustness_{STRATEGY_ID}.md"
    md_path.write_text("\n".join(md))
    print(f"  ✅ MD   : {md_path}")


if __name__ == "__main__":
    main()
