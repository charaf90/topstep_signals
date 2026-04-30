"""
Analyse univariée des features OPR : discrimination TP vs non-TP.

Pour chaque feature :
  - Mann-Whitney U test (IS) — séparation des distributions TP vs SL+TE
  - Corrélation point-biserial avec is_tp
  - Win rate par décile (IS)
  - Chart 3 panneaux (KDE / décile / violin IS-OOS)

Usage :
    python analyse/02_univariate_analysis.py
    python analyse/02_univariate_analysis.py --ticker NQ1
"""

import argparse
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from scipy.stats import mannwhitneyu, pointbiserialr, gaussian_kde

warnings.filterwarnings("ignore", category=RuntimeWarning)

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import INSTRUMENTS, CHART_STYLE

# ─────────────────────────────────────────────────────────────────────────────
# Constantes
# ─────────────────────────────────────────────────────────────────────────────

IS_END = "2025-09-30"

FEATURES_CONTINUES = [
    "time_since_opr_mins", "bars_since_opr", "session_hour_ny",
    "time_to_close_mins", "opr_range_atr_ratio", "opr_range_pts",
    "trigger_body_ratio", "trigger_close_strength",
    "close_beyond_opr_atr", "trigger_candle_size_atr",
    "trigger_vol_vs_opr", "trigger_vol_zscore",
    "max_excursion_atr",
    "alignment_score", "atr_ratio", "prev_return",
    "ovn_path_eff", "prev_close_pos",
]

FEATURES_DISCRETES = [
    "n_tests_before", "trigger_idx", "is_trend_aligned",
]

FEATURES_CATEGORIELLES = ["regime"]

COLOR_TP = "#26a69a"
COLOR_SL = "#ef5350"
COLOR_BASELINE = "#787b86"


# ─────────────────────────────────────────────────────────────────────────────
# Statistiques univariées
# ─────────────────────────────────────────────────────────────────────────────

