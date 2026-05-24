"""
Test de l'hypothèse divergence prix/oscillateurs sur pivots M5 order=20.

Hypothèse : ajouter des features de pente du prix + pente d'oscillateurs
(RSI, Stoch K, CCI) + signaux de divergence explicites augmente le lift
du détecteur de pivots — surtout sur MCL1/MGC1 (commodities, leaders
identifiés à l'étape précédente).

Pipeline : walk-forward 4 splits, comparaison baseline vs enrichi.

Usage : python scripts/research_pivot_divergence.py --ticker MCL1
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

OUT_ROOT = ROOT / "output" / "pivot_research_div"
SPLITS = [
    pd.Timestamp("2025-12-31"),
    pd.Timestamp("2026-01-31"),
    pd.Timestamp("2026-02-28"),
    pd.Timestamp("2026-03-31"),
]
OOS_HORIZON_DAYS = 60
ORDER = 20

BASELINE_FEATURES = [
    "ema9_slope", "ema21_slope", "ema50_slope",
    "roc_5", "roc_20", "roc_50", "adx_14",
    "atr_14", "atr_ratio_short_long", "bb_width",
    "dist_close_ema21_atr", "range_atr_ratio",
    "body_range_ratio", "upper_wick_ratio", "lower_wick_ratio", "vol_rel",
    "dist_to_max20_atr", "dist_to_min20_atr",
    "past_pivot_density_2atr", "past_pivot_density_1atr",
    "ret_lag_1", "ret_lag_2", "ret_lag_3", "ret_lag_5", "ret_lag_10",
    "up_bars_last_10",
    "hour_ny", "minute_ny", "dow", "bars_since_open", "is_macro_day",
]


# ────────────────────────────────────────────────────────────────────────────
# Calcul vectorisé des pentes de régression linéaire (rolling)
# ────────────────────────────────────────────────────────────────────────────
def rolling_slope(s: pd.Series, w: int) -> pd.Series:
    """Pente de la régression linéaire sur les w dernières valeurs.

    Vectorisé via la formule analytique. Beaucoup plus rapide que
    .rolling(w).apply(polyfit).
    """
    x = np.arange(w, dtype=np.float64)
    x_mean = x.mean()
    x_centered = x - x_mean
    denom = (x_centered ** 2).sum()
    # convolution causale : on inverse les poids pour matcher le "dernier point au temps t"
    weights = x_centered / denom
    y = s.to_numpy(dtype=np.float64)
    # mode 'valid' = sortie de longueur N - w + 1
    conv = np.convolve(y, weights[::-1], mode="valid")
    out = np.full(len(s), np.nan)
    out[w - 1:] = conv
    return pd.Series(out, index=s.index)


# ────────────────────────────────────────────────────────────────────────────
# Oscillateurs
# ────────────────────────────────────────────────────────────────────────────
def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def stoch_k(df: pd.DataFrame, n: int = 14) -> pd.Series:
    ll = df["low"].rolling(n).min()
    hh = df["high"].rolling(n).max()
    return 100 * (df["close"] - ll) / (hh - ll).replace(0, np.nan)


def cci(df: pd.DataFrame, n: int = 20) -> pd.Series:
    tp = (df["high"] + df["low"] + df["close"]) / 3
    sma = tp.rolling(n).mean()
    mad = tp.rolling(n).apply(lambda x: np.mean(np.abs(x - x.mean())), raw=True)
    return (tp - sma) / (0.015 * mad.replace(0, np.nan))


# ────────────────────────────────────────────────────────────────────────────
# Feature engineering enrichi
# ────────────────────────────────────────────────────────────────────────────
SLOPE_WINDOWS = (5, 10, 20)
OSCILLATORS = ("rsi14", "stochk14", "cci20")


def add_divergence_features(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Ajoute pentes prix + oscillateurs + divergences. Retourne le df enrichi
    et la liste des nouvelles colonnes.
    """
    out = df.copy()
    new_cols = []

    # Calcule les oscillateurs
    out["rsi14"] = rsi(out["close"], 14)
    out["stochk14"] = stoch_k(out, 14)
    out["cci20"] = cci(out, 20)

    # Pentes du prix
    for w in SLOPE_WINDOWS:
        col = f"slope_price_{w}"
        out[col] = rolling_slope(out["close"], w)
        new_cols.append(col)

    # Pentes des oscillateurs
    for osc in OSCILLATORS:
        for w in SLOPE_WINDOWS:
            col = f"slope_{osc}_{w}"
            out[col] = rolling_slope(out[osc], w)
            new_cols.append(col)

    # Divergences : signe opposé prix vs oscillateur
    # = +1 si divergence haussière (prix down, osc up)
    # = -1 si divergence baissière (prix up, osc down)
    # = 0 sinon (alignement)
    for osc in OSCILLATORS:
        for w in SLOPE_WINDOWS:
            sp = out[f"slope_price_{w}"]
            so = out[f"slope_{osc}_{w}"]
            div_bull = (sp < 0) & (so > 0)
            div_bear = (sp > 0) & (so < 0)
            col = f"div_{osc}_{w}"
            out[col] = div_bull.astype(np.int8) - div_bear.astype(np.int8)
            new_cols.append(col)

    # Niveaux des oscillateurs eux-mêmes (peuvent contenir du signal indépendant)
    for osc in OSCILLATORS:
        new_cols.append(osc)

    return out, new_cols


