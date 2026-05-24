"""
Analyse data science approfondie du dataset OPR v5 enrichi.

Approche : aucun a priori sur les filtres. Laisser les données identifier les patterns.
- Stats descriptives par ticker
- Corrélations Pearson + Spearman
- Distributions par décile (10 bins)
- Interactions multivariées (croisements 5×5)
- Best vs Worst quintile : caractérisation des extrêmes
- Grid search greedy sur seuils (alternative à arbre de décision)
- Visualisations (scatter + heatmaps)

Pas de sklearn (non installé). Pandas + numpy + scipy + matplotlib uniquement.
"""

from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy import stats
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import cross_val_score
from sklearn.preprocessing import StandardScaler
from sklearn.tree import DecisionTreeClassifier, export_text

np.random.seed(42)

# ============================================================================
# 1. Setup & chargement
# ============================================================================
ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "output" / "opr_v5_features.csv"
FIG_DIR = ROOT / "output" / "figures"
FIG_DIR.mkdir(exist_ok=True, parents=True)
REPORT_PATH = ROOT / "output" / "data_science_opr_v5.md"

df = pd.read_csv(DATA_PATH)
df["v5_reject_reason"] = df["v5_reject_reason"].astype(str).replace("nan", "NO_REJECT")

df_filled = df[df["result"].isin(["TP", "SL", "TE"])].copy()
df_unfilled = df[df["result"] == "NOT_FILLED"].copy()

FEATURES = ["f1_bars", "f2_excursion_atr", "f2_excursion_oprrange", "f3_bars"]

df_filled["date"] = pd.to_datetime(df_filled["date"])
df_filled["weekday"] = df_filled["date"].dt.day_name()
df_filled["month"] = df_filled["date"].dt.month
df_filled["is_winner"] = (df_filled["pnl"] > 0).astype(int)
df_filled["is_long"] = (df_filled["dir"] == "long").astype(int)


# ============================================================================
# Utilitaires
# ============================================================================
lines = []


def w(line=""):
    lines.append(line)


def to_md(df_in: pd.DataFrame, floatfmt: str = ".2f") -> str:
    """Convertit un DataFrame en markdown sans dépendre de tabulate."""
    if len(df_in) == 0:
        return "_vide_"
    if df_in.index.name is None and not isinstance(df_in.index, pd.MultiIndex):
        if df_in.index.tolist() != list(range(len(df_in))):
            df_in = df_in.reset_index()
        else:
            df_in = df_in.reset_index(drop=True)
    else:
        df_in = df_in.reset_index()

    def fmt(v):
        if isinstance(v, float):
            if pd.isna(v):
                return "_"
            if abs(v) < 0.01 and v != 0:
                return f"{v:.4f}"
            return format(v, floatfmt)
        return str(v)

    cols = df_in.columns.tolist()
    rows = [["|" + "|".join(str(c) for c in cols) + "|"]]
    rows.append(["|" + "|".join(["---"] * len(cols)) + "|"])
    for _, row in df_in.iterrows():
        rows.append(["|" + "|".join(fmt(row[c]) for c in cols) + "|"])
    return "\n".join(r[0] for r in rows)


def wt(df_in: pd.DataFrame, floatfmt: str = ".2f"):
    w(to_md(df_in, floatfmt))
    w()


def pf_safe(x):
    pos = x[x > 0].sum()
    neg = -x[x < 0].sum()
    if neg == 0:
        return float("inf") if pos > 0 else 0.0
    return pos / neg


# ============================================================================
# 2. Stats descriptives
# ============================================================================
w("# Analyse data science — OPR v5 features (F1, F2, F3)")
w()
w(f"Dataset : `output/opr_v5_features.csv` — {len(df)} trades v4 enrichis.")
w(f"Filled : {len(df_filled)} ({len(df_filled) / len(df):.1%}). Not filled : {len(df_unfilled)}.")
w()
w("**Approche** : aucun a priori sur les filtres. Laisser les données parler.")
w()
w("---")
w()
w("## 1. Stats descriptives par ticker (filled only)")
w()

stats_tbl = (
    df_filled.groupby("ticker")
    .agg(
        n=("pnl", "count"),
        wr=("is_winner", lambda x: 100 * x.mean()),
        pnl_total=("pnl", "sum"),
        pnl_mean=("pnl", "mean"),
        pnl_median=("pnl", "median"),
        pnl_std=("pnl", "std"),
        f1_mean=("f1_bars", "mean"),
        f1_p50=("f1_bars", "median"),
        f2_mean=("f2_excursion_atr", "mean"),
        f2_p50=("f2_excursion_atr", "median"),
        f3_mean=("f3_bars", "mean"),
        f3_p50=("f3_bars", "median"),
    )
    .round(2)
)
wt(stats_tbl, ".2f")


# ============================================================================
# 3. Corrélations
# ============================================================================
w("## 2. Corrélations features ↔ PnL")
w()
w("Pearson (linéaire). Spearman (monotonie, robuste outliers).")
w()


