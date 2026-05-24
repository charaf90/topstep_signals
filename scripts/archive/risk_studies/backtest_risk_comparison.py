"""
Backtest comparatif — sensibilité au risque par trade.

Pour chaque niveau de risque $100/$200/$300 par trade :
  - Re-scaling linéaire du PnL des CSVs backtest existants (pnl × risk/risk_orig)
  - Métriques agrégées par stratégie et portefeuille
  - Graphiques PnL cumulé (par strat + portfolio)

Sortie : output/risk_comparison/
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Dict, List

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]

ALL_FILES = {
    "OPR/MES1 (v4)":  ROOT / "output/backtest_MES1_opr.csv",
    "OPR/NQ1 (v5.1)": ROOT / "output/backtest_NQ1_opr_v5_1.csv",
    "OPR/YM1 (v5.1)": ROOT / "output/backtest_YM1_opr_v5_1.csv",
    "FIB/MES1 (v4)":  ROOT / "output/backtest_MES1_fib_v4.csv",
    "FIB/NQ1 (v4)":   ROOT / "output/backtest_NQ1_fib_v4.csv",
    "FIB/MGC1 (v4)":  ROOT / "output/backtest_MGC1_fib_v4.csv",
}

RISK_LEVELS = [100, 200, 300]
DEFAULT_OUTDIR = ROOT / "output/risk_comparison"

IS_END = "2025-09-30"
OOS_START = "2025-10-01"

# Globaux mutables — initialisés par main()
FILES: Dict[str, Path] = dict(ALL_FILES)
OUTDIR: Path = DEFAULT_OUTDIR


def load_trades() -> Dict[str, pd.DataFrame]:
    all_trades: Dict[str, pd.DataFrame] = {}
    for name, path in FILES.items():
        df = pd.read_csv(path)
        df["date"] = pd.to_datetime(df["date"]).dt.date
        if "result" in df.columns:
            df = df[df["result"].isin(["TP", "SL", "TE"])].copy()
        risk_col = "risk_$" if "risk_$" in df.columns else "risk"
        df = df[df[risk_col] > 0].copy()
        df["risk_orig"] = df[risk_col].astype(float)
        df["pnl_per_dollar_risk"] = df["pnl"].astype(float) / df["risk_orig"]
        df = df.sort_values("date").reset_index(drop=True)
        all_trades[name] = df
    return all_trades


def metrics_from_cumulative(pnl_per_trade: pd.Series, dates: pd.Series) -> Dict[str, float]:
    """Calcule métriques sur série de PnL par trade ordonnée chronologiquement."""
    if len(pnl_per_trade) == 0:
        return {"n": 0, "total": 0.0, "max_dd": 0.0, "pf": 0.0,
                "wr": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
                "best_day": 0.0, "worst_day": 0.0}

    df = pd.DataFrame({"date": pd.to_datetime(dates), "pnl": pnl_per_trade.values})
    daily = df.groupby("date")["pnl"].sum().sort_index()
    cum = daily.cumsum()
    running_max = cum.cummax()
    dd = cum - running_max

    gains = pnl_per_trade[pnl_per_trade > 0]
    losses = pnl_per_trade[pnl_per_trade < 0]

    return {
        "n": int(len(pnl_per_trade)),
        "total": float(cum.iloc[-1]),
        "max_dd": float(dd.min()),
        "pf": float(gains.sum() / -losses.sum()) if len(losses) > 0 and losses.sum() < 0 else float("inf"),
        "wr": float((pnl_per_trade > 0).mean() * 100),
        "avg_win": float(gains.mean()) if len(gains) > 0 else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) > 0 else 0.0,
        "best_day": float(daily.max()),
        "worst_day": float(daily.min()),
    }


def build_portfolio_series(all_trades: Dict[str, pd.DataFrame], risk: int) -> pd.DataFrame:
    parts: List[pd.DataFrame] = []
    for name, df in all_trades.items():
        parts.append(pd.DataFrame({
            "date": pd.to_datetime(df["date"]),
            "pnl": df["pnl_per_dollar_risk"] * risk,
            "strategy": name,
        }))
    portfolio = pd.concat(parts, ignore_index=True).sort_values("date").reset_index(drop=True)
    return portfolio


def plot_per_strategy(all_trades: Dict[str, pd.DataFrame]) -> Path:
    n_strat = len(all_trades)
    fig, axes = plt.subplots(2, 3, figsize=(18, 10), sharex=False)
    colors = {100: "#1f77b4", 200: "#ff7f0e", 300: "#d62728"}
    for ax, (name, df) in zip(axes.flat, all_trades.items()):
        dates = pd.to_datetime(df["date"])
        for risk in RISK_LEVELS:
            pnl_rescaled = df["pnl_per_dollar_risk"] * risk
            cum = pnl_rescaled.cumsum().values
            ax.plot(dates, cum, label=f"Risk ${risk}",
                    color=colors[risk], linewidth=1.6)
        ax.set_title(name, fontsize=11, fontweight="bold")
        ax.legend(loc="upper left", fontsize=8)
        ax.grid(alpha=0.3)
        ax.set_ylabel("PnL cumulé ($)", fontsize=9)
        ax.axhline(0, color="black", linewidth=0.5)
        ax.axvline(pd.Timestamp(OOS_START), color="grey",
                   linestyle="--", linewidth=0.8, alpha=0.5)
    plt.suptitle("PnL cumulé par stratégie — sensibilité au risque "
                 "(ligne pointillée = début OOS)", fontsize=13)
    plt.tight_layout()
    path = OUTDIR / "pnl_per_strategy.png"
    plt.savefig(path, dpi=110, bbox_inches="tight")
    plt.close()
    return path


def plot_portfolio(all_trades: Dict[str, pd.DataFrame]) -> Path:
    fig, ax = plt.subplots(figsize=(14, 7))
    colors = {100: "#1f77b4", 200: "#ff7f0e", 300: "#d62728"}
    for risk in RISK_LEVELS:
        portfolio = build_portfolio_series(all_trades, risk)
        daily = portfolio.groupby("date")["pnl"].sum().sort_index()
        cum = daily.cumsum()
        ax.plot(cum.index, cum.values, label=f"Risk ${risk}",
                color=colors[risk], linewidth=2)
    ax.set_title("PnL cumulé portefeuille agrégé — sensibilité au risque par trade",
                 fontsize=13, fontweight="bold")
    ax.legend(loc="upper left")
    ax.grid(alpha=0.3)
    ax.set_ylabel("PnL cumulé ($)", fontsize=11)
    ax.axhline(0, color="black", linewidth=0.5)
    ax.axvline(pd.Timestamp(OOS_START), color="grey",
               linestyle="--", linewidth=0.8, alpha=0.5,
               label=f"OOS start ({OOS_START})")
    plt.tight_layout()
    path = OUTDIR / "pnl_portfolio.png"
    plt.savefig(path, dpi=110, bbox_inches="tight")
    plt.close()
    return path


def plot_drawdown(all_trades: Dict[str, pd.DataFrame]) -> Path:
    """Drawdown curves pour le portefeuille agrégé."""
    fig, ax = plt.subplots(figsize=(14, 5))
    colors = {100: "#1f77b4", 200: "#ff7f0e", 300: "#d62728"}
    for risk in RISK_LEVELS:
        portfolio = build_portfolio_series(all_trades, risk)
        daily = portfolio.groupby("date")["pnl"].sum().sort_index()
        cum = daily.cumsum()
        running_max = cum.cummax()
        dd = cum - running_max
        ax.fill_between(dd.index, dd.values, 0, alpha=0.25, color=colors[risk])
        ax.plot(dd.index, dd.values, label=f"Risk ${risk}",
                color=colors[risk], linewidth=1.3)
    ax.set_title("Drawdown portfolio — sensibilité au risque",
                 fontsize=13, fontweight="bold")
    ax.legend(loc="lower left")
    ax.grid(alpha=0.3)
    ax.set_ylabel("Drawdown ($)", fontsize=11)
    ax.axhline(0, color="black", linewidth=0.5)
    plt.tight_layout()
    path = OUTDIR / "drawdown_portfolio.png"
    plt.savefig(path, dpi=110, bbox_inches="tight")
    plt.close()
    return path


def build_report(all_trades: Dict[str, pd.DataFrame]) -> Path:
    lines: List[str] = []
    n_total = sum(len(d) for d in all_trades.values())
    date_min = min(d["date"].min() for d in all_trades.values())
    date_max = max(d["date"].max() for d in all_trades.values())

    lines.append("# Backtest comparatif — sensibilité au risque par trade\n")
    lines.append(f"**Période** : {date_min} → {date_max}  ")
    lines.append(f"**Stratégies** : {len(all_trades)} (OPR routage v4/v5.1 + FIB v4)  ")
    lines.append(f"**Trades fillés** : {n_total}  ")
    lines.append(f"**Risk levels testés** : ${RISK_LEVELS[0]}, ${RISK_LEVELS[1]}, ${RISK_LEVELS[2]} par trade  ")
    lines.append(f"**Méthode** : re-scaling linéaire `pnl_new = pnl_orig × (risk_target / risk_orig)`")
    lines.append(f"  → commissions et slippage sont rescalés au pro-rata (déjà inclus dans pnl_orig)\n")

    # Synthèse portfolio en tête
    lines.append("## Synthèse portfolio (toutes stratégies agrégées)\n")
    lines.append("| Risk/trade | PnL total | Max DD | PF | WinRate | Best day | Worst day |")
    lines.append("|---|---|---|---|---|---|---|")
    for risk in RISK_LEVELS:
        portfolio = build_portfolio_series(all_trades, risk)
        m = metrics_from_cumulative(portfolio["pnl"], portfolio["date"])
        lines.append(
            f"| **${risk}** | ${m['total']:+,.0f} | ${m['max_dd']:+,.0f} | "
            f"{m['pf']:.2f} | {m['wr']:.1f}% | ${m['best_day']:+,.0f} | ${m['worst_day']:+,.0f} |"
        )

    # Split IS / OOS
    lines.append("\n## Split IS / OOS (portfolio)\n")
    lines.append(f"IS = jusqu'au {IS_END} • OOS = à partir du {OOS_START}\n")
    lines.append("| Risk | IS PnL | IS MaxDD | IS PF | OOS PnL | OOS MaxDD | OOS PF |")
    lines.append("|---|---|---|---|---|---|---|")
    for risk in RISK_LEVELS:
        portfolio = build_portfolio_series(all_trades, risk)
        is_mask = portfolio["date"] <= pd.Timestamp(IS_END)
        oos_mask = portfolio["date"] >= pd.Timestamp(OOS_START)
        mi = metrics_from_cumulative(portfolio.loc[is_mask, "pnl"], portfolio.loc[is_mask, "date"])
        mo = metrics_from_cumulative(portfolio.loc[oos_mask, "pnl"], portfolio.loc[oos_mask, "date"])
        lines.append(
            f"| ${risk} | ${mi['total']:+,.0f} | ${mi['max_dd']:+,.0f} | {mi['pf']:.2f} | "
            f"${mo['total']:+,.0f} | ${mo['max_dd']:+,.0f} | {mo['pf']:.2f} |"
        )

    # Détail par stratégie pour chaque risk
    for risk in RISK_LEVELS:
        lines.append(f"\n## Détail par stratégie — risk = ${risk}\n")
        lines.append("| Stratégie | n | PnL total | Max DD | PF | WinRate | Avg win | Avg loss |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for name, df in all_trades.items():
            pnl_rescaled = df["pnl_per_dollar_risk"] * risk
            m = metrics_from_cumulative(pnl_rescaled, df["date"])
            lines.append(
                f"| {name} | {m['n']} | ${m['total']:+,.0f} | ${m['max_dd']:+,.0f} | "
                f"{m['pf']:.2f} | {m['wr']:.1f}% | ${m['avg_win']:+,.0f} | ${m['avg_loss']:+,.0f} |"
            )
        # Portfolio line
        portfolio = build_portfolio_series(all_trades, risk)
        m = metrics_from_cumulative(portfolio["pnl"], portfolio["date"])
        lines.append(
            f"| **PORTFOLIO** | **{m['n']}** | **${m['total']:+,.0f}** | **${m['max_dd']:+,.0f}** | "
            f"**{m['pf']:.2f}** | **{m['wr']:.1f}%** | **${m['avg_win']:+,.0f}** | **${m['avg_loss']:+,.0f}** |"
        )

    # Caveats
    lines.append("\n## Caveats méthodologiques\n")
    lines.append("- Le re-scaling linéaire est exact pour pnl / commissions / slippage tant que `n_ct ≥ 1` est respecté ; quelques trades à `n_ct = 1` dans le CSV original pourraient surestimer légèrement le risque ↑ effectif (effet d'arrondi).")
    lines.append("- Aucun garde-fou portefeuille (`risk_portfolio.py`) appliqué dans cette simulation : le PnL agrégé représente le **plafond théorique** sans blocage de signaux concurrents.")
    lines.append("- Pas d'effet de drawdown trailing Topstep dans la simulation : le PnL cumulé peut traverser des seuils qui seraient bloquants en live.")
    lines.append("- Le calcul `metrics` agrège par jour pour le PF — un jour avec mixte TP/SL est noté en net.")

    path = OUTDIR / "report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main():
    global FILES, OUTDIR
    ap = argparse.ArgumentParser()
    ap.add_argument("--exclude", action="append", default=[],
                    help="Nom exact de stratégie à exclure (répétable). "
                         f"Choix : {list(ALL_FILES.keys())}")
    ap.add_argument("--outdir", default=str(DEFAULT_OUTDIR),
                    help="Sous-dossier de sortie (défaut : output/risk_comparison)")
    args = ap.parse_args()

    FILES = {k: v for k, v in ALL_FILES.items() if k not in set(args.exclude)}
    OUTDIR = Path(args.outdir)
    OUTDIR.mkdir(parents=True, exist_ok=True)

    if args.exclude:
        print(f"→ Stratégies exclues : {args.exclude}")
    print(f"→ Output : {OUTDIR}/")
    print("→ Chargement des CSVs backtest…")
    all_trades = load_trades()
    n_total = sum(len(d) for d in all_trades.values())
    print(f"  {len(all_trades)} stratégies, {n_total} trades fillés")

    print("→ Génération chart per-strategy…")
    p1 = plot_per_strategy(all_trades)
    print(f"  {p1}")

    print("→ Génération chart portfolio agrégé…")
    p2 = plot_portfolio(all_trades)
    print(f"  {p2}")

    print("→ Génération chart drawdown portfolio…")
    p3 = plot_drawdown(all_trades)
    print(f"  {p3}")

    print("→ Génération rapport markdown…")
    p4 = build_report(all_trades)
    print(f"  {p4}")

    print(f"\n✓ Tous les outputs dans {OUTDIR}/")


if __name__ == "__main__":
    main()
