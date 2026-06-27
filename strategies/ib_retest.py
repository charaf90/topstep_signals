"""
Stratégie ib-retest-v3 (Initial Balance break franche + retest) — PORT NATIF M1.

Backtest = `core/bt_engine.py` (backtesting.py sur barres M1) : les SIGNAUX sont émis ici
sur M15 (reconstruit depuis le M1, features à la clôture de barre → no leak), les fills +
SL/TP sont résolus à la minute par backtesting.py (corrige le fill same-bar du moteur maison).

Production live : core/ib_retest.py::get_ib_retest_live_signal (machine d'états temps réel) —
NON modifié. Ce module est le **wrapper backtest/recherche** équivalent. Paramètres lus de
`config.IB_RETEST_*` (fidélité prod par défaut). `pnl` = P&L NET (slippage + commission).

Archétype gagnant du système : cassure FRANCHE ≥ BREAKOUT_BUF_ATR×ATR de l'IB [09:30,10:30[ NY,
**entrée LIMITE au retest** de la borne cassée, SL 0.5×ATR, rr 1.5, invalidation timeout/EOD.
"""

from __future__ import annotations

import sys
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from config import (
    IB_RETEST_ATR_PERIOD,
    IB_RETEST_BREAKOUT_BUF_ATR,
    IB_RETEST_IB_END,
    IB_RETEST_IB_START,
    IB_RETEST_MAX_TRADES_PER_DAY,
    IB_RETEST_ORDER_TIMEOUT_BARS,
    IB_RETEST_RISK_PER_TRADE_USD,
    IB_RETEST_RR,
    IB_RETEST_SESSION_END,
    IB_RETEST_SL_ATR,
    IB_RETEST_STRATEGY_VERSION,
    IB_RETEST_TICKERS,
    IB_RETEST_TIMEZONE,
    INSTRUMENTS,
    RISK_PER_TRADE_USD,
)
from core import bt_engine
from core.fib_helpers import compute_adx

np.random.seed(42)

# ── Identité + contrat moteur M1 ──────────────────────────────────────────────
STRATEGY_ID = IB_RETEST_STRATEGY_VERSION  # "ib-retest-v3"
TICKERS = list(IB_RETEST_TICKERS)  # ["MES1", "NQ1"]
SIGNAL_TF = "15min"
CSV_SUFFIX = "_ibr"
CSV_TIMEFRAME = "m15"

_NY = ZoneInfo(IB_RETEST_TIMEZONE)
_IB_START_MIN = IB_RETEST_IB_START[0] * 60 + IB_RETEST_IB_START[1]
_IB_END_MIN = IB_RETEST_IB_END[0] * 60 + IB_RETEST_IB_END[1]
SESSION_END_MIN = IB_RETEST_SESSION_END[0] * 60 + IB_RETEST_SESSION_END[1]  # 960 = 16:00
# Fenêtre intraday simulée (perf) : de l'ouverture IB à la clôture de session.
RUN_MIN_WINDOW = (_IB_START_MIN, SESSION_END_MIN + 1)

# Grille d'optimisation (recherche uniquement). Défauts d'emit_signals = params PROD config.
PARAM_GRID = {
    "break_buf_atr": [0.1, 0.3, 0.5],
    "sl_atr": [0.5, 1.0],
    "rr": [1.5, 2.0, 3.0],
}


# ── Helpers GELÉS — verbatim de core/ib_retest.py. Ne pas diverger. ──────────────
def _round_tick(p, tick):
    return round(round(p / tick) * tick, 10)


def _atr(df, period):
    h, l, pc = df["high"], df["low"], df["close"].shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=period).mean()


def _regime(adx_val: float) -> str:
    if adx_val is None or np.isnan(adx_val):
        return "neutral"
    if adx_val >= 25:
        return "trending"
    if adx_val < 20:
        return "ranging"
    return "neutral"


