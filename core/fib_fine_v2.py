"""
État live de la stratégie fib-fine-v2 — utilisé par broker/live_runner.py.

Mirror live de `strategies/fib_fine.py::run_backtest` (M5) avec FIDÉLITÉ ABSOLUE :
la machine d'états pending/fill est rejouée à l'identique sur les barres M5
CLÔTURÉES, puis arrêtée à l'état COURANT (PENDING / ACTIVE / CANCEL) au lieu de
finaliser des trades. C'est le pendant live de
`core/strategy_fib_v4.py::get_fib_v4_live_signal`, adapté au TF M5.

Différences vs fib-v4 live :
  • TF M5 (pas M15) : structure pivots/timeouts depuis FIB_FINE_STRUCT_PER_TF["m5"].
  • Filtre wick LOOK-AHEAD RETIRÉ. Remplacé par le filtre CAUSAL d'expansion de
    volatilité `passes_atr_expansion_filter` (lit atr_short à la barre i-1 = close
    AVANT le fill) → no look-ahead PAR CONSTRUCTION. AUCUN M1Buffer requis.
  • Session DST-aware NY (zoneinfo America/New_York), comme le backtest.
  • Sizing dédié FIB_FINE_RISK_USD ($130), pas le RISK_PER_TRADE_USD global.
  • Time-stop intraday (`max_hold_bars` M5 OU changement de jour) géré côté
    live_runner._sync_broker (fidèle au backtest qui produit un TE).

LIVE-ÉQUIVALENCE : toutes les features de décision (trend, impulse, expansion de
volatilité) sont calculées sur des barres M5 CLÔTURÉES strictement avant le fill.
Les helpers Fib purs (compute_atr/ema/adx, detect_pivots, find_last_impulse,
build_signal, detect_trend) sont importés en LECTURE SEULE de core/fib_helpers.py.
Les fonctions causales `passes_atr_expansion_filter` / `add_atr_short` et la
neutralisation des rolls sont réutilisées (import) depuis strategies/fib_fine.py.

Cette fonction n'utilise QUE des barres M5 closes (le caller fournit un df dont
la dernière ligne est la dernière M5 CLÔTURÉE).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from broker.m1_buffer import M1Buffer

from config import (
    FIB_FINE_ADX_PERIOD,
    FIB_FINE_ADX_TREND_THRESHOLD,
    FIB_FINE_ATR_EXPANSION_MIN_PER_TICKER,
    FIB_FINE_ATR_EXPANSION_PERIOD,
    FIB_FINE_ATR_PERIOD,
    FIB_FINE_EMA_FAST_PERIOD,
    FIB_FINE_EMA_SLOW_PERIOD,
    FIB_FINE_ENABLED,
    FIB_FINE_LEVEL_PER_TICKER,
    FIB_FINE_LIVE_TF,
    FIB_FINE_MIN_IMPULSE_ATR_PER_TICKER,
    FIB_FINE_PIVOT_BREAK_BUFFER_ATR_PER_TICKER,
    FIB_FINE_RISK_USD,
    FIB_FINE_ROLL_GAP_PCT,
    FIB_FINE_SESSION_PER_TICKER,
    FIB_FINE_SESSION_WINDOWS_NY,
    FIB_FINE_SKIP_MACRO_PER_TICKER,
    FIB_FINE_SL_ATR_MULT_PER_TICKER,
    FIB_FINE_STRUCT_PER_TF,
    FIB_FINE_TP_ATR_MULT_PER_TICKER,
    INSTRUMENTS,
    MACRO_EVENT_DATES,
    RISK_PER_TRADE_USD,
)

# Helpers Fib partagés — LECTURE SEULE (module pur, sans état).
from core.fib_helpers import (
    build_signal,
    compute_adx,
    compute_atr,
    compute_ema,
    detect_pivots,
    detect_trend,
    find_last_impulse,
)

# Réutilisation directe (import) des fonctions causales de la stratégie de
# recherche → FIDÉLITÉ ABSOLUE (même code que le backtest qui a produit le verdict).
from strategies.fib_fine import (
    _flag_roll_gaps,
    add_atr_short,
    passes_atr_expansion_filter,
)

# Fuseau NY pour le filtrage de session DST-aware (pattern projet, cf. core/opr.py).
_NY_TZ = ZoneInfo("America/New_York")

_SESSION_WINDOWS_NY = dict(FIB_FINE_SESSION_WINDOWS_NY)
_MACRO_DATES_SET = set(MACRO_EVENT_DATES)

# Reproductibilité (aucun aléatoire dans cette fonction, par précaution projet).
np.random.seed(42)


def get_fib_fine_live_signal(
    df_m5: pd.DataFrame,
    ticker: str,
    m1_buffer: M1Buffer | None = None,
    contract_id: str | None = None,
) -> dict | None:
    """État live courant de la stratégie fib-fine-v2 sur barres M5 closes.

    Paramètres :
      df_m5        : DataFrame OHLCV M5, dernière ligne = dernière M5 CLÔTURÉE.
      ticker       : "NQ1" ou "MES1" (univers live v2, cf. FIB_FINE_LIVE_TICKERS).
      m1_buffer    : INUTILISÉ (signature alignée sur get_fib_v4_live_signal pour
                     homogénéité du runner). Le filtre causal atr_ratio_sl ne lit
                     que des barres M5 closes → aucun M1Buffer requis.
      contract_id  : INUTILISÉ (idem). Conservé pour parité de signature.

    Retourne None si rien d'actif, sinon dict :
      {
        "state":          "PENDING" | "ACTIVE" | "CANCEL",
        "signal":         dict (entry/sl/tp/n_ct/... + strategy="FIB_FINE"),
        "impulse_key":    str (déduplication),
        "bars_remaining": int (timeout restant, si PENDING),
        "reason":         str (si CANCEL — pivot break sur M5 closes),
      }

    "CANCEL" → annuler l'ordre limite broker-side (invalidation pivot break
               pendant la phase pending, mesurée sur M5 closes).

    Pas d'état "CANCEL_FILL" : le filtre wick look-ahead est neutralisé en v2 ;
    le filtre de remplacement (expansion de volatilité) est causal et tranche au
    moment du fill sur la barre M5 close — il n'y a donc rien à annuler intra-bar.
    """
    if not FIB_FINE_ENABLED:
        return None
    if ticker not in FIB_FINE_LEVEL_PER_TICKER:
        return None

    timeframe = FIB_FINE_LIVE_TF  # "m5"

    fib_level = FIB_FINE_LEVEL_PER_TICKER.get(ticker, 0.382)
    sl_mult = FIB_FINE_SL_ATR_MULT_PER_TICKER[ticker]
    tp_mult = FIB_FINE_TP_ATR_MULT_PER_TICKER[ticker]
    min_imp = FIB_FINE_MIN_IMPULSE_ATR_PER_TICKER.get(ticker, 1.5)
    atr_exp_min = FIB_FINE_ATR_EXPANSION_MIN_PER_TICKER.get(ticker, 0.0)
    pivot_break_buffer_atr = FIB_FINE_PIVOT_BREAK_BUFFER_ATR_PER_TICKER.get(ticker, 0.0)
    skip_macro = FIB_FINE_SKIP_MACRO_PER_TICKER.get(ticker, False)
    risk_usd = FIB_FINE_RISK_USD or RISK_PER_TRADE_USD

    struct = FIB_FINE_STRUCT_PER_TF[timeframe]
    pivot_left = struct["pivot_left"]
    pivot_right = struct["pivot_right"]
    impulse_lookback = struct["impulse_lookback"]
    max_impulse_bars = struct["max_impulse_bars"]
    order_timeout_bars = struct["order_timeout_bars"]
    # max_hold_bars : géré côté _sync_broker (time-stop). Pas utilisé ici.

    df = df_m5.copy()
    if df.empty:
        return None

    instr = INSTRUMENTS[ticker]
    dpp = instr["dollar_per_point"]

    # Indicateurs causaux (identiques au backtest).
    df["atr"] = compute_atr(df, FIB_FINE_ATR_PERIOD)
    df["ema_fast"] = compute_ema(df["close"], FIB_FINE_EMA_FAST_PERIOD)
    df["ema_slow"] = compute_ema(df["close"], FIB_FINE_EMA_SLOW_PERIOD)
    df["adx"] = compute_adx(df, FIB_FINE_ADX_PERIOD)
    # v2 : ATR court terme pour le filtre causal d'expansion de volatilité.
    df = add_atr_short(df, FIB_FINE_ATR_EXPANSION_PERIOD)

    # Neutralisation des gaps de roll (mirror backtest).
    roll_gap_mask = _flag_roll_gaps(df, FIB_FINE_ROLL_GAP_PCT)
    roll_cum = np.cumsum(roll_gap_mask.astype(np.int64))

    pivot_highs, pivot_lows = detect_pivots(df, pivot_left, pivot_right)

    # Session DST-aware NY (index UTC naïf en interne → conversion NY).
    sess_key = FIB_FINE_SESSION_PER_TICKER.get(ticker, "us_session")
    s_h, e_h = _SESSION_WINDOWS_NY.get(sess_key, (9, 17))
    idx_ny = df.index.tz_localize("UTC").tz_convert(_NY_TZ)
    hours = idx_ny.hour
    in_session = (hours >= s_h) & (hours < e_h)

    n = len(df)
    warmup = max(FIB_FINE_EMA_SLOW_PERIOD, pivot_left + pivot_right + 5, 250)
    pending: dict | None = None
    position: dict | None = None
    last_impulse_key = None

    def _swing_crosses_roll(idx_a: int, idx_b: int) -> bool:
        a, b = (idx_a, idx_b) if idx_a <= idx_b else (idx_b, idx_a)
        a = max(a, 0)
        b = min(b, n - 1)
        return roll_cum[b] != roll_cum[a]

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
            # Time-stop intraday (max_hold_bars OU changement de jour) : côté live
            # c'est _sync_broker qui close la position. Ici on se contente de
            # libérer l'état local pour rester fidèle au déroulé du backtest.
            bars_held = i - position["fill_idx"]
            new_day = df.index[i].date() != pd.Timestamp(position["fill_time"]).date()
            if bars_held >= struct["max_hold_bars"] or new_day:
                position = None
            continue

        # ── 2. Ordre limite pending ────────────────────────────────────────
        if pending is not None:
            pending["low_min_pending"] = min(pending["low_min_pending"], float(bar["low"]))
            pending["high_max_pending"] = max(pending["high_max_pending"], float(bar["high"]))
            atr_arm = pending.get("atr", float("nan"))

            # Invalidation #1 : pivot break (sur M5 closes parcourues).
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
                # v2 : Invalidation #2 — filtre CAUSAL d'expansion de volatilité.
                # Lit atr_short à i-1 (barre close AVANT le fill) → no look-ahead.
                if not passes_atr_expansion_filter(df, i, atr_arm, atr_exp_min):
                    pending = None
                    continue
                # Fill validé → position active.
                position = {
                    **{k: v for k, v in pending.items() if k != "pending_idx"},
                    "fill_idx": int(i),
                    "fill_time": str(df.index[i]),
                }
                pending = None
                # Check immédiat SL/TP sur la barre de fill (conservateur, SL-prio).
                if position["direction"] == "long":
                    hit_sl = bar["low"] <= position["sl"]
                    hit_tp = bar["high"] >= position["tp"]
                else:
                    hit_sl = bar["high"] >= position["sl"]
                    hit_tp = bar["low"] <= position["tp"]
                if hit_sl or hit_tp:
                    position = None
                continue

            # Timeout pending.
            if (i - pending["pending_idx"]) >= order_timeout_bars:
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

        # v2 : sizing dédié. build_signal dimensionne sur RISK_PER_TRADE_USD global
        # ($200) ; on re-dimensionne n_ct sur risk_usd ($130, MC DD Topstep).
        if risk_usd != RISK_PER_TRADE_USD and sig["sl_dist"] > 0:
            n_ct = int(risk_usd / (sig["sl_dist"] * dpp))
            if n_ct <= 0:
                continue
            sig["n_ct"] = int(n_ct)
            sig["risk"] = float(n_ct * sig["sl_dist"] * dpp)

        # Marque la stratégie pour la séparation des chemins live (event_logger,
        # signal_selector, time-stop _sync_broker). build_signal pose "FIB" ;
        # on surcharge en "FIB_FINE" pour ne JAMAIS croiser fib-v4.
        sig["strategy"] = "FIB_FINE"
        sig["pending_idx"] = int(i)
        sig["pending_time"] = str(df.index[i])
        sig["fib_level"] = float(fib_level)
        sig["trend"] = trend
        sig["low_min_pending"] = float(bar["low"])
        sig["high_max_pending"] = float(bar["high"])
        last_impulse_key = imp_key
        pending = sig

    # ── 4. État final ──────────────────────────────────────────────────────
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

    impulse_key = (
        f"{pending['direction']}_{pending.get('swing_low', 0)}_{pending.get('swing_high', 0)}"
    )
    bars_remaining = order_timeout_bars - ((n - 1) - pending["pending_idx"])

    # Invalidation pivot break courante (sur M5 closes) → CANCEL.
    atr_arm = pending.get("atr", float("nan"))
    if atr_arm and not pd.isna(atr_arm):
        if pending["direction"] == "long":
            pb_now = (pending["low_min_pending"] - pending["swing_low"]) / atr_arm
        else:
            pb_now = (pending["swing_high"] - pending["high_max_pending"]) / atr_arm
        if pb_now < -pivot_break_buffer_atr:
            return {
                "state": "CANCEL",
                "signal": pending,
                "impulse_key": impulse_key,
                "reason": f"pivot_break (pb_now={pb_now:.3f} < {-pivot_break_buffer_atr})",
            }

    return {
        "state": "PENDING",
        "signal": pending,
        "impulse_key": impulse_key,
        "bars_remaining": int(max(0, bars_remaining)),
    }
