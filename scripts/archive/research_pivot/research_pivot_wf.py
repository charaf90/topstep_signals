"""
Walk-forward multi-fenêtre pour la recherche pivots ML.

Mêmes labels + features que research_pivot_nq1.py. Évalue la stabilité
temporelle du lift en exécutant 4 splits expanding window de ~2 mois OOS
chacun, puis rapporte moyenne ± std des métriques par (ticker, order).

Usage : python scripts/research_pivot_wf.py --ticker NQ1 --order 10
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
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.inspection import permutation_importance
from sklearn.metrics import average_precision_score, precision_recall_curve

warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)
np.random.seed(42)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

# On réutilise label_pivots / build_features / leak_check / FEATURE_COLS
# en monkey-patchant les globals nécessaires.
import research_pivot_nq1 as base  # noqa: E402

from core.data import load_csv  # noqa: E402

OUT_ROOT = ROOT / "output" / "pivot_research_wf"

# Splits expanding window. Bornes choisies pour ~2 mois OOS chacun sur
# 2025-10 → 2026-05.
SPLITS = [
    pd.Timestamp("2025-12-31"),
    pd.Timestamp("2026-01-31"),
    pd.Timestamp("2026-02-28"),
    pd.Timestamp("2026-03-31"),
]
OOS_HORIZON_DAYS = 60  # ~2 mois

FEATURE_COLS = [
    "ema9_slope",
    "ema21_slope",
    "ema50_slope",
    "roc_5",
    "roc_20",
    "roc_50",
    "adx_14",
    "atr_14",
    "atr_ratio_short_long",
    "bb_width",
    "dist_close_ema21_atr",
    "range_atr_ratio",
    "body_range_ratio",
    "upper_wick_ratio",
    "lower_wick_ratio",
    "vol_rel",
    "dist_to_max20_atr",
    "dist_to_min20_atr",
    "past_pivot_density_2atr",
    "past_pivot_density_1atr",
    "ret_lag_1",
    "ret_lag_2",
    "ret_lag_3",
    "ret_lag_5",
    "ret_lag_10",
    "up_bars_last_10",
    "hour_ny",
    "minute_ny",
    "dow",
    "bars_since_open",
    "is_macro_day",
]


def precision_at_recall(y_true, y_score, target_recall):
    p, r, _ = precision_recall_curve(y_true, y_score)
    mask = r >= target_recall
    if not mask.any():
        return float("nan")
    return float(p[mask].max())


def train_one(X_is, y_is, X_oos, y_oos, model_name):
    if model_name == "random_forest":
        m = RandomForestClassifier(
            n_estimators=300,
            max_depth=8,
            class_weight="balanced",
            random_state=42,
            n_jobs=-1,
            min_samples_leaf=20,
        )
    else:
        m = HistGradientBoostingClassifier(
            max_iter=300,
            learning_rate=0.05,
            max_depth=6,
            class_weight="balanced",
            random_state=42,
        )
    m.fit(X_is, y_is)
    p_oos = m.predict_proba(X_oos)[:, 1]
    return m, p_oos


def score_proba(y_true, p_score, base_rate):
    return {
        "pr_auc": float(average_precision_score(y_true, p_score)),
        "prec_at_recall_5": precision_at_recall(y_true, p_score, 0.05),
        "prec_at_recall_10": precision_at_recall(y_true, p_score, 0.10),
        "prec_at_recall_20": precision_at_recall(y_true, p_score, 0.20),
        "base_rate": base_rate,
        "n_oos": int(len(y_true)),
        "n_pos_oos": int(y_true.sum()),
    }


def run_one_cell(ticker: str, order: int) -> dict:
    """Exécute le walk-forward pour une cellule (ticker, order). Retourne un
    dict structuré pour agrégation et un rapport markdown ad-hoc.
    """
    out_dir = OUT_ROOT / ticker / f"order_{order}"
    out_dir.mkdir(parents=True, exist_ok=True)
    data_csv = ROOT / "data" / f"{ticker}_data_m5.csv"
    if not data_csv.exists():
        return {"error": f"data not found: {data_csv}"}

    # Monkey-patch globals pour réutiliser les helpers de research_pivot_nq1
    base.TICKER = ticker
    base.PIVOT_ORDER = order
    base.OUT_DIR = out_dir
    base.DATA_CSV = data_csv

    df = load_csv(str(data_csv))
    df = base.label_pivots(df, order=order)
    df = base.build_features(df)
    df = df.dropna(subset=FEATURE_COLS + ["is_pivot_any"]).copy()

    # Walk-forward
    per_split = []
    last_split_X_oos = None
    last_split_y_oos = None
    last_model = None
    for i, is_end in enumerate(SPLITS, start=1):
        oos_end = is_end + pd.Timedelta(days=OOS_HORIZON_DAYS)
        df_is = df[df.index <= is_end]
        df_oos = df[(df.index > is_end) & (df.index <= oos_end)]
        if len(df_is) < 5000 or len(df_oos) < 500:
            per_split.append({"split": i, "is_end": str(is_end.date()), "skipped": True})
            continue
        X_is = df_is[FEATURE_COLS].to_numpy(dtype=np.float32)
        y_is = df_is["is_pivot_any"].to_numpy(dtype=np.int32)
        X_oos = df_oos[FEATURE_COLS].to_numpy(dtype=np.float32)
        y_oos = df_oos["is_pivot_any"].to_numpy(dtype=np.int32)
        if y_is.sum() < 50 or y_oos.sum() < 20:
            per_split.append({"split": i, "is_end": str(is_end.date()), "skipped": True})
            continue
        base_rate_oos = float(y_oos.mean())

        # On garde RF (plus stable pour permutation importance)
        rf, p_rf = train_one(X_is, y_is, X_oos, y_oos, "random_forest")
        sc_rf = score_proba(y_oos, p_rf, base_rate_oos)
        # HGB (rapide et souvent meilleur)
        hgb, p_hgb = train_one(X_is, y_is, X_oos, y_oos, "hist_gbm")
        sc_hgb = score_proba(y_oos, p_hgb, base_rate_oos)

        per_split.append(
            {
                "split": i,
                "is_end": str(is_end.date()),
                "oos_end": str(min(oos_end, df.index[-1]).date()),
                "n_is": int(len(df_is)),
                "n_oos": int(len(df_oos)),
                "base_rate": base_rate_oos,
                "rf": sc_rf,
                "hgb": sc_hgb,
            }
        )
        last_split_X_oos = X_oos
        last_split_y_oos = y_oos
        last_model = rf

    # Agrégation
    used = [s for s in per_split if "rf" in s]
    if not used:
        return {"ticker": ticker, "order": order, "splits": per_split, "error": "no usable split"}

    def agg(metric, model_key):
        vals = np.array([s[model_key][metric] for s in used])
        return float(vals.mean()), float(vals.std())

    summary = {
        "ticker": ticker,
        "order": order,
        "n_splits": len(used),
        "base_rate_mean": float(np.mean([s["base_rate"] for s in used])),
        "n_oos_mean": float(np.mean([s["n_oos"] for s in used])),
    }
    for model_key in ("rf", "hgb"):
        for metric in ("pr_auc", "prec_at_recall_5", "prec_at_recall_10", "prec_at_recall_20"):
            mu, sd = agg(metric, model_key)
            summary[f"{model_key}_{metric}_mean"] = mu
            summary[f"{model_key}_{metric}_std"] = sd

    # Permutation importance sur le dernier split (modèle RF)
    if last_model is not None and last_split_X_oos is not None and len(last_split_y_oos) > 200:
        try:
            pi = permutation_importance(
                last_model,
                last_split_X_oos,
                last_split_y_oos,
                n_repeats=3,
                random_state=42,
                n_jobs=-1,
                scoring="average_precision",
            )
            imp = pd.DataFrame(
                {
                    "feature": FEATURE_COLS,
                    "importance": pi.importances_mean,
                }
            ).sort_values("importance", ascending=False)
            summary["top_features"] = imp.head(10).to_dict(orient="records")
        except Exception as e:
            summary["top_features_error"] = str(e)

    # Rapport per-cell
    lines = [f"# Walk-forward — {ticker} order={order}\n"]
    w = lines.append
    w(f"- Splits utilisés : **{len(used)}/{len(SPLITS)}**")
    w(f"- Base rate OOS moyen : **{summary['base_rate_mean']:.2%}**")
    w(f"- n_oos moyen : **{summary['n_oos_mean']:.0f}**\n")

    w("## Métriques par split\n")
    rows = []
    for s in used:
        rows.append(
            {
                "split": s["split"],
                "is_end": s["is_end"],
                "oos_end": s["oos_end"],
                "n_oos": s["n_oos"],
                "base_rate": f"{s['base_rate']:.2%}",
                "rf_pr_auc": f"{s['rf']['pr_auc']:.3f}",
                "rf_p@r10": f"{s['rf']['prec_at_recall_10']:.2%}",
                "hgb_pr_auc": f"{s['hgb']['pr_auc']:.3f}",
                "hgb_p@r10": f"{s['hgb']['prec_at_recall_10']:.2%}",
            }
        )
    w(pd.DataFrame(rows).to_markdown(index=False))
    w("")

    w("## Synthèse walk-forward (moyenne ± std)\n")
    agg_rows = []
    for model_key, lbl in (("rf", "RF"), ("hgb", "HGB")):
        agg_rows.append(
            {
                "modèle": lbl,
                "PR-AUC": f"{summary[f'{model_key}_pr_auc_mean']:.3f} ± {summary[f'{model_key}_pr_auc_std']:.3f}",
                "P@R5%": f"{summary[f'{model_key}_prec_at_recall_5_mean']:.2%} ± {summary[f'{model_key}_prec_at_recall_5_std']:.2%}",
                "P@R10%": f"{summary[f'{model_key}_prec_at_recall_10_mean']:.2%} ± {summary[f'{model_key}_prec_at_recall_10_std']:.2%}",
                "P@R20%": f"{summary[f'{model_key}_prec_at_recall_20_mean']:.2%} ± {summary[f'{model_key}_prec_at_recall_20_std']:.2%}",
            }
        )
    w(pd.DataFrame(agg_rows).to_markdown(index=False))
    w("")

    if "top_features" in summary:
        w("## Top features (permutation importance — dernier split)\n")
        imp_df = pd.DataFrame(summary["top_features"])
        imp_df["importance"] = imp_df["importance"].map(lambda x: f"{x:+.4f}")
        w(imp_df.to_markdown(index=False))

    (out_dir / "rapport_wf.md").write_text("\n".join(lines), encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--order", type=int, required=True)
    args = parser.parse_args()

    print(f"▸ WF {args.ticker} order={args.order}")
    summary = run_one_cell(args.ticker, args.order)
    if "error" in summary:
        print(f"  ⚠ {summary['error']}")
        return
    print(f"  • splits utilisés : {summary['n_splits']}/{len(SPLITS)}")
    print(f"  • base rate mean : {summary['base_rate_mean']:.2%}")
    for k in ("rf", "hgb"):
        print(
            f"  • {k:4s}  PR-AUC = {summary[f'{k}_pr_auc_mean']:.3f} ± {summary[f'{k}_pr_auc_std']:.3f}  "
            f"P@R10% = {summary[f'{k}_prec_at_recall_10_mean']:.2%} ± "
            f"{summary[f'{k}_prec_at_recall_10_std']:.2%}"
        )


if __name__ == "__main__":
    main()
