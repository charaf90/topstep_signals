"""
Phase 3b — Data science fib-v4 : sklearn + permutation tests.

Charge `output/fib_v4_trades_all.csv` (issu de explore_fib_v4_features.py)
et applique la méthodologie OPR v5.1 :
  • Corrélations Pearson + Spearman feature ↔ PnL et feature ↔ win
  • Grid search univarié : meilleur seuil par feature
  • Modèles sklearn : DecisionTree depth=2, RandomForest, LogisticRegression,
    Permutation importance
  • Permutation tests (10 000 itérations) : significativité statistique
  • Identification du filtre dominant par cellule

Sortie :
  • output/data_science_fib_v4.md  — rapport synthétique
  • output/fib_v4_feature_importance.png  — barplot importance moyenne
  • output/fib_v4_decile_pf.png  — déciles PF des 3 features dominantes

Usage :
  python scripts/data_science_fib_v4.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.stats import pearsonr, spearmanr
from sklearn.ensemble import RandomForestClassifier
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.tree import DecisionTreeClassifier, export_text

OUTPUT_DIR = PROJECT_ROOT / "output"
RNG = np.random.default_rng(42)

FEATURES_NUM = [
    "bars_to_fill",
    "pivot_break_atr",
    "mae_pending_atr",
    "wick_through_atr",
    "dist_to_ema_fast_atr",
    "bar_color_streak_pre",
    "volume_at_arm_norm",
    "bars_since_confirm",
    "adx_at_arm",
    "adx_slope_3",
    "ema_stack_atr",
    "price_extension_atr",
    "impulse_velocity_atr",
    "impulse_size_atr",
    "recent_vol_atr",
    "session_hour_utc",
]

N_PERMUTATIONS = 10_000


def pf(arr_pnl: np.ndarray) -> float:
    gp = arr_pnl[arr_pnl > 0].sum()
    gl = -arr_pnl[arr_pnl < 0].sum()
    if gl <= 0:
        return float("inf") if gp > 0 else float("nan")
    return float(gp / gl)


def best_threshold_univariate(df: pd.DataFrame, feat: str, n_grid: int = 21) -> dict:
    """Recherche le meilleur seuil 1-D : direction 'gt' ou 'lt', basé sur PF."""
    s = df[feat].dropna()
    if len(s) < 50:
        return {}
    qs = np.linspace(0.1, 0.9, n_grid)
    candidates = np.quantile(s, qs)
    best = {
        "feature": feat,
        "direction": None,
        "threshold": None,
        "pf_after": float("nan"),
        "n_after": 0,
        "pf_before": pf(df["pnl"].to_numpy()),
    }
    base_pf = best["pf_before"]
    for thr in candidates:
        for direction in ("gt", "lt"):
            if direction == "gt":
                mask = df[feat] > thr
            else:
                mask = df[feat] < thr
            if mask.sum() < 30:
                continue
            kept = df.loc[mask, "pnl"].to_numpy()
            cur_pf = pf(kept)
            if pd.isna(best["pf_after"]) or cur_pf > best["pf_after"]:
                best.update(
                    direction=direction,
                    threshold=float(thr),
                    pf_after=cur_pf,
                    n_after=int(mask.sum()),
                    delta_pf=cur_pf - base_pf,
                )
    return best


def permutation_test_pf(
    df: pd.DataFrame, feat: str, direction: str, threshold: float, n_iter: int = N_PERMUTATIONS
) -> float:
    """Test : la PF après filtre est-elle significativement > random ?"""
    mask = (df[feat] > threshold) if direction == "gt" else (df[feat] < threshold)
    n_kept = int(mask.sum())
    if n_kept < 30:
        return float("nan")
    observed_pf = pf(df.loc[mask, "pnl"].to_numpy())

    pnl_arr = df["pnl"].to_numpy()
    rng = np.random.default_rng(42)
    null_pfs = np.empty(n_iter)
    n_total = len(pnl_arr)
    for k in range(n_iter):
        idx = rng.choice(n_total, n_kept, replace=False)
        null_pfs[k] = pf(pnl_arr[idx])
    # p-value : probabilité d'obtenir un PF >= observed par chance
    p = float((null_pfs >= observed_pf).mean())
    return p


def fit_models(X: pd.DataFrame, y: np.ndarray):
    """Ajuste 3 modèles sklearn + permutation importance."""
    # Drop NaN
    mask_complete = X.notna().all(axis=1)
    X = X.loc[mask_complete].copy()
    y = y[mask_complete.to_numpy()]

    results = {}

    # Decision Tree depth=2
    dt = DecisionTreeClassifier(max_depth=2, random_state=42)
    dt.fit(X, y)
    results["decision_tree"] = {
        "feature_importance": dict(zip(X.columns, dt.feature_importances_)),
        "tree_text": export_text(dt, feature_names=list(X.columns)),
    }

    # Random Forest
    rf = RandomForestClassifier(n_estimators=200, max_depth=4, random_state=42, n_jobs=-1)
    rf.fit(X, y)
    results["random_forest"] = {
        "feature_importance": dict(zip(X.columns, rf.feature_importances_)),
        "oob_proxy_acc": float(rf.score(X, y)),
    }

    # Logistic Regression (standardized)
    scaler = StandardScaler()
    Xs = scaler.fit_transform(X)
    lr = LogisticRegression(max_iter=1000, random_state=42)
    lr.fit(Xs, y)
    results["logistic_regression"] = {
        "coefficients": dict(zip(X.columns, lr.coef_[0])),
        "intercept": float(lr.intercept_[0]),
        "fit_acc": float(lr.score(Xs, y)),
    }

    # Permutation importance sur RF (plus robuste)
    perm = permutation_importance(rf, X, y, n_repeats=30, random_state=42, n_jobs=-1)
    results["permutation_importance"] = {
        "mean": dict(zip(X.columns, perm.importances_mean)),
        "std": dict(zip(X.columns, perm.importances_std)),
    }

    return results


def top_features_consensus(model_results: dict, k: int = 5) -> list:
    """Combine les rankings des 4 méthodes pour un top-k consensus."""
    methods = {
        "decision_tree": model_results["decision_tree"]["feature_importance"],
        "random_forest": model_results["random_forest"]["feature_importance"],
        "logistic_regression": {
            f: abs(v) for f, v in model_results["logistic_regression"]["coefficients"].items()
        },
        "permutation_importance": model_results["permutation_importance"]["mean"],
    }
    # Rank moyen (1 = meilleur)
    all_features = list(next(iter(methods.values())).keys())
    rank_sum = {f: 0.0 for f in all_features}
    for m, scores in methods.items():
        sorted_feats = sorted(scores.items(), key=lambda x: -x[1])
        for rank, (f, _) in enumerate(sorted_feats, 1):
            rank_sum[f] += rank
    sorted_consensus = sorted(rank_sum.items(), key=lambda x: x[1])
    return [f for f, _ in sorted_consensus[:k]]


def plot_feature_importance(model_results: dict, out_path: Path) -> None:
    perm = model_results["permutation_importance"]["mean"]
    sorted_items = sorted(perm.items(), key=lambda x: x[1])
    feats = [k for k, _ in sorted_items]
    vals = [v for _, v in sorted_items]
    fig, ax = plt.subplots(figsize=(9, 6))
    ax.barh(feats, vals, color="steelblue")
    ax.set_xlabel("Permutation importance (RandomForest)")
    ax.set_title("Importance des features pour P(win) — fib-v4 baseline (toutes cellules)")
    plt.tight_layout()
    plt.savefig(out_path, dpi=110)
    plt.close()


def plot_decile_pf(df: pd.DataFrame, features: list, out_path: Path) -> None:
    n_feats = len(features)
    fig, axes = plt.subplots(1, n_feats, figsize=(5 * n_feats, 4), squeeze=False)
    for ax, feat in zip(axes[0], features):
        if feat not in df.columns:
            continue
        bins = pd.qcut(df[feat], 10, duplicates="drop")
        pf_by_bin = df.groupby(bins, observed=True).apply(
            lambda x: pf(x["pnl"].to_numpy()), include_groups=False
        )
        n_by_bin = df.groupby(bins, observed=True).size()
        x = np.arange(len(pf_by_bin))
        ax.bar(x, pf_by_bin.values, color="darkorange")
        ax.axhline(1.0, color="black", linestyle="--", lw=1)
        ax.set_xticks(x)
        ax.set_xticklabels([f"D{i+1}" for i in range(len(pf_by_bin))], rotation=45)
        ax.set_title(f"{feat}\nn≈{int(n_by_bin.mean())} / décile")
        ax.set_ylabel("PF")
    plt.tight_layout()
    plt.savefig(out_path, dpi=110)
    plt.close()


def main():
    csv_path = OUTPUT_DIR / "fib_v4_trades_all.csv"
    if not csv_path.exists():
        print("Lance d'abord explore_fib_v4_features.py")
        sys.exit(1)
    df_all = pd.read_csv(csv_path)
    print(f"  Trades chargés : {len(df_all)} sur {df_all['cell_id'].nunique()} cellules")

    lines = []
    lines.append("# Phase 3b — Data science fib-v4 (sklearn + permutation tests)\n")
    lines.append(
        f"**Méthodologie** : reproduction de OPR v5.1 — 4 modèles "
        f"sklearn + permutation tests à {N_PERMUTATIONS:,} itérations.\n"
    )
    lines.append(
        f"**Population globale** : {len(df_all)} trades sur "
        f"{df_all['cell_id'].nunique()} cellules viables.\n"
    )

    # ── A. Corrélations globales ──
    lines.append("\n## A. Corrélations globales feature ↔ PnL\n")
    lines.append("| Feature | Pearson r | p | Spearman ρ | p |")
    lines.append("|---|---|---|---|---|")
    for feat in FEATURES_NUM:
        if feat not in df_all.columns:
            continue
        d = df_all[[feat, "pnl"]].dropna()
        if len(d) < 50:
            continue
        r_p, p_p = pearsonr(d[feat], d["pnl"])
        r_s, p_s = spearmanr(d[feat], d["pnl"])
        lines.append(f"| `{feat}` | {r_p:+.3f} | {p_p:.4f} | {r_s:+.3f} | {p_s:.4f} |")

    # ── B. Grid univarié — meilleur seuil par feature ──
    lines.append("\n\n## B. Grid univarié — meilleur seuil par feature\n")
    lines.append("Sélectionne le seuil qui maximise PF post-filtre (n ≥ 30).\n")
    lines.append("| Feature | Direction | Seuil | PF avant | PF après | ΔPF | n filtrés |")
    lines.append("|---|---|---|---|---|---|---|")
    base_pf = pf(df_all["pnl"].to_numpy())
    univariate_results = []
    for feat in FEATURES_NUM:
        if feat not in df_all.columns:
            continue
        b = best_threshold_univariate(df_all, feat)
        if not b or b.get("direction") is None:
            continue
        univariate_results.append(b)
        lines.append(
            f"| `{feat}` | {b['direction']} | {b['threshold']:.3f} "
            f"| {base_pf:.2f} | {b['pf_after']:.2f} | "
            f"{b.get('delta_pf', 0):+.2f} | {b['n_after']} |"
        )

    # Top-5 univariate
    univariate_results.sort(key=lambda x: -x["pf_after"])
    lines.append("\n**Top 5 univariate :**\n")
    for i, b in enumerate(univariate_results[:5], 1):
        lines.append(
            f"{i}. `{b['feature']}` {b['direction']} {b['threshold']:.3f} "
            f"→ PF = {b['pf_after']:.2f} (n={b['n_after']}) "
            f"vs baseline {base_pf:.2f}"
        )

    # ── C. Modèles sklearn (toutes cellules cumulées) ──
    lines.append("\n\n## C. Modèles sklearn (vue globale, toutes cellules)\n")
    X = df_all[FEATURES_NUM].copy()
    y = (df_all["pnl"] > 0).astype(int).to_numpy()
    print(f"  Ajustement modèles globaux ({len(X)} échantillons)...")
    models = fit_models(X, y)

    lines.append(
        "\n### Decision Tree (depth=2)\n```\n" f"{models['decision_tree']['tree_text']}\n```"
    )

    lines.append("\n### Random Forest — Top importances\n")
    rf_imp = sorted(models["random_forest"]["feature_importance"].items(), key=lambda x: -x[1])
    for f, v in rf_imp[:8]:
        lines.append(f"- `{f}` : {v:.3f}")

    lines.append("\n### Logistic Regression — Top coefficients (|c|)\n")
    lr_coefs = sorted(
        models["logistic_regression"]["coefficients"].items(), key=lambda x: -abs(x[1])
    )
    for f, v in lr_coefs[:8]:
        sign = "+" if v > 0 else "−"
        lines.append(
            f"- `{f}` : {sign}{abs(v):.3f}  (effet "
            f"{'augmente P(win)' if v > 0 else 'réduit P(win)'})"
        )

    lines.append("\n### Permutation importance — Top (mean)\n")
    perm_imp = sorted(models["permutation_importance"]["mean"].items(), key=lambda x: -x[1])
    for f, v in perm_imp[:8]:
        std = models["permutation_importance"]["std"][f]
        lines.append(f"- `{f}` : {v:+.4f} ± {std:.4f}")

    # ── D. Consensus top features ──
    consensus = top_features_consensus(models, k=5)
    lines.append("\n### Consensus top-5 (rank moyen des 4 méthodes)\n")
    for i, f in enumerate(consensus, 1):
        lines.append(f"{i}. `{f}`")

    # ── E. Permutation tests sur le top univariate ──
    lines.append("\n\n## D. Permutation tests (significativité statistique)\n")
    lines.append(
        f"Test : la PF observée après filtre est-elle supérieure "
        f"à des filtres aléatoires de même n ? "
        f"({N_PERMUTATIONS:,} itérations)\n"
    )
    lines.append("| Feature | Filtre | PF observée | p-value | Verdict |")
    lines.append("|---|---|---|---|---|")
    for b in univariate_results[:8]:
        p = permutation_test_pf(
            df_all, b["feature"], b["direction"], b["threshold"], n_iter=N_PERMUTATIONS
        )
        if pd.isna(p):
            continue
        if p < 0.001:
            verdict = "🟢 hautement significatif"
        elif p < 0.01:
            verdict = "🟢 significatif"
        elif p < 0.05:
            verdict = "🟡 marginal"
        else:
            verdict = "🔴 non significatif"
        lines.append(
            f"| `{b['feature']}` | {b['direction']} {b['threshold']:.3f} "
            f"| {b['pf_after']:.2f} | {p:.4f} | {verdict} |"
        )

    # ── F. Par cellule (top 4 cellules les plus volumineuses) ──
    lines.append("\n\n## E. Analyse par cellule (top cellules)\n")
    cell_sizes = df_all["cell_id"].value_counts()
    top_cells = cell_sizes.head(6).index.tolist()
    for cell in top_cells:
        sub = df_all[df_all["cell_id"] == cell].copy()
        if len(sub) < 80:
            continue
        lines.append(f"\n### Cellule `{cell}` ({len(sub)} trades)\n")
        sub_X = sub[FEATURES_NUM].copy()
        sub_y = (sub["pnl"] > 0).astype(int).to_numpy()
        sub_models = fit_models(sub_X, sub_y)
        sub_consensus = top_features_consensus(sub_models, k=3)
        lines.append(f"Top-3 features : {', '.join(f'`{f}`' for f in sub_consensus)}\n")

        # Univariate sur les 3 top
        sub_base = pf(sub["pnl"].to_numpy())
        for feat in sub_consensus:
            b = best_threshold_univariate(sub, feat)
            if not b or b.get("direction") is None:
                continue
            lines.append(
                f"- `{feat}` {b['direction']} {b['threshold']:.3f} "
                f"→ PF {sub_base:.2f} → {b['pf_after']:.2f} "
                f"(n_after={b['n_after']})"
            )

    # ── Visuels ──
    print("  Génération figures...")
    plot_feature_importance(models, OUTPUT_DIR / "fib_v4_feature_importance.png")
    # Décile pour les 3 top features
    plot_decile_pf(df_all, consensus[:3], OUTPUT_DIR / "fib_v4_decile_pf.png")
    lines.append("\n\n## F. Visuels\n")
    lines.append("- `output/fib_v4_feature_importance.png` — barplot importance globale")
    lines.append("- `output/fib_v4_decile_pf.png` — déciles PF des 3 features dominantes\n")

    out_md = OUTPUT_DIR / "data_science_fib_v4.md"
    out_md.write_text("\n".join(lines))
    print(f"  ✅ Rapport : {out_md}")


if __name__ == "__main__":
    main()
