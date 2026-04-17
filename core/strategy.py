"""
Moteur de la stratégie : génération des signaux d'ordres limites.
Combine zones S/R + tendance + filtres pré-market.
"""

import math
import pandas as pd
import numpy as np
from typing import Dict, List, Optional

from config import (
    INSTRUMENTS, SL_MINIMUM, RR_TARGET, ZONE_QUALITY_MIN,
    SL_BUFFER_TICKS, RISK_PER_TRADE_USD, MAX_TRADES_PER_DAY,
    ZONE_DISTANCE_MIN_PCT, ZONE_DISTANCE_MAX_PCT,
    CUTOFF_HOUR_UTC, US_SESSION_START_UTC, US_SESSION_END_UTC,
    MIN_BARS_HISTORY, MIN_BARS_US_SESSION,
    USE_ATR_BUFFER, ATR_PERIOD, ATR_BUFFER_MULT,
    USE_STRUCTURAL_TP, STRUCTURAL_TP_MIN_RR,
    USE_DYNAMIC_RR, DYNAMIC_RR_STRONG_MULT, DYNAMIC_RR_MODERATE_MULT,
    DYNAMIC_RR_RANGE_MULT, DYNAMIC_RR_MIN,
    USE_POC_ENTRY, POC_NUM_BINS,
    USE_SCALE_IN,
)
from core.zones import detect_zones
from core.trend import precompute_trends, get_regime_with_score
from core.premarket import compute_features as compute_pm, filter_pass as pm_filter


def _quartile_entry(zone: dict, direction: str, tick: float) -> float:
    """Entrée au 1er quartile de la zone (côté prix actuel)."""
    zone_width = zone["high"] - zone["low"]
    if direction == "long":
        raw = zone["low"] + 0.25 * zone_width
        return math.floor(raw / tick) * tick
    else:
        raw = zone["high"] - 0.25 * zone_width
        return math.ceil(raw / tick) * tick


def _poc_entry(zone: dict, before: pd.DataFrame, tick: float) -> Optional[float]:
    """Entrée au Point of Control (prix avec le plus de volume dans la zone)."""
    zone_width = zone["high"] - zone["low"]
    if zone_width <= 0:
        return None

    overlapping = before[
        (before["low"] <= zone["high"]) & (before["high"] >= zone["low"])
    ]
    if len(overlapping) == 0 or "volume" not in overlapping.columns:
        return None

    # Profil de volume simplifié : midpoints pondérés par le volume
    prices_mid = ((overlapping["high"] + overlapping["low"]) / 2).values
    prices_mid = np.clip(prices_mid, zone["low"], zone["high"])
    weights = overlapping["volume"].values

    if weights.sum() <= 0:
        return None

    hist, bin_edges = np.histogram(
        prices_mid, bins=POC_NUM_BINS,
        range=(zone["low"], zone["high"]),
        weights=weights,
    )
    poc_bin = np.argmax(hist)
    poc_price = (bin_edges[poc_bin] + bin_edges[poc_bin + 1]) / 2
    return round(poc_price / tick) * tick


