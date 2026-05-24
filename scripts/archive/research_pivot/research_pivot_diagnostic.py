"""
Diagnostic des high-confidence errors sur MCL1/MGC1 order=20.

Question : quand le modèle prédit "pivot" avec une forte proba mais se
trompe, qu'ont en commun ces erreurs ?

Pipeline :
1. Entraîne RF baseline (WF 4 splits)
2. Concatène les probas OOS des 4 splits
3. Identifie le seuil tel que recall ≈ 10 % (cohérent avec notre métrique)
4. Sépare prédictions à ce seuil en TP (raison) et FP (erreurs)
5. Compare TP vs FP sur features clés : moyenne, distribution, heure, etc.
6. Cherche des règles "n'afficher pas le signal si X" qui retirent
   surtout des FP en préservant les TP → filtre exploitable

Usage : python scripts/research_pivot_diagnostic.py --ticker MCL1
"""

from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy import stats
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import precision_recall_curve

warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)
np.random.seed(42)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import research_pivot_nq1 as base  # noqa: E402
from research_pivot_divergence import (  # noqa: E402
    BASELINE_FEATURES,
    OOS_HORIZON_DAYS,
    ORDER,
    SPLITS,
)

from core.data import load_csv  # noqa: E402

OUT_ROOT = ROOT / "output" / "pivot_research_diag"

# Features qu'on regarde pour le diagnostic (sous-ensemble interprétable)
DIAG_FEATURES = [
    "dist_to_min20_atr",
    "dist_to_max20_atr",
    "dist_close_ema21_atr",
    "atr_14",
    "atr_ratio_short_long",
    "bb_width",
    "range_atr_ratio",
    "adx_14",
    "vol_rel",
    "ret_lag_1",
    "ret_lag_2",
    "ret_lag_5",
    "hour_ny",
    "dow",
    "is_macro_day",
    "past_pivot_density_2atr",
]


def precision_at_recall(y_true, y_score, target):
    p, r, _ = precision_recall_curve(y_true, y_score)
    mask = r >= target
    if not mask.any():
        return float("nan"), float("nan")
    idx = np.where(mask)[0]
    # On choisit le seuil qui donne le recall le plus proche au-dessus de target
    best_idx = idx[np.argmax(p[idx])]
    # threshold associé : précision_recall_curve renvoie thresholds[:-1] aligné avec p[:-1]
    _, _, thresh = precision_recall_curve(y_true, y_score)
    if best_idx >= len(thresh):
        best_idx = len(thresh) - 1
    return float(p[best_idx]), float(thresh[best_idx])


def compute_oos_predictions(df, feature_cols, label_col="is_pivot_any"):
    """Walk-forward expanding window. Concatène tous les OOS pour avoir une
    masse de prédictions à analyser.
    """
    rows = []
    for i, is_end in enumerate(SPLITS, start=1):
        oos_end = is_end + pd.Timedelta(days=OOS_HORIZON_DAYS)
        df_is = df[df.index <= is_end]
        df_oos = df[(df.index > is_end) & (df.index <= oos_end)]
        if len(df_is) < 5000 or df_oos[label_col].sum() < 20:
            continue
        X_is = df_is[feature_cols].to_numpy(dtype=np.float32)
        y_is = df_is[label_col].to_numpy(dtype=np.int32)
        X_oos = df_oos[feature_cols].to_numpy(dtype=np.float32)
        y_oos = df_oos[label_col].to_numpy(dtype=np.int32)

        rf = RandomForestClassifier(
            n_estimators=300,
            max_depth=8,
            class_weight="balanced",
            random_state=42,
            n_jobs=-1,
            min_samples_leaf=20,
        )
        rf.fit(X_is, y_is)
        p_oos = rf.predict_proba(X_oos)[:, 1]
        # On garde index temporel + features + label + proba
        sub = df_oos[DIAG_FEATURES + [label_col]].copy()
        sub["proba"] = p_oos
        sub["split"] = i
        rows.append(sub)
    return pd.concat(rows).sort_index()


