"""
Détection des zones Support / Résistance multi-timeframe.
"""

import numpy as np
import pandas as pd
from typing import Dict, List

from config import (
    PIVOT_CONFIGS, ZONE_TOLERANCE_PCT, ZONE_MIN_TOUCHES,
    ZONE_MIN_TF_OR_TOUCHES, ZONE_MAX_WIDTH_PCT, ZONE_RECENCY_THRESHOLD,
)


def detect_pivots(highs: np.ndarray, lows: np.ndarray, left: int, right: int):
    """
    Swing highs / swing lows.
    Un swing high : high[i] est le max sur [i-left, i+right].
    """
    n = len(highs)
    sh = np.full(n, np.nan)
    sl = np.full(n, np.nan)
    for i in range(left, n - right):
        if highs[i] == highs[i - left : i + right + 1].max():
            sh[i] = highs[i]
        if lows[i] == lows[i - left : i + right + 1].min():
            sl[i] = lows[i]
    return sh, sl


def detect_zones(tf_dict: Dict[str, pd.DataFrame], cutoff: pd.Timestamp) -> List[dict]:
    """
    Détecte les zones S/R de haute qualité.

    1. Pivots sur chaque TF (uniquement données avant cutoff)
    2. Clustering des pivots proches en zones
    3. Filtrage qualité (touches, multi-TF, récence, largeur)
    4. Scoring et tri par qualité décroissante
    """
    # Map TF names to pandas freq strings for flooring — used to exclude the
    # current *unfinished* bar of each timeframe (its index is the period START
    # but its OHLC covers data until the period END, which is after cutoff).
    _TF_FREQ = {"D1": "D", "H4": "4h", "H1": "h", "15m": "15min"}

    all_pivots = []

    for tf_name, cfg in PIVOT_CONFIGS.items():
        tf_cutoff = cutoff.floor(_TF_FREQ.get(tf_name, "15min"))
        data = tf_dict[tf_name][tf_dict[tf_name].index < tf_cutoff].iloc[-cfg["window"]:]
        if len(data) < 50:
            continue

        sh, sl = detect_pivots(
            data["high"].values, data["low"].values, cfg["left"], cfg["right"]
        )
        n = len(data)
        for i in range(n):
            recency = i / max(n - 1, 1)
            is_recent = recency > ZONE_RECENCY_THRESHOLD
            if not np.isnan(sh[i]):
                all_pivots.append({
                    "price": sh[i], "tf": tf_name, "weight": cfg["weight"],
                    "recency": recency, "is_recent": is_recent,
                })
            if not np.isnan(sl[i]):
                all_pivots.append({
                    "price": sl[i], "tf": tf_name, "weight": cfg["weight"],
                    "recency": recency, "is_recent": is_recent,
                })

    if len(all_pivots) < 5:
        return []

    # Clustering
    all_pivots.sort(key=lambda x: x["price"])
    median_price = np.median([p["price"] for p in all_pivots])
    tolerance = median_price * ZONE_TOLERANCE_PCT

    groups, current = [], [all_pivots[0]]
    for p in all_pivots[1:]:
        if abs(p["price"] - np.mean([x["price"] for x in current])) <= tolerance:
            current.append(p)
        else:
            groups.append(current)
            current = [p]
    groups.append(current)

    # Filtrage et scoring
    zones = []
    for group in groups:
        n_touches = len(group)
        tfs = set(p["tf"] for p in group)

        if n_touches < ZONE_MIN_TOUCHES:
            continue
        if len(tfs) < ZONE_MIN_TF_OR_TOUCHES[0] and n_touches < ZONE_MIN_TF_OR_TOUCHES[1]:
            continue
        if not any(p["is_recent"] for p in group):
            continue

        prices = [p["price"] for p in group]
        zone_low, zone_high, zone_mid = min(prices), max(prices), np.mean(prices)
        if (zone_high - zone_low) / zone_mid > ZONE_MAX_WIDTH_PCT:
            continue

        quality = (
            min(n_touches, 8) / 8 * 30
            + min(len(tfs), 4) / 4 * 25
            + min(sum(p["weight"] for p in group) / 10, 1) * 15
            + np.mean([p["recency"] for p in group]) * 15
        )

        # Liste des TF contributrices triée par poids cumulé décroissant
        # (utilisée par les graphiques d'analyse pour colorer la zone par TF dominante)
        tf_weights = {}
        for p in group:
            tf_weights[p["tf"]] = tf_weights.get(p["tf"], 0.0) + p["weight"]
        tfs_sorted = sorted(tf_weights.keys(), key=lambda t: tf_weights[t], reverse=True)

        zones.append({
            "low": zone_low, "high": zone_high, "mid": zone_mid,
            "touches": n_touches, "n_tf": len(tfs), "quality": quality,
            "tfs": tfs_sorted,
            "dominant_tf": tfs_sorted[0],
        })

    zones.sort(key=lambda z: z["quality"], reverse=True)
    return zones
