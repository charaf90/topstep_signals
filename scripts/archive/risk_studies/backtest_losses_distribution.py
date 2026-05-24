"""
Distribution des pertes journalières (per stratégie + portfolio).

Sortie : output/losses_distribution/
  - report.md            : tables percentiles + top worst days avec décomposition
  - losses_histogram.png : histogramme des pertes journalières (portfolio + per strat)
  - losses_cdf.png       : CDF des pertes (lecture rapide : P50/P75/P95/P99)
  - daily_pnl_full.png   : histogramme COMPLET (gains + pertes) pour mise en perspective
"""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
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
OUTDIR = ROOT / "output/losses_distribution"
OUTDIR.mkdir(parents=True, exist_ok=True)


def load_trades(risk: int) -> pd.DataFrame:
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
                }
            )
        )
    return pd.concat(parts, ignore_index=True).sort_values("date").reset_index(drop=True)


def daily_pnl_per_strategy(trades: pd.DataFrame) -> pd.DataFrame:
    """Retourne DataFrame indexé par (date, strategy) avec PnL journalier."""
    return trades.groupby([trades["date"].dt.date, "strategy"])["pnl"].sum().reset_index()


def daily_pnl_portfolio(trades: pd.DataFrame) -> pd.Series:
    return trades.groupby(trades["date"].dt.date)["pnl"].sum().sort_index()


def percentile_table(losses: pd.Series, label: str) -> str:
    lines = [f"\n**{label}** :"]
    if len(losses) == 0:
        return "\n_Aucune perte._"
    for pct in [50, 75, 90, 95, 99]:
        v = np.percentile(losses, pct)
        lines.append(f"- P{pct} : ${v:+,.0f}")
    lines.append(f"- Pire perte : ${losses.min():+,.0f}")
    lines.append(f"- Perte moyenne : ${losses.mean():+,.0f}")
    return "\n".join(lines)


def plot_histogram(trades: pd.DataFrame) -> Path:
    """Histogramme des pertes par stratégie + portfolio."""
    fig, axes = plt.subplots(2, 3, figsize=(18, 10))
    daily_strat = daily_pnl_per_strategy(trades)

    # Per strategy
    for ax, strat in zip(axes.flat[:5], FILES.keys()):
        pnl = daily_strat[daily_strat["strategy"] == strat]["pnl"]
        losses = pnl[pnl < 0]
        if len(losses) > 0:
            ax.hist(losses, bins=20, color="#d62728", alpha=0.75, edgecolor="black")
            ax.axvline(
                losses.median(),
                color="black",
                linestyle="--",
                label=f"Médiane ${losses.median():.0f}",
            )
            ax.axvline(
                losses.min(), color="darkred", linestyle="-", label=f"Pire ${losses.min():.0f}"
            )
            ax.legend(fontsize=8)
        ax.set_title(
            f"{strat} — {len(losses)} jours perdants / "
            f"{len(pnl)} jours actifs ({100*len(losses)/max(1,len(pnl)):.0f}%)",
            fontsize=10,
            fontweight="bold",
        )
        ax.set_xlabel("PnL journalier ($)")
        ax.set_ylabel("Fréquence (n jours)")
        ax.grid(alpha=0.3)

    # Portfolio
    ax = axes.flat[5]
    daily_port = daily_pnl_portfolio(trades)
    losses_port = daily_port[daily_port < 0]
    ax.hist(losses_port, bins=20, color="#8b0000", alpha=0.75, edgecolor="black")
    ax.axvline(
        losses_port.median(),
        color="black",
        linestyle="--",
        label=f"Médiane ${losses_port.median():.0f}",
    )
    ax.axvline(
        losses_port.min(), color="darkred", linestyle="-", label=f"Pire ${losses_port.min():.0f}"
    )
    ax.axvline(-950, color="orange", linestyle=":", linewidth=2, label="Topstep DLL -$950")
    ax.legend(fontsize=8)
    ax.set_title(
        f"PORTFOLIO — {len(losses_port)} jours perdants / "
        f"{len(daily_port)} jours ({100*len(losses_port)/max(1,len(daily_port)):.0f}%)",
        fontsize=10,
        fontweight="bold",
    )
    ax.set_xlabel("PnL journalier ($)")
    ax.set_ylabel("Fréquence (n jours)")
    ax.grid(alpha=0.3)

    plt.suptitle(
        f"Distribution des PERTES journalières (risk = ${REFERENCE_RISK}/trade)",
        fontsize=13,
        fontweight="bold",
    )
    plt.tight_layout()
    path = OUTDIR / "losses_histogram.png"
    plt.savefig(path, dpi=110, bbox_inches="tight")
    plt.close()
    return path