def generate_signals(
    df_15m: pd.DataFrame,
    tf_dict: Dict[str, pd.DataFrame],
    ticker: str,
    cutoff: pd.Timestamp,
    trend_scores: Optional[Dict] = None,
    max_signals: int = 0,
) -> List[dict]:
    """
    Génère les signaux pour un actif à un instant donné.

    Args:
        max_signals: Nombre max de signaux à retourner. 0 = tous les qualifiés.
                     Pour le live, passer MAX_TRADES_PER_DAY.
                     Pour le backtest, laisser 0 (le backtest gère le fill).

    Retourne une liste de signaux triés par qualité de zone décroissante.
    """
    inst = INSTRUMENTS[ticker]
    dpp = inst["dollar_per_point"]
    tick = inst["tick_size"]
    sl_min = SL_MINIMUM[ticker]
    rr = RR_TARGET[ticker]
    quality_min = ZONE_QUALITY_MIN[ticker]

    # Données avant le cutoff (utilisé pour ATR, POC, prix actuel)
    before = df_15m[df_15m.index <= cutoff]
    if len(before) < MIN_BARS_HISTORY:
        return []
    price_now = before["close"].iloc[-1]

    # Buffer SL : ATR dynamique ou statique
    if USE_ATR_BUFFER:
        tr_high_low = before["high"] - before["low"]
        tr_high_close = (before["high"] - before["close"].shift(1)).abs()
        tr_low_close = (before["low"] - before["close"].shift(1)).abs()
        true_range = pd.concat([tr_high_low, tr_high_close, tr_low_close], axis=1).max(axis=1)
        atr = true_range.rolling(ATR_PERIOD).mean().iloc[-1]
        buffer = ATR_BUFFER_MULT * atr
        buffer = math.ceil(buffer / tick) * tick
    else:
        buffer = SL_BUFFER_TICKS * tick

    # Tendance
    if trend_scores is None:
        trend_scores = precompute_trends(tf_dict)
    regime, alignment_score = get_regime_with_score(trend_scores, cutoff)
    if regime is None:
        return []

    # Zones
    zones = detect_zones(tf_dict, cutoff)
    if not zones:
        return []

    # Pré-market
    pm = compute_pm(df_15m, cutoff)
    if pm is None:
        return []
    if not pm_filter(pm, ticker):
        return []

    signals = []
    for zone in zones:
        if max_signals > 0 and len(signals) >= max_signals:
            break

        if zone["quality"] < quality_min:
            continue

        zm = zone["mid"]
        is_support = zm < price_now
        is_resistance = zm > price_now
        if not is_support and not is_resistance:
            continue

        dist_pct = abs(price_now - zm) / price_now * 100
        if dist_pct < ZONE_DISTANCE_MIN_PCT or dist_pct > ZONE_DISTANCE_MAX_PCT:
            continue

        # Filtre tendance
        if is_support and regime == "BEAR":
            continue
        if is_resistance and regime == "BULL":
            continue

        direction = "long" if is_support else "short"

        # ── Entrée ──────────────────────────────────────────────
        if USE_POC_ENTRY:
            poc = _poc_entry(zone, before, tick)
            entry = poc if poc is not None else _quartile_entry(zone, direction, tick)
        else:
            entry = _quartile_entry(zone, direction, tick)

        # ── SL ──────────────────────────────────────────────────
        if direction == "long":
            sl_price = min(zone["low"] - buffer, entry - sl_min)
            sl_dist = entry - sl_price
        else:
            sl_price = max(zone["high"] + buffer, entry + sl_min)
            sl_dist = sl_price - entry

        # ── TP ──────────────────────────────────────────────────
        used_structural_tp = False
        skip_signal = False

        if USE_STRUCTURAL_TP:
            if direction == "long":
                next_zones = sorted(
                    [z for z in zones if z["low"] > entry and z is not zone],
                    key=lambda z: z["low"],
                )
            else:
                next_zones = sorted(
                    [z for z in zones if z["high"] < entry and z is not zone],
                    key=lambda z: z["high"], reverse=True,
                )

            if next_zones:
                if direction == "long":
                    tp_price = next_zones[0]["low"] - buffer
                    tp_dist = tp_price - entry
                else:
                    tp_price = next_zones[0]["high"] + buffer
                    tp_dist = entry - tp_price

                effective_rr = tp_dist / sl_dist if sl_dist > 0 else 0
                if effective_rr >= STRUCTURAL_TP_MIN_RR:
                    rr_used = round(effective_rr, 2)
                    used_structural_tp = True
                else:
                    skip_signal = True  # Zone trouvée mais RR insuffisant

        if skip_signal:
            continue

        if not used_structural_tp:
            # RR dynamique ou statique
            rr_effective = rr
            if USE_DYNAMIC_RR:
                abs_align = abs(alignment_score)
                if abs_align > 0.6:
                    rr_effective = rr * DYNAMIC_RR_STRONG_MULT
                elif abs_align > 0.33:
                    rr_effective = rr * DYNAMIC_RR_MODERATE_MULT
                else:
                    rr_effective = rr * DYNAMIC_RR_RANGE_MULT
                rr_effective = max(rr_effective, DYNAMIC_RR_MIN)

            tp_dist = sl_dist * rr_effective
            tp_price = entry + tp_dist if direction == "long" else entry - tp_dist
            rr_used = round(rr_effective, 2)

        # ── Scale-in ────────────────────────────────────────────
        scale_in_active = False
        entry_1 = entry
        entry_2 = entry
        n_ct_1 = 0
        n_ct_2 = 0

        if USE_SCALE_IN:
            zone_mid = round(zone["mid"] / tick) * tick
            # entry_2 doit être plus profond dans la zone
            if direction == "long" and zone_mid < entry:
                entry_2 = zone_mid
            elif direction == "short" and zone_mid > entry:
                entry_2 = zone_mid

        # ── Sizing ──────────────────────────────────────────────
        n_ct = int(RISK_PER_TRADE_USD / (sl_dist * dpp))
        if n_ct == 0:
            continue

        if USE_SCALE_IN and entry_1 != entry_2 and n_ct >= 2:
            n_ct_1 = n_ct // 2
            n_ct_2 = n_ct - n_ct_1
            scale_in_active = True
        else:
            n_ct_1 = n_ct
            n_ct_2 = 0
            entry_2 = entry  # annuler le scale-in

        risk = n_ct * sl_dist * dpp
        gain = n_ct * tp_dist * dpp

        sig = {
            "ticker": ticker,
            "direction": direction,
            "entry": entry,
            "sl": sl_price,
            "tp": tp_price,
            "sl_dist": sl_dist,
            "tp_dist": tp_dist,
            "rr": rr_used,
            "n_ct": n_ct,
            "risk": risk,
            "gain": gain,
            "quality": zone["quality"],
            "n_tf": zone["n_tf"],
            "touches": zone["touches"],
            "regime": regime,
            "zone_low": zone["low"],
            "zone_high": zone["high"],
            "price_now": price_now,
            "tp_type": "structural" if used_structural_tp else "rr",
        }

        if scale_in_active:
            sig["entry_1"] = entry_1
            sig["entry_2"] = entry_2
            sig["n_ct_1"] = n_ct_1
            sig["n_ct_2"] = n_ct_2
            sig["scale_in"] = True

        signals.append(sig)

    return signals


