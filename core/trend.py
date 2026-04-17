"""
Détection de la tendance multi-timeframe.
Score EMA triple sur D1, H4, H1 → régime BULL / BEAR / RANGE.
"""

import numpy as np
import pandas as pd
from typing import Dict, Optional

from config import (
    TREND_EMA_PARAMS, TREND_WEIGHTS,
    TREND_BULL_THRESHOLD, TREND_BEAR_THRESHOLD,
)


def precompute_trends(tf_dict: Dict[str, pd.DataFrame]) -> Dict[str, pd.Series]:
    """
    Pré-calcule le score de tendance pour chaque TF.
    Score = (sign(prix-EMA_fast) + sign(prix-EMA_slow) + sign(EMA_fast-EMA_slow)) / 3
    """
    scores = {}
    for tf_name, params in TREND_EMA_PARAMS.items():
        close = tf_dict[tf_name]["close"]
        ema_f = close.ewm(span=params["fast"], adjust=False).mean()
        ema_s = close.ewm(span=params["slow"], adjust=False).mean()
        scores[tf_name] = (
            np.sign(close - ema_f) + np.sign(close - ema_s) + np.sign(ema_f - ema_s)
        ) / 3
    return scores


def get_regime(trend_scores: Dict[str, pd.Series], cutoff: pd.Timestamp) -> Optional[str]:
    """Retourne BULL, BEAR ou RANGE à un instant donné."""
    # Use < floor(freq) to exclude the current unfinished bar of each timeframe.
    # The bar index is the period START but its close covers data until the period END.
    _TF_FREQ = {"D1": "D", "H4": "4h", "H1": "h"}
    alignment = 0
    for tf_name, weight in TREND_WEIGHTS.items():
        s = trend_scores[tf_name]
        tf_cutoff = cutoff.floor(_TF_FREQ.get(tf_name, "h"))
        available = s[s.index < tf_cutoff]
        if len(available) == 0:
            return None
        alignment += available.iloc[-1] * weight

    if alignment > TREND_BULL_THRESHOLD:
        return "BULL"
    elif alignment < TREND_BEAR_THRESHOLD:
        return "BEAR"
    return "RANGE"


def get_regime_with_score(
    trend_scores: Dict[str, pd.Series], cutoff: pd.Timestamp
) -> tuple:
    """Retourne (regime, alignment_score) à un instant donné."""
    _TF_FREQ = {"D1": "D", "H4": "4h", "H1": "h"}
    alignment = 0.0
    for tf_name, weight in TREND_WEIGHTS.items():
        s = trend_scores[tf_name]
        tf_cutoff = cutoff.floor(_TF_FREQ.get(tf_name, "h"))
        available = s[s.index < tf_cutoff]
        if len(available) == 0:
            return None, 0.0
        alignment += available.iloc[-1] * weight

    if alignment > TREND_BULL_THRESHOLD:
        regime = "BULL"
    elif alignment < TREND_BEAR_THRESHOLD:
        regime = "BEAR"
    else:
        regime = "RANGE"
    return regime, alignment