def correlations(df_sub, label):
    rows = []
    for f in FEATURES:
        sub = df_sub.dropna(subset=[f, "pnl"])
        if len(sub) < 20:
            continue
        pr, pp = stats.pearsonr(sub[f], sub["pnl"])
        sr, sp = stats.spearmanr(sub[f], sub["pnl"])
        rows.append(
            {
                "feature": f,
                "n": len(sub),
                "pearson_r": round(pr, 3),
                "pearson_p": f"{pp:.2e}",
                "spearman_r": round(sr, 3),
                "spearman_p": f"{sp:.2e}",
            }
        )
    return pd.DataFrame(rows)


for ticker in ["ALL"] + sorted(df_filled["ticker"].unique().tolist()):
    w(f"### {ticker}")
    w()
    sub = df_filled if ticker == "ALL" else df_filled[df_filled["ticker"] == ticker]
    wt(correlations(sub, ticker), ".3f")


# ============================================================================
# 4. Distributions par décile
# ============================================================================
w("## 3. Distributions par décile — PnL par 10 bins de feature")
w()


def deciles(df_sub, feature):
    sub = df_sub.dropna(subset=[feature, "pnl"]).copy()
    if len(sub) < 100:
        return None
    try:
        sub["q"] = pd.qcut(sub[feature], 10, duplicates="drop")
    except ValueError:
        return None
    grp = (
        sub.groupby("q", observed=True)
        .agg(
            n=("pnl", "count"),
            wr_pct=("is_winner", lambda x: round(100 * x.mean(), 1)),
            pf=("pnl", pf_safe),
            pnl_mean=("pnl", "mean"),
            pnl_sum=("pnl", "sum"),
        )
        .round(2)
    )
    grp["bin"] = [str(idx) for idx in grp.index]
    return grp.reset_index(drop=True)[["bin", "n", "wr_pct", "pf", "pnl_mean", "pnl_sum"]]


for feature in FEATURES:
    w(f"### `{feature}` — portfolio")
    w()
    res = deciles(df_filled, feature)
    if res is None:
        w("_n insuffisant_")
    else:
        wt(res, ".2f")


# ============================================================================
# 5. Grid search greedy : meilleur seuil univarié par feature
# ============================================================================
w("## 4. Grid search univarié — meilleur seuil de filtrage par feature")
w()
w(
    "Question : pour chaque feature, quel seuil optimise le PnL conservé (sum si on rejette > seuil OU < seuil) ?"
)
w()


def grid_univariate(df_sub, feature, direction="upper", min_n=50):
    """
    direction='upper' : rejette si feature > seuil
    direction='lower' : rejette si feature < seuil
    Renvoie le top 5 seuils par PnL conservé.
    """
    sub = df_sub.dropna(subset=[feature, "pnl"]).copy()
    if len(sub) < min_n:
        return None
    quantiles = np.linspace(0.05, 0.95, 19)
    thresholds = sub[feature].quantile(quantiles).unique()
    base_pnl = sub["pnl"].sum()
    base_n = len(sub)
    base_pf = pf_safe(sub["pnl"])

    rows = []
    for t in thresholds:
        if direction == "upper":
            kept = sub[sub[feature] <= t]
        else:
            kept = sub[sub[feature] >= t]
        if len(kept) < min_n:
            continue
        rows.append(
            {
                "direction": direction,
                "threshold": round(t, 3),
                "kept_n": len(kept),
                "rejected_n": base_n - len(kept),
                "kept_pnl": round(kept["pnl"].sum(), 2),
                "kept_pf": round(pf_safe(kept["pnl"]), 2),
                "kept_pnl_mean": round(kept["pnl"].mean(), 2),
                "delta_pnl_vs_base": round(kept["pnl"].sum() - base_pnl, 2),
                "delta_pf_vs_base": round(pf_safe(kept["pnl"]) - base_pf, 2),
            }
        )
    if not rows:
        return None
    return pd.DataFrame(rows).sort_values("kept_pf", ascending=False).head(5)


for ticker in ["ALL", "MES1", "NQ1", "YM1"]:
    w(f"### {ticker}")
    w()
    sub = df_filled if ticker == "ALL" else df_filled[df_filled["ticker"] == ticker]

    for feature in ["f1_bars", "f2_excursion_atr", "f3_bars"]:
        for direction in ["upper", "lower"]:
            res = grid_univariate(sub, feature, direction)
            if res is None or len(res) == 0:
                continue
            w(f"**`{feature}` filtre {direction} (top 5 par PF kept)** :")
            w()
            wt(res, ".2f")


# ============================================================================
# 6. Grid search bivarié — combinaisons F1 × F2, etc.
# ============================================================================
w("## 5. Grid search bivarié — meilleure combinaison de 2 filtres")
w()


