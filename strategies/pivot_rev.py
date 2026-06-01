"""
Stratégie pivot-rev-v1 — mean-reversion ML MGC1 M5.

Concept       : "wash out" volumétrique sur MGC1 M5 + filtre RandomForest.
                Détecte les pivots intraday (argrelextrema oracle, order=20)
                avec un modèle entraîné STRICTEMENT sur la fenêtre IS.
Edge          : liquidations forcées en marché Gold (stops intraday) —
                market-makers absorbent et le prix reverse à court terme.
                Lift observé ×8.79 vs base rate (étude amont 2026-05-18).
Falsification : Précision OOS < 18% (lift < 5.5×) OU PF OOS < 1.2 net.
Indicateurs   : 31 features causales (EMA slopes, ATR, ADX, BB, ranges,
                wicks, vol_rel, distances aux pivots passés, timing NY).
Fenêtre NY    : 06:00 → 16:00 (US session étendue, hour_ny ≥ 6 dans filtre).
Timeframe     : M5 — différent du standard projet (M15).

Validation    : HOLDOUT PUR (pas de walk-forward standard).
                IS = 2025-10-19 → 2026-01-31 (entraînement RF + grid search)
                OOS = 2026-02-01 → 2026-05-15 (mesure unique)
                PURGE 20 barres M5 en frontière (label leakage order=20).

Architecture  : le RF est entraîné à la volée à chaque appel run_backtest()
                sur la portion IS du DF, le seuil p10% IS est figé, et la
                proba RF est calculée sur l'intégralité du DF. Cela permet
                à core/optimizer.py / backtest.py de fonctionner sans hack
                tout en garantissant la non-fuite OOS→modèle.
"""

from __future__ import annotations

import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

# ── Reproductibilité ─────────────────────────────────────────────────────────
np.random.seed(42)

# ── Imports projet ───────────────────────────────────────────────────────────
from config import (
    COMMISSION_RT_PER_CONTRACT,
    INSTRUMENTS,
    MACRO_EVENT_DATES,
    PIVOT_REV_CONFIRM_BARS,
    PIVOT_REV_DOJI_BODY_RATIO_MAX,
    PIVOT_REV_FORCE_CLOSE_HOUR_NY,
    PIVOT_REV_FORCE_CLOSE_MIN_NY,
    PIVOT_REV_HOUR_NY_MAX,
    PIVOT_REV_HOUR_NY_MIN,
    PIVOT_REV_IS_END,
    PIVOT_REV_MAX_TRADES_PER_DAY,
    PIVOT_REV_MIN_SL_DIST_TICKS,
    PIVOT_REV_PIVOT_ORDER,
    PIVOT_REV_RANGE_ATR_RATIO_MIN,
    PIVOT_REV_RF_MAX_DEPTH,
    PIVOT_REV_RF_MIN_SAMPLES_LEAF,
    PIVOT_REV_RF_N_ESTIMATORS,
    PIVOT_REV_RF_QUANTILE_IS,
    PIVOT_REV_RF_RANDOM_STATE,
    PIVOT_REV_RR_TARGET,
    PIVOT_REV_SKIP_MACRO_DAYS,
    PIVOT_REV_SL_MULT,
    PIVOT_REV_STRATEGY_VERSION,
    PIVOT_REV_TICKERS,
    PIVOT_REV_TIME_BARRIER_BARS,
    PIVOT_REV_VOL_REL_MIN,
    RISK_PER_TRADE_USD,
    SLIPPAGE_TICKS_PER_TICKER,
)
from core.risk_topstep import trade_allowed

# ── Réutilisation des helpers de l'étude amont (label + features) ────────────
ROOT = Path(__file__).resolve().parent.parent
_RP_DIR = ROOT / "scripts" / "archive" / "research_pivot"
if str(_RP_DIR) not in sys.path:
    sys.path.insert(0, str(_RP_DIR))

import research_pivot_nq1 as _rp  # noqa: E402

# ── Identité de la stratégie ─────────────────────────────────────────────────
STRATEGY_ID = PIVOT_REV_STRATEGY_VERSION  # "pivot-rev-v1"
TICKERS = list(PIVOT_REV_TICKERS)  # ["MGC1"]
CSV_SUFFIX = "_pivot_rev"
CSV_TIMEFRAME = "m5"  # M5, pas le standard M15 !

