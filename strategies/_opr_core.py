"""
Cœur PARTAGÉ de l'OPR de base (`opr-base-v1`) — module privé (préfixe `_` →
non découvert par le registry). Utilisé par strategies/opr_m5.py et
strategies/opr_m15.py.

OPR PUR, SANS AUCUN FILTRE (demande utilisateur — ne pas hériter d'opr-v4) :

  1. OPR = opening range [9h30, 9h45[ NY. opr_high = max(high) des bougies de la
     fenêtre, opr_low = min(low), opr_mid = (high+low)/2.
       - M15 → 1 bougie ; M5 → 3 bougies. Défini par la FENÊTRE temporelle, donc
         générique aux deux TF.
  2. CASSURE : bougie post-OPR qui ouvre DANS l'OPR et CLÔTURE DEHORS :
       LONG  : open < opr_high ET close > opr_high
       SHORT : open > opr_low  ET close < opr_low
  3. ENTRÉE : ordre LIMIT au niveau cassé (retest), fillé au retouch
     (low <= niveau <= high) → "rebond après cassure".
  4. SL = milieu OPR (sl_dist = sl_frac × range) ; TP = rr × sl_dist.
  5. Une position à la fois ; close-all à la cloche RTH ; ordre limite valide
     jusqu'à la cloche. AUCUN filtre, AUCUN cap de trades, AUCUN timeout.

Fill intra-bar conservateur SL-prioritaire (jamais TP en cas d'ambiguïté).
P&L NET (slippage entrée+sortie + commission round-trip).

Données : M5/M15 RESAMPLÉS depuis le M1 DATA_BACKTEST → IS+OOS propre, même
période pour les deux TF.
"""

from __future__ import annotations

import os
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from config import (
    COMMISSION_RT_PER_CONTRACT,
    INSTRUMENTS,
    OPR_BASE_OPR_END,
    OPR_BASE_RISK_PER_TRADE_USD,
    OPR_BASE_RR,
    OPR_BASE_SESSION_END,
    OPR_BASE_SESSION_OPEN,
    OPR_BASE_SL_FRAC,
    OPR_BASE_TIMEZONE,
    RISK_PER_TRADE_USD,
    SLIPPAGE_TICKS_PER_TICKER,
)

np.random.seed(42)

_NY_TZ = ZoneInfo(OPR_BASE_TIMEZONE)
_M1_DIR = "DATA_BACKTEST"
_TF_TO_PANDAS = {"m5": "5min", "m15": "15min"}

TRADE_COLS = [
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
    # audit (sans ATR ni filtre) — sert à lier les trades aux fenêtres M1
    "pnl_gross",
    "opr_high",
    "opr_low",
    "opr_range",
    "trigger_time",
    "bars_to_fill",
    "entry_hour_ny",
    "timeframe",
]


# ═════════════════════════════════════════════════════════════════════════════
# Données — resampling M1 → TF (IS+OOS, même série pour M5 et M15)
# ═════════════════════════════════════════════════════════════════════════════


def _load_m1(ticker: str) -> pd.DataFrame:
    path = os.path.join(_M1_DIR, f"{ticker}_data_m1.csv")
    if not os.path.exists(path):
        raise FileNotFoundError(f"M1 introuvable pour {ticker} : {path}")
    df = pd.read_csv(path, parse_dates=["datetime"])
    df = df.set_index("datetime").sort_index()
    df = df[~df.index.duplicated(keep="first")]
    return df[["open", "high", "low", "close", "volume"]]


def load_tf(ticker: str, timeframe: str) -> pd.DataFrame:
    """M5/M15 resamplé depuis le M1 (index UTC naïf). Resample en UTC : les
    bornes 9h30/9h45 NY tombent sur des frontières 5/15min quel que soit le DST
    (décalage entier d'heures), donc l'alignement OPR est préservé."""
    rule = _TF_TO_PANDAS[timeframe]
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    return _load_m1(ticker).resample(rule).agg(agg).dropna(subset=["close"])


def _round_tick(price: float, tick: float) -> float:
    return round(round(price / tick) * tick, 10)


