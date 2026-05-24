"""
Statistiques temporelles (journalières, hebdomadaires, mensuelles) par stratégie.

Sortie : output/period_stats/
  - report.md          : tables markdown (jour/semaine/mois × stratégie + portfolio)
  - monthly_heatmap.png: heatmap PnL mensuel par stratégie + portfolio
  - monthly_pnl.png    : courbes PnL cumulé mensuel par stratégie
  - weekly_pnl_box.png : distribution PnL hebdo (boxplot) par stratégie

Stratégies incluses : OPR routage v5.1 (NQ1, YM1) + FIB v4 (MES1, NQ1, MGC1).
OPR/MES1 (v4) volontairement exclu (drag identifié).
"""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]

FILES = {
    "OPR/NQ1 (v5.1)": ROOT / "output/backtest_NQ1_opr_v5_1.csv",
    "OPR/YM1 (v5.1)": ROOT / "output/backtest_YM1_opr_v5_1.csv",
    "FIB/MES1 (v4)": ROOT / "output/backtest_MES1_fib_v4.csv",
    "FIB/NQ1 (v4)": ROOT / "output/backtest_NQ1_fib_v4.csv",
    "FIB/MGC1 (v4)": ROOT / "output/backtest_MGC1_fib_v4.csv",
}

REFERENCE_RISK = 200
OUTDIR = ROOT / "output/period_stats"
OUTDIR.mkdir(parents=True, exist_ok=True)


def load_all_trades(risk: int = REFERENCE_RISK) -> pd.DataFrame:
    """Charge tous les trades, rescale au risk de référence, retourne un DataFrame unifié."""
    parts = []
    for name, path in FILES.items():
        df = pd.read_csv(path)
        df["date"] = pd.to_datetime(df["date"])
        if "result" in df.columns:
            df = df[df["result"].isin(["TP", "SL", "TE"])].copy()
        risk_col = "risk_$" if "risk_$" in df.columns else "risk"
        df = df[df[risk_col] > 0].copy()
        df["pnl_rescaled"] = (df["pnl"].astype(float) / df[risk_col].astype(float)) * risk
        parts.append(
            pd.DataFrame(
                {
                    "date": df["date"],
                    "strategy": name,
                    "pnl": df["pnl_rescaled"],
                    "result": df["result"] if "result" in df.columns else None,
                }
            )
        )
    return pd.concat(parts, ignore_index=True).sort_values("date").reset_index(drop=True)


def aggregate_period(trades: pd.DataFrame, period: str) -> pd.DataFrame:
    """
    period ∈ {"D", "W", "M"}.
    Retourne un DataFrame indexé (period_start, strategy) avec colonnes n, pnl, wins, losses.
    """
    t = trades.copy()
    t["period"] = t["date"].dt.to_period(period).dt.start_time
    by = t.groupby(["period", "strategy"]).agg(
        n=("pnl", "size"),
        pnl=("pnl", "sum"),
        wins=("pnl", lambda x: (x > 0).sum()),
        losses=("pnl", lambda x: (x < 0).sum()),
    )
    return by


def period_stats_table(by_period: pd.DataFrame, label: str) -> pd.DataFrame:
    """Calcule les stats agrégées par stratégie pour la période donnée."""
    rows = []
    for strat, grp in by_period.groupby(level="strategy"):
        pnl = grp["pnl"]
        n_periods = len(pnl)
        n_pos = (pnl > 0).sum()
        n_neg = (pnl < 0).sum()
        rows.append(
            {
                "strategy": strat,
                "n_period": n_periods,
                "n_trades": int(grp["n"].sum()),
                "avg_n_trades": grp["n"].mean(),
                "pnl_total": pnl.sum(),
                "pnl_mean": pnl.mean(),
                "pnl_median": pnl.median(),
                "pnl_std": pnl.std(ddof=0),
                "pct_positive": 100 * n_pos / n_periods if n_periods > 0 else 0,
                "best": pnl.max(),
                "worst": pnl.min(),
            }
        )
    return pd.DataFrame(rows).set_index("strategy")


def portfolio_period(trades: pd.DataFrame, period: str) -> pd.Series:
    """PnL portfolio agrégé par période (toutes stratégies confondues)."""
    t = trades.copy()
    t["period"] = t["date"].dt.to_period(period).dt.start_time
    return t.groupby("period")["pnl"].sum().sort_index()


def add_portfolio_row(stats: pd.DataFrame, trades: pd.DataFrame, period: str) -> pd.DataFrame:
    pnl = portfolio_period(trades, period)
    n_periods = len(pnl)
    n_pos = (pnl > 0).sum()
    total_trades = len(trades)
    avg_n = total_trades / n_periods if n_periods > 0 else 0
    row = pd.DataFrame(
        [
            {
                "n_period": n_periods,
                "n_trades": total_trades,
                "avg_n_trades": avg_n,
                "pnl_total": pnl.sum(),
                "pnl_mean": pnl.mean(),
                "pnl_median": pnl.median(),
                "pnl_std": pnl.std(ddof=0),
                "pct_positive": 100 * n_pos / n_periods if n_periods > 0 else 0,
                "best": pnl.max(),
                "worst": pnl.min(),
            }
        ],
        index=["**PORTFOLIO**"],
    )
    return pd.concat([stats, row])


