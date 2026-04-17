"""
Score composite et features de volatilité pré-marché.

Combine qualité de zone, force de tendance, contexte pré-marché et
régime de volatilité en un score unique 0-100 utilisé pour un filtrage
ultra-sélectif des signaux (orienté challenge Topstep).
"""

import math
from typing import Dict, Optional

import numpy as np
import pandas as pd

from config import (
    COMPOSITE_WEIGHTS, COMPOSITE_SCORE_MIN,
    TREND_STRENGTH_MIN,
    ATR_OVN_PERIOD,
    ATR_RATIO_MIN, ATR_RATIO_MAX,
    GAP_ATR_MAX, OVN_RANGE_MAX,
    VOL_SCORE_CENTER, VOL_SCORE_TOL, ATR30_LOOKBACK_DAYS,
)


# ══════════════════════════════════════════════════════════════════════════════
# Utilitaires internes
# ══════════════════════════════════════════════════════════════════════════════

def _bell(x: float, center: float, tol: float) -> float:
    """Courbe en cloche ∈ [0, 1]. Maximum = 1.0 à x == center, décroît avec |x - center|."""
    if tol <= 0:
        return 1.0 if x == center else 0.0
    z = (x - center) / tol
    return float(math.exp(-0.5 * z * z))


def _atr_series(df: pd.DataFrame, period: int) -> pd.Series:
    """True Range puis moyenne mobile simple (ATR classique)."""
    high = df["high"]
    low = df["low"]
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


# ══════════════════════════════════════════════════════════════════════════════
# Features de volatilité pré-marché
# ══════════════════════════════════════════════════════════════════════════════