def opr_quality_m1(ticker: str) -> dict[str, dict]:
    """Pré-calcule la QUALITÉ de formation de l'OPR depuis le M1, par jour NY.

    {date_str -> {body_ratio, vol_accel}} mesuré sur les bougies M1 de la fenêtre
    [9h30, 9h45[ NY :
      • body_ratio = Σ|close-open| / Σ(high-low)  (formation directionnelle vs chop)
      • vol_accel  = volume(2e moitié) / volume(1re moitié)  (accélération de flux)
    CAUSAL : l'OPR est clôturé à 9h45, donc connu avant tout trigger post-OPR.
    Indépendant du TF de trading (toujours mesuré en M1, comme la découverte)."""
    m1 = _load_m1(ticker)
    m1_ny = m1.copy()
    m1_ny.index = m1.index.tz_localize("UTC").tz_convert(_NY_TZ)
    o_h, o_m = OPR_BASE_SESSION_OPEN
    oe_h, oe_m = OPR_BASE_OPR_END
    out: dict[str, dict] = {}
    for day_str, g in m1_ny.groupby(m1_ny.index.strftime("%Y-%m-%d")):
        d0 = g.index[0].normalize()
        opr = g[
            (g.index >= d0.replace(hour=o_h, minute=o_m))
            & (g.index < d0.replace(hour=oe_h, minute=oe_m))
        ]
        if len(opr) < 3:
            continue
        o_, c_ = opr["open"].values, opr["close"].values
        h_, l_ = opr["high"].values, opr["low"].values
        v_ = opr["volume"].values
        rng = np.maximum(h_ - l_, 1e-9).sum()
        half = max(len(v_) // 2, 1)
        v_first, v_last = v_[:half].sum(), v_[half:].sum()
        out[day_str] = {
            "body_ratio": float(np.abs(c_ - o_).sum() / rng),
            "vol_accel": float(v_last / v_first) if v_first > 0 else np.nan,
        }
    return out


# ═════════════════════════════════════════════════════════════════════════════
# Backtest base (TF paramétrable)
# ═════════════════════════════════════════════════════════════════════════════


def run_opr_base(
    ticker: str,
    timeframe: str,
    params: dict | None = None,
) -> pd.DataFrame:
    """Backtest OPR PUR sur `timeframe` ('m5' | 'm15'), jour par jour, heure NY."""
    p = params or {}
    sl_frac = float(p.get("sl_frac", OPR_BASE_SL_FRAC))
    rr = float(p.get("rr", OPR_BASE_RR))
    risk_usd = float(p.get("risk_usd", OPR_BASE_RISK_PER_TRADE_USD) or RISK_PER_TRADE_USD)

    # Filtre de QUALITÉ OPR optionnel (version featured) — seuils FIXES
    # pré-spécifiés (découverts cross-TF, non optimisés ici) :
    #   feat = {"body_min": 0.50, "accel_min": 0.65}
    feat = p.get("feat")
    quality = opr_quality_m1(ticker) if feat else None

    instr = INSTRUMENTS[ticker]
    tick_size = instr["tick_size"]
    dpp = instr["dollar_per_point"]
    slip_px = SLIPPAGE_TICKS_PER_TICKER.get(ticker, 1) * tick_size

    df = load_tf(ticker, timeframe)
    if df.empty:
        return pd.DataFrame(columns=TRADE_COLS)

    # Index NY (DST-aware) pour le découpage par jour de session.
    df_ny = df.copy()
    df_ny.index = df.index.tz_localize("UTC").tz_convert(_NY_TZ)

    o_h, o_m = OPR_BASE_SESSION_OPEN
    oe_h, oe_m = OPR_BASE_OPR_END
    e_h, e_m = OPR_BASE_SESSION_END

    trades: list[dict] = []
    ny_days = pd.DatetimeIndex(df_ny.index.normalize().unique()).sort_values()

    for day in ny_days:
        day_open = day.replace(hour=o_h, minute=o_m)
        opr_end = day.replace(hour=oe_h, minute=oe_m)  # exclusif
        sess_end = day.replace(hour=e_h, minute=e_m)

        opr_bars = df_ny[(df_ny.index >= day_open) & (df_ny.index < opr_end)]
        if opr_bars.empty:
            continue
        opr_high = float(opr_bars["high"].max())
        opr_low = float(opr_bars["low"].min())
        if opr_high <= opr_low:
            continue

        # Gate de qualité OPR (featured) — skip le jour si formation non qualifiée.
        if feat:
            q = quality.get(day.strftime("%Y-%m-%d"))
            if q is None:
                continue
            if q["body_ratio"] < feat.get("body_min", 0.0):
                continue
            accel = q.get("vol_accel")
            if feat.get("accel_min") is not None and (
                accel is None or np.isnan(accel) or accel < feat["accel_min"]
            ):
                continue

        post = df_ny[(df_ny.index >= opr_end) & (df_ny.index <= sess_end)]
        if post.empty:
            continue

        _run_day(
            post=post,
            opr_high=opr_high,
            opr_low=opr_low,
            sl_frac=sl_frac,
            rr=rr,
            risk_usd=risk_usd,
            dpp=dpp,
            tick_size=tick_size,
            slip_px=slip_px,
            sess_end=sess_end,
            timeframe=timeframe,
            trades=trades,
        )

    return pd.DataFrame(trades) if trades else pd.DataFrame(columns=TRADE_COLS)


def _run_day(
    post: pd.DataFrame,
    opr_high: float,
    opr_low: float,
    sl_frac: float,
    rr: float,
    risk_usd: float,
    dpp: float,
    tick_size: float,
    slip_px: float,
    sess_end: pd.Timestamp,
    timeframe: str,
    trades: list[dict],
) -> None:
    """Joue une session post-OPR : cassure → limit retest → SL/TP. Aucun filtre."""
    opr_range = opr_high - opr_low
    pending: dict | None = None
    position: dict | None = None
    bars = list(post.iterrows())

    for i in range(len(bars)):
        ts, bar = bars[i]
        o, h, l, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])

        # ── 1. Position ouverte : SL/TP / close-all ──────────────────────────
        if position is not None:
            direction = position["direction"]
            sl, tp = position["sl"], position["tp"]
            if direction == "long":
                hit_sl, hit_tp = (l <= sl), (h >= tp)
            else:
                hit_sl, hit_tp = (h >= sl), (l <= tp)
            exit_price, result = None, None
            if (hit_sl and hit_tp) or hit_sl:  # ambigu → SL prioritaire
                exit_price, result = sl, "SL"
            elif hit_tp:
                exit_price, result = tp, "TP"
            elif ts >= sess_end:
                exit_price, result = c, "TE"
            if result is not None:
                _append_trade(trades, position, exit_price, result, ts, i, dpp, slip_px, timeframe)
                position = None
            continue

        # ── 2. Ordre limite pending : retest (valide jusqu'à la cloche) ───────
        if pending is not None:
            if ts >= sess_end:
                pending = None
            else:
                level = pending["entry"]
                if l <= level <= h:
                    position = {
                        **pending,
                        "fill_time": str(ts),
                        "bars_to_fill": int(i - pending["trigger_idx"]),
                    }
                    pending = None
                    # Check immédiat SL/TP sur la bougie de fill (conservateur).
                    direction = position["direction"]
                    sl, tp = position["sl"], position["tp"]
                    if direction == "long":
                        hit_sl, hit_tp = (l <= sl), (h >= tp)
                    else:
                        hit_sl, hit_tp = (h >= sl), (l <= tp)
                    exit_price, result = None, None
                    if (hit_sl and hit_tp) or hit_sl:
                        exit_price, result = sl, "SL"
                    elif hit_tp:
                        exit_price, result = tp, "TP"
                    if result is not None:
                        _append_trade(
                            trades, position, exit_price, result, ts, i, dpp, slip_px, timeframe
                        )
                        position = None
                    continue

        # ── 3. Recherche d'une cassure (open dedans, close dehors) ───────────
        if pending is not None or position is not None:
            continue
        if ts >= sess_end:
            continue
        direction = None
        if o < opr_high and c > opr_high:
            direction = "long"
        elif o > opr_low and c < opr_low:
            direction = "short"
        if direction is None:
            continue

        entry = opr_high if direction == "long" else opr_low
        sl_dist = sl_frac * opr_range
        tp_dist = rr * sl_dist
        if direction == "long":
            sl_price = _round_tick(entry - sl_dist, tick_size)
            tp_price = _round_tick(entry + tp_dist, tick_size)
        else:
            sl_price = _round_tick(entry + sl_dist, tick_size)
            tp_price = _round_tick(entry - tp_dist, tick_size)
        sl_dist = abs(entry - sl_price)
        tp_dist = abs(entry - tp_price)
        if sl_dist <= 0 or tp_dist <= 0:
            continue
        n_ct = int(risk_usd / (sl_dist * dpp))
        if n_ct <= 0:
            continue

        pending = {
            "direction": direction,
            "entry": float(entry),
            "sl": float(sl_price),
            "tp": float(tp_price),
            "sl_dist": float(sl_dist),
            "tp_dist": float(tp_dist),
            "rr": round(tp_dist / sl_dist, 2),
            "n_ct": int(n_ct),
            "opr_high": float(opr_high),
            "opr_low": float(opr_low),
            "opr_range": float(opr_range),
            "trigger_time": str(ts),
            "trigger_idx": i,
        }

    # Fin de session : position ouverte → close ; ordre pending → NOT_FILLED.
    if position is not None:
        last_ts, last_bar = bars[-1]
        _append_trade(
            trades,
            position,
            float(last_bar["close"]),
            "TE",
            last_ts,
            len(bars) - 1,
            dpp,
            slip_px,
            timeframe,
        )
    elif pending is not None:
        trades.append(_not_filled_row(pending, timeframe))


