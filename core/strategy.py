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
)
from core.zones import detect_zones
from core.trend import precompute_trends, get_regime
from core.premarket import compute_features as compute_pm, filter_pass as pm_filter


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
    buffer = SL_BUFFER_TICKS * tick

    # Tendance
    if trend_scores is None:
        trend_scores = precompute_trends(tf_dict)
    regime = get_regime(trend_scores, cutoff)
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

    # Prix actuel
    before = df_15m[df_15m.index <= cutoff]
    if len(before) < MIN_BARS_HISTORY:
        return []
    price_now = before["close"].iloc[-1]

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

        # Entrée au 1er quartile de la zone (côté prix actuel)
        # Long  → bas de zone + 25% de la largeur  → arrondi vers le bas
        # Short → haut de zone - 25% de la largeur → arrondi vers le haut
        zone_width = zone["high"] - zone["low"]
        if direction == "long":
            raw_entry = zone["low"] + 0.25 * zone_width
            entry = math.floor(raw_entry / tick) * tick
        else:
            raw_entry = zone["high"] - 0.25 * zone_width
            entry = math.ceil(raw_entry / tick) * tick

        # SL ancré au bord de la zone + buffer (comme v3)
        # TP calculé sur le RR × distance zone (mid → bord opposé + buffer)
        if direction == "long":
            sl_price = min(zone["low"] - buffer, entry - sl_min)
            sl_dist = entry - sl_price
            tp_dist = sl_dist * rr
            tp_price = entry + tp_dist
        else:
            sl_price = max(zone["high"] + buffer, entry + sl_min)
            sl_dist = sl_price - entry
            tp_dist = sl_dist * rr
            tp_price = entry - tp_dist

        # Sizing
        n_ct = int(RISK_PER_TRADE_USD / (sl_dist * dpp))
        if n_ct == 0:
            continue

        risk = n_ct * sl_dist * dpp
        gain = n_ct * tp_dist * dpp

        signals.append({
            "ticker": ticker,
            "direction": direction,
            "entry": entry,
            "sl": sl_price,
            "tp": tp_price,
            "sl_dist": sl_dist,
            "tp_dist": tp_dist,
            "rr": rr,
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
        })

    return signals


def generate_signals_zones_only(
    df_15m: pd.DataFrame,
    tf_dict: Dict[str, pd.DataFrame],
    ticker: str,
    cutoff: pd.Timestamp,
    trend_scores: Optional[Dict] = None,
    max_signals: int = 0,
) -> List[dict]:
    """
    Variante zones pures : pas de filtre tendance, pas de filtre pré-market.
    Tous les signaux qualifiés sont retournés (support → LONG, résistance → SHORT).
    """
    inst = INSTRUMENTS[ticker]
    dpp = inst["dollar_per_point"]
    tick = inst["tick_size"]
    sl_min = SL_MINIMUM[ticker]
    rr = RR_TARGET[ticker]
    quality_min = ZONE_QUALITY_MIN[ticker]
    buffer = SL_BUFFER_TICKS * tick

    # Zones (pas de tendance, pas de pré-market)
    zones = detect_zones(tf_dict, cutoff)
    if not zones:
        return []

    # Prix actuel
    before = df_15m[df_15m.index <= cutoff]
    if len(before) < MIN_BARS_HISTORY:
        return []
    price_now = before["close"].iloc[-1]

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

        direction = "long" if is_support else "short"

        zone_width = zone["high"] - zone["low"]
        if direction == "long":
            raw_entry = zone["low"] + 0.25 * zone_width
            entry = math.floor(raw_entry / tick) * tick
        else:
            raw_entry = zone["high"] - 0.25 * zone_width
            entry = math.ceil(raw_entry / tick) * tick

        if direction == "long":
            sl_price = min(zone["low"] - buffer, entry - sl_min)
            sl_dist = entry - sl_price
            tp_dist = sl_dist * rr
            tp_price = entry + tp_dist
        else:
            sl_price = max(zone["high"] + buffer, entry + sl_min)
            sl_dist = sl_price - entry
            tp_dist = sl_dist * rr
            tp_price = entry - tp_dist

        n_ct = int(RISK_PER_TRADE_USD / (sl_dist * dpp))
        if n_ct == 0:
            continue

        risk = n_ct * sl_dist * dpp
        gain = n_ct * tp_dist * dpp

        signals.append({
            "ticker": ticker,
            "direction": direction,
            "entry": entry,
            "sl": sl_price,
            "tp": tp_price,
            "sl_dist": sl_dist,
            "tp_dist": tp_dist,
            "rr": rr,
            "n_ct": n_ct,
            "risk": risk,
            "gain": gain,
            "quality": zone["quality"],
            "n_tf": zone["n_tf"],
            "touches": zone["touches"],
            "regime": "NONE",
            "zone_low": zone["low"],
            "zone_high": zone["high"],
            "price_now": price_now,
        })

    return signals


