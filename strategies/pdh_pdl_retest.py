"""
Strategie PDH/PDL Retest — Previous Day High/Low Retest.

Concept       : Retest du Previous Day High (PDH) ou Previous Day Low (PDL) apres
                cassure confirmee en session RTH. Le PDH/PDL est un niveau
                institutionnel majeur : la cassure initiale cree une zone de liquidite
                que le prix reteste avant la continuation.
Edge          : Liquidity sweep : les stops au-dessus/dessous du PDH/PDL sont
                declenches lors de la cassure, puis le retest offre soit une
                continuation (trend continuation) soit un piege (mean-reversion).
                Le filtrage par ATR garantit que seuls les cassures significatives
                (pas le bruit) generent un signal.
Falsification : PF OOS < 1.2 ou bootstrap portfolio < 50% invalide l'hypothese.
                Si NOT_FILLED > 40%, le niveau LIMIT est trop strict (ajuster BREAK_BUFFER).
Indicateurs   : ATR14 causal, PDH/PDL RTH J-1 gele a 09:30 NY
Fenetre NY    : 10:00-15:30 (exclusion premiere demi-heure RTH volatilite)
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from config import (
    COMMISSION_RT_PER_CONTRACT,
    INSTRUMENTS,
    MACRO_EVENT_DATES,
    PDH_PDL_BREAK_BUFFER_TICKS,
    PDH_PDL_GAP_EXCLUSION_ATR,
    PDH_PDL_MAX_RETEST_BARS,
    PDH_PDL_MIN_BREAK_ATR,
    PDH_PDL_RETEST_CLOSE_BUFFER,
    PDH_PDL_RETEST_ZONE_ATR_HIGH,
    PDH_PDL_RETEST_ZONE_ATR_LOW,
    PDH_PDL_SL_ATR_MULT,
    PDH_PDL_STRATEGY_VERSION,
    PDH_PDL_TICKERS,
    PDH_PDL_TP_RR,
    RISK_PER_TRADE_USD,
    SLIPPAGE_TICKS_PER_TICKER,
)
from core.risk_topstep import trade_allowed

# Reproductibilite
np.random.seed(42)

# ── Identite ──────────────────────────────────────────────────────────────────
STRATEGY_ID = PDH_PDL_STRATEGY_VERSION  # "pdh-pdl-retest-v1"
TICKERS = PDH_PDL_TICKERS  # ["MES1", "NQ1", "YM1", "MGC1"]
CSV_SUFFIX = "_pdh_pdl"
CSV_TIMEFRAME = "m15"

_NY = ZoneInfo("America/New_York")  # DST-aware nativement

# ── Grille d'optimisation ─────────────────────────────────────────────────────
# 4 dimensions : 3 x 3 x 3 x 3 = 81 combos
# Bonferroni : p_seuil = 0.05 / 81 = 0.000617
# Justification :
#   break_buffer_ticks : [0, 1, 2] — buffer de confirmation cassure (tick_size)
#   sl_atr_mult        : [0.3, 0.5, 0.7] — stop loss en multiple ATR
#   tp_rr              : [1.5, 2.0, 2.5] — risk-reward ratio cible
#   max_retest_bars    : [4, 8, 16] — fenetre max d'attente du retest (barres M15)
PARAM_GRID = {
    "break_buffer_ticks": [0, 1, 2],
    "sl_atr_mult": [0.3, 0.5, 0.7],
    "tp_rr": [1.5, 2.0, 2.5],
    "max_retest_bars": [4, 8, 16],
}


# ── Helpers indicateurs ───────────────────────────────────────────────────────


def _compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """ATR sans look-ahead — EWM sur True Range."""
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift(1)).abs()
    lc = (df["low"] - df["close"].shift(1)).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def _compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """ADX pour classification trending/ranging — stress tests PHASE 5."""
    high, low, close = df["high"], df["low"], df["close"]
    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm.shift(0)) & (minus_dm > 0), 0.0)
    tr = pd.concat(
        [
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.ewm(span=period, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(span=period, adjust=False).mean() / atr
    minus_di = 100 * minus_dm.ewm(span=period, adjust=False).mean() / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(span=period, adjust=False).mean()


def _rth_session_bounds(ts_ny: pd.Timestamp) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Retourne les bornes RTH 09:30-16:00 NY pour un jour donne."""
    date = ts_ny.date()
    open_rth = pd.Timestamp(date, tz=_NY).replace(hour=9, minute=30)
    close_rth = pd.Timestamp(date, tz=_NY).replace(hour=16, minute=0)
    return open_rth, close_rth


