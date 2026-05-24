"""
Analyse des séquences de SL consécutifs (intra-day et cross-day).

Objectif : calibrer un éventuel garde-fou "stop trading sur ce ticker après N SL".

Sortie : output/sl_streaks/
  - report.md             : distributions + analyse coût/bénéfice par seuil N
  - streaks_intraday.png  : histogramme longueur de streaks intra-day par stratégie
  - cutoff_analysis.png   : courbe coût/bénéfice selon N (par strat)
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]

FILES = {
    "OPR/NQ1 (v5.1)": ROOT / "output/backtest_NQ1_opr_v5_1.csv",
    "OPR/YM1 (v5.1)": ROOT / "output/backtest_YM1_opr_v5_1.csv",
    "FIB/MES1 (v4)":  ROOT / "output/backtest_MES1_fib_v4.csv",
    "FIB/NQ1 (v4)":   ROOT / "output/backtest_NQ1_fib_v4.csv",
    "FIB/MGC1 (v4)":  ROOT / "output/backtest_MGC1_fib_v4.csv",
}

REFERENCE_RISK = 200
OUTDIR = ROOT / "output/sl_streaks"
OUTDIR.mkdir(parents=True, exist_ok=True)


def load_trades_chrono(risk: int) -> Dict[str, pd.DataFrame]:
    """Charge chaque stratégie, rescale PnL, trie par fill_time si dispo sinon date."""
    out = {}
    for name, path in FILES.items():
        df = pd.read_csv(path)
        df["date"] = pd.to_datetime(df["date"]).dt.date
        if "result" in df.columns:
            df = df[df["result"].isin(["TP", "SL", "TE"])].copy()
        risk_col = "risk_$" if "risk_$" in df.columns else "risk"
        df = df[df[risk_col] > 0].copy()
        df["pnl_rescaled"] = (df["pnl"].astype(float) / df[risk_col].astype(float)) * risk
        # Tri chronologique le plus précis possible
        if "fill_time" in df.columns:
            df["__sort__"] = pd.to_datetime(df["fill_time"], errors="coerce", utc=True)
        elif "trigger_time" in df.columns:
            df["__sort__"] = pd.to_datetime(df["trigger_time"], errors="coerce", utc=True)
        else:
            df["__sort__"] = pd.to_datetime(df["date"])
        df = df.sort_values("__sort__").reset_index(drop=True)
        out[name] = df
    return out


def compute_intraday_streaks(df: pd.DataFrame) -> List[Dict]:
    """
    Pour chaque jour, identifie les streaks de SL consécutifs (au sein du jour).
    Retourne une liste de dicts avec longueur, date.
    """
    streaks = []
    for d, grp in df.groupby("date"):
        cur = 0
        max_streak = 0
        for r in grp["result"].values:
            if r == "SL":
                cur += 1
                max_streak = max(max_streak, cur)
            else:
                if cur > 0:
                    streaks.append({"date": d, "len": cur, "type": "intermediate"})
                cur = 0
        if cur > 0:
            streaks.append({"date": d, "len": cur, "type": "trailing"})
    return streaks


def streak_position_history(df: pd.DataFrame) -> pd.DataFrame:
    """
    Pour chaque trade, calcule le streak SL cumulé AVANT ce trade (au sein du jour).
    Permet de simuler "si on avait coupé après N SL, ce trade aurait-il été bloqué ?"
    """
    df = df.copy()
    df["sl_streak_before"] = 0
    for d, grp in df.groupby("date"):
        cur = 0
        for idx in grp.index:
            df.at[idx, "sl_streak_before"] = cur
            if df.at[idx, "result"] == "SL":
                cur += 1
            else:
                cur = 0
    return df


def cutoff_cost_benefit(df_with_streak: pd.DataFrame, max_n: int = 5) -> pd.DataFrame:
    """
    Pour chaque seuil N (1..max_n), simule "ne plus trader sur ce ticker après N SL le même jour".
    Retourne un tableau : pour chaque N, le PnL des trades qui auraient été bloqués
    (négatif = pertes évitées, positif = gains ratés).
    """
    rows = []
    total_pnl = df_with_streak["pnl_rescaled"].sum()
    for n in range(1, max_n + 1):
        # Trades qui auraient été BLOQUÉS = ceux où sl_streak_before >= N
        blocked = df_with_streak[df_with_streak["sl_streak_before"] >= n]
        kept = df_with_streak[df_with_streak["sl_streak_before"] < n]
        blocked_pnl = blocked["pnl_rescaled"].sum()
        kept_pnl = kept["pnl_rescaled"].sum()
        blocked_wins = blocked[blocked["pnl_rescaled"] > 0]
        blocked_losses = blocked[blocked["pnl_rescaled"] < 0]
        rows.append({
            "N": n,
            "n_blocked": int(len(blocked)),
            "n_kept": int(len(kept)),
            "blocked_pnl": float(blocked_pnl),
            "blocked_wins_$": float(blocked_wins["pnl_rescaled"].sum()),
            "blocked_losses_$": float(blocked_losses["pnl_rescaled"].sum()),
            "n_blocked_wins": int(len(blocked_wins)),
            "n_blocked_losses": int(len(blocked_losses)),
            "kept_pnl": float(kept_pnl),
            "delta_vs_baseline": float(kept_pnl - total_pnl),
        })
    return pd.DataFrame(rows)


def plot_streaks_intraday(trades_by_strat: Dict[str, pd.DataFrame]) -> Path:
    fig, axes = plt.subplots(2, 3, figsize=(15, 8))
    for ax, (name, df) in zip(axes.flat[:5], trades_by_strat.items()):
        streaks = compute_intraday_streaks(df)
        lens = [s["len"] for s in streaks]
        if not lens:
            ax.text(0.5, 0.5, "Aucun streak", ha="center", va="center")
        else:
            ax.hist(lens, bins=range(1, max(lens) + 2),
                    color="#d62728", alpha=0.75, edgecolor="black", align="left")
            from collections import Counter
            cnt = Counter(lens)
            for k, v in sorted(cnt.items()):
                ax.text(k, v + 0.5, str(v), ha="center", fontsize=9)
        ax.set_title(f"{name}", fontsize=11, fontweight="bold")
        ax.set_xlabel("Longueur du streak SL intra-day")
        ax.set_ylabel("Nb d'occurrences")
        ax.grid(alpha=0.3, axis="y")
    axes.flat[5].axis("off")
    plt.suptitle("Distribution des streaks de SL consécutifs intra-day (toutes périodes)",
                 fontsize=12, fontweight="bold")
    plt.tight_layout()
    path = OUTDIR / "streaks_intraday.png"
    plt.savefig(path, dpi=110, bbox_inches="tight")
    plt.close()
    return path


def plot_cutoff_analysis(trades_by_strat: Dict[str, pd.DataFrame]) -> Path:
    fig, axes = plt.subplots(1, 2, figsize=(15, 6))
    colors = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd"]
    ax1, ax2 = axes
    for color, (name, df) in zip(colors, trades_by_strat.items()):
        df_s = streak_position_history(df)
        cb = cutoff_cost_benefit(df_s, max_n=5)
        ax1.plot(cb["N"], cb["delta_vs_baseline"], marker="o",
                 label=name, linewidth=1.8, color=color)
        ax2.plot(cb["N"], cb["n_blocked"], marker="s",
                 label=name, linewidth=1.8, color=color)
    ax1.axhline(0, color="black", linewidth=0.5)
    ax1.set_title("Impact PnL d'un cutoff après N SL/jour (par stratégie)",
                  fontsize=11, fontweight="bold")
    ax1.set_xlabel("N (cut après N SL consécutifs intra-day)")
    ax1.set_ylabel("Δ PnL vs baseline ($)")
    ax1.legend(fontsize=8)
    ax1.grid(alpha=0.3)

    ax2.set_title("Nb de trades qui auraient été bloqués (par stratégie)",
                  fontsize=11, fontweight="bold")
    ax2.set_xlabel("N (cut après N SL consécutifs intra-day)")
    ax2.set_ylabel("Nb trades bloqués")
    ax2.legend(fontsize=8)
    ax2.grid(alpha=0.3)
    plt.tight_layout()
    path = OUTDIR / "cutoff_analysis.png"
    plt.savefig(path, dpi=110, bbox_inches="tight")
    plt.close()
    return path


def main():
    print(f"→ Chargement (risk = ${REFERENCE_RISK})…")
    trades_by_strat = load_trades_chrono(REFERENCE_RISK)
    n_total = sum(len(d) for d in trades_by_strat.values())
    print(f"  {n_total} trades sur {len(trades_by_strat)} stratégies")

    print("→ Charts streaks intra-day + cutoff…")
    p1 = plot_streaks_intraday(trades_by_strat); print(f"  {p1}")
    p2 = plot_cutoff_analysis(trades_by_strat); print(f"  {p2}")

    print("→ Report.md…")
    lines = []
    lines.append(f"# Analyse séquences de SL consécutifs (risk = ${REFERENCE_RISK}/trade)\n")
    lines.append("**Objectif** : calibrer un éventuel garde-fou "
                 "« stop trading sur ce ticker après N SL le même jour ».\n")
    lines.append(f"Stratégies : {list(trades_by_strat.keys())}\n")

    # 1. Distribution streaks intra-day
    lines.append("## 1. Distribution des streaks SL intra-day par stratégie\n")
    lines.append("Lecture : longueur des séries de SL consécutifs au sein d'un même jour.\n")
    lines.append("| Stratégie | Longueur max | n streaks=1 | =2 | =3 | =4 | =5+ |")
    lines.append("|---|---|---|---|---|---|---|")
    for name, df in trades_by_strat.items():
        streaks = compute_intraday_streaks(df)
        lens = [s["len"] for s in streaks]
        if not lens:
            lines.append(f"| {name} | — | — | — | — | — | — |")
            continue
        from collections import Counter
        c = Counter(lens)
        ge5 = sum(v for k, v in c.items() if k >= 5)
        lines.append(
            f"| {name} | **{max(lens)}** | {c.get(1, 0)} | {c.get(2, 0)} | "
            f"{c.get(3, 0)} | {c.get(4, 0)} | {ge5} |"
        )

    # 2. Détail des journées avec ≥3 SL consécutifs (les dangereuses)
    lines.append("\n## 2. Journées avec ≥3 SL consécutifs intra-day\n")
    for name, df in trades_by_strat.items():
        streaks = compute_intraday_streaks(df)
        bad_days = [s for s in streaks if s["len"] >= 3]
        if not bad_days:
            lines.append(f"\n**{name}** : aucune journée ≥3 SL consécutifs.")
            continue
        lines.append(f"\n**{name}** ({len(bad_days)} occurrences) :")
        for s in bad_days:
            lines.append(f"- {s['date']} : {s['len']} SL d'affilée")

    # 3. Analyse coût/bénéfice cutoff
    lines.append("\n## 3. Analyse coût/bénéfice — cutoff après N SL intra-day\n")
    lines.append("Pour chaque seuil N : on simule « si après N SL on arrête de "
                 "trader CE TICKER pour la journée, quel impact ?»\n")
    lines.append("- **n_blocked** : trades qui n'auraient pas été pris\n"
                 "- **delta_pnl** : variation PnL portfolio (négatif = on aurait perdu en bloquant, positif = gain)\n"
                 "- **gains_loss** : gains qu'on aurait raté ($)\n"
                 "- **losses_avoided** : pertes qu'on aurait évité ($)")

    portfolio_summary = []
    for name, df in trades_by_strat.items():
        df_s = streak_position_history(df)
        cb = cutoff_cost_benefit(df_s, max_n=5)
        lines.append(f"\n### {name}\n")
        lines.append("| N | n_blocked | gains_ratés | pertes_évitées | Δ PnL net | Verdict |")
        lines.append("|---|---|---|---|---|---|")
        for _, r in cb.iterrows():
            verdict = ""
            if r["delta_vs_baseline"] > 0:
                verdict = "✅ bénéfique"
            elif r["delta_vs_baseline"] < -100:
                verdict = "❌ trop restrictif"
            else:
                verdict = "≈ neutre"
            lines.append(
                f"| {int(r['N'])} | {int(r['n_blocked'])} | "
                f"${r['blocked_wins_$']:+,.0f} | ${r['blocked_losses_$']:+,.0f} | "
                f"${r['delta_vs_baseline']:+,.0f} | {verdict} |"
            )
        portfolio_summary.append((name, cb))

    # 4. Recommandations basées sur les chiffres
    lines.append("\n## 4. Synthèse cross-stratégie\n")
    lines.append("| Stratégie | N optimal | Δ PnL à N opt | Pourquoi |")
    lines.append("|---|---|---|---|")
    for name, cb in portfolio_summary:
        best = cb.loc[cb["delta_vs_baseline"].idxmax()]
        explanation = ""
        if best["delta_vs_baseline"] <= 0:
            explanation = "aucun cutoff bénéfique"
        else:
            explanation = f"évite ${-best['blocked_losses_$']:,.0f}, " \
                          f"rate ${best['blocked_wins_$']:,.0f}"
        lines.append(f"| {name} | N={int(best['N'])} | "
                     f"${best['delta_vs_baseline']:+,.0f} | {explanation} |")

    (OUTDIR / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n✓ Tout dans {OUTDIR}/")


if __name__ == "__main__":
    main()
