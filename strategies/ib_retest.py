"""
Stratégie ib-retest-v3 (Initial Balance break franche + retest) — wrapper backtest DURABLE.

Production live : core/ib_retest.py::get_ib_retest_live_signal (machine d'états temps réel).
Ce module est le **wrapper backtest/recherche** équivalent — la même logique rejouée sur
tout l'historique pour produire des trades finalisés (≠ état courant).

Pourquoi ce fichier existe : ib-retest-v3 a été PROMUE en live (2026-06-14) mais n'avait
qu'un backtest en `brouillon/strategies/ib_retest_v1.py` (jetable, gitignoré) AUX DÉFAUTS
0.3/1.0/2.0 ≠ prod 0.5/0.5/1.5. Ce wrapper durable lit ses paramètres de `config.IB_RETEST_*`
→ fidélité prod par défaut + survit à un `clear_brouillon.sh`.

FIDÉLITÉ : machine d'états identique à core/ib_retest.py (cassure FRANCHE ≥ BREAKOUT_BUF_ATR×ATR
de l'IB [09:30,10:30[ NY, retest LIMITE intra-bar, fill SL-prioritaire, invalidation EOD/timeout
SEULE — jamais close-based). Helpers _round_tick/_atr GELÉS verbatim (cf. core/ib_retest.py).
`pnl` = P&L NET (slippage entrée+sortie + commission round-trip).
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from config import (
    COMMISSION_RT_PER_CONTRACT,
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
    SLIPPAGE_TICKS_PER_TICKER,
)
from core.fib_helpers import compute_adx

np.random.seed(42)

# ── Identité de la stratégie ──────────────────────────────────────────────────
STRATEGY_ID = IB_RETEST_STRATEGY_VERSION  # "ib-retest-v3"
TICKERS = list(IB_RETEST_TICKERS)  # ["MES1", "NQ1"]
CSV_SUFFIX = "_ibr"
CSV_TIMEFRAME = "m15"

_NY = ZoneInfo(IB_RETEST_TIMEZONE)

# Grille d'optimisation (recherche uniquement, via optimize.py). Les défauts de
# run_backtest sont les params PROD lus de config — la grille ne sert qu'à re-explorer.
PARAM_GRID = {
    "break_buf_atr": [0.1, 0.3, 0.5],
    "sl_atr": [0.5, 1.0],
    "rr": [1.5, 2.0, 3.0],
}

_TRADE_COLS = [
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
    "pnl_gross",
    "ib_range_atr",
    "bars_to_fill",
    "bars_held",
    "adx",
    "atr_pct",
    "timeframe",
]


# ── Helpers GELÉS — verbatim de core/ib_retest.py. Ne pas diverger. ──────────────
def _round_tick(p, tick):
    return round(round(p / tick) * tick, 10)


def _atr(df, period):
    h, l, pc = df["high"], df["low"], df["close"].shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=period).mean()


def run_backtest(
    df: pd.DataFrame | None = None,
    ticker: str = "NQ1",
    tf=None,
    params: dict | None = None,
    topstep_guard: bool = True,
) -> pd.DataFrame:
    """Backtest ib-retest-v3 sur tout l'historique du df M15.

    params override (sinon défauts PROD de config.IB_RETEST_*) :
        break_buf_atr, sl_atr, rr, max_hold_bars, risk_usd
    """
    p = params or {}
    break_buf_atr = float(p.get("break_buf_atr", IB_RETEST_BREAKOUT_BUF_ATR))
    sl_atr = float(p.get("sl_atr", IB_RETEST_SL_ATR))
    rr = float(p.get("rr", IB_RETEST_RR))
    max_hold_bars = p.get("max_hold_bars")
    risk_usd = float(p.get("risk_usd", IB_RETEST_RISK_PER_TRADE_USD or RISK_PER_TRADE_USD))

    atr_period = IB_RETEST_ATR_PERIOD
    open_min = IB_RETEST_IB_START[0] * 60 + IB_RETEST_IB_START[1]
    ib_end_min = IB_RETEST_IB_END[0] * 60 + IB_RETEST_IB_END[1]
    sess_end_min = IB_RETEST_SESSION_END[0] * 60 + IB_RETEST_SESSION_END[1]
    timeout_bars = IB_RETEST_ORDER_TIMEOUT_BARS
    max_trades_day = IB_RETEST_MAX_TRADES_PER_DAY

    if df is None or len(df) == 0:
        return pd.DataFrame(columns=_TRADE_COLS)
    df = df.copy()
    df["atr"] = _atr(df, atr_period)
    df["adx"] = compute_adx(df, 14)
    df["atr_pct"] = df["atr"].rolling(window=4 * 24 * 20, min_periods=200).rank(pct=True)
    adx_arr = df["adx"].values
    atr_pct_arr = df["atr_pct"].values

    instr = INSTRUMENTS[ticker]
    tick = instr["tick_size"]
    dpp = instr["dollar_per_point"]
    slip_px = SLIPPAGE_TICKS_PER_TICKER.get(ticker, 1) * tick

    idx_ny = df.index.tz_localize("UTC").tz_convert(_NY)
    mins = (idx_ny.hour * 60 + idx_ny.minute).values
    ny_date = idx_ny.strftime("%Y-%m-%d").values

    high = df["high"].values
    low = df["low"].values
    close = df["close"].values
    atr = df["atr"].values
    ts_arr = df.index

    # ── Initial Balance (1ère heure RTH) par date — causal (connu à 10h30) ───────
    is_ib = (mins >= open_min) & (mins < ib_end_min)
    ib_df = pd.DataFrame({"date": ny_date, "high": high, "low": low})[is_ib]
    ibh_map = ib_df.groupby("date")["high"].max().to_dict()
    ibl_map = ib_df.groupby("date")["low"].min().to_dict()
    ibh_arr = np.array([ibh_map.get(d, np.nan) for d in ny_date])
    ibl_arr = np.array([ibl_map.get(d, np.nan) for d in ny_date])

    in_trade_win = (mins >= ib_end_min) & (mins < sess_end_min)

    n = len(df)
    trades: list[dict] = []
    position = None
    pending = None
    cur_day = None
    broke = None
    day_trades: dict[str, int] = {}

    for i in range(atr_period + 2, n):
        d = ny_date[i]
        if d != cur_day:
            cur_day = d
            broke = None
            pending = None

        # ── Position ouverte ─────────────────────────────────────────────────────
        if position is not None:
            dirn = position["dir"]
            sl, tp = position["sl"], position["tp"]
            if dirn == "long":
                hsl, htp = (low[i] <= sl), (high[i] >= tp)
            else:
                hsl, htp = (high[i] >= sl), (low[i] <= tp)
            exitp, res = None, None
            if (hsl and htp) or hsl:
                exitp, res = sl, "SL"
            elif htp:
                exitp, res = tp, "TP"
            elif (
                mins[i] >= sess_end_min
                or d != position["fill_day"]
                or max_hold_bars is not None
                and (i - position["fill_idx"]) >= max_hold_bars
            ):
                exitp, res = close[i], "TE"
            if res is not None:
                _append(trades, position, exitp, res, ts_arr[i], i, dpp, slip_px)
                position = None
            continue

        # ── Pending limite (retest) ──────────────────────────────────────────────
        if pending is not None:
            if mins[i] >= sess_end_min or (i - pending["arm_idx"]) > timeout_bars:
                pending = None
            else:
                lvl = pending["entry"]
                hit = (low[i] <= lvl) if pending["dir"] == "long" else (high[i] >= lvl)
                if hit:
                    position = {
                        **pending,
                        "fill_day": d,
                        "fill_time": str(ts_arr[i]),
                        "fill_idx": i,
                        "bars_to_fill": int(i - pending["arm_idx"]),
                    }
                    pending = None
                    dirn = position["dir"]
                    sl, tp = position["sl"], position["tp"]
                    if dirn == "long":
                        hsl, htp = (low[i] <= sl), (high[i] >= tp)
                    else:
                        hsl, htp = (high[i] >= sl), (low[i] <= tp)
                    exitp, res = None, None
                    if (hsl and htp) or hsl:
                        exitp, res = sl, "SL"
                    elif htp:
                        exitp, res = tp, "TP"
                    if res is not None:
                        _append(trades, position, exitp, res, ts_arr[i], i, dpp, slip_px)
                        position = None
                    continue
            if pending is not None or position is not None:
                continue

        # ── Détection cassure franche de l'IB (post-IB, 1 setup/j) ───────────────
        if not in_trade_win[i]:
            continue
        if broke is not None or day_trades.get(d, 0) >= max_trades_day:
            continue
        ibh, ibl = ibh_arr[i], ibl_arr[i]
        a = atr[i]
        if np.isnan(ibh) or np.isnan(ibl) or ibh <= ibl or np.isnan(a) or a <= 0:
            continue
        ib_range = ibh - ibl

        if close[i] > ibh + break_buf_atr * a:
            dirn, level = "long", ibh
        elif close[i] < ibl - break_buf_atr * a:
            dirn, level = "short", ibl
        else:
            continue

        broke = dirn
        entry = _round_tick(level, tick)
        if dirn == "long":
            sl = _round_tick(entry - sl_atr * a, tick)
            sl_dist = entry - sl
            tp = _round_tick(entry + rr * sl_dist, tick)
            if not (sl < entry < tp):
                continue
        else:
            sl = _round_tick(entry + sl_atr * a, tick)
            sl_dist = sl - entry
            tp = _round_tick(entry - rr * sl_dist, tick)
            if not (tp < entry < sl):
                continue
        sl_dist = abs(entry - sl)
        tp_dist = abs(entry - tp)
        if sl_dist <= 0 or tp_dist <= 0:
            continue
        n_ct = int(risk_usd / (sl_dist * dpp))
        if n_ct <= 0:
            continue

        day_trades[d] = day_trades.get(d, 0) + 1
        pending = {
            "dir": dirn,
            "entry": float(entry),
            "sl": float(sl),
            "tp": float(tp),
            "sl_dist": float(sl_dist),
            "tp_dist": float(tp_dist),
            "rr": round(tp_dist / sl_dist, 2),
            "n_ct": int(n_ct),
            "arm_idx": i,
            "ib_range_atr": round(float(ib_range / a), 2),
            "adx_arm": float(adx_arr[i]) if not np.isnan(adx_arr[i]) else None,
            "atr_pct_arm": float(atr_pct_arr[i]) if not np.isnan(atr_pct_arr[i]) else None,
        }

    return pd.DataFrame(trades) if trades else pd.DataFrame(columns=_TRADE_COLS)


def _append(trades, pos, exitp, res, exit_ts, exit_idx, dpp, slip_px):
    dirn = pos["dir"]
    entry = pos["entry"]
    n_ct = pos["n_ct"]
    raw = (exitp - entry) if dirn == "long" else (entry - exitp)
    g = raw * n_ct * dpp
    net = g - 2 * slip_px * n_ct * dpp - COMMISSION_RT_PER_CONTRACT * n_ct
    adx_arm = pos.get("adx_arm")
    if adx_arm is None:
        regime = "neutral"
    elif adx_arm >= 25:
        regime = "trending"
    elif adx_arm < 20:
        regime = "ranging"
    else:
        regime = "neutral"
    trades.append(
        {
            "date": pd.Timestamp(exit_ts).strftime("%Y-%m-%d"),
            "dir": dirn,
            "entry": float(entry),
            "sl": float(pos["sl"]),
            "tp": float(pos["tp"]),
            "sl_dist": float(pos["sl_dist"]),
            "tp_dist": float(pos["tp_dist"]),
            "rr": float(pos["rr"]),
            "n_ct": int(n_ct),
            "result": res,
            "pnl": round(float(net), 2),
            "fill_time": pos.get("fill_time"),
            "exit_time": str(exit_ts),
            "exit": float(exitp),
            "regime": regime,
            "pnl_gross": round(float(g), 2),
            "ib_range_atr": pos.get("ib_range_atr"),
            "bars_to_fill": pos.get("bars_to_fill"),
            "bars_held": int(exit_idx - pos["fill_idx"]),
            "adx": round(adx_arm, 1) if adx_arm is not None else None,
            "atr_pct": pos.get("atr_pct_arm"),
            "timeframe": "m15",
        }
    )