def plot_cdf(trades: pd.DataFrame) -> Path:
    """CDF des pertes : pour chaque perte donnée, % des jours qui sont >= ce niveau."""
    fig, ax = plt.subplots(figsize=(12, 7))
    daily_strat = daily_pnl_per_strategy(trades)
    colors = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd"]

    for color, strat in zip(colors, FILES.keys()):
        pnl = daily_strat[daily_strat["strategy"] == strat]["pnl"]
        losses = sorted(pnl[pnl < 0].values)
        if len(losses) == 0:
            continue
        cdf_x = losses
        cdf_y = 100 * np.arange(1, len(losses) + 1) / len(losses)
        ax.plot(cdf_x, cdf_y, label=strat, linewidth=1.8, color=color)

    daily_port = daily_pnl_portfolio(trades)
    losses_port = sorted(daily_port[daily_port < 0].values)
    if losses_port:
        ax.plot(
            losses_port,
            100 * np.arange(1, len(losses_port) + 1) / len(losses_port),
            label="PORTFOLIO",
            linewidth=3,
            color="black",
        )

    ax.axvline(-950, color="orange", linestyle=":", linewidth=2, label="Topstep DLL -$950")
    ax.axvline(-2000, color="red", linestyle=":", linewidth=2, label="Topstep Trail DD -$2000")
    ax.set_xlabel("Perte journalière ($)")
    ax.set_ylabel("% des jours perdants ≤ ce niveau")
    ax.set_title(
        f"CDF des pertes journalières (risk = ${REFERENCE_RISK}/trade)",
        fontsize=13,
        fontweight="bold",
    )
    ax.legend(loc="lower right", fontsize=9)
    ax.grid(alpha=0.3)
    plt.tight_layout()
    path = OUTDIR / "losses_cdf.png"
    plt.savefig(path, dpi=110, bbox_inches="tight")
    plt.close()
    return path


def plot_full_daily(trades: pd.DataFrame) -> Path:
    """Histogramme COMPLET portfolio pour mettre les pertes en perspective."""
    daily_port = daily_pnl_portfolio(trades)
    fig, ax = plt.subplots(figsize=(12, 6))
    ax.hist(daily_port, bins=40, color="#4c72b0", alpha=0.7, edgecolor="black")
    ax.axvline(0, color="black", linewidth=1)
    ax.axvline(
        daily_port.mean(), color="green", linestyle="--", label=f"Moyenne ${daily_port.mean():+.0f}"
    )
    ax.axvline(
        daily_port.median(),
        color="orange",
        linestyle="--",
        label=f"Médiane ${daily_port.median():+.0f}",
    )
    ax.axvline(-950, color="red", linestyle=":", linewidth=2, label="Topstep DLL -$950")
    ax.axvline(1450, color="darkgreen", linestyle=":", linewidth=2, label="Profit Target +$1450")
    ax.set_xlabel("PnL journalier ($)")
    ax.set_ylabel("Fréquence (n jours)")
    ax.set_title(
        f"PORTFOLIO — distribution COMPLÈTE PnL journalier (risk = ${REFERENCE_RISK}/trade)",
        fontsize=12,
        fontweight="bold",
    )
    ax.legend(loc="upper right", fontsize=9)
    ax.grid(alpha=0.3)
    plt.tight_layout()
    path = OUTDIR / "daily_pnl_full.png"
    plt.savefig(path, dpi=110, bbox_inches="tight")
    plt.close()
    return path


