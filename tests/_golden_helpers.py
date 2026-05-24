"""Helpers pour les golden master tests.

Construit un baseline déterministe pour chaque stratégie en exécutant
le backtest exact et en sérialisant la liste complète des trades.

Le format produit est un dict Python directement comparable :

    {
        "strategy_id": "opr-v4",
        "tickers": {
            "MES1": {
                "n_total":  <int>,
                "n_filled": <int>,
                "sum_pnl":  <float 2 décimales>,
                "trades":   [<dict canonique trade>, ...],
            },
            "NQ1":  {...},
            "YM1":  {...},
        },
    }

Tout champ float est arrondi (pnl: 2 décimales, prix: 4 décimales) pour
absorber le bruit numérique non-significatif. Les NaN sont convertis en
None pour permettre la sérialisation JSON.

Aucune dépendance sur le stdout du backtest : on appelle directement
`core.backtester.run_for_ticker` en mode silencieux.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pandas as pd

from config import INSTRUMENTS
from core import backtester
from core.data import build_timeframes, load_csv
from core.registry import load_strategy

# Champs canoniques du DataFrame de trades à figer dans le golden master.
# Reflète le schéma documenté dans CLAUDE.md.
_TRADE_FIELDS = [
    "date",
    "dir",
    "entry",
    "sl",
    "tp",
    "sl_dist",
    "tp_dist",
    "rr",
    "n_ct",
    "result",
    "pnl",
    "fill_time",
    "exit_time",
    "exit",
    "regime",
]

# Précision d'arrondi par champ — au cent près sur les PnL, au tick près sur les prix.
_ROUND_2 = {"pnl", "sl_dist", "tp_dist", "rr"}
_ROUND_4 = {"entry", "sl", "tp", "exit"}


def _normalize_value(field: str, value: Any) -> Any:
    """Sérialise une valeur de trade en type JSON-compatible et déterministe."""
    if value is None:
        return None
    # NaN / NaT
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, pd.Timestamp):
        if pd.isna(value):
            return None
        return value.isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    # Arrondis numériques
    if isinstance(value, (int, float)):
        if field in _ROUND_2:
            return round(float(value), 2)
        if field in _ROUND_4:
            return round(float(value), 4)
        if isinstance(value, int):
            return int(value)
        return round(float(value), 6)
    return str(value)


def _serialize_trade(row: pd.Series) -> dict[str, Any]:
    """Sérialise une ligne de df_trades en dict canonique."""
    out: dict[str, Any] = {}
    for field in _TRADE_FIELDS:
        if field not in row.index:
            continue
        out[field] = _normalize_value(field, row[field])
    return out


def _summarize(df_trades: pd.DataFrame) -> dict[str, Any]:
    """Résume un DataFrame de trades en bloc déterministe."""
    if df_trades is None or len(df_trades) == 0:
        return {"n_total": 0, "n_filled": 0, "sum_pnl": 0.0, "trades": []}

    filled_mask = (
        df_trades["result"] != "NOT_FILLED"
        if "result" in df_trades.columns
        else pd.Series([True] * len(df_trades))
    )
    n_filled = int(filled_mask.sum())
    sum_pnl = float(df_trades.loc[filled_mask, "pnl"].fillna(0).sum()) if n_filled else 0.0

    # Tri stable : par date puis fill_time si présent
    sort_cols = [c for c in ["date", "fill_time"] if c in df_trades.columns]
    df_sorted = df_trades.sort_values(sort_cols).reset_index(drop=True) if sort_cols else df_trades

    return {
        "n_total": int(len(df_sorted)),
        "n_filled": n_filled,
        "sum_pnl": round(sum_pnl, 2),
        "trades": [_serialize_trade(r) for _, r in df_sorted.iterrows()],
    }


def build_baseline(strategy_name: str, csv_dir: str | Path = "data") -> dict[str, Any]:
    """Reconstruit le baseline d'une stratégie (mêmes données, mêmes params)."""
    csv_dir = Path(csv_dir)
    module = load_strategy(strategy_name)
    strategy_id = getattr(module, "STRATEGY_ID", strategy_name)
    tickers = getattr(module, "TICKERS", list(INSTRUMENTS))
    tf_suffix = getattr(module, "CSV_TIMEFRAME", "m15")

    out: dict[str, Any] = {"strategy_id": strategy_id, "tickers": {}}

    for ticker in tickers:
        csv_path = csv_dir / f"{ticker}_data_{tf_suffix}.csv"
        if not csv_path.exists():
            out["tickers"][ticker] = {"missing_data": str(csv_path)}
            continue

        df_15m = load_csv(str(csv_path))
        tf = build_timeframes(df_15m)
        res = backtester.run_for_ticker(
            module,
            df_15m,
            ticker,
            tf=tf,
            plot=False,
            output_dir=None,
            verbose=False,
        )
        out["tickers"][ticker] = _summarize(res["df_trades"])

    return out