# ══════════════════════════════════════════════════════════════════════════════
# SIMULATION
# ══════════════════════════════════════════════════════════════════════════════

def _find_fill(us_data: pd.DataFrame, entry: float, direction: str) -> int:
    """Trouve l'index de la bougie de fill pour un prix d'entrée donné."""
    for i in range(len(us_data)):
        if direction == "long" and us_data["low"].iloc[i] <= entry:
            return i
        elif direction == "short" and us_data["high"].iloc[i] >= entry:
            return i
    return -1


def simulate_trade(us_data: pd.DataFrame, signal: dict, dpp: float) -> dict:
    """
    Simule un trade sur la session US.
    Retourne le résultat : TP, SL, ou TE (time exit).
    """
    if signal.get("scale_in", False):
        return _simulate_scale_in(us_data, signal, dpp)

    entry = signal["entry"]
    sl = signal["sl"]
    tp = signal["tp"]
    direction = signal["direction"]
    n_ct = signal["n_ct"]

    # Fill
    fill_idx = _find_fill(us_data, entry, direction)
    if fill_idx < 0:
        return {"result": "NOT_FILLED", "pnl": 0, "exit": None,
                "fill_time": None, "exit_time": None}

    fill_time = us_data.index[fill_idx]

    # Résolution : bougie de fill — TP autorisé seulement si bougie dans le sens du trade
    result = None
    exit_price = None
    exit_time = None

    bar = us_data.iloc[fill_idx]
    bougie_haussiere = bar["close"] >= bar["open"]

    if direction == "long":
        if bar["low"] <= sl:
            result = "SL"; exit_price = sl; exit_time = us_data.index[fill_idx]
        elif bougie_haussiere and bar["high"] >= tp:
            result = "TP"; exit_price = tp; exit_time = us_data.index[fill_idx]
    else:
        if bar["high"] >= sl:
            result = "SL"; exit_price = sl; exit_time = us_data.index[fill_idx]
        elif (not bougie_haussiere) and bar["low"] <= tp:
            result = "TP"; exit_price = tp; exit_time = us_data.index[fill_idx]

    # Bougies suivantes : SL d'abord, puis TP
    if result is None:
        for i in range(fill_idx + 1, len(us_data)):
            bar = us_data.iloc[i]
            if direction == "long":
                if bar["low"] <= sl:
                    result = "SL"; exit_price = sl; exit_time = us_data.index[i]; break
                if bar["high"] >= tp:
                    result = "TP"; exit_price = tp; exit_time = us_data.index[i]; break
            else:
                if bar["high"] >= sl:
                    result = "SL"; exit_price = sl; exit_time = us_data.index[i]; break
                if bar["low"] <= tp:
                    result = "TP"; exit_price = tp; exit_time = us_data.index[i]; break

    if result is None:
        result = "TE"
        exit_price = us_data["close"].iloc[-1]
        exit_time = us_data.index[-1]

    pnl_pts = (exit_price - entry) if direction == "long" else (entry - exit_price)
    pnl = n_ct * pnl_pts * dpp

    return {
        "result": result, "pnl": pnl, "exit": exit_price,
        "fill_time": str(fill_time), "exit_time": str(exit_time),
    }


