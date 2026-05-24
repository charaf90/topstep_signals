"""
Validation split-by-split des combos trouvés par grid search.

Pour chaque combo de filtres, on calcule la précision et le recall sur
chacun des 4 splits walk-forward INDIVIDUELLEMENT. Si la précision est
stable d'un split à l'autre → combo robuste. Si grand spread → potentiel
overfit du grid search global.

Usage : python scripts/research_pivot_validate.py
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import precision_recall_curve

warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)
np.random.seed(42)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import research_pivot_nq1 as base  # noqa: E402
from core.data import load_csv  # noqa: E402
from research_pivot_divergence import BASELINE_FEATURES, SPLITS, OOS_HORIZON_DAYS, ORDER  # noqa: E402

OUT_DIR = ROOT / "output" / "pivot_research_combo" / "validation"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Combos à valider — issus du grid search
COMBOS = {
    "MCL1": [
        ("baseline (proba ≥ p10%)", None),
        ("hour_ny ≥ 13", [("hour_ny", "ge", 13)]),
        ("hour_ny ≥ 14", [("hour_ny", "ge", 14)]),
        ("vol_rel ≥ 1.5", [("vol_rel", "ge", 1.5)]),
        ("hour_ny ≥ 13 AND vol_rel ≥ 1.5", [("hour_ny", "ge", 13), ("vol_rel", "ge", 1.5)]),
        ("hour_ny ≥ 14 AND vol_rel ≥ 1.5", [("hour_ny", "ge", 14), ("vol_rel", "ge", 1.5)]),
    ],
    "MGC1": [
        ("baseline (proba ≥ p10%)", None),
        ("vol_rel ≥ 1.734", [("vol_rel", "ge", 1.734)]),
        ("vol_rel ≥ 2.5", [("vol_rel", "ge", 2.5)]),
        ("vol_rel ≥ 2.5 AND range_atr_ratio ≥ 2", [("vol_rel", "ge", 2.5), ("range_atr_ratio", "ge", 2.0)]),
        ("vol_rel ≥ 2.5 AND range_atr_ratio ≥ 1.5 AND hour_ny ≥ 6",
         [("vol_rel", "ge", 2.5), ("range_atr_ratio", "ge", 1.5), ("hour_ny", "ge", 6)]),
    ],
}


def apply_filters(df, filters):
    if filters is None:
        return np.ones(len(df), dtype=bool)
    mask = np.ones(len(df), dtype=bool)
    for feat, op, val in filters:
        if op == "ge":
            mask &= (df[feat] >= val).to_numpy()
        elif op == "le":
            mask &= (df[feat] <= val).to_numpy()
    return mask


def validate_ticker(ticker: str):
    print(f"\n▸ Validation {ticker}")
    data_csv = ROOT / "data" / f"{ticker}_data_m5.csv"
    df = load_csv(str(data_csv))

    base.TICKER = ticker
    base.PIVOT_ORDER = ORDER
    base.OUT_DIR = OUT_DIR
    base.DATA_CSV = data_csv

    df = base.label_pivots(df, order=ORDER)
    df = base.build_features(df)
    df = df.dropna(subset=BASELINE_FEATURES + ["is_pivot_any"]).copy()

    combos = COMBOS[ticker]
    per_combo_rows = []

    # Pour chaque split, train + predict + appliquer chaque combo
    split_data = []
    for i, is_end in enumerate(SPLITS, start=1):
        oos_end = is_end + pd.Timedelta(days=OOS_HORIZON_DAYS)
        df_is = df[df.index <= is_end]
        df_oos = df[(df.index > is_end) & (df.index <= oos_end)]
        if len(df_is) < 5000 or df_oos["is_pivot_any"].sum() < 20:
            print(f"  • split {i} : skip (trop petit)")
            continue
        X_is = df_is[BASELINE_FEATURES].to_numpy(dtype=np.float32)
        y_is = df_is["is_pivot_any"].to_numpy(dtype=np.int32)
        X_oos = df_oos[BASELINE_FEATURES].to_numpy(dtype=np.float32)
        y_oos = df_oos["is_pivot_any"].to_numpy(dtype=np.int32)
        rf = RandomForestClassifier(
            n_estimators=300, max_depth=8, class_weight="balanced",
            random_state=42, n_jobs=-1, min_samples_leaf=20,
        )
        rf.fit(X_is, y_is)
        p_oos = rf.predict_proba(X_oos)[:, 1]

        # Seuil tel que recall ≈ 10 % SUR CE SPLIT
        prec, rec, thr = precision_recall_curve(y_oos, p_oos)
        mask = rec[:-1] >= 0.10
        if not mask.any():
            continue
        valid_idx = np.where(mask)[0]
        best_idx = valid_idx[np.argmax(prec[:-1][valid_idx])]
        thr_10 = float(thr[best_idx])
        sub = df_oos[BASELINE_FEATURES + ["is_pivot_any"]].copy()
        sub["proba"] = p_oos
        signals = sub[sub["proba"] >= thr_10]
        n_pivots_total = int((sub["is_pivot_any"] == 1).sum())
        split_data.append({
            "split": i,
            "signals": signals,
            "n_pivots_total": n_pivots_total,
            "n_oos": len(df_oos),
        })

    # Pour chaque combo, calculer perf par split
    print(f"  • {len(split_data)} splits évalués")
    for label, filters in combos:
        row = {"combo": label}
        precs = []
        recalls = []
        n_signals_list = []
        for sd in split_data:
            sig = sd["signals"]
            mask = apply_filters(sig, filters)
            sel = sig[mask]
            n = len(sel)
            n_tp = int((sel["is_pivot_any"] == 1).sum())
            row[f"s{sd['split']}_n"] = n
            row[f"s{sd['split']}_prec"] = n_tp / n if n > 0 else np.nan
            row[f"s{sd['split']}_recall"] = n_tp / sd["n_pivots_total"] if sd["n_pivots_total"] > 0 else np.nan
            if n > 0:
                precs.append(n_tp / n)
                n_signals_list.append(n)
                recalls.append(n_tp / sd["n_pivots_total"])
        row["mean_prec"] = float(np.mean(precs)) if precs else np.nan
        row["std_prec"] = float(np.std(precs)) if len(precs) > 1 else np.nan
        row["min_prec"] = float(np.min(precs)) if precs else np.nan
        row["max_prec"] = float(np.max(precs)) if precs else np.nan
        row["mean_recall"] = float(np.mean(recalls)) if recalls else np.nan
        row["total_signals"] = int(sum(n_signals_list))
        per_combo_rows.append(row)

    res = pd.DataFrame(per_combo_rows)
    return res, split_data


def format_report(ticker: str, res: pd.DataFrame):
    lines = [f"# Validation split-by-split — {ticker} order={ORDER}\n"]
    w = lines.append
    w("## Précision par split\n")
    show_cols = ["combo"]
    for s in (1, 2, 3, 4):
        show_cols.extend([f"s{s}_n", f"s{s}_prec"])
    show_cols.extend(["mean_prec", "std_prec", "min_prec", "max_prec", "mean_recall"])
    show = res[show_cols].copy()
    for c in show.columns:
        if c.endswith("_prec") or c.startswith("mean_") or c.startswith("min_") or c.startswith("max_") or c == "std_prec" or c == "mean_recall":
            show[c] = show[c].map(lambda x: f"{x:.2%}" if pd.notna(x) else "—")
        if c.endswith("_n"):
            show[c] = show[c].astype(int)
    w(show.to_markdown(index=False))

    w("\n## Lecture\n")
    baseline_row = res[res["combo"].str.startswith("baseline")].iloc[0]
    w(f"- **Baseline** : précision moyenne {baseline_row['mean_prec']:.2%}, std {baseline_row['std_prec']:.2%}")
    w(f"- Mesure de stabilité : combo solide si `std_prec` ≤ 5 % et `min_prec` ≥ baseline_mean.")
    w(f"- Combo suspect d'overfit si `max_prec - min_prec` > 15 pp ou `min_prec` < baseline_mean.")
    return "\n".join(lines)


def main():
    all_results = {}
    for ticker in ("MCL1", "MGC1"):
        res, _ = validate_ticker(ticker)
        report = format_report(ticker, res)
        (OUT_DIR / f"{ticker}_validation.md").write_text(report, encoding="utf-8")
        all_results[ticker] = res
        # Print summary
        print(f"\n=== {ticker} ===")
        for _, r in res.iterrows():
            print(f"  {r['combo'][:50]:50s}  mean={r['mean_prec']:.2%}  "
                  f"std={r['std_prec']:.2%}  min={r['min_prec']:.2%}  "
                  f"max={r['max_prec']:.2%}  recall={r['mean_recall']:.2%}")

    # Plot
    fig, axes = plt.subplots(1, 2, figsize=(14, 6))
    for ax, ticker in zip(axes, ("MCL1", "MGC1")):
        res = all_results[ticker]
        n_combos = len(res)
        x = np.arange(n_combos)
        means = res["mean_prec"].values
        stds = res["std_prec"].values
        mins = res["min_prec"].values
        maxs = res["max_prec"].values
        ax.errorbar(x, means, yerr=stds, fmt="o", color="steelblue",
                    capsize=4, markersize=8, label="moyenne ± std")
        ax.scatter(x, mins, marker="v", color="firebrick", label="min")
        ax.scatter(x, maxs, marker="^", color="seagreen", label="max")
        ax.set_xticks(x)
        ax.set_xticklabels([r["combo"][:30] for _, r in res.iterrows()],
                           rotation=30, ha="right", fontsize=8)
        ax.set_ylabel("Précision OOS")
        ax.set_title(f"{ticker} — précision par split (4 splits WF)")
        ax.legend(); ax.grid(alpha=0.3, axis="y")
    fig.tight_layout()
    fig.savefig(OUT_DIR / "validation_split_by_split.png", dpi=110)
    plt.close(fig)
    print(f"\n→ {OUT_DIR}")


if __name__ == "__main__":
    main()
