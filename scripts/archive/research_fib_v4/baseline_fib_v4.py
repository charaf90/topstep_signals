"""
Baseline fib-v4 — Phase 1 du plan d'amélioration de la stratégie Fibonacci.

Croise 5 tickers × 2 timeframes × 3 niveaux Fibonacci = 30 cellules.
Chaque cellule exécute `core.strategy_fib.run_fib_backtest` SANS filtre
trigger (apply_filter=False), puis applique slippage + commission pour
obtenir un P&L net comparable.

Sortie :
  • output/fib_v4_baseline.csv  — métriques structurées
  • output/fib_v4_baseline.md   — tableau Markdown lisible

Critère de filtrage pour les phases suivantes :
  • Cellules avec n_trades ≥ 50 ET PF ≥ 1.0  → "viable" (data science phase 2-3)
  • Sinon                                     → rejet structurel précoce

Méthodologie M5 :
  Les constantes `FIB_*` du module core/strategy_fib.py sont calibrées
  pour M15. Sur M5, on les scale ×3 pour conserver l'équivalence
  temporelle (pivots, ATR, EMA, lookbacks, timeouts). Cette adaptation
  est documentée dans le rapport.

Usage :
  python scripts/baseline_fib_v4.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

# Garantit l'import des modules projet quel que soit le cwd
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import core.strategy_fib as sf

from config import (
    COMMISSION_RT_PER_CONTRACT,
    FIB_MIN_IMPULSE_ATR_PER_TICKER,
    FIB_SL_ATR_MULT_PER_TICKER,
    FIB_TP_ATR_MULT_PER_TICKER,
    INSTRUMENTS,
    SLIPPAGE_TICKS_PER_TICKER,
)
from core.data import load_csv

# ── Paramètres de l'expérience ──────────────────────────────────────────────
TICKERS = ["MES1", "NQ1", "YM1", "MGC1", "MCL1"]
TIMEFRAMES = ["m15", "m5"]
FIB_LEVELS = [0.382, 0.50, 0.618]

DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = PROJECT_ROOT / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Constantes Fib pour M15 (valeurs natives config.py)
FIB_CONSTANTS_M15 = {
    "FIB_ATR_PERIOD": 14,
    "FIB_EMA_FAST_PERIOD": 50,
    "FIB_EMA_SLOW_PERIOD": 200,
    "FIB_ADX_PERIOD": 14,
    "FIB_PIVOT_LEFT": 8,
    "FIB_PIVOT_RIGHT": 8,
    "FIB_MAX_IMPULSE_BARS": 25,
    "FIB_IMPULSE_LOOKBACK": 60,
    "FIB_ORDER_TIMEOUT_BARS": 12,
    "FIB_MAX_HOLD_BARS": 32,
}

# M5 = M15 × 3 (3 barres M5 par barre M15)
FIB_CONSTANTS_M5 = {k: v * 3 for k, v in FIB_CONSTANTS_M15.items()}


def patch_fib_constants(constants: dict) -> None:
    """Monkey-patch les constantes du module strategy_fib.

    Nécessaire car run_fib_backtest lit les valeurs depuis le namespace
    du module (importées au top), donc le scaling M5 doit passer par
    une mutation du module lui-même.
    """
    for name, value in constants.items():
        setattr(sf, name, value)


def cost_per_round_trip(ticker: str) -> float:
    """Coût total entrée+sortie pour 1 contrat : slippage 2 côtés + commission RT."""
    slip = SLIPPAGE_TICKS_PER_TICKER.get(ticker, 1)
    tick = INSTRUMENTS[ticker]["tick_size"]
    dpp = INSTRUMENTS[ticker]["dollar_per_point"]
    slip_cost = 2.0 * slip * tick * dpp  # 2 × car entrée + sortie
    return slip_cost + COMMISSION_RT_PER_CONTRACT


def apply_frictions(trades: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """Soustrait slippage + commission au PnL brut produit par run_fib_backtest."""
    if len(trades) == 0:
        return trades
    rt_cost = cost_per_round_trip(ticker)
    trades = trades.copy()
    trades["pnl_gross"] = trades["pnl"]
    trades["pnl"] = trades["pnl_gross"] - rt_cost * trades["n_ct"]
    return trades


def compute_metrics(trades: pd.DataFrame) -> dict:
    """Métriques baseline : n, WR, PF, P&L, DD, Sharpe (par trade), avg_bars_held."""
    if len(trades) == 0:
        return dict(
            n_trades=0,
            n_filled=0,
            wr_pct=np.nan,
            pf=np.nan,
            pnl_total=0.0,
            dd_max=0.0,
            sharpe_trade=np.nan,
            avg_bars_held=np.nan,
        )

    filled = trades[trades["result"].isin(["TP", "SL", "TE"])].copy()
    n_filled = len(filled)
    if n_filled == 0:
        return dict(
            n_trades=len(trades),
            n_filled=0,
            wr_pct=np.nan,
            pf=np.nan,
            pnl_total=0.0,
            dd_max=0.0,
            sharpe_trade=np.nan,
            avg_bars_held=np.nan,
        )

    wins = filled[filled["pnl"] > 0]
    losses = filled[filled["pnl"] < 0]
    gross_profit = wins["pnl"].sum()
    gross_loss = -losses["pnl"].sum()
    pf = (
        (gross_profit / gross_loss)
        if gross_loss > 0
        else (float("inf") if gross_profit > 0 else float("nan"))
    )

    eq = filled["pnl"].cumsum()
    dd = eq - eq.cummax()
    dd_max = float(dd.min()) if len(dd) else 0.0

    pnl_std = filled["pnl"].std(ddof=1) if n_filled > 1 else float("nan")
    sharpe_trade = (filled["pnl"].mean() / pnl_std) if pnl_std and pnl_std > 0 else float("nan")

    return dict(
        n_trades=int(len(trades)),
        n_filled=int(n_filled),
        wr_pct=float(100.0 * len(wins) / n_filled),
        pf=float(pf),
        pnl_total=float(filled["pnl"].sum()),
        dd_max=dd_max,
        sharpe_trade=float(sharpe_trade) if not np.isnan(sharpe_trade) else float("nan"),
        avg_bars_held=float(filled["bars_held"].mean()),
    )


def run_cell(ticker: str, tf: str, fib_level: float) -> dict:
    """Exécute un backtest baseline pour une cellule (ticker, tf, fib_level)."""
    csv_path = DATA_DIR / f"{ticker}_data_{tf}.csv"
    if not csv_path.exists():
        return dict(ticker=ticker, tf=tf, fib_level=fib_level, error=f"missing csv {csv_path.name}")

    df = load_csv(str(csv_path))

    if tf == "m15":
        patch_fib_constants(FIB_CONSTANTS_M15)
    elif tf == "m5":
        patch_fib_constants(FIB_CONSTANTS_M5)
    else:
        raise ValueError(f"timeframe non géré : {tf}")

    trades = sf.run_fib_backtest(
        df,
        ticker,
        fib_level=fib_level,
        sl_mult=FIB_SL_ATR_MULT_PER_TICKER[ticker],
        tp_mult=FIB_TP_ATR_MULT_PER_TICKER[ticker],
        min_imp=FIB_MIN_IMPULSE_ATR_PER_TICKER[ticker],
        apply_filter=False,
    )
    trades = apply_frictions(trades, ticker)

    metrics = compute_metrics(trades)
    return dict(
        ticker=ticker,
        tf=tf,
        fib_level=fib_level,
        period_start=str(df.index[0]),
        period_end=str(df.index[-1]),
        **metrics,
    )


def classify(row: pd.Series) -> str:
    """Classification rapide pour filtrage phase suivante."""
    if row.get("error"):
        return "MISSING"
    if row["n_filled"] < 50 or pd.isna(row["pf"]) or row["pf"] < 1.0:
        return "REJECT"
    if row["pf"] >= 1.5:
        return "STRONG"
    return "VIABLE"


def main():
    rows = []
    total = len(TICKERS) * len(TIMEFRAMES) * len(FIB_LEVELS)
    i = 0
    for ticker in TICKERS:
        for tf in TIMEFRAMES:
            for fib_level in FIB_LEVELS:
                i += 1
                print(f"  [{i:>2}/{total}] {ticker} {tf} fib={fib_level} ... ", end="", flush=True)
                row = run_cell(ticker, tf, fib_level)
                rows.append(row)
                n = row.get("n_filled", 0)
                pf = row.get("pf", float("nan"))
                pnl = row.get("pnl_total", 0.0)
                print(f"n={n:>4}  PF={pf:>5.2f}  P&L=${pnl:>+8.0f}")

    df = pd.DataFrame(rows)
    df["verdict"] = df.apply(classify, axis=1)

    csv_path = OUTPUT_DIR / "fib_v4_baseline.csv"
    df.to_csv(csv_path, index=False)
    print(f"\n  ✅ CSV : {csv_path}")

    md_path = OUTPUT_DIR / "fib_v4_baseline.md"
    write_markdown_report(df, md_path)
    print(f"  ✅ MD  : {md_path}")

    # Récap console
    print(f"\n{'─'*70}")
    print("RÉCAPITULATIF VERDICT BASELINE")
    print("─" * 70)
    for verdict in ["STRONG", "VIABLE", "REJECT", "MISSING"]:
        sub = df[df["verdict"] == verdict]
        print(f"  {verdict:>8}  {len(sub):>3} cellules")
    print("\nCellules retenues pour data science (STRONG + VIABLE) :")
    keep = df[df["verdict"].isin(["STRONG", "VIABLE"])].copy()
    if len(keep):
        print(
            keep[
                ["ticker", "tf", "fib_level", "n_filled", "pf", "pnl_total", "dd_max", "verdict"]
            ].to_string(index=False)
        )


def write_markdown_report(df: pd.DataFrame, path: Path) -> None:
    lines = []
    lines.append("# Baseline fib-v4 — Phase 1\n")
    lines.append(
        "Backtest **sans filtre trigger** sur la grille croisée "
        "5 tickers × 2 TF × 3 niveaux Fibonacci.\n"
    )
    lines.append(
        f"**Frictions** : slippage (config.py) + commission "
        f"${COMMISSION_RT_PER_CONTRACT:.2f} RT par contrat appliqués au PnL.\n"
    )
    lines.append("**Constantes M5** : valeurs M15 ×3 (équivalence temporelle).\n")
    lines.append("\n## Résultats par cellule\n")
    lines.append(
        "| Ticker | TF | Fib | n trades | n fill | WR% | PF | P&L | DD max | Sharpe/trade | Bars | Verdict |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for _, r in df.iterrows():
        if r.get("error"):
            lines.append(
                f"| {r['ticker']} | {r['tf']} | {r['fib_level']} "
                f"| — | — | — | — | — | — | — | — | MISSING ({r['error']}) |"
            )
            continue
        lines.append(
            f"| {r['ticker']} | {r['tf']} | {r['fib_level']:.3f} "
            f"| {int(r['n_trades'])} | {int(r['n_filled'])} "
            f"| {r['wr_pct']:.1f} | {r['pf']:.2f} "
            f"| ${r['pnl_total']:+,.0f} | ${r['dd_max']:+,.0f} "
            f"| {r['sharpe_trade']:.3f} | {r['avg_bars_held']:.1f} "
            f"| {r['verdict']} |"
        )

    # Synthèse par ticker
    lines.append("\n## Synthèse par ticker (meilleure cellule)\n")
    lines.append("| Ticker | Meilleur (TF, fib) | n fill | PF | P&L | Verdict |")
    lines.append("|---|---|---|---|---|---|")
    for tk in TICKERS:
        sub = df[(df["ticker"] == tk) & (df["pf"].notna())].copy()
        if len(sub) == 0:
            lines.append(f"| {tk} | aucune cellule valable | — | — | — | REJECT |")
            continue
        best = sub.sort_values("pf", ascending=False).iloc[0]
        lines.append(
            f"| {tk} | ({best['tf']}, {best['fib_level']:.3f}) "
            f"| {int(best['n_filled'])} | {best['pf']:.2f} "
            f"| ${best['pnl_total']:+,.0f} | {best['verdict']} |"
        )

    # Synthèse par niveau Fib
    lines.append("\n## Synthèse par niveau Fibonacci\n")
    lines.append("| Niveau | n cellules viables | PF moyen | P&L total |")
    lines.append("|---|---|---|---|")
    for lvl in FIB_LEVELS:
        sub = df[(df["fib_level"] == lvl) & (df["pf"].notna()) & (df["n_filled"] >= 50)]
        if len(sub) == 0:
            lines.append(f"| {lvl:.3f} | 0 | — | — |")
            continue
        lines.append(
            f"| {lvl:.3f} | {len(sub)} | {sub['pf'].mean():.2f} "
            f"| ${sub['pnl_total'].sum():+,.0f} |"
        )

    # Synthèse par timeframe
    lines.append("\n## Synthèse par timeframe\n")
    lines.append("| TF | n cellules viables | PF moyen | P&L total |")
    lines.append("|---|---|---|---|")
    for tf in TIMEFRAMES:
        sub = df[(df["tf"] == tf) & (df["pf"].notna()) & (df["n_filled"] >= 50)]
        if len(sub) == 0:
            lines.append(f"| {tf} | 0 | — | — |")
            continue
        lines.append(
            f"| {tf} | {len(sub)} | {sub['pf'].mean():.2f} " f"| ${sub['pnl_total'].sum():+,.0f} |"
        )

    path.write_text("\n".join(lines))


if __name__ == "__main__":
    main()
