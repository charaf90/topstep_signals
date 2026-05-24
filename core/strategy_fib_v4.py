"""
Stratégie Fibonacci v4 — extension data-driven de fib-v3.

Différences vs `core/strategy_fib.py` (fib-v3) :

  1. **Invalidation pivot break** (pendant pending) :
     Annule l'ordre limite si le prix casse le pivot d'impulse au-delà
     d'un buffer ATR (paramètre `pivot_break_buffer_atr`). L'idée :
     si le swing low (LONG) est traversé, la thèse de rebond est cassée
     → inutile de garder l'ordre.

  2. **Invalidation wick excess** (au fill) :
     Annule le fill si la barre qui touche le niveau Fib perce
     au-delà de ce niveau de plus de `wick_max × ATR`. C'est un signal
     de washout / liquidation forcée — données Phase 3 (sklearn) montrent
     que l'edge est très négatif au-delà de ce seuil
     (`wick_through_atr > seuil` → PF effondre à 0-0.4).

Les helpers (compute_atr, detect_pivots, find_last_impulse, build_signal)
sont importés du module fib-v3 — la logique de détection reste identique.

Seule la boucle principale est ré-implémentée pour intégrer les
2 conditions d'invalidation.

Schéma de colonnes du DataFrame de trades : identique à fib-v3 + features
fib-v4 (bars_to_fill, pivot_break_atr, mae_pending_atr, wick_through_atr,
is_macro_day, dist_to_ema_fast_atr, bar_color_streak_pre, volume_at_arm_norm).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from broker.m1_buffer import M1Buffer

from config import (
    FIB_ADX_PERIOD,
    FIB_ADX_TREND_THRESHOLD,
    FIB_ATR_PERIOD,
    FIB_EMA_FAST_PERIOD,
    FIB_EMA_SLOW_PERIOD,
    FIB_IMPULSE_LOOKBACK,
    FIB_MAX_HOLD_BARS,
    FIB_MAX_IMPULSE_BARS,
    FIB_MIN_IMPULSE_ATR,
    FIB_MIN_IMPULSE_ATR_PER_TICKER,
    FIB_ORDER_TIMEOUT_BARS,
    FIB_PIVOT_LEFT,
    FIB_PIVOT_RIGHT,
    FIB_SESSION_PER_TICKER,
    FIB_SL_ATR_MULT_PER_TICKER,
    FIB_TP_ATR_MULT_PER_TICKER,
    FIB_V4_ENABLED,
    FIB_V4_LEVEL_PER_TICKER,
    FIB_V4_PIVOT_BREAK_BUFFER_ATR_PER_TICKER,
    FIB_V4_SKIP_MACRO_PER_TICKER,
    FIB_V4_WICK_THROUGH_MAX_ATR_PER_TICKER,
    INSTRUMENTS,
    MACRO_EVENT_DATES,
    US_SESSION_END_UTC,
    US_SESSION_START_UTC,
)

# Helpers Fib partagés (extrait de strategy_fib.py vers fib_helpers.py
# lors du nettoyage 2026-05-19 — fib-v3 supprimée du code prod).
from core.fib_helpers import (
    build_signal,
    compute_adx,
    compute_atr,
    compute_ema,
    detect_pivots,
    detect_trend,
    find_last_impulse,
)

_SESSION_WINDOWS = {
    "us_session": (US_SESSION_START_UTC, US_SESSION_END_UTC),
    "no_nuit": (0, 21),
    "europe_us": (7, 17),
    "all": (0, 24),
}
_MACRO_DATES_SET = set(MACRO_EVENT_DATES)


def run_fib_v4_backtest(
    df: pd.DataFrame,
    ticker: str,
    fib_level: float | None = None,
    sl_mult: float | None = None,
    tp_mult: float | None = None,
    min_imp: float | None = None,
    pivot_break_buffer_atr: float | None = None,
    wick_max_atr: float | None = None,
    skip_macro: bool | None = None,
) -> pd.DataFrame:
    """
    Backtest fib-v4 (avec invalidation pivot break + wick excess).

    Paramètres surchargeables (sinon valeurs config.py per-ticker) :
      fib_level               : niveau Fibonacci
      sl_mult                 : SL en multiples d'ATR
      tp_mult                 : TP en multiples d'ATR
      min_imp                 : taille minimale impulse (×ATR)
      pivot_break_buffer_atr  : buffer pivot break (×ATR), 0 = strict
      wick_max_atr            : seuil maximum mèche au fill (×ATR)
    """
    if fib_level is None:
        fib_level = FIB_V4_LEVEL_PER_TICKER.get(ticker, 0.382)
    if sl_mult is None:
        sl_mult = FIB_SL_ATR_MULT_PER_TICKER[ticker]
    if tp_mult is None:
        tp_mult = FIB_TP_ATR_MULT_PER_TICKER[ticker]
    if min_imp is None:
        min_imp = FIB_MIN_IMPULSE_ATR_PER_TICKER.get(ticker, FIB_MIN_IMPULSE_ATR)
    if pivot_break_buffer_atr is None:
        pivot_break_buffer_atr = FIB_V4_PIVOT_BREAK_BUFFER_ATR_PER_TICKER.get(ticker, 0.0)
    if wick_max_atr is None:
        wick_max_atr = FIB_V4_WICK_THROUGH_MAX_ATR_PER_TICKER.get(ticker, 1e9)
    if skip_macro is None:
        skip_macro = FIB_V4_SKIP_MACRO_PER_TICKER.get(ticker, False)

    df = df.copy()
    df["atr"] = compute_atr(df, FIB_ATR_PERIOD)
    df["ema_fast"] = compute_ema(df["close"], FIB_EMA_FAST_PERIOD)
    df["ema_slow"] = compute_ema(df["close"], FIB_EMA_SLOW_PERIOD)
    df["adx"] = compute_adx(df, FIB_ADX_PERIOD)

    pivot_highs, pivot_lows = detect_pivots(df, FIB_PIVOT_LEFT, FIB_PIVOT_RIGHT)

    sess_key = FIB_SESSION_PER_TICKER.get(ticker, "us_session")
    s_h, e_h = _SESSION_WINDOWS.get(sess_key, (US_SESSION_START_UTC, US_SESSION_END_UTC))
    hours = df.index.hour
    in_session = (hours >= s_h) & (hours < e_h)

    n = len(df)
    trades: list[dict] = []
    pending: dict | None = None
    position: dict | None = None
    last_impulse_key = None
    warmup = max(FIB_EMA_SLOW_PERIOD, 250)

    for i in range(warmup, n):
        bar = df.iloc[i]

        # ── 1. Gestion position ouverte ──────────────────────────────────
        if position is not None:
            exit_price, exit_reason = None, None
            if position["direction"] == "long":
                hit_sl = bar["low"] <= position["sl"]
                hit_tp = bar["high"] >= position["tp"]
            else:
                hit_sl = bar["high"] >= position["sl"]
                hit_tp = bar["low"] <= position["tp"]
            if hit_sl and hit_tp:
                bull_bar = bar["close"] >= bar["open"]
                if position["direction"] == "long":
                    exit_price, exit_reason = (
                        (position["tp"], "TP") if bull_bar else (position["sl"], "SL")
                    )
                else:
                    exit_price, exit_reason = (
                        (position["tp"], "TP") if not bull_bar else (position["sl"], "SL")
                    )
            elif hit_sl:
                exit_price, exit_reason = position["sl"], "SL"
            elif hit_tp:
                exit_price, exit_reason = position["tp"], "TP"

            bars_held = i - position["fill_idx"]
            if exit_reason is None and bars_held >= FIB_MAX_HOLD_BARS:
                exit_price, exit_reason = float(bar["close"]), "TE"

            if exit_reason is not None:
                pnl_pts = (
                    (exit_price - position["entry"])
                    if position["direction"] == "long"
                    else (position["entry"] - exit_price)
                )
                dpp = INSTRUMENTS[position["ticker"]]["dollar_per_point"]
                pnl = position["n_ct"] * pnl_pts * dpp
                trades.append(
                    {
                        **{k: v for k, v in position.items() if k != "pending_idx"},
                        "exit": float(exit_price),
                        "exit_idx": int(i),
                        "exit_time": str(df.index[i]),
                        "result": exit_reason,
                        "pnl": float(pnl),
                        "bars_held": int(bars_held),
                        "date": df.index[i].strftime("%Y-%m-%d"),
                    }
                )
                position = None
            continue

        # ── 2. Gestion ordre limite pending ──────────────────────────────
        if pending is not None:
            # Update trackers excursion adverse
            pending["low_min_pending"] = min(pending["low_min_pending"], float(bar["low"]))
            pending["high_max_pending"] = max(pending["high_max_pending"], float(bar["high"]))
            atr_arm = pending.get("atr", float("nan"))

            # ── INVALIDATION #1 : pivot break ──
            # Annulation si le prix a cassé le pivot au-delà du buffer.
            if atr_arm and not pd.isna(atr_arm):
                if pending["direction"] == "long":
                    pivot_break_now = (pending["low_min_pending"] - pending["swing_low"]) / atr_arm
                else:
                    pivot_break_now = (
                        pending["swing_high"] - pending["high_max_pending"]
                    ) / atr_arm
                if pivot_break_now < -pivot_break_buffer_atr:
                    pending = None
                    continue

            # Check fill ordre limite
            level = pending["entry"]
            if bar["low"] <= level <= bar["high"]:
                # ── INVALIDATION #2 : wick excess ──
                # Si la mèche perce le niveau Fib trop profondément, c'est
                # un washout / liquidation — edge négatif data-driven.
                if atr_arm and not pd.isna(atr_arm):
                    if pending["direction"] == "long":
                        wick = (level - float(bar["low"])) / atr_arm
                    else:
                        wick = (float(bar["high"]) - level) / atr_arm
                    if wick > wick_max_atr:
                        pending = None
                        continue
                else:
                    wick = float("nan")

                # Calcul features finales au fill
                if pending["direction"] == "long":
                    pivot_break_final = (
                        pending["low_min_pending"] - pending["swing_low"]
                    ) / atr_arm
                    mae = (pending["entry"] - pending["low_min_pending"]) / atr_arm
                else:
                    pivot_break_final = (
                        pending["swing_high"] - pending["high_max_pending"]
                    ) / atr_arm
                    mae = (pending["high_max_pending"] - pending["entry"]) / atr_arm
                fill_day = df.index[i].strftime("%Y-%m-%d")

                position = {
                    **{k: v for k, v in pending.items() if k != "pending_idx"},
                    "fill_idx": int(i),
                    "fill_time": str(df.index[i]),
                    "bars_to_fill": int(i - pending["pending_idx"]),
                    "pivot_break_atr": float(pivot_break_final),
                    "mae_pending_atr": float(mae),
                    "wick_through_atr": float(wick),
                    "is_macro_day": bool(fill_day in _MACRO_DATES_SET),
                }
                pending = None
                # Check immédiat SL/TP sur la barre de fill
                if position["direction"] == "long":
                    hit_sl = bar["low"] <= position["sl"]
                    hit_tp = bar["high"] >= position["tp"]
                else:
                    hit_sl = bar["high"] >= position["sl"]
                    hit_tp = bar["low"] <= position["tp"]
                exit_price, exit_reason = None, None
                if hit_sl and hit_tp:
                    exit_price, exit_reason = position["sl"], "SL"  # conservateur
                elif hit_sl:
                    exit_price, exit_reason = position["sl"], "SL"
                elif hit_tp:
                    exit_price, exit_reason = position["tp"], "TP"
                if exit_reason is not None:
                    pnl_pts = (
                        (exit_price - position["entry"])
                        if position["direction"] == "long"
                        else (position["entry"] - exit_price)
                    )
                    dpp = INSTRUMENTS[position["ticker"]]["dollar_per_point"]
                    pnl = position["n_ct"] * pnl_pts * dpp
                    trades.append(
                        {
                            **position,
                            "exit": float(exit_price),
                            "exit_idx": int(i),
                            "exit_time": str(df.index[i]),
                            "result": exit_reason,
                            "pnl": float(pnl),
                            "bars_held": 0,
                            "date": df.index[i].strftime("%Y-%m-%d"),
                        }
                    )
                    position = None
                continue

            # Timeout pending
            bars_pending = i - pending["pending_idx"]
            if bars_pending >= FIB_ORDER_TIMEOUT_BARS:
                pending = None

        # ── 3. Génération nouveau signal ─────────────────────────────────
        if pending is not None or position is not None:
            continue
        if not bool(in_session[i]):
            continue
        # Skip macro days si configuré pour ce ticker (filtre data-driven Phase 6).
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
            FIB_ADX_TREND_THRESHOLD,
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
            FIB_PIVOT_RIGHT,
            min_imp,
            FIB_MAX_IMPULSE_BARS,
            FIB_IMPULSE_LOOKBACK,
            fib_level=fib_level,
        )
        if impulse is None:
            continue

        impulse_key = (impulse["direction"], impulse["pivot_low_idx"], impulse["pivot_high_idx"])
        if impulse_key == last_impulse_key:
            continue

        # Si prix a déjà passé fib_50 dans le sens du trade, on rate la zone
        if impulse["direction"] == "long" and bar["close"] <= impulse["fib_50"]:
            continue
        if impulse["direction"] == "short" and bar["close"] >= impulse["fib_50"]:
            continue

        sig = build_signal(impulse, float(atr_now), ticker, sl_mult, tp_mult)
        if sig is None:
            continue

        # Features à l'armement
        sig["bars_since_confirm"] = int(i - impulse["confirm_idx"])
        sig["adx_at_arm"] = float(bar["adx"]) if not pd.isna(bar["adx"]) else float("nan")
        sig["pending_idx"] = int(i)
        sig["pending_time"] = str(df.index[i])
        sig["fib_level"] = float(fib_level)
        sig["trend"] = trend
        sig["impulse_confirm_idx"] = impulse["confirm_idx"]
        # Trackers excursion (init avec la barre d'armement)
        sig["low_min_pending"] = float(bar["low"])
        sig["high_max_pending"] = float(bar["high"])
        last_impulse_key = impulse_key
        pending = sig

    return pd.DataFrame(trades)


# ═════════════════════════════════════════════════════════════════════════════
# État live — utilisé par broker/live_runner.py
# ═════════════════════════════════════════════════════════════════════════════


def _current_m15_extrema_from_m1(
    m1_buffer: M1Buffer,
    contract_id: str,
    last_close_m15_ts: pd.Timestamp,
) -> dict | None:
    """Agrège les bars M1 (closed + forming) appartenant à la M15 EN COURS
    pour reconstituer le low/high COURANT de cette M15.

    `last_close_m15_ts` = timestamp d'ouverture de la dernière barre M15
    présente dans le df (donc close). La M15 EN COURS débute à
    last_close_m15_ts + 15min.

    Retourne None si le buffer ne contient aucun M1 dans la fenêtre.
    """
    next_m15_start = last_close_m15_ts + pd.Timedelta(minutes=15)
    next_m15_end = next_m15_start + pd.Timedelta(minutes=15)
    # M1Buffer.get_bars_since attend un datetime UTC.
    bars = m1_buffer.get_bars_since(
        contract_id,
        next_m15_start.to_pydatetime(),
        include_forming=True,
    )
    bars = [b for b in bars if b.start_ts < next_m15_end.to_pydatetime()]
    if not bars:
        return None
    return {
        "low": float(min(b.low for b in bars)),
        "high": float(max(b.high for b in bars)),
        "n_m1": len(bars),
    }


def get_fib_v4_live_signal(
    df_15m: pd.DataFrame,
    ticker: str,
    m1_buffer: M1Buffer | None = None,
    contract_id: str | None = None,
) -> dict | None:
    """État live courant de la stratégie fib-v4.

    Différences vs `core.strategy_fib.get_fib_live_signal` (fib-v3) :

      • Conditions d'invalidation v4 (pivot break + wick excess) évaluées
        à la fois sur les bars M15 closes ET sur la M15 EN COURS reconstituée
        à partir du M1Buffer (si fourni).
      • Skip macro days si `FIB_V4_SKIP_MACRO_PER_TICKER[ticker]=True`.
      • Filtre trigger walk-forward fib-v3 (`FIB_TRIGGER_FILTERS_PER_TICKER`)
        **NON appliqué** ici — c'est une nouvelle stratégie, pas un héritage.

    Paramètres :
      df_15m       : DataFrame OHLCV 15min, dernière ligne = dernière M15 CLOSE
      ticker       : "MES1", "NQ1", "MGC1" (cf. FIB_V4_TICKERS)
      m1_buffer    : instance M1Buffer alimentée par le live runner.
                     Si None, la fonction se rabat sur les bars M15 closes
                     uniquement (équivalent dégradé — fenêtre d'évaluation
                     intra-bar perdue, à éviter en production).
      contract_id  : identifiant ProjectX du contrat (ex: "CON.F.US.MGC.M26"),
                     requis si m1_buffer fourni.

    Retourne None si rien d'actif, sinon dict :
      {
        "state":          "PENDING" | "ACTIVE" | "CANCEL" | "CANCEL_FILL",
        "signal":         dict (entry/sl/tp/n_ct/...),
        "impulse_key":    str (déduplication),
        "bars_remaining": int (timeout restant, si PENDING),
        "reason":         str (si CANCEL/CANCEL_FILL — raison data-driven),
        "current_m15_low":  float | None (si évalué intra-bar via M1Buffer),
        "current_m15_high": float | None,
      }

    "CANCEL"      → annuler l'ordre limite broker-side (invalidation pivot
                    pendant pending).
    "CANCEL_FILL" → la barre M15 en cours a fillé mais avec mèche excessive ;
                    annuler avant que le broker ne l'exécute (limit pas encore
                    placé) OU fermer immédiatement la position (si déjà fillée).
    """
    if not FIB_V4_ENABLED:
        return None
    if ticker not in FIB_V4_LEVEL_PER_TICKER:
        return None

    fib_level = FIB_V4_LEVEL_PER_TICKER[ticker]
    sl_mult = FIB_SL_ATR_MULT_PER_TICKER[ticker]
    tp_mult = FIB_TP_ATR_MULT_PER_TICKER[ticker]
    min_imp = FIB_MIN_IMPULSE_ATR_PER_TICKER.get(ticker, FIB_MIN_IMPULSE_ATR)
    pivot_break_buffer_atr = FIB_V4_PIVOT_BREAK_BUFFER_ATR_PER_TICKER.get(ticker, 0.0)
    wick_max_atr = FIB_V4_WICK_THROUGH_MAX_ATR_PER_TICKER.get(ticker, 1e9)
    skip_macro = FIB_V4_SKIP_MACRO_PER_TICKER.get(ticker, False)

    df = df_15m.copy()
    df["atr"] = compute_atr(df, FIB_ATR_PERIOD)
    df["ema_fast"] = compute_ema(df["close"], FIB_EMA_FAST_PERIOD)
    df["ema_slow"] = compute_ema(df["close"], FIB_EMA_SLOW_PERIOD)
    df["adx"] = compute_adx(df, FIB_ADX_PERIOD)

    pivot_highs, pivot_lows = detect_pivots(df, FIB_PIVOT_LEFT, FIB_PIVOT_RIGHT)

    sess_key = FIB_SESSION_PER_TICKER.get(ticker, "us_session")
    s_h, e_h = _SESSION_WINDOWS.get(sess_key, (US_SESSION_START_UTC, US_SESSION_END_UTC))
    hours = df.index.hour
    in_session = (hours >= s_h) & (hours < e_h)

    n = len(df)
    warmup = max(FIB_EMA_SLOW_PERIOD, 250)
    pending: dict | None = None
    position: dict | None = None
    last_impulse_key = None

    for i in range(warmup, n):
        bar = df.iloc[i]

        # ── 1. Position ouverte ────────────────────────────────────────────
        if position is not None:
            if position["direction"] == "long":
                hit_sl = bar["low"] <= position["sl"]
                hit_tp = bar["high"] >= position["tp"]
            else:
                hit_sl = bar["high"] >= position["sl"]
                hit_tp = bar["low"] <= position["tp"]
            if hit_sl or hit_tp:
                position = None
                continue
            if (i - position["fill_idx"]) >= FIB_MAX_HOLD_BARS:
                position = None
            continue

        # ── 2. Ordre limite pending ────────────────────────────────────────
        if pending is not None:
            # Trackers excursion sur les M15 closes
            pending["low_min_pending"] = min(pending["low_min_pending"], float(bar["low"]))
            pending["high_max_pending"] = max(pending["high_max_pending"], float(bar["high"]))
            atr_arm = pending.get("atr", float("nan"))

            # Invalidation pivot break (sur les M15 closes parcourues)
            if atr_arm and not pd.isna(atr_arm):
                if pending["direction"] == "long":
                    pb_now = (pending["low_min_pending"] - pending["swing_low"]) / atr_arm
                else:
                    pb_now = (pending["swing_high"] - pending["high_max_pending"]) / atr_arm
                if pb_now < -pivot_break_buffer_atr:
                    pending = None
                    continue

            # Check fill ordre limite sur la M15 close
            level = pending["entry"]
            if bar["low"] <= level <= bar["high"]:
                # Wick check au fill
                if atr_arm and not pd.isna(atr_arm):
                    if pending["direction"] == "long":
                        wick = (level - float(bar["low"])) / atr_arm
                    else:
                        wick = (float(bar["high"]) - level) / atr_arm
                    if wick > wick_max_atr:
                        pending = None
                        continue
                # Fill validé → position active
                position = {**pending, "fill_idx": int(i), "fill_time": str(df.index[i])}
                pending = None
                # Check immédiat SL/TP sur la bougie de fill
                if position["direction"] == "long":
                    hit_sl = bar["low"] <= position["sl"]
                    hit_tp = bar["high"] >= position["tp"]
                else:
                    hit_sl = bar["high"] >= position["sl"]
                    hit_tp = bar["low"] <= position["tp"]
                if hit_sl or hit_tp:
                    position = None
                continue
            # Timeout
            if (i - pending["pending_idx"]) >= FIB_ORDER_TIMEOUT_BARS:
                pending = None

        # ── 3. Génération nouveau signal ───────────────────────────────────
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
            FIB_ADX_TREND_THRESHOLD,
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
            FIB_PIVOT_RIGHT,
            min_imp,
            FIB_MAX_IMPULSE_BARS,
            FIB_IMPULSE_LOOKBACK,
            fib_level=fib_level,
        )
        if impulse is None:
            continue
        imp_key = (impulse["direction"], impulse["pivot_low_idx"], impulse["pivot_high_idx"])
        if imp_key == last_impulse_key:
            continue
        if impulse["direction"] == "long" and bar["close"] <= impulse["fib_50"]:
            continue
        if impulse["direction"] == "short" and bar["close"] >= impulse["fib_50"]:
            continue
        sig = build_signal(impulse, float(atr_now), ticker, sl_mult, tp_mult)
        if sig is None:
            continue
        sig["pending_idx"] = int(i)
        sig["pending_time"] = str(df.index[i])
        sig["fib_level"] = float(fib_level)
        sig["trend"] = trend
        sig["impulse_confirm_idx"] = impulse["confirm_idx"]
        sig["low_min_pending"] = float(bar["low"])
        sig["high_max_pending"] = float(bar["high"])
        last_impulse_key = imp_key
        pending = sig

    # ── 4. État final + évaluation intra-bar M15 EN COURS ─────────────────
    if position is not None:
        return {
            "state": "ACTIVE",
            "signal": position,
            "impulse_key": (
                f"{position['direction']}"
                f"_{position.get('swing_low', 0)}"
                f"_{position.get('swing_high', 0)}"
            ),
        }

    if pending is None:
        return None

    # PENDING — évaluer intra-bar via M1Buffer si dispo
    current_low = None
    current_high = None
    if m1_buffer is not None and contract_id is not None and len(df) > 0:
        try:
            last_m15_ts = df.index[-1]
            if not isinstance(last_m15_ts, pd.Timestamp):
                last_m15_ts = pd.Timestamp(last_m15_ts)
            extrema = _current_m15_extrema_from_m1(m1_buffer, contract_id, last_m15_ts)
            if extrema is not None:
                current_low = extrema["low"]
                current_high = extrema["high"]
        except Exception:
            current_low = current_high = None  # défensif — on retombe en mode bar-close

    atr_arm = pending.get("atr", float("nan"))
    impulse_key = (
        f"{pending['direction']}_{pending.get('swing_low', 0)}_{pending.get('swing_high', 0)}"
    )
    bars_remaining = FIB_ORDER_TIMEOUT_BARS - ((n - 1) - pending["pending_idx"])

    # Si on a une vue intra-bar, vérifier pivot break + wick courants
    if current_low is not None and current_high is not None and atr_arm and not pd.isna(atr_arm):
        # 1) Pivot break intra-bar
        if pending["direction"] == "long":
            pb_now = (min(pending["low_min_pending"], current_low) - pending["swing_low"]) / atr_arm
        else:
            pb_now = (
                pending["swing_high"] - max(pending["high_max_pending"], current_high)
            ) / atr_arm
        if pb_now < -pivot_break_buffer_atr:
            return {
                "state": "CANCEL",
                "signal": pending,
                "impulse_key": impulse_key,
                "reason": f"pivot_break_intra_bar (pb_now={pb_now:.3f} < {-pivot_break_buffer_atr})",
                "current_m15_low": current_low,
                "current_m15_high": current_high,
            }
        # 2) Wick courant si le niveau a déjà été touché intra-bar
        level = pending["entry"]
        if current_low <= level <= current_high:
            if pending["direction"] == "long":
                wick = (level - current_low) / atr_arm
            else:
                wick = (current_high - level) / atr_arm
            if wick > wick_max_atr:
                return {
                    "state": "CANCEL_FILL",
                    "signal": pending,
                    "impulse_key": impulse_key,
                    "reason": (
                        f"wick_through_atr={wick:.3f} > {wick_max_atr} intra-bar (M1 reconstructed)"
                    ),
                    "current_m15_low": current_low,
                    "current_m15_high": current_high,
                }

    return {
        "state": "PENDING",
        "signal": pending,
        "impulse_key": impulse_key,
        "bars_remaining": int(max(0, bars_remaining)),
        "current_m15_low": current_low,
        "current_m15_high": current_high,
    }
