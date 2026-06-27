"""
Stratégie fib-fine-v2 — retracement Fibonacci NATIF sur M5 (LOOK-AHEAD-FREE).

Concept       : mirror data-driven de fib-v4 (continuation sur retracement Fib),
                détecté nativement sur M5. Univers v2 = NQ1 + MES1 + MGC1.

CHANGEMENTS v1 → v2 (issus de @quant discover + audit, 2026-06-01)
  • LOOK-AHEAD RETIRÉ : le filtre `wick_through_atr` lisait la mèche COMPLÈTE de
    la barre de FILL (low/high finaux) = info non observable en live à l'instant
    où le broker fille l'ordre limite intra-bar. C'était le blocant #1 de l'audit.
  • FILTRE CAUSAL AJOUTÉ : `atr_ratio_sl = ATR(5)[i-1] / ATR_armement >= 1.0`
    (seuil FIXE pré-spécifié, 0 degré de liberté ; perm-test pooled m5 p=0.002,
    Bonferroni OK, stable IS→OOS). Lu sur barre i-1 (close AVANT le fill) →
    no look-ahead PAR CONSTRUCTION. Hypothèse causale : la continuation post-
    retracement a besoin d'énergie résiduelle (momentum), pas "les données le disent".
  • UNIVERS RÉDUIT : M5 uniquement (M1 tué par frictions, PF NET ≤ 0.76) ;
    YM1 retiré (edge v1 = mirage look-ahead, OOS plat après causal).
  • DST-AWARE : session filtrée en heures NY (America/New_York), plus en UTC fixe.
  • SIZING : risk-per-trade réduit (FIB_FINE_RISK_PER_TRADE_USD) pour MC DD < Topstep.

LIVE-ÉQUIVALENCE : toutes les features de décision sont calculées sur des barres
CLÔTURÉES strictement avant le fill → v2 est live-équivalent PAR CONSTRUCTION.
Aucun wirage M1Buffer nécessaire pour un filtre wick. Le fill reste conservateur
SL-prioritaire intra-bar (honnête, pas du look-ahead).

Edge          : capture de la liquidité au niveau de retracement avant la
                continuation. Paie ce P&L : stops sous swing low + momentum-chasers.
Falsification : si PF NET OOS portefeuille ≤ 1.2 (frictions incluses), rejet.
Indicateurs   : ATR(14), ATR(5) court, EMA(50/200), ADX(14), pivots left/right.

Caveats données (cf. rapport) :
  • NQ1/MES1 : M5 RESAMPLÉ depuis M1 DATA_BACKTEST (2025-02-16 → 2026-03-02) →
    a un IS (fév-sep 2025) + OOS (oct 2025-mars 2026) → walk-forward propre.
  • MGC1 : M5 NATIF data/MGC1_data_m5.csv (2025-10-19 → 2026-05-15) = OOS-PUR
    (aucune barre avant IS_END) → stabilité IS→OOS NON validable, confiance moindre.
  • Rolls : un saut de prix bar-to-bar > FIB_FINE_ROLL_GAP_PCT neutralise le swing.

Architecture : logique AUTO-CONTENUE. Helpers Fib purs (compute_atr/ema/adx,
detect_pivots, find_last_impulse, build_signal) importés en LECTURE SEULE depuis
core/fib_helpers.py. Aucune modification de la prod (core/strategy_fib_v4.py).
"""

from __future__ import annotations

import os
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from config import (
    COMMISSION_RT_PER_CONTRACT,
    FIB_FINE_ADX_PERIOD,
    FIB_FINE_ADX_TREND_THRESHOLD,
    FIB_FINE_ATR_EXPANSION_MIN_PER_TICKER,
    FIB_FINE_ATR_EXPANSION_PERIOD,
    FIB_FINE_ATR_PERIOD,
    FIB_FINE_DEFAULT_TF,
    FIB_FINE_EMA_FAST_PERIOD,
    FIB_FINE_EMA_SLOW_PERIOD,
    FIB_FINE_LEVEL_PER_TICKER,
    FIB_FINE_MIN_IMPULSE_ATR_PER_TICKER,
    FIB_FINE_PIVOT_BREAK_BUFFER_ATR_PER_TICKER,
    FIB_FINE_RISK_PER_TRADE_USD,
    FIB_FINE_ROLL_GAP_PCT,
    FIB_FINE_SESSION_PER_TICKER,
    FIB_FINE_SESSION_WINDOWS_NY,
    FIB_FINE_SKIP_MACRO_PER_TICKER,
    FIB_FINE_SL_ATR_MULT_PER_TICKER,
    FIB_FINE_SOURCE_PER_TICKER,
    FIB_FINE_STRATEGY_VERSION,
    FIB_FINE_STRUCT_PER_TF,
    FIB_FINE_TICKERS,
    FIB_FINE_TP_ATR_MULT_PER_TICKER,
    FIB_FINE_WICK_THROUGH_MAX_ATR_PER_TICKER,
    INSTRUMENTS,
    MACRO_EVENT_DATES,
    RISK_PER_TRADE_USD,
    SLIPPAGE_TICKS_PER_TICKER,
)