def main():
    trades = load_trades(REFERENCE_RISK)
    print(
        f"→ {len(trades)} trades, {trades['strategy'].nunique()} stratégies, "
        f"risk = ${REFERENCE_RISK}/trade"
    )

    daily_strat = daily_pnl_per_strategy(trades)
    daily_port = daily_pnl_portfolio(trades)

    print("→ Génération charts…")
    p1 = plot_histogram(trades)
    print(f"  {p1}")
    p2 = plot_cdf(trades)
    print(f"  {p2}")
    p3 = plot_full_daily(trades)
    print(f"  {p3}")

    print("→ Génération report.md…")
    lines = []
    lines.append(f"# Distribution des pertes journalières (risk = ${REFERENCE_RISK}/trade)\n")
    lines.append(f"**Période** : {trades['date'].min().date()} → {trades['date'].max().date()}  ")
    lines.append("**Stratégies** : 5 (OPR/MES1 v4 exclu)  ")
    lines.append(f"**Trades** : {len(trades)}\n")
    lines.append(
        "Pour autre risk : multiplier toutes les valeurs $ par 0.5 (→$100), "
        "0.75 (→$150), 1.5 (→$300).\n"
    )

    # Per-strategy summary
    lines.append("## Synthèse jours perdants par stratégie\n")
    lines.append(
        "| Stratégie | Jours actifs | Jours - | % jours - | Pire jour | Médian | Avg loss |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    for strat in FILES:
        pnl = daily_strat[daily_strat["strategy"] == strat]["pnl"]
        losses = pnl[pnl < 0]
        if len(pnl) == 0:
            continue
        med = losses.median() if len(losses) > 0 else 0
        avg = losses.mean() if len(losses) > 0 else 0
        pire = losses.min() if len(losses) > 0 else 0
        lines.append(
            f"| {strat} | {len(pnl)} | {len(losses)} | "
            f"{100*len(losses)/len(pnl):.1f}% | ${pire:+,.0f} | "
            f"${med:+,.0f} | ${avg:+,.0f} |"
        )
    # Portfolio
    losses_port = daily_port[daily_port < 0]
    lines.append(
        f"| **PORTFOLIO** | **{len(daily_port)}** | **{len(losses_port)}** | "
        f"**{100*len(losses_port)/len(daily_port):.1f}%** | "
        f"**${losses_port.min():+,.0f}** | **${losses_port.median():+,.0f}** | "
        f"**${losses_port.mean():+,.0f}** |"
    )

    # Percentiles
    lines.append("\n## Percentiles des pertes (uniquement jours négatifs)\n")
    for strat in FILES:
        pnl = daily_strat[daily_strat["strategy"] == strat]["pnl"]
        losses = pnl[pnl < 0]
        lines.append(percentile_table(losses, strat))
    lines.append(percentile_table(losses_port, "**PORTFOLIO**"))

    # Buckets
    lines.append("\n## Buckets de pertes — portfolio\n")
    buckets = [
        ("0 → -$100", lambda x: (x < 0) & (x >= -100)),
        ("-$100 → -$250", lambda x: (x < -100) & (x >= -250)),
        ("-$250 → -$500", lambda x: (x < -250) & (x >= -500)),
        ("-$500 → -$750", lambda x: (x < -500) & (x >= -750)),
        ("-$750 → -$950", lambda x: (x < -750) & (x >= -950)),
        ("-$950 → -$1500", lambda x: (x < -950) & (x >= -1500)),
        ("< -$1500", lambda x: x < -1500),
    ]
    lines.append("| Range | n jours | % du total | Comment |")
    lines.append("|---|---|---|---|")
    for label, func in buckets:
        mask = func(daily_port)
        n = int(mask.sum())
        pct = 100 * n / len(daily_port)
        comment = ""
        if "-$950" in label or "< -" in label:
            comment = "⚠️ déclenche Topstep DLL"
        lines.append(f"| {label} | {n} | {pct:.1f}% | {comment} |")

    # Top 15 worst days with breakdown
    lines.append("\n## Top 15 pires jours portfolio (avec décomposition par stratégie)\n")
    worst_dates = daily_port.nsmallest(15).index
    lines.append("| Date | Total | OPR/NQ1 | OPR/YM1 | FIB/MES1 | FIB/NQ1 | FIB/MGC1 |")
    lines.append("|---|---|---|---|---|---|---|")
    for d in worst_dates:
        total = daily_port[d]
        row = daily_strat[daily_strat["date"] == d]
        cells = []
        for strat in [
            "OPR/NQ1 (v5.1)",
            "OPR/YM1 (v5.1)",
            "FIB/MES1 (v4)",
            "FIB/NQ1 (v4)",
            "FIB/MGC1 (v4)",
        ]:
            val = row[row["strategy"] == strat]["pnl"].sum()
            cells.append(f"${val:+,.0f}" if val != 0 else "—")
        lines.append(f"| {d} | **${total:+,.0f}** | " + " | ".join(cells) + " |")

    # Comparaison aux limites Topstep
    lines.append("\n## Risque sur les limites Topstep\n")
    n_dll = (daily_port <= -950).sum()
    n_trail = (daily_port <= -2000).sum()
    lines.append(
        f"- Jours qui auraient touché ta limite **DLL -$950** : "
        f"**{n_dll}** / {len(daily_port)} ({100*n_dll/len(daily_port):.1f}%)"
    )
    lines.append(
        f"- Jours qui auraient touché **Trailing DD -$2000** : "
        f"**{n_trail}** / {len(daily_port)} ({100*n_trail/len(daily_port):.1f}%)"
    )
    if n_dll > 0:
        lines.append("\n**Jours concernés par DLL -$950** :")
        for d in daily_port[daily_port <= -950].index:
            v = daily_port[d]
            lines.append(f"- {d} : ${v:+,.0f}")

    # À risk $150 (sweet spot envisagé)
    lines.append("\n## Vue alternative à risk $150/trade (multiplicateur 0.75)\n")
    daily_150 = daily_port * 0.75
    losses_150 = daily_150[daily_150 < 0]
    n_dll_150 = (daily_150 <= -950).sum()
    lines.append(f"- Pire jour : ${daily_150.min():+,.0f}")
    lines.append(
        f"- Jours touchant DLL -$950 : **{n_dll_150}** ({100*n_dll_150/len(daily_150):.1f}%)"
    )
    lines.append(f"- P95 des pertes : ${np.percentile(losses_150, 5):+,.0f}")
    lines.append(f"- P99 des pertes : ${np.percentile(losses_150, 1):+,.0f}")

    (OUTDIR / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n✓ Tout dans {OUTDIR}/")


if __name__ == "__main__":
    main()
