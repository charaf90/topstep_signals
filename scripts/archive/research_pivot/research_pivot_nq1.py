"""
Recherche pivots NQ1 M5 — feature discovery via ML.

Question scientifique : peut-on prédire (au sens précision) qu'une barre M5
sera identifiée comme pivot local par argrelextrema(Close, order=5) ?

⚠ Causalité : argrelextrema regarde 5 barres avant ET après. Le label à t
utilise donc du futur (t+1..t+5). Les FEATURES restent strictement causales
(info ≤ t). On évalue la prédictibilité, pas la tradabilité — un signal
serait techniquement actionable à t+6.

Périmètre : NQ1 only, M5, ~7 mois (2025-10-19 → 2026-05-15).
Output : output/pivot_research/{dataset.csv, label_diagnostics.md,
                                 precision_recall_OOS.png,
                                 feature_importance.png, rapport.md}

Aucune modification de core/, broker/, strategies/, config.py.
"""

from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.signal import argrelextrema
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, precision_recall_curve
from sklearn.preprocessing import StandardScaler

warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)
np.random.seed(42)

# ────────────────────────────────────────────────────────────────────────────
# Setup
# ────────────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import MACRO_EVENT_DATES  # noqa: E402
from core.data import load_csv  # noqa: E402

OUT_ROOT = ROOT / "output" / "pivot_research"

IS_END = pd.Timestamp("2026-02-15")

# Valeurs initialisées dans main() — restent globales pour rester accessibles
# aux helpers sans tout refactorer en arguments.
TICKER = "NQ1"
PIVOT_ORDER = 5
OUT_DIR = OUT_ROOT
DATA_CSV = ROOT / "data" / "NQ1_data_m5.csv"

NY = ZoneInfo("America/New_York")
MACRO_DATES = set(pd.to_datetime(MACRO_EVENT_DATES).date)


# ────────────────────────────────────────────────────────────────────────────
# 1. Génération des labels
# ────────────────────────────────────────────────────────────────────────────
def label_pivots(df: pd.DataFrame, order: int) -> pd.DataFrame:
    """Pose is_pivot_high / is_pivot_low / is_pivot_any.

    `argrelextrema` est l'ORACLE — utilise le futur, c'est attendu. Les
    features calculées plus loin n'utiliseront jamais ces colonnes.
    """
    close = df["close"].to_numpy()
    high_idx = argrelextrema(close, np.greater, order=order)[0]
    low_idx = argrelextrema(close, np.less, order=order)[0]
    df["is_pivot_high"] = 0
    df["is_pivot_low"] = 0
    df.iloc[high_idx, df.columns.get_loc("is_pivot_high")] = 1
    df.iloc[low_idx, df.columns.get_loc("is_pivot_low")] = 1
    df["is_pivot_any"] = ((df["is_pivot_high"] + df["is_pivot_low"]) > 0).astype(int)
    return df


# ────────────────────────────────────────────────────────────────────────────
# 2. Feature engineering (toutes causales)
# ────────────────────────────────────────────────────────────────────────────
def _ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False).mean()