# Fuseau NY pour le filtrage de session DST-aware (pattern projet, cf. core/opr.py).
_NY_TZ = ZoneInfo("America/New_York")

# Helpers Fib partagés — LECTURE SEULE (module pur, sans état). Autorisé par la
# consigne : référence prod sans import-modif de la logique d'exécution.
from core.fib_helpers import (
    build_signal,
    compute_adx,
    compute_atr,
    compute_ema,
    detect_pivots,
    detect_trend,
    find_last_impulse,
)

# Reproductibilité (aucun aléatoire dans cette stratégie, par précaution projet).
np.random.seed(42)

# ── Identité ──────────────────────────────────────────────────────────────────
STRATEGY_ID = FIB_FINE_STRATEGY_VERSION  # "fib-fine-v2"
TICKERS = list(FIB_FINE_TICKERS)
CSV_SUFFIX = "_fib_fine"
CSV_TIMEFRAME = FIB_FINE_DEFAULT_TF  # "m5" uniquement en v2

# ── Contrat moteur M1 (core/bt_engine) ────────────────────────────────────────
SIGNAL_TF = "5min"  # signaux natifs M5 (reconstruits depuis le M1)
SESSION_END_MIN = 20 * 60  # 20:00 NY (≈ 00:00 UTC EDT = bascule jour futures)
MAX_HOLD_MIN = FIB_FINE_STRUCT_PER_TF["m5"]["max_hold_bars"] * 5  # 60 barres M5 = 300 min
RUN_MIN_WINDOW = (4 * 60, 20 * 60 + 5)  # couvre no_nuit (4h NY) → clôture
_TF_DELTA_FF = pd.Timedelta(minutes=5)

# Grille v2 : wick_max retiré (filtre neutralisé), atr_expansion_min ABSENT
# (seuil fixe pré-spécifié → ne pas optimiser, p-hacking).
PARAM_GRID = {
    "fib_level": [0.382, 0.5, 0.618],
    "sl_mult": [0.75, 1.0, 1.5],
}

# Fenêtres de session en heures NY (DST-aware) — cf. config FIB_FINE_SESSION_WINDOWS_NY.
_SESSION_WINDOWS_NY = dict(FIB_FINE_SESSION_WINDOWS_NY)
_MACRO_DATES_SET = set(MACRO_EVENT_DATES)

# Cache des chemins M1 (DATA_BACKTEST) pour resampling.
_M1_DIR = "DATA_BACKTEST"


# ═════════════════════════════════════════════════════════════════════════════
# Chargement / préparation des données fine-TF
# ═════════════════════════════════════════════════════════════════════════════


def _load_m1(ticker: str) -> pd.DataFrame:
    """Charge le CSV M1 brut (DATA_BACKTEST) et normalise le schéma OHLCV."""
    path = os.path.join(_M1_DIR, f"{ticker}_data_m1.csv")
    if not os.path.exists(path):
        raise FileNotFoundError(f"M1 introuvable pour {ticker} : {path}")
    df = pd.read_csv(path, parse_dates=["datetime"])
    df = df.set_index("datetime").sort_index()
    df = df[~df.index.duplicated(keep="first")]
    return df[["open", "high", "low", "close", "volume"]]


def _resample_to_m5(df_m1: pd.DataFrame) -> pd.DataFrame:
    """Resample un M1 vers M5 (OHLCV). Garde la même série / back-adjustment."""
    agg = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }
    out = df_m1.resample("5min").agg(agg).dropna(subset=["close"])
    return out


