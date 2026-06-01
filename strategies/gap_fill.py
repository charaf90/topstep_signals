"""
Stratégie gap-fill-v1 — Gap Fill Open NY (reversion vers close RTH J-1).

Concept       : Le marche tend a combler les gaps entre le close RTH J-1 (PSC)
                et l'open RTH J (OPEN_RTH). Edge statistique : ~60-70% des gaps
                moderes (< 0.5% PSC) se comblent dans la matinee NY.
Edge          : Market makers et algos VWAP/TWAP rebalancent autour du dernier
                prix RTH significatif (PSC). Les participants qui ont acheteles
                gap overnight liquident quand il se comble -> pression directionnelle
                mecanique le matin. Qui paie : les holders du gap overnight.
Falsification : PF OOS < 1.2 OU taux comblage < 50% sur 8 barres M15
                OU bootstrap portfolio < 50%.
Indicateurs   : ATR14 sur barres RTH uniquement, GAP_pct, GAP_atr.
Fenetre NY    : Detection setup a 09:30 ET. Entree : open barre 09:45 ET.
                Confirmation : close barre 09:30 dans sens du gap.
                Time Exit : 11:00 ET.

Pièges critiques implementes :
  1. PSC = close de la DERNIERE barre M15 dont ts_ny <= 16:00 ET J-1 (RTH only).
     Ne jamais prendre df.iloc[-1] sans filtrer sur RTH.
  2. ATR14 = rolling 14 barres sur index RTH uniquement (09:30-16:00 ET).
     Calcul sur DF brut inclurait barres overnight -> ATR sous-estime.
  3. DST-aware via zoneinfo("America/New_York").
  4. No look-ahead : decision a 09:30 = barres J-1 et anterieures.
     Confirmation = close barre 09:30 (connue a 09:45 ET uniquement).
     Entree effective = open barre 09:45 ET.
  5. C4 (open dans range RTH J-1) : implementee. Range sur barres RTH J-1.
  6. Fill conservatif : SL prioritaire si SL et TP dans meme barre M15.
  7. Frictions : SLIPPAGE_TICKS_PER_TICKER + COMMISSION_RT_PER_CONTRACT.
"""

from __future__ import annotations

import datetime as _datetime
from zoneinfo import ZoneInfo

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

# Reproductibilite
np.random.seed(42)

from config import (
    COMMISSION_RT_PER_CONTRACT,
    GAP_FILTER_MONDAY,
    GAP_MAX_ATR,
    GAP_MIN_PCT,
    GAP_SL_ATR_MULT,
    GAP_STRATEGY_VERSION,
    GAP_TE_HOUR,
    GAP_TE_MINUTE,
    INSTRUMENTS,
    MACRO_EVENT_DATES,
    RISK_PER_TRADE_USD,
    SLIPPAGE_TICKS_PER_TICKER,
)
from core.risk_topstep import trade_allowed

# ── Identite ─────────────────────────────────────────────────────────────────
STRATEGY_ID = GAP_STRATEGY_VERSION  # "gap-fill-v1"
TICKERS = ["MES1", "NQ1", "YM1"]
CSV_SUFFIX = "_data"
CSV_TIMEFRAME = "m15"

_NY = ZoneInfo("America/New_York")

# ── Grille d'optimisation (PHASE 4) ──────────────────────────────────────────
# 4 dimensions : GAP_MIN_PCT x GAP_MAX_ATR x SL_ATR_MULT x FILTER_MONDAY
# 3 x 3 x 3 x 2 = 54 combos — Bonferroni applicable (seuil p < 0.05/54 = 0.0009)
PARAM_GRID = {
    "gap_min_pct": [0.03, 0.05, 0.08],
    "gap_max_atr": [0.20, 0.30, 0.40],
    "sl_atr_mult": [0.75, 1.0, 1.5],
    "filter_monday": [True, False],
}


# ── Helpers indicateurs ───────────────────────────────────────────────────────


