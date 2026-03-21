"""
Features pré-market calculées avant midi.
Seules les features statistiquement significatives (p < 0.10) sont utilisées.
"""

import numpy as np
import pandas as pd
from typing import Dict, Optional

from config import USE_PM_FILTER


def compute_features(df_15m: pd.DataFrame, cutoff: pd.Timestamp) -> Optional[Dict]:
    """
    Calcule les features pré-market disponibles à midi.
    
    Retourne :
        ovn_path_eff  — Efficacité directionnelle de la nuit (NQ, p=0.040)
        prev_return   — Return de la session J-1 (MES, p=0.031)
        prev_close_pos — Close position J-1 dans le range (MES, p=0.092)
    """
    day = cutoff.normalize()
    ds = day.strftime("%Y-%m-%d")

    # Overnight : 23h veille → cutoff
    ovn_start = pd.Timestamp(f"{(day - pd.Timedelta(days=1)).strftime('%Y-%m-%d')} 23:00:00")
    ovn = df_15m[(df_15m.index >= ovn_start) & (df_15m.index <= cutoff)]
    if len(ovn) < 4:
        return None

    f = {}

    # Path efficiency overnight
    net = abs(ovn["close"].iloc[-1] - ovn["open"].iloc[0])
    tot = (ovn["close"] - ovn["open"]).abs().sum()
    f["ovn_path_eff"] = net / tot if tot > 0 else 0

    # Session J-1 (13h-21h UTC de la veille)
    for offset in [1, 3]:  # 1 jour, ou 3 si weekend
        prev_day = (day - pd.Timedelta(days=offset)).strftime("%Y-%m-%d")
        prev_s = pd.Timestamp(f"{prev_day} 13:00:00")
        prev_e = pd.Timestamp(f"{prev_day} 21:00:00")
        prev = df_15m[(df_15m.index >= prev_s) & (df_15m.index <= prev_e)]
        if len(prev) > 8:
            break

    if len(prev) > 8:
        po, pc = prev["open"].iloc[0], prev["close"].iloc[-1]
        ph, pl = prev["high"].max(), prev["low"].min()
        f["prev_return"] = (pc - po) / po * 100
        f["prev_close_pos"] = (pc - pl) / (ph - pl) if (ph - pl) > 0 else 0.5
    else:
        f["prev_return"] = 0
        f["prev_close_pos"] = 0.5

    return f


def filter_pass(features: Dict, ticker: str) -> bool:
    """
    Filtre binaire par actif. Retourne True si le contexte est favorable.
    
    MES : prev_return < 0 OU prev_close_pos < 0.5 (p=0.031 / p=0.092)
    NQ  : ovn_path_eff > 0.10 (p=0.040)
    YM  : pas de filtre
    """
    if not USE_PM_FILTER.get(ticker, False):
        return True

    if ticker == "MES1":
        return features["prev_return"] < 0 or features["prev_close_pos"] < 0.5

    if ticker == "NQ1":
        return features["ovn_path_eff"] > 0.10

    return True