def _flag_roll_gaps(df: pd.DataFrame, gap_pct: float) -> np.ndarray:
    """Marque les barres dont le saut close-to-close dépasse `gap_pct` (rolls /
    back-adjust). Retourne un masque booléen aligné sur df : True = barre 'gap'.
    Un swing chevauchant un gap sera neutralisé dans la détection d'impulse.
    """
    ret = df["close"].pct_change().abs()
    return (ret > gap_pct).values


def load_fine_df(
    ticker: str,
    timeframe: str,
    source: str = "resampled",
    csv_dir: str = "data",
) -> pd.DataFrame:
    """
    Charge le DataFrame fine-TF pour un ticker.

    timeframe : "m1" ou "m5".
    source    : "resampled" → resample depuis le M1 DATA_BACKTEST (comparaison à
                  périmètre égal, même back-adjustment) ;
                "native"    → CSV natif data/{TICKER}_data_m5.csv (m5 uniquement,
                  bonus robustesse / MGC1).
    """
    if timeframe == "m1":
        return _load_m1(ticker)
    if timeframe == "m5":
        if source == "native":
            path = os.path.join(csv_dir, f"{ticker}_data_m5.csv")
            df = pd.read_csv(path, parse_dates=["datetime"])
            df = df.set_index("datetime").sort_index()
            df = df[~df.index.duplicated(keep="first")]
            return df[["open", "high", "low", "close", "volume"]]
        # resampled depuis M1
        return _resample_to_m5(_load_m1(ticker))
    raise ValueError(f"timeframe non supporté : {timeframe}")


# ═════════════════════════════════════════════════════════════════════════════
# v2 — Feature engineering CAUSAL : expansion de volatilité (remplace le wick LA)
# ═════════════════════════════════════════════════════════════════════════════


def add_atr_short(df: pd.DataFrame, period: int) -> pd.DataFrame:
    """Ajoute la colonne `atr_short` = ATR(period) court terme.

    compute_atr est un rolling causal : chaque valeur i n'utilise que les barres
    <= i. Au moment du filtrage on lira atr_short à l'index i-1 (barre clôturée
    AVANT la barre de fill) → no look-ahead par construction.
    """
    df = df.copy()
    df["atr_short"] = compute_atr(df, period)
    return df


def passes_atr_expansion_filter(
    df: pd.DataFrame,
    fill_i: int,
    atr_at_arm: float,
    min_ratio: float,
) -> bool:
    """Filtre CAUSAL d'expansion de volatilité — remplace le wick look-ahead.

    Lit atr_short à fill_i-1 (dernière barre CLÔTURÉE strictement AVANT la barre
    de fill). Aucune lecture de la barre de fill (fill_i) ni d'aucune barre
    postérieure → no look-ahead par construction.

    Live-équivalent : à l'instant où le prix touche le niveau Fib (décision de
    fill), la barre fill_i n'est pas encore close ; la dernière info close
    disponible est fill_i-1. C'est exactement ce qu'on lit ici.

    Retourne True si on garde le fill, False si on l'annule.
    Si min_ratio est None/0 → filtre inactif (True).
    """
    if not min_ratio:
        return True
    if atr_at_arm is None or pd.isna(atr_at_arm) or atr_at_arm <= 0:
        return True  # pas d'ATR de référence → on ne filtre pas
    j = fill_i - 1
    if j < 0 or "atr_short" not in df.columns:
        return True
    atr_s = df["atr_short"].iloc[j]
    if pd.isna(atr_s):
        return True  # warmup → ne pas filtrer (conservateur)
    return (atr_s / atr_at_arm) >= min_ratio


# ═════════════════════════════════════════════════════════════════════════════
# Backtest principal — boucle auto-contenue (mirror fib-v4, TF paramétrable)
# ═════════════════════════════════════════════════════════════════════════════