def compute_volatility_features(
    df_15m: pd.DataFrame,
    cutoff: pd.Timestamp,
    ticker: str,
) -> Optional[Dict]:
    """
    Calcule les features de volatilité disponibles au cutoff (~11h UTC).

    Utilise l'ATR journalier (bougies D1 reconstruites) comme référence — évite
    la superposition entre fenêtre 15m et overnight.

    Retourne :
        atr_daily     — ATR sur les ATR_OVN_PERIOD derniers jours (échelle journalière)
        atr_ratio     — ovn_range / atr_daily (régime de la nuit vs journée typique)
        gap_atr       — |open_ovn - close_session_J-1| / atr_daily
        ovn_range_atr — (ovn_high - ovn_low) / atr_daily (doublon de atr_ratio, conservé pour lisibilité)
        vol_score     — ∈ [0, 1], favorise atr_ratio ≈ VOL_SCORE_CENTER (nuit moyenne)
    """
    before = df_15m[df_15m.index < cutoff]
    if len(before) < 200:
        return None

    # ATR daily : reconstruire les bougies journalières sur la fenêtre de référence
    lookback_bars = ATR30_LOOKBACK_DAYS * 96
    daily_window = before.iloc[-lookback_bars:] if len(before) > lookback_bars else before
    daily = daily_window.resample("D").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    ).dropna()
    # Exclure la journée courante (partielle, post-cutoff non disponible)
    daily = daily[daily.index < cutoff.normalize()]
    if len(daily) < ATR_OVN_PERIOD + 2:
        return None
    atr_daily_series = _atr_series(daily, ATR_OVN_PERIOD)
    atr_daily = atr_daily_series.dropna().iloc[-1] if atr_daily_series.dropna().size > 0 else 0.0
    if atr_daily <= 0:
        return None

    # Overnight : 23h veille → cutoff
    day = cutoff.normalize()
    ovn_start = pd.Timestamp(f"{(day - pd.Timedelta(days=1)).strftime('%Y-%m-%d')} 23:00:00")
    ovn = before[(before.index >= ovn_start) & (before.index < cutoff)]
    if len(ovn) < 4:
        return None

    ovn_high = float(ovn["high"].max())
    ovn_low = float(ovn["low"].min())
    ovn_range = ovn_high - ovn_low
    ovn_range_atr = ovn_range / atr_daily

    # Session J-1 (13h-21h UTC) pour close de référence
    prev_close = None
    for offset in [1, 3]:
        prev_day = (day - pd.Timedelta(days=offset)).strftime("%Y-%m-%d")
        prev_s = pd.Timestamp(f"{prev_day} 13:00:00")
        prev_e = pd.Timestamp(f"{prev_day} 21:00:00")
        prev = before[(before.index >= prev_s) & (before.index <= prev_e)]
        if len(prev) > 8:
            prev_close = float(prev["close"].iloc[-1])
            break
    session_open = float(ovn["open"].iloc[0])
    gap = abs(session_open - prev_close) if prev_close is not None else 0.0
    gap_atr = gap / atr_daily

    # atr_ratio = volatilité overnight / volatilité journée typique
    atr_ratio = ovn_range_atr
    vol_score = _bell(atr_ratio, center=VOL_SCORE_CENTER, tol=VOL_SCORE_TOL)

    return {
        "atr_daily": float(atr_daily),
        "atr_ratio": float(atr_ratio),
        "gap_atr": float(gap_atr),
        "ovn_range_atr": float(ovn_range_atr),
        "vol_score": float(vol_score),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Score pré-marché normalisé ∈ [0, 1]
# ══════════════════════════════════════════════════════════════════════════════

def _pm_score(pm_features: Dict, ticker: str) -> float:
    """
    Convertit les features pré-marché en score ∈ [0, 1].

    - ovn_path_eff : efficacité directionnelle overnight, favorise > 0.15
    - prev_return  : return session J-1, signe utilisé comme contexte
    - prev_close_pos : position du close J-1 dans son range
    """
    if pm_features is None:
        return 0.5

    ovn_eff = pm_features.get("ovn_path_eff", 0.0) or 0.0
    prev_ret = pm_features.get("prev_return", 0.0) or 0.0
    close_pos = pm_features.get("prev_close_pos", 0.5) or 0.5

    s_eff = min(max(ovn_eff / 0.35, 0.0), 1.0)
    abs_ret = min(abs(prev_ret) / 1.5, 1.0)
    s_ret = 0.5 + 0.5 * math.tanh(2 * abs_ret - 1)
    s_pos = 1.0 - 2.0 * abs(close_pos - 0.5)
    s_pos = max(0.0, min(1.0, s_pos))

    if ticker == "NQ1":
        return 0.55 * s_eff + 0.25 * s_ret + 0.20 * s_pos
    if ticker == "MES1":
        return 0.30 * s_eff + 0.35 * s_ret + 0.35 * s_pos
    # YM1 et défaut
    return 0.40 * s_eff + 0.30 * s_ret + 0.30 * s_pos


# ══════════════════════════════════════════════════════════════════════════════
# Score composite principal
# ══════════════════════════════════════════════════════════════════════════════

def compute_composite_score(
    zone: Dict,
    alignment_score: float,
    pm_features: Optional[Dict],
    vol_features: Optional[Dict],
    ticker: str,
) -> Optional[float]:
    """
    Score composite 0-100 combinant zone, tendance, pré-marché et volatilité.

    Retourne None si un garde-fou hard rejette la configuration.
    """
    if vol_features is None:
        return None

    atr_ratio = vol_features["atr_ratio"]
    if atr_ratio < ATR_RATIO_MIN.get(ticker, 0.0):
        return None
    if atr_ratio > ATR_RATIO_MAX.get(ticker, float("inf")):
        return None
    if vol_features["gap_atr"] > GAP_ATR_MAX.get(ticker, float("inf")):
        return None
    if vol_features["ovn_range_atr"] > OVN_RANGE_MAX.get(ticker, float("inf")):
        return None
    if abs(alignment_score) < TREND_STRENGTH_MIN.get(ticker, 0.0):
        return None

    w = COMPOSITE_WEIGHTS
    s_zone = min(zone["quality"], 100) / 100.0
    s_trend = min(abs(alignment_score), 1.0)
    s_pm = _pm_score(pm_features, ticker)
    s_vol = vol_features["vol_score"]

    score = 100.0 * (
        w["zone_quality"] * s_zone
        + w["trend_alignment"] * s_trend
        + w["pm_context"] * s_pm
        + w["volatility"] * s_vol
    )
    return float(score)


def passes_composite_threshold(score: Optional[float], ticker: str) -> bool:
    """True si le score composite dépasse le seuil minimum de l'asset."""
    if score is None:
        return False
    return score >= COMPOSITE_SCORE_MIN.get(ticker, 0.0)