def _atr(df: pd.DataFrame, n: int) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def _adx(df: pd.DataFrame, n: int) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    up = high.diff()
    down = -low.diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr = pd.concat(
        [high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()],
        axis=1,
    ).max(axis=1)
    atr_n = tr.ewm(alpha=1 / n, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / atr_n
    minus_di = (
        100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / atr_n
    )
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean()


def _past_pivot_density(
    close: np.ndarray,
    atr: np.ndarray,
    lookback: int = 200,
    recent_gap: int = 10,
    band_atr: float = 2.0,
    order: int = 5,
) -> np.ndarray:
    """Pour chaque t, compte les pivots détectés strictement avant
    (t - recent_gap), à une distance ≤ band_atr * ATR(t) de Close(t).

    Strictement causal : on n'utilise que des indices < t - recent_gap, donc
    leur statut de pivot est déjà confirmé.
    """
    n = len(close)
    density = np.zeros(n, dtype=np.float32)
    # On précalcule un masque de pivots détectés sur la série (peu importe le
    # futur ici car on n'utilisera l'info qu'à des t suffisamment ultérieurs).
    high_idx = set(argrelextrema(close, np.greater, order=order)[0].tolist())
    low_idx = set(argrelextrema(close, np.less, order=order)[0].tolist())
    pivots = sorted(high_idx | low_idx)
    pivots = np.array(pivots, dtype=np.int64)
    if len(pivots) == 0:
        return density
    pivot_prices = close[pivots]
    for t in range(n):
        cutoff = t - recent_gap
        if cutoff <= 0 or atr[t] != atr[t] or atr[t] <= 0:
            continue
        # pivots avant cutoff
        mask = pivots < cutoff
        if not mask.any():
            continue
        # on borne aussi le passé (fenêtre glissante)
        lower_bound = max(0, cutoff - lookback)
        win_mask = mask & (pivots >= lower_bound)
        if not win_mask.any():
            continue
        prices_in_win = pivot_prices[win_mask]
        band = band_atr * atr[t]
        density[t] = float(np.sum(np.abs(prices_in_win - close[t]) <= band))
    return density


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """Calcule toutes les features causales. Pose NaN sur la zone de
    warm-up — on filtrera après.
    """
    out = df.copy()
    c, h, l, v = out["close"], out["high"], out["low"], out["volume"]

    # ── Tendance / momentum ──
    ema9 = _ema(c, 9)
    ema21 = _ema(c, 21)
    ema50 = _ema(c, 50)
    out["ema9_slope"] = ema9.diff(1)
    out["ema21_slope"] = ema21.diff(1)
    out["ema50_slope"] = ema50.diff(1)
    out["roc_5"] = c.pct_change(5)
    out["roc_20"] = c.pct_change(20)
    out["roc_50"] = c.pct_change(50)
    out["adx_14"] = _adx(out, 14)

    # ── Volatilité ──
    atr_14 = _atr(out, 14)
    atr_5 = _atr(out, 5)
    atr_50 = _atr(out, 50)
    out["atr_14"] = atr_14
    out["atr_ratio_short_long"] = atr_5 / atr_50.replace(0, np.nan)
    sma20 = c.rolling(20).mean()
    std20 = c.rolling(20).std()
    out["bb_width"] = (4 * std20) / sma20  # 2*std haut + 2*std bas / sma
    out["dist_close_ema21_atr"] = (c - ema21) / atr_14.replace(0, np.nan)

    bar_range = (h - l).replace(0, np.nan)
    out["range_atr_ratio"] = bar_range / atr_14.replace(0, np.nan)

    # ── Microstructure ──
    body = (c - out["open"]).abs()
    out["body_range_ratio"] = body / bar_range
    out["upper_wick_ratio"] = (h - out[["open", "close"]].max(axis=1)) / bar_range
    out["lower_wick_ratio"] = (out[["open", "close"]].min(axis=1) - l) / bar_range
    vol_sma20 = v.rolling(20).mean()
    out["vol_rel"] = v / vol_sma20.replace(0, np.nan)

    # ── Position relative (clé pour le concept "zone") ──
    rolling_max_20 = c.rolling(20).max()
    rolling_min_20 = c.rolling(20).min()
    out["dist_to_max20_atr"] = (rolling_max_20 - c) / atr_14.replace(0, np.nan)
    out["dist_to_min20_atr"] = (c - rolling_min_20) / atr_14.replace(0, np.nan)

    close_np = c.to_numpy()
    atr_np = atr_14.to_numpy()
    out["past_pivot_density_2atr"] = _past_pivot_density(
        close_np, atr_np, lookback=200, recent_gap=10, band_atr=2.0, order=PIVOT_ORDER
    )
    out["past_pivot_density_1atr"] = _past_pivot_density(
        close_np, atr_np, lookback=200, recent_gap=10, band_atr=1.0, order=PIVOT_ORDER
    )

    # ── Séquence ──
    for lag in (1, 2, 3, 5, 10):
        out[f"ret_lag_{lag}"] = c.pct_change(lag)
    up_bar = (c > c.shift(1)).astype(int)
    out["up_bars_last_10"] = up_bar.rolling(10).sum()

    # ── Timing (NY, DST-aware) ──
    idx_utc = pd.to_datetime(out.index)
    if idx_utc.tz is None:
        idx_ny = idx_utc.tz_localize("UTC").tz_convert(NY)
    else:
        idx_ny = idx_utc.tz_convert(NY)
    out["hour_ny"] = idx_ny.hour.astype(np.int16)
    out["minute_ny"] = idx_ny.minute.astype(np.int16)
    out["dow"] = idx_ny.dayofweek.astype(np.int16)
    # Distance à 09:30 NY en barres M5 (négatif avant ouverture, positif après)
    minutes_since_midnight = idx_ny.hour * 60 + idx_ny.minute
    out["bars_since_open"] = ((minutes_since_midnight - (9 * 60 + 30)) // 5).astype(np.int32)
    out["is_macro_day"] = pd.Series(
        [d.date() in MACRO_DATES for d in idx_ny], index=out.index
    ).astype(np.int8)

    return out


# ────────────────────────────────────────────────────────────────────────────
# 3. Diagnostic du label
# ────────────────────────────────────────────────────────────────────────────
def write_label_diagnostics(df: pd.DataFrame, atr_col: str = "atr_14") -> dict:
    lines = []
    w = lines.append
    n = len(df)
    n_high = int(df["is_pivot_high"].sum())
    n_low = int(df["is_pivot_low"].sum())
    n_any = int(df["is_pivot_any"].sum())
    base_rate = n_any / n if n else 0.0

    w(f"# Diagnostic du label pivot — {TICKER} M5 (order={PIVOT_ORDER})\n")
    w(f"- Barres totales : **{n:,}**")
    w(f"- Pivots hauts : **{n_high:,}** ({n_high / n:.2%})")
    w(f"- Pivots bas : **{n_low:,}** ({n_low / n:.2%})")
    w(f"- Pivots any : **{n_any:,}** (base rate = **{base_rate:.2%}**)")
    w(f"- Order argrelextrema : **{PIVOT_ORDER}**\n")

    # Base rate par heure NY
    w("## Base rate par heure NY\n")
    by_hour = df.groupby("hour_ny")["is_pivot_any"].agg(["mean", "sum", "count"])
    by_hour.columns = ["base_rate", "n_pivots", "n_bars"]
    by_hour["base_rate"] = by_hour["base_rate"].map(lambda x: f"{x:.2%}")
    w(by_hour.to_markdown())
    w("")

    # Distribution distance entre pivots successifs
    pivot_idx = df.index[df["is_pivot_any"] == 1]
    if len(pivot_idx) > 1:
        gaps = np.diff(pivot_idx.values).astype("timedelta64[m]").astype(int)
        w("## Distance entre pivots successifs (minutes)\n")
        w(f"- Médiane : **{int(np.median(gaps))} min** ({int(np.median(gaps) / 5)} barres M5)")
        w(f"- Moyenne : **{int(np.mean(gaps))} min**")
        w(
            f"- P10 / P90 : **{int(np.percentile(gaps, 10))} / {int(np.percentile(gaps, 90))} min**\n"
        )

    # Densité dans une zone
    if "past_pivot_density_2atr" in df.columns:
        d = df["past_pivot_density_2atr"].replace([np.inf, -np.inf], np.nan).dropna()
        w("## Densité de pivots passés dans ±2·ATR autour de Close\n")
        w(f"- Médiane : **{d.median():.1f}**")
        w(
            f"- P75 / P90 / Max : **{d.quantile(0.75):.0f} / {d.quantile(0.9):.0f} / {d.max():.0f}**\n"
        )

        # Base rate conditionnel à la densité
        df_d = df.copy()
        df_d["dens_bucket"] = pd.qcut(d, q=5, duplicates="drop")
        rates = df_d.groupby("dens_bucket", observed=True)["is_pivot_any"].agg(["mean", "count"])
        rates.columns = ["base_rate", "n_bars"]
        rates["base_rate"] = rates["base_rate"].map(lambda x: f"{x:.2%}")
        w("### Base rate conditionnel à la densité de pivots passés\n")
        w(rates.to_markdown())
        w("")

    (OUT_DIR / "label_diagnostics.md").write_text("\n".join(lines), encoding="utf-8")
    return {"n": n, "n_any": n_any, "base_rate": base_rate}


# ────────────────────────────────────────────────────────────────────────────
# 4. Leak check
# ────────────────────────────────────────────────────────────────────────────
def leak_check(
    df: pd.DataFrame,
    feature_cols: list[str],
    label_col: str = "is_pivot_any",
    threshold: float = 0.5,
) -> list[tuple[str, float]]:
    """Liste les features dont |Pearson| > threshold avec le label. Si non
    vide → leak suspect, à investiguer.
    """
    suspects = []
    for c in feature_cols:
        s = df[[c, label_col]].dropna()
        if len(s) < 100:
            continue
        corr = s[c].corr(s[label_col])
        if pd.notna(corr) and abs(corr) > threshold:
            suspects.append((c, float(corr)))
    return suspects


# ────────────────────────────────────────────────────────────────────────────
# 5. Pipeline ML
# ────────────────────────────────────────────────────────────────────────────
def precision_at_recall(y_true: np.ndarray, y_score: np.ndarray, target_recall: float) -> float:
    p, r, _ = precision_recall_curve(y_true, y_score)
    # p et r sont décroissants/croissants de manière inversée — on cherche le
    # max de précision parmi les points où recall >= target.
    mask = r >= target_recall
    if not mask.any():
        return float("nan")
    return float(p[mask].max())


def train_and_eval(X_is, y_is, X_oos, y_oos, feature_names, base_rate_oos):
    results = {}

    # Logistic
    scaler = StandardScaler()
    X_is_s = scaler.fit_transform(X_is)
    X_oos_s = scaler.transform(X_oos)
    logit = LogisticRegression(max_iter=2000, class_weight="balanced", random_state=42)
    logit.fit(X_is_s, y_is)
    p_logit = logit.predict_proba(X_oos_s)[:, 1]
    results["logistic"] = _scores(y_oos, p_logit, base_rate_oos)

    # Random Forest
    rf = RandomForestClassifier(
        n_estimators=500,
        max_depth=8,
        class_weight="balanced",
        random_state=42,
        n_jobs=-1,
        min_samples_leaf=20,
    )
    rf.fit(X_is, y_is)
    p_rf = rf.predict_proba(X_oos)[:, 1]
    results["random_forest"] = _scores(y_oos, p_rf, base_rate_oos)

    # HistGradientBoosting
    hgb = HistGradientBoostingClassifier(
        max_iter=300,
        learning_rate=0.05,
        max_depth=6,
        class_weight="balanced",
        random_state=42,
    )
    hgb.fit(X_is, y_is)
    p_hgb = hgb.predict_proba(X_oos)[:, 1]
    results["hist_gbm"] = _scores(y_oos, p_hgb, base_rate_oos)

    return results, {"logistic": p_logit, "random_forest": p_rf, "hist_gbm": p_hgb}, hgb, rf


def _scores(y_true, y_score, base_rate):
    return {
        "pr_auc": float(average_precision_score(y_true, y_score)),
        "prec_at_recall_5": precision_at_recall(y_true, y_score, 0.05),
        "prec_at_recall_10": precision_at_recall(y_true, y_score, 0.10),
        "prec_at_recall_20": precision_at_recall(y_true, y_score, 0.20),
        "base_rate": base_rate,
    }


# ────────────────────────────────────────────────────────────────────────────
# 6. Plotting
# ────────────────────────────────────────────────────────────────────────────
def plot_pr_curves(y_oos, probas: dict, base_rate, path: Path):
    fig, ax = plt.subplots(figsize=(8, 6))
    for name, p in probas.items():
        prec, rec, _ = precision_recall_curve(y_oos, p)
        ap = average_precision_score(y_oos, p)
        ax.plot(rec, prec, label=f"{name} (PR-AUC={ap:.3f})", linewidth=1.5)
    ax.axhline(
        base_rate, color="gray", linestyle="--", alpha=0.6, label=f"base rate OOS ({base_rate:.2%})"
    )
    ax.set_xlabel("Recall")
    ax.set_ylabel("Precision")
    ax.set_title(f"Courbes Précision-Recall OOS — {TICKER} M5 pivots (order={PIVOT_ORDER})")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, max(1.0, base_rate * 5))
    ax.legend(loc="upper right")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def plot_feature_importance(model, X_oos, y_oos, feature_names, path: Path, top_n: int = 15):
    print("  • Permutation importance (peut prendre 1-2 min)…")
    pi = permutation_importance(
        model,
        X_oos,
        y_oos,
        n_repeats=5,
        random_state=42,
        n_jobs=-1,
        scoring="average_precision",
    )
    imp = pd.DataFrame({"feature": feature_names, "importance": pi.importances_mean})
    imp = imp.sort_values("importance", ascending=False).head(top_n)
    fig, ax = plt.subplots(figsize=(8, max(4, top_n * 0.3)))
    ax.barh(imp["feature"][::-1], imp["importance"][::-1], color="steelblue")
    ax.set_xlabel("Δ PR-AUC (permutation)")
    ax.set_title(f"Top {top_n} features — permutation importance OOS")
    ax.grid(alpha=0.3, axis="x")
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return imp


# ────────────────────────────────────────────────────────────────────────────
# 7. Rapport final
# ────────────────────────────────────────────────────────────────────────────
def write_report(
    stats_label, results, imp_df, leak_suspects, n_is, n_oos, is_start, is_end, oos_start, oos_end
):
    lines = []
    w = lines.append
    base_rate = stats_label["base_rate"]

    w(f"# Recherche pivots {TICKER} M5 — rapport\n")
    w(f"**Ticker** : {TICKER} | **Timeframe** : M5 | **argrelextrema order** : {PIVOT_ORDER}\n")
    w("## 1. Setup\n")
    w(f"- Données : `data/{TICKER}_data_m5.csv` ({stats_label['n']:,} barres)")
    w(f"- IS : {is_start} → {is_end} ({n_is:,} barres)")
    w(f"- OOS : {oos_start} → {oos_end} ({n_oos:,} barres)")
    w(f"- Base rate `is_pivot_any` global : **{base_rate:.2%}**")
    w("- Label = argrelextrema(Close, order=5) — non-causal par construction (oracle)")
    w("- Features = strictement causales (info ≤ t)\n")

    w("## 2. Leak check\n")
    if not leak_suspects:
        w("✅ Aucune feature avec |Pearson| > 0.5 vs label.\n")
    else:
        w("⚠️ Features suspectes (|corr| > 0.5) :\n")
        for name, c in leak_suspects:
            w(f"- `{name}` : {c:+.3f}")
        w("")

    w("## 3. Performance OOS des modèles\n")
    rows = []
    for name, sc in results.items():
        rows.append(
            {
                "modèle": name,
                "PR-AUC": f"{sc['pr_auc']:.3f}",
                "prec@recall=5%": f"{sc['prec_at_recall_5']:.2%}",
                "prec@recall=10%": f"{sc['prec_at_recall_10']:.2%}",
                "prec@recall=20%": f"{sc['prec_at_recall_20']:.2%}",
            }
        )
    w(pd.DataFrame(rows).to_markdown(index=False))
    w(f"\nBase rate OOS de référence : **{base_rate:.2%}**")
    w("- Lift = précision / base_rate. Lift > 1 = mieux que le hasard.\n")

    # Meilleur modèle = meilleur PR-AUC
    best_name = max(results.keys(), key=lambda k: results[k]["pr_auc"])
    best = results[best_name]
    w(f"**Meilleur modèle** : `{best_name}` (PR-AUC = {best['pr_auc']:.3f})\n")

    w("## 4. Top features (permutation importance OOS)\n")
    imp_md = imp_df.copy()
    imp_md["importance"] = imp_md["importance"].map(lambda x: f"{x:+.4f}")
    w(imp_md.to_markdown(index=False))
    w("")

    # ────────── Verdict ──────────
    best_prec_r10 = best["prec_at_recall_10"]
    lift_r10 = best_prec_r10 / base_rate if base_rate > 0 else 0
    w("## 5. Verdict\n")
    w(
        f"Précision @ recall=10 % du meilleur modèle : **{best_prec_r10:.2%}** "
        f"(lift = ×{lift_r10:.2f}).\n"
    )
    if best_prec_r10 > 0.50:
        verdict = "🟢 **PROMETTEUR**"
        explication = (
            "Précision > 50 % à recall 10 % — concept solide. "
            "Étape suivante : formaliser un label causal (ex: 'pivot "
            "confirmé à t' prédit à t+5), puis lancer `@new-strategy` ou "
            "`@quant MODE: discover` pour transformer en signal trading."
        )
    elif best_prec_r10 > 0.30:
        verdict = "🟡 **INTÉRESSANT**"
        explication = (
            "Précision 30-50 % à recall 10 % — features ont du signal. "
            "Étape suivante : présenter le rapport à `@quant MODE: discover` "
            "pour test comme filtre sur OPR/Fib."
        )
    elif best_prec_r10 > 1.5 * base_rate:
        verdict = "🟠 **LIMITE**"
        explication = (
            "Précision modeste mais lift > 1.5×. Insuffisant pour une "
            "stratégie pure ; envisager comme feature parmi d'autres "
            "dans un modèle plus large."
        )
    else:
        verdict = "🔴 **INUTILE**"
        explication = (
            "Précision ≤ 1.5× base rate. Le concept de pivot "
            "argrelextrema sur Close M5 NQ1 n'est pas prédictible avec "
            "ces features. Pivots ressemblent à du bruit conditionnel."
        )
    w(f"### {verdict}\n")
    w(explication + "\n")

    w("## 6. Caveats\n")
    w(
        "- Label non-causal : un signal posé à t ne peut être tradé qu'à t+6 (réalité physique). "
        "Métriques affichées surévaluent légèrement la tradabilité."
    )
    w(
        "- `past_pivot_density_*` utilise un cutoff t-10 et un lookback=200 — strictement "
        "causal mais à valider visuellement si le concept est poussé en stratégie."
    )
    w(
        "- Une seule fenêtre OOS (~3 mois). Walk-forward multi-fenêtre recommandé avant tout commit "
        "vers une stratégie."
    )
    w("- Aucun tuning d'hyperparamètres — résultats indicatifs.")
    w("")

    (OUT_DIR / "rapport.md").write_text("\n".join(lines), encoding="utf-8")
    return verdict


# ────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--order", type=int, default=5, help="Ordre argrelextrema (défaut: 5)")
    parser.add_argument(
        "--ticker",
        type=str,
        default="NQ1",
        help="Ticker (défaut: NQ1). Doit avoir data/<TICKER>_data_m5.csv",
    )
    args = parser.parse_args()

    global PIVOT_ORDER, OUT_DIR, TICKER, DATA_CSV
    PIVOT_ORDER = args.order
    TICKER = args.ticker
    DATA_CSV = ROOT / "data" / f"{TICKER}_data_m5.csv"
    OUT_DIR = OUT_ROOT / TICKER / f"order_{PIVOT_ORDER}"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if not DATA_CSV.exists():
        print(f"⚠ Fichier introuvable : {DATA_CSV}")
        sys.exit(1)
    print(f"▸ Run ticker={TICKER} order={PIVOT_ORDER} | outputs → {OUT_DIR}")
    print(f"▸ Chargement {DATA_CSV}…")
    df = load_csv(str(DATA_CSV))
    print(f"  • {len(df):,} barres ({df.index[0]} → {df.index[-1]})")

    print(f"▸ Labels argrelextrema(order={PIVOT_ORDER})…")
    df = label_pivots(df, order=PIVOT_ORDER)

    print("▸ Features (~30)…")
    df = build_features(df)

    feature_cols = [
        # tendance / momentum
        "ema9_slope",
        "ema21_slope",
        "ema50_slope",
        "roc_5",
        "roc_20",
        "roc_50",
        "adx_14",
        # volatilité
        "atr_14",
        "atr_ratio_short_long",
        "bb_width",
        "dist_close_ema21_atr",
        "range_atr_ratio",
        # microstructure
        "body_range_ratio",
        "upper_wick_ratio",
        "lower_wick_ratio",
        "vol_rel",
        # position relative
        "dist_to_max20_atr",
        "dist_to_min20_atr",
        "past_pivot_density_2atr",
        "past_pivot_density_1atr",
        # séquence
        "ret_lag_1",
        "ret_lag_2",
        "ret_lag_3",
        "ret_lag_5",
        "ret_lag_10",
        "up_bars_last_10",
        # timing
        "hour_ny",
        "minute_ny",
        "dow",
        "bars_since_open",
        "is_macro_day",
    ]
    print(f"  • {len(feature_cols)} features")

    # Drop NaN sur les features + label, après warm-up
    df_clean = df.dropna(subset=feature_cols + ["is_pivot_any"]).copy()
    print(f"  • {len(df_clean):,} barres après dropna (warm-up)")

    print("▸ Diagnostic label…")
    stats_label = write_label_diagnostics(df_clean)
    print(f"  • base rate = {stats_label['base_rate']:.2%}")

    print("▸ Leak check…")
    suspects = leak_check(df_clean, feature_cols)
    if suspects:
        print(f"  ⚠ {len(suspects)} suspects : {suspects}")
    else:
        print("  ✅ pas de leak suspect")

    # Save dataset (CSV, pyarrow non dispo)
    print("▸ Sauvegarde dataset…")
    df_clean.to_csv(OUT_DIR / f"dataset_{TICKER.lower()}_m5.csv")

    # Split
    is_end = IS_END
    if df_clean.index[-1].tz is not None:
        is_end = is_end.tz_localize(df_clean.index.tz) if is_end.tz is None else is_end
    df_is = df_clean[df_clean.index <= is_end]
    df_oos = df_clean[df_clean.index > is_end]
    print(f"  • IS : {len(df_is):,} | OOS : {len(df_oos):,}")
    if len(df_oos) < 1000:
        print("⚠ OOS trop court, arrêt.")
        return

    X_is = df_is[feature_cols].to_numpy(dtype=np.float32)
    y_is = df_is["is_pivot_any"].to_numpy(dtype=np.int32)
    X_oos = df_oos[feature_cols].to_numpy(dtype=np.float32)
    y_oos = df_oos["is_pivot_any"].to_numpy(dtype=np.int32)
    base_rate_oos = y_oos.mean()

    print(f"▸ Entraînement 3 modèles (IS={len(X_is):,}, OOS={len(X_oos):,})…")
    results, probas, hgb, rf = train_and_eval(X_is, y_is, X_oos, y_oos, feature_cols, base_rate_oos)
    for name, sc in results.items():
        print(
            f"  • {name:18s}  PR-AUC={sc['pr_auc']:.3f}  "
            f"P@R5%={sc['prec_at_recall_5']:.2%}  "
            f"P@R10%={sc['prec_at_recall_10']:.2%}  "
            f"P@R20%={sc['prec_at_recall_20']:.2%}"
        )

    print("▸ Plots…")
    plot_pr_curves(y_oos, probas, base_rate_oos, OUT_DIR / "precision_recall_OOS.png")
    # Importance sur le meilleur modèle non-linéaire (RF, plus rapide que HGB ici)
    imp_df = plot_feature_importance(
        rf,
        X_oos,
        y_oos,
        feature_cols,
        OUT_DIR / "feature_importance.png",
        top_n=15,
    )

    print("▸ Rapport markdown…")
    verdict = write_report(
        stats_label,
        results,
        imp_df,
        suspects,
        len(df_is),
        len(df_oos),
        df_is.index[0].date(),
        df_is.index[-1].date(),
        df_oos.index[0].date(),
        df_oos.index[-1].date(),
    )

    print("\n══════════════════════════════════════════════")
    print(f"  VERDICT : {verdict}")
    print("══════════════════════════════════════════════")
    print(f"  Outputs → {OUT_DIR}")


if __name__ == "__main__":
    main()
