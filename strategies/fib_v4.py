"""
Stratégie Fibonacci v4 — wrapper backtest/recherche.

Interface plug-and-play :
    run_backtest(df_15m, ticker, tf=None, params=None, topstep_guard=True)

Production : core/strategy_fib_v4.py
Ce module est le wrapper backtest/recherche uniquement.
"""

import pandas as pd

from config import (
    FIB_MAX_HOLD_BARS,
    FIB_MIN_IMPULSE_ATR_PER_TICKER,
    FIB_SL_ATR_MULT_PER_TICKER,
    FIB_TP_ATR_MULT_PER_TICKER,
    FIB_V4_LEVEL_PER_TICKER,
    FIB_V4_PIVOT_BREAK_BUFFER_ATR_PER_TICKER,
    FIB_V4_STRATEGY_VERSION,
    FIB_V4_TICKERS,
    FIB_V4_WICK_THROUGH_MAX_ATR_PER_TICKER,
)
from core.strategy_fib_v4 import run_fib_v4_backtest

# ── Identité de la stratégie ──────────────────────────────────────────────
STRATEGY_ID = FIB_V4_STRATEGY_VERSION  # "fib-v4"
TICKERS = FIB_V4_TICKERS  # ["MES1", "NQ1", "MGC1"]
CSV_SUFFIX = "_fib_v4"

# ── Contrat moteur M1 (core/bt_engine) ────────────────────────────────────
SIGNAL_TF = "15min"  # signaux M15 (reconstruits depuis le M1)
INTRADAY_DAYCLOSE = False  # fib-v4 ne ferme pas au changement de jour
SESSION_END_MIN = 24 * 60  # pas de clôture forcée (sorties = SL/TP + max_hold)
MAX_HOLD_MIN = FIB_MAX_HOLD_BARS * 15  # 32 barres M15 = 480 min
RUN_MIN_WINDOW = None  # session UTC large → simuler toute la journée
_TF_DELTA_FV4 = pd.Timedelta(minutes=15)

# Grille d'optimisation (utilisable via optimize.py — recherche uniquement)
PARAM_GRID = {
    "sl_mult": [0.50, 0.75, 1.00, 1.50, 2.00],
    "tp_mult": [1.00, 1.50, 2.00, 2.50, 3.00],
    "wick_max_atr": [0.05, 0.10, 0.20, 0.40, 0.80],
    "pivot_break_buffer_atr": [0.00, 0.10, 0.20],
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


def _regime(adx_arm) -> str:
    if adx_arm is None:
        return "neutral"
    try:
        a = float(adx_arm)
    except (TypeError, ValueError):
        return "neutral"
    if a >= 25:
        return "trending"
    if a < 20:
        return "ranging"
    return "neutral"


def emit_signals(sig_df: pd.DataFrame, ticker: str, params: dict | None = None) -> list[dict]:
    """Émet les ordres fib-v4 pour le moteur M1 (core/bt_engine).

    Réutilise la logique VALIDÉE `run_fib_v4_backtest` (M15 : impulse + filtre causal au fill
    `FIB_V4_FILL_FILTER` + invalidation pivot-break + timeout, tous sur barres CLOSES, no leak)
    → on extrait les ordres RÉELLEMENT remplis, puis core/bt_engine résout le fill + SL/TP à
    la minute (corrige le same-bar). `sig_df` = M15 reconstruit depuis le M1 par le moteur.
    """
    trades = run_backtest(sig_df, ticker, params=params, topstep_guard=False)
    if trades is None or len(trades) == 0:
        return []
    arms: list[dict] = []
    for t in trades.to_dict("records"):
        if t.get("result") == "NOT_FILLED" or not t.get("fill_time"):
            continue
        fill_ts = pd.Timestamp(t["fill_time"])
        dirn = t.get("dir") or t.get("direction")
        entry, sl, tp = float(t["entry"]), float(t["sl"]), float(t["tp"])
        arms.append(
            {
                "dir": dirn,
                "entry": entry,
                "sl": sl,
                "tp": tp,
                "sl_dist": float(t.get("sl_dist") or abs(entry - sl)),
                "tp_dist": float(t.get("tp_dist") or abs(entry - tp)),
                "rr": float(t.get("rr") or 0.0),
                "n_ct": int(t["n_ct"]),
                "regime": _regime(t.get("adx_at_arm")),
                "arm_ts": fill_ts,
                "place_ts": fill_ts - _TF_DELTA_FV4,  # ordre vif dès la barre M15 AVANT le fill
                "timeout_ts": fill_ts + _TF_DELTA_FV4,  # doit filler dans la barre de fill
                "extras": {
                    "pivot_break_atr": t.get("pivot_break_atr"),
                    "wick_through_atr": t.get("wick_through_atr"),
                },
            }
        )
    return arms