def diagnose(ticker: str):
    out_dir = OUT_ROOT / ticker
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"▸ Diagnostic {ticker} order={ORDER}")
    data_csv = ROOT / "data" / f"{ticker}_data_m5.csv"
    df = load_csv(str(data_csv))

    base.TICKER = ticker
    base.PIVOT_ORDER = ORDER
    base.OUT_DIR = out_dir
    base.DATA_CSV = data_csv

    df = base.label_pivots(df, order=ORDER)
    df = base.build_features(df)
    df = df.dropna(subset=BASELINE_FEATURES + ["is_pivot_any"]).copy()
    print(f"  • {len(df):,} barres après dropna")

    # Construire un dataset de prédictions WF
    print("  • Train + predict WF (4 splits, RF)…")
    pred_df = compute_oos_predictions(df, BASELINE_FEATURES)
    print(f"  • {len(pred_df):,} prédictions OOS au total")

    y = pred_df["is_pivot_any"].to_numpy()
    p = pred_df["proba"].to_numpy()

    # Trouver le seuil tel que recall ≈ 10 %
    prec, rec, thr = precision_recall_curve(y, p)
    mask = rec[:-1] >= 0.10
    if not mask.any():
        print("  ⚠ pas assez de signal pour atteindre recall 10%")
        return
    # On prend le seuil qui maximise précision parmi ceux qui donnent recall ≥ 10%
    valid_idx = np.where(mask)[0]
    best_idx = valid_idx[np.argmax(prec[:-1][valid_idx])]
    thr_10 = thr[best_idx]
    p_at_10 = prec[best_idx]
    r_at_10 = rec[best_idx]
    print(f"  • Seuil proba @ recall≈10% : {thr_10:.3f}")
    print(f"  • Précision à ce seuil : {p_at_10:.2%}  (recall réel : {r_at_10:.2%})")

    # High-confidence predictions
    signals = pred_df[pred_df["proba"] >= thr_10].copy()
    tp = signals[signals["is_pivot_any"] == 1]
    fp = signals[signals["is_pivot_any"] == 0]
    print(f"  • Signaux totaux : {len(signals)}  (TP : {len(tp)} | FP : {len(fp)})")
    print(
        f"  • Base rate global : {y.mean():.2%}  | Précision signaux : {len(tp) / max(1, len(signals)):.2%}"
    )

    # ────────── Analyse comparative TP vs FP ──────────
    # Pour chaque feature, on calcule mean(TP), mean(FP), Welch t-test
    compare_rows = []
    for f in DIAG_FEATURES:
        a = tp[f].dropna()
        b = fp[f].dropna()
        if len(a) < 5 or len(b) < 5:
            continue
        t_stat, p_val = stats.ttest_ind(a, b, equal_var=False)
        compare_rows.append(
            {
                "feature": f,
                "mean_TP": a.mean(),
                "mean_FP": b.mean(),
                "median_TP": a.median(),
                "median_FP": b.median(),
                "diff_mean": a.mean() - b.mean(),
                "t_stat": t_stat,
                "p_value": p_val,
            }
        )
    cmp_df = pd.DataFrame(compare_rows)
    cmp_df["abs_t"] = cmp_df["t_stat"].abs()
    cmp_df = cmp_df.sort_values("abs_t", ascending=False)

    # ────────── Recherche de règles filtres ──────────
    # Pour chaque feature numérique discriminante (|t| > 2), on essaye un cutoff
    # qui retire un max de FP en préservant le max de TP.
    filter_rows = []
    for _, r in cmp_df.iterrows():
        f = r["feature"]
        if r["abs_t"] < 2.0 or f in ("hour_ny", "dow", "is_macro_day"):
            continue
        # Sens : si mean_FP > mean_TP, on veut filtrer "feature > seuil"
        # On teste différents quantiles de la distribution des SIGNAUX
        is_fp_higher = r["mean_FP"] > r["mean_TP"]
        vals = signals[f].dropna()
        if len(vals) < 50:
            continue
        for q in (0.6, 0.7, 0.8, 0.9):
            cutoff = vals.quantile(q if is_fp_higher else 1 - q)
            if is_fp_higher:
                kept = signals[signals[f] <= cutoff]
                rule = f"{f} ≤ {cutoff:.3f}"
            else:
                kept = signals[signals[f] >= cutoff]
                rule = f"{f} ≥ {cutoff:.3f}"
            n_tp_kept = (kept["is_pivot_any"] == 1).sum()
            n_fp_kept = (kept["is_pivot_any"] == 0).sum()
            n_kept = len(kept)
            if n_kept == 0:
                continue
            prec_filtered = n_tp_kept / n_kept
            recall_kept = n_tp_kept / max(1, len(tp))
            filter_rows.append(
                {
                    "feature": f,
                    "rule": rule,
                    "quantile": q,
                    "n_signaux": n_kept,
                    "n_TP": n_tp_kept,
                    "n_FP": n_fp_kept,
                    "precision": prec_filtered,
                    "recall_pivots": recall_kept,
                    "lift_vs_baseline": prec_filtered / max(1e-6, p_at_10),
                }
            )
    filt_df = pd.DataFrame(filter_rows).sort_values("precision", ascending=False)

    # ────────── Analyse heure NY ──────────
    by_hour = signals.groupby("hour_ny").apply(
        lambda g: pd.Series(
            {
                "n_signaux": len(g),
                "n_TP": (g["is_pivot_any"] == 1).sum(),
                "precision": (g["is_pivot_any"] == 1).mean(),
            }
        )
    )

    # ────────── Plots ──────────
    # Distribution proba TP vs FP
    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    axes[0].hist(
        tp["proba"], bins=30, alpha=0.6, label=f"TP (n={len(tp)})", color="seagreen", density=True
    )
    axes[0].hist(
        fp["proba"], bins=30, alpha=0.6, label=f"FP (n={len(fp)})", color="indianred", density=True
    )
    axes[0].axvline(thr_10, color="black", linestyle="--", label=f"seuil = {thr_10:.3f}")
    axes[0].set_title(f"{ticker} — Distribution proba")
    axes[0].set_xlabel("proba pivot")
    axes[0].legend()
    axes[0].grid(alpha=0.3)

    # Top feature discriminante : distribution TP vs FP
    if len(cmp_df) > 0:
        top_feat = cmp_df.iloc[0]["feature"]
        if top_feat in tp.columns:
            axes[1].hist(
                tp[top_feat].dropna(),
                bins=30,
                alpha=0.6,
                label="TP",
                color="seagreen",
                density=True,
            )
            axes[1].hist(
                fp[top_feat].dropna(),
                bins=30,
                alpha=0.6,
                label="FP",
                color="indianred",
                density=True,
            )
            axes[1].set_title(f"{ticker} — {top_feat} (|t|={cmp_df.iloc[0]['abs_t']:.2f})")
            axes[1].set_xlabel(top_feat)
            axes[1].legend()
            axes[1].grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_dir / "fp_vs_tp.png", dpi=110)
    plt.close(fig)

    # ────────── Rapport ──────────
    lines = [f"# Diagnostic high-confidence errors — {ticker} order={ORDER}\n"]
    w = lines.append
    w(f"- Prédictions OOS totales (4 splits) : **{len(pred_df):,}**")
    w(f"- Base rate global : **{y.mean():.2%}**")
    w(f"- Seuil proba choisi (recall ≈ 10 %) : **{thr_10:.3f}**")
    w(f"- Précision à ce seuil : **{p_at_10:.2%}** (recall réel : {r_at_10:.2%})")
    w(f"- Signaux émis : **{len(signals)}** | TP : **{len(tp)}** | FP : **{len(fp)}**\n")

    w("## 1. Caractéristiques discriminantes TP vs FP\n")
    if len(cmp_df) > 0:
        cmp_show = cmp_df.head(12).copy()
        for c in ("mean_TP", "mean_FP", "median_TP", "median_FP", "diff_mean", "t_stat"):
            cmp_show[c] = cmp_show[c].map(lambda x: f"{x:+.3f}")
        cmp_show["p_value"] = cmp_show["p_value"].map(lambda x: f"{x:.1e}")
        cmp_show["abs_t"] = cmp_show["abs_t"].map(lambda x: f"{x:.2f}")
        w(
            cmp_show[
                ["feature", "mean_TP", "mean_FP", "diff_mean", "t_stat", "p_value"]
            ].to_markdown(index=False)
        )
    else:
        w("_pas assez de signaux pour analyse_\n")

    w("\n## 2. Précision par heure NY\n")
    bh = by_hour.copy()
    bh["precision"] = bh["precision"].map(lambda x: f"{x:.2%}")
    bh["n_signaux"] = bh["n_signaux"].astype(int)
    bh["n_TP"] = bh["n_TP"].astype(int)
    w(bh.to_markdown())

    w("\n## 3. Filtres candidats — règles simples qui améliorent la précision\n")
    if len(filt_df) > 0:
        ftop = filt_df.head(10).copy()
        ftop["precision"] = ftop["precision"].map(lambda x: f"{x:.2%}")
        ftop["recall_pivots"] = ftop["recall_pivots"].map(lambda x: f"{x:.2%}")
        ftop["lift_vs_baseline"] = ftop["lift_vs_baseline"].map(lambda x: f"×{x:.2f}")
        w(
            ftop[
                [
                    "rule",
                    "n_signaux",
                    "n_TP",
                    "n_FP",
                    "precision",
                    "recall_pivots",
                    "lift_vs_baseline",
                ]
            ].to_markdown(index=False)
        )
        w("")
        # Best filter
        best = filt_df.iloc[0]
        w(
            f"**Meilleur filtre** : `{best['rule']}` → précision **{best['precision']:.2%}** "
            f"(vs baseline {p_at_10:.2%}, lift ×{best['lift_vs_baseline']:.2f}), "
            f"recall pivots conservé : {best['recall_pivots']:.2%}"
        )
    else:
        w("_aucune feature discriminante au seuil |t|>2_")

    (out_dir / "rapport.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"  → {out_dir}/rapport.md")

    return {
        "ticker": ticker,
        "n_signals": len(signals),
        "precision_baseline": p_at_10,
        "best_filter": filt_df.iloc[0].to_dict() if len(filt_df) > 0 else None,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", required=True)
    args = parser.parse_args()
    diagnose(args.ticker)


if __name__ == "__main__":
    main()