def run_backtest(
    df: pd.DataFrame | None,
    ticker: str,
    tf=None,
    params: dict | None = None,
    topstep_guard: bool = True,
) -> pd.DataFrame:
    """
    Backtest fib-fine sur un timeframe fin.

    Le DataFrame d'entrée est OPTIONNEL : si None, la stratégie charge elle-même
    les données fine-TF (resampling M1→TF). Cela permet à backtest.py/optimize.py
    (qui passent un df M15 standard) de continuer à fonctionner — on IGNORE le df
    M15 fourni et on recharge le TF fin. Le caller peut forcer le df via `df`.

    params (tous optionnels — sinon valeurs config per-ticker) :
      timeframe   : "m1" | "m5"            (défaut FIB_FINE_DEFAULT_TF)
      source      : "resampled" | "native" (défaut "resampled")
      fib_level   : niveau Fibonacci
      sl_mult     : SL en multiples d'ATR
      tp_mult     : TP en multiples d'ATR
      min_imp     : taille minimale impulse (×ATR)
      wick_max    : seuil mèche au fill (×ATR)
      pivot_break_buffer_atr : buffer pivot break (×ATR)
      skip_macro  : bool

    Schéma de colonnes du DataFrame retourné (canonique, compat core/optimizer) :
      date, dir, entry, sl, tp, sl_dist, tp_dist, rr, n_ct,
      result (TP|SL|TE|NOT_FILLED), pnl, fill_time, exit_time, exit, regime
      + optionnelles : pnl_gross, adx, atr_pct, is_macro_day, wick_through_atr,
        pivot_break_atr, bars_to_fill, timeframe
    `pnl` = P&L NET (slippage entrée+sortie + commission round-trip).
    """
    p = params or {}
    timeframe = p.get("timeframe", FIB_FINE_DEFAULT_TF)
    # Source par défaut PAR ticker (NQ1/MES1 = resampled M1 → IS+OOS ;
    # MGC1 = native M5 → OOS-only). Surchargeable via params["source"].
    source = p.get("source", FIB_FINE_SOURCE_PER_TICKER.get(ticker, "resampled"))

    fib_level = p.get("fib_level", FIB_FINE_LEVEL_PER_TICKER.get(ticker, 0.382))
    sl_mult = p.get("sl_mult", FIB_FINE_SL_ATR_MULT_PER_TICKER[ticker])
    tp_mult = p.get("tp_mult", FIB_FINE_TP_ATR_MULT_PER_TICKER[ticker])
    min_imp = p.get("min_imp", FIB_FINE_MIN_IMPULSE_ATR_PER_TICKER.get(ticker, 1.5))
    wick_max = p.get("wick_max", FIB_FINE_WICK_THROUGH_MAX_ATR_PER_TICKER.get(ticker, 1e9))
    # v2 : filtre causal d'expansion de volatilité (seuil FIXE, NON optimisable).
    atr_exp_min = p.get(
        "atr_expansion_min",
        FIB_FINE_ATR_EXPANSION_MIN_PER_TICKER.get(ticker, 0.0),
    )
    pivot_break_buffer_atr = p.get(
        "pivot_break_buffer_atr",
        FIB_FINE_PIVOT_BREAK_BUFFER_ATR_PER_TICKER.get(ticker, 0.0),
    )
    skip_macro = p.get("skip_macro", FIB_FINE_SKIP_MACRO_PER_TICKER.get(ticker, False))
    # v2 : sizing dédié (Topstep MC DD). None → nominal global RISK_PER_TRADE_USD.
    risk_usd = p.get("risk_usd", FIB_FINE_RISK_PER_TRADE_USD) or RISK_PER_TRADE_USD

    struct = FIB_FINE_STRUCT_PER_TF[timeframe]
    pivot_left = struct["pivot_left"]
    pivot_right = struct["pivot_right"]
    impulse_lookback = struct["impulse_lookback"]
    max_impulse_bars = struct["max_impulse_bars"]
    order_timeout_bars = struct["order_timeout_bars"]
    max_hold_bars = struct["max_hold_bars"]

    # Charge le TF fin si non fourni (cas backtest.py qui passe le M15 standard).
    if df is None:
        df = load_fine_df(ticker, timeframe, source=source)
    else:
        df = df.copy()

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
        "wick_through_atr",
        "pivot_break_atr",
        "bars_to_fill",
        "timeframe",
    ]
    if df.empty:
        return pd.DataFrame(columns=cols)

    instr = INSTRUMENTS[ticker]
    tick_size = instr["tick_size"]
    dpp = instr["dollar_per_point"]
    slip_ticks = SLIPPAGE_TICKS_PER_TICKER.get(ticker, 1)
    slip_px = slip_ticks * tick_size

    # Indicateurs causaux.
    df["atr"] = compute_atr(df, FIB_FINE_ATR_PERIOD)
    df["ema_fast"] = compute_ema(df["close"], FIB_FINE_EMA_FAST_PERIOD)
    df["ema_slow"] = compute_ema(df["close"], FIB_FINE_EMA_SLOW_PERIOD)
    df["adx"] = compute_adx(df, FIB_FINE_ADX_PERIOD)
    # Percentile ATR roulant (~1 mois de barres) pour stress vol.
    bars_per_day = {"m1": 60 * 23, "m5": 12 * 23}.get(timeframe, 96)
    df["atr_pct"] = df["atr"].rolling(window=bars_per_day * 20, min_periods=200).rank(pct=True)
    # v2 : ATR court terme pour le filtre causal d'expansion de volatilité.
    df = add_atr_short(df, FIB_FINE_ATR_EXPANSION_PERIOD)

    # Masque des gaps de roll (pour neutraliser les faux swings).
    roll_gap_mask = _flag_roll_gaps(df, FIB_FINE_ROLL_GAP_PCT)
    # Index cumulatif des gaps : roll_cum[i] = nb de gaps dans [0..i]. Un swing
    # [a..b] chevauche un gap ssi roll_cum[b] != roll_cum[a].
    roll_cum = np.cumsum(roll_gap_mask.astype(np.int64))

    pivot_highs, pivot_lows = detect_pivots(df, pivot_left, pivot_right)

    # v2 : filtrage de session DST-AWARE. L'index est UTC naïf en interne ; on le
    # convertit en heure NY (America/New_York) puis on filtre sur l'heure NY. Cela
    # élimine le glissement 1h hiver/été du filtre UTC fixe de v1.
    sess_key = FIB_FINE_SESSION_PER_TICKER.get(ticker, "us_session")
    s_h, e_h = _SESSION_WINDOWS_NY.get(sess_key, (9, 17))
    idx_ny = df.index.tz_localize("UTC").tz_convert(_NY_TZ)
    hours = idx_ny.hour
    in_session = (hours >= s_h) & (hours < e_h)

    n = len(df)
    warmup = max(FIB_FINE_EMA_SLOW_PERIOD, pivot_left + pivot_right + 5, 250)
    trades: list[dict] = []
    pending: dict | None = None
    position: dict | None = None
    last_impulse_key = None

    def _swing_crosses_roll(idx_a: int, idx_b: int) -> bool:
        """True si l'intervalle [idx_a, idx_b] contient un gap de roll."""
        a, b = (idx_a, idx_b) if idx_a <= idx_b else (idx_b, idx_a)
        a = max(a, 0)
        b = min(b, n - 1)
        return roll_cum[b] != roll_cum[a]

    for i in range(warmup, n):
        bar = df.iloc[i]

        # ── 1. Gestion position ouverte ──────────────────────────────────────
        if position is not None:
            exit_price, exit_reason = None, None
            if position["direction"] == "long":
                hit_sl = bar["low"] <= position["sl"]
                hit_tp = bar["high"] >= position["tp"]
            else:
                hit_sl = bar["high"] >= position["sl"]
                hit_tp = bar["low"] <= position["tp"]
            # Ambiguïté SL+TP même barre → SL prioritaire (conservateur).
            if hit_sl and hit_tp or hit_sl:
                exit_price, exit_reason = position["sl"], "SL"
            elif hit_tp:
                exit_price, exit_reason = position["tp"], "TP"

            bars_held = i - position["fill_idx"]
            # Time-stop intraday (pas de hold overnight) : close de jour différent
            # OU dépassement de max_hold_bars.
            new_day = df.index[i].date() != pd.Timestamp(position["fill_time"]).date()
            if exit_reason is None and (bars_held >= max_hold_bars or new_day):
                exit_price, exit_reason = float(bar["close"]), "TE"

            if exit_reason is not None:
                trades.append(
                    _finalize_trade(
                        position,
                        exit_price,
                        exit_reason,
                        i,
                        df,
                        bars_held,
                        dpp,
                        slip_px,
                        COMMISSION_RT_PER_CONTRACT,
                        timeframe,
                    )
                )
                position = None
            continue

        # ── 2. Gestion ordre limite pending ──────────────────────────────────
        if pending is not None:
            pending["low_min_pending"] = min(pending["low_min_pending"], float(bar["low"]))
            pending["high_max_pending"] = max(pending["high_max_pending"], float(bar["high"]))
            atr_arm = pending.get("atr", float("nan"))

            # Invalidation #1 : pivot break.
            if atr_arm and not pd.isna(atr_arm):
                if pending["direction"] == "long":
                    pb_now = (pending["low_min_pending"] - pending["swing_low"]) / atr_arm
                else:
                    pb_now = (pending["swing_high"] - pending["high_max_pending"]) / atr_arm
                if pb_now < -pivot_break_buffer_atr:
                    pending = None
                    continue

            # Fin de journée pendant pending → annulation (intraday only).
            if df.index[i].date() != pd.Timestamp(pending["pending_time"]).date():
                pending = None
                continue

            level = pending["entry"]
            if bar["low"] <= level <= bar["high"]:
                # ── v2 : Invalidation #2 — filtre CAUSAL d'expansion de volatilité
                # (remplace le wick look-ahead). Lit atr_short à i-1 (barre close
                # AVANT le fill) → no look-ahead par construction. Si l'impulsion
                # n'a plus d'énergie résiduelle (ATR court < ATR d'armement), on
                # annule l'entrée. Le wick n'est PLUS lu pour décider.
                if not passes_atr_expansion_filter(df, i, atr_arm, atr_exp_min):
                    pending = None
                    continue
                # wick conservé en COLONNE D'AUDIT uniquement (jamais filtre).
                wick = float("nan")

                if pending["direction"] == "long":
                    pivot_break_final = (
                        pending["low_min_pending"] - pending["swing_low"]
                    ) / atr_arm
                else:
                    pivot_break_final = (
                        pending["swing_high"] - pending["high_max_pending"]
                    ) / atr_arm

                fill_day = df.index[i].strftime("%Y-%m-%d")
                position = {
                    **{k: v for k, v in pending.items() if k != "pending_idx"},
                    "fill_idx": int(i),
                    "fill_time": str(df.index[i]),
                    "bars_to_fill": int(i - pending["pending_idx"]),
                    "wick_through_atr": float(wick),
                    "pivot_break_atr": float(pivot_break_final),
                    "is_macro_day": bool(fill_day in _MACRO_DATES_SET),
                    "atr_pct_arm": float(bar["atr_pct"]) if pd.notna(bar["atr_pct"]) else None,
                    "adx_arm": float(bar["adx"]) if pd.notna(bar["adx"]) else None,
                }
                pending = None
                # Check immédiat SL/TP sur la barre de fill (conservateur).
                if position["direction"] == "long":
                    hit_sl = bar["low"] <= position["sl"]
                    hit_tp = bar["high"] >= position["tp"]
                else:
                    hit_sl = bar["high"] >= position["sl"]
                    hit_tp = bar["low"] <= position["tp"]
                exit_price, exit_reason = None, None
                if hit_sl and hit_tp or hit_sl:
                    exit_price, exit_reason = position["sl"], "SL"
                elif hit_tp:
                    exit_price, exit_reason = position["tp"], "TP"
                if exit_reason is not None:
                    trades.append(
                        _finalize_trade(
                            position,
                            exit_price,
                            exit_reason,
                            i,
                            df,
                            0,
                            dpp,
                            slip_px,
                            COMMISSION_RT_PER_CONTRACT,
                            timeframe,
                        )
                    )
                    position = None
                continue

            if (i - pending["pending_idx"]) >= order_timeout_bars:
                pending = None

        # ── 3. Génération nouveau signal ─────────────────────────────────────
        if pending is not None or position is not None:
            continue
        if not bool(in_session[i]):
            continue
        if skip_macro and df.index[i].strftime("%Y-%m-%d") in _MACRO_DATES_SET:
            continue
        atr_now = bar["atr"]
        if pd.isna(atr_now) or atr_now <= 0:
            continue

        trend = detect_trend(
            float(bar["close"]),
            float(bar["ema_fast"]),
            float(bar["ema_slow"]),
            float(bar["adx"]) if not pd.isna(bar["adx"]) else np.nan,
            FIB_FINE_ADX_TREND_THRESHOLD,
        )
        if trend == "RANGE":
            continue

        impulse = find_last_impulse(
            df,
            i,
            pivot_highs,
            pivot_lows,
            df["atr"],
            trend,
            pivot_right,
            min_imp,
            max_impulse_bars,
            impulse_lookback,
            fib_level=fib_level,
        )
        if impulse is None:
            continue

        # Neutralisation roll : si l'impulse chevauche un gap de roll, rejet.
        if _swing_crosses_roll(impulse["pivot_low_idx"], impulse["pivot_high_idx"]):
            continue

        impulse_key = (impulse["direction"], impulse["pivot_low_idx"], impulse["pivot_high_idx"])
        if impulse_key == last_impulse_key:
            continue
        if impulse["direction"] == "long" and bar["close"] <= impulse["fib_50"]:
            continue
        if impulse["direction"] == "short" and bar["close"] >= impulse["fib_50"]:
            continue

        sig = build_signal(impulse, float(atr_now), ticker, sl_mult, tp_mult)
        if sig is None:
            continue

        # v2 : sizing dédié. build_signal dimensionne sur RISK_PER_TRADE_USD global
        # ($200) ; on re-dimensionne n_ct sur risk_usd (calibré Topstep MC DD).
        if risk_usd != RISK_PER_TRADE_USD and sig["sl_dist"] > 0:
            n_ct = int(risk_usd / (sig["sl_dist"] * dpp))
            if n_ct <= 0:
                continue
            sig["n_ct"] = int(n_ct)
            sig["risk"] = float(n_ct * sig["sl_dist"] * dpp)

        sig["pending_idx"] = int(i)
        sig["pending_time"] = str(df.index[i])
        sig["fib_level"] = float(fib_level)
        sig["trend"] = trend
        sig["low_min_pending"] = float(bar["low"])
        sig["high_max_pending"] = float(bar["high"])
        last_impulse_key = impulse_key
        pending = sig

    return pd.DataFrame(trades) if trades else pd.DataFrame(columns=cols)


