"""
Stratégie Fibonacci 38.2 % — fib-v2.

Interface plug-and-play :
    run_backtest(df_15m, ticker, tf=None, params=None, topstep_guard=True)

Production : core/strategy_fib.py (live_runner.py l'importe via get_fib_live_signal)
Ce module est le wrapper backtest/recherche uniquement.
"""

import pandas as pd

from config import (
    FIB_STRATEGY_VERSION,
    FIB_SL_ATR_MULT_PER_TICKER, FIB_TP_ATR_MULT_PER_TICKER,
    FIB_MIN_IMPULSE_ATR_PER_TICKER, FIB_LEVEL_PER_TICKER,
)
from core.strategy_fib import run_fib_backtest

# ── Identité de la stratégie ─────────────────────────────────────────────────
STRATEGY_ID = FIB_STRATEGY_VERSION          # "fib-v2"
TICKERS     = ["MES1", "NQ1", "YM1"]
CSV_SUFFIX  = "_fib"

# ── Grille d'optimisation ────────────────────────────────────────────────────
PARAM_GRID = {
    "sl_mult":  [0.50, 0.75, 1.00, 1.25, 1.50, 2.00],
    "tp_mult":  [1.00, 1.50, 2.00, 2.50, 3.00],
    "min_imp":  [1.00, 1.50, 2.00, 2.50],
}


# ══════════════════════════════════════════════════════════════════════════════
# Backtest
# ══════════════════════════════════════════════════════════════════════════════

def run_backtest(
    df_15m: pd.DataFrame,
    ticker: str,
    tf=None,
    params: dict = None,
    topstep_guard: bool = True,
) -> pd.DataFrame:
    """
    Backtest Fib complet sur toute l'historique.
    params = {"sl_mult": float, "tp_mult": float, "min_imp": float}

    Note : Fib traite la totalité du DataFrame en une passe (pas de boucle
    jour-par-jour). Le topstep_guard est ignoré pour conserver la cohérence
    avec le backtest original.
    """
    sl_mult = (params or {}).get("sl_mult", FIB_SL_ATR_MULT_PER_TICKER.get(ticker))
    tp_mult = (params or {}).get("tp_mult", FIB_TP_ATR_MULT_PER_TICKER.get(ticker))
    min_imp = (params or {}).get("min_imp", FIB_MIN_IMPULSE_ATR_PER_TICKER.get(ticker))
    fib_lvl = (params or {}).get("fib_level", FIB_LEVEL_PER_TICKER.get(ticker, 0.382))

    return run_fib_backtest(
        df_15m,
        ticker,
        fib_level=fib_lvl,
        sl_mult=sl_mult,
        tp_mult=tp_mult,
        min_imp=min_imp,
        apply_filter=True,
    )
