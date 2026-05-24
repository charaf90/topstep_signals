"""
Multi-TF H1 — pivots agrégés en H1 prédits avec features H1.

Setup : on agrège M5 → H1 (OHLCV), on calcule LES MÊMES 31 features sur H1
qu'auparavant sur M5, et on pose les labels argrelextrema sur Close H1.

Hypothèse à valider : pivots H1 = retournements plus structurels que pivots
M5, donc le lift devrait monter encore (au prix d'une base rate ↓ et d'un
échantillon plus petit).

Usage : python scripts/research_pivot_h1.py --ticker NQ1 --order 5
"""
from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.inspection import permutation_importance
from sklearn.metrics import average_precision_score, precision_recall_curve

warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)
np.random.seed(42)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import research_pivot_nq1 as base  # noqa: E402
from core.data import load_csv  # noqa: E402

OUT_ROOT = ROOT / "output" / "pivot_research_h1"
IS_END = pd.Timestamp("2026-02-15")

FEATURE_COLS = [
    "ema9_slope", "ema21_slope", "ema50_slope",
    "roc_5", "roc_20", "roc_50", "adx_14",
    "atr_14", "atr_ratio_short_long", "bb_width",
    "dist_close_ema21_atr", "range_atr_ratio",
    "body_range_ratio", "upper_wick_ratio", "lower_wick_ratio", "vol_rel",
    "dist_to_max20_atr", "dist_to_min20_atr",
    "past_pivot_density_2atr", "past_pivot_density_1atr",
    "ret_lag_1", "ret_lag_2", "ret_lag_3", "ret_lag_5", "ret_lag_10",
    "up_bars_last_10",
    "hour_ny", "dow", "is_macro_day",
    # On retire bars_since_open et minute_ny (peu de sens en H1)
]


def resample_to_h1(df_m5: pd.DataFrame) -> pd.DataFrame:
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    return df_m5.resample("1h").agg(agg).dropna()