def _compute_pdh_pdl(df_15m: pd.DataFrame) -> pd.DataFrame:
    """
    Calcule le PDH/PDL RTH J-1 pour chaque barre.
    PDH = high RTH (09:30-16:00 NY) du jour J-1.
    PDL = low RTH (09:30-16:00 NY) du jour J-1.

    CRITIQUE : gele a 09:30 NY du jour J — jamais recalcule avec donnees J.
    Implementation : calcul par jour RTH en avance, puis merge.
    """
    # Convertir l'index en NY
    df = df_15m.copy()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df["ts_ny"] = df.index.tz_convert(_NY)
    df["date_ny"] = df["ts_ny"].dt.date
    df["hour_ny"] = df["ts_ny"].dt.hour
    df["minute_ny"] = df["ts_ny"].dt.minute

    # Masque RTH : 09:30 <= heure < 16:00
    # Inclut les barres dont le debut est dans la RTH
    rth_mask = ((df["hour_ny"] > 9) | ((df["hour_ny"] == 9) & (df["minute_ny"] >= 30))) & (
        df["hour_ny"] < 16
    )

    df_rth = df[rth_mask].copy()

    # High et low RTH par jour
    daily_rth = (
        df_rth.groupby("date_ny")
        .agg(
            pdh=("high", "max"),
            pdl=("low", "min"),
        )
        .reset_index()
    )
    daily_rth.columns = ["date_ny", "pdh", "pdl"]

    # Shifter d'un jour : PDH/PDL du jour J = PDH/PDL RTH du jour J-1
    daily_rth = daily_rth.sort_values("date_ny").reset_index(drop=True)
    daily_rth["pdh_prev"] = daily_rth["pdh"].shift(1)
    daily_rth["pdl_prev"] = daily_rth["pdl"].shift(1)

    # Merger avec les donnees originales
    df = df.merge(
        daily_rth[["date_ny", "pdh_prev", "pdl_prev"]],
        on="date_ny",
        how="left",
    )
    df.index = df_15m.index  # restaurer l'index original

    return df


# ── Logique principale ────────────────────────────────────────────────────────