def grid_bivariate(df_sub, fx, fy, min_n=50):
    sub = df_sub.dropna(subset=[fx, fy, "pnl"]).copy()
    if len(sub) < min_n:
        return None
    base_pnl = sub["pnl"].sum()
    base_pf = pf_safe(sub["pnl"])
    base_n = len(sub)

    # Quantiles modérément résolus
    qs = np.linspace(0.1, 0.9, 9)
    tx_options = sub[fx].quantile(qs).unique()
    ty_options = sub[fy].quantile(qs).unique()

    rows = []
    for tx in tx_options:
        for ty in ty_options:
            # Garde si fx <= tx AND fy <= ty (filtre supérieur sur les deux)
            kept = sub[(sub[fx] <= tx) & (sub[fy] <= ty)]
            if len(kept) < min_n:
                continue
            rows.append(
                {
                    "fx": fx,
                    "tx_max": round(tx, 3),
                    "fy": fy,
                    "ty_max": round(ty, 3),
                    "kept_n": len(kept),
                    "kept_pnl": round(kept["pnl"].sum(), 2),
                    "kept_pf": round(pf_safe(kept["pnl"]), 2),
                    "delta_pnl_vs_base": round(kept["pnl"].sum() - base_pnl, 2),
                    "delta_pf_vs_base": round(pf_safe(kept["pnl"]) - base_pf, 2),
                }
            )
    if not rows:
        return None
    df_r = pd.DataFrame(rows).sort_values("kept_pf", ascending=False)
    return df_r.head(8)


for ticker in ["ALL", "MES1", "NQ1", "YM1"]:
    w(f"### {ticker}")
    w()
    sub = df_filled if ticker == "ALL" else df_filled[df_filled["ticker"] == ticker]
    for fx, fy in [
        ("f1_bars", "f2_excursion_atr"),
        ("f2_excursion_atr", "f3_bars"),
        ("f1_bars", "f3_bars"),
    ]:
        res = grid_bivariate(sub, fx, fy)
        if res is None:
            continue
        w(f"**{fx} × {fy} — top 8 combinaisons** :")
        w()
        wt(res, ".2f")


# ============================================================================
# 7. Interactions multivariées (heatmap PnL moyen par quintile × quintile)
# ============================================================================
w("## 6. Interactions multivariées — PnL moyen par croisement 5×5")
w()


def interaction(df_sub, fx, fy):
    sub = df_sub.dropna(subset=[fx, fy, "pnl"]).copy()
    if len(sub) < 50:
        return None
    try:
        sub[f"{fx}_q"] = pd.qcut(sub[fx], 5, duplicates="drop", labels=False)
        sub[f"{fy}_q"] = pd.qcut(sub[fy], 5, duplicates="drop", labels=False)
    except ValueError:
        return None
    mp = sub.pivot_table(index=f"{fy}_q", columns=f"{fx}_q", values="pnl", aggfunc="mean").round(1)
    cp = sub.pivot_table(index=f"{fy}_q", columns=f"{fx}_q", values="pnl", aggfunc="count")
    return mp, cp


for fx, fy in [
    ("f1_bars", "f2_excursion_atr"),
    ("f1_bars", "f3_bars"),
    ("f2_excursion_atr", "f3_bars"),
]:
    w(f"### `{fx}` × `{fy}` — portfolio")
    w()
    res = interaction(df_filled, fx, fy)
    if res is None:
        w("_n insuffisant_")
        continue
    mp, cp = res
    w("PnL moyen ($) :")
    w()
    wt(mp.round(1), ".1f")
    w("Effectifs (n) :")
    w()
    wt(cp.fillna(0).astype(int), ".0f")


# ============================================================================
# 8. Best vs Worst quintile
# ============================================================================
w("## 7. Best vs Worst quintile — caractérisation des extrêmes")
w()


def best_worst(df_sub):
    sub = df_sub.dropna(subset=FEATURES + ["pnl"]).copy()
    if len(sub) < 100:
        return None
    q20 = sub["pnl"].quantile(0.20)
    q80 = sub["pnl"].quantile(0.80)
    rows = []
    for tier, name in [
        (sub[sub["pnl"] >= q80], "Best 20%"),
        (sub[(sub["pnl"] > q20) & (sub["pnl"] < q80)], "Mid 60%"),
        (sub[sub["pnl"] <= q20], "Worst 20%"),
    ]:
        rows.append(
            {
                "tier": name,
                "n": len(tier),
                "pnl_mean": round(tier["pnl"].mean(), 2),
                "f1_mean": round(tier["f1_bars"].mean(), 2),
                "f1_p50": tier["f1_bars"].median(),
                "f2_atr_mean": round(tier["f2_excursion_atr"].mean(), 2),
                "f2_atr_p50": round(tier["f2_excursion_atr"].median(), 2),
                "f3_mean": round(tier["f3_bars"].mean(), 2),
                "f3_p50": tier["f3_bars"].median(),
            }
        )
    return pd.DataFrame(rows)


for ticker in ["ALL", "MES1", "NQ1", "YM1"]:
    w(f"### {ticker}")
    w()
    sub = df_filled if ticker == "ALL" else df_filled[df_filled["ticker"] == ticker]
    res = best_worst(sub)
    if res is None:
        w("_n insuffisant_")
    else:
        wt(res, ".2f")


