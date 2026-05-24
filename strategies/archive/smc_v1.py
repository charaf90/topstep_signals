"""
Stratégie SMC (Smart Money Concepts - LuxAlgo) — smc-v1.

Concept :
    Multi-zone Smart Money Concepts détectées en temps réel sur M15 (causal) :
      - OB_internal  : Order Block sur pivots length=5  (latence +5 barres)
      - OB_swing     : Order Block sur pivots length=50 (latence +50 barres)
      - FVG bull/bear: Fair Value Gap (gap 3-bougies)   (latence +0, à la close)
      - EQH bearish  : 2 pivots high égaux (length=3)   (latence +3 barres)
      - EQL bullish  : 2 pivots low égaux  (length=3)   (latence +3 barres)

    Entry : LIMIT au bord PROCHE de la zone (variante V_A).
    Filtre dur Premium/Discount sur le dernier swing range CONFIRMÉ.

    Exits :
      SL = bord opposé de la zone ± SMC_SL_BUFFER_TICKS
      TP = prochain swing HH (long) / LL (short) confirmé APRÈS fill_bar
      TP fallback : entry ± 1.5 × sl_dist si pas de pivot dans 40 barres

Edge théorique :
    Confluence d'ordres institutionnels (liquidity zones) + filtre directionnel
    Premium/Discount. Les pivots length=50 sélectionnent des structures fortes.

Falsification :
    Bootstrap portfolio OOS < 50 %, OU PF OOS < 1.0 sur 60 trades live.

Architecture : RECHERCHE pure (strategies/). N'importe rien de broker/
ni de core/opr.py / core/strategy_fib.py / core/vpc.py.

Causalité (anti-leak) :
    - Pivots confirmés UNIQUEMENT après pivot_length barres droites.
    - FVG dispo dès la clôture de la 3e bougie (barre i où le gap est défini).
    - ATR/ADX au fill : shift(1) → barre fill_bar - 1 (la barre du fill n'est
      pas encore clôturée à l'instant du touch intra-barre).
    - Premium/Discount mid calculé sur swings dont les deux pivots sont
      confirmés à fill_bar (j_pivot + pivot_length ≤ fill_bar).
    - Exits scan strictement séquentiel (j > fill_bar pour TP/SL).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple
from zoneinfo import ZoneInfo

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from config import (
    INSTRUMENTS,
    RISK_PER_TRADE_USD,
    SLIPPAGE_TICKS_PER_TICKER,
    COMMISSION_RT_PER_CONTRACT,
    MACRO_EVENT_DATES,
    # SMC params
    SMC_STRATEGY_VERSION,
    SMC_TICKERS,
    SMC_PIVOT_LENGTH_INTERNAL, SMC_PIVOT_LENGTH_SWING, SMC_PIVOT_LENGTH_EQHL,
    SMC_FVG_MIN_GAP_TICKS,
    SMC_EQH_EQL_THRESHOLD,
    SMC_SWING_FILTER_LENGTH,
    SMC_ZONE_MAX_AGE_BARS,
    SMC_SL_BUFFER_TICKS,
    SMC_TP_FALLBACK_RR, SMC_TP_FALLBACK_BARS,
    SMC_MAX_CONTRACTS_PER_TRADE, SMC_MAX_TRADES_PER_DAY,
    SMC_SESSION_START_NY, SMC_SESSION_END_NY,
    SMC_ZONE_PRIORITY,
    SMC_ATR_PERIOD, SMC_ADX_PERIOD,
    SMC_EXCLUDE_MACRO_DAYS,
)
from core.risk_topstep import trade_allowed

# Reproductibilité
np.random.seed(42)

# ── Identité de la stratégie ─────────────────────────────────────────────────
STRATEGY_ID   = SMC_STRATEGY_VERSION          # "smc-v1"
TICKERS       = SMC_TICKERS                   # ["MES1", "NQ1", "YM1"]
CSV_SUFFIX    = "_smc"
CSV_TIMEFRAME = "m15"

_NY = ZoneInfo("America/New_York")

# ── Grille d'optimisation (4 dimensions, ≤ 81 combos) ───────────────────────
# Bonferroni-friendly : 81 tests → p_seuil = 0.05/81 ≈ 0.0006
PARAM_GRID = {
    "swing_filter_length":  [5, 10, 20],
    "zone_max_age_bars":    [48, 96, 192],
    "eqh_eql_threshold":    [0.05, 0.10, 0.15],
    "tp_fallback_rr":       [1.0, 1.5, 2.0],
}


# ═════════════════════════════════════════════════════════════════════════════
# Helpers indicateurs (no look-ahead par construction — utilisent .shift(1))
# ═════════════════════════════════════════════════════════════════════════════

def _compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """ATR Wilder, sans look-ahead. Disponible à la clôture de chaque barre."""
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift(1)).abs()
    lc = (df["low"]  - df["close"].shift(1)).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def _compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """ADX(14) Wilder, sans look-ahead."""
    high, low, close = df["high"], df["low"], df["close"]
    up   = high.diff()
    down = -low.diff()
    plus_dm  = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    plus_di  = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean()  / atr
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / period, adjust=False).mean()


# ═════════════════════════════════════════════════════════════════════════════
# Pivots (LuxAlgo style) — confirmation différée
# ═════════════════════════════════════════════════════════════════════════════

def _find_pivots(
    series: np.ndarray,
    length: int,
    kind: str = "high",
) -> List[int]:
    """
    Retourne les index des pivots confirmés (un pivot à l'index `i` requiert
    `length` barres droites supérieures/inférieures de chaque côté).

    kind = "high" → pivots hauts. "low" → pivots bas.

    Conformément à la règle de causalité : un pivot à l'index `i` n'est
    confirmé/visible qu'à partir de `i + length`. À utiliser uniquement
    après vérification que `current_bar >= pivot_idx + length`.
    """
    n = len(series)
    pivots: List[int] = []
    if n < 2 * length + 1:
        return pivots

    if kind == "high":
        for i in range(length, n - length):
            window = series[i - length : i + length + 1]
            # pivot strict : valeur centrale ≥ toutes les voisines, et > au moins
            # une de chaque côté (pour éviter les plateaux)
            if series[i] == window.max() and series[i] > window[0] and series[i] > window[-1]:
                pivots.append(i)
    else:  # low
        for i in range(length, n - length):
            window = series[i - length : i + length + 1]
            if series[i] == window.min() and series[i] < window[0] and series[i] < window[-1]:
                pivots.append(i)
    return pivots


# ═════════════════════════════════════════════════════════════════════════════
# Structure de zone
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class ZoneCandidate:
    """
    Zone SMC candidate. Toutes les métadonnées figées à la création.

    `available_from` = première barre où un ORDRE LIMIT placé après la clôture
    de la barre de confirmation peut effectivement être fillé. Convention :
      - FVG : confirmé à clôture de la barre i (3e bougie du gap), donc
              available_from = i + 1 (la barre suivante peut le toucher).
      - OB_internal : pivot length=5 confirmé à pivot_idx + 5 (clôture),
              donc available_from = pivot_idx + 5 + 1.
      - OB_swing : idem avec length=50 → available_from = pivot_idx + 50 + 1.
      - EQH/EQL : pivot length=3 confirmé à pivot_idx + 3 (clôture),
              donc available_from = pivot_idx + 3 + 1.

    Cette convention élimine le look-ahead intra-barre : un ordre ne peut être
    fillé qu'à partir de la barre suivante celle de la confirmation.
    """
    zone_type: str           # "OB_internal" | "OB_swing" | "FVG" | "EQH" | "EQL"
    direction: str           # "long" | "short"
    top: float               # bord haut de la zone
    bottom: float            # bord bas de la zone
    created_at_bar: int      # index de formation (clôture barre)
    available_from: int      # première barre où ordre limit fillable
    invalidated_at: Optional[int] = None  # index d'invalidation (close au-delà)


def _detect_zones_causal(
    df: pd.DataFrame,
    current_bar: int,
    pivot_length_internal: int,
    pivot_length_swing: int,
    pivot_length_eqhl: int,
    eqh_eql_threshold: float,
    fvg_min_gap_ticks: int,
    tick_size: float,
    atr_series: pd.Series,
) -> List[ZoneCandidate]:
    """
    Détecte toutes les zones SMC disponibles à `current_bar` (incluse).

    Causalité : utilise UNIQUEMENT `df.iloc[:current_bar + 1]`. Aucune zone
    référence un pivot non encore confirmé.

    Returns: liste de ZoneCandidate triée par `available_from` croissant.
    """
    zones: List[ZoneCandidate] = []
    # df_past : tout ce qui est disponible jusqu'à current_bar inclus.
    # Pour les pivots, on n'utilise que les indices ≤ current_bar - length.
    df_past = df.iloc[: current_bar + 1]
    n = len(df_past)
    if n < max(pivot_length_swing, 50) + 5:
        return zones

    highs = df_past["high"].values
    lows  = df_past["low"].values
    opens = df_past["open"].values
    closes = df_past["close"].values

    # ── OB_internal (pivots length=5) ────────────────────────────────────────
    p_internal_highs = _find_pivots(highs, pivot_length_internal, "high")
    p_internal_lows  = _find_pivots(lows,  pivot_length_internal, "low")

    # OB bearish (top après pivot high) : zone = dernière bougie haussière avant pivot
    for p_idx in p_internal_highs:
        # On cherche la dernière bougie haussière (close > open) avant le pivot
        # dans une fenêtre raisonnable (pivot_length barres)
        for k in range(p_idx - 1, max(p_idx - pivot_length_internal - 1, -1), -1):
            if k < 0:
                break
            if closes[k] > opens[k]:  # bullish candle → OB bearish reversal
                zone_top    = max(opens[k], closes[k])
                zone_bottom = min(opens[k], closes[k])
                zones.append(ZoneCandidate(
                    zone_type="OB_internal",
                    direction="short",
                    top=float(zone_top),
                    bottom=float(zone_bottom),
                    created_at_bar=k,
                    available_from=p_idx + pivot_length_internal + 1,
                ))
                break

    # OB bullish (bottom après pivot low)
    for p_idx in p_internal_lows:
        for k in range(p_idx - 1, max(p_idx - pivot_length_internal - 1, -1), -1):
            if k < 0:
                break
            if closes[k] < opens[k]:  # bearish candle → OB bullish reversal
                zone_top    = max(opens[k], closes[k])
                zone_bottom = min(opens[k], closes[k])
                zones.append(ZoneCandidate(
                    zone_type="OB_internal",
                    direction="long",
                    top=float(zone_top),
                    bottom=float(zone_bottom),
                    created_at_bar=k,
                    available_from=p_idx + pivot_length_internal + 1,
                ))
                break

    # ── OB_swing (pivots length=50) ──────────────────────────────────────────
    p_swing_highs = _find_pivots(highs, pivot_length_swing, "high")
    p_swing_lows  = _find_pivots(lows,  pivot_length_swing, "low")

    for p_idx in p_swing_highs:
        for k in range(p_idx - 1, max(p_idx - pivot_length_swing - 1, -1), -1):
            if k < 0:
                break
            if closes[k] > opens[k]:
                zone_top    = max(opens[k], closes[k])
                zone_bottom = min(opens[k], closes[k])
                zones.append(ZoneCandidate(
                    zone_type="OB_swing",
                    direction="short",
                    top=float(zone_top),
                    bottom=float(zone_bottom),
                    created_at_bar=k,
                    available_from=p_idx + pivot_length_swing + 1,
                ))
                break

    for p_idx in p_swing_lows:
        for k in range(p_idx - 1, max(p_idx - pivot_length_swing - 1, -1), -1):
            if k < 0:
                break
            if closes[k] < opens[k]:
                zone_top    = max(opens[k], closes[k])
                zone_bottom = min(opens[k], closes[k])
                zones.append(ZoneCandidate(
                    zone_type="OB_swing",
                    direction="long",
                    top=float(zone_top),
                    bottom=float(zone_bottom),
                    created_at_bar=k,
                    available_from=p_idx + pivot_length_swing + 1,
                ))
                break

    # ── FVG (Fair Value Gap, 3 bougies) ──────────────────────────────────────
    # Bullish : low[i] > high[i-2] → gap haussier. Zone = [high[i-2], low[i]].
    # Bearish : high[i] < low[i-2] → gap baissier. Zone = [high[i], low[i-2]].
    # Disponible à la CLÔTURE de la barre i (available_from = i).
    min_gap = fvg_min_gap_ticks * tick_size
    for i in range(2, n):
        # bullish FVG
        if lows[i] - highs[i - 2] >= min_gap:
            zones.append(ZoneCandidate(
                zone_type="FVG",
                direction="long",
                top=float(lows[i]),
                bottom=float(highs[i - 2]),
                created_at_bar=i,
                available_from=i + 1,   # ordre limit placé à clôture i, fillable à i+1
            ))
        # bearish FVG
        if lows[i - 2] - highs[i] >= min_gap:
            zones.append(ZoneCandidate(
                zone_type="FVG",
                direction="short",
                top=float(lows[i - 2]),
                bottom=float(highs[i]),
                created_at_bar=i,
                available_from=i + 1,
            ))

    # ── EQH / EQL (length=3) ─────────────────────────────────────────────────
    # On cherche 2 pivots high consécutifs (resp. low) quasi égaux.
    p_eqhl_highs = _find_pivots(highs, pivot_length_eqhl, "high")
    p_eqhl_lows  = _find_pivots(lows,  pivot_length_eqhl, "low")

    # EQH bearish : 2 derniers pivots high consécutifs |h2 - h1| ≤ thr × ATR
    for idx in range(1, len(p_eqhl_highs)):
        i1, i2 = p_eqhl_highs[idx - 1], p_eqhl_highs[idx]
        h1, h2 = highs[i1], highs[i2]
        atr_ref = atr_series.iloc[i2] if i2 < len(atr_series) and pd.notna(atr_series.iloc[i2]) else np.nan
        if not np.isfinite(atr_ref) or atr_ref <= 0:
            continue
        if abs(h2 - h1) <= eqh_eql_threshold * atr_ref:
            zone_top    = max(h1, h2)
            zone_bottom = min(h1, h2)
            # petit buffer en bas pour épaisseur de zone (sinon zone trop fine)
            buffer = 0.05 * atr_ref
            zones.append(ZoneCandidate(
                zone_type="EQH",
                direction="short",
                top=float(zone_top + buffer),
                bottom=float(zone_bottom),
                created_at_bar=i2,
                available_from=i2 + pivot_length_eqhl + 1,
            ))

    # EQL bullish
    for idx in range(1, len(p_eqhl_lows)):
        i1, i2 = p_eqhl_lows[idx - 1], p_eqhl_lows[idx]
        l1, l2 = lows[i1], lows[i2]
        atr_ref = atr_series.iloc[i2] if i2 < len(atr_series) and pd.notna(atr_series.iloc[i2]) else np.nan
        if not np.isfinite(atr_ref) or atr_ref <= 0:
            continue
        if abs(l2 - l1) <= eqh_eql_threshold * atr_ref:
            zone_top    = max(l1, l2)
            zone_bottom = min(l1, l2)
            buffer = 0.05 * atr_ref
            zones.append(ZoneCandidate(
                zone_type="EQL",
                direction="long",
                top=float(zone_top),
                bottom=float(zone_bottom - buffer),
                created_at_bar=i2,
                available_from=i2 + pivot_length_eqhl + 1,
            ))

    return zones


def _filter_active_zones(
    zones: List[ZoneCandidate],
    df: pd.DataFrame,
    current_bar: int,
    zone_max_age_bars: int,
) -> List[ZoneCandidate]:
    """
    Retourne les zones encore actives à `current_bar` :
      - available_from ≤ current_bar (latence respectée)
      - non invalidées (close au-delà du bord opposé sur les barres clôturées)
      - âge ≤ zone_max_age_bars

    Causalité : pour l'invalidation on regarde uniquement les barres CLÔTURÉES
    avant `current_bar` (intervalle [available_from, current_bar - 1]).
    La barre `current_bar` n'est pas encore clôturée à l'instant du fill
    intra-barre.
    """
    active: List[ZoneCandidate] = []
    closes = df["close"].values

    for z in zones:
        # Latence
        if z.available_from > current_bar:
            continue
        # Âge max
        if (current_bar - z.created_at_bar) > zone_max_age_bars:
            continue

        # Invalidation : close au-delà du bord opposé sur [available_from, current_bar - 1]
        invalidated = False
        for j in range(z.available_from, current_bar):  # exclut current_bar
            c = closes[j]
            # Pour LONG zone : invalidation si close < bottom
            # Pour SHORT zone : invalidation si close > top
            if z.direction == "long" and c < z.bottom:
                invalidated = True
                z.invalidated_at = j
                break
            if z.direction == "short" and c > z.top:
                invalidated = True
                z.invalidated_at = j
                break
        if invalidated:
            continue

        active.append(z)
    return active


# ═════════════════════════════════════════════════════════════════════════════
# Premium / Discount filter
# ═════════════════════════════════════════════════════════════════════════════

def _last_confirmed_swing_range(
    df: pd.DataFrame,
    current_bar: int,
    pivot_length: int,
) -> Optional[Tuple[float, float]]:
    """
    Retourne (swing_low, swing_high) du dernier swing range CONFIRMÉ
    (les deux pivots ont leur confirmation < current_bar : pivot_idx + length ≤ current_bar).

    None si insuffisamment de pivots confirmés.
    """
    df_past = df.iloc[: current_bar + 1]
    if len(df_past) < 2 * pivot_length + 2:
        return None
    highs = df_past["high"].values
    lows  = df_past["low"].values
    # Pivots confirmés : on récupère ceux dont l'index ≤ current_bar - pivot_length
    max_allowed_idx = current_bar - pivot_length
    if max_allowed_idx < pivot_length:
        return None

    p_highs = [p for p in _find_pivots(highs[: max_allowed_idx + 1], pivot_length, "high")]
    p_lows  = [p for p in _find_pivots(lows[:  max_allowed_idx + 1], pivot_length, "low")]
    if not p_highs or not p_lows:
        return None

    # Dernier pivot high et dernier pivot low confirmés
    last_h_idx = p_highs[-1]
    last_l_idx = p_lows[-1]
    swing_high = float(highs[last_h_idx])
    swing_low  = float(lows[last_l_idx])
    if swing_high <= swing_low:
        return None
    return swing_low, swing_high


# ═════════════════════════════════════════════════════════════════════════════
# TP : dernier swing HH/LL CONFIRMÉ avant le fill (causal)
# ═════════════════════════════════════════════════════════════════════════════
#
# ATTENTION look-ahead : "prochain swing HH/LL APRÈS fill" comme indiqué dans
# certaines specs SMC est NON CAUSAL. Le pivot futur n'est confirmé qu'après
# `swing_filter_length` barres droites, ce qui veut dire que pour qu'il existe,
# le prix DOIT y atteindre. Utiliser ce niveau comme TP = leak structurel
# (le TP est garanti par construction).
#
# Solution causale : on cible le dernier pivot CONFIRMÉ AVANT le fill, dans la
# direction "naturelle" du trade. Pour un LONG, on vise le dernier swing high
# strictement au-dessus de l'entry. Pour un SHORT, le dernier swing low en
# dessous. Si non trouvé → fallback R/R fixe.

def _last_confirmed_swing_target_before_fill(
    df: pd.DataFrame,
    fill_bar: int,
    direction: str,
    entry: float,
    swing_filter_length: int,
) -> Optional[float]:
    """
    Retourne le dernier swing HH (long) / LL (short) CONFIRMÉ strictement
    avant `fill_bar`, et dont le niveau est dans la direction du trade
    (au-dessus de l'entry pour long, en-dessous pour short).

    Causalité stricte : pivot_idx + swing_filter_length ≤ fill_bar.
    """
    if fill_bar - swing_filter_length < swing_filter_length:
        return None

    highs = df["high"].values
    lows  = df["low"].values

    # On scanne uniquement les indices ≤ fill_bar - swing_filter_length
    max_allowed = fill_bar - swing_filter_length

    if direction == "long":
        p_highs = _find_pivots(highs[: max_allowed + 1], swing_filter_length, "high")
        if not p_highs:
            return None
        # Cherche le dernier pivot high au-dessus de entry (target naturel)
        for p in reversed(p_highs):
            if highs[p] > entry:
                return float(highs[p])
        return None
    else:
        p_lows = _find_pivots(lows[: max_allowed + 1], swing_filter_length, "low")
        if not p_lows:
            return None
        for p in reversed(p_lows):
            if lows[p] < entry:
                return float(lows[p])
        return None


# ═════════════════════════════════════════════════════════════════════════════
# Backtest principal
# ═════════════════════════════════════════════════════════════════════════════

def run_backtest(
    df_15m: pd.DataFrame,
    ticker: str,
    tf=None,
    params: Optional[dict] = None,
    topstep_guard: bool = True,
) -> pd.DataFrame:
    """
    Backtest SMC v1 sur df_15m (M15).

    Schéma de colonnes (standard core/optimizer.py + tagging SMC) :
      date, dir, entry, sl, tp, sl_dist, tp_dist, rr, n_ct, result, pnl,
      fill_time, exit_time, exit, regime,
      zone_type, zone_age_bars, zone_width_atr,
      distance_to_swing_mid_pct, prior_BOS_within_50_bars, hour_NY,
      adx_at_entry, atr_pct_at_entry, tp_type, is_macro_day,
      pnl_gross
    """
    p = params or {}
    swing_filter_length = int(p.get("swing_filter_length", SMC_SWING_FILTER_LENGTH))
    zone_max_age_bars   = int(p.get("zone_max_age_bars",   SMC_ZONE_MAX_AGE_BARS))
    eqh_eql_threshold   = float(p.get("eqh_eql_threshold", SMC_EQH_EQL_THRESHOLD))
    tp_fallback_rr      = float(p.get("tp_fallback_rr",    SMC_TP_FALLBACK_RR))
    tp_fallback_bars    = int(p.get("tp_fallback_bars",    SMC_TP_FALLBACK_BARS))

    instr      = INSTRUMENTS[ticker]
    tick_size  = instr["tick_size"]
    pt_value   = instr["dollar_per_point"]
    slip_ticks = SLIPPAGE_TICKS_PER_TICKER.get(ticker, 1)
    slip_px    = slip_ticks * tick_size
    sl_buffer  = SMC_SL_BUFFER_TICKS * tick_size

    # Préparation indicateurs (sans look-ahead)
    df = df_15m.copy()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df["atr"]     = _compute_atr(df, SMC_ATR_PERIOD)
    df["adx"]     = _compute_adx(df, SMC_ADX_PERIOD)
    df["atr_pct"] = df["atr"] / df["close"] * 100.0
    df["atr_pct_rank"] = df["atr"].rolling(window=20 * 24, min_periods=50).rank(pct=True)

    trades: List[Dict] = []
    daily_count: Dict[str, int] = {}

    # Pré-détection des zones sur toute la série pour éviter recomputation
    # par barre — calcul O(N) avec accumulation, mais on filtre causalement
    # à chaque barre via available_from.
    all_zones = _detect_zones_causal(
        df=df,
        current_bar=len(df) - 1,
        pivot_length_internal=SMC_PIVOT_LENGTH_INTERNAL,
        pivot_length_swing=SMC_PIVOT_LENGTH_SWING,
        pivot_length_eqhl=SMC_PIVOT_LENGTH_EQHL,
        eqh_eql_threshold=eqh_eql_threshold,
        fvg_min_gap_ticks=SMC_FVG_MIN_GAP_TICKS,
        tick_size=tick_size,
        atr_series=df["atr"],
    )

    # Pour suivi des trades déjà placés (1 entrée par barre M15)
    already_traded_bars: set = set()

    # Risk tracker pour topstep_guard
    cum_pnl = peak_pnl = 0.0

    warmup = max(SMC_PIVOT_LENGTH_SWING + 10, SMC_ATR_PERIOD + 10, 50)

    for i in range(warmup, len(df)):
        bar  = df.iloc[i]
        prev = df.iloc[i - 1]

        # Filtre temporel NY (DST-aware)
        ts_ny = df.index[i].tz_convert(_NY)
        h_start, m_start = SMC_SESSION_START_NY
        h_end,   m_end   = SMC_SESSION_END_NY
        # Comparaison par minutes pour gérer 9h30 correctement
        ts_min  = ts_ny.hour * 60 + ts_ny.minute
        start_min = h_start * 60 + m_start
        end_min   = h_end   * 60 + m_end
        if not (start_min <= ts_min < end_min):
            continue
        if ts_ny.weekday() >= 5:
            continue

        date_str = ts_ny.date().isoformat()

        # Filtre macro
        is_macro = date_str in MACRO_EVENT_DATES
        if SMC_EXCLUDE_MACRO_DAYS and is_macro:
            continue

        # Max trades / jour
        if daily_count.get(date_str, 0) >= SMC_MAX_TRADES_PER_DAY:
            continue

        # 1 entrée par barre M15
        if i in already_traded_bars:
            continue

        # ── Filtrer les zones actives à i ─────────────────────────────────
        active_zones = _filter_active_zones(
            zones=all_zones,
            df=df,
            current_bar=i,
            zone_max_age_bars=zone_max_age_bars,
        )
        if not active_zones:
            continue

        # ── Filtre Premium/Discount : on a besoin du dernier swing range ──
        swing_range = _last_confirmed_swing_range(
            df=df,
            current_bar=i,
            pivot_length=swing_filter_length,
        )
        if swing_range is None:
            continue
        swing_low, swing_high = swing_range
        mid = (swing_low + swing_high) / 2.0

        # ── Trouver les zones touchables à la barre i ────────────────────
        # Pour LONG : zone bullish, on entre LIMIT au top (bord proche du dessus)
        # quand low_bar <= top. Slippage worst-case si gap d'ouverture.
        # Pour SHORT : zone bearish, on entre LIMIT au bottom quand high_bar >= bottom.
        # On respecte la priorité multi-zone.
        candidates: List[Dict] = []
        bar_open = bar["open"]
        bar_high = bar["high"]
        bar_low  = bar["low"]

        for z in active_zones:
            # Pas de fill rétroactif : la zone doit être "atteinte" à la barre i,
            # pas avant. Si elle a déjà été touchée à barre antérieure, on saute.
            # Vérif rapide : a-t-elle été touchée dans (available_from, i-1] ?
            already_touched = False
            for j in range(z.available_from, i):
                hj = df["high"].iloc[j]
                lj = df["low"].iloc[j]
                if z.direction == "long" and lj <= z.top:
                    already_touched = True
                    break
                if z.direction == "short" and hj >= z.bottom:
                    already_touched = True
                    break
            if already_touched:
                continue

            if z.direction == "long":
                # Entry LIMIT au top de la zone (bord proche, le prix vient du dessus)
                limit_px = z.top
                # Touch à la barre i ?
                if bar_low <= limit_px:
                    # Gap d'ouverture : si bar_open < limit_px → on est déjà
                    # passé en dessous de la zone à l'ouverture. C'est un
                    # gap au-delà du limit. Conformément à la spec :
                    # "Si gap au-delà → zone non touchée (skip, pas de fill
                    # rétroactif)". MAIS si bar_open ∈ [bottom, top] : fill
                    # à `open + slippage worst-case`.
                    if bar_open < z.bottom:
                        # gap complet au-delà de la zone → skip
                        continue
                    elif bar_open <= z.top:
                        # open dans la zone → fill worst-case (open + slip)
                        fill_px = bar_open + slip_px
                    else:
                        # open au-dessus de la zone, prix redescend touch limit
                        fill_px = limit_px + slip_px
                    # Filtre Premium/Discount : long autorisé si fill ≤ mid
                    if fill_px > mid:
                        continue
                    candidates.append({"zone": z, "fill_px": fill_px})
            else:  # short
                limit_px = z.bottom
                if bar_high >= limit_px:
                    if bar_open > z.top:
                        # gap complet au-delà → skip
                        continue
                    elif bar_open >= z.bottom:
                        # open dans la zone → worst-case
                        fill_px = bar_open - slip_px
                    else:
                        # open en dessous, prix monte touch limit
                        fill_px = limit_px - slip_px
                    if fill_px < mid:
                        continue
                    candidates.append({"zone": z, "fill_px": fill_px})

        if not candidates:
            continue

        # ── Appliquer la priorité multi-zone : OB_swing > OB_internal > FVG > EQH/EQL
        priority_map = {t: idx for idx, t in enumerate(SMC_ZONE_PRIORITY)}
        candidates.sort(key=lambda c: priority_map.get(c["zone"].zone_type, 99))
        chosen = candidates[0]
        z      = chosen["zone"]
        entry  = chosen["fill_px"]
        direction = z.direction

        # ── SL / TP ───────────────────────────────────────────────────────
        if direction == "long":
            sl = z.bottom - sl_buffer
            sl_dist = entry - sl
        else:
            sl = z.top + sl_buffer
            sl_dist = sl - entry
        if sl_dist <= 0:
            continue

        # TP : dernier swing HH/LL CONFIRMÉ avant le fill (causal)
        # Le pattern "next swing après fill" est un look-ahead structurel
        # (cf. note dans _last_confirmed_swing_target_before_fill).
        last_swing_target = _last_confirmed_swing_target_before_fill(
            df=df,
            fill_bar=i,
            direction=direction,
            entry=entry,
            swing_filter_length=swing_filter_length,
        )
        if last_swing_target is not None:
            if direction == "long":
                tp = last_swing_target
                tp_dist = tp - entry
            else:
                tp = last_swing_target
                tp_dist = entry - tp
            tp_type = "swing"
            # Sécurité : si la cible est trop proche (rare car filtre direction
            # déjà appliqué), fallback RR
            if tp_dist < sl_dist * 0.5:
                if direction == "long":
                    tp = entry + tp_fallback_rr * sl_dist
                else:
                    tp = entry - tp_fallback_rr * sl_dist
                tp_dist = tp_fallback_rr * sl_dist
                tp_type = "rr"
        else:
            if direction == "long":
                tp = entry + tp_fallback_rr * sl_dist
            else:
                tp = entry - tp_fallback_rr * sl_dist
            tp_dist = tp_fallback_rr * sl_dist
            tp_type = "rr"

        rr = tp_dist / sl_dist if sl_dist > 0 else 0.0

        # ── Sizing ────────────────────────────────────────────────────────
        risk_per_ct = (sl_dist + slip_px) * pt_value
        if risk_per_ct <= 0:
            continue
        n_ct = max(1, int(RISK_PER_TRADE_USD / risk_per_ct))
        n_ct = min(n_ct, SMC_MAX_CONTRACTS_PER_TRADE)

        # Garde-fou Topstep
        if topstep_guard:
            allowed, _ = trade_allowed(day_pnl=0.0, cum_pnl=cum_pnl, peak_pnl=peak_pnl)
            if not allowed:
                continue

        # ── Simulation exit (fill confirmé à la barre i) ─────────────────
        # Vérifier si SL/TP touché DANS la barre i (rare, mais possible)
        fill_time = df.index[i]
        result    = None
        exit_px   = None
        exit_time = None

        # SL/TP même barre = SL prioritaire
        if direction == "long":
            sl_hit_now = (bar_low <= sl)
            tp_hit_now = (bar_high >= tp)
        else:
            sl_hit_now = (bar_high >= sl)
            tp_hit_now = (bar_low  <= tp)

        if sl_hit_now and tp_hit_now:
            result, exit_px, exit_time = "SL", sl, df.index[i]
        elif sl_hit_now:
            result, exit_px, exit_time = "SL", sl, df.index[i]
        elif tp_hit_now:
            result, exit_px, exit_time = "TP", tp, df.index[i]
        else:
            # Suivre les barres suivantes (sans limite stricte, mais raisonnable)
            max_hold = tp_fallback_bars * 2  # plafond souple
            found = False
            for k in range(i + 1, min(i + 1 + max_hold, len(df))):
                fb = df.iloc[k]
                if direction == "long":
                    sl_hit = fb["low"]  <= sl
                    tp_hit = fb["high"] >= tp
                else:
                    sl_hit = fb["high"] >= sl
                    tp_hit = fb["low"]  <= tp
                if sl_hit and tp_hit:
                    result, exit_px, exit_time = "SL", sl, df.index[k]
                    found = True
                    break
                if sl_hit:
                    result, exit_px, exit_time = "SL", sl, df.index[k]
                    found = True
                    break
                if tp_hit:
                    result, exit_px, exit_time = "TP", tp, df.index[k]
                    found = True
                    break
            if not found:
                # Time-out → close à la dernière barre du hold
                last_k = min(i + max_hold, len(df) - 1)
                result    = "TE"
                exit_px   = float(df.iloc[last_k]["close"])
                exit_time = df.index[last_k]

        # ── P&L ───────────────────────────────────────────────────────────
        raw_pnl   = (exit_px - entry) * (1 if direction == "long" else -1)
        pnl_gross = raw_pnl * n_ct * pt_value
        # Slippage déjà appliqué à l'entrée. On applique le slippage à la sortie.
        slip_cost = slip_px * n_ct * pt_value   # uniquement sortie ici (entrée déjà dans entry)
        comm_cost = COMMISSION_RT_PER_CONTRACT * n_ct
        pnl       = pnl_gross - slip_cost - comm_cost

        # ── Tagging contextuel (causalité OK : tout à i ou avant) ────────
        zone_age = i - z.created_at_bar
        atr_prev = df["atr"].iloc[i - 1] if pd.notna(df["atr"].iloc[i - 1]) else np.nan
        adx_prev = df["adx"].iloc[i - 1] if pd.notna(df["adx"].iloc[i - 1]) else np.nan
        atr_pct_prev = df["atr_pct"].iloc[i - 1] if pd.notna(df["atr_pct"].iloc[i - 1]) else np.nan

        zone_width_atr = (z.top - z.bottom) / atr_prev if np.isfinite(atr_prev) and atr_prev > 0 else np.nan
        distance_to_swing_mid_pct = ((entry - mid) / mid * 100.0) if mid > 0 else np.nan

        # prior BOS : break of last confirmed swing high/low (length=swing_filter_length)
        # dans les 50 dernières barres avant i. Utilise pivots confirmés.
        prior_BOS = _prior_BOS_within(
            df=df,
            current_bar=i,
            direction=direction,
            pivot_length=swing_filter_length,
            window=50,
        )

        # Régime (pour stress tests PHASE 5)
        if pd.notna(adx_prev) and adx_prev > 25:
            regime = "trending"
        elif pd.notna(adx_prev) and adx_prev < 20:
            regime = "ranging"
        else:
            regime = "neutral"

        trades.append({
            # ── Schéma standard (obligatoire) ──
            "date":         date_str,
            "dir":          direction,
            "entry":        round(float(entry), 4),
            "sl":           round(float(sl), 4),
            "tp":           round(float(tp), 4),
            "sl_dist":      round(float(sl_dist), 4),
            "tp_dist":      round(float(tp_dist), 4),
            "rr":           round(float(rr), 2),
            "n_ct":         int(n_ct),
            "result":       result,
            "pnl":          round(float(pnl), 2),
            "fill_time":    fill_time,
            "exit_time":    exit_time,
            "exit":         round(float(exit_px), 4),
            "regime":       regime,
            # ── Must-have @quant catalog (figées à fill) ──
            "zone_type":    z.zone_type,
            "zone_age_bars": int(zone_age),
            "zone_width_atr": round(float(zone_width_atr), 4) if np.isfinite(zone_width_atr) else None,
            # ── Tagging contextuel (causal à fill, utile pour @quant discover) ──
            "distance_to_swing_mid_pct": round(float(distance_to_swing_mid_pct), 4) if np.isfinite(distance_to_swing_mid_pct) else None,
            "prior_BOS_within_50_bars": bool(prior_BOS),
            "hour_NY":      int(ts_ny.hour),
            "adx_at_entry": round(float(adx_prev), 2) if np.isfinite(adx_prev) else None,
            "atr_pct_at_entry": round(float(atr_pct_prev), 4) if np.isfinite(atr_pct_prev) else None,
            "tp_type":      tp_type,
            "is_macro_day": bool(is_macro),
            # ── Colonne optionnelle ──
            "pnl_gross":    round(float(pnl_gross), 2),
        })

        # Compteurs
        daily_count[date_str] = daily_count.get(date_str, 0) + 1
        already_traded_bars.add(i)

        # Mise à jour du tracker risk
        cum_pnl += pnl
        if cum_pnl > peak_pnl:
            peak_pnl = cum_pnl

    cols = [
        "date","dir","entry","sl","tp","sl_dist","tp_dist","rr","n_ct",
        "result","pnl","fill_time","exit_time","exit","regime",
        "zone_type","zone_age_bars","zone_width_atr",
        "distance_to_swing_mid_pct","prior_BOS_within_50_bars","hour_NY",
        "adx_at_entry","atr_pct_at_entry","tp_type","is_macro_day",
        "pnl_gross",
    ]
    if trades:
        return pd.DataFrame(trades)[cols]
    return pd.DataFrame(columns=cols)


def _prior_BOS_within(
    df: pd.DataFrame,
    current_bar: int,
    direction: str,
    pivot_length: int,
    window: int = 50,
) -> bool:
    """
    Vrai si un Break of Structure (cassure du dernier swing high/low confirmé
    dans la direction du trade) s'est produit dans les `window` dernières barres
    avant `current_bar`.

    Causalité : on utilise uniquement des pivots confirmés AVANT current_bar.
    """
    df_past = df.iloc[: current_bar + 1]
    n = len(df_past)
    if n < 2 * pivot_length + window + 2:
        return False
    highs = df_past["high"].values
    lows  = df_past["low"].values
    closes = df_past["close"].values

    max_pivot_idx = current_bar - pivot_length
    if max_pivot_idx < pivot_length:
        return False

    if direction == "long":
        p_highs = _find_pivots(highs[: max_pivot_idx + 1], pivot_length, "high")
        if not p_highs:
            return False
        last_h = p_highs[-1]
        # BOS long = close au-dessus du swing high récent dans les `window` barres
        start = max(last_h + pivot_length, current_bar - window)
        for k in range(start, current_bar + 1):
            if closes[k] > highs[last_h]:
                return True
        return False
    else:
        p_lows = _find_pivots(lows[: max_pivot_idx + 1], pivot_length, "low")
        if not p_lows:
            return False
        last_l = p_lows[-1]
        start = max(last_l + pivot_length, current_bar - window)
        for k in range(start, current_bar + 1):
            if closes[k] < lows[last_l]:
                return True
        return False


# ═════════════════════════════════════════════════════════════════════════════
# Visualisation par jour (pour --plot)
# ═════════════════════════════════════════════════════════════════════════════

def plot_day(
    df_15m: pd.DataFrame,
    ticker: str,
    date_str: str,
    day_trades: list,
    output_path: str,
) -> None:
    """Chart M15 d'un jour : chandeliers, zones SMC, fills et exits."""
    if df_15m.index.tz is None:
        ts_idx = df_15m.index.tz_localize("UTC").tz_convert(_NY)
    else:
        ts_idx = df_15m.index.tz_convert(_NY)
    mask = ts_idx.normalize() == pd.Timestamp(date_str, tz=_NY).normalize()
    day  = df_15m[mask.values].copy()
    if day.empty:
        return

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(16, 10), gridspec_kw={"height_ratios": [3, 1]}, sharex=True
    )
    x = range(len(day))

    # Chandeliers
    for i, (_, row) in enumerate(day.iterrows()):
        color = "#26a69a" if row["close"] >= row["open"] else "#ef5350"
        ax1.plot([i, i], [row["low"], row["high"]], color=color, lw=0.8)
        ax1.bar(i, abs(row["close"] - row["open"]),
                bottom=min(row["open"], row["close"]),
                color=color, width=0.6, alpha=0.9)

    day_pnl = 0.0
    for trade in day_trades:
        if trade.get("result") == "NOT_FILLED":
            continue
        try:
            fill_idx = list(day.index).index(trade["fill_time"])
        except (ValueError, KeyError):
            continue
        try:
            exit_idx = list(day.index).index(trade["exit_time"])
        except (ValueError, KeyError):
            exit_idx = fill_idx

        color  = "#00c853" if trade["dir"] == "long" else "#d50000"
        marker = "^"       if trade["dir"] == "long" else "v"
        ax1.scatter(fill_idx, trade["entry"], marker=marker, color=color, s=150, zorder=5)
        ax1.scatter(exit_idx, trade["exit"],  marker="x", color="white", s=120, zorder=5)
        ax1.axhline(trade["sl"], color="#ef5350", ls="--", lw=0.8, alpha=0.7)
        ax1.axhline(trade["tp"], color="#26a69a", ls="--", lw=0.8, alpha=0.7)
        day_pnl += trade.get("pnl", 0.0)

    if "volume" in day.columns:
        ax2.bar(x, day["volume"].values, color="#7986cb", alpha=0.6)
        ax2.set_ylabel("Volume", fontsize=9)

    ax1.set_title(
        f"{ticker} — {date_str} (SMC v1)  |  P&L net jour : {day_pnl:+.0f} $",
        fontsize=12, fontweight="bold",
    )
    ax1.set_facecolor("#1a1a2e")
    ax2.set_facecolor("#1a1a2e")
    fig.patch.set_facecolor("#13131f")
    ax1.tick_params(colors="white")
    ax2.tick_params(colors="white")

    plt.tight_layout()
    plt.savefig(output_path, dpi=120, bbox_inches="tight")
    plt.close(fig)