def df_to_md(df: pd.DataFrame, period_label: str) -> str:
    lines = [f"\n### Stats {period_label} (risk = ${REFERENCE_RISK})\n"]
    lines.append(
        "| Stratégie | n périodes | n trades | trades/période | PnL total | PnL moy | PnL médian | σ | % périodes + | Best | Worst |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for strat, row in df.iterrows():
        lines.append(
            f"| {strat} | {int(row['n_period'])} | {int(row['n_trades'])} | "
            f"{row['avg_n_trades']:.2f} | ${row['pnl_total']:+,.0f} | "
            f"${row['pnl_mean']:+,.0f} | ${row['pnl_median']:+,.0f} | "
            f"${row['pnl_std']:,.0f} | {row['pct_positive']:.1f}% | "
            f"${row['best']:+,.0f} | ${row['worst']:+,.0f} |"
        )
    return "\n".join(lines)


def plot_monthly_heatmap(trades: pd.DataFrame) -> Path:
    """Heatmap PnL mensuel : lignes = stratégie + portfolio, colonnes = mois."""
    t = trades.copy()
    t["month"] = t["date"].dt.to_period("M").dt.start_time
    by = t.groupby(["month", "strategy"])["pnl"].sum().unstack(fill_value=0.0)
    portfolio = t.groupby("month")["pnl"].sum()
    by["**PORTFOLIO**"] = portfolio
    by = by.T  # stratégies en lignes, mois en colonnes
    by = by.sort_index(axis=1)

    fig, ax = plt.subplots(figsize=(max(12, 0.45 * by.shape[1]), 0.6 * by.shape[0] + 2))
    vmax = max(abs(by.values.min()), abs(by.values.max()))
    im = ax.imshow(by.values, cmap="RdYlGn", aspect="auto", vmin=-vmax, vmax=vmax)
    ax.set_xticks(range(by.shape[1]))
    ax.set_xticklabels([d.strftime("%Y-%m") for d in by.columns], rotation=70, fontsize=8)
    ax.set_yticks(range(by.shape[0]))
    ax.set_yticklabels(by.index, fontsize=9)
    for i in range(by.shape[0]):
        for j in range(by.shape[1]):
            v = by.values[i, j]
            ax.text(
                j,
                i,
                f"{v:+.0f}",
                ha="center",
                va="center",
                fontsize=7,
                color="black" if abs(v) < vmax * 0.6 else "white",
            )
    plt.colorbar(im, ax=ax, label=f"PnL mensuel ($, risk ${REFERENCE_RISK})")
    plt.title(
        f"PnL mensuel par stratégie + portfolio (risk = ${REFERENCE_RISK}/trade)",
        fontsize=12,
        fontweight="bold",
    )
    plt.tight_layout()
    path = OUTDIR / "monthly_heatmap.png"
    plt.savefig(path, dpi=110, bbox_inches="tight")
    plt.close()
    return path


def plot_monthly_pnl(trades: pd.DataFrame) -> Path:
    """Courbes PnL cumulé mensuel par stratégie + portfolio."""
    t = trades.copy()
    t["month"] = t["date"].dt.to_period("M").dt.start_time
    fig, ax = plt.subplots(figsize=(14, 7))
    for strat in trades["strategy"].unique():
        sub = t[t["strategy"] == strat].groupby("month")["pnl"].sum().sort_index().cumsum()
        ax.plot(sub.index, sub.values, label=strat, linewidth=1.5, marker="o", markersize=4)
    portfolio = t.groupby("month")["pnl"].sum().sort_index().cumsum()
    ax.plot(
        portfolio.index,
        portfolio.values,
        label="PORTFOLIO",
        linewidth=2.5,
        color="black",
        marker="s",
        markersize=5,
    )
    ax.set_title(
        f"PnL cumulé mensuel par stratégie (risk = ${REFERENCE_RISK}/trade)",
        fontsize=13,
        fontweight="bold",
    )
    ax.legend(loc="upper left", fontsize=9)
    ax.grid(alpha=0.3)
    ax.set_ylabel("PnL cumulé ($)")
    ax.axhline(0, color="black", linewidth=0.5)
    plt.tight_layout()
    path = OUTDIR / "monthly_pnl.png"
    plt.savefig(path, dpi=110, bbox_inches="tight")
    plt.close()
    return path


def plot_weekly_box(trades: pd.DataFrame) -> Path:
    """Boxplot distribution PnL hebdo par stratégie."""
    t = trades.copy()
    t["week"] = t["date"].dt.to_period("W").dt.start_time
    pivot = t.groupby(["week", "strategy"])["pnl"].sum().unstack(fill_value=0.0)
    fig, ax = plt.subplots(figsize=(12, 6))
    data = [pivot[c].dropna() for c in pivot.columns]
    bp = ax.boxplot(data, labels=pivot.columns, patch_artist=True, showmeans=True)
    for patch in bp["boxes"]:
        patch.set_facecolor("#a8d8ea")
    ax.set_title(
        f"Distribution du PnL hebdomadaire par stratégie (risk = ${REFERENCE_RISK})",
        fontsize=12,
        fontweight="bold",
    )
    ax.set_ylabel("PnL hebdomadaire ($)")
    ax.axhline(0, color="red", linewidth=0.8, linestyle="--", alpha=0.5)
    ax.grid(alpha=0.3, axis="y")
    plt.xticks(rotation=20, fontsize=9)
    plt.tight_layout()
    path = OUTDIR / "weekly_pnl_box.png"
    plt.savefig(path, dpi=110, bbox_inches="tight")
    plt.close()
    return path


def main():
    print(f"→ Chargement (risk de référence = ${REFERENCE_RISK})…")
    trades = load_all_trades(REFERENCE_RISK)
    print(
        f"  {len(trades)} trades sur {trades['strategy'].nunique()} stratégies "
        f"({trades['date'].min().date()} → {trades['date'].max().date()})"
    )

    print("→ Aggrégations…")
    daily_by = aggregate_period(trades, "D")
    weekly_by = aggregate_period(trades, "W")
    monthly_by = aggregate_period(trades, "M")

    daily_stats = period_stats_table(daily_by, "journalier")
    weekly_stats = period_stats_table(weekly_by, "hebdomadaire")
    monthly_stats = period_stats_table(monthly_by, "mensuel")

    daily_stats = add_portfolio_row(daily_stats, trades, "D")
    weekly_stats = add_portfolio_row(weekly_stats, trades, "W")
    monthly_stats = add_portfolio_row(monthly_stats, trades, "M")

    print("→ Génération charts…")
    h = plot_monthly_heatmap(trades)
    print(f"  {h}")
    m = plot_monthly_pnl(trades)
    print(f"  {m}")
    w = plot_weekly_box(trades)
    print(f"  {w}")

    print("→ Génération report.md…")
    lines = []
    lines.append(f"# Stats temporelles par stratégie (risk = ${REFERENCE_RISK}/trade)\n")
    lines.append(f"**Période** : {trades['date'].min().date()} → {trades['date'].max().date()}  ")
    lines.append(f"**Stratégies** : {trades['strategy'].nunique()} (OPR/MES1 v4 exclu)  ")
    lines.append(f"**Trades** : {len(trades)}\n")
    lines.append(
        "Pour passer à un autre niveau de risque : multiplier par 0.5 (→$100), "
        "0.75 (→$150), 1.5 (→$300). PF, win rate et % périodes positives sont invariants.\n"
    )

    lines.append(df_to_md(daily_stats, "JOURNALIER"))
    lines.append("")
    lines.append(df_to_md(weekly_stats, "HEBDOMADAIRE"))
    lines.append("")
    lines.append(df_to_md(monthly_stats, "MENSUEL"))
    lines.append("")

    # Best/worst month détail (intéressant pour Topstep cycle)
    lines.append("\n## Top/Bottom 5 mois — portfolio agrégé\n")
    monthly_port = portfolio_period(trades, "M")
    lines.append("**Top 5 meilleurs mois** :\n")
    for d, v in monthly_port.nlargest(5).items():
        lines.append(f"- {d.strftime('%Y-%m')} : ${v:+,.0f}")
    lines.append("\n**Top 5 pires mois** :\n")
    for d, v in monthly_port.nsmallest(5).items():
        lines.append(f"- {d.strftime('%Y-%m')} : ${v:+,.0f}")

    # Distribution mensuelle
    lines.append("\n## Distribution mensuelle portfolio (résumé)\n")
    lines.append(f"- Mois testés : **{len(monthly_port)}**")
    lines.append(
        f"- Mois positifs : **{(monthly_port > 0).sum()}** ({100*(monthly_port > 0).mean():.1f}%)"
    )
    lines.append(
        f"- Mois ≥ +$3000 (challenge passé) : **{(monthly_port >= 3000).sum()}** "
        f"({100*(monthly_port >= 3000).mean():.1f}%)"
    )
    lines.append(
        f"- Mois ≤ -$2000 (bust trailing DD) : **{(monthly_port <= -2000).sum()}** "
        f"({100*(monthly_port <= -2000).mean():.1f}%)"
    )
    lines.append(
        f"- Mois ≤ -$1000 (risque daily loss) : **{(monthly_port <= -1000).sum()}** "
        f"({100*(monthly_port <= -1000).mean():.1f}%)"
    )

    (OUTDIR / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n✓ Tout dans {OUTDIR}/")


if __name__ == "__main__":
    main()