# ============================================================================
# 9. Patterns temporels
# ============================================================================
w("## 8. Patterns temporels")
w()


def time_patterns(df_sub):
    wd = (
        df_sub.groupby("weekday")
        .agg(
            n=("pnl", "count"),
            wr_pct=("is_winner", lambda x: round(100 * x.mean(), 1)),
            pf=("pnl", pf_safe),
            pnl_mean=("pnl", "mean"),
            pnl_sum=("pnl", "sum"),
        )
        .round(2)
    )
    wd = wd.reindex(["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"])
    mo = (
        df_sub.groupby("month")
        .agg(
            n=("pnl", "count"),
            wr_pct=("is_winner", lambda x: round(100 * x.mean(), 1)),
            pf=("pnl", pf_safe),
            pnl_mean=("pnl", "mean"),
            pnl_sum=("pnl", "sum"),
        )
        .round(2)
    )
    return wd, mo


wd, mo = time_patterns(df_filled)
w("### Performance par jour de semaine (portfolio)")
w()
wt(wd, ".2f")
w("### Performance par mois (portfolio)")
w()
wt(mo, ".2f")


# ============================================================================
# 10. Comparaison filtres v5 actuels vs filtres data-driven optimaux
# ============================================================================
w("## 9. Filtres v5 actuels vs filtres data-driven optimaux")
w()
w("Rappel filtres optimaux retenus en PHASE 4 walk-forward (config.py) :")
w()
w("| Ticker | f1_max | f2_max_atr | f3_max | Rejets v5 |")
w("|---|---|---|---|---|")
w("| MES1 | None | None | None | 0 % |")
w("| NQ1 | None | 0.5 | None | 5 % |")
w("| YM1 | 10 | 1.0 | None | 45 % |")
w()
w("Comparons ces filtres aux **optima univariés data-driven** (cf section 4) sur la période full.")
w()


def apply_filter(df_sub, f1_max=None, f2_max=None, f3_max=None):
    sub = df_sub.copy()
    if f1_max is not None:
        sub = sub[sub["f1_bars"] <= f1_max]
    if f2_max is not None:
        sub = sub[(sub["f2_excursion_atr"].isna()) | (sub["f2_excursion_atr"] <= f2_max)]
    if f3_max is not None:
        sub = sub[(sub["f3_bars"].isna()) | (sub["f3_bars"] <= f3_max)]
    return sub


# Comparaison : sans filtre vs v5 actuel vs candidats
candidate_filters = {
    "v4 (no filter)": {},
    "v5 actuel (MES1=None, NQ1 f2_max=0.5, YM1 f1_max=10/f2_max=1.0)": "tiered",
}


def evaluate(df_sub, label):
    base_pnl = df_sub["pnl"].sum()
    base_pf = pf_safe(df_sub["pnl"])
    return {
        "scenario": label,
        "n": len(df_sub),
        "pnl_sum": round(base_pnl, 2),
        "pf": round(base_pf, 2),
        "wr_pct": round(100 * (df_sub["pnl"] > 0).mean(), 1),
    }


# v4 baseline
rows = [evaluate(df_filled, "v4 (no filter)")]

# v5 actuel : appliquer par ticker
v5_sub_list = []
for ticker, params in [
    ("MES1", {}),
    ("NQ1", {"f2_max": 0.5}),
    ("YM1", {"f1_max": 10, "f2_max": 1.0}),
]:
    s = df_filled[df_filled["ticker"] == ticker]
    s = apply_filter(s, **params)
    v5_sub_list.append(s)
v5_total = pd.concat(v5_sub_list)
rows.append(evaluate(v5_total, "v5 actuel"))

# Candidats data-driven : tester quelques bornes obtenues du grid univarié
# (à adapter en fonction des résultats observés — placeholder ici)
for candidate_name, params_per_ticker in [
    (
        "Hypothèse A : f2_max=0.5 sur les 3 tickers",
        {"MES1": {"f2_max": 0.5}, "NQ1": {"f2_max": 0.5}, "YM1": {"f2_max": 0.5}},
    ),
    (
        "Hypothèse B : f2_max=0.75 sur les 3 tickers",
        {"MES1": {"f2_max": 0.75}, "NQ1": {"f2_max": 0.75}, "YM1": {"f2_max": 0.75}},
    ),
    (
        "Hypothèse C : f1_max=12 + f2_max=1.0 sur les 3",
        {
            "MES1": {"f1_max": 12, "f2_max": 1.0},
            "NQ1": {"f1_max": 12, "f2_max": 1.0},
            "YM1": {"f1_max": 12, "f2_max": 1.0},
        },
    ),
]:
    parts = []
    for ticker, params in params_per_ticker.items():
        s = df_filled[df_filled["ticker"] == ticker]
        parts.append(apply_filter(s, **params))
    total = pd.concat(parts)
    rows.append(evaluate(total, candidate_name))

compare_df = pd.DataFrame(rows)
w("Comparaison de scénarios (toutes périodes, filled only) :")
w()
wt(compare_df, ".2f")