def generate_signals_zones_rsi(
    df_15m: pd.DataFrame,
    tf_dict: Dict[str, pd.DataFrame],
    ticker: str,
    cutoff: pd.Timestamp,
    trend_scores: Optional[Dict] = None,
    max_signals: int = 0,
) -> List[dict]:
    """
    Variante zones + RSI : pas de filtre tendance ni pré-market,
    mais enrichi avec le score RSI multi-TF (0-4).
    Le RSI n'est PAS un filtre — tous les signaux sont conservés.
    """
    from core.rsi import detect_rsi_zones, compute_rsi_score
    from config import RSI_BONUS_PER_TF

    inst = INSTRUMENTS[ticker]
    dpp = inst["dollar_per_point"]
    tick = inst["tick_size"]
    sl_min = SL_MINIMUM[ticker]
    rr = RR_TARGET[ticker]
    quality_min = ZONE_QUALITY_MIN[ticker]
    buffer = SL_BUFFER_TICKS * tick

    # Zones prix
    zones = detect_zones(tf_dict, cutoff)
    if not zones:
        return []

    # Zones RSI
    rsi_zones = detect_rsi_zones(tf_dict, cutoff)

    # Prix actuel
    before = df_15m[df_15m.index <= cutoff]
    if len(before) < MIN_BARS_HISTORY:
        return []
    price_now = before["close"].iloc[-1]

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

        direction = "long" if is_support else "short"

        # Score RSI
        rsi_score = compute_rsi_score(rsi_zones, tf_dict, cutoff, direction)
        rsi_bonus = rsi_score * RSI_BONUS_PER_TF

        zone_width = zone["high"] - zone["low"]
        if direction == "long":
            raw_entry = zone["low"] + 0.25 * zone_width
            entry = math.floor(raw_entry / tick) * tick
        else:
            raw_entry = zone["high"] - 0.25 * zone_width
            entry = math.ceil(raw_entry / tick) * tick

        if direction == "long":
            sl_price = min(zone["low"] - buffer, entry - sl_min)
            sl_dist = entry - sl_price
            tp_dist = sl_dist * rr
            tp_price = entry + tp_dist
        else:
            sl_price = max(zone["high"] + buffer, entry + sl_min)
            sl_dist = sl_price - entry
            tp_dist = sl_dist * rr
            tp_price = entry - tp_dist

        n_ct = int(RISK_PER_TRADE_USD / (sl_dist * dpp))
        if n_ct == 0:
            continue

        risk = n_ct * sl_dist * dpp
        gain = n_ct * tp_dist * dpp

        signals.append({
            "ticker": ticker,
            "direction": direction,
            "entry": entry,
            "sl": sl_price,
            "tp": tp_price,
            "sl_dist": sl_dist,
            "tp_dist": tp_dist,
            "rr": rr,
            "n_ct": n_ct,
            "risk": risk,
            "gain": gain,
            "quality": zone["quality"],
            "n_tf": zone["n_tf"],
            "touches": zone["touches"],
            "regime": "NONE",
            "zone_low": zone["low"],
            "zone_high": zone["high"],
            "price_now": price_now,
            "rsi_score": rsi_score,
            "rsi_bonus": rsi_bonus,
        })

    return signals


def simulate_trade(us_data: pd.DataFrame, signal: dict, dpp: float) -> dict:
    """
    Simule un trade sur la session US.
    Retourne le résultat : TP, SL, ou TE (time exit).
    """
    entry = signal["entry"]
    sl = signal["sl"]
    tp = signal["tp"]
    direction = signal["direction"]
    n_ct = signal["n_ct"]

    # Fill
    fill_idx = -1
    for i in range(len(us_data)):
        if direction == "long" and us_data["low"].iloc[i] <= entry:
            fill_idx = i
            break
        elif direction == "short" and us_data["high"].iloc[i] >= entry:
            fill_idx = i
            break

    if fill_idx < 0:
        return {"result": "NOT_FILLED", "pnl": 0, "exit": None,
                "fill_time": None, "exit_time": None}

    fill_time = us_data.index[fill_idx]

    # Résolution : bougie de fill — TP autorisé seulement si bougie dans le sens du trade
    # (sur OHLC, on infère le parcours intra-bougie via la direction de la bougie)
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
    