"""
Chargement des données et construction des timeframes.
Deux sources : fichiers CSV locaux ou TradingView (live).
"""

import pandas as pd
import time
from pathlib import Path
from typing import Dict, Optional

from config import INSTRUMENTS


def load_csv(filepath: str) -> pd.DataFrame:
    """Charge un CSV OHLCV 15 minutes, déduplique et trie."""
    df = pd.read_csv(filepath, parse_dates=["datetime"])
    df = df.drop_duplicates(subset=["datetime"], keep="last")
    df = df.sort_values("datetime").set_index("datetime")
    return df[["open", "high", "low", "close", "volume"]]


def fetch_live(ticker: str, bars: int = 10000) -> pd.DataFrame:
    """
    Récupère les données 15m live depuis TradingView.
    Nécessite tvDatafeed installé.
    """
    from tvDatafeed import TvDatafeed, Interval

    inst = INSTRUMENTS[ticker]
    tv = TvDatafeed()

    max_retries = 5
    for attempt in range(1, max_retries + 1):
        try:
            data = tv.get_hist(
                symbol=inst["tv_symbol"],
                exchange=inst["tv_exchange"],
                interval=Interval.in_15_minute,
                n_bars=bars,
            )
            if data is None or data.empty:
                if attempt < max_retries:
                    time.sleep(2)
                    continue
                return pd.DataFrame()

            data = data.rename(columns={
                "open": "open", "high": "high",
                "low": "low", "close": "close", "volume": "volume",
            })
            if "symbol" in data.columns:
                data = data.drop(columns="symbol")

            data.index.name = "datetime"
            return data[["open", "high", "low", "close", "volume"]]

        except Exception as e:
            print(f"  [!] Erreur {ticker} (tentative {attempt}): {e}")
            if attempt < max_retries:
                time.sleep(2)

    return pd.DataFrame()


def build_timeframes(df_15m: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    """Construit D1, H4, H1 à partir du 15 minutes."""
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    return {
        "15m": df_15m,
        "H1":  df_15m.resample("1h").agg(agg).dropna(),
        "H4":  df_15m.resample("4h").agg(agg).dropna(),
        "D1":  df_15m.resample("D").agg(agg).dropna(),
    }


def load_all(
    tickers: list,
    data_dir: Optional[str] = None,
    live: bool = False,
    bars: int = 10000,
) -> Dict[str, Dict[str, pd.DataFrame]]:
    """
    Charge les données pour tous les actifs.
    Retourne {ticker: {"15m": df, "H1": df, "H4": df, "D1": df}}.
    """
    result = {}
    for ticker in tickers:
        if live:
            print(f"  ▸ {ticker} — récupération live...")
            df_15m = fetch_live(ticker, bars=bars)
        else:
            path = Path(data_dir) / f"{ticker}_data_m15.csv"
            print(f"  ▸ {ticker} — chargement {path}...")
            df_15m = load_csv(str(path))

        if df_15m.empty:
            print(f"  [!] Aucune donnée pour {ticker}")
            continue

        print(f"    {len(df_15m):,} bougies [{df_15m.index.min()} → {df_15m.index.max()}]")
        result[ticker] = build_timeframes(df_15m)

    return result