def _finalize_trade(
    position: dict,
    exit_price: float,
    exit_reason: str,
    exit_idx: int,
    df: pd.DataFrame,
    bars_held: int,
    dpp: float,
    slip_px: float,
    commission_rt: float,
    timeframe: str,
) -> dict:
    """Construit la ligne de trade canonique avec P&L NET (frictions incluses)."""
    direction = position["direction"]
    entry = position["entry"]
    n_ct = position["n_ct"]
    raw_pts = (exit_price - entry) if direction == "long" else (entry - exit_price)
    pnl_gross = raw_pts * n_ct * dpp
    # Frictions : slippage entrée + sortie (2× slip_px) + commission round-trip.
    slip_cost = 2.0 * slip_px * n_ct * dpp
    comm_cost = commission_rt * n_ct
    pnl_net = pnl_gross - slip_cost - comm_cost

    adx_arm = position.get("adx_arm")
    if adx_arm is not None and adx_arm > 25:
        regime = "trending"
    elif adx_arm is not None and adx_arm < 20:
        regime = "ranging"
    else:
        regime = "neutral"

    return {
        "date": df.index[exit_idx].strftime("%Y-%m-%d"),
        "dir": direction,
        "entry": float(entry),
        "sl": float(position["sl"]),
        "tp": float(position["tp"]),
        "sl_dist": float(position["sl_dist"]),
        "tp_dist": float(position["tp_dist"]),
        "rr": float(position["rr"]),
        "n_ct": int(n_ct),
        "result": exit_reason,
        "pnl": round(float(pnl_net), 2),
        "fill_time": position["fill_time"],
        "exit_time": str(df.index[exit_idx]),
        "exit": float(exit_price),
        "regime": regime,
        "pnl_gross": round(float(pnl_gross), 2),
        "adx": round(adx_arm, 1) if adx_arm is not None else None,
        "atr_pct": position.get("atr_pct_arm"),
        "is_macro_day": bool(position.get("is_macro_day", False)),
        "wick_through_atr": position.get("wick_through_atr"),
        "pivot_break_atr": position.get("pivot_break_atr"),
        "bars_to_fill": position.get("bars_to_fill"),
        "timeframe": timeframe,
    }


