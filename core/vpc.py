"""
core/vpc.py — exécution live de la stratégie Volume Profile Confluence (vpc-v4).

Promu en production le 2026-05-11 après skill new-strategy → verdict 🟢.

Architecture :
    Réutilise les helpers déjà validés en backtest (strategies/vpc.py) pour
    garantir une parité exacte backtest/live. C'est volontaire : toute
    divergence entre la fonction qui simule et celle qui place les ordres
    réels est une source d'écart inacceptable pour une stratégie financière.

Interface symétrique avec :
    • core/opr.py :: run_opr_day(df, ticker, day_ny)
    • core/strategy_fib.py :: get_fib_live_signal(df, ticker)

Cette fonction :
    get_vpc_live_signal(df_15m, ticker) -> Optional[Dict]

retourne :
    None                              si aucun signal sur la dernière barre
    {                                 sinon
      "state":     "PENDING",
      "signal":    { ticker, strategy="VPC", direction, entry, sl, tp,
                     sl_dist, tp_dist, rr, n_ct, risk, setup, atr,
                     poc, vah, val, trigger_time, zone_low, zone_high },
      "setup_key": "YYYYMMDD_<setup>_<direction>",
    }

Le live_runner construit le tag d'idempotence
    VPC_{ticker}_{YYYYMMDD}_{direction}_{setup}
et le passe au broker en custom_tag.
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from config import (
    INSTRUMENTS,
    RISK_PER_TRADE_USD,
    SLIPPAGE_TICKS_PER_TICKER,
    MACRO_EVENT_DATES,
    VPC_ENABLED,
    VPC_TICKERS,
    VPC_STRATEGY_VERSION,
    VPC_HOUR_END_NY,
    VPC_OPEN_HOUR_NY, VPC_OPEN_MIN_NY,
    VPC_PROFILE_BUCKET_TICKS, VPC_VALUE_AREA_PCT,
    VPC_HVN_VOL_MULT, VPC_LVN_VOL_MULT,
    VPC_VOL_AVG_WINDOW, VPC_VOL_MULT_THRESHOLD,
    VPC_EMA_FAST_PERIOD, VPC_EMA_SLOW_PERIOD,
    VPC_ATR_PERIOD,
    VPC_SL_ATR_MULT_PER_TICKER, VPC_TP_ATR_MULT_PER_TICKER,
    VPC_SL_BUFFER_TICKS,
    VPC_ENABLE_OPEN_OUTSIDE, VPC_ENABLE_BREAKOUT_RETEST, VPC_ENABLE_HVN_REBOUND,
    VPC_HVN_ADX_MIN, VPC_HVN_HOUR_START_NY, VPC_HVN_HOUR_END_NY, VPC_HVN_MIN_RR,
    VPC_OPEN_OUTSIDE_FADE, VPC_OPEN_OUTSIDE_MIN_GAP_ATR,
    VPC_EXCLUDE_MACRO_DAYS, VPC_OPEN_OUTSIDE_ADX_MAX,
)
# Helpers validés en backtest — réutilisation directe pour parité parfaite
# avec strategies/vpc.py (zéro risque de divergence).
from strategies.vpc import (
    _compute_atr,
    _compute_adx,
    _compute_emas,
    _build_previous_day_profile,
    _detect_setup_open_outside,
    _detect_setup_breakout_retest,
    _detect_setup_hvn_rebound,
)


_NY = ZoneInfo("America/New_York")
_STRATEGY = "VPC"


def _build_signal(
    ticker: str,
    setup: str,
    direction: str,
    entry: float,
    sl: float,
    tp: float,
    atr: float,
    profile: Dict,
    setup_low: float,
    setup_high: float,
    trigger_time: pd.Timestamp,
) -> Optional[Dict]:
    """
    Calcule sizing + assemble le dict signal attendu par live_runner.
    Retourne None si sizing impossible (sl_dist nul, n_ct=0, etc.).
    """
    instr = INSTRUMENTS[ticker]
    tick_size = instr["tick_size"]
    pt_value  = instr["dollar_per_point"]
    slip_px   = SLIPPAGE_TICKS_PER_TICKER.get(ticker, 1) * tick_size

    # Arrondi au tick (essentiel pour place_limit_order côté broker)
    def _to_tick(p):
        return round(round(p / tick_size) * tick_size, 10)

    entry = _to_tick(entry)
    sl    = _to_tick(sl)
    tp    = _to_tick(tp)

    sl_dist = abs(entry - sl)
    tp_dist = abs(tp - entry)
    if sl_dist <= 0 or tp_dist <= 0:
        return None

    # Sizing : RISK_PER_TRADE_USD / (sl_dist + slippage) × point_value
    risk_per_contract = (sl_dist + slip_px) * pt_value
    if risk_per_contract <= 0:
        return None
    n_ct = max(1, int(RISK_PER_TRADE_USD / risk_per_contract))

    rr = tp_dist / sl_dist
    realized_risk = n_ct * sl_dist * pt_value   # ne compte pas le slippage (cohérent OPR/Fib)

    return {
        "ticker":       ticker,
        "strategy":     _STRATEGY,
        "direction":    direction,
        "entry":        float(entry),
        "sl":           float(sl),
        "tp":           float(tp),
        "sl_dist":      float(sl_dist),
        "tp_dist":      float(tp_dist),
        "rr":           float(round(rr, 3)),
        "n_ct":         int(n_ct),
        "risk":         float(round(realized_risk, 2)),
        # Méta-données VPC (pour log et debug)
        "setup":        setup,
        "atr":          float(atr),
        "poc":          float(profile["poc"]),
        "vah":          float(profile["vah"]),
        "val":          float(profile["val"]),
        "trigger_time": str(trigger_time),
        # Champs neutres pour cohérence avec les schémas signal OPR/Fib
        "zone_low":     float(setup_low),
        "zone_high":    float(setup_high),
        "quality":      0.0, "composite": 0.0, "alignment": 0.0,
        "atr_ratio":    0.0, "n_tf": 1, "touches": 0,
        "regime":       "VPC", "tp_type": "atr",
    }


def get_vpc_live_signal(
    df_15m: pd.DataFrame,
    ticker: str,
) -> Optional[Dict]:
    """
    Détecte un signal VPC sur la DERNIÈRE BARRE FERMÉE du DataFrame.

    Args:
        df_15m : DataFrame OHLCV M15, index UTC naïf (cohérent avec le projet).
                 ≥ 2 000 barres recommandé pour le warmup EMA50 + profil veille.
        ticker : "MES1" | "NQ1" (YM1 hors-périmètre vpc-v4).

    Retourne None si :
      • Stratégie désactivée
      • Ticker hors-périmètre
      • Pas assez de données pour le warmup
      • Pas de profil veille reconstructible
      • Aucun des 3 setups ne matche sur la dernière barre fermée
      • Jour macro et VPC_EXCLUDE_MACRO_DAYS=True
      • Hors fenêtre horaire NY [9h30, 16h00[
    """
    if not VPC_ENABLED:
        return None
    if ticker not in VPC_TICKERS:
        return None
    if df_15m is None or len(df_15m) < 100:
        return None

    # Conversion index → NY pour le test fenêtre + jour
    if df_15m.index.tz is None:
        idx_ny_all = df_15m.index.tz_localize("UTC").tz_convert(_NY)
    else:
        idx_ny_all = df_15m.index.tz_convert(_NY)

    # Dernière barre FERMÉE = df_15m.iloc[-1] (le live_runner appelle
    # `_fetch_bars(include_partial=False)` donc on a bien la dernière barre close)
    i = len(df_15m) - 1
    bar = df_15m.iloc[i]
    prev = df_15m.iloc[i - 1] if i > 0 else None
    ts_ny = idx_ny_all[i]

    # ── Filtres temporels ────────────────────────────────────────────────
    if ts_ny.weekday() >= 5:
        return None

    hr, mn = ts_ny.hour, ts_ny.minute
    # Fenêtre globale NY [9h30, 16h00[
    if hr < VPC_OPEN_HOUR_NY:
        return None
    if hr == VPC_OPEN_HOUR_NY and mn < VPC_OPEN_MIN_NY:
        return None
    if hr >= VPC_HOUR_END_NY:
        return None

    date_str = ts_ny.strftime("%Y-%m-%d")
    if VPC_EXCLUDE_MACRO_DAYS and date_str in MACRO_EVENT_DATES:
        return None

    # ── Indicateurs (calcul sur tout le DF, sans look-ahead) ─────────────
    df = df_15m.copy()
    df["atr"] = _compute_atr(df, VPC_ATR_PERIOD)
    df["adx"] = _compute_adx(df, 14)
    df["ema_fast"], df["ema_slow"] = _compute_emas(
        df, VPC_EMA_FAST_PERIOD, VPC_EMA_SLOW_PERIOD,
    )
    df["vol_avg"] = df["volume"].rolling(
        VPC_VOL_AVG_WINDOW, min_periods=10
    ).mean()

    bar  = df.iloc[i]
    prev = df.iloc[i - 1]

    atr_val = float(bar["atr"]) if pd.notna(bar["atr"]) else 0.0
    if atr_val <= 0:
        return None
    ema_fast_v = float(bar["ema_fast"]) if pd.notna(bar["ema_fast"]) else None
    ema_slow_v = float(bar["ema_slow"]) if pd.notna(bar["ema_slow"]) else None
    if ema_fast_v is None or ema_slow_v is None:
        return None

    adx_val = float(bar["adx"]) if pd.notna(bar["adx"]) else 0.0
    avg_vol = float(prev["vol_avg"]) if pd.notna(prev["vol_avg"]) else 0.0

    instr = INSTRUMENTS[ticker]
    tick_size = instr["tick_size"]
    sl_atr_mult = VPC_SL_ATR_MULT_PER_TICKER.get(ticker, 1.5)
    tp_atr_mult = VPC_TP_ATR_MULT_PER_TICKER.get(ticker, 2.0)

    # ── Profil veille (recalculé à chaque appel — pas de cache live) ────
    profile = _build_previous_day_profile(
        df_15m, ts_ny.normalize(), tick_size,
        VPC_PROFILE_BUCKET_TICKS, VPC_VALUE_AREA_PCT,
        VPC_HVN_VOL_MULT, VPC_LVN_VOL_MULT,
    )
    if profile is None:
        return None

    # ── Setup 1 : OPEN_OUTSIDE (uniquement à 9h30 NY exact) ─────────────
    raw = None
    is_open_bar = (hr == VPC_OPEN_HOUR_NY and mn == VPC_OPEN_MIN_NY)
    if VPC_ENABLE_OPEN_OUTSIDE and is_open_bar:
        if adx_val < VPC_OPEN_OUTSIDE_ADX_MAX:
            raw = _detect_setup_open_outside(
                bar, bar, profile, tick_size,
                VPC_SL_BUFFER_TICKS, sl_atr_mult, tp_atr_mult, atr_val,
                fade=VPC_OPEN_OUTSIDE_FADE,
                min_gap_atr=VPC_OPEN_OUTSIDE_MIN_GAP_ATR,
            )

    # ── Setup 2 : BREAKOUT_RETEST (désactivé en vpc-v4 — garde le hook) ─
    if raw is None and VPC_ENABLE_BREAKOUT_RETEST:
        raw = _detect_setup_breakout_retest(
            bar, prev, profile, ema_fast_v, ema_slow_v,
            avg_vol, VPC_VOL_MULT_THRESHOLD, tick_size,
            VPC_SL_BUFFER_TICKS, sl_atr_mult, tp_atr_mult, atr_val,
        )

    # ── Setup 3 : HVN_REBOUND (fenêtre 10h-15h NY + filtres) ────────────
    if raw is None and VPC_ENABLE_HVN_REBOUND:
        if VPC_HVN_HOUR_START_NY <= hr < VPC_HVN_HOUR_END_NY:
            raw = _detect_setup_hvn_rebound(
                bar, profile, ema_fast_v, ema_slow_v,
                avg_vol, VPC_VOL_MULT_THRESHOLD, tick_size,
                VPC_SL_BUFFER_TICKS, sl_atr_mult, tp_atr_mult, atr_val,
                adx_val, VPC_HVN_MIN_RR,
            )

    if raw is None:
        return None

    signal = _build_signal(
        ticker       = ticker,
        setup        = raw["setup"],
        direction    = raw["direction"],
        entry        = float(raw["entry"]),
        sl           = float(raw["sl"]),
        tp           = float(raw["tp"]),
        atr          = atr_val,
        profile      = profile,
        setup_low    = float(raw.get("setup_low", profile["val"])),
        setup_high   = float(raw.get("setup_high", profile["vah"])),
        trigger_time = df.index[i],
    )
    if signal is None:
        return None

    # Clé d'idempotence : un seul setup VPC du même type, même direction
    # par jour. Le live_runner construit le tag broker à partir de ça.
    setup_key = f"{ts_ny.strftime('%Y%m%d')}_{raw['setup']}_{raw['direction']}"

    return {
        "state":     "PENDING",
        "signal":    signal,
        "setup_key": setup_key,
    }


__all__ = ["get_vpc_live_signal", "VPC_STRATEGY_VERSION"]
