"""
Chargement des données et construction des timeframes.
Source : fichiers CSV locaux uniquement.
"""

import pandas as pd
from pathlib import Path
from typing import Dict


from config import INSTRUMENTS


def load_csv(filepath: str) -> pd.DataFrame:
    """Charge un CSV OHLCV 15 minutes, déduplique et trie."""
    df = pd.read_csv(filepath, parse_dates=["datetime"])
    df = df.drop_duplicates(subset=["datetime"], keep="last")
    df = df.sort_values("datetime").set_index("datetime")
    return df[["open", "high", "low", "close", "volume"]]


def build_timeframes(df_15m: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    """Construit D1, H4, H1 à partir du 15 minutes."""
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    return {
        "15m": df_15m,
        "H1":  df_15m.resample("1h").agg(agg).dropna(),
        "H4":  df_15m.resample("4h").agg(agg).dropna(),
        "D1":  df_15m.resample("D").agg(agg).dropna(),
    }
