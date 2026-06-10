"""
État live de la stratégie bos-fvg-v1 (ICT : Break of Structure + Fair Value Gap).

Mirror live de `strategies/bos_fvg.py::run_backtest` (M5) avec FIDÉLITÉ ABSOLUE :
la machine d'états pending/position est rejouée à l'identique sur les barres M5
CLÔTURÉES fournies par le caller, puis arrêtée à l'état COURANT (PENDING / ACTIVE
/ CANCEL) au lieu de finaliser des trades. Pendant live de
`core/fib_fine_v2.py::get_fib_fine_live_signal`, adapté au setup BOS+FVG.

Pattern fib-fine-v2 (PAS de M1Buffer) :
  • TF M5 : barres M5 CLÔTURÉES fournies par le caller (live_runner._fetch_bars_m5,
    REST dédié). Aucune dépendance M1Buffer.
  • Entrée LIMIT @ CE (Consequent Encroachment du FVG, en discount/premium) =
    fill HONNÊTE : toutes les features (BOS, impulsion, FVG, CE, discount) sont
    calculées sur des barres CLÔTURÉES strictement avant l'armement de l'ordre.
  • Pivots révélés à j-w (causal). Fill intra-bar SL-prioritaire.
  • Session DST-aware NY (zoneinfo America/New_York), comme le backtest.
  • Sizing dédié BOS_FVG_RISK_USD ($150). Params per-ticker (rr, sl_buf).

LIVE-ÉQUIVALENCE : par construction (audit @auditor 2026-06-03 : look-ahead absent).

⚠️ HELPERS GELÉS : _round_tick / _atr / _pivots / _find_fvg sont copiés VERBATIM
depuis la version 🟢 de strategies/bos_fvg.py. NE PAS diverger sans nouvelle
promotion (la prod ne doit JAMAIS suivre une édition recherche en silence).
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from config import (
    BOS_FVG_ATR_PERIOD,
    BOS_FVG_ENABLED,
    BOS_FVG_LIVE_TICKERS,
    BOS_FVG_LOOKBACK_BARS,
    BOS_FVG_MAX_TRADES_PER_DAY,
    BOS_FVG_MIN_IMPULSE_ATR,
    BOS_FVG_ORDER_TIMEOUT_BARS,
    BOS_FVG_PIVOT_W,
    BOS_FVG_RISK_USD,
    BOS_FVG_RR,
    BOS_FVG_RR_PER_TICKER,
    BOS_FVG_SESSION_END,
    BOS_FVG_SESSION_OPEN,
    BOS_FVG_SL_BUF,
    BOS_FVG_SL_BUF_PER_TICKER,
    BOS_FVG_TIMEZONE,
    INSTRUMENTS,
    RISK_PER_TRADE_USD,
)

_NY_TZ = ZoneInfo(BOS_FVG_TIMEZONE)
np.random.seed(42)


# ═════════════════════════════════════════════════════════════════════════════
# Helpers GELÉS — verbatim de strategies/bos_fvg.py (version 🟢). Ne pas diverger.
# ═════════════════════════════════════════════════════════════════════════════


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


# ═════════════════════════════════════════════════════════════════════════════
# Signal live
# ═════════════════════════════════════════════════════════════════════════════


def get_bos_fvg_live_signal(df_m5: pd.DataFrame, ticker: str) -> dict | None:
    """État live courant de bos-fvg-v1 sur barres M5 CLÔTURÉES.

    df_m5  : DataFrame OHLCV M5, dernière ligne = dernière M5 CLÔTURÉE.
    ticker : "NQ1" ou "MES1" (univers live, cf. BOS_FVG_LIVE_TICKERS).

    Retourne None si rien d'actif, sinon :
      {"state": "PENDING"|"ACTIVE"|"CANCEL", "signal": {...}, "impulse_key": str,
       "reason": str (si CANCEL)}
    "CANCEL" → annuler l'ordre limite broker-side (invalidation FVG sur M5 closes).
    Le signal contient direction/entry/sl/tp/sl_dist/tp_dist/rr/n_ct/risk/strategy.
    """
    if not BOS_FVG_ENABLED or ticker not in BOS_FVG_LIVE_TICKERS:
        return None

    rr = float(BOS_FVG_RR_PER_TICKER.get(ticker, BOS_FVG_RR))
    sl_buf = float(BOS_FVG_SL_BUF_PER_TICKER.get(ticker, BOS_FVG_SL_BUF))
    risk_usd = float(BOS_FVG_RISK_USD or RISK_PER_TRADE_USD)
    w = BOS_FVG_PIVOT_W
    lookback = BOS_FVG_LOOKBACK_BARS

    instr = INSTRUMENTS[ticker]
    tick = instr["tick_size"]
    dpp = instr["dollar_per_point"]

    if df_m5 is None or df_m5.empty:
        return None
    df = df_m5.copy()
    df["atr"] = _atr(df, BOS_FVG_ATR_PERIOD)

    # Session NY DST-aware (index UTC naïf → conversion NY), identique au backtest.
    idx_ny = df.index.tz_localize("UTC").tz_convert(_NY_TZ)
    o_h, o_m = BOS_FVG_SESSION_OPEN
    e_h, e_m = BOS_FVG_SESSION_END
    mins = (idx_ny.hour * 60 + idx_ny.minute).values
    in_rth = (mins >= o_h * 60 + o_m) & (mins < e_h * 60 + e_m)
    sess_end_min = e_h * 60 + e_m
    day_str = idx_ny.strftime("%Y-%m-%d").values

    high = df["high"].values
    low = df["low"].values
    close = df["close"].values
    atr = df["atr"].values
    ts_arr = df.index
    ph, pl = _pivots(high, low, w)

    n = len(df)
    pending = None
    position = None
    last_sh_i = last_sl_i = None
    sh_broken = sl_broken = False
    day_trades: dict[str, int] = {}

    for j in range(w + 2, n):
        d = day_str[j]
        ci = j - w
        if ci >= 0:
            if ph[ci]:
                last_sh_i = ci
                sh_broken = False
            if pl[ci]:
                last_sl_i = ci
                sl_broken = False

        # ── Position ouverte → libération sur SL/TP/TE (broker tient le P&L réel)
        if position is not None:
            dirn = position["direction"]
            sl, tp = position["sl"], position["tp"]
            if dirn == "long":
                hsl, htp = (low[j] <= sl), (high[j] >= tp)
            else:
                hsl, htp = (high[j] >= sl), (low[j] <= tp)
            if (hsl and htp) or hsl or htp or mins[j] >= sess_end_min or d != position["fill_day"]:
                position = None
            continue

        # ── Pending limit ───────────────────────────────────────────────────
        if pending is not None:
            if (
                pending["direction"] == "long"
                and close[j] < pending["fvg_bot"]
                or pending["direction"] == "short"
                and close[j] > pending["fvg_top"]
                or mins[j] >= sess_end_min
                or d != pending["arm_day"]
                or (j - pending["arm_idx"]) > BOS_FVG_ORDER_TIMEOUT_BARS
            ):
                pending = None
            else:
                lvl = pending["entry"]
                hit = (low[j] <= lvl) if pending["direction"] == "long" else (high[j] >= lvl)
                if hit:
                    position = {
                        **pending,
                        "fill_day": d,
                        "fill_idx": j,
                        "fill_time": str(ts_arr[j]),
                    }
                    pending = None
                    dirn = position["direction"]
                    sl, tp = position["sl"], position["tp"]
                    if dirn == "long":
                        hsl, htp = (low[j] <= sl), (high[j] >= tp)
                    else:
                        hsl, htp = (high[j] >= sl), (low[j] <= tp)
                    if (hsl and htp) or hsl or htp:
                        position = None
                    continue
            if pending is not None or position is not None:
                continue

        # ── Recherche d'un nouveau setup (RTH only) ─────────────────────────
        if not bool(in_rth[j]) or mins[j] >= sess_end_min:
            continue
        if day_trades.get(d, 0) >= BOS_FVG_MAX_TRADES_PER_DAY:
            continue
        if last_sh_i is None or last_sl_i is None or np.isnan(atr[j]) or atr[j] <= 0:
            continue

        setup = None
        if (not sh_broken) and close[j] > high[last_sh_i] and last_sl_i < j:
            origin = last_sl_i
            imp_low = low[origin]
            imp_high = high[origin : j + 1].max()
            rng = imp_high - imp_low
            if rng >= BOS_FVG_MIN_IMPULSE_ATR * atr[j] and (j - origin) <= lookback:
                eq = imp_low + 0.5 * rng
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
            "strategy": "BOS_FVG",
            "ticker": ticker,
            "direction": dirn,
            "entry": float(entry),
            "sl": float(sl),
            "tp": float(tp),
            "sl_dist": float(sl_dist),
            "tp_dist": float(tp_dist),
            "rr": round(tp_dist / sl_dist, 2),
            "n_ct": int(n_ct),
            "risk": float(n_ct * sl_dist * dpp),
            "fvg_top": float(fvg_top),
            "fvg_bot": float(fvg_bot),
            "origin_idx": int(origin),
            "arm_idx": j,
            "arm_day": d,
        }

    # ── État final ──────────────────────────────────────────────────────────
    if position is not None:
        # Clé d'identité STABLE par les prix (entry CE + sl), inchangée d'un cycle
        # à l'autre quand la fenêtre M5 glisse — calquée sur fib_fine_v2
        # (direction_swing_low_swing_high). Évite l'empilement d'ordres LIMIT
        # dupliqués (les indices origin/arm dérivaient à chaque re-fetch → tags
        # distincts → dédup `_already_placed` ratée + CANCEL FVG no-op).
        key = f"{position['direction']}_{position['entry']:.2f}_{position['sl']:.2f}"
        return {"state": "ACTIVE", "signal": position, "impulse_key": key}

    if pending is None:
        return None

    # Clé d'identité STABLE par les prix (cf. état ACTIVE ci-dessus) : un pending
    # qui persiste sur plusieurs cycles garde le MÊME tag → un seul ordre placé,
    # et le CANCEL sur invalidation FVG cible bien l'ordre d'origine.
    key = f"{pending['direction']}_{pending['entry']:.2f}_{pending['sl']:.2f}"
    last_close = float(close[n - 1])
    if pending["direction"] == "long" and last_close < pending["fvg_bot"]:
        return {
            "state": "CANCEL",
            "signal": pending,
            "impulse_key": key,
            "reason": f"FVG invalidé (close={last_close:.4f} < fvg_bot={pending['fvg_bot']:.4f})",
        }
    if pending["direction"] == "short" and last_close > pending["fvg_top"]:
        return {
            "state": "CANCEL",
            "signal": pending,
            "impulse_key": key,
            "reason": f"FVG invalidé (close={last_close:.4f} > fvg_top={pending['fvg_top']:.4f})",
        }

    return {"state": "PENDING", "signal": pending, "impulse_key": key}