def run_backtest(
    df_15m: pd.DataFrame,
    ticker: str,
    tf=None,
    params: dict | None = None,
    topstep_guard: bool = True,
) -> pd.DataFrame:
    """
    Execute le backtest de la strategie PDH/PDL Retest sur df_15m.

    Schéma de colonnes obligatoire (compatibilite core/optimizer.py) :
      date, dir, entry, sl, tp, sl_dist, tp_dist, rr, n_ct,
      result (TP|SL|TE|NOT_FILLED), pnl, fill_time, exit_time, exit, regime

    Colonnes optionnelles :
      pnl_gross, adx, atr_pct, is_macro_day

    `pnl` = P&L net (slippage + commissions inclus).
    """
    p = params or {}
    break_buffer_ticks = p.get("break_buffer_ticks", PDH_PDL_BREAK_BUFFER_TICKS)
    sl_atr_mult = p.get("sl_atr_mult", PDH_PDL_SL_ATR_MULT)
    tp_rr = p.get("tp_rr", PDH_PDL_TP_RR)
    max_retest_bars = p.get("max_retest_bars", PDH_PDL_MAX_RETEST_BARS)

    instr = INSTRUMENTS[ticker]
    tick_size = instr["tick_size"]
    pt_value = instr["dollar_per_point"]
    slip_ticks = SLIPPAGE_TICKS_PER_TICKER.get(ticker, 1)
    slip_px = slip_ticks * tick_size  # slippage en prix

    # ── Calcul indicateurs causal ─────────────────────────────────────────────
    df = df_15m.copy()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")

    df["atr"] = _compute_atr(df, period=14)
    df["adx"] = _compute_adx(df, period=14)
    # Percentile ATR sur fenetre glissante 30 jours ~ 30*26 = 780 barres M15 RTH
    df["atr_pct"] = df["atr"].rolling(window=780, min_periods=100).rank(pct=True)

    # PDH/PDL causal (J-1 RTH)
    df = _compute_pdh_pdl(df)

    # Reconvertir les timestamps NY
    df["ts_ny"] = df.index.tz_convert(_NY)
    df["date_ny_str"] = df["ts_ny"].dt.date.astype(str)

    # Warmup : 14 barres ATR + 780 barres atr_pct + 1 jour PDH (~ 26 barres RTH)
    warmup = 820

    # Précomputation : open de la première barre RTH 09:30 par date
    rth_open_by_date: dict = {}
    for idx_row, row in df.iterrows():
        ts = row["ts_ny"]
        if ts.hour == 9 and ts.minute == 30:
            d = str(ts.date())
            if d not in rth_open_by_date:
                rth_open_by_date[d] = row["open"]

    trades = []
    # Compteurs par jour : long et short separes
    daily_long: dict = {}  # date -> nb long fills
    daily_short: dict = {}  # date -> nb short fills

    # Tracking des positions ouvertes (LIMIT en attente ou position active)
    # On itere sequentiellement, donc on suit l'etat courant
    pending_long: dict | None = None  # {entry, sl, tp, setup_bar, sl_dist, tp_dist, n_ct, date}
    pending_short: dict | None = None

    i = warmup
    while i < len(df):
        bar = df.iloc[i]
        ts_ny = df["ts_ny"].iloc[i]

        # Weekends off
        if ts_ny.weekday() >= 5:
            i += 1
            continue

        date_str = df["date_ny_str"].iloc[i]
        hour_ny = ts_ny.hour
        minute_ny = ts_ny.minute

        # PDH/PDL disponibles ? (NaN au premier jour de donnees)
        pdh = bar["pdh_prev"]
        pdl = bar["pdl_prev"]
        atr = bar["atr"]

        if pd.isna(pdh) or pd.isna(pdl) or pd.isna(atr) or atr <= 0:
            i += 1
            continue

        # ── Detection des signaux a 10:00-15:30 NY ───────────────────────────
        in_window = ((hour_ny > 10) or (hour_ny == 10 and minute_ny >= 0)) and (
            (hour_ny < 15) or (hour_ny == 15 and minute_ny <= 15)
        )
        # Barre 10:00 = debut fenetre ; barre 15:15 = derniere possible
        # (la barre 15:30 declenche TE des positions ouvertes — voir ci-dessous)

        # ── Time Exit force a 15:30 NY ────────────────────────────────────────
        # Annuler toute pending et forcer cloture position ouverte
        is_te_bar = hour_ny == 15 and minute_ny >= 30

        if is_te_bar:
            # Pas de nouveaux setups, annuler les pending en cours
            pending_long = None
            pending_short = None
            i += 1
            continue

        # ── Break buffer en prix ───────────────────────────────────────────────
        break_px = break_buffer_ticks * tick_size

        # ── Seuil de cassure minimale ──────────────────────────────────────────
        min_break = PDH_PDL_MIN_BREAK_ATR * atr

        # ── Annuler les pending expirés ────────────────────────────────────────
        if pending_long is not None:
            bars_since = i - pending_long["setup_bar"]
            if bars_since > max_retest_bars:
                # Enregistrer comme NOT_FILLED
                pl = pending_long
                trades.append(
                    {
                        "date": pl["date"],
                        "dir": "long",
                        "entry": pl["entry"],
                        "sl": pl["sl"],
                        "tp": pl["tp"],
                        "sl_dist": pl["sl_dist"],
                        "tp_dist": pl["tp_dist"],
                        "rr": round(pl["tp_dist"] / pl["sl_dist"], 2) if pl["sl_dist"] > 0 else 0,
                        "n_ct": pl["n_ct"],
                        "result": "NOT_FILLED",
                        "pnl": 0.0,
                        "fill_time": None,
                        "exit_time": None,
                        "exit": None,
                        "regime": pl["regime"],
                        "pnl_gross": 0.0,
                        "adx": pl["adx"],
                        "atr_pct": pl["atr_pct"],
                        "is_macro_day": pl["is_macro_day"],
                    }
                )
                pending_long = None

        if pending_short is not None:
            bars_since = i - pending_short["setup_bar"]
            if bars_since > max_retest_bars:
                pl = pending_short
                trades.append(
                    {
                        "date": pl["date"],
                        "dir": "short",
                        "entry": pl["entry"],
                        "sl": pl["sl"],
                        "tp": pl["tp"],
                        "sl_dist": pl["sl_dist"],
                        "tp_dist": pl["tp_dist"],
                        "rr": round(pl["tp_dist"] / pl["sl_dist"], 2) if pl["sl_dist"] > 0 else 0,
                        "n_ct": pl["n_ct"],
                        "result": "NOT_FILLED",
                        "pnl": 0.0,
                        "fill_time": None,
                        "exit_time": None,
                        "exit": None,
                        "regime": pl["regime"],
                        "pnl_gross": 0.0,
                        "adx": pl["adx"],
                        "atr_pct": pl["atr_pct"],
                        "is_macro_day": pl["is_macro_day"],
                    }
                )
                pending_short = None

        # ── Tentative de fill des pending ─────────────────────────────────────
        for direction, pending, counter in [
            ("long", pending_long, daily_long),
            ("short", pending_short, daily_short),
        ]:
            if pending is None:
                continue

            entry_px = pending["entry"]
            sl = pending["sl"]
            tp = pending["tp"]

            # Fill LIMIT : long -> low(i) <= entry ; short -> high(i) >= entry
            if direction == "long":
                filled = bar["low"] <= entry_px
            else:
                filled = bar["high"] >= entry_px

            if not filled:
                continue

            fill_time = df.index[i]
            n_ct = pending["n_ct"]

            # SL / TP / TE dans la meme barre (conservatif : SL prioritaire)
            if direction == "long":
                sl_hit = bar["low"] <= sl
                tp_hit = bar["high"] >= tp
            else:
                sl_hit = bar["high"] >= sl
                tp_hit = bar["low"] <= tp

            if sl_hit:
                result = "SL"
                exit_px = sl
                exit_time = df.index[i]
            elif tp_hit:
                result = "TP"
                exit_px = tp
                exit_time = df.index[i]
            else:
                # Chercher TP/SL dans les barres suivantes (jusqu'a 15:30 NY ou fin fenetre)
                result = "TE"
                exit_px = None
                exit_time = None
                for k in range(i + 1, len(df)):
                    fut = df.iloc[k]
                    ts_k = df["ts_ny"].iloc[k]

                    # TE a 15:30 NY
                    if (
                        ts_k.date() != ts_ny.date()
                        or (ts_k.hour == 15 and ts_k.minute >= 30)
                        or ts_k.hour > 15
                    ):
                        result = "TE"
                        exit_px = fut["open"]  # approximation : open barre suivante
                        exit_time = df.index[k]
                        break

                    if direction == "long":
                        sl_k = fut["low"] <= sl
                        tp_k = fut["high"] >= tp
                    else:
                        sl_k = fut["high"] >= sl
                        tp_k = fut["low"] <= tp

                    if sl_k and tp_k:
                        result = "SL"
                        exit_px = sl
                        exit_time = df.index[k]
                        break
                    if sl_k:
                        result = "SL"
                        exit_px = sl
                        exit_time = df.index[k]
                        break
                    if tp_k:
                        result = "TP"
                        exit_px = tp
                        exit_time = df.index[k]
                        break

                if exit_px is None:
                    # Fin du dataset
                    result = "TE"
                    exit_px = df.iloc[-1]["close"]
                    exit_time = df.index[-1]

            # P&L brut + net
            raw_pnl = (exit_px - entry_px) * (1 if direction == "long" else -1)
            pnl_gross = raw_pnl * n_ct * pt_value
            slip_cost = 2 * slip_px * n_ct * pt_value
            comm_cost = COMMISSION_RT_PER_CONTRACT * n_ct
            pnl = pnl_gross - slip_cost - comm_cost

            # Incrementer le compteur pour ce jour
            counter[date_str] = counter.get(date_str, 0) + 1

            trades.append(
                {
                    "date": pending["date"],
                    "dir": direction,
                    "entry": entry_px,
                    "sl": sl,
                    "tp": tp,
                    "sl_dist": pending["sl_dist"],
                    "tp_dist": pending["tp_dist"],
                    "rr": (
                        round(pending["tp_dist"] / pending["sl_dist"], 2)
                        if pending["sl_dist"] > 0
                        else 0
                    ),
                    "n_ct": n_ct,
                    "result": result,
                    "pnl": round(pnl, 2),
                    "fill_time": fill_time,
                    "exit_time": exit_time,
                    "exit": exit_px,
                    "regime": pending["regime"],
                    "pnl_gross": round(pnl_gross, 2),
                    "adx": pending["adx"],
                    "atr_pct": pending["atr_pct"],
                    "is_macro_day": pending["is_macro_day"],
                }
            )

            if direction == "long":
                pending_long = None
            else:
                pending_short = None

        # ── Detection nouveau signal ─────────────────────────────────────────
        if not in_window:
            i += 1
            continue

        # Limites quotidiennes
        n_long_today = daily_long.get(date_str, 0)
        n_short_today = daily_short.get(date_str, 0)

        # Tag regime (pour stress tests)
        adx_val = bar["adx"] if pd.notna(bar["adx"]) else None
        if adx_val is not None and adx_val > 25:
            regime = "trending"
        elif adx_val is not None and adx_val < 20:
            regime = "ranging"
        else:
            regime = "neutral"

        is_macro = date_str in MACRO_EVENT_DATES
        atr_pct_val = bar["atr_pct"] if pd.notna(bar["atr_pct"]) else None

        # Filtre macro : pas de nouveau setup les jours FOMC/CPI/NFP
        if is_macro:
            i += 1
            continue

        # ── SIGNAL LONG (retest PDH) ──────────────────────────────────────────
        if pending_long is None and n_long_today < 1:
            # Condition 1 : cassure confirmee — close precedente > PDH + buffer + min_break
            # On utilise bar["close"] = barre COURANTE pour verifier la cassure
            # NOTE : on detecte la cassure SUR la barre courante i et on pose
            # un ordre LIMIT au PDH pour le retest. Le retest = bar i+1 ou suivantes.
            # Pas de look-ahead car on utilise close(i) pour placer l'ordre LIMIT.
            prev_close = df.iloc[i - 1]["close"] if i > 0 else bar["close"]
            close_i = bar["close"]

            # Cassure : close(i) > PDH + break_px + min_break
            cassure_long = close_i > pdh + break_px + min_break

            if cassure_long:
                # Condition 2 : retest — low(i) dans la zone de retest autour du PDH
                retest_low_bound = pdh - PDH_PDL_RETEST_ZONE_ATR_LOW * atr
                retest_high_bound = pdh + PDH_PDL_RETEST_ZONE_ATR_HIGH * atr
                retest_zone = bar["low"] <= retest_high_bound

                # Close(i) ne doit pas etre trop en dessous du PDH
                retest_close_ok = close_i >= pdh - PDH_PDL_RETEST_CLOSE_BUFFER * atr

                # Condition 3 : exclusion gap overnight (lookup O(1) via dict précompté)
                open_rth = rth_open_by_date.get(date_str)
                gap_ok_long = True
                if open_rth is not None and open_rth > pdh + PDH_PDL_GAP_EXCLUSION_ATR * atr:
                    gap_ok_long = False

                if retest_zone and retest_close_ok and gap_ok_long:
                    # Entree LIMIT @ PDH (le retest se fait sur la barre suivante)
                    entry_px = pdh
                    sl_dist = sl_atr_mult * atr
                    tp_dist = sl_dist * tp_rr
                    sl = entry_px - sl_dist
                    tp = entry_px + tp_dist
                    n_ct = max(1, int(RISK_PER_TRADE_USD / ((sl_dist + slip_px) * pt_value)))

                    if topstep_guard:
                        allowed, _ = trade_allowed(0.0, 0.0, 0.0)
                        if not allowed:
                            i += 1
                            continue

                    pending_long = {
                        "entry": entry_px,
                        "sl": sl,
                        "tp": tp,
                        "sl_dist": sl_dist,
                        "tp_dist": tp_dist,
                        "n_ct": n_ct,
                        "setup_bar": i,
                        "date": date_str,
                        "regime": regime,
                        "adx": round(adx_val, 1) if adx_val is not None else None,
                        "atr_pct": round(atr_pct_val, 2) if atr_pct_val is not None else None,
                        "is_macro_day": is_macro,
                    }

        # ── SIGNAL SHORT (retest PDL) ─────────────────────────────────────────
        if pending_short is None and n_short_today < 1:
            close_i = bar["close"]

            # Cassure : close(i) < PDL - break_px - min_break
            cassure_short = close_i < pdl - break_px - min_break

            if cassure_short:
                # Retest : high(i) dans la zone de retest autour du PDL
                retest_high_bound_s = pdl + PDH_PDL_RETEST_ZONE_ATR_LOW * atr
                retest_zone_s = bar["high"] >= pdl - PDH_PDL_RETEST_ZONE_ATR_HIGH * atr

                # Close(i) ne doit pas etre trop au-dessus du PDL
                retest_close_ok_s = close_i <= pdl + PDH_PDL_RETEST_CLOSE_BUFFER * atr

                # Exclusion gap overnight SHORT (lookup O(1) via dict précompté)
                open_rth = rth_open_by_date.get(date_str)
                gap_ok_short = True
                if open_rth is not None and open_rth < pdl - PDH_PDL_GAP_EXCLUSION_ATR * atr:
                    gap_ok_short = False

                if retest_zone_s and retest_close_ok_s and gap_ok_short:
                    entry_px = pdl
                    sl_dist = sl_atr_mult * atr
                    tp_dist = sl_dist * tp_rr
                    sl = entry_px + sl_dist
                    tp = entry_px - tp_dist
                    n_ct = max(1, int(RISK_PER_TRADE_USD / ((sl_dist + slip_px) * pt_value)))

                    if topstep_guard:
                        allowed, _ = trade_allowed(0.0, 0.0, 0.0)
                        if not allowed:
                            i += 1
                            continue

                    pending_short = {
                        "entry": entry_px,
                        "sl": sl,
                        "tp": tp,
                        "sl_dist": sl_dist,
                        "tp_dist": tp_dist,
                        "n_ct": n_ct,
                        "setup_bar": i,
                        "date": date_str,
                        "regime": regime,
                        "adx": round(adx_val, 1) if adx_val is not None else None,
                        "atr_pct": round(atr_pct_val, 2) if atr_pct_val is not None else None,
                        "is_macro_day": is_macro,
                    }

        i += 1

    # Schema standard
    cols = [
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
        "adx",
        "atr_pct",
        "is_macro_day",
    ]
    if not trades:
        return pd.DataFrame(columns=cols)

    df_trades = pd.DataFrame(trades)
    for col in cols:
        if col not in df_trades.columns:
            df_trades[col] = None
    return df_trades[cols]


