"""
RSI multi-timeframe : calcul, détection de zones RSI, scoring.
Implémentation manuelle (numpy + pandas uniquement).
"""

import numpy as np
import pandas as pd
from typing import Dict, List

from config import (
    PIVOT_CONFIGS, RSI_PERIOD, RSI_ZONE_TOLERANCE,
    RSI_ZONE_NEAR, RSI_MIN_TOUCHES,
)
from core.zones import detect_pivots


def compute_rsi(series: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    """
    RSI de Wilder (lissage exponentiel, alpha = 1/period).
    Retourne une Series RSI 0-100.
    """
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)

    # ewm avec alpha=1/period reproduit le lissage de Wilder
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi


def detect_rsi_zones(tf_dict: Dict[str, pd.DataFrame],
                     cutoff: pd.Timestamp) -> List[dict]:
    """
    Détecte les zones S/R sur le RSI multi-timeframe.

    1. Calcule le RSI sur chaque TF
    2. Détecte les pivots RSI (swing highs/lows sur la série RSI)
    3. Clustering des pivots proches en zones RSI
    4. Classification support/résistance selon le type de pivots
    """
    _TF_FREQ = {"D1": "D", "H4": "4h", "H1": "h", "15m": "15min"}

    all_pivots = []

    for tf_name, cfg in PIVOT_CONFIGS.items():
        tf_cutoff = cutoff.floor(_TF_FREQ.get(tf_name, "15min"))
        data = tf_dict[tf_name][tf_dict[tf_name].index < tf_cutoff].iloc[-cfg["window"]:]
        if len(data) < 50:
            continue

        # Calculer le RSI sur les closes
        rsi = compute_rsi(data["close"], RSI_PERIOD)
        rsi_values = rsi.values

        # Détecter les pivots sur la série RSI
        # detect_pivots attend highs et lows ; pour le RSI (série unique),
        # on passe la même série pour les deux
        sh, sl = detect_pivots(rsi_values, rsi_values, cfg["left"], cfg["right"])

        n = len(data)
        for i in range(n):
            if np.isnan(rsi_values[i]):
                continue
            if not np.isnan(sh[i]):
                all_pivots.append({
                    "value": sh[i], "type": "high", "tf": tf_name,
                })
            if not np.isnan(sl[i]):
                all_pivots.append({
                    "value": sl[i], "type": "low", "tf": tf_name,
                })

    if len(all_pivots) < 3:
        return []

    # Clustering par valeur RSI (tolérance absolue, pas pourcentage)
    all_pivots.sort(key=lambda x: x["value"])
    tolerance = RSI_ZONE_TOLERANCE

    groups, current = [], [all_pivots[0]]
    for p in all_pivots[1:]:
        if abs(p["value"] - np.mean([x["value"] for x in current])) <= tolerance:
            current.append(p)
        else:
            groups.append(current)
            current = [p]
    groups.append(current)

    # Filtrage et classification
    zones = []
    for group in groups:
        if len(group) < RSI_MIN_TOUCHES:
            continue

        values = [p["value"] for p in group]
        tfs = set(p["tf"] for p in group)
        n_highs = sum(1 for p in group if p["type"] == "high")
        n_lows = sum(1 for p in group if p["type"] == "low")

        # Classification : majorité de lows → support RSI, majorité de highs → résistance RSI
        zone_type = "support" if n_lows > n_highs else "resistance"

        zones.append({
            "low": min(values),
            "high": max(values),
            "mid": np.mean(values),
            "type": zone_type,
            "touches": len(group),
            "n_tf": len(tfs),
            "tfs": tfs,
        })

    return zones


def compute_rsi_score(rsi_zones: List[dict],
                      tf_dict: Dict[str, pd.DataFrame],
                      cutoff: pd.Timestamp,
                      direction: str) -> int:
    """
    Compte le nombre de TFs où le RSI actuel est proche d'une zone RSI cohérente.

    LONG  → RSI près d'une zone support RSI (rebond haussier historique)
    SHORT → RSI près d'une zone résistance RSI (rebond baissier historique)

    Retourne rsi_score (0-4).
    """
    _TF_FREQ = {"D1": "D", "H4": "4h", "H1": "h", "15m": "15min"}
    target_type = "support" if direction == "long" else "resistance"

    aligned_tfs = set()

    for tf_name in PIVOT_CONFIGS:
        freq = _TF_FREQ.get(tf_name, "15min")
        tf_cutoff = cutoff.floor(freq)
        data = tf_dict[tf_name][tf_dict[tf_name].index < tf_cutoff]
        if len(data) < RSI_PERIOD + 1:
            continue

        # RSI actuel sur ce TF
        rsi = compute_rsi(data["close"], RSI_PERIOD)
        current_rsi = rsi.iloc[-1]
        if np.isnan(current_rsi):
            continue

        # Vérifier si le RSI actuel est proche d'une zone cohérente
        for zone in rsi_zones:
            if zone["type"] != target_type:
                continue
            if abs(current_rsi - zone["mid"]) <= RSI_ZONE_NEAR:
                aligned_tfs.add(tf_name)
                break

    return len(aligned_tfs)