def _compute_atr_rth_daily(df_with_ts_ny: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    ATR14 calcule sur les sessions RTH — deux interpretations reconciliees :

    La fiche researcher: 'ATR 14 periodes calcule UNIQUEMENT sur les barres RTH'.
    Interpretation retenue: ATR JOURNALIER (daily True Range) calcule sur les
    14 derniers JOURS de session RTH. C'est la seule interpretation coherente
    avec le filtre GAP_MAX_ATR = 0.30 : un gap NQ1 typique de 100 pts / ATR
    daily 300 pts = 0.33 (filtere). Vs ATR M15 (~60 pts): 100/60 = 1.67 >>
    GAP_MAX_ATR=0.30, ce qui eliminerait 99% des gaps (non conforme au concept).

    Mais pour le SL (GAP_SL_ATR_MULT × ATR14), un ATR daily sur NQ1 (~300 pts)
    avec SL_MULT=1.0 donne SL=300 pts >> gap de 50-200 pts -> RR < 1.
    Le SL sera donc en ATR M15 (barres) pour garder un RR raisonnable.

    Solution retenue (deux ATR distincts dans run_backtest) :
      - atr_daily  : daily ATR RTH (14 jours) -> filtre C3 GAP_MAX_ATR
      - atr_m15    : ATR M15 RTH (14 barres)  -> SL distance

    Cette fonction retourne l'ATR JOURNALIER (pour C3).
    Pas de look-ahead : ATR[jour J] = rolling sur les 14 jours AVANT J.
    """
    ts_ny = df_with_ts_ny["_ts_ny"]
    is_rth = ((ts_ny.dt.hour > 9) | ((ts_ny.dt.hour == 9) & (ts_ny.dt.minute >= 30))) & (
        (ts_ny.dt.hour < 16) | ((ts_ny.dt.hour == 16) & (ts_ny.dt.minute == 0))
    )

    df_rth = df_with_ts_ny[is_rth].copy()
    df_rth["_date_ny"] = df_rth["_ts_ny"].dt.date

    # Aggregation journaliere : high_rth, low_rth, close_rth
    daily = (
        df_rth.groupby("_date_ny")
        .agg(
            high_rth=("high", "max"),
            low_rth=("low", "min"),
            close_rth=("close", "last"),
        )
        .sort_index()
    )

    # True Range journalier
    hl_d = daily["high_rth"] - daily["low_rth"]
    hc_d = (daily["high_rth"] - daily["close_rth"].shift(1)).abs()
    lc_d = (daily["low_rth"] - daily["close_rth"].shift(1)).abs()
    tr_d = pd.concat([hl_d, hc_d, lc_d], axis=1).max(axis=1)

    # Rolling 14 jours + shift(1) = no look-ahead
    atr_daily = tr_d.rolling(window=period, min_periods=max(5, period // 2)).mean().shift(1)

    # Re-aligner sur l'index M15 complet
    dates_ny = ts_ny.dt.date
    atr_full = dates_ny.map(atr_daily)
    return pd.Series(atr_full.values, index=df_with_ts_ny.index)


def _compute_atr_rth_m15(df_with_ts_ny: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    ATR14 sur barres M15 RTH uniquement (pour calcul du SL).

    Un ATR M15 sur NQ1 est ~50-80 pts, ce qui donne des SL coherents
    avec le gap (50-200 pts). Avec SL_MULT=1.0 et gap ~100 pts, RR ~1-2.
    Pas de look-ahead : shift(1) sur le rolling.
    """
    ts_ny = df_with_ts_ny["_ts_ny"]
    is_rth = ((ts_ny.dt.hour > 9) | ((ts_ny.dt.hour == 9) & (ts_ny.dt.minute >= 30))) & (
        (ts_ny.dt.hour < 16) | ((ts_ny.dt.hour == 16) & (ts_ny.dt.minute == 0))
    )

    df_rth = df_with_ts_ny[is_rth].copy()
    hl = df_rth["high"] - df_rth["low"]
    hc = (df_rth["high"] - df_rth["close"].shift(1)).abs()
    lc = (df_rth["low"] - df_rth["close"].shift(1)).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    atr = tr.rolling(window=period, min_periods=max(5, period // 2)).mean().shift(1)

    atr_full = atr.reindex(df_with_ts_ny.index).ffill()
    return atr_full


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
    plus_di = 100 * plus_dm.ewm(span=period, adjust=False).mean() / atr.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(span=period, adjust=False).mean() / atr.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(span=period, adjust=False).mean()


# ── Logique principale ────────────────────────────────────────────────────────


def run_backtest(
    df_15m: pd.DataFrame,
    ticker: str,
    tf=None,
    params: dict | None = None,
    topstep_guard: bool = True,
) -> pd.DataFrame:
    """
    Backtest gap-fill-v1.

    Schema colonnes obligatoire :
      date, dir, entry, sl, tp, sl_dist, tp_dist, rr, n_ct,
      result (TP|SL|TE|NOT_FILLED), pnl, fill_time, exit_time, exit, regime

    pnl = P&L NET (slippage + commissions inclus).

    Parametres (depuis config.py, overridables par `params` lors de l'optimisation) :
      gap_min_pct   : gap minimum en % du PSC (filtre C2)
      gap_max_atr   : gap maximum en multiple d'ATR14 RTH (filtre C3)
      sl_atr_mult   : SL en multiples d'ATR14 depuis OPEN_RTH
      filter_monday : exclure les lundis (GAP_FILTER_MONDAY)
    """
    p = params or {}
    gap_min_pct = p.get("gap_min_pct", GAP_MIN_PCT)
    gap_max_atr = p.get("gap_max_atr", GAP_MAX_ATR)
    sl_atr_mult = p.get("sl_atr_mult", GAP_SL_ATR_MULT.get(ticker, 1.0))
    filter_monday = p.get("filter_monday", GAP_FILTER_MONDAY)
    te_hour = GAP_TE_HOUR
    te_minute = GAP_TE_MINUTE

    instr = INSTRUMENTS[ticker]
    tick_size = instr["tick_size"]
    pt_value = instr.get("point_value", instr.get("dollar_per_point", 1.0))
    slip_ticks = SLIPPAGE_TICKS_PER_TICKER.get(ticker, 1)
    slip_px = slip_ticks * tick_size

    # ── Preparation du DataFrame avec timestamps NY ───────────────────────────
    df = df_15m.copy()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df["_ts_ny"] = df.index.tz_convert(_NY)

    # Indicateurs : deux ATR distincts
    # atr_daily -> filtre C3 GAP_MAX_ATR (daily range RTH, 14 jours)
    # atr_m15   -> SL distance (ATR M15 RTH, 14 barres)
    df["atr_daily"] = _compute_atr_rth_daily(df, period=14)
    df["atr_m15"] = _compute_atr_rth_m15(df, period=14)
    df["adx"] = _compute_adx(df, period=14)
    # Percentile ATR daily (pour stress tests PHASE 5)
    df["atr_pct"] = df["atr_daily"].rolling(window=300, min_periods=50).rank(pct=True)

    # Precalcul des close/open RTH par date NY pour lookup efficace
    # ── Dictionnaire par date NY : close PSC (derniere barre RTH J-1 <= 16:00) ──
    ts_ny_series = df["_ts_ny"]
    is_rth_close = (
        (ts_ny_series.dt.hour > 9) | ((ts_ny_series.dt.hour == 9) & (ts_ny_series.dt.minute >= 30))
    ) & (
        (ts_ny_series.dt.hour < 16) | ((ts_ny_series.dt.hour == 16) & (ts_ny_series.dt.minute == 0))
    )

    # Grouper par date et extraire le close de la derniere barre RTH <= 16:00
    df_rth = df[is_rth_close].copy()
    df_rth["_date_ny"] = df_rth["_ts_ny"].dt.date
    # Pour chaque date, la derniere barre RTH = last dans le groupe date
    psc_map = {}  # date_ny -> PSC (close derniere barre RTH)
    rth_lo_map = {}  # date_ny -> low RTH (pour C4)
    rth_hi_map = {}  # date_ny -> high RTH (pour C4)
    for date_ny, grp in df_rth.groupby("_date_ny"):
        psc_map[date_ny] = grp["close"].iloc[-1]
        rth_lo_map[date_ny] = grp["low"].min()
        rth_hi_map[date_ny] = grp["high"].max()

    # ── Identification des barres 09:30 ET et 09:45 ET (entree/confirmation) ──
    # La barre 09:30 = barre dont le timestamp NY est exactement 09:30
    # La barre 09:45 = barre dont le timestamp NY est exactement 09:45
    is_930 = (ts_ny_series.dt.hour == 9) & (ts_ny_series.dt.minute == 30)
    is_945 = (ts_ny_series.dt.hour == 9) & (ts_ny_series.dt.minute == 45)

    # Warmup suffisant pour ATR
    warmup = 50

    trades = []
    macro_dates = set(MACRO_EVENT_DATES)

    # ── Boucle par barre 09:45 (entree potentielle) ───────────────────────────
    # On itere sur les barres 09:45 ET uniquement — c'est la barre d'entree.
    idx_945 = df.index[is_945.values]

    for entry_bar_ts in idx_945:
        # Position dans le DataFrame
        entry_pos = df.index.get_loc(entry_bar_ts)
        if entry_pos < warmup:
            continue

        ts_entry_ny = df.at[entry_bar_ts, "_ts_ny"]
        date_ny = ts_entry_ny.date()
        date_str = date_ny.isoformat()

        # Filtre weekend
        if ts_entry_ny.weekday() >= 5:
            continue

        # Filtre lundi (C6)
        if filter_monday and ts_entry_ny.weekday() == 0:
            continue

        # Filtre macro (C5)
        if date_str in macro_dates:
            continue

        # ── Detection de la barre 09:30 du meme jour ─────────────────────────
        # La barre 09:30 est celle dont ts_ny = 09:30 ET et date = date_ny
        # Elle est a position entry_pos - 1 (barres consecutives)
        if entry_pos < 1:
            continue
        bar_930_ts = df.index[entry_pos - 1]
        bar_930_ny = df.at[bar_930_ts, "_ts_ny"]
        if bar_930_ny.hour != 9 or bar_930_ny.minute != 30:
            continue  # pas la barre attendue (gap dans les donnees ?)
        if bar_930_ny.date() != date_ny:
            continue

        # ── Calcul du PSC (close RTH J-1) ────────────────────────────────────
        # On cherche la date precedente = date la plus recente dans psc_map
        # qui soit < date_ny ET ait des barres RTH
        # PIEGE: ne pas prendre df.iloc[entry_pos-1]["close"] qui serait le
        # close de la barre precedente (possiblement overnight)
        prev_date = date_ny - _datetime.timedelta(days=1)
        # Chercher la derniere date de trading (peut etre vendredi si lundi)
        attempts = 0
        while prev_date not in psc_map and attempts < 7:
            prev_date -= _datetime.timedelta(days=1)
            attempts += 1
        if prev_date not in psc_map:
            continue

        psc = psc_map[prev_date]
        lo_j1 = rth_lo_map[prev_date]
        hi_j1 = rth_hi_map[prev_date]

        # ── OPEN_RTH = open de la barre 09:30 du jour J ──────────────────────
        open_rth = df.at[bar_930_ts, "open"]

        # ── ATR (deux variantes) ──────────────────────────────────────────────
        # atr_daily -> filtre C3 (daily ATR RTH, 14 jours)
        # atr_m15   -> SL (ATR M15 RTH, 14 barres) pour RR raisonnable
        atr_daily = df.at[bar_930_ts, "atr_daily"]
        atr_m15 = df.at[bar_930_ts, "atr_m15"]
        if pd.isna(atr_daily) or atr_daily <= 0:
            continue
        if pd.isna(atr_m15) or atr_m15 <= 0:
            continue

        # ── Calcul du GAP ─────────────────────────────────────────────────────
        gap = open_rth - psc  # positif = gap up
        gap_abs = abs(gap)
        gap_pct = gap_abs / psc * 100 if psc > 0 else 0.0
        gap_atr = gap_abs / atr_daily if atr_daily > 0 else 999.0

        # ── Conditions de setup ───────────────────────────────────────────────
        # C1 : gap existe
        if gap_abs < 1e-6:
            continue
        # C2 : gap suffisamment grand (filtre le bruit)
        if gap_pct < gap_min_pct:
            continue
        # C3 : gap pas trop violent vs ATR (evite les gap de rupture)
        if gap_atr > gap_max_atr:
            continue
        # C4 : OPEN_RTH dans le range RTH J-1 (gap modere, pas rupture)
        # Tolerance: si open est juste au bord (1 tick), on accepte
        tol = tick_size
        if not (lo_j1 - tol <= open_rth <= hi_j1 + tol):
            continue

        # ── Direction ─────────────────────────────────────────────────────────
        # SHORT si gap up (open > PSC) -> combler vers le bas vers PSC
        # LONG  si gap down (open < PSC) -> combler vers le haut vers PSC
        if gap > 0:
            direction = "short"
        else:
            direction = "long"

        # ── Confirmation : close barre 09:30 doit aller dans le bon sens ─────
        # SHORT : la premiere barre a commence a combler (close < OPEN_RTH)
        # LONG  : la premiere barre a commence a remonter (close > OPEN_RTH)
        close_930 = df.at[bar_930_ts, "close"]
        if direction == "short" and close_930 >= open_rth:
            # Pas de confirmation : le gap ne se comble pas encore
            # -> NOT_FILLED (mais on enregistre quand meme pour analyse)
            trades.append(
                {
                    "date": date_str,
                    "dir": direction,
                    "entry": open_rth,  # entry theorique
                    "sl": np.nan,
                    "tp": psc,
                    "sl_dist": np.nan,
                    "tp_dist": gap_abs,
                    "rr": np.nan,
                    "n_ct": 1,
                    "result": "NOT_FILLED",
                    "pnl": 0.0,
                    "fill_time": None,
                    "exit_time": None,
                    "exit": None,
                    "regime": _regime_tag(df.at[entry_bar_ts, "adx"]),
                    "pnl_gross": 0.0,
                    "adx": (
                        round(df.at[entry_bar_ts, "adx"], 1)
                        if pd.notna(df.at[entry_bar_ts, "adx"])
                        else None
                    ),
                    "atr_pct": (
                        round(df.at[entry_bar_ts, "atr_pct"], 2)
                        if pd.notna(df.at[entry_bar_ts, "atr_pct"])
                        else None
                    ),
                    "is_macro_day": date_str in macro_dates,
                    "gap_pct": round(gap_pct, 4),
                    "gap_atr": round(gap_atr, 4),
                    "psc": psc,
                    "open_rth": open_rth,
                }
            )
            continue
        if direction == "long" and close_930 <= open_rth:
            trades.append(
                {
                    "date": date_str,
                    "dir": direction,
                    "entry": open_rth,
                    "sl": np.nan,
                    "tp": psc,
                    "sl_dist": np.nan,
                    "tp_dist": gap_abs,
                    "rr": np.nan,
                    "n_ct": 1,
                    "result": "NOT_FILLED",
                    "pnl": 0.0,
                    "fill_time": None,
                    "exit_time": None,
                    "exit": None,
                    "regime": _regime_tag(df.at[entry_bar_ts, "adx"]),
                    "pnl_gross": 0.0,
                    "adx": (
                        round(df.at[entry_bar_ts, "adx"], 1)
                        if pd.notna(df.at[entry_bar_ts, "adx"])
                        else None
                    ),
                    "atr_pct": (
                        round(df.at[entry_bar_ts, "atr_pct"], 2)
                        if pd.notna(df.at[entry_bar_ts, "atr_pct"])
                        else None
                    ),
                    "is_macro_day": date_str in macro_dates,
                    "gap_pct": round(gap_pct, 4),
                    "gap_atr": round(gap_atr, 4),
                    "psc": psc,
                    "open_rth": open_rth,
                }
            )
            continue

        # ── Prix d'entree effectif (open barre 09:45) ────────────────────────
        entry_px = df.at[entry_bar_ts, "open"]
        if direction == "short":
            entry_eff = entry_px - slip_px  # on vend, slippage adverse
        else:
            entry_eff = entry_px + slip_px  # on achete, slippage adverse

        # ── Garde : le trade doit avoir du sens depuis entry_eff ─────────────
        # Si le gap a ete comble ou inverse durant la barre 09:30, l'entree
        # peut se retrouver du mauvais cote de PSC -> trade non viable.
        # SHORT : entry_eff doit etre STRICTEMENT au-dessus de PSC (si on est
        #   deja en dessous, le gap est comble, on aurait chasse la position).
        # LONG  : entry_eff doit etre STRICTEMENT en dessous de PSC.
        # Tolerance : 2 ticks (bruit de fill)
        tol_entry = 2 * tick_size
        if direction == "short" and entry_eff <= psc + tol_entry:
            # Gap deja comble ou proche -> skip
            trades.append(
                {
                    "date": date_str,
                    "dir": direction,
                    "entry": entry_eff,
                    "sl": np.nan,
                    "tp": psc,
                    "sl_dist": np.nan,
                    "tp_dist": abs(entry_eff - psc),
                    "rr": np.nan,
                    "n_ct": 1,
                    "result": "NOT_FILLED",
                    "pnl": 0.0,
                    "fill_time": None,
                    "exit_time": None,
                    "exit": None,
                    "regime": _regime_tag(df.at[entry_bar_ts, "adx"]),
                    "pnl_gross": 0.0,
                    "adx": (
                        round(df.at[entry_bar_ts, "adx"], 1)
                        if pd.notna(df.at[entry_bar_ts, "adx"])
                        else None
                    ),
                    "atr_pct": (
                        round(df.at[entry_bar_ts, "atr_pct"], 2)
                        if pd.notna(df.at[entry_bar_ts, "atr_pct"])
                        else None
                    ),
                    "is_macro_day": date_str in macro_dates,
                    "gap_pct": round(gap_pct, 4),
                    "gap_atr": round(gap_atr, 4),
                    "psc": psc,
                    "open_rth": open_rth,
                }
            )
            continue
        if direction == "long" and entry_eff >= psc - tol_entry:
            trades.append(
                {
                    "date": date_str,
                    "dir": direction,
                    "entry": entry_eff,
                    "sl": np.nan,
                    "tp": psc,
                    "sl_dist": np.nan,
                    "tp_dist": abs(entry_eff - psc),
                    "rr": np.nan,
                    "n_ct": 1,
                    "result": "NOT_FILLED",
                    "pnl": 0.0,
                    "fill_time": None,
                    "exit_time": None,
                    "exit": None,
                    "regime": _regime_tag(df.at[entry_bar_ts, "adx"]),
                    "pnl_gross": 0.0,
                    "adx": (
                        round(df.at[entry_bar_ts, "adx"], 1)
                        if pd.notna(df.at[entry_bar_ts, "adx"])
                        else None
                    ),
                    "atr_pct": (
                        round(df.at[entry_bar_ts, "atr_pct"], 2)
                        if pd.notna(df.at[entry_bar_ts, "atr_pct"])
                        else None
                    ),
                    "is_macro_day": date_str in macro_dates,
                    "gap_pct": round(gap_pct, 4),
                    "gap_atr": round(gap_atr, 4),
                    "psc": psc,
                    "open_rth": open_rth,
                }
            )
            continue

        # ── Niveaux SL/TP ────────────────────────────────────────────────────────
        # TP  = PSC (comblage complet du gap)
        # SL  = OPEN_RTH + sl_atr_mult × ATR_M15 (ancre sur l'ouverture RTH)
        # Justification : le SL est ancre sur OPEN_RTH (niveau de reference du gap)
        # et non sur l'entry_eff (qui peut avoir bouge pendant barre 09:30).
        # Cela preserve la semantique du risque : si le prix retourne a OPEN_RTH
        # et depasse d'1 ATR, le gap fill est invalide (breakout).
        # Note : ce design cree un RR variable selon la position d'entry_eff vs
        # OPEN_RTH. Si entry_eff < OPEN_RTH (SHORT, gap partiellement comble),
        # le risque reel est plus petit que sl_dist_from_open -> benefice.
        tp = psc

        if direction == "short":
            sl = open_rth + sl_atr_mult * atr_m15  # SL au-dessus OPEN_RTH
            sl_dist = sl - entry_eff  # distance reelle risque
            tp_dist = entry_eff - tp  # distance reelle profit
        else:
            sl = open_rth - sl_atr_mult * atr_m15  # SL en dessous OPEN_RTH
            sl_dist = entry_eff - sl  # distance reelle risque
            tp_dist = tp - entry_eff  # distance reelle profit

        if sl_dist <= 0 or tp_dist <= 0:
            # Peut arriver si entry_eff est deja au-dela de sl (gap trop comble)
            continue

        rr = tp_dist / sl_dist

        # Sizing base sur sl_dist (distance de risque reelle)
        n_ct = max(1, int(RISK_PER_TRADE_USD / (sl_dist * pt_value)))

        # Garde-fou Topstep (signature : day_pnl, cum_pnl, peak_pnl)
        if topstep_guard:
            allowed, _ = trade_allowed(0.0, 0.0, 0.0)
            if not allowed:
                continue

        # ── Time Exit : 11:00 ET ─────────────────────────────────────────────
        # Calculer le timestamp de time exit pour ce jour
        te_ts_ny = ts_entry_ny.replace(hour=te_hour, minute=te_minute, second=0, microsecond=0)
        # Convertir en UTC pour comparer avec l'index
        te_ts_utc = te_ts_ny.astimezone(_utc_from_ny())

        # ── Simulation fill + exit ────────────────────────────────────────────
        result = "NOT_FILLED"
        fill_time = None
        exit_time = None
        exit_px = None
        pnl_gross = 0.0
        pnl = 0.0

        # Entree au marche sur la barre 09:45 (entry_bar_ts)
        # On est toujours fille sur la barre 09:45 (entree au marche, pas limite)
        fill_time = entry_bar_ts
        result = "OPEN"  # temporaire

        # Chercher SL/TP/TE dans les barres suivantes
        for k in range(entry_pos + 1, len(df)):
            fut = df.iloc[k]
            fut_ts = df.index[k]
            fut_ts_ny = df.at[fut_ts, "_ts_ny"]

            # Time Exit check (11:00 ET)
            if fut_ts_ny >= te_ts_ny:
                result = "TE"
                exit_px = fut["open"]  # sortie au marche = open barre suivante
                exit_time = fut_ts
                break

            # SL/TP check conservatif
            sl_hit = (direction == "short" and fut["high"] >= sl) or (
                direction == "long" and fut["low"] <= sl
            )
            tp_hit = (direction == "short" and fut["low"] <= tp) or (
                direction == "long" and fut["high"] >= tp
            )

            if sl_hit and tp_hit:
                # Ambigu — SL prioritaire (convention projet)
                result, exit_px, exit_time = "SL", sl, fut_ts
                break
            elif sl_hit:
                result, exit_px, exit_time = "SL", sl, fut_ts
                break
            elif tp_hit:
                result, exit_px, exit_time = "TP", tp, fut_ts
                break
        else:
            # Fin de donnees sans TP/SL/TE
            result = "TE"
            exit_px = df.iloc[-1]["close"]
            exit_time = df.index[-1]

        # ── Calcul P&L ────────────────────────────────────────────────────────
        # Slippage sortie : SL/TE = marche (slippage adverse), TP = proche limit
        # Pour simplification : slippage sur entree et sortie (conservatif)
        if result in ("SL", "TE"):
            slip_exit = slip_px
        else:
            slip_exit = slip_px  # TP : assume slip aussi (conservatif)

        if direction == "short":
            raw_pnl = (entry_eff - exit_px) * n_ct * pt_value
        else:
            raw_pnl = (exit_px - entry_eff) * n_ct * pt_value

        pnl_gross = raw_pnl
        slip_cost = slip_exit * n_ct * pt_value  # sortie
        comm_cost = COMMISSION_RT_PER_CONTRACT * n_ct
        pnl = pnl_gross - slip_cost - comm_cost

        # Regime
        adx_val = df.at[entry_bar_ts, "adx"]
        atr_pct_val = df.at[entry_bar_ts, "atr_pct"]
        regime = _regime_tag(adx_val)

        trades.append(
            {
                "date": date_str,
                "dir": direction,
                "entry": entry_eff,  # prix effectif apres slippage entree
                "sl": sl,
                "tp": tp,
                "sl_dist": sl_dist,
                "tp_dist": tp_dist,
                "rr": round(rr, 2),
                "n_ct": n_ct,
                "result": result,
                "pnl": round(pnl, 2),
                "fill_time": fill_time,
                "exit_time": exit_time,
                "exit": exit_px,
                "regime": regime,
                # Colonnes optionnelles PHASE 5
                "pnl_gross": round(pnl_gross, 2),
                "adx": round(adx_val, 1) if pd.notna(adx_val) else None,
                "atr_pct": round(atr_pct_val, 2) if pd.notna(atr_pct_val) else None,
                "is_macro_day": date_str in macro_dates,
                # Colonnes debug gap
                "gap_pct": round(gap_pct, 4),
                "gap_atr": round(gap_atr, 4),
                "psc": psc,
                "open_rth": open_rth,
            }
        )

    cols_std = [
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
        "gap_pct",
        "gap_atr",
        "psc",
        "open_rth",
    ]
    if not trades:
        return pd.DataFrame(columns=cols_std)
    df_out = pd.DataFrame(trades)
    for c in cols_std:
        if c not in df_out.columns:
            df_out[c] = None
    return df_out[cols_std]


def _utc_from_ny():
    """Retourne le ZoneInfo UTC."""
    return ZoneInfo("UTC")


def _regime_tag(adx_val) -> str:
    """Tag regime pour stress tests."""
    if pd.isna(adx_val) or adx_val is None:
        return "neutral"
    if adx_val > 25:
        return "trending"
    if adx_val < 20:
        return "ranging"
    return "neutral"


# ── Visualisation par jour ────────────────────────────────────────────────────


def plot_day(
    df_15m: pd.DataFrame,
    ticker: str,
    date_str: str,
    day_trades: list,
    output_path: str,
) -> None:
    """
    Chart complet d'une journee de trading gap-fill.

    Affiche :
      - Chandeliers OHLC
      - Niveau PSC (Prior Session Close) en ligne horizontale orange
      - Niveau OPEN_RTH en ligne horizontale bleue
      - Niveaux SL/TP en tirets
      - Fleches entree (^/v), marqueur sortie (x)
      - Zone setup (ombre entre PSC et OPEN_RTH)
    """
    if df_15m.index.tz is None:
        df_idx = pd.DatetimeIndex(df_15m.index).tz_localize("UTC")
    else:
        df_idx = df_15m.index

    ts_ny = pd.DatetimeIndex(df_idx).tz_convert(_NY)
    mask = ts_ny.normalize() == pd.Timestamp(date_str, tz=_NY)
    day = df_15m[mask.values].copy()
    if day.empty:
        return

    fig, (ax1, ax2) = plt.subplots(
        2,
        1,
        figsize=(16, 10),
        gridspec_kw={"height_ratios": [3, 1]},
        sharex=True,
    )

    x = range(len(day))

    # Chandeliers
    for i, (_, row) in enumerate(day.iterrows()):
        color = "#26a69a" if row["close"] >= row["open"] else "#ef5350"
        ax1.plot([i, i], [row["low"], row["high"]], color=color, lw=0.8)
        ax1.bar(
            i,
            abs(row["close"] - row["open"]),
            bottom=min(row["open"], row["close"]),
            color=color,
            width=0.6,
            alpha=0.9,
        )

    day_pnl = 0.0
    psc_drawn = False
    opr_drawn = False

    for trade in day_trades:
        psc_val = trade.get("psc")
        open_rth = trade.get("open_rth")

        # Niveaux PSC et OPEN_RTH
        if psc_val and not psc_drawn:
            ax1.axhline(
                psc_val, color="#ff9800", ls="-", lw=1.5, alpha=0.9, label=f"PSC {psc_val:.2f}"
            )
            psc_drawn = True
        if open_rth and not opr_drawn:
            ax1.axhline(
                open_rth,
                color="#2196F3",
                ls="-",
                lw=1.5,
                alpha=0.9,
                label=f"OPEN_RTH {open_rth:.2f}",
            )
            opr_drawn = True

        # Zone gap
        if psc_val and open_rth and not psc_drawn and not opr_drawn:
            lo_gap = min(psc_val, open_rth)
            hi_gap = max(psc_val, open_rth)
            ax1.axhspan(lo_gap, hi_gap, alpha=0.10, color="#ff9800")

        if trade.get("result") == "NOT_FILLED":
            continue

        # Trouver l'index de la barre de fill
        try:
            day_idx_list = list(day.index)
            fill_ts = trade.get("fill_time")
            if fill_ts is not None:
                fill_idx = day_idx_list.index(fill_ts)
            else:
                continue
        except (ValueError, KeyError):
            continue

        try:
            exit_ts = trade.get("exit_time")
            exit_idx = (
                day_idx_list.index(exit_ts) if exit_ts in day_idx_list else len(day_idx_list) - 1
            )
        except (ValueError, KeyError):
            exit_idx = fill_idx

        color = "#00c853" if trade["dir"] == "long" else "#d50000"
        marker = "^" if trade["dir"] == "long" else "v"
        ax1.scatter(fill_idx, trade["entry"], marker=marker, color=color, s=180, zorder=5)
        if trade.get("exit") is not None:
            ax1.scatter(exit_idx, trade["exit"], marker="x", color="white", s=140, zorder=5)
        if pd.notna(trade.get("sl")):
            ax1.axhline(trade["sl"], color="#ef5350", ls="--", lw=0.9, alpha=0.7)
        if pd.notna(trade.get("tp")):
            ax1.axhline(trade["tp"], color="#26a69a", ls="--", lw=0.9, alpha=0.7)

        # Zone gap shadee
        if psc_val and open_rth:
            lo_g = min(psc_val, open_rth)
            hi_g = max(psc_val, open_rth)
            ax1.axhspan(lo_g, hi_g, alpha=0.08, color="#ff9800")

        day_pnl += trade.get("pnl", 0.0)

    # Sous-panneau : volume
    if "volume" in day.columns:
        ax2.bar(x, day["volume"].values, color="#7986cb", alpha=0.5)
        ax2.set_ylabel("Volume", fontsize=9)
    elif "atr_rth" in day.columns:
        # Fallback : ATR
        pass

    ax1.set_title(
        f"{ticker} — {date_str}  |  gap-fill-v1  |  P&L net : {day_pnl:+.0f}$",
        fontsize=12,
        fontweight="bold",
    )
    ax1.legend(loc="upper left", fontsize=8)
    ax1.set_facecolor("#1a1a2e")
    ax2.set_facecolor("#1a1a2e")
    fig.patch.set_facecolor("#13131f")
    for ax in (ax1, ax2):
        ax.tick_params(colors="white")
        for spine in ax.spines.values():
            spine.set_edgecolor("#333355")

    plt.tight_layout()
    plt.savefig(output_path, dpi=120, bbox_inches="tight")
    plt.close(fig)
