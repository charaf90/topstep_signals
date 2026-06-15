"""
État live de la stratégie ib-retest-v3 (Initial Balance break franche + retest).

Mirror live de `brouillon/strategies/ib_retest_v1.py::run_backtest` (M15) avec
FIDÉLITÉ ABSOLUE : la machine d'états pending/position est rejouée à l'identique
sur les barres M15 CLÔTURÉES fournies par le caller, puis arrêtée à l'état COURANT
(PENDING / ACTIVE / CANCEL) au lieu de finaliser des trades. Calqué sur le pattern
`core/bos_fvg.py::get_bos_fvg_live_signal`, adapté au setup IB break + retest.

Pattern (NATIF M15, PAS de M1Buffer) :
  • TF M15 : barres M15 CLÔTURÉES fournies par le caller (live_runner._fetch_bars,
    le MÊME df que OPR/fib-v4). Aucune dépendance M1Buffer / REST dédié.
  • IB = range de la 1ère heure RTH [09:30,10:30[ NY (IBH/IBL connus à 10:30, causal).
  • CASSURE FRANCHE : une bougie M15 CLÔTURE au-delà de la borne + BREAKOUT_BUF_ATR×ATR
    (après 10:30) → la franchise EST l'edge. 1ère cassure franche du jour seulement.
  • RETEST : ordre LIMIT @ la borne cassée (devient support/résistance). Fill HONNÊTE :
    le prix doit traverser le niveau → pas d'invalidation close-based possible.
  • INVALIDATION = EOD (16:00 NY) + timeout (ORDER_TIMEOUT_BARS) SEULE. PAS de
    close-based qui racerait le fill (bug bos-fvg à NE PAS reproduire : ici la
    machine d'états traite le fill AVANT le timeout, donc un pending qui a fillé
    devient ACTIVE, jamais CANCEL).
  • SL = entrée ∓ SL_ATR×ATR ; TP = entrée ± RR×sl_dist. Fill intra-bar SL-prioritaire.
  • Session DST-aware NY (zoneinfo America/New_York), comme le backtest.
  • Sizing IB_RETEST_RISK_PER_TRADE_USD (None → RISK_PER_TRADE_USD global, $200).

LIVE-ÉQUIVALENCE : par construction (entrée limit broker-native au niveau IB EXACT,
features calculées sur barres CLÔTURÉES strictement avant l'armement).

⚠️ HELPERS GELÉS : _round_tick / _atr sont copiés VERBATIM depuis la version 🟢 de
brouillon/strategies/ib_retest_v1.py. NE PAS diverger sans nouvelle promotion.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from config import (
    IB_RETEST_ATR_PERIOD,
    IB_RETEST_BREAKOUT_BUF_ATR,
    IB_RETEST_ENABLED,
    IB_RETEST_IB_END,
    IB_RETEST_IB_START,
    IB_RETEST_MAX_TRADES_PER_DAY,
    IB_RETEST_ORDER_TIMEOUT_BARS,
    IB_RETEST_RISK_PER_TRADE_USD,
    IB_RETEST_RR,
    IB_RETEST_SESSION_END,
    IB_RETEST_SL_ATR,
    IB_RETEST_TICKERS,
    IB_RETEST_TIMEZONE,
    INSTRUMENTS,
    RISK_PER_TRADE_USD,
)

_NY_TZ = ZoneInfo(IB_RETEST_TIMEZONE)
np.random.seed(42)


# ═════════════════════════════════════════════════════════════════════════════
# Helpers GELÉS — verbatim de brouillon/strategies/ib_retest_v1.py. Ne pas diverger.
# ═════════════════════════════════════════════════════════════════════════════


def _round_tick(p, tick):
    return round(round(p / tick) * tick, 10)


def _atr(df, period):
    h, l, pc = df["high"], df["low"], df["close"].shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=period).mean()


# ═════════════════════════════════════════════════════════════════════════════
# Signal live
# ═════════════════════════════════════════════════════════════════════════════


def get_ib_retest_live_signal(df_15m: pd.DataFrame, ticker: str) -> dict | None:
    """État live courant de ib-retest-v3 sur barres M15 CLÔTURÉES.

    df_15m : DataFrame OHLCV M15, dernière ligne = dernière M15 CLÔTURÉE.
    ticker : "MES1" ou "NQ1" (univers live, cf. IB_RETEST_TICKERS).

    Retourne None si rien d'actif, sinon :
      {"state": "PENDING"|"ACTIVE"|"CANCEL", "signal": {...}, "impulse_key": str,
       "reason": str (si CANCEL)}
    "CANCEL" → annuler l'ordre limite broker-side. Émis UNIQUEMENT sur timeout
    pending (ORDER_TIMEOUT_BARS) ou EOD (SESSION_END) — JAMAIS close-based, et
    JAMAIS quand la barre courante a fillé (le fill est traité avant le timeout
    dans la machine d'états → état ACTIVE, pas CANCEL).
    Le signal contient direction/entry/sl/tp/sl_dist/tp_dist/rr/n_ct/risk/strategy.
    """
    if not IB_RETEST_ENABLED or ticker not in IB_RETEST_TICKERS:
        return None

    rr = float(IB_RETEST_RR)
    sl_atr_mult = float(IB_RETEST_SL_ATR)
    break_buf_atr = float(IB_RETEST_BREAKOUT_BUF_ATR)
    risk_usd = float(IB_RETEST_RISK_PER_TRADE_USD or RISK_PER_TRADE_USD)

    instr = INSTRUMENTS[ticker]
    tick = instr["tick_size"]
    dpp = instr["dollar_per_point"]

    if df_15m is None or df_15m.empty:
        return None
    df = df_15m.copy()
    df["atr"] = _atr(df, IB_RETEST_ATR_PERIOD)

    # Session NY DST-aware (index UTC naïf → conversion NY), identique au backtest.
    idx_ny = df.index.tz_localize("UTC").tz_convert(_NY_TZ)
    open_min = IB_RETEST_IB_START[0] * 60 + IB_RETEST_IB_START[1]
    ib_end_min = IB_RETEST_IB_END[0] * 60 + IB_RETEST_IB_END[1]
    sess_end_min = IB_RETEST_SESSION_END[0] * 60 + IB_RETEST_SESSION_END[1]
    mins = (idx_ny.hour * 60 + idx_ny.minute).values
    ny_date = idx_ny.strftime("%Y-%m-%d").values

    high = df["high"].values
    low = df["low"].values
    close = df["close"].values
    atr = df["atr"].values
    ts_arr = df.index

    # ── Initial Balance (1ère heure RTH) par date — causal (connu à 10h30) ───
    is_ib = (mins >= open_min) & (mins < ib_end_min)
    ib_df = pd.DataFrame({"date": ny_date, "high": high, "low": low})[is_ib]
    ibh_map = ib_df.groupby("date")["high"].max().to_dict()
    ibl_map = ib_df.groupby("date")["low"].min().to_dict()
    ibh_arr = np.array([ibh_map.get(d, np.nan) for d in ny_date])
    ibl_arr = np.array([ibl_map.get(d, np.nan) for d in ny_date])

    # Fenêtre de trade = post-IB (10h30 → 16h00).
    in_trade_win = (mins >= ib_end_min) & (mins < sess_end_min)

    n = len(df)
    pending = None
    position = None
    cur_day = None
    broke = None
    day_trades: dict[str, int] = {}

    for i in range(IB_RETEST_ATR_PERIOD + 2, n):
        d = ny_date[i]
        if d != cur_day:
            cur_day = d
            broke = None
            pending = None

        # ── Position ouverte → libération sur SL/TP/TE (broker tient le P&L réel)
        if position is not None:
            dirn = position["direction"]
            sl, tp = position["sl"], position["tp"]
            if dirn == "long":
                hsl, htp = (low[i] <= sl), (high[i] >= tp)
            else:
                hsl, htp = (high[i] >= sl), (low[i] <= tp)
            if (hsl and htp) or hsl or htp or mins[i] >= sess_end_min or d != position["fill_day"]:
                position = None
            continue

        # ── Pending limite (retest) ──────────────────────────────────────────
        # Invalidation = EOD/timeout SEULE (PAS de close-based). Le fill est
        # traité AVANT le timeout → un pending qui touche le niveau devient
        # position (ACTIVE), jamais annulé.
        if pending is not None:
            if mins[i] >= sess_end_min or (i - pending["arm_idx"]) > IB_RETEST_ORDER_TIMEOUT_BARS:
                pending = None
            else:
                lvl = pending["entry"]
                hit = (low[i] <= lvl) if pending["direction"] == "long" else (high[i] >= lvl)
                if hit:
                    position = {
                        **pending,
                        "fill_day": d,
                        "fill_idx": i,
                        "fill_time": str(ts_arr[i]),
                    }
                    pending = None
                    dirn = position["direction"]
                    sl, tp = position["sl"], position["tp"]
                    if dirn == "long":
                        hsl, htp = (low[i] <= sl), (high[i] >= tp)
                    else:
                        hsl, htp = (high[i] >= sl), (low[i] <= tp)
                    if (hsl and htp) or hsl or htp:
                        position = None
                    continue
            if pending is not None or position is not None:
                continue

        # ── Détection cassure franche de l'IB (post-IB, 1 setup/j) ───────────
        if not bool(in_trade_win[i]):
            continue
        if broke is not None or day_trades.get(d, 0) >= IB_RETEST_MAX_TRADES_PER_DAY:
            continue
        ibh, ibl = ibh_arr[i], ibl_arr[i]
        a = atr[i]
        if np.isnan(ibh) or np.isnan(ibl) or ibh <= ibl or np.isnan(a) or a <= 0:
            continue

        if close[i] > ibh + break_buf_atr * a:
            dirn, level = "long", ibh
        elif close[i] < ibl - break_buf_atr * a:
            dirn, level = "short", ibl
        else:
            continue

        broke = dirn
        entry = _round_tick(level, tick)
        if dirn == "long":
            sl = _round_tick(entry - sl_atr_mult * a, tick)
            sl_dist = entry - sl
            tp = _round_tick(entry + rr * sl_dist, tick)
            if not (sl < entry < tp):
                continue
        else:
            sl = _round_tick(entry + sl_atr_mult * a, tick)
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
            "strategy": "IB_RETEST",
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
            "ib_high": float(ibh),
            "ib_low": float(ibl),
            "arm_idx": i,
            "arm_day": d,
        }

    # ── État final ───────────────────────────────────────────────────────────
    # Clé d'identité STABLE par les prix (calquée sur bos_fvg / fib_fine) : un
    # pending/position qui persiste sur plusieurs cycles garde le MÊME tag → un
    # seul ordre placé, et le CANCEL au timeout cible bien l'ordre d'origine.
    if position is not None:
        key = f"{position['direction']}_{position['entry']:.2f}_{position['sl']:.2f}"
        return {"state": "ACTIVE", "signal": position, "impulse_key": key}

    if pending is None:
        return None

    key = f"{pending['direction']}_{pending['entry']:.2f}_{pending['sl']:.2f}"
    # CANCEL si le pending a expiré (timeout) ou si on est en fin de session sur
    # la DERNIÈRE barre close — annulation broker-side de l'ordre non fillé.
    # NB : ce n'est PAS close-based (pas de comparaison du close au niveau) : on
    # ne déclenche QUE sur timeout/EOD, et la machine ci-dessus a déjà converti
    # tout fill en position → aucun risque de racer un fill.
    last_i = n - 1
    expired = (last_i - pending["arm_idx"]) > IB_RETEST_ORDER_TIMEOUT_BARS
    eod = mins[last_i] >= sess_end_min
    if expired or eod:
        reason = "timeout pending (retest non fillé)" if expired else "EOD (close RTH)"
        return {"state": "CANCEL", "signal": pending, "impulse_key": key, "reason": reason}

    return {"state": "PENDING", "signal": pending, "impulse_key": key}