def _stats_feature(df_is, feature):
    """
    Retourne un dict avec Mann-Whitney U (IS) + point-biserial + win rate
    par décile. Travaille uniquement sur IS.
    """
    df = df_is.dropna(subset=[feature, "is_tp"])
    n = len(df)
    if n < 10:
        return {
            "feature": feature, "mwu_pval": np.nan, "pbc": np.nan,
            "mwu_stat": np.nan, "n_is": n,
            "wr_decile_range": np.nan, "n_tp_is": 0,
        }

    tp = df.loc[df["is_tp"] == 1, feature].values
    ntp = df.loc[df["is_tp"] == 0, feature].values

    if len(tp) < 2 or len(ntp) < 2:
        return {
            "feature": feature, "mwu_pval": np.nan, "pbc": np.nan,
            "mwu_stat": np.nan, "n_is": n,
            "wr_decile_range": np.nan, "n_tp_is": int(len(tp)),
        }

    try:
        stat, pval = mannwhitneyu(tp, ntp, alternative="two-sided")
    except Exception:
        stat, pval = np.nan, np.nan

    try:
        pbc, _ = pointbiserialr(df["is_tp"].values.astype(float),
                                df[feature].values.astype(float))
    except Exception:
        pbc = np.nan

    # Win rate par décile
    try:
        df = df.copy()
        df["_dec"] = pd.qcut(df[feature], 10, labels=False, duplicates="drop")
        wr_by_dec = df.groupby("_dec")["is_tp"].mean()
        wr_range = float(wr_by_dec.max() - wr_by_dec.min())
    except Exception:
        wr_range = np.nan

    return {
        "feature": feature,
        "mwu_stat": float(stat) if not np.isnan(stat) else np.nan,
        "mwu_pval": float(pval) if not np.isnan(pval) else np.nan,
        "pbc": float(pbc) if not np.isnan(pbc) else np.nan,
        "n_is": int(n),
        "n_tp_is": int(len(tp)),
        "wr_decile_range": wr_range,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Charts
# ─────────────────────────────────────────────────────────────────────────────

def _setup_fig():
    plt.rcParams.update(CHART_STYLE)
    fig = plt.figure(figsize=(14, 8))
    spec = gridspec.GridSpec(2, 2, figure=fig, height_ratios=[1.4, 1],
                             hspace=0.40, wspace=0.30)
    ax_kde = fig.add_subplot(spec[0, 0])
    ax_dec = fig.add_subplot(spec[0, 1])
    ax_vio = fig.add_subplot(spec[1, :])
    return fig, ax_kde, ax_dec, ax_vio


def _plot_continue(df_filled, feature, ticker, out_dir):
    """Chart pour feature continue : KDE / décile / violin."""
    df_is = df_filled[df_filled["date"] <= IS_END].dropna(subset=[feature, "is_tp"])
    df_oos = df_filled[df_filled["date"] > IS_END].dropna(subset=[feature, "is_tp"])

    stats = _stats_feature(df_is, feature)
    fig, ax_kde, ax_dec, ax_vio = _setup_fig()

    # ── A : KDE TP vs SL+TE (IS) ──────────────────────────────────────────
    tp_is = df_is.loc[df_is["is_tp"] == 1, feature].values
    ntp_is = df_is.loc[df_is["is_tp"] == 0, feature].values

    all_vals = np.concatenate([tp_is, ntp_is])
    x_lo = np.percentile(all_vals, 1) if len(all_vals) > 0 else 0
    x_hi = np.percentile(all_vals, 99) if len(all_vals) > 0 else 1
    x_range = np.linspace(x_lo, x_hi, 200)

    for vals, label, col in [(tp_is, "TP", COLOR_TP), (ntp_is, "SL+TE", COLOR_SL)]:
        if len(vals) >= 4:
            try:
                kde = gaussian_kde(vals, bw_method="scott")
                y = kde(x_range)
                ax_kde.fill_between(x_range, y, alpha=0.25, color=col)
                ax_kde.plot(x_range, y, color=col, lw=1.5,
                            label=f"{label} (n={len(vals)})")
            except Exception:
                pass

    ax_kde.set_title("Distribution IS — TP vs SL+TE", fontsize=9)
    ax_kde.legend(fontsize=8)
    ax_kde.set_xlabel(feature, fontsize=8)
    ax_kde.set_ylabel("Densité", fontsize=8)

    # ── B : Win rate par décile (IS) ──────────────────────────────────────
    if len(df_is) >= 20:
        df_tmp = df_is.copy()
        try:
            df_tmp["_dec"] = pd.qcut(df_tmp[feature], 10,
                                     labels=False, duplicates="drop")
            wr_df = df_tmp.groupby("_dec").agg(
                wr=("is_tp", "mean"), cnt=("is_tp", "count")
            ).reset_index()
            bars = ax_dec.bar(
                wr_df["_dec"], wr_df["wr"] * 100,
                color=[COLOR_TP if v > 0.5 else COLOR_SL for v in wr_df["wr"]],
                alpha=0.8, width=0.7
            )
            for bar, cnt in zip(bars, wr_df["cnt"]):
                ax_dec.text(
                    bar.get_x() + bar.get_width() / 2,
                    bar.get_height() + 0.5,
                    str(int(cnt)), ha="center", va="bottom", fontsize=6
                )
        except Exception:
            ax_dec.text(0.5, 0.5, "données insuffisantes",
                        ha="center", va="center", transform=ax_dec.transAxes)

    baseline_wr = df_is["is_tp"].mean() * 100 if len(df_is) > 0 else 50
    ax_dec.axhline(baseline_wr, ls="--", color="#d1d4dc", lw=0.8,
                   label=f"Baseline {baseline_wr:.0f}%")
    ax_dec.set_title("Win rate IS par décile", fontsize=9)
    ax_dec.set_xlabel("Décile (0=bas, 9=haut)", fontsize=8)
    ax_dec.set_ylabel("Win rate %", fontsize=8)
    ax_dec.legend(fontsize=7)

    # ── C : Violin IS / OOS × TP / SL+TE ─────────────────────────────────
    groups = []
    labels_v = []
    colors_v = []
    for set_name, df_set in [("IS", df_is), ("OOS", df_oos)]:
        for outcome, col in [(1, COLOR_TP), (0, COLOR_SL)]:
            label_str = "TP" if outcome == 1 else "SL+TE"
            vals = df_set.loc[df_set["is_tp"] == outcome, feature].dropna().values
            if len(vals) >= 4:
                groups.append(vals)
                labels_v.append(f"{label_str}\n{set_name}\n(n={len(vals)})")
                colors_v.append(col)

    if groups:
        try:
            parts = ax_vio.violinplot(
                groups, positions=range(len(groups)),
                showmedians=True, showextrema=False
            )
            for pc, col in zip(parts["bodies"], colors_v):
                pc.set_facecolor(col)
                pc.set_alpha(0.35)
            parts["cmedians"].set_color("#d1d4dc")
            parts["cmedians"].set_linewidth(1.5)
        except Exception:
            pass

    ax_vio.set_xticks(range(len(labels_v)))
    ax_vio.set_xticklabels(labels_v, fontsize=7)
    ax_vio.set_ylabel(feature, fontsize=8)

    # Titre global
    pval_str = f"{stats['mwu_pval']:.3f}" if not np.isnan(stats["mwu_pval"]) else "N/A"
    pbc_str = f"{stats['pbc']:.3f}" if not np.isnan(stats["pbc"]) else "N/A"
    fig.suptitle(
        f"{ticker} — {feature}"
        f"  |  IS n={stats['n_is']}  MWU p={pval_str}  PBC={pbc_str}",
        fontsize=10, y=0.98
    )

    _save_fig(fig, out_dir / f"{feature}_{ticker}.png")
    return stats


def _plot_discrete(df_filled, feature, ticker, out_dir):
    """Chart pour feature discrète (entiers) ou binaire : bar plots."""
    df_is = df_filled[df_filled["date"] <= IS_END].dropna(subset=[feature, "is_tp"])
    df_oos = df_filled[df_filled["date"] > IS_END].dropna(subset=[feature, "is_tp"])

    stats = _stats_feature(df_is, feature)
    fig, ax_kde, ax_dec, ax_vio = _setup_fig()

    # ── A : Bar plot win rate par valeur (IS) ─────────────────────────────
    if len(df_is) >= 5:
        df_tmp = df_is.copy()
        wr_df = df_tmp.groupby(feature).agg(
            wr=("is_tp", "mean"), cnt=("is_tp", "count")
        ).reset_index()
        ax_kde.bar(wr_df[feature].astype(str), wr_df["wr"] * 100,
                   color=[COLOR_TP if v > 0.5 else COLOR_SL for v in wr_df["wr"]],
                   alpha=0.8)
        for _, row in wr_df.iterrows():
            ax_kde.text(str(row[feature]), row["wr"] * 100 + 0.5,
                        str(int(row["cnt"])), ha="center", va="bottom", fontsize=7)
    baseline_wr = df_is["is_tp"].mean() * 100 if len(df_is) > 0 else 50
    ax_kde.axhline(baseline_wr, ls="--", color="#d1d4dc", lw=0.8)
    ax_kde.set_title("Win rate IS par valeur", fontsize=9)
    ax_kde.set_xlabel(feature, fontsize=8)
    ax_kde.set_ylabel("Win rate %", fontsize=8)

    # ── B : Comptage IS par valeur (TP vs SL+TE empilé) ───────────────────
    if len(df_is) >= 5:
        df_tmp2 = df_is.groupby([feature, "is_tp"]).size().unstack(fill_value=0)
        vals_x = [str(v) for v in df_tmp2.index]
        if 0 in df_tmp2.columns:
            ax_dec.bar(vals_x, df_tmp2[0], label="SL+TE", color=COLOR_SL, alpha=0.7)
        if 1 in df_tmp2.columns:
            bottom = df_tmp2[0] if 0 in df_tmp2.columns else 0
            ax_dec.bar(vals_x, df_tmp2[1], bottom=bottom,
                       label="TP", color=COLOR_TP, alpha=0.7)
    ax_dec.set_title("Distribution IS par valeur", fontsize=9)
    ax_dec.set_xlabel(feature, fontsize=8)
    ax_dec.set_ylabel("Nb trades", fontsize=8)
    ax_dec.legend(fontsize=7)

    # ── C : Win rate IS vs OOS côte à côte ────────────────────────────────
    for i, (set_name, df_set) in enumerate([("IS", df_is), ("OOS", df_oos)]):
        if len(df_set) < 5:
            continue
        wr_df_s = df_set.groupby(feature)["is_tp"].mean() * 100
        ax_vio.plot(
            [str(v) for v in wr_df_s.index], wr_df_s.values,
            "o-", color=COLOR_TP if i == 0 else COLOR_SL,
            label=set_name, alpha=0.8, lw=1.5, ms=5
        )
    ax_vio.axhline(50, ls="--", color=COLOR_BASELINE, lw=0.8)
    ax_vio.set_title("Win rate IS vs OOS par valeur", fontsize=9)
    ax_vio.set_xlabel(feature, fontsize=8)
    ax_vio.set_ylabel("Win rate %", fontsize=8)
    ax_vio.legend(fontsize=7)

    pval_str = f"{stats['mwu_pval']:.3f}" if not np.isnan(stats["mwu_pval"]) else "N/A"
    pbc_str = f"{stats['pbc']:.3f}" if not np.isnan(stats["pbc"]) else "N/A"
    fig.suptitle(
        f"{ticker} — {feature}"
        f"  |  IS n={stats['n_is']}  MWU p={pval_str}  PBC={pbc_str}",
        fontsize=10, y=0.98
    )
    _save_fig(fig, out_dir / f"{feature}_{ticker}.png")
    return stats


def _plot_categorielle(df_filled, feature, ticker, out_dir):
    """Chart pour feature catégorielle (regime)."""
    df_is = df_filled[df_filled["date"] <= IS_END].dropna(subset=[feature, "is_tp"])
    df_oos = df_filled[df_filled["date"] > IS_END].dropna(subset=[feature, "is_tp"])

    # Pas de MWU sur catégorielle → stats simplifiées
    stats = {
        "feature": feature, "mwu_pval": np.nan, "pbc": np.nan,
        "mwu_stat": np.nan, "n_is": len(df_is), "n_tp_is": int(df_is["is_tp"].sum()),
        "wr_decile_range": np.nan,
    }

    plt.rcParams.update(CHART_STYLE)
    fig, axes = plt.subplots(1, 2, figsize=(12, 5))

    for ax, (set_name, df_set) in zip(axes, [("IS", df_is), ("OOS", df_oos)]):
        if len(df_set) == 0:
            ax.set_title(f"{set_name} — vide")
            continue
        wr_df = df_set.groupby(feature).agg(
            wr=("is_tp", "mean"), cnt=("is_tp", "count")
        ).reset_index()
        bars = ax.bar(wr_df[feature].astype(str), wr_df["wr"] * 100,
                      color=[COLOR_TP if v > 0.5 else COLOR_SL for v in wr_df["wr"]],
                      alpha=0.8)
        for bar, cnt in zip(bars, wr_df["cnt"]):
            ax.text(bar.get_x() + bar.get_width() / 2,
                    bar.get_height() + 0.5, str(int(cnt)),
                    ha="center", va="bottom", fontsize=8)
        baseline_wr = df_set["is_tp"].mean() * 100
        ax.axhline(baseline_wr, ls="--", color="#d1d4dc", lw=0.8,
                   label=f"Baseline {baseline_wr:.0f}%")
        ax.set_title(f"Win rate {set_name} par {feature}", fontsize=9)
        ax.set_ylabel("Win rate %", fontsize=8)
        ax.legend(fontsize=7)

    fig.suptitle(f"{ticker} — {feature}  |  IS n={len(df_is)}", fontsize=10)
    _save_fig(fig, out_dir / f"{feature}_{ticker}.png")
    return stats


def _save_fig(fig, path):
    fig.savefig(
        str(path), dpi=120, bbox_inches="tight",
        facecolor=fig.get_facecolor()
    )
    plt.close(fig)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Analyse univariée features OPR"
    )
    parser.add_argument("--ticker", type=str, default=None)
    args = parser.parse_args()

    data_dir = Path(__file__).parent / "data"
    chart_dir = Path(__file__).parent / "charts" / "univariate"
    chart_dir.mkdir(parents=True, exist_ok=True)

    tickers = [args.ticker] if args.ticker else list(INSTRUMENTS.keys())

    for ticker in tickers:
        csv_path = data_dir / f"opr_triggers_{ticker}.csv"
        if not csv_path.exists():
            print(f"[!] {csv_path} manquant — lancer 01_extract_features.py d'abord")
            continue

        print(f"\n{'='*60}")
        print(f"  {ticker}")
        print(f"{'='*60}")

        df = pd.read_csv(csv_path, low_memory=False)
        df["date"] = df["date"].astype(str)

        # Analyse sur les fills uniquement (exclure NOT_FILLED pour is_tp)
        df_filled = df[df["result"] != "NOT_FILLED"].copy()

        n_is = (df_filled["date"] <= IS_END).sum()
        n_oos = (df_filled["date"] > IS_END).sum()
        baseline_is = df_filled.loc[df_filled["date"] <= IS_END, "is_tp"].mean()
        print(f"  Fills IS={n_is}  OOS={n_oos}  "
              f"Baseline win rate IS={baseline_is*100:.1f}%")

        ranking_rows = []

        for feature in FEATURES_CONTINUES:
            if feature not in df_filled.columns:
                print(f"  [!] {feature} absent du CSV")
                continue
            stats = _plot_continue(df_filled, feature, ticker, chart_dir)
            ranking_rows.append(stats)
            pval_str = f"{stats['mwu_pval']:.3f}" if not np.isnan(stats["mwu_pval"]) else " N/A "
            pbc_str = f"{stats['pbc']:+.3f}" if not np.isnan(stats["pbc"]) else "  N/A "
            print(f"  {feature:30s}  p={pval_str}  pbc={pbc_str}")

        for feature in FEATURES_DISCRETES:
            if feature not in df_filled.columns:
                continue
            stats = _plot_discrete(df_filled, feature, ticker, chart_dir)
            ranking_rows.append(stats)
            pval_str = f"{stats['mwu_pval']:.3f}" if not np.isnan(stats["mwu_pval"]) else " N/A "
            pbc_str = f"{stats['pbc']:+.3f}" if not np.isnan(stats["pbc"]) else "  N/A "
            print(f"  {feature:30s}  p={pval_str}  pbc={pbc_str}")

        for feature in FEATURES_CATEGORIELLES:
            if feature not in df_filled.columns:
                continue
            stats = _plot_categorielle(df_filled, feature, ticker, chart_dir)
            ranking_rows.append(stats)
            print(f"  {feature:30s}  (catégorielle)")

        # Ranking
        df_rank = pd.DataFrame(ranking_rows)
        df_rank["abs_pbc"] = df_rank["pbc"].abs()
        df_rank_sorted = df_rank.sort_values(
            ["mwu_pval", "abs_pbc"], ascending=[True, False]
        ).drop(columns=["abs_pbc"])

        rank_path = data_dir / f"feature_ranking_{ticker}.csv"
        df_rank_sorted.to_csv(rank_path, index=False)

        print(f"\n  TOP features (IS) :")
        top = df_rank_sorted.dropna(subset=["mwu_pval"]).head(8)
        for _, row in top.iterrows():
            print(f"    {row['feature']:30s}  "
                  f"p={row['mwu_pval']:.3f}  "
                  f"pbc={row['pbc']:+.3f}  "
                  f"n={int(row['n_is'])}")

        print(f"\n  Ranking exporté → {rank_path}")
        print(f"  Charts → {chart_dir}/")


if __name__ == "__main__":
    main()