def precision_at_recall(y_true, y_score, target_recall):
    p, r, _ = precision_recall_curve(y_true, y_score)
    mask = r >= target_recall
    if not mask.any():
        return float("nan")
    return float(p[mask].max())


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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--order", type=int, required=True)
    args = parser.parse_args()

    ticker, order = args.ticker, args.order
    out_dir = OUT_ROOT / ticker / f"order_{order}"
    out_dir.mkdir(parents=True, exist_ok=True)

    data_csv = ROOT / "data" / f"{ticker}_data_m5.csv"
    if not data_csv.exists():
        print(f"⚠ {data_csv} introuvable.")
        sys.exit(1)

    print(f"▸ {ticker} H1 order={order}")
    df_m5 = load_csv(str(data_csv))
    df = resample_to_h1(df_m5)
    print(f"  • {len(df):,} barres H1 ({df.index[0]} → {df.index[-1]})")

    # On monkey-patche les globals utilisés par les helpers
    base.TICKER = ticker
    base.PIVOT_ORDER = order
    base.OUT_DIR = out_dir
    base.DATA_CSV = data_csv

    df = base.label_pivots(df, order=order)
    df = base.build_features(df)
    df = df.dropna(subset=FEATURE_COLS + ["is_pivot_any"]).copy()
    print(f"  • {len(df):,} barres après dropna")

    base_rate = float(df["is_pivot_any"].mean())
    print(f"  • base rate is_pivot_any : {base_rate:.2%}")

    # Split
    df_is = df[df.index <= IS_END]
    df_oos = df[df.index > IS_END]
    print(f"  • IS : {len(df_is):,} | OOS : {len(df_oos):,}")
    if len(df_oos) < 300 or df_oos["is_pivot_any"].sum() < 20:
        print(f"⚠ OOS trop petit ({df_oos['is_pivot_any'].sum()} pivots), arrêt.")
        sys.exit(1)

    X_is = df_is[FEATURE_COLS].to_numpy(dtype=np.float32)
    y_is = df_is["is_pivot_any"].to_numpy(dtype=np.int32)
    X_oos = df_oos[FEATURE_COLS].to_numpy(dtype=np.float32)
    y_oos = df_oos["is_pivot_any"].to_numpy(dtype=np.int32)
    base_rate_oos = float(y_oos.mean())

    rf = RandomForestClassifier(
        n_estimators=500, max_depth=8, class_weight="balanced",
        random_state=42, n_jobs=-1, min_samples_leaf=10,
    )
    rf.fit(X_is, y_is)
    p_rf = rf.predict_proba(X_oos)[:, 1]
    sc_rf = score_proba(y_oos, p_rf, base_rate_oos)

    hgb = HistGradientBoostingClassifier(
        max_iter=300, learning_rate=0.05, max_depth=6,
        class_weight="balanced", random_state=42,
    )
    hgb.fit(X_is, y_is)
    p_hgb = hgb.predict_proba(X_oos)[:, 1]
    sc_hgb = score_proba(y_oos, p_hgb, base_rate_oos)

    print(f"  • rf   PR-AUC={sc_rf['pr_auc']:.3f}  "
          f"P@R5%={sc_rf['prec_at_recall_5']:.2%}  "
          f"P@R10%={sc_rf['prec_at_recall_10']:.2%}  "
          f"P@R20%={sc_rf['prec_at_recall_20']:.2%}")
    print(f"  • hgb  PR-AUC={sc_hgb['pr_auc']:.3f}  "
          f"P@R5%={sc_hgb['prec_at_recall_5']:.2%}  "
          f"P@R10%={sc_hgb['prec_at_recall_10']:.2%}  "
          f"P@R20%={sc_hgb['prec_at_recall_20']:.2%}")

    # Plot PR
    fig, ax = plt.subplots(figsize=(8, 6))
    for name, p in (("rf", p_rf), ("hgb", p_hgb)):
        prec, rec, _ = precision_recall_curve(y_oos, p)
        ap = average_precision_score(y_oos, p)
        ax.plot(rec, prec, label=f"{name} (PR-AUC={ap:.3f})", linewidth=1.5)
    ax.axhline(base_rate_oos, color="gray", linestyle="--",
               label=f"base rate OOS ({base_rate_oos:.2%})")
    ax.set_xlabel("Recall")
    ax.set_ylabel("Precision")
    ax.set_title(f"Précision-Recall H1 — {ticker} (order={order})")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, max(1.0, base_rate_oos * 5))
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_dir / "precision_recall_OOS.png", dpi=110)
    plt.close(fig)

    # Permutation importance sur RF
    try:
        pi = permutation_importance(
            rf, X_oos, y_oos, n_repeats=5, random_state=42, n_jobs=-1,
            scoring="average_precision",
        )
        imp_df = pd.DataFrame({
            "feature": FEATURE_COLS,
            "importance": pi.importances_mean,
        }).sort_values("importance", ascending=False).head(15)
        fig, ax = plt.subplots(figsize=(8, 5))
        ax.barh(imp_df["feature"][::-1], imp_df["importance"][::-1], color="steelblue")
        ax.set_xlabel("Δ PR-AUC (permutation)")
        ax.set_title(f"Top features H1 — {ticker} order={order}")
        ax.grid(alpha=0.3, axis="x")
        fig.tight_layout()
        fig.savefig(out_dir / "feature_importance.png", dpi=110)
        plt.close(fig)
    except Exception as e:
        imp_df = pd.DataFrame()
        print(f"  ⚠ permutation importance failed: {e}")

    # Rapport
    lines = [f"# H1 multi-TF — {ticker} order={order}\n"]
    w = lines.append
    w(f"- Barres H1 totales : **{len(df):,}**")
    w(f"- Base rate global is_pivot_any : **{base_rate:.2%}**")
    w(f"- IS : {len(df_is):,} barres, OOS : {len(df_oos):,} barres ({y_oos.sum()} pivots)\n")

    w("## Scores OOS\n")
    rows = []
    for k, sc in (("RF", sc_rf), ("HGB", sc_hgb)):
        rows.append({
            "modèle": k,
            "PR-AUC": f"{sc['pr_auc']:.3f}",
            "P@R5%": f"{sc['prec_at_recall_5']:.2%}",
            "P@R10%": f"{sc['prec_at_recall_10']:.2%}",
            "P@R20%": f"{sc['prec_at_recall_20']:.2%}",
        })
    w(pd.DataFrame(rows).to_markdown(index=False))
    best_p10 = max(sc_rf["prec_at_recall_10"], sc_hgb["prec_at_recall_10"])
    lift = best_p10 / base_rate_oos if base_rate_oos else 0
    w(f"\nBase rate OOS : **{base_rate_oos:.2%}** | Lift @ R10% : **×{lift:.2f}**\n")

    if not imp_df.empty:
        w("## Top features (permutation importance OOS sur RF)\n")
        imp_md = imp_df.copy()
        imp_md["importance"] = imp_md["importance"].map(lambda x: f"{x:+.4f}")
        w(imp_md.to_markdown(index=False))
    (out_dir / "rapport.md").write_text("\n".join(lines), encoding="utf-8")

    print(f"  → {out_dir}")


if __name__ == "__main__":
    main()