def _simulate_scale_in(us_data: pd.DataFrame, signal: dict, dpp: float) -> dict:
    """Simule un trade avec 2 entrées fractionnées."""
    direction = signal["direction"]
    sl = signal["sl"]
    tp = signal["tp"]
    entry_1 = signal["entry_1"]
    entry_2 = signal["entry_2"]
    n_ct_1 = signal["n_ct_1"]
    n_ct_2 = signal["n_ct_2"]

    fill_1_idx = _find_fill(us_data, entry_1, direction)
    fill_2_idx = _find_fill(us_data, entry_2, direction)

    if fill_1_idx < 0 and fill_2_idx < 0:
        return {"result": "NOT_FILLED", "pnl": 0, "exit": None,
                "fill_time": None, "exit_time": None}

    first_fill = min(f for f in [fill_1_idx, fill_2_idx] if f >= 0)
    filled_1 = fill_1_idx >= 0
    filled_2 = fill_2_idx >= 0

    result = None
    exit_price = None
    exit_time = None

    for i in range(first_fill, len(us_data)):
        bar = us_data.iloc[i]

        # Vérifier si entry_2 se remplit sur cette bougie (ou avant)
        if not filled_2 and fill_2_idx >= 0 and i >= fill_2_idx:
            filled_2 = True
        if not filled_1 and fill_1_idx >= 0 and i >= fill_1_idx:
            filled_1 = True

        # SL check (prioritaire)
        if direction == "long" and bar["low"] <= sl:
            result = "SL"; exit_price = sl; exit_time = us_data.index[i]; break
        elif direction == "short" and bar["high"] >= sl:
            result = "SL"; exit_price = sl; exit_time = us_data.index[i]; break

        # TP check — bougie de fill : TP seulement si bougie dans le sens du trade
        if i == first_fill:
            bougie_haussiere = bar["close"] >= bar["open"]
            if direction == "long" and bougie_haussiere and bar["high"] >= tp:
                result = "TP"; exit_price = tp; exit_time = us_data.index[i]; break
            elif direction == "short" and (not bougie_haussiere) and bar["low"] <= tp:
                result = "TP"; exit_price = tp; exit_time = us_data.index[i]; break
        else:
            if direction == "long" and bar["high"] >= tp:
                result = "TP"; exit_price = tp; exit_time = us_data.index[i]; break
            elif direction == "short" and bar["low"] <= tp:
                result = "TP"; exit_price = tp; exit_time = us_data.index[i]; break

    if result is None:
        result = "TE"
        exit_price = us_data["close"].iloc[-1]
        exit_time = us_data.index[-1]

    # PnL combiné
    active_ct_1 = n_ct_1 if filled_1 else 0
    active_ct_2 = n_ct_2 if filled_2 else 0

    if direction == "long":
        pnl = (active_ct_1 * (exit_price - entry_1) + active_ct_2 * (exit_price - entry_2)) * dpp
    else:
        pnl = (active_ct_1 * (entry_1 - exit_price) + active_ct_2 * (entry_2 - exit_price)) * dpp

    return {
        "result": result, "pnl": pnl, "exit": exit_price,
        "fill_time": str(us_data.index[first_fill]), "exit_time": str(exit_time),
        "filled_legs": (1 if filled_1 else 0) + (1 if filled_2 else 0),
    }