# ============================================================================
# 11. Visualisations
# ============================================================================
print("Génération des figures...")


def scatter_feature_vs_pnl(feature, fname):
    fig, axes = plt.subplots(1, 3, figsize=(18, 5))
    for ax, ticker in zip(axes, ["MES1", "NQ1", "YM1"]):
        sub = df_filled[df_filled["ticker"] == ticker].dropna(subset=[feature, "pnl"])
        if len(sub) == 0:
            continue
        colors = ["red" if p < 0 else "green" for p in sub["pnl"]]
        ax.scatter(sub[feature], sub["pnl"], c=colors, alpha=0.45, s=14)
        ax.axhline(0, color="black", lw=0.6)
        sub_sorted = sub.sort_values(feature).reset_index(drop=True)
        sub_sorted["pnl_med"] = sub_sorted["pnl"].rolling(40, min_periods=10, center=True).median()
        ax.plot(
            sub_sorted[feature], sub_sorted["pnl_med"], "b-", lw=2.0, label="rolling median (40)"
        )
        ax.set_title(f"{ticker} : {feature} vs PnL (n={len(sub)})")
        ax.set_xlabel(feature)
        ax.set_ylabel("PnL ($)")
        ax.legend(loc="upper right", fontsize=8)
        ax.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(FIG_DIR / fname, dpi=110, bbox_inches="tight")
    plt.close()


scatter_feature_vs_pnl("f1_bars", "ds_opr_v5_scatter_f1.png")
scatter_feature_vs_pnl("f2_excursion_atr", "ds_opr_v5_scatter_f2.png")
scatter_feature_vs_pnl("f3_bars", "ds_opr_v5_scatter_f3.png")


def heatmap_interaction(fx, fy, fname, ticker=None):
    sub = df_filled if ticker is None else df_filled[df_filled["ticker"] == ticker]
    sub = sub.dropna(subset=[fx, fy, "pnl"]).copy()
    if len(sub) < 50:
        return
    try:
        sub[f"{fx}_q"] = pd.qcut(sub[fx], 5, duplicates="drop", labels=False)
        sub[f"{fy}_q"] = pd.qcut(sub[fy], 5, duplicates="drop", labels=False)
    except ValueError:
        return
    pivot = sub.pivot_table(index=f"{fy}_q", columns=f"{fx}_q", values="pnl", aggfunc="mean")
    counts = sub.pivot_table(index=f"{fy}_q", columns=f"{fx}_q", values="pnl", aggfunc="count")

    fig, axes = plt.subplots(1, 2, figsize=(14, 5.5))
    ax = axes[0]
    im = ax.imshow(pivot.values, cmap="RdYlGn", aspect="auto", vmin=-50, vmax=50)
    ax.set_xticks(range(pivot.shape[1]))
    ax.set_xticklabels(pivot.columns)
    ax.set_yticks(range(pivot.shape[0]))
    ax.set_yticklabels(pivot.index)
    ax.set_xlabel(f"{fx} quintile (0=bas → 4=haut)")
    ax.set_ylabel(f"{fy} quintile (0=bas → 4=haut)")
    title = f"PnL moy : {fx} × {fy}"
    if ticker:
        title += f" ({ticker})"
    ax.set_title(title)
    for i in range(pivot.shape[0]):
        for j in range(pivot.shape[1]):
            v = pivot.values[i, j]
            if not np.isnan(v):
                ax.text(
                    j,
                    i,
                    f"{v:.0f}",
                    ha="center",
                    va="center",
                    color="white" if abs(v) > 30 else "black",
                    fontsize=10,
                    fontweight="bold",
                )
    fig.colorbar(im, ax=ax)

    ax2 = axes[1]
    cnt = counts.fillna(0).values.astype(int)
    im2 = ax2.imshow(cnt, cmap="Blues", aspect="auto")
    ax2.set_xticks(range(counts.shape[1]))
    ax2.set_xticklabels(counts.columns)
    ax2.set_yticks(range(counts.shape[0]))
    ax2.set_yticklabels(counts.index)
    ax2.set_xlabel(f"{fx} quintile")
    ax2.set_ylabel(f"{fy} quintile")
    ax2.set_title("Effectifs (n)")
    for i in range(cnt.shape[0]):
        for j in range(cnt.shape[1]):
            v = cnt[i, j]
            ax2.text(
                j,
                i,
                f"{v}",
                ha="center",
                va="center",
                color="white" if v > cnt.max() / 2 else "black",
                fontsize=10,
                fontweight="bold",
            )
    fig.colorbar(im2, ax=ax2)
    plt.tight_layout()
    plt.savefig(FIG_DIR / fname, dpi=110, bbox_inches="tight")
    plt.close()


