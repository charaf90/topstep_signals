"""
Pré-analyse exploratoire des features F1/F2/F3 d'opr-v5.

Objectif :
  1. Exécuter opr-v5 avec tous les filtres à None (= équivalent opr-v4 enrichi)
     sur l'historique complet pour les 3 tickers (MES1, NQ1, YM1).
  2. Agréger tous les trades en un DataFrame unique.
  3. Imprimer la distribution P&L par bins de F1/F2/F3 avec WR, PF, P&L moyen/total.
  4. Identifier les bins clairement perdants (n ≥ 20, P&L négatif significatif)
     → ces bornes alimentent le PARAM_GRID resserré pour la PHASE 4 walk-forward.

Sortie :
  • output/opr_v5_features.csv        — DataFrame agrégé (1 ligne par trade)
  • output/opr_v5_features_analysis.md — rapport markdown synthétique

Reproductibilité : seed=42 (cohérent avec convention projet).

Usage :
    python scripts/explore_opr_v5_features.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

np.random.seed(42)

# Ajustement chemin pour exécution depuis racine projet
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.data import build_timeframes, load_csv
from strategies import opr_v5

TICKERS = ["MES1", "NQ1", "YM1"]


# ─────────────────────────────────────────────────────────────────────────────
# Calcul des stats par bin
# ─────────────────────────────────────────────────────────────────────────────


def _stats_for_bin(df: pd.DataFrame) -> dict:
    """Stats d'un bin : n, WR, PF, P&L total, P&L moyen."""
    if df.empty:
        return {"n": 0, "WR_pct": np.nan, "PF": np.nan, "pnl_tot": 0.0, "pnl_mean": np.nan}
    n = len(df)
    wins = df[df["pnl"] > 0]
    losses = df[df["pnl"] < 0]
    wr = 100.0 * len(wins) / n if n > 0 else np.nan
    gross_win = wins["pnl"].sum() if not wins.empty else 0.0
    gross_loss = abs(losses["pnl"].sum()) if not losses.empty else 0.0
    if gross_loss <= 0:
        pf = np.inf if gross_win > 0 else np.nan
    else:
        pf = gross_win / gross_loss
    return {
        "n": int(n),
        "WR_pct": round(float(wr), 1),
        "PF": round(float(pf), 2) if np.isfinite(pf) else "inf",
        "pnl_tot": round(float(df["pnl"].sum()), 2),
        "pnl_mean": round(float(df["pnl"].mean()), 2),
    }


def _bin_label(left: float, right: float, right_inclusive: bool = False) -> str:
    """Label de bin : [left, right) ou [left, +inf)."""
    if np.isinf(right):
        return f"[{left:g}, +inf)"
    bracket = "]" if right_inclusive else ")"
    return f"[{left:g}, {right:g}{bracket}"


def _analyze_feature(
    df_trades: pd.DataFrame,
    feature_col: str,
    bins: list,
    label: str,
    output_lines: list,
):
    """
    Imprime + accumule l'analyse d'une feature par bins.

    Args:
        df_trades   : DataFrame agrégé (filled trades uniquement pour F2/F3)
        feature_col : nom de la colonne feature
        bins        : liste de bornes (ex: [0,1,2,4,8,12,np.inf])
        label       : titre humain
        output_lines: liste de strings où on ajoute les lignes du rapport
    """
    output_lines.append("")
    output_lines.append(f"### Distribution P&L par bin — {label}")
    output_lines.append("")
    output_lines.append("| Bin | n | WR % | PF | P&L tot $ | P&L moy $ |")
    output_lines.append("|---|---:|---:|---:|---:|---:|")

    print(f"\n  {label}")
    print(f"  {'Bin':<14} {'n':>5} {'WR%':>7} {'PF':>7} {'P&L tot':>11} {'P&L moy':>9}")
    print(f"  {'-'*14} {'-'*5} {'-'*7} {'-'*7} {'-'*11} {'-'*9}")

    for i in range(len(bins) - 1):
        left, right = bins[i], bins[i + 1]
        sub = df_trades[(df_trades[feature_col] >= left) & (df_trades[feature_col] < right)]
        s = _stats_for_bin(sub)
        bin_lbl = _bin_label(left, right, right_inclusive=False)
        print(
            f"  {bin_lbl:<14} {s['n']:>5d} {s['WR_pct']:>7} {str(s['PF']):>7} "
            f"{s['pnl_tot']:>+11.2f} {s['pnl_mean']:>+9.2f}"
        )
        output_lines.append(
            f"| {bin_lbl} | {s['n']} | {s['WR_pct']} | {s['PF']} | "
            f"{s['pnl_tot']:+.2f} | {s['pnl_mean']:+.2f} |"
        )


