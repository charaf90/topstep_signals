"""
V2 : divergences continues normalisées + features volume orthogonales.

Différences vs v1 (research_pivot_divergence.py) :
- Divergences en **z-score continu** au lieu de binaire {-1, 0, 1}
- Ajout OBV et MFI (vrai volume-based, orthogonal au prix)
- Restriction aux oscillateurs les moins redondants : RSI, OBV, MFI
- Plus parcimonieux : ~10 nouvelles features au lieu de 24

Usage : python scripts/research_pivot_divergence_v2.py --ticker MCL1
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
# On réutilise rolling_slope et rsi du script v1
from research_pivot_divergence import (  # noqa: E402
    rolling_slope, rsi, BASELINE_FEATURES, SPLITS, OOS_HORIZON_DAYS, ORDER,
    score_proba, train_set, wf_run,
)

OUT_ROOT = ROOT / "output" / "pivot_research_div_v2"
ZSCORE_WIN = 200  # fenêtre pour z-score des pentes


# ────────────────────────────────────────────────────────────────────────────
# Nouveaux indicateurs
# ────────────────────────────────────────────────────────────────────────────
def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    """On-Balance Volume cumulé."""
    sign = np.sign(close.diff()).fillna(0).astype(int)
    return (sign * volume).cumsum()


def mfi(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Money Flow Index — RSI pondéré par volume."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    rmf = tp * df["volume"]
    tp_diff = tp.diff()
    pos_mf = rmf.where(tp_diff > 0, 0.0)
    neg_mf = rmf.where(tp_diff < 0, 0.0)
    pos_sum = pos_mf.rolling(n).sum()
    neg_sum = neg_mf.rolling(n).sum().replace(0, np.nan)
    mfr = pos_sum / neg_sum
    return 100 - 100 / (1 + mfr)


def zscore(s: pd.Series, w: int) -> pd.Series:
    """Z-score rolling causal."""
    mu = s.rolling(w).mean()
    sd = s.rolling(w).std()
    return (s - mu) / sd.replace(0, np.nan)


# ────────────────────────────────────────────────────────────────────────────
# Feature engineering v2
# ────────────────────────────────────────────────────────────────────────────
SLOPE_WINDOWS = (10, 20)
DIV_OSCILLATORS = ("rsi14", "obv", "mfi14")


def add_v2_features(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    out = df.copy()
    new_cols = []

    # Indicateurs
    out["rsi14"] = rsi(out["close"], 14)
    out["obv"] = obv(out["close"], out["volume"])
    out["mfi14"] = mfi(out, 14)

    # Pentes prix (sur les 2 fenêtres seulement)
    for w in SLOPE_WINDOWS:
        col = f"slope_price_{w}"
        out[col] = rolling_slope(out["close"], w)

    # Pentes oscillateurs (sur les 2 fenêtres)
    for osc in DIV_OSCILLATORS:
        for w in SLOPE_WINDOWS:
            col = f"slope_{osc}_{w}"
            out[col] = rolling_slope(out[osc], w)

    # Volume-based niveaux (orthogonal au prix)
    new_cols.append("mfi14")
    for w in SLOPE_WINDOWS:
        c = f"slope_obv_{w}"
        new_cols.append(c)
    new_cols.append("slope_mfi14_10")  # seulement 1 fenêtre pour MFI slope

    # Divergences continues normalisées en z-score
    # z(slope_price) - z(slope_osc) sur les 2 fenêtres
    for w in SLOPE_WINDOWS:
        z_price = zscore(out[f"slope_price_{w}"], ZSCORE_WIN)
        for osc in DIV_OSCILLATORS:
            z_osc = zscore(out[f"slope_{osc}_{w}"], ZSCORE_WIN)
            col = f"div_cont_{osc}_{w}"
            out[col] = z_price - z_osc
            new_cols.append(col)

    return out, new_cols


# ────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", required=True)
    args = parser.parse_args()
    ticker = args.ticker

    print(f"▸ V2 {ticker} order={ORDER} | baseline vs enrichi v2 (volume + div continues)")
    data_csv = ROOT / "data" / f"{ticker}_data_m5.csv"
    out_dir = OUT_ROOT / ticker
    out_dir.mkdir(parents=True, exist_ok=True)

    df = load_csv(str(data_csv))
    base.TICKER = ticker
    base.PIVOT_ORDER = ORDER
    base.OUT_DIR = out_dir
    base.DATA_CSV = data_csv

    df = base.label_pivots(df, order=ORDER)
    df = base.build_features(df)
    df, new_cols = add_v2_features(df)
    enriched = BASELINE_FEATURES + new_cols
    print(f"  • baseline : {len(BASELINE_FEATURES)} | nouvelles : {len(new_cols)} | total : {len(enriched)}")
    print(f"  • nouvelles features : {new_cols}")

    df_clean = df.dropna(subset=enriched + ["is_pivot_any"]).copy()
    print(f"  • {len(df_clean):,} barres après dropna (warm-up ≈ 200 pour z-score)")

    print("\n=== BASELINE ===")
    sum_base = wf_run(df_clean, BASELINE_FEATURES)
    for mk in ("rf", "hgb"):
        print(f"  • {mk}  PR-AUC={sum_base[f'{mk}_pr_auc_mean']:.3f}±{sum_base[f'{mk}_pr_auc_std']:.3f}  "
              f"P@R10%={sum_base[f'{mk}_p@r10_mean']:.2%}±{sum_base[f'{mk}_p@r10_std']:.2%}")

    print("\n=== ENRICHI V2 ===")
    sum_enr = wf_run(df_clean, enriched)
    for mk in ("rf", "hgb"):
        print(f"  • {mk}  PR-AUC={sum_enr[f'{mk}_pr_auc_mean']:.3f}±{sum_enr[f'{mk}_pr_auc_std']:.3f}  "
              f"P@R10%={sum_enr[f'{mk}_p@r10_mean']:.2%}±{sum_enr[f'{mk}_p@r10_std']:.2%}")

    # Permutation importance
    print("\n▸ Permutation importance (enrichi, dernier split)…")
    pi = permutation_importance(
        sum_enr["last_model"], sum_enr["last_X_oos"], sum_enr["last_y_oos"],
        n_repeats=5, random_state=42, n_jobs=-1, scoring="average_precision",
    )
    imp = pd.DataFrame({
        "feature": enriched, "importance": pi.importances_mean,
        "is_new": [f in new_cols for f in enriched],
    }).sort_values("importance", ascending=False)

    # Rapport
    lines = [f"# Divergences V2 (continues + volume) — {ticker} order={ORDER}\n"]
    w = lines.append
    w(f"- Nouvelles features : **{len(new_cols)}** (vs 24 en v1)")
    w(f"  - Volume-based : OBV slope 10/20, MFI(14), slope MFI(14)/10 → 4")
    w(f"  - Divergences continues prix-(rsi/obv/mfi) sur w=10/20 → 6")
    w(f"- Z-score fenêtre : {ZSCORE_WIN} barres\n")

    w("## Baseline vs Enrichi V2\n")
    rows = []
    for mk, lbl in (("rf", "RF"), ("hgb", "HGB")):
        rows.append({
            "modèle": lbl,
            "PR-AUC base": f"{sum_base[f'{mk}_pr_auc_mean']:.3f} ± {sum_base[f'{mk}_pr_auc_std']:.3f}",
            "PR-AUC v2": f"{sum_enr[f'{mk}_pr_auc_mean']:.3f} ± {sum_enr[f'{mk}_pr_auc_std']:.3f}",
            "Δ PR-AUC": f"{sum_enr[f'{mk}_pr_auc_mean'] - sum_base[f'{mk}_pr_auc_mean']:+.3f}",
            "P@R10 base": f"{sum_base[f'{mk}_p@r10_mean']:.2%}",
            "P@R10 v2": f"{sum_enr[f'{mk}_p@r10_mean']:.2%}",
            "Δ P@R10": f"{sum_enr[f'{mk}_p@r10_mean'] - sum_base[f'{mk}_p@r10_mean']:+.2%}",
        })
    w(pd.DataFrame(rows).to_markdown(index=False))

    w("\n## Top 15 features global (enrichi)\n")
    top = imp.head(15).copy()
    top["importance"] = top["importance"].map(lambda x: f"{x:+.4f}")
    top["is_new"] = top["is_new"].map(lambda b: "🆕" if b else "")
    w(top.to_markdown(index=False))

    w("\n## Nouvelles features V2 (toutes)\n")
    new_only = imp[imp["is_new"]].copy().reset_index(drop=True)
    new_only["importance"] = new_only["importance"].map(lambda x: f"{x:+.4f}")
    w(new_only[["feature", "importance"]].to_markdown(index=False))

    (out_dir / "rapport_v2.md").write_text("\n".join(lines), encoding="utf-8")

    # Plot
    fig, ax = plt.subplots(figsize=(10, 6))
    metrics = ["pr_auc", "p@r5", "p@r10", "p@r20"]
    x = np.arange(len(metrics))
    width = 0.35
    base_vals = [sum_base[f"rf_{m}_mean"] for m in metrics]
    enr_vals = [sum_enr[f"rf_{m}_mean"] for m in metrics]
    ax.bar(x - width / 2, base_vals, width, label="baseline (31)", color="steelblue")
    ax.bar(x + width / 2, enr_vals, width, label=f"v2 (+{len(new_cols)})", color="seagreen")
    ax.set_xticks(x); ax.set_xticklabels(["PR-AUC", "P@R5%", "P@R10%", "P@R20%"])
    ax.set_title(f"{ticker} V2 — Baseline vs Enrichi (RF, WF mean)")
    ax.legend(); ax.grid(alpha=0.3, axis="y")
    fig.tight_layout()
    fig.savefig(out_dir / "baseline_vs_v2.png", dpi=110)
    plt.close(fig)

    print(f"\n→ {out_dir}/rapport_v2.md")


if __name__ == "__main__":
    main()