heatmap_interaction("f1_bars", "f2_excursion_atr", "ds_opr_v5_heatmap_f1_f2_ALL.png")
heatmap_interaction("f2_excursion_atr", "f3_bars", "ds_opr_v5_heatmap_f2_f3_ALL.png")
heatmap_interaction("f1_bars", "f3_bars", "ds_opr_v5_heatmap_f1_f3_ALL.png")
for t in ["MES1", "NQ1", "YM1"]:
    heatmap_interaction("f1_bars", "f2_excursion_atr", f"ds_opr_v5_heatmap_f1_f2_{t}.png", ticker=t)


# ============================================================================
# 12. Modèles supervisés (sklearn) — triangulation
# ============================================================================
w("## 10. Modèles supervisés — triangulation des patterns")
w()
w(
    "Approche : confirmer (ou réfuter) les patterns identifiés en sections 2-9 "
    "via des modèles supervisés. Si Random Forest / Decision Tree / Logistic "
    "Regression convergent vers `f2_excursion_atr` comme feature dominante, "
    "le pattern est robuste."
)
w()

FEATURES_SUP = ["f1_bars", "f2_excursion_atr", "f3_bars"]


def supervised_analysis(df_sub, label):
    """Multi-model analysis with 5-fold CV."""
    sub = df_sub.dropna(subset=FEATURES_SUP + ["pnl"]).copy()
    if len(sub) < 100:
        return None

    X = sub[FEATURES_SUP].values
    y_cls = (sub["pnl"] > 0).astype(int).values
    y_reg = sub["pnl"].values

    baseline_acc = max(y_cls.mean(), 1 - y_cls.mean())

    # 1. Decision Tree (interpretable)
    dt = DecisionTreeClassifier(
        max_depth=4, min_samples_leaf=max(20, len(sub) // 40), random_state=42
    )
    dt_scores = cross_val_score(dt, X, y_cls, cv=5, scoring="accuracy")
    dt.fit(X, y_cls)
    tree_rules = export_text(dt, feature_names=FEATURES_SUP, decimals=2)

    # 2. Random Forest Classifier
    rf_cls = RandomForestClassifier(
        n_estimators=300, max_depth=6, min_samples_leaf=10, random_state=42, n_jobs=-1
    )
    rf_cls_scores = cross_val_score(rf_cls, X, y_cls, cv=5, scoring="accuracy")
    rf_cls.fit(X, y_cls)
    rf_cls_imp = pd.Series(rf_cls.feature_importances_, index=FEATURES_SUP).sort_values(
        ascending=False
    )

    # 3. Random Forest Regressor (predicting pnl)
    rf_reg = RandomForestRegressor(
        n_estimators=300, max_depth=6, min_samples_leaf=10, random_state=42, n_jobs=-1
    )
    rf_reg_scores = cross_val_score(rf_reg, X, y_reg, cv=5, scoring="r2")
    rf_reg.fit(X, y_reg)
    rf_reg_imp = pd.Series(rf_reg.feature_importances_, index=FEATURES_SUP).sort_values(
        ascending=False
    )

    # 4. Logistic Regression (linear)
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)
    lr = LogisticRegression(max_iter=1000, random_state=42)
    lr_scores = cross_val_score(lr, X_scaled, y_cls, cv=5, scoring="accuracy")
    lr.fit(X_scaled, y_cls)
    lr_coefs = pd.Series(lr.coef_[0], index=FEATURES_SUP).sort_values(key=abs, ascending=False)

    # 5. Permutation importance (model-agnostic, robust)
    perm = permutation_importance(rf_cls, X, y_cls, n_repeats=10, random_state=42, n_jobs=-1)
    perm_imp = pd.Series(perm.importances_mean, index=FEATURES_SUP).sort_values(ascending=False)
    perm_imp_std = pd.Series(perm.importances_std, index=FEATURES_SUP)

    return {
        "n": len(sub),
        "baseline_acc": baseline_acc,
        "dt_acc_mean": dt_scores.mean(),
        "dt_acc_std": dt_scores.std(),
        "tree_rules": tree_rules,
        "rf_cls_acc_mean": rf_cls_scores.mean(),
        "rf_cls_acc_std": rf_cls_scores.std(),
        "rf_cls_imp": rf_cls_imp,
        "rf_reg_r2_mean": rf_reg_scores.mean(),
        "rf_reg_r2_std": rf_reg_scores.std(),
        "rf_reg_imp": rf_reg_imp,
        "lr_acc_mean": lr_scores.mean(),
        "lr_coefs": lr_coefs,
        "perm_imp": perm_imp,
        "perm_imp_std": perm_imp_std,
    }