def _analyze_feature_per_ticker(
    df_trades: pd.DataFrame,
    feature_col: str,
    bins: list,
    label: str,
    output_lines: list,
):
    """Version per-ticker d'_analyze_feature."""
    for ticker in TICKERS:
        sub = df_trades[df_trades["ticker"] == ticker]
        if sub.empty:
            continue
        output_lines.append("")
        output_lines.append(f"### {label} — {ticker} (n={len(sub)})")
        output_lines.append("")
        output_lines.append("| Bin | n | WR % | PF | P&L tot $ | P&L moy $ |")
        output_lines.append("|---|---:|---:|---:|---:|---:|")

        print(f"\n  {label} — {ticker} (n={len(sub)})")
        print(f"  {'Bin':<14} {'n':>5} {'WR%':>7} {'PF':>7} {'P&L tot':>11} {'P&L moy':>9}")
        print(f"  {'-'*14} {'-'*5} {'-'*7} {'-'*7} {'-'*11} {'-'*9}")

        for i in range(len(bins) - 1):
            left, right = bins[i], bins[i + 1]
            sub_b = sub[(sub[feature_col] >= left) & (sub[feature_col] < right)]
            s = _stats_for_bin(sub_b)
            bin_lbl = _bin_label(left, right, right_inclusive=False)
            print(
                f"  {bin_lbl:<14} {s['n']:>5d} {s['WR_pct']:>7} {str(s['PF']):>7} "
                f"{s['pnl_tot']:>+11.2f} {s['pnl_mean']:>+9.2f}"
            )
            output_lines.append(
                f"| {bin_lbl} | {s['n']} | {s['WR_pct']} | {s['PF']} | "
                f"{s['pnl_tot']:+.2f} | {s['pnl_mean']:+.2f} |"
            )


# ─────────────────────────────────────────────────────────────────────────────
# Identification des bins perdants (alimentation PARAM_GRID resserré)
# ─────────────────────────────────────────────────────────────────────────────


def _identify_losing_bins(
    df_trades: pd.DataFrame,
    feature_col: str,
    bins: list,
    label: str,
    output_lines: list,
):
    """
    Liste les bins clairement perdants : n ≥ 20 ET P&L tot < 0 ET PF < 1.0.
    Ces bornes sont des candidats à exclure dans le PARAM_GRID.
    """
    output_lines.append("")
    output_lines.append(f"### Bins clairement perdants — {label}")
    output_lines.append("")
    output_lines.append("Critère : n ≥ 20 ET P&L < 0 ET PF < 1.0 (portfolio).")
    output_lines.append("")

    found = False
    for i in range(len(bins) - 1):
        left, right = bins[i], bins[i + 1]
        sub = df_trades[(df_trades[feature_col] >= left) & (df_trades[feature_col] < right)]
        s = _stats_for_bin(sub)
        if s["n"] >= 20 and isinstance(s["pnl_tot"], float) and s["pnl_tot"] < 0:
            pf_val = s["PF"]
            if pf_val == "inf":
                continue
            try:
                pf_num = float(pf_val) if isinstance(pf_val, (int, float)) else float(pf_val)
            except (ValueError, TypeError):
                continue
            if pf_num < 1.0:
                bin_lbl = _bin_label(left, right, right_inclusive=False)
                line = (
                    f"- **{bin_lbl}** : n={s['n']}, P&L={s['pnl_tot']:+.2f} $, "
                    f"PF={pf_val}, WR={s['WR_pct']} %"
                )
                output_lines.append(line)
                print(
                    f"    [PERDANT] {label} {bin_lbl} : "
                    f"n={s['n']}, P&L={s['pnl_tot']:+.2f}, PF={pf_val}"
                )
                found = True
    if not found:
        output_lines.append("Aucun bin perdant identifié sur ce critère.")
        print(f"    [INFO] {label} : aucun bin perdant n≥20 PF<1.0")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────