# ────────────────────────────────────────────────────────────────────────────
# Train / eval
# ────────────────────────────────────────────────────────────────────────────
def precision_at_recall(y_true, y_score, target):
    p, r, _ = precision_recall_curve(y_true, y_score)
    mask = r >= target
    if not mask.any():
        return float("nan")
    return float(p[mask].max())


def score_proba(y_true, p, base_rate):
    return {
        "pr_auc": float(average_precision_score(y_true, p)),
        "p@r5": precision_at_recall(y_true, p, 0.05),
        "p@r10": precision_at_recall(y_true, p, 0.10),
        "p@r20": precision_at_recall(y_true, p, 0.20),
        "base_rate": base_rate,
        "n_oos": int(len(y_true)),
        "n_pos_oos": int(y_true.sum()),
    }


def train_set(X_is, y_is, X_oos, y_oos, base_rate):
    rf = RandomForestClassifier(
        n_estimators=300, max_depth=8, class_weight="balanced",
        random_state=42, n_jobs=-1, min_samples_leaf=20,
    )
    rf.fit(X_is, y_is)
    p_rf = rf.predict_proba(X_oos)[:, 1]
    sc_rf = score_proba(y_oos, p_rf, base_rate)

    hgb = HistGradientBoostingClassifier(
        max_iter=300, learning_rate=0.05, max_depth=6,
        class_weight="balanced", random_state=42,
    )
    hgb.fit(X_is, y_is)
    p_hgb = hgb.predict_proba(X_oos)[:, 1]
    sc_hgb = score_proba(y_oos, p_hgb, base_rate)
    # Best of two
    best = "rf" if sc_rf["pr_auc"] >= sc_hgb["pr_auc"] else "hgb"
    return {
        "rf": sc_rf, "hgb": sc_hgb,
        "best": best,
        "best_score": sc_rf if best == "rf" else sc_hgb,
        "best_model": rf if best == "rf" else hgb,
    }


def wf_run(df, feature_cols, label_col="is_pivot_any"):
    per_split = []
    last_model = None
    last_X_oos = None
    last_y_oos = None
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
        base_rate = float(y_oos.mean())
        res = train_set(X_is, y_is, X_oos, y_oos, base_rate)
        per_split.append({"split": i, "n_oos": len(df_oos),
                          "base_rate": base_rate, **res})
        last_model = res["best_model"]
        last_X_oos = X_oos
        last_y_oos = y_oos

    def agg(metric, model_key):
        v = np.array([s[model_key][metric] for s in per_split])
        return float(v.mean()), float(v.std())

    summary = {"n_splits": len(per_split), "per_split": per_split}
    for mk in ("rf", "hgb"):
        for m in ("pr_auc", "p@r5", "p@r10", "p@r20"):
            mu, sd = agg(m, mk)
            summary[f"{mk}_{m}_mean"] = mu
            summary[f"{mk}_{m}_std"] = sd
    summary["last_model"] = last_model
    summary["last_X_oos"] = last_X_oos
    summary["last_y_oos"] = last_y_oos
    return summary