def emit_signals(sig_df: pd.DataFrame, ticker: str, params: dict | None = None) -> list[dict]:
    """Émet les ordres ib-retest sur M15 (1 cassure franche/jour, entrée LIMITE au retest).

    Le fill et la résolution SL/TP sont délégués au M1 (core/bt_engine). On ne fait ICI
    aucune résolution de fill (≠ ancien run_backtest M15).
    """
    p = params or {}
    break_buf_atr = float(p.get("break_buf_atr", IB_RETEST_BREAKOUT_BUF_ATR))
    sl_atr = float(p.get("sl_atr", IB_RETEST_SL_ATR))
    rr = float(p.get("rr", IB_RETEST_RR))
    risk_usd = float(p.get("risk_usd", IB_RETEST_RISK_PER_TRADE_USD or RISK_PER_TRADE_USD))

    if sig_df is None or len(sig_df) == 0:
        return []
    df = sig_df
    atr = _atr(df, IB_RETEST_ATR_PERIOD).to_numpy()
    adx = compute_adx(df, 14).to_numpy()

    instr = INSTRUMENTS[ticker]
    tick, dpp = instr["tick_size"], instr["dollar_per_point"]

    idx_ny = df.index.tz_localize("UTC").tz_convert(_NY)
    mins = (idx_ny.hour * 60 + idx_ny.minute).to_numpy()
    nyd = idx_ny.strftime("%Y-%m-%d").to_numpy()
    ts_arr = df.index

    # Initial Balance (1ère heure RTH) par jour — causal (connu à 10:30).
    is_ib = (mins >= _IB_START_MIN) & (mins < _IB_END_MIN)
    ibdf = pd.DataFrame({"d": nyd, "h": df["high"].to_numpy(), "l": df["low"].to_numpy()})[is_ib]
    ibh_map = ibdf.groupby("d")["h"].max().to_dict()
    ibl_map = ibdf.groupby("d")["l"].min().to_dict()
    ibh = np.array([ibh_map.get(d, np.nan) for d in nyd])
    ibl = np.array([ibl_map.get(d, np.nan) for d in nyd])

    in_win = (mins >= _IB_END_MIN) & (mins < SESSION_END_MIN)
    close = df["close"].to_numpy()
    timeout = pd.Timedelta(minutes=IB_RETEST_ORDER_TIMEOUT_BARS * 15)

    arms: list[dict] = []
    cur_day, broke, n_day = None, None, 0
    for i in range(IB_RETEST_ATR_PERIOD + 2, len(df)):
        d = nyd[i]
        if d != cur_day:
            cur_day, broke, n_day = d, None, 0
        if not in_win[i] or broke is not None or n_day >= IB_RETEST_MAX_TRADES_PER_DAY:
            continue
        H, L, a = ibh[i], ibl[i], atr[i]
        if np.isnan(H) or np.isnan(L) or H <= L or np.isnan(a) or a <= 0:
            continue
        if close[i] > H + break_buf_atr * a:
            dirn, level = "long", H
            sl = _round_tick(level - sl_atr * a, tick)
        elif close[i] < L - break_buf_atr * a:
            dirn, level = "short", L
            sl = _round_tick(level + sl_atr * a, tick)
        else:
            continue
        broke = dirn
        entry = _round_tick(level, tick)
        sl_dist = abs(entry - sl)
        if dirn == "long":
            tp = _round_tick(entry + rr * sl_dist, tick)
            if not (sl < entry < tp):
                continue
        else:
            tp = _round_tick(entry - rr * sl_dist, tick)
            if not (tp < entry < sl):
                continue
        tp_dist = abs(entry - tp)
        if sl_dist <= 0 or tp_dist <= 0:
            continue
        n_ct = int(risk_usd / (sl_dist * dpp))
        if n_ct <= 0:
            continue
        n_day += 1
        arms.append(
            {
                "dir": dirn,
                "entry": float(entry),
                "sl": float(sl),
                "tp": float(tp),
                "sl_dist": float(sl_dist),
                "tp_dist": float(tp_dist),
                "rr": round(tp_dist / sl_dist, 2),
                "n_ct": int(n_ct),
                "arm_ts": ts_arr[i],
                "timeout_ts": ts_arr[i] + timeout,
                "regime": _regime(adx[i]),
                "extras": {"ib_range_atr": round(float((H - L) / a), 2)},
            }
        )
    return arms


def run_backtest(
    df: pd.DataFrame | None = None,
    ticker: str = "NQ1",
    tf=None,
    params: dict | None = None,
    topstep_guard: bool = True,
) -> pd.DataFrame:
    """Adaptateur de compat : route vers le moteur M1 (core/bt_engine).

    Le `df` (M15) passé par optimize.py / les outils n'est utilisé que pour BORNER la fenêtre
    (sa plage de dates = IS ou OOS) ; les barres réelles du backtest sont le M1. Retourne le
    schéma canonique de trades. Pour la viz HTML, appeler directement `core.bt_engine.run`.
    """
    start = end = None
    if df is not None and len(df) > 0:
        start = pd.Timestamp(df.index.min()).strftime("%Y-%m-%d")
        end = pd.Timestamp(df.index.max()).strftime("%Y-%m-%d")
    return bt_engine.run(
        sys.modules[__name__], ticker, params=params, start=start, end=end, html=False
    )["trades"]