def _append_trade(
    trades: list[dict],
    position: dict,
    exit_price: float,
    result: str,
    exit_ts: pd.Timestamp,
    exit_idx: int,
    dpp: float,
    slip_px: float,
    timeframe: str,
) -> None:
    """Ligne de trade canonique, P&L NET (slippage 2× + commission RT)."""
    direction = position["direction"]
    entry = position["entry"]
    n_ct = position["n_ct"]
    raw_pts = (exit_price - entry) if direction == "long" else (entry - exit_price)
    pnl_gross = raw_pts * n_ct * dpp
    slip_cost = 2.0 * slip_px * n_ct * dpp
    comm_cost = COMMISSION_RT_PER_CONTRACT * n_ct
    pnl_net = pnl_gross - slip_cost - comm_cost
    fill_ts = pd.Timestamp(position["fill_time"])
    trades.append(
        {
            "date": exit_ts.strftime("%Y-%m-%d"),
            "dir": direction,
            "entry": float(entry),
            "sl": float(position["sl"]),
            "tp": float(position["tp"]),
            "sl_dist": float(position["sl_dist"]),
            "tp_dist": float(position["tp_dist"]),
            "rr": float(position["rr"]),
            "n_ct": int(n_ct),
            "result": result,
            "pnl": round(float(pnl_net), 2),
            "fill_time": position["fill_time"],
            "exit_time": str(exit_ts),
            "exit": float(exit_price),
            "regime": "OPR_BASE",
            "pnl_gross": round(float(pnl_gross), 2),
            "opr_high": position["opr_high"],
            "opr_low": position["opr_low"],
            "opr_range": position["opr_range"],
            "trigger_time": position["trigger_time"],
            "bars_to_fill": position.get("bars_to_fill"),
            "entry_hour_ny": int(fill_ts.hour),
            "timeframe": timeframe,
        }
    )


def _not_filled_row(pending: dict, timeframe: str) -> dict:
    """Ligne NOT_FILLED (ordre limite jamais retouché avant la cloche)."""
    return {
        "date": pd.Timestamp(pending["trigger_time"]).strftime("%Y-%m-%d"),
        "dir": pending["direction"],
        "entry": float(pending["entry"]),
        "sl": float(pending["sl"]),
        "tp": float(pending["tp"]),
        "sl_dist": float(pending["sl_dist"]),
        "tp_dist": float(pending["tp_dist"]),
        "rr": float(pending["rr"]),
        "n_ct": int(pending["n_ct"]),
        "result": "NOT_FILLED",
        "pnl": 0.0,
        "fill_time": None,
        "exit_time": None,
        "exit": None,
        "regime": "OPR_BASE",
        "pnl_gross": 0.0,
        "opr_high": pending["opr_high"],
        "opr_low": pending["opr_low"],
        "opr_range": pending["opr_range"],
        "trigger_time": pending["trigger_time"],
        "bars_to_fill": None,
        "entry_hour_ny": None,
        "timeframe": timeframe,
    }