# ═════════════════════════════════════════════════════════════════════════════
# Contrat moteur M1 — emit_signals (core/bt_engine)
# ═════════════════════════════════════════════════════════════════════════════


def emit_signals(sig_df: pd.DataFrame, ticker: str, params: dict | None = None) -> list[dict]:
    """Émet les ordres fib-fine pour le moteur M1 (core/bt_engine).

    Réutilise la logique de signal/fill VALIDÉE (`run_backtest` M5 : impulse + filtre causal
    d'expansion + invalidation pivot-break + timeout, tous résolus sur barres CLOSES, no leak)
    → on en extrait les ordres RÉELLEMENT remplis, puis on délègue à core/bt_engine la
    résolution PRÉCISE du fill + SL/TP à la minute (corrige le same-bar du moteur maison).
    `sig_df` = M5 reconstruit depuis le M1 par le moteur.
    """
    trades = run_backtest(df=sig_df, ticker=ticker, params=params, topstep_guard=False)
    if trades is None or len(trades) == 0:
        return []
    arms: list[dict] = []
    for t in trades.to_dict("records"):
        if t.get("result") == "NOT_FILLED" or not t.get("fill_time"):
            continue
        fill_ts = pd.Timestamp(t["fill_time"])
        arms.append(
            {
                "dir": t["dir"],
                "entry": float(t["entry"]),
                "sl": float(t["sl"]),
                "tp": float(t["tp"]),
                "sl_dist": float(t["sl_dist"]),
                "tp_dist": float(t["tp_dist"]),
                "rr": float(t["rr"]),
                "n_ct": int(t["n_ct"]),
                "regime": t.get("regime", "neutral"),
                "arm_ts": fill_ts,
                "place_ts": fill_ts - _TF_DELTA_FF,  # ordre vif dès la barre M5 AVANT le fill
                "timeout_ts": fill_ts + _TF_DELTA_FF,  # doit filler dans la barre de fill
                "extras": {
                    "pivot_break_atr": t.get("pivot_break_atr"),
                    "bars_to_fill": t.get("bars_to_fill"),
                },
            }
        )
    return arms


