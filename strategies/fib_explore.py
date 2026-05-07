"""
Stratégie Fibonacci — COPIE EXPLORATOIRE sans restriction de session.
NE PAS utiliser en production.
"""

import pandas as pd

from config import (
    FIB_STRATEGY_VERSION,
    FIB_SL_ATR_MULT_PER_TICKER, FIB_TP_ATR_MULT_PER_TICKER,
    FIB_MIN_IMPULSE_ATR_PER_TICKER, FIB_LEVEL_PER_TICKER,
)
from core.strategy_fib_explore import run_fib_backtest

# ── Identité de la stratégie ─────────────────────────────────────────────────
STRATEGY_ID = "fib-explore"
TICKERS     = ["MES1", "NQ1", "YM1"]
CSV_SUFFIX  = "_fib_explore"

# ── Grille d'optimisation ────────────────────────────────────────────────────
PARAM_GRID = {
    "session":          ["no_nuit", "all"],
    "direction_filter": ["both"],
    "min_imp":          [0.5, 1.0, 1.5, 2.0],
    "fib_level":        [0.382, 0.50],
    "sl_mult":          [0.75, 1.0, 1.25],
    "tp_mult":          [1.5, 2.0, 2.5],
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
    p = params or {}
    sl_mult          = p.get("sl_mult",          FIB_SL_ATR_MULT_PER_TICKER.get(ticker))
    tp_mult          = p.get("tp_mult",          FIB_TP_ATR_MULT_PER_TICKER.get(ticker))
    min_imp          = p.get("min_imp",          FIB_MIN_IMPULSE_ATR_PER_TICKER.get(ticker))
    fib_lvl          = p.get("fib_level",        FIB_LEVEL_PER_TICKER.get(ticker, 0.382))
    session          = p.get("session",          None)
    direction_filter = p.get("direction_filter", None)
    if direction_filter == "both":
        direction_filter = None

    return run_fib_backtest(
        df_15m, ticker,
        fib_level=fib_lvl,
        sl_mult=sl_mult,
        tp_mult=tp_mult,
        min_imp=min_imp,
        apply_filter=True,
        session=session,
        direction_filter=direction_filter,
    )
