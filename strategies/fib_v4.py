"""
Stratégie Fibonacci v4 — wrapper backtest/recherche.

Interface plug-and-play :
    run_backtest(df_15m, ticker, tf=None, params=None, topstep_guard=True)

Production : core/strategy_fib_v4.py
Ce module est le wrapper backtest/recherche uniquement.
"""
import pandas as pd

from config import (
    FIB_V4_STRATEGY_VERSION,
    FIB_V4_TICKERS,
    FIB_V4_LEVEL_PER_TICKER,
    FIB_V4_PIVOT_BREAK_BUFFER_ATR_PER_TICKER,
    FIB_V4_WICK_THROUGH_MAX_ATR_PER_TICKER,
    FIB_SL_ATR_MULT_PER_TICKER, FIB_TP_ATR_MULT_PER_TICKER,
    FIB_MIN_IMPULSE_ATR_PER_TICKER,
)
from core.strategy_fib_v4 import run_fib_v4_backtest

# ── Identité de la stratégie ──────────────────────────────────────────────
STRATEGY_ID = FIB_V4_STRATEGY_VERSION   # "fib-v4"
TICKERS     = FIB_V4_TICKERS            # ["MES1", "NQ1", "MGC1"]
CSV_SUFFIX  = "_fib_v4"

# Grille d'optimisation (utilisable via optimize.py — recherche uniquement)
PARAM_GRID = {
    "sl_mult":                 [0.50, 0.75, 1.00, 1.50, 2.00],
    "tp_mult":                 [1.00, 1.50, 2.00, 2.50, 3.00],
    "wick_max_atr":            [0.05, 0.10, 0.20, 0.40, 0.80],
    "pivot_break_buffer_atr":  [0.00, 0.10, 0.20],
}


def run_backtest(
    df_15m: pd.DataFrame,
    ticker: str,
    tf=None,
    params: dict = None,
    topstep_guard: bool = True,
) -> pd.DataFrame:
    """Backtest fib-v4 complet sur toute l'historique.

    params override (clé → valeur) :
        sl_mult, tp_mult, min_imp, fib_level,
        wick_max_atr, pivot_break_buffer_atr
    """
    p = params or {}
    return run_fib_v4_backtest(
        df_15m,
        ticker,
        fib_level=p.get("fib_level", FIB_V4_LEVEL_PER_TICKER.get(ticker)),
        sl_mult=p.get("sl_mult", FIB_SL_ATR_MULT_PER_TICKER.get(ticker)),
        tp_mult=p.get("tp_mult", FIB_TP_ATR_MULT_PER_TICKER.get(ticker)),
        min_imp=p.get("min_imp", FIB_MIN_IMPULSE_ATR_PER_TICKER.get(ticker)),
        pivot_break_buffer_atr=p.get(
            "pivot_break_buffer_atr",
            FIB_V4_PIVOT_BREAK_BUFFER_ATR_PER_TICKER.get(ticker, 0.0),
        ),
        wick_max_atr=p.get(
            "wick_max_atr",
            FIB_V4_WICK_THROUGH_MAX_ATR_PER_TICKER.get(ticker, 1e9),
        ),
    )