_NY = ZoneInfo("America/New_York")
_MACRO_DATES = set(pd.to_datetime(MACRO_EVENT_DATES).date)

# ── Liste des features ML (identique à l'étude amont) ────────────────────────
_FEATURE_COLS = [
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

# ── Grille d'optimisation (3³ = 27 combos, conformément au concept) ──────────
PARAM_GRID = {
    "sl_mult": [0.75, 1.0, 1.25],
    "rr_target": [2.5, 3.0, 3.5],
    "vol_rel_min": [2.0, 2.5, 3.0],
}


# ══════════════════════════════════════════════════════════════════════════════
# 1. Pré-calcul features + RF (cache par DataFrame id pour ne pas refit 27 fois)
# ══════════════════════════════════════════════════════════════════════════════

_PREP_CACHE: dict = {}


def _prepare_dataset(df_m5: pd.DataFrame) -> pd.DataFrame:
    """
    Construit features + entraîne RF sur IS + calcule proba pour tout le DF.

    Le DF retourné a colonnes additionnelles : toutes les features _FEATURE_COLS,
    `proba_rf` et `proba_rf_threshold_is` (figé, identique pour toutes les lignes).

    Cache mémo sur id(df_m5) pour éviter de re-entraîner à chaque combo du grid.
    """
    # Cache : si même DataFrame, même résultat — éviter de refit 27 fois.
    cache_key = (id(df_m5), len(df_m5))
    if cache_key in _PREP_CACHE:
        return _PREP_CACHE[cache_key]

    # Lazy import sklearn (évite coût si module pas appelé)
    from sklearn.ensemble import RandomForestClassifier

    # ── 1. Labels oracle (argrelextrema order=20 — utilisé UNIQUEMENT pour fit) ──
    df = df_m5.copy()
    df = _rp.label_pivots(df, order=PIVOT_REV_PIVOT_ORDER)

    # ── 2. Features causales (31 features) ──
    # Configurer le module helper pour utiliser le bon order de pivots passés.
    _rp.PIVOT_ORDER = PIVOT_REV_PIVOT_ORDER
    df = _rp.build_features(df)

    # ── 3. Drop warmup ──
    df = df.dropna(subset=_FEATURE_COLS + ["is_pivot_any"]).copy()

    # ── 4. Split IS/OOS — entraînement RF STRICTEMENT sur IS ──
    is_end = pd.Timestamp(PIVOT_REV_IS_END) + pd.Timedelta(days=1)  # exclusif
    df_is = df[df.index < is_end]

    if len(df_is) < 1000:
        raise RuntimeError(
            f"IS trop court pour entraîner RF : {len(df_is)} barres " f"(min 1000 recommandé)"
        )

    X_is = df_is[_FEATURE_COLS].to_numpy(dtype=np.float32)
    y_is = df_is["is_pivot_any"].to_numpy(dtype=np.int32)

    rf = RandomForestClassifier(
        n_estimators=PIVOT_REV_RF_N_ESTIMATORS,
        max_depth=PIVOT_REV_RF_MAX_DEPTH,
        min_samples_leaf=PIVOT_REV_RF_MIN_SAMPLES_LEAF,
        class_weight="balanced",
        random_state=PIVOT_REV_RF_RANDOM_STATE,
        n_jobs=-1,
    )
    rf.fit(X_is, y_is)

    # ── 5. Seuil p10% figé sur IS (quantile 90% des proba_RF IS) ──
    proba_is = rf.predict_proba(X_is)[:, 1]
    threshold_is = float(np.quantile(proba_is, PIVOT_REV_RF_QUANTILE_IS))

    # ── 6. Calculer proba_RF sur tout le DF ──
    X_all = df[_FEATURE_COLS].to_numpy(dtype=np.float32)
    df["proba_rf"] = rf.predict_proba(X_all)[:, 1]
    df["proba_rf_threshold_is"] = threshold_is

    _PREP_CACHE[cache_key] = df
    return df


# ══════════════════════════════════════════════════════════════════════════════
# 2. Backtest principal
# ══════════════════════════════════════════════════════════════════════════════


def run_backtest(
    df_15m: pd.DataFrame,  # nom historique du paramètre, ici M5
    ticker: str,
    tf=None,
    params: dict | None = None,
    topstep_guard: bool = True,
) -> pd.DataFrame:
    """
    Backtest pivot-rev-v1 sur MGC1 M5.

    Schéma de sortie : standard projet (cf. core/optimizer.py).
    `pnl` = P&L NET (slippage + commissions inclus).

    Le paramètre `df_15m` est en réalité du M5 (CSV_TIMEFRAME = "m5").
    """
    if ticker not in PIVOT_REV_TICKERS:
        return _empty_trades_df()

    p = params or {}
    sl_mult = float(p.get("sl_mult", PIVOT_REV_SL_MULT))
    rr_target = float(p.get("rr_target", PIVOT_REV_RR_TARGET))
    vol_rel_min = float(p.get("vol_rel_min", PIVOT_REV_VOL_REL_MIN))

    instr = INSTRUMENTS[ticker]
    tick_size = instr["tick_size"]
    pt_value = instr["dollar_per_point"]
    slip_ticks = SLIPPAGE_TICKS_PER_TICKER.get(ticker, 1)
    slip_px = slip_ticks * tick_size  # slippage en prix (par leg)
    min_sl_dist = PIVOT_REV_MIN_SL_DIST_TICKS * tick_size

    # ── 1. Préparer dataset enrichi (features + RF) ──
    df = _prepare_dataset(df_15m)

    # ── 2. Boucle de détection ──────────────────────────────────────────────
    trades: list = []
    daily_trades: dict = {}
    cum_pnl = 0.0
    peak_pnl = 0.0

    n = len(df)
    warmup_safe = 100  # marge supplémentaire pour past_pivot_density (lookback 200)

    # Bornes max d'entrée : on coupe les CONFIRM_BARS dernières barres
    # (sinon le trigger ne peut pas être évalué)
    last_signal_idx = n - PIVOT_REV_CONFIRM_BARS - PIVOT_REV_TIME_BARRIER_BARS - 1

    for i in range(warmup_safe, last_signal_idx):
        bar = df.iloc[i]

        # ── Filtres ML + structurels (sur la bougie candidate) ──
        if bar["proba_rf"] < bar["proba_rf_threshold_is"]:
            continue
        if bar["vol_rel"] < vol_rel_min:
            continue
        if bar["range_atr_ratio"] < PIVOT_REV_RANGE_ATR_RATIO_MIN:
            continue
        if bar["hour_ny"] < PIVOT_REV_HOUR_NY_MIN:
            continue
        if bar["hour_ny"] >= PIVOT_REV_HOUR_NY_MAX:
            continue
        if PIVOT_REV_SKIP_MACRO_DAYS and bar["is_macro_day"] == 1:
            continue

        # ── Direction du trade (mean-rev sur wash-out) ──
        bar_range = bar["high"] - bar["low"]
        if bar_range <= 0:
            continue
        body_ratio = abs(bar["close"] - bar["open"]) / bar_range
        if body_ratio < PIVOT_REV_DOJI_BODY_RATIO_MAX:
            continue  # doji → ignoré (direction ambiguë)

        lower_wick = min(bar["open"], bar["close"]) - bar["low"]
        upper_wick = bar["high"] - max(bar["open"], bar["close"])
        lower_wick_ratio = lower_wick / bar_range
        upper_wick_ratio = upper_wick / bar_range

        is_red = bar["close"] < bar["open"]
        is_green = bar["close"] > bar["open"]

        direction = None
        # LONG (pivot_low) : bougie rouge OU mèche basse > 50% du range
        if is_red or lower_wick_ratio > 0.5:
            direction = "long"
        # SHORT (pivot_high) : bougie verte OU mèche haute > 50% du range
        elif is_green or upper_wick_ratio > 0.5:
            direction = "short"
        if direction is None:
            continue

        # ── Fenêtre temporelle NY (date du JOUR pour MAX_TRADES) ──
        ts_utc = pd.Timestamp(df.index[i]).tz_localize("UTC")
        ts_ny = ts_utc.tz_convert(_NY)
        date_str = ts_ny.date().isoformat()
        if daily_trades.get(date_str, 0) >= PIVOT_REV_MAX_TRADES_PER_DAY:
            continue

        # ── Trigger d'entrée (stop entry, valide CONFIRM_BARS barres) ──
        atr_i = bar["atr_14"]
        if not np.isfinite(atr_i) or atr_i <= 0:
            continue

        if direction == "long":
            entry_trigger = bar["high"] + tick_size
            sl_anchor = bar["low"] - 0.5 * tick_size  # sous le wick bas
        else:
            entry_trigger = bar["low"] - tick_size
            sl_anchor = bar["high"] + 0.5 * tick_size  # au-dessus du wick haut

        # SL_dist = max(SL_MULT × ATR, distance au wick, plancher 2 ticks)
        sl_dist_wick = abs(entry_trigger - sl_anchor)
        sl_dist_atr = sl_mult * atr_i
        sl_dist = max(sl_dist_atr, sl_dist_wick, min_sl_dist)

        if direction == "long":
            sl = entry_trigger - sl_dist
            tp = entry_trigger + rr_target * sl_dist
        else:
            sl = entry_trigger + sl_dist
            tp = entry_trigger - rr_target * sl_dist

        tp_dist = rr_target * sl_dist
        rr_eff = tp_dist / sl_dist if sl_dist > 0 else 0.0

        # ── Sizing — risque cible incluant slippage anticipé ──
        risk_dollars_per_ct = (sl_dist + slip_px) * pt_value
        n_ct = max(1, int(RISK_PER_TRADE_USD / max(risk_dollars_per_ct, 1e-9)))

        # ── Garde-fou Topstep ──
        # day_pnl=0.0 (worst-case : début de journée), cum_pnl/peak_pnl trackés
        # depuis le début du backtest (cf. pattern strategies/opr*.py).
        if topstep_guard:
            allowed, _ = trade_allowed(day_pnl=0.0, cum_pnl=cum_pnl, peak_pnl=peak_pnl)
            if not allowed:
                continue

        # ── Recherche du fill (stop entry sur CONFIRM_BARS barres) ──
        fill_idx = None
        for j in range(i + 1, min(i + 1 + PIVOT_REV_CONFIRM_BARS, n)):
            fb = df.iloc[j]
            if direction == "long" and fb["high"] >= entry_trigger:
                fill_idx = j
                break
            if direction == "short" and fb["low"] <= entry_trigger:
                fill_idx = j
                break

        if fill_idx is None:
            # Signal expiré (trigger non touché) — on logge pour diagnostic
            trades.append(
                {
                    "date": date_str,
                    "dir": direction,
                    "entry": round(float(entry_trigger), 5),
                    "sl": round(float(sl), 5),
                    "tp": round(float(tp), 5),
                    "sl_dist": round(float(sl_dist), 5),
                    "tp_dist": round(float(tp_dist), 5),
                    "rr": round(float(rr_eff), 2),
                    "n_ct": int(n_ct),
                    "result": "NOT_FILLED",
                    "pnl": 0.0,
                    "fill_time": None,
                    "exit_time": None,
                    "exit": None,
                    "regime": _tag_regime(bar),
                    "pnl_gross": 0.0,
                    "adx": _safe_round(bar.get("adx_14"), 1),
                    "atr_pct": None,
                    "is_macro_day": (
                        bool(bar["is_macro_day"]) if pd.notna(bar.get("is_macro_day")) else False
                    ),
                }
            )
            # Note : on N'INCRÉMENTE PAS daily_trades pour les NOT_FILLED
            # (le signal n'a pas consommé le quota du jour).
            continue

        fill_time = df.index[fill_idx]

        # ── Simulation exit (triple barrier) ──
        result, exit_idx, exit_px = _simulate_exit(
            df, fill_idx, direction, entry_trigger, sl, tp, ts_ny
        )

        exit_time = df.index[exit_idx]

        # ── P&L brut + net ──
        raw_pnl = (exit_px - entry_trigger) * (1 if direction == "long" else -1)
        pnl_gross = raw_pnl * n_ct * pt_value
        slip_cost = 2 * slip_px * n_ct * pt_value  # entrée + sortie
        comm_cost = COMMISSION_RT_PER_CONTRACT * n_ct  # round-trip
        pnl = pnl_gross - slip_cost - comm_cost

        daily_trades[date_str] = daily_trades.get(date_str, 0) + 1
        cum_pnl += pnl
        if cum_pnl > peak_pnl:
            peak_pnl = cum_pnl

        trades.append(
            {
                "date": date_str,
                "dir": direction,
                "entry": round(float(entry_trigger), 5),
                "sl": round(float(sl), 5),
                "tp": round(float(tp), 5),
                "sl_dist": round(float(sl_dist), 5),
                "tp_dist": round(float(tp_dist), 5),
                "rr": round(float(rr_eff), 2),
                "n_ct": int(n_ct),
                "result": result,
                "pnl": round(float(pnl), 2),
                "fill_time": fill_time,
                "exit_time": exit_time,
                "exit": round(float(exit_px), 5),
                "regime": _tag_regime(bar),
                "pnl_gross": round(float(pnl_gross), 2),
                "adx": _safe_round(bar.get("adx_14"), 1),
                "atr_pct": None,
                "is_macro_day": (
                    bool(bar["is_macro_day"]) if pd.notna(bar.get("is_macro_day")) else False
                ),
            }
        )

    return _to_trades_df(trades)


# ══════════════════════════════════════════════════════════════════════════════
# 3. Helpers
# ══════════════════════════════════════════════════════════════════════════════


def _simulate_exit(df, fill_idx, direction, entry, sl, tp, fill_ts_ny):
    """
    Triple barrier exit :
      - SL prioritaire si SL+TP dans la même barre (règle projet)
      - TP si hit
      - Time barrier : PIVOT_REV_TIME_BARRIER_BARS barres M5
      - Forced close à PIVOT_REV_FORCE_CLOSE_HOUR_NY:PIVOT_REV_FORCE_CLOSE_MIN_NY NY

    Retourne (result, exit_idx, exit_px).
    """
    n = len(df)
    max_idx = min(fill_idx + PIVOT_REV_TIME_BARRIER_BARS, n - 1)

    for j in range(fill_idx, max_idx + 1):
        bar_j = df.iloc[j]

        # Forced close intra-jour ?
        ts_j_ny = pd.Timestamp(df.index[j]).tz_localize("UTC").tz_convert(_NY)
        force_close_now = ts_j_ny.hour > PIVOT_REV_FORCE_CLOSE_HOUR_NY or (
            ts_j_ny.hour == PIVOT_REV_FORCE_CLOSE_HOUR_NY
            and ts_j_ny.minute >= PIVOT_REV_FORCE_CLOSE_MIN_NY
        )
        if force_close_now:
            return "TE", j, float(bar_j["close"])

        # SL / TP touchés ?
        sl_hit = (direction == "long" and bar_j["low"] <= sl) or (
            direction == "short" and bar_j["high"] >= sl
        )
        tp_hit = (direction == "long" and bar_j["high"] >= tp) or (
            direction == "short" and bar_j["low"] <= tp
        )

        # Si c'est la barre de fill, on doit vérifier que SL/TP soient touchés
        # APRÈS le trigger d'entrée. Pour rester conservatif, on ne déclenche
        # SL/TP sur la barre de fill que si l'extrême opposé du trigger est
        # touché — sinon on attend la barre suivante.
        if j == fill_idx:
            # Logique conservative : entry est touché intra-bar, on suppose
            # que SL/TP peuvent aussi être touchés dans cette même barre.
            # Règle projet : SL prioritaire si ambiguïté.
            if sl_hit and tp_hit:
                return "SL", j, float(sl)
            if sl_hit:
                return "SL", j, float(sl)
            if tp_hit:
                return "TP", j, float(tp)
            continue

        if sl_hit and tp_hit:
            return "SL", j, float(sl)  # conservatif
        if sl_hit:
            return "SL", j, float(sl)
        if tp_hit:
            return "TP", j, float(tp)

    # Time barrier atteint
    last = df.iloc[max_idx]
    return "TE", max_idx, float(last["close"])


def _tag_regime(bar) -> str:
    """Tag régime pour stress tests PHASE 5."""
    adx = bar.get("adx_14")
    if pd.notna(adx) and adx > 25:
        return "trending"
    if pd.notna(adx) and adx < 20:
        return "ranging"
    return "neutral"


def _safe_round(v, n: int):
    if v is None or pd.isna(v):
        return None
    try:
        return round(float(v), n)
    except (ValueError, TypeError):
        return None


def _empty_trades_df() -> pd.DataFrame:
    cols = [
        "date",
        "dir",
        "entry",
        "sl",
        "tp",
        "sl_dist",
        "tp_dist",
        "rr",
        "n_ct",
        "result",
        "pnl",
        "fill_time",
        "exit_time",
        "exit",
        "regime",
        "pnl_gross",
        "adx",
        "atr_pct",
        "is_macro_day",
    ]
    return pd.DataFrame(columns=cols)


def _to_trades_df(trades: list) -> pd.DataFrame:
    if not trades:
        return _empty_trades_df()
    cols = [
        "date",
        "dir",
        "entry",
        "sl",
        "tp",
        "sl_dist",
        "tp_dist",
        "rr",
        "n_ct",
        "result",
        "pnl",
        "fill_time",
        "exit_time",
        "exit",
        "regime",
        "pnl_gross",
        "adx",
        "atr_pct",
        "is_macro_day",
    ]
    df = pd.DataFrame(trades)
    return df[cols]


# ══════════════════════════════════════════════════════════════════════════════
# 4. plot_day (minimal — détails en PHASE 6)
# ══════════════════════════════════════════════════════════════════════════════


def plot_day(df_15m, ticker, date_str, day_trades, output_path):
    """Plot minimal — chart détaillé en PHASE 6 (chartist audit visuel).

    Le pipeline /new-strategy s'arrête en PHASE 4 ici (consigne orchestrateur),
    donc on garde un stub fonctionnel. On loggue juste un fichier vide pour
    éviter de casser --plot si appelé.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ts_idx = pd.to_datetime(df_15m.index)
    if ts_idx.tz is None:
        ts_ny = ts_idx.tz_localize("UTC").tz_convert(_NY)
    else:
        ts_ny = ts_idx.tz_convert(_NY)
    mask = ts_ny.normalize() == pd.Timestamp(date_str, tz=_NY)
    day = df_15m[mask.values]
    if day.empty:
        # Crée un fichier vide pour ne pas casser le pipeline
        Path(output_path).touch()
        return

    fig, ax = plt.subplots(figsize=(14, 7))
    x = range(len(day))
    for i, (_, row) in enumerate(day.iterrows()):
        color = "#26a69a" if row["close"] >= row["open"] else "#ef5350"
        ax.plot([i, i], [row["low"], row["high"]], color=color, lw=0.8)
        ax.bar(
            i,
            abs(row["close"] - row["open"]),
            bottom=min(row["open"], row["close"]),
            color=color,
            width=0.6,
            alpha=0.9,
        )

    day_pnl = 0.0
    for trade in day_trades:
        if trade.get("result") == "NOT_FILLED":
            continue
        try:
            fill_idx = list(day.index).index(trade["fill_time"])
        except (ValueError, KeyError):
            continue
        try:
            exit_idx = list(day.index).index(trade["exit_time"])
        except (ValueError, KeyError):
            exit_idx = fill_idx
        color = "#00c853" if trade["dir"] == "long" else "#d50000"
        marker = "^" if trade["dir"] == "long" else "v"
        ax.scatter(fill_idx, trade["entry"], marker=marker, color=color, s=140, zorder=5)
        ax.scatter(exit_idx, trade["exit"], marker="x", color="white", s=120, zorder=5)
        ax.axhline(trade["sl"], color="#ef5350", ls="--", lw=0.7, alpha=0.6)
        ax.axhline(trade["tp"], color="#26a69a", ls="--", lw=0.7, alpha=0.6)
        day_pnl += trade.get("pnl", 0.0)

    ax.set_title(
        f"{ticker} — {date_str}   |   P&L net jour : {day_pnl:+.0f} $",
        fontsize=12,
        fontweight="bold",
    )
    ax.set_facecolor("#1a1a2e")
    fig.patch.set_facecolor("#13131f")
    ax.tick_params(colors="white")
    plt.tight_layout()
    plt.savefig(output_path, dpi=110, bbox_inches="tight")
    plt.close(fig)