for ticker in ["ALL", "MES1", "NQ1", "YM1"]:
    w(f"### {ticker}")
    w()
    sub = df_filled if ticker == "ALL" else df_filled[df_filled["ticker"] == ticker]
    info = supervised_analysis(sub, ticker)
    if info is None:
        w("_n insuffisant_")
        continue

    w(
        f"**Dataset** : n={info['n']}, baseline accuracy (majority class) = {info['baseline_acc']:.3f}"
    )
    w()

    # Recap performance
    perf = pd.DataFrame(
        [
            {
                "Model": "Decision Tree (depth=4)",
                "CV accuracy": f"{info['dt_acc_mean']:.3f} ± {info['dt_acc_std']:.3f}",
                "Edge vs baseline": f"{info['dt_acc_mean'] - info['baseline_acc']:+.3f}",
            },
            {
                "Model": "Random Forest (cls)",
                "CV accuracy": f"{info['rf_cls_acc_mean']:.3f} ± {info['rf_cls_acc_std']:.3f}",
                "Edge vs baseline": f"{info['rf_cls_acc_mean'] - info['baseline_acc']:+.3f}",
            },
            {
                "Model": "Logistic Regression",
                "CV accuracy": f"{info['lr_acc_mean']:.3f}",
                "Edge vs baseline": f"{info['lr_acc_mean'] - info['baseline_acc']:+.3f}",
            },
            {
                "Model": "Random Forest (reg, R²)",
                "CV accuracy": f"{info['rf_reg_r2_mean']:.3f} ± {info['rf_reg_r2_std']:.3f}",
                "Edge vs baseline": "(R² vs 0)",
            },
        ]
    )
    w("**Performance des modèles (5-fold CV)** :")
    w()
    wt(perf)

    # Decision tree rules
    w("**Decision Tree — règles apprises (interprétation directe)** :")
    w()
    w("```")
    w(info["tree_rules"])
    w("```")
    w()

    # Importances - trois sources convergentes
    imp_table = pd.DataFrame(
        {
            "Feature": FEATURES_SUP,
            "RF Cls importance": [round(info["rf_cls_imp"].get(f, 0), 3) for f in FEATURES_SUP],
            "RF Reg importance": [round(info["rf_reg_imp"].get(f, 0), 3) for f in FEATURES_SUP],
            "Permutation importance": [round(info["perm_imp"].get(f, 0), 4) for f in FEATURES_SUP],
            "LogReg |coef|": [round(abs(info["lr_coefs"].get(f, 0)), 3) for f in FEATURES_SUP],
        }
    )
    w("**Importance des features (3 sources convergentes)** :")
    w()
    wt(imp_table)

    # Linear sign : direction de l'effet
    w("**Logistic Regression — coefficients (signe = direction de l'effet sur P(win))** :")
    w()
    coef_df = pd.DataFrame(
        {
            "Feature": FEATURES_SUP,
            "Coef (standardized)": [round(info["lr_coefs"].get(f, 0), 4) for f in FEATURES_SUP],
            "Direction": [
                "augmente P(win)" if info["lr_coefs"].get(f, 0) > 0 else "diminue P(win)"
                for f in FEATURES_SUP
            ],
        }
    )
    wt(coef_df)


# ============================================================================
# 13. Test du filtre data-driven sur les modèles
# ============================================================================
w("## 11. Validation supervisée — test du filtre f2_min_atr")
w()
w(
    "Hypothèse : le filtre `f2_min_atr ≥ 0.15` rejette les trades sous-performants. "
    "Vérifions via classification : un modèle peut-il prédire `pnl > 0` à partir de "
    "f2_excursion_atr seul ?"
)
w()