# ── Visualisation par jour ────────────────────────────────────────────────────


def plot_day(
    df_15m: pd.DataFrame,
    ticker: str,
    date_str: str,
    day_trades: list,
    output_path: str,
) -> None:
    """
    Chart complet d'une journee de trading PDH/PDL Retest.

    Affiche :
      - Chandeliers OHLC
      - Niveaux PDH et PDL (lignes horizontales bleues)
      - ATR (panneau inferieur)
      - Fleches entree + marqueur sortie + SL/TP
      - Zone de retest (rectangle semi-transparent)
    """
    df = df_15m.copy()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")

    ts_ny = df.index.tz_convert(_NY)
    mask = ts_ny.date == pd.Timestamp(date_str, tz=_NY).date()
    day = df[mask].copy()
    if day.empty:
        return

    # Recalculer ATR et PDH/PDL pour ce jour
    df_full = df.copy()
    df_full["atr"] = _compute_atr(df_full)
    df_full = _compute_pdh_pdl(df_full)

    day_with_indic = df_full[mask].copy()
    if day_with_indic.empty:
        return

    pdh = day_with_indic["pdh_prev"].iloc[0] if "pdh_prev" in day_with_indic.columns else None
    pdl = day_with_indic["pdl_prev"].iloc[0] if "pdl_prev" in day_with_indic.columns else None

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(16, 10), gridspec_kw={"height_ratios": [3, 1]}, sharex=True
    )

    x_range = range(len(day))

    # Chandeliers
    for idx, (_, row) in enumerate(day.iterrows()):
        color = "#26a69a" if row["close"] >= row["open"] else "#ef5350"
        ax1.plot([idx, idx], [row["low"], row["high"]], color=color, lw=0.8)
        ax1.bar(
            idx,
            abs(row["close"] - row["open"]),
            bottom=min(row["open"], row["close"]),
            color=color,
            width=0.6,
            alpha=0.9,
        )

    # PDH et PDL
    if pdh is not None and not pd.isna(pdh):
        ax1.axhline(pdh, color="#1e88e5", ls="-", lw=1.5, alpha=0.9, label=f"PDH {pdh:.2f}")
    if pdl is not None and not pd.isna(pdl):
        ax1.axhline(pdl, color="#e53935", ls="-", lw=1.5, alpha=0.9, label=f"PDL {pdl:.2f}")

    day_pnl = 0.0
    day_index_list = list(day.index)

    for trade in day_trades:
        if trade.get("result") == "NOT_FILLED":
            continue
        try:
            fill_idx = day_index_list.index(trade["fill_time"])
        except (ValueError, KeyError):
            continue
        try:
            exit_idx = day_index_list.index(trade["exit_time"])
        except (ValueError, KeyError):
            exit_idx = fill_idx

        color_trade = "#00c853" if trade["dir"] == "long" else "#d50000"
        marker = "^" if trade["dir"] == "long" else "v"
        ax1.scatter(fill_idx, trade["entry"], marker=marker, color=color_trade, s=150, zorder=5)
        ax1.scatter(exit_idx, trade["exit"], marker="x", color="white", s=120, zorder=5)
        ax1.axhline(trade["sl"], color="#ef5350", ls="--", lw=0.8, alpha=0.7)
        ax1.axhline(trade["tp"], color="#26a69a", ls="--", lw=0.8, alpha=0.7)

        # Zone de retest autour du niveau
        if trade["dir"] == "long" and pdh is not None and not pd.isna(pdh):
            atr_est = trade["sl_dist"] / PDH_PDL_SL_ATR_MULT if PDH_PDL_SL_ATR_MULT > 0 else 0
            ax1.axhspan(
                pdh - PDH_PDL_RETEST_ZONE_ATR_LOW * atr_est,
                pdh + PDH_PDL_RETEST_ZONE_ATR_HIGH * atr_est,
                alpha=0.08,
                color="#1e88e5",
            )
        elif trade["dir"] == "short" and pdl is not None and not pd.isna(pdl):
            atr_est = trade["sl_dist"] / PDH_PDL_SL_ATR_MULT if PDH_PDL_SL_ATR_MULT > 0 else 0
            ax1.axhspan(
                pdl - PDH_PDL_RETEST_ZONE_ATR_HIGH * atr_est,
                pdl + PDH_PDL_RETEST_ZONE_ATR_LOW * atr_est,
                alpha=0.08,
                color="#e53935",
            )

        day_pnl += trade.get("pnl", 0.0)

    # ATR panneau inferieur
    if "atr" in day_with_indic.columns:
        ax2.fill_between(
            x_range, 0, day_with_indic["atr"].values, alpha=0.4, color="#7986cb", label="ATR14"
        )
        ax2.set_ylabel("ATR14", fontsize=9)
        ax2.legend(loc="upper right", fontsize=8)

    ax1.set_title(
        f"{ticker} — {date_str}   |   PDH/PDL Retest   |   P&L net : {day_pnl:+.0f} $",
        fontsize=12,
        fontweight="bold",
    )
    ax1.legend(loc="upper left", fontsize=8)
    ax1.set_facecolor("#1a1a2e")
    ax2.set_facecolor("#1a1a2e")
    fig.patch.set_facecolor("#13131f")
    ax1.tick_params(colors="white")
    ax2.tick_params(colors="white")
    ax1.yaxis.label.set_color("white")
    ax2.yaxis.label.set_color("white")

    plt.tight_layout()
    plt.savefig(output_path, dpi=120, bbox_inches="tight")
    plt.close(fig)
