"""
Grid search de combinaisons de filtres simples sur les signaux baseline.

Reprend le pipeline WF baseline du diagnostic, puis teste systématiquement
des combos de 2-3 filtres atomiques (seuils sur les top features
discriminantes) pour trouver la meilleure précision avec recall ≥ 50%.

Usage : python scripts/research_pivot_combo.py --ticker MCL1
"""
from __future__ import annotations

import argparse
import itertools
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

OUT_ROOT = ROOT / "output" / "pivot_research_combo"
MIN_RECALL = 0.30  # on garde les combos avec recall conservé ≥ 30 %
MIN_SIGNALS = 80   # minimum d'échantillons pour que la précision ait du sens

# Atomic filters par actif — on définit des seuils candidats raisonnables
ATOMIC_FILTERS = {
    "MCL1": {
        "hour_ny": [("ge", h) for h in (12, 13, 14, 15)],
        "vol_rel": [("ge", v) for v in (1.5, 2.0, 2.5, 3.0)],
        "range_atr_ratio": [("ge", r) for r in (1.5, 2.0, 2.5)],
        "ret_lag_1": [("le", v) for v in (-0.002, -0.004, -0.006)],
    },
    "MGC1": {
        "vol_rel": [("ge", v) for v in (1.5, 1.7, 2.0, 2.5, 3.0)],
        "range_atr_ratio": [("ge", r) for r in (1.5, 1.8, 2.0, 2.5)],
        "hour_ny": [("ge", h) for h in (6, 8, 10, 12)],
        "dist_to_min20_atr": [("ge", v) for v in (0.0, 0.5, 1.0)],
    },
}


def compute_oos_predictions(df, feature_cols, label_col="is_pivot_any"):
    rows = []
    for is_end in SPLITS:
        oos_end = is_end + pd.Timedelta(days=OOS_HORIZON_DAYS)
        df_is = df[df.index <= is_end]
        df_oos = df[(df.index > is_end) & (df.index <= oos_end)]
        if len(df_is) < 5000 or df_oos[label_col].sum() < 20:
            continue
        X_is = df_is[feature_cols].to_numpy(dtype=np.float32)
        y_is = df_is[label_col].to_numpy(dtype=np.int32)
        X_oos = df_oos[feature_cols].to_numpy(dtype=np.float32)
        rf = RandomForestClassifier(
            n_estimators=300, max_depth=8, class_weight="balanced",
            random_state=42, n_jobs=-1, min_samples_leaf=20,
        )
        rf.fit(X_is, y_is)
        p_oos = rf.predict_proba(X_oos)[:, 1]
        sub = df_oos[feature_cols + [label_col]].copy()
        sub["proba"] = p_oos
        rows.append(sub)
    return pd.concat(rows).sort_index()


def apply_atomic(df, feat, op, val):
    if op == "ge":
        return df[feat] >= val
    elif op == "le":
        return df[feat] <= val
    else:
        raise ValueError(op)


def fmt_atomic(feat, op, val):
    sym = "≥" if op == "ge" else "≤"
    return f"{feat} {sym} {val:g}"


def score_filter(signals, mask):
    sel = signals[mask]
    n = len(sel)
    if n == 0:
        return None
    n_tp = int((sel["is_pivot_any"] == 1).sum())
    n_fp = int((sel["is_pivot_any"] == 0).sum())
    return {
        "n_signaux": n,
        "n_TP": n_tp,
        "n_FP": n_fp,
        "precision": n_tp / n,
    }