def f2_only_model(df_sub, label):
    sub = df_sub.dropna(subset=["f2_excursion_atr", "pnl"]).copy()
    if len(sub) < 100:
        return None
    X = sub[["f2_excursion_atr"]].values
    y = (sub["pnl"] > 0).astype(int).values

    # Simple shallow tree to find threshold
    dt = DecisionTreeClassifier(
        max_depth=2, min_samples_leaf=max(30, len(sub) // 30), random_state=42
    )
    scores = cross_val_score(dt, X, y, cv=5, scoring="accuracy")
    dt.fit(X, y)
    rules = export_text(dt, feature_names=["f2_excursion_atr"], decimals=3)

    # Test specific threshold 0.15
    above = sub[sub["f2_excursion_atr"] >= 0.15]
    below = sub[sub["f2_excursion_atr"] < 0.15]
    return {
        "n": len(sub),
        "baseline": max(y.mean(), 1 - y.mean()),
        "dt_acc": scores.mean(),
        "rules": rules,
        "above_0.15_n": len(above),
        "above_0.15_wr": (above["pnl"] > 0).mean(),
        "above_0.15_pnl_mean": above["pnl"].mean(),
        "below_0.15_n": len(below),
        "below_0.15_wr": (below["pnl"] > 0).mean(),
        "below_0.15_pnl_mean": below["pnl"].mean(),
    }


comparison_rows = []
for ticker in ["ALL", "MES1", "NQ1", "YM1"]:
    sub = df_filled if ticker == "ALL" else df_filled[df_filled["ticker"] == ticker]
    info = f2_only_model(sub, ticker)
    if info is None:
        continue
    w(f"### {ticker}")
    w()
    w(
        f"- Decision Tree (depth=2, f2 seul) — accuracy CV : {info['dt_acc']:.3f} (baseline {info['baseline']:.3f}, edge {info['dt_acc'] - info['baseline']:+.3f})"
    )
    w()
    w("Règle apprise par l'arbre (seuil que l'algorithme trouve sans qu'on lui dise) :")
    w("```")
    w(info["rules"])
    w("```")
    w()
    w("Test manuel du seuil 0.15 :")
    w()
    comparison_rows.append(
        {
            "ticker": ticker,
            "n_above_0.15": info["above_0.15_n"],
            "wr_above_0.15": f"{info['above_0.15_wr']:.1%}",
            "pnl_mean_above": round(info["above_0.15_pnl_mean"], 2),
            "n_below_0.15": info["below_0.15_n"],
            "wr_below_0.15": f"{info['below_0.15_wr']:.1%}",
            "pnl_mean_below": round(info["below_0.15_pnl_mean"], 2),
        }
    )

if comparison_rows:
    w("### Tableau de synthèse — seuil 0.15 ATR")
    w()
    wt(pd.DataFrame(comparison_rows))


# ============================================================================
# 14. Permutation test : la différence est-elle aléatoire ?
# ============================================================================
w("## 12. Test de permutation — significativité statistique du seuil F2=0.15")
w()
w(
    "Question : la différence de PnL moyen entre f2_atr ≥ 0.15 vs < 0.15 "
    "est-elle significative, ou pourrait-elle résulter du hasard ?"
)
w()
w(
    "Méthode : permuter aléatoirement les valeurs f2_excursion_atr 10 000 fois, "
    "recalculer la différence de PnL moyen, comparer à la valeur observée."
)
w()


def permutation_test(df_sub, n_perms=10000, threshold=0.15):
    sub = df_sub.dropna(subset=["f2_excursion_atr", "pnl"]).copy()
    if len(sub) < 100:
        return None
    above = sub[sub["f2_excursion_atr"] >= threshold]["pnl"]
    below = sub[sub["f2_excursion_atr"] < threshold]["pnl"]
    if len(above) < 30 or len(below) < 30:
        return None
    observed_diff = above.mean() - below.mean()

    # Permutation
    pooled = sub["pnl"].values.copy()
    flag = (sub["f2_excursion_atr"] >= threshold).values
    rng = np.random.default_rng(42)
    perm_diffs = np.empty(n_perms)
    for i in range(n_perms):
        perm = rng.permutation(pooled)
        diff = perm[flag].mean() - perm[~flag].mean()
        perm_diffs[i] = diff

    p_value = (np.abs(perm_diffs) >= np.abs(observed_diff)).mean()
    return {
        "observed_diff": observed_diff,
        "p_value": p_value,
        "n_above": len(above),
        "n_below": len(below),
        "perm_p5": np.percentile(perm_diffs, 5),
        "perm_p95": np.percentile(perm_diffs, 95),
        "mean_above": above.mean(),
        "mean_below": below.mean(),
    }


rows = []
for ticker in ["ALL", "MES1", "NQ1", "YM1"]:
    sub = df_filled if ticker == "ALL" else df_filled[df_filled["ticker"] == ticker]
    info = permutation_test(sub)
    if info is None:
        continue
    rows.append(
        {
            "ticker": ticker,
            "n_above": info["n_above"],
            "n_below": info["n_below"],
            "pnl_mean_above": round(info["mean_above"], 2),
            "pnl_mean_below": round(info["mean_below"], 2),
            "observed_diff": round(info["observed_diff"], 2),
            "perm_p5": round(info["perm_p5"], 2),
            "perm_p95": round(info["perm_p95"], 2),
            "p_value": f"{info['p_value']:.4f}",
            "significant_5pct": "✅" if info["p_value"] < 0.05 else "❌",
        }
    )

if rows:
    w("Résultats du test de permutation (seuil f2_atr=0.15, 10 000 permutations) :")
    w()
    wt(pd.DataFrame(rows))


# ============================================================================
# 15. Synthèse finale
# ============================================================================
w("## 13. Synthèse — patterns identifiés et règles candidates")
w()
w(
    "Cette section sera complétée à la lecture manuelle du rapport. Les sections "
    "précédentes contiennent les observations brutes — pas de conclusion automatique pour éviter le biais de surinterprétation."
)
w()
w("**À examiner attentivement** :")
w()
w("- Section 2 : signe et significativité des corrélations Pearson/Spearman par ticker")
w("- Section 3 : monotonie des déciles (un décile clairement à exclure ?)")
w("- Section 4 : seuils univariés qui maximisent PF — comparer à v5 actuel")
w(
    "- Section 5 : combinaisons bivariées — y a-t-il une cellule (tx_max, ty_max) qui domine clairement ?"
)
w("- Section 6 : pattern visuel sur les heatmaps — quintiles clairement perdants ?")
w(
    "- Section 7 : différence de moyenne F1/F2/F3 entre Best 20% et Worst 20% — si différence forte = pattern exploitable"
)
w("- Section 9 : comparaison brute des scénarios — quel scénario optimise PnL ET PF ?")
w()

# Save
REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
print(f"Rapport écrit : {REPORT_PATH}")
print(f"Figures dans : {FIG_DIR}")
