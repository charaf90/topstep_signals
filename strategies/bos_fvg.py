"""
Stratégie BOS_FVG (`bos-fvg-v1`) — ICT : Break of Structure + Fair Value Gap.

Setup M5 (idée utilisateur), bullish (bearish symétrique) :
  1. BOS : un bar CLÔTURE au-dessus du dernier swing high confirmé (cassure de
     structure haussière).
  2. IMPULSION : du swing low origine (dernier swing low confirmé avant le BOS)
     jusqu'au plus haut atteint. Requiert range ≥ MIN_IMPULSE_ATR × ATR(M5).
  3. FVG : imbalance 3 bougies dans l'impulsion (bullish : low[k] > high[k-2]).
     Zone = [high[k-2], low[k]] ; CE (Consequent Encroachment) = milieu.
  4. ENTRÉE : buy limit @ CE, à condition que CE soit en DISCOUNT (CE < 50% Fib
     de l'impulsion). Valide jusqu'à fill / invalidation / timeout / cloche.
  5. SL = swing low origine − SL_BUF × range. TP = entrée + RR × sl_dist.
  6. INVALIDATION : une bougie CLÔTURE sous le bas du FVG sans rebond → annulation.

Entrée LIMIT = fill HONNÊTE (pas de look-ahead stop-entry). Fill intra-bar
SL-prioritaire. M5 resamplé M1 (IS+OOS). Intraday RTH only, close-all à la cloche.
Auto-contenu, prod intouchée.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from config import (
    BOS_FVG_ATR_PERIOD,
    BOS_FVG_LOOKBACK_BARS,
    BOS_FVG_MAX_TRADES_PER_DAY,
    BOS_FVG_MIN_IMPULSE_ATR,
    BOS_FVG_ORDER_TIMEOUT_BARS,
    BOS_FVG_PIVOT_W,
    BOS_FVG_RISK_PER_TRADE_USD,
    BOS_FVG_RR,
    BOS_FVG_SESSION_END,
    BOS_FVG_SESSION_OPEN,
    BOS_FVG_SL_BUF,
    BOS_FVG_STRATEGY_VERSION,
    BOS_FVG_TICKERS,
    BOS_FVG_TIMEZONE,
    COMMISSION_RT_PER_CONTRACT,
    INSTRUMENTS,
    RISK_PER_TRADE_USD,
    SLIPPAGE_TICKS_PER_TICKER,
)
from core.fib_helpers import compute_adx  # helper pur LECTURE SEULE (cf. fib_fine)
from strategies._opr_core import load_tf

np.random.seed(42)

STRATEGY_ID = BOS_FVG_STRATEGY_VERSION  # "bos-fvg-v1"
TICKERS = list(BOS_FVG_TICKERS)
CSV_SUFFIX = "_bos_fvg"
CSV_TIMEFRAME = "m5"
_NY = ZoneInfo(BOS_FVG_TIMEZONE)

PARAM_GRID = {
    "rr": [1.5, 2.0, 3.0],
    "sl_buf": [0.0, 0.10, 0.25],
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
    "impulse_atr",
    "fvg_size",
    "bars_to_fill",
    "timeframe",
]


def _round_tick(p, tick):
    return round(round(p / tick) * tick, 10)


def _atr(df, period):
    h, l, pc = df["high"], df["low"], df["close"].shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=period).mean()


def _pivots(high, low, w):
    """Booléens swing high / swing low (max/min strict sur fenêtre [i-w, i+w])."""
    n = len(high)
    ph = np.zeros(n, dtype=bool)
    pl = np.zeros(n, dtype=bool)
    for i in range(w, n - w):
        hi = high[i]
        lo = low[i]
        if (
            hi == high[i - w : i + w + 1].max()
            and hi > high[i - w : i].max()
            and hi > high[i + 1 : i + w + 1].max()
        ):
            ph[i] = True
        if (
            lo == low[i - w : i + w + 1].min()
            and lo < low[i - w : i].min()
            and lo < low[i + 1 : i + w + 1].min()
        ):
            pl[i] = True
    return ph, pl


def run_backtest(df=None, ticker="NQ1", tf=None, params=None, topstep_guard=True) -> pd.DataFrame:
    p = params or {}
    rr = float(p.get("rr", BOS_FVG_RR))
    sl_buf = float(p.get("sl_buf", BOS_FVG_SL_BUF))
    risk_usd = float(p.get("risk_usd", BOS_FVG_RISK_PER_TRADE_USD) or RISK_PER_TRADE_USD)
    # Time-stop optionnel (recherche) : ferme la position (TE au close) après
    # max_hold_bars barres M5 depuis le fill, en plus de l'EOD. None = comportement
    # prod actuel (EOD seul). Live-équivalent : mirror du max_hold fib_fine
    # (broker/live_runner.py:1133 — elapsed_bars >= max_hold → close market).
    max_hold_bars = p.get("max_hold_bars")
    w = BOS_FVG_PIVOT_W
    lookback = BOS_FVG_LOOKBACK_BARS

    instr = INSTRUMENTS[ticker]
    tick = instr["tick_size"]
    dpp = instr["dollar_per_point"]
    slip_px = SLIPPAGE_TICKS_PER_TICKER.get(ticker, 1) * tick

    df = load_tf(ticker, "m5")
    if df.empty:
        return pd.DataFrame(columns=_TRADE_COLS)
    df = df.copy()
    df["atr"] = _atr(df, BOS_FVG_ATR_PERIOD)
    df["adx"] = compute_adx(df, 14)  # régime (causal) pour le stress par régime
    # Percentile ATR roulant (~20 jours M5) → régime de volatilité (vol_h/vol_b).
    df["atr_pct"] = df["atr"].rolling(window=12 * 23 * 20, min_periods=300).rank(pct=True)
    adx_arr = df["adx"].values
    atr_pct_arr = df["atr_pct"].values

    idx_ny = df.index.tz_localize("UTC").tz_convert(_NY)
    o_h, o_m = BOS_FVG_SESSION_OPEN
    e_h, e_m = BOS_FVG_SESSION_END
    mins = idx_ny.hour * 60 + idx_ny.minute
    in_rth = (mins >= o_h * 60 + o_m) & (mins < e_h * 60 + e_m)
    sess_end_min = e_h * 60 + e_m
    day_str = idx_ny.strftime("%Y-%m-%d").values

    high = df["high"].values
    low = df["low"].values
    close = df["close"].values
    opn = df["open"].values
    atr = df["atr"].values
    ts_arr = df.index  # UTC naïf
    ph, pl = _pivots(high, low, w)

    n = len(df)
    trades: list[dict] = []
    pending = None
    position = None
    last_sh_i = last_sl_i = None
    sh_broken = sl_broken = False
    day_trades: dict[str, int] = {}

    for j in range(w + 2, n):
        d = day_str[j]
        # Révéler les pivots confirmés (index <= j-w).
        ci = j - w
        if ci >= 0:
            if ph[ci]:
                last_sh_i = ci
                sh_broken = False
            if pl[ci]:
                last_sl_i = ci
                sl_broken = False

        # ── Position ouverte ────────────────────────────────────────────────
        if position is not None:
            dirn = position["dir"]
            sl, tp = position["sl"], position["tp"]
            if dirn == "long":
                hsl, htp = (low[j] <= sl), (high[j] >= tp)
            else:
                hsl, htp = (high[j] >= sl), (low[j] <= tp)
            exitp, res = None, None
            if (hsl and htp) or hsl:
                exitp, res = sl, "SL"
            elif htp:
                exitp, res = tp, "TP"
            elif mins[j] >= sess_end_min or d != position["fill_day"]:
                exitp, res = close[j], "TE"
            elif max_hold_bars is not None and (j - position["fill_idx"]) >= max_hold_bars:
                exitp, res = close[j], "TE"  # time-stop (recherche)
            if res is not None:
                _append(trades, position, exitp, res, ts_arr[j], j, dpp, slip_px)
                position = None
            continue

        # ── Pending limit ───────────────────────────────────────────────────
        if pending is not None:
            # Invalidation : clôture au-delà du FVG sans rebond.
            if (
                pending["dir"] == "long"
                and close[j] < pending["fvg_bot"]
                or pending["dir"] == "short"
                and close[j] > pending["fvg_top"]
                or mins[j] >= sess_end_min
                or d != pending["arm_day"]
                or (j - pending["arm_idx"]) > BOS_FVG_ORDER_TIMEOUT_BARS
            ):
                pending = None
            else:
                lvl = pending["entry"]
                hit = (low[j] <= lvl) if pending["dir"] == "long" else (high[j] >= lvl)
                if hit:
                    position = {
                        **pending,
                        "fill_day": d,
                        "fill_time": str(ts_arr[j]),
                        "fill_idx": j,
                        "bars_to_fill": int(j - pending["arm_idx"]),
                    }
                    pending = None
                    dirn = position["dir"]
                    sl, tp = position["sl"], position["tp"]
                    if dirn == "long":
                        hsl, htp = (low[j] <= sl), (high[j] >= tp)
                    else:
                        hsl, htp = (high[j] >= sl), (low[j] <= tp)
                    exitp, res = None, None
                    if (hsl and htp) or hsl:
                        exitp, res = sl, "SL"
                    elif htp:
                        exitp, res = tp, "TP"
                    if res is not None:
                        _append(trades, position, exitp, res, ts_arr[j], j, dpp, slip_px)
                        position = None
                    continue
            if pending is not None or position is not None:
                continue

        # ── Recherche d'un nouveau setup (RTH only) ─────────────────────────
        if not in_rth[j] or mins[j] >= sess_end_min:
            continue
        if day_trades.get(d, 0) >= BOS_FVG_MAX_TRADES_PER_DAY:
            continue
        if last_sh_i is None or last_sl_i is None or np.isnan(atr[j]) or atr[j] <= 0:
            continue

        setup = None
        # BOS haussier : close franchit le dernier swing high, swing low origine avant.
        if (not sh_broken) and close[j] > high[last_sh_i] and last_sl_i < j:
            origin = last_sl_i
            imp_low = low[origin]
            imp_high = high[origin : j + 1].max()
            rng = imp_high - imp_low
            if rng >= BOS_FVG_MIN_IMPULSE_ATR * atr[j] and (j - origin) <= lookback:
                eq = imp_low + 0.5 * rng  # 50% Fib (équilibre)
                fvg = _find_fvg(high, low, origin, j, "long", eq)
                if fvg is not None:
                    setup = ("long", origin, imp_low, imp_high, rng, fvg)
            sh_broken = True

        elif (not sl_broken) and close[j] < low[last_sl_i] and last_sh_i < j:
            origin = last_sh_i
            imp_high = high[origin]
            imp_low = low[origin : j + 1].min()
            rng = imp_high - imp_low
            if rng >= BOS_FVG_MIN_IMPULSE_ATR * atr[j] and (j - origin) <= lookback:
                eq = imp_high - 0.5 * rng
                fvg = _find_fvg(high, low, origin, j, "short", eq)
                if fvg is not None:
                    setup = ("short", origin, imp_low, imp_high, rng, fvg)
            sl_broken = True

        if setup is None:
            continue
        dirn, origin, imp_low, imp_high, rng, (ce, fvg_top, fvg_bot) = setup
        if dirn == "long":
            entry = _round_tick(ce, tick)
            sl = _round_tick(imp_low - sl_buf * rng, tick)
            sl_dist = entry - sl
            tp = _round_tick(entry + rr * sl_dist, tick)
        else:
            entry = _round_tick(ce, tick)
            sl = _round_tick(imp_high + sl_buf * rng, tick)
            sl_dist = sl - entry
            tp = _round_tick(entry - rr * sl_dist, tick)
        # Géométrie valide : SL < entrée < TP (long) / TP < entrée < SL (short).
        # Garde contre un FVG dégénéré (CE hors de l'ordre attendu) — sinon
        # abs() ci-dessous masquerait des distances de signe inversé.
        if dirn == "long" and not (sl < entry < tp):
            continue
        if dirn == "short" and not (tp < entry < sl):
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
            "fvg_top": float(fvg_top),
            "fvg_bot": float(fvg_bot),
            "impulse_atr": round(float(rng / atr[j]), 2),
            "fvg_size": float(fvg_top - fvg_bot),
            "arm_idx": j,
            "arm_day": d,
            "adx_arm": float(adx_arr[j]) if not np.isnan(adx_arr[j]) else None,
            "atr_pct_arm": float(atr_pct_arr[j]) if not np.isnan(atr_pct_arr[j]) else None,
        }

    return pd.DataFrame(trades) if trades else pd.DataFrame(columns=_TRADE_COLS)


def _find_fvg(high, low, origin, j, direction, eq):
    """FVG 3 bougies le plus RÉCENT dans [origin, j] dont le CE est en discount/
    premium. Retourne (ce, fvg_top, fvg_bot) ou None.
      bullish : low[k] > high[k-2]  → gap = [high[k-2], low[k]], CE < eq (discount)
      bearish : high[k] < low[k-2]  → gap = [high[k], low[k-2]], CE > eq (premium)
    """
    for k in range(j, origin + 1, -1):
        if k - 2 < 0:
            break
        if direction == "long":
            if low[k] > high[k - 2]:
                top, bot = low[k], high[k - 2]
                ce = (top + bot) / 2.0
                if ce < eq:
                    return ce, top, bot
        else:
            if high[k] < low[k - 2]:
                top, bot = low[k - 2], high[k]
                ce = (top + bot) / 2.0
                if ce > eq:
                    return ce, top, bot
    return None


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
            "fill_time": pos["fill_time"],
            "exit_time": str(exit_ts),
            "exit": float(exitp),
            "regime": regime,
            "pnl_gross": round(float(g), 2),
            "impulse_atr": pos.get("impulse_atr"),
            "fvg_size": pos.get("fvg_size"),
            "bars_to_fill": pos.get("bars_to_fill"),
            "adx": round(adx_arm, 1) if adx_arm is not None else None,
            "atr_pct": pos.get("atr_pct_arm"),
            "is_macro_day": None,
            "timeframe": "m5",
        }
    )