# ═════════════════════════════════════════════════════════════════════════════
# Visualisation par jour
# ═════════════════════════════════════════════════════════════════════════════


def plot_day(df, ticker: str, date_str: str, day_trades: list, output_path: str) -> None:
    """Chart OHLC d'une journée fine-TF avec entrées/sorties/SL/TP."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    idx = pd.to_datetime(df.index)
    mask = idx.strftime("%Y-%m-%d") == date_str
    day = df[mask].copy()
    if day.empty:
        return

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(16, 10), gridspec_kw={"height_ratios": [3, 1]}, sharex=True
    )
    for k, (_, row) in enumerate(day.iterrows()):
        color = "#26a69a" if row["close"] >= row["open"] else "#ef5350"
        ax1.plot([k, k], [row["low"], row["high"]], color=color, lw=0.6)
        ax1.bar(
            k,
            abs(row["close"] - row["open"]),
            bottom=min(row["open"], row["close"]),
            color=color,
            width=0.6,
            alpha=0.9,
        )

    day_index = list(day.index)
    day_pnl = 0.0
    for t in day_trades:
        if t.get("result") == "NOT_FILLED":
            continue
        try:
            fi = day_index.index(pd.Timestamp(t["fill_time"]))
        except (ValueError, KeyError):
            continue
        try:
            xi = day_index.index(pd.Timestamp(t["exit_time"]))
        except (ValueError, KeyError):
            xi = fi
        color = "#00c853" if t["dir"] == "long" else "#d50000"
        marker = "^" if t["dir"] == "long" else "v"
        ax1.scatter(fi, t["entry"], marker=marker, color=color, s=120, zorder=5)
        ax1.scatter(xi, t["exit"], marker="x", color="white", s=100, zorder=5)
        ax1.axhline(t["sl"], color="#ef5350", ls="--", lw=0.7, alpha=0.6)
        ax1.axhline(t["tp"], color="#26a69a", ls="--", lw=0.7, alpha=0.6)
        day_pnl += t.get("pnl", 0.0)

    if "atr" in day.columns:
        ax2.fill_between(range(len(day)), 0, day["atr"].values, alpha=0.4, color="#7986cb")
        ax2.set_ylabel("ATR", fontsize=9)

    ax1.set_title(
        f"{ticker} fib-fine — {date_str}  |  P&L net jour : {day_pnl:+.0f} $",
        fontsize=12,
        fontweight="bold",
    )
    ax1.set_facecolor("#1a1a2e")
    ax2.set_facecolor("#1a1a2e")
    fig.patch.set_facecolor("#13131f")
    ax1.tick_params(colors="white")
    ax2.tick_params(colors="white")
    plt.tight_layout()
    plt.savefig(output_path, dpi=110, bbox_inches="tight")
    plt.close(fig)