# ────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", required=True)
    args = parser.parse_args()
    ticker = args.ticker

    print(f"▸ {ticker} order={ORDER} | baseline vs enrichi")
    data_csv = ROOT / "data" / f"{ticker}_data_m5.csv"
    out_dir = OUT_ROOT / ticker
    out_dir.mkdir(parents=True, exist_ok=True)

    df = load_csv(str(data_csv))
    print(f"  • {len(df):,} barres M5")

    base.TICKER = ticker
    base.PIVOT_ORDER = ORDER
    base.OUT_DIR = out_dir
    base.DATA_CSV = data_csv

    df = base.label_pivots(df, order=ORDER)
    df = base.build_features(df)

    print("  • Ajout features divergence (12 pentes + 9 divs + 3 niveaux = 24)")
    df, new_cols = add_divergence_features(df)
    enriched_features = BASELINE_FEATURES + new_cols
    print(f"  • {len(new_cols)} nouvelles features (total {len(enriched_features)})")

    df_clean = df.dropna(subset=enriched_features + ["is_pivot_any"]).copy()
    print(f"  • {len(df_clean):,} barres après dropna")

    # Run baseline
    print("\n=== BASELINE ({} features) ===".format(len(BASELINE_FEATURES)))
    sum_base = wf_run(df_clean, BASELINE_FEATURES)
    for mk in ("rf", "hgb"):
        print(f"  • {mk}  PR-AUC={sum_base[f'{mk}_pr_auc_mean']:.3f}±{sum_base[f'{mk}_pr_auc_std']:.3f}  "
              f"P@R10%={sum_base[f'{mk}_p@r10_mean']:.2%}±{sum_base[f'{mk}_p@r10_std']:.2%}")

    print("\n=== ENRICHI ({} features) ===".format(len(enriched_features)))
    sum_enr = wf_run(df_clean, enriched_features)
    for mk in ("rf", "hgb"):
        print(f"  • {mk}  PR-AUC={sum_enr[f'{mk}_pr_auc_mean']:.3f}±{sum_enr[f'{mk}_pr_auc_std']:.3f}  "
              f"P@R10%={sum_enr[f'{mk}_p@r10_mean']:.2%}±{sum_enr[f'{mk}_p@r10_std']:.2%}")

    # Permutation importance des nouvelles features (enrichi, dernier split)
    print("\n▸ Permutation importance dernier split (enrichi)…")
    pi = permutation_importance(
        sum_enr["last_model"], sum_enr["last_X_oos"], sum_enr["last_y_oos"],
        n_repeats=5, random_state=42, n_jobs=-1, scoring="average_precision",
    )
    imp = pd.DataFrame({"feature": enriched_features, "importance": pi.importances_mean,
                        "is_new": [f in new_cols for f in enriched_features]})
    imp = imp.sort_values("importance", ascending=False)

    # ────── Rapport ──────
    lines = [f"# Divergences prix/oscillateurs — {ticker} order={ORDER}\n"]
    w = lines.append
    w(f"- Barres totales : **{len(df_clean):,}**  | Splits WF utilisés : **{sum_enr['n_splits']}/{len(SPLITS)}**")
    w(f"- Base rate OOS moyen : **{np.mean([s['base_rate'] for s in sum_enr['per_split']]):.2%}**")
    w(f"- Nouvelles features ajoutées : **{len(new_cols)}** "
      f"(pentes prix×3 + pentes osc×9 + divergences×9 + niveaux osc×3)\n")

    w("## Baseline vs Enrichi (best model par split — moyenne ± std sur 4 splits)\n")
    rows = []
    for mk, lbl in (("rf", "RF"), ("hgb", "HGB")):
        rows.append({
            "modèle": lbl,
            "PR-AUC base": f"{sum_base[f'{mk}_pr_auc_mean']:.3f} ± {sum_base[f'{mk}_pr_auc_std']:.3f}",
            "PR-AUC enr.": f"{sum_enr[f'{mk}_pr_auc_mean']:.3f} ± {sum_enr[f'{mk}_pr_auc_std']:.3f}",
            "Δ PR-AUC": f"{(sum_enr[f'{mk}_pr_auc_mean'] - sum_base[f'{mk}_pr_auc_mean']):+.3f}",
            "P@R10 base": f"{sum_base[f'{mk}_p@r10_mean']:.2%}",
            "P@R10 enr.": f"{sum_enr[f'{mk}_p@r10_mean']:.2%}",
            "Δ P@R10": f"{(sum_enr[f'{mk}_p@r10_mean'] - sum_base[f'{mk}_p@r10_mean']):+.2%}",
        })
    w(pd.DataFrame(rows).to_markdown(index=False))

    w("\n## Top 20 features (enrichi, permutation importance dernier split)\n")
    top = imp.head(20).copy()
    top["importance"] = top["importance"].map(lambda x: f"{x:+.4f}")
    top["is_new"] = top["is_new"].map(lambda b: "🆕" if b else "")
    w(top.to_markdown(index=False))

    w("\n## Nouvelles features uniquement (rang dans le top global)\n")
    new_only = imp[imp["is_new"]].copy()
    new_only["rank"] = range(1, len(new_only) + 1)
    new_only["importance"] = new_only["importance"].map(lambda x: f"{x:+.4f}")
    new_only = new_only[["rank", "feature", "importance"]].head(15)
    w(new_only.to_markdown(index=False))

    (out_dir / "rapport_divergence.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n→ {out_dir}/rapport_divergence.md")

    # Plot delta
    fig, ax = plt.subplots(figsize=(10, 6))
    metrics = ["pr_auc", "p@r5", "p@r10", "p@r20"]
    x = np.arange(len(metrics))
    width = 0.35
    base_vals = [sum_base[f"hgb_{m}_mean"] for m in metrics]
    enr_vals = [sum_enr[f"hgb_{m}_mean"] for m in metrics]
    ax.bar(x - width / 2, base_vals, width, label="baseline (31 feat)", color="steelblue")
    ax.bar(x + width / 2, enr_vals, width, label="enrichi (+24 feat)", color="darkorange")
    ax.set_xticks(x)
    ax.set_xticklabels(["PR-AUC", "P@R5%", "P@R10%", "P@R20%"])
    ax.set_title(f"{ticker} order={ORDER} — Baseline vs Enrichi (HGB)")
    ax.legend()
    ax.grid(alpha=0.3, axis="y")
    fig.tight_layout()
    fig.savefig(out_dir / "baseline_vs_enrichi.png", dpi=110)
    plt.close(fig)


if __name__ == "__main__":
    main()