def main() -> int:
    print("=" * 70)
    print("  PRÉ-ANALYSE FEATURES OPR v5 (F1, F2, F3)")
    print("=" * 70)

    # ── Chargement données + exécution opr-v5 (filtres None) ─────────────────
    all_trades = []
    for ticker in TICKERS:
        csv = PROJECT_ROOT / "data" / f"{ticker}_data_m15.csv"
        if not csv.exists():
            print(f"  [!] Manquant : {csv}")
            continue
        df = load_csv(str(csv))
        tf = build_timeframes(df)
        print(f"\n  → Backtest opr-v5 {ticker} (filtres None)...")
        trades = opr_v5.run_backtest(df, ticker, tf=tf, params=None, topstep_guard=False)
        trades["ticker"] = ticker
        all_trades.append(trades)
        print(
            f"    {ticker}: {len(trades):4d} trades  "
            f"(filled = {(trades['result'] != 'NOT_FILLED').sum()}, "
            f"P&L = {trades['pnl'].sum():+,.2f} $)"
        )

    if not all_trades:
        print("  [!] Aucun trade généré — arrêt.")
        return 1

    df_all = pd.concat(all_trades, ignore_index=True)

    # Sauvegarde du DataFrame agrégé (utile pour analyses ultérieures)
    out_dir = PROJECT_ROOT / "output"
    out_dir.mkdir(exist_ok=True)
    cols_to_keep = [
        "ticker",
        "date",
        "dir",
        "result",
        "pnl",
        "f1_bars",
        "f2_excursion_pts",
        "f2_excursion_atr",
        "f2_excursion_oprrange",
        "f3_bars",
        "v5_reject_reason",
        "trigger_time",
        "fill_time",
    ]
    cols_present = [c for c in cols_to_keep if c in df_all.columns]
    df_export = df_all[cols_present].copy()
    csv_path = out_dir / "opr_v5_features.csv"
    df_export.to_csv(csv_path, index=False)
    print(f"\n  ✓ {csv_path}")

    # ── Préparation rapport markdown ──────────────────────────────────────────
    output_lines: list = []
    output_lines.append("# Pré-analyse exploratoire — features OPR v5")
    output_lines.append("")
    output_lines.append(
        "Distributions de P&L par bin de F1, F2, F3 sur l'historique "
        "complet sept 2024 → mai 2026 (filtres None — opr-v5 = opr-v4 enrichi)."
    )
    output_lines.append("")
    output_lines.append("## Résumé")
    output_lines.append("")
    output_lines.append("| Ticker | Trades totaux | Filled | P&L brut $ | P&L par trade |")
    output_lines.append("|---|---:|---:|---:|---:|")
    for ticker in TICKERS:
        sub = df_all[df_all["ticker"] == ticker]
        if sub.empty:
            continue
        filled = sub[sub["result"] != "NOT_FILLED"]
        pnl_total = float(sub["pnl"].sum())
        pnl_per_trade = pnl_total / len(filled) if len(filled) > 0 else 0.0
        output_lines.append(
            f"| {ticker} | {len(sub)} | {len(filled)} | "
            f"{pnl_total:+,.2f} | {pnl_per_trade:+,.2f} |"
        )

    # ── Analyses par feature ─────────────────────────────────────────────────
    # On distingue :
    #   F1 : analyse sur TOUS les trades (filled + NOT_FILLED nativement) car
    #        F1 est toujours calculable depuis trigger_time
    #   F2/F3 : analyse uniquement sur trades filled (NaN sinon)

    # F1 bins
    F1_BINS = [0, 1, 2, 4, 8, 12, np.inf]
    print("\n" + "=" * 70)
    print("  ANALYSE F1 : nb bougies fin OPR → trigger")
    print("  (TOUS les trades, filled + NOT_FILLED nativement)")
    print("=" * 70)

    output_lines.append("")
    output_lines.append("---")
    output_lines.append("")
    output_lines.append("## F1 : nb bougies fin OPR → trigger")
    output_lines.append("")
    output_lines.append(
        "Calculé pour tous les trades v4 (filled + NOT_FILLED). "
        "F1 = 0 signifie cassure immédiate à la 1ère bougie post-OPR. "
        "F1 ≥ 1 signifie au moins une bougie écoulée avant cassure."
    )

    # Filtrer les NaN sur F1 (cas pathologiques)
    df_f1 = df_all.dropna(subset=["f1_bars"]).copy()
    _analyze_feature(df_f1, "f1_bars", F1_BINS, "F1 — portfolio (tous tickers)", output_lines)
    _analyze_feature_per_ticker(df_f1, "f1_bars", F1_BINS, "F1", output_lines)
    _identify_losing_bins(df_f1, "f1_bars", F1_BINS, "F1 portfolio", output_lines)

    # F2 (excursion en ATR daily) — uniquement sur filled
    F2_BINS = [0, 0.25, 0.5, 0.75, 1.0, 1.5, np.inf]
    print("\n" + "=" * 70)
    print("  ANALYSE F2 : excursion max trigger → fill (normalisée ATR daily)")
    print("  (UNIQUEMENT trades filled)")
    print("=" * 70)

    output_lines.append("")
    output_lines.append("---")
    output_lines.append("")
    output_lines.append("## F2 : excursion max trigger → fill (en ATR daily)")
    output_lines.append("")
    output_lines.append(
        "F2 = max excursion dans le sens de la cassure entre `trigger_time` "
        "et `fill_time`, divisée par l'ATR daily 14j. Hypothèse : une "
        "excursion trop grande suggère un retournement plutôt qu'un "
        "pullback ordonné. Uniquement sur trades filled nativement."
    )

    df_filled = df_all[df_all["result"] != "NOT_FILLED"].copy()
    df_f2 = df_filled.dropna(subset=["f2_excursion_atr"])
    _analyze_feature(
        df_f2, "f2_excursion_atr", F2_BINS, "F2 — portfolio (filled uniquement)", output_lines
    )
    _analyze_feature_per_ticker(df_f2, "f2_excursion_atr", F2_BINS, "F2", output_lines)
    _identify_losing_bins(df_f2, "f2_excursion_atr", F2_BINS, "F2 portfolio", output_lines)

    # F3 (nb bougies trigger → fill) — uniquement sur filled
    F3_BINS = [1, 2, 3, 5, 8, np.inf]
    print("\n" + "=" * 70)
    print("  ANALYSE F3 : nb bougies trigger → fill")
    print("  (UNIQUEMENT trades filled)")
    print("=" * 70)

    output_lines.append("")
    output_lines.append("---")
    output_lines.append("")
    output_lines.append("## F3 : nb bougies trigger → fill (M15)")
    output_lines.append("")
    output_lines.append(
        "F3 = nb de bougies M15 strictement entre `trigger_time` et "
        "`fill_time`. F3 = 1 : pullback à la bougie immédiatement "
        "post-trigger. F3 ≥ 2 : pullback retardé. "
        "Uniquement sur trades filled nativement."
    )

    df_f3 = df_filled.dropna(subset=["f3_bars"])
    _analyze_feature(df_f3, "f3_bars", F3_BINS, "F3 — portfolio (filled uniquement)", output_lines)
    _analyze_feature_per_ticker(df_f3, "f3_bars", F3_BINS, "F3", output_lines)
    _identify_losing_bins(df_f3, "f3_bars", F3_BINS, "F3 portfolio", output_lines)

    # ── Bornes prometteuses suggérées pour PARAM_GRID resserré ───────────────
    output_lines.append("")
    output_lines.append("---")
    output_lines.append("")
    output_lines.append("## Bornes prometteuses suggérées pour PARAM_GRID resserré")
    output_lines.append("")
    output_lines.append(
        "À partir de la distribution observée, candidats à tester en "
        "walk-forward (à valider par optimize.py) :"
    )
    output_lines.append("")
    output_lines.append(
        "- **F1 max** : bornes à tester pour exclure les cassures "
        "trop tardives → voir bins perdants F1 portfolio + per-ticker"
    )
    output_lines.append(
        "- **F2 max ATR** : bornes à tester pour exclure les excursions "
        "excessives → voir bins perdants F2"
    )
    output_lines.append(
        "- **F3 max** : bornes à tester pour exclure les pullbacks "
        "trop tardifs → voir bins perdants F3"
    )
    output_lines.append("")
    output_lines.append(
        "Décision finale du PARAM_GRID : voir `strategies/opr_v5.py` "
        "(constante `PARAM_GRID`). Le grid initial est large pour "
        "explorer ; après ce rapport, il sera resserré aux bornes "
        "prometteuses."
    )

    # ── Écriture du rapport markdown ─────────────────────────────────────────
    md_path = out_dir / "opr_v5_features_analysis.md"
    md_path.write_text("\n".join(output_lines))
    print(f"\n  ✓ {md_path}")
    print("\n" + "=" * 70)
    print("  ✅ Pré-analyse terminée — voir output/opr_v5_features_analysis.md")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