def search_combos(signals, atomics):
    """Test toutes les combos 1, 2 et 3 atomic filters.

    atomics : dict feature → liste de (op, val).
    Retourne un DataFrame triée par précision.
    """
    # Liste plate d'atomic filters
    all_atomics = []
    for feat, conds in atomics.items():
        for op, val in conds:
            all_atomics.append((feat, op, val))

    n_pivots_total = int((signals["is_pivot_any"] == 1).sum())
    if n_pivots_total == 0:
        return pd.DataFrame()

    rows = []
    # k = 1, 2, 3
    for k in (1, 2, 3):
        for combo in itertools.combinations(all_atomics, k):
            # Pour éviter de combiner 2 conditions sur la même feature
            feats = [c[0] for c in combo]
            if len(set(feats)) < len(feats):
                continue
            mask = np.ones(len(signals), dtype=bool)
            for feat, op, val in combo:
                mask &= apply_atomic(signals, feat, op, val).to_numpy()
            sc = score_filter(signals, mask)
            if sc is None or sc["n_signaux"] < MIN_SIGNALS:
                continue
            recall_kept = sc["n_TP"] / n_pivots_total
            if recall_kept < MIN_RECALL:
                continue
            rows.append({
                "k": k,
                "rule": " AND ".join(fmt_atomic(*c) for c in combo),
                "n_signaux": sc["n_signaux"],
                "n_TP": sc["n_TP"],
                "n_FP": sc["n_FP"],
                "precision": sc["precision"],
                "recall_kept": recall_kept,
            })
    return pd.DataFrame(rows).sort_values("precision", ascending=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", required=True)
    args = parser.parse_args()
    ticker = args.ticker

    out_dir = OUT_ROOT / ticker
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"▸ Combo {ticker} order={ORDER}")
    data_csv = ROOT / "data" / f"{ticker}_data_m5.csv"
    df = load_csv(str(data_csv))

    base.TICKER = ticker
    base.PIVOT_ORDER = ORDER
    base.OUT_DIR = out_dir
    base.DATA_CSV = data_csv

    df = base.label_pivots(df, order=ORDER)
    df = base.build_features(df)
    df = df.dropna(subset=BASELINE_FEATURES + ["is_pivot_any"]).copy()

    print("  • Train + predict WF (4 splits, RF)…")
    pred = compute_oos_predictions(df, BASELINE_FEATURES)
    y = pred["is_pivot_any"].to_numpy()
    p = pred["proba"].to_numpy()
    base_rate = float(y.mean())

    # Seuil tel que recall ≈ 10 %
    prec, rec, thr = precision_recall_curve(y, p)
    mask = rec[:-1] >= 0.10
    if not mask.any():
        print("⚠ recall 10% impossible")
        return
    valid_idx = np.where(mask)[0]
    best_idx = valid_idx[np.argmax(prec[:-1][valid_idx])]
    thr_10 = float(thr[best_idx])
    prec_baseline = float(prec[best_idx])
    recall_baseline = float(rec[best_idx])
    print(f"  • Seuil RF : {thr_10:.3f} → précision baseline = {prec_baseline:.2%} "
          f"(recall {recall_baseline:.2%})")

    signals = pred[pred["proba"] >= thr_10].copy()
    n_pivots = int((signals["is_pivot_any"] == 1).sum())
    print(f"  • Signaux à filtrer : {len(signals)} (TP : {n_pivots} | FP : {len(signals) - n_pivots})")

    print("  • Grid search combos (k=1,2,3)…")
    atomics = ATOMIC_FILTERS[ticker]
    combos = search_combos(signals, atomics)
    print(f"  • {len(combos)} combos valides (recall≥{MIN_RECALL:.0%}, n≥{MIN_SIGNALS})")

    # Rapport
    lines = [f"# Grid search combos — {ticker} order={ORDER}\n"]
    w = lines.append
    w(f"- Signaux baseline (seuil RF {thr_10:.3f}) : **{len(signals):,}**")
    w(f"- Précision baseline : **{prec_baseline:.2%}**  (lift ×{prec_baseline/base_rate:.2f})")
    w(f"- Contraintes : recall conservé ≥ {MIN_RECALL:.0%}, n_signaux ≥ {MIN_SIGNALS}")
    w(f"- Atomic filters testés :")
    for feat, conds in atomics.items():
        for op, val in conds:
            w(f"  - {fmt_atomic(feat, op, val)}")
    w("")

    w("## Top 15 combos\n")
    if len(combos) > 0:
        top = combos.head(15).copy()
        top["precision"] = top["precision"].map(lambda x: f"{x:.2%}")
        top["recall_kept"] = top["recall_kept"].map(lambda x: f"{x:.2%}")
        top["lift_vs_base"] = combos.head(15).apply(
            lambda r: f"×{r['precision']/prec_baseline:.2f}", axis=1)
        w(top[["k", "rule", "n_signaux", "n_TP", "precision",
               "recall_kept", "lift_vs_base"]].to_markdown(index=False))
    else:
        w("_aucun combo ne respecte les contraintes_")
    w("")

    # Frontière de Pareto (precision vs recall)
    if len(combos) > 0:
        # Garde uniquement les points sur la frontière (non dominés)
        pareto = []
        sorted_combos = combos.sort_values("recall_kept", ascending=False)
        best_prec_so_far = 0
        for _, r in sorted_combos.iterrows():
            if r["precision"] > best_prec_so_far:
                pareto.append(r)
                best_prec_so_far = r["precision"]
        if pareto:
            par = pd.DataFrame(pareto)
            w(f"\n## Frontière de Pareto (precision vs recall conservé)\n")
            par_show = par.copy()
            par_show["precision"] = par_show["precision"].map(lambda x: f"{x:.2%}")
            par_show["recall_kept"] = par_show["recall_kept"].map(lambda x: f"{x:.2%}")
            w(par_show[["k", "rule", "n_signaux", "precision", "recall_kept"]].to_markdown(index=False))

    # Plot Pareto
    if len(combos) > 0:
        fig, ax = plt.subplots(figsize=(8, 6))
        for k_val, sub in combos.groupby("k"):
            ax.scatter(sub["recall_kept"], sub["precision"],
                       label=f"{k_val} filtre(s)", alpha=0.6, s=40)
        ax.axhline(prec_baseline, color="gray", linestyle="--",
                   label=f"baseline ({prec_baseline:.2%})")
        ax.set_xlabel("Recall conservé (vs baseline)")
        ax.set_ylabel("Précision")
        ax.set_title(f"{ticker} — combos de filtres (k=1,2,3)")
        ax.legend()
        ax.grid(alpha=0.3)
        fig.tight_layout()
        fig.savefig(out_dir / "combos.png", dpi=110)
        plt.close(fig)

    (out_dir / "rapport.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"→ {out_dir}/rapport.md")

    if len(combos) > 0:
        best = combos.iloc[0]
        print(f"\n🏆 Meilleur combo : {best['rule']}")
        print(f"   → précision {best['precision']:.2%} (×{best['precision']/prec_baseline:.2f}), "
              f"recall {best['recall_kept']:.2%}, n={best['n_signaux']}")


if __name__ == "__main__":
    main()
