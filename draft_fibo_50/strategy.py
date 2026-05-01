"""
Cœur logique de la stratégie Fibonacci 50% retracement.

Module pur (pas d'I/O, pas d'effet de bord). Toutes les fonctions prennent
des DataFrames pandas et retournent des structures simples (dict, list, tuple).
"""

from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


# ═════════════════════════════════════════════════════════════════════════════
# Indicateurs techniques (implémentation maison — pas de TA-Lib pour rester sans dépendance)
# ═════════════════════════════════════════════════════════════════════════════

def compute_ema(series: pd.Series, period: int) -> pd.Series:
    """EMA exponentielle classique (adjust=False, identique à la formule TradingView)."""
    return series.ewm(span=period, adjust=False).mean()


def compute_atr(df: pd.DataFrame, period: int) -> pd.Series:
    """
    ATR par moyenne simple sur le True Range.
    Pour la cohérence avec le reste du projet (cf. core/opr.py), on utilise
    une rolling mean au lieu de la version Wilder (smoothed).
    """
    high = df["high"]
    low = df["low"]
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def compute_adx(df: pd.DataFrame, period: int) -> pd.Series:
    """
    ADX standard (Wilder simplifié avec rolling mean).

    ADX = rolling_mean(DX, period) où :
      DX = 100 × |+DI − −DI| / (+DI + −DI)
      ±DI = 100 × rolling_mean(±DM, period) / ATR(period)
      +DM = max(high − high[-1], 0) si > −DM sinon 0
      −DM = max(low[-1] − low,  0) si > +DM sinon 0
    """
    high = df["high"]
    low = df["low"]
    prev_close = df["close"].shift(1)

    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    atr = tr.rolling(period).mean()
    plus_di = 100.0 * (plus_dm.rolling(period).mean() / atr.replace(0, np.nan))
    minus_di = 100.0 * (minus_dm.rolling(period).mean() / atr.replace(0, np.nan))

    di_sum = plus_di + minus_di
    dx = 100.0 * (plus_di - minus_di).abs() / di_sum.replace(0, np.nan)
    adx = dx.rolling(period).mean()
    return adx


# ═════════════════════════════════════════════════════════════════════════════
# Détection des pivots (méthode left/right)
# ═════════════════════════════════════════════════════════════════════════════

def detect_pivots(
    df: pd.DataFrame, left: int, right: int
) -> Tuple[List[int], List[int]]:
    """
    Détecte les pivots high (max local) et low (min local) par méthode left/right.

    Un pivot à l'index i est confirmé si :
      - high[i]  = max(high[i-left : i+right+1])  (et strictement > voisins immédiats)
      - low[i]   = min(low[i-left : i+right+1])   (idem côté bas)

    Retourne (pivot_high_indices, pivot_low_indices) — listes triées d'indices.

    NB : un pivot ne peut être détecté qu'après `right` bougies de confirmation.
    En backtest live, on considère le pivot comme connu à partir de l'index `i + right`.
    """
    highs = df["high"].values
    lows = df["low"].values
    n = len(df)

    pivot_highs: List[int] = []
    pivot_lows: List[int] = []

    for i in range(left, n - right):
        h_window = highs[i - left:i + right + 1]
        l_window = lows[i - left:i + right + 1]

        # Pivot high : strict max sur la fenêtre, plus haut que les voisins immédiats
        if highs[i] == h_window.max() and highs[i] > highs[i - 1] and highs[i] > highs[i + 1]:
            pivot_highs.append(i)
        # Pivot low : strict min sur la fenêtre
        if lows[i] == l_window.min() and lows[i] < lows[i - 1] and lows[i] < lows[i + 1]:
            pivot_lows.append(i)

    return pivot_highs, pivot_lows


# ═════════════════════════════════════════════════════════════════════════════
# Détection de tendance multi-critères (EMA + ADX)
# ═════════════════════════════════════════════════════════════════════════════

def detect_trend(close: float, ema_fast: float, ema_slow: float,
                 adx: float, adx_threshold: float) -> str:
    """
    Retourne 'BULL', 'BEAR' ou 'RANGE' selon la combinaison :
      BULL  : close > EMA_fast > EMA_slow ET ADX > seuil
      BEAR  : close < EMA_fast < EMA_slow ET ADX > seuil
      RANGE : tout le reste (ADX faible ou EMAs non alignées)
    """
    if any(np.isnan(x) for x in (ema_fast, ema_slow, adx)):
        return "RANGE"
    if adx < adx_threshold:
        return "RANGE"
    if close > ema_fast > ema_slow:
        return "BULL"
    if close < ema_fast < ema_slow:
        return "BEAR"
    return "RANGE"


# ═════════════════════════════════════════════════════════════════════════════
# Détection de l'impulse — pivots récents + filtres ATR / durée
# ═════════════════════════════════════════════════════════════════════════════

def find_last_impulse(
    df: pd.DataFrame,
    current_idx: int,
    pivot_highs: List[int],
    pivot_lows: List[int],
    atr_series: pd.Series,
    trend: str,
    config: Dict,
) -> Optional[Dict]:
    """
    Cherche la dernière impulse VALIDE qui se termine à un pivot confirmé
    avant `current_idx`.

    Pour BULL : impulse haussière = pivot_low → pivot_high récent
    Pour BEAR : impulse baissière = pivot_high → pivot_low récent

    Validation :
      - Pivot final confirmé : `pivot_idx + right <= current_idx`
      - Distance ≥ MIN_IMPULSE_ATR × ATR(au moment du pivot final)
      - Durée ≤ MAX_IMPULSE_BARS
      - Recherche limitée aux IMPULSE_LOOKBACK dernières bougies

    Retourne un dict décrivant l'impulse, ou None si aucun candidat valide.
    """
    if trend not in ("BULL", "BEAR"):
        return None

    right = config["PIVOT_RIGHT"]
    min_atr_mult = config["MIN_IMPULSE_ATR"]
    max_bars = config["MAX_IMPULSE_BARS"]
    lookback = config["IMPULSE_LOOKBACK"]

    # Un pivot est utilisable seulement après `right + 1` bougies (offset de confirmation)
    confirmation_offset = right + 1

    if trend == "BULL":
        # Dernier pivot_high CONFIRMÉ dans la fenêtre lookback
        confirmed_highs = [
            p for p in pivot_highs
            if p + confirmation_offset <= current_idx
            and current_idx - p <= lookback
        ]
        if not confirmed_highs:
            return None
        ph_idx = max(confirmed_highs)

        # Pivot low PRÉCÉDENT immédiat (pour former l'impulse low → high)
        prior_lows = [p for p in pivot_lows if p < ph_idx]
        if not prior_lows:
            return None
        pl_idx = max(prior_lows)

        swing_high = float(df["high"].iloc[ph_idx])
        swing_low = float(df["low"].iloc[pl_idx])
        impulse_size = swing_high - swing_low
        impulse_bars = ph_idx - pl_idx

        atr_at_pivot = atr_series.iloc[ph_idx]
        if pd.isna(atr_at_pivot) or atr_at_pivot <= 0:
            return None
        if impulse_size < min_atr_mult * atr_at_pivot:
            return None
        if impulse_bars > max_bars:
            return None

        fib_50 = swing_low + 0.5 * impulse_size

        return {
            "direction": "long",
            "pivot_low_idx": int(pl_idx),
            "pivot_high_idx": int(ph_idx),
            "swing_low": swing_low,
            "swing_high": swing_high,
            "impulse_size": float(impulse_size),
            "impulse_bars": int(impulse_bars),
            "fib_50": float(fib_50),
            "atr_at_pivot": float(atr_at_pivot),
            "confirm_idx": int(ph_idx + right),
        }

    # BEAR : dernier pivot_low confirmé, pivot_high précédent
    confirmed_lows = [
        p for p in pivot_lows
        if p + confirmation_offset <= current_idx
        and current_idx - p <= lookback
    ]
    if not confirmed_lows:
        return None
    pl_idx = max(confirmed_lows)

    prior_highs = [p for p in pivot_highs if p < pl_idx]
    if not prior_highs:
        return None
    ph_idx = max(prior_highs)

    swing_high = float(df["high"].iloc[ph_idx])
    swing_low = float(df["low"].iloc[pl_idx])
    impulse_size = swing_high - swing_low
    impulse_bars = pl_idx - ph_idx

    atr_at_pivot = atr_series.iloc[pl_idx]
    if pd.isna(atr_at_pivot) or atr_at_pivot <= 0:
        return None
    if impulse_size < min_atr_mult * atr_at_pivot:
        return None
    if impulse_bars > max_bars:
        return None

    fib_50 = swing_high - 0.5 * impulse_size

    return {
        "direction": "short",
        "pivot_high_idx": int(ph_idx),
        "pivot_low_idx": int(pl_idx),
        "swing_low": swing_low,
        "swing_high": swing_high,
        "impulse_size": float(impulse_size),
        "impulse_bars": int(impulse_bars),
        "fib_50": float(fib_50),
        "atr_at_pivot": float(atr_at_pivot),
        "confirm_idx": int(pl_idx + right),
    }


# ═════════════════════════════════════════════════════════════════════════════
# Construction du signal trade (sizing risque fixe)
# ═════════════════════════════════════════════════════════════════════════════

def build_signal(
    impulse: Dict, current_atr: float, ticker: str,
    config: Dict, instruments: Dict
) -> Optional[Dict]:
    """
    Convertit une impulse + ATR courant en signal exécutable :
      entry = fib_50  |  SL = entry ∓ SL_mult×ATR  |  TP = entry ± TP_mult×ATR
      sizing : n_ct = RISK_PER_TRADE_USD / (sl_dist × $/pt)

    Retourne None si sizing impossible (ATR invalide ou n_ct = 0).
    """
    if current_atr is None or pd.isna(current_atr) or current_atr <= 0:
        return None

    sl_mult = config["SL_ATR_MULT"]
    tp_mult = config["TP_ATR_MULT"]
    risk_usd = config["RISK_PER_TRADE_USD"]

    entry = impulse["fib_50"]
    sl_dist = sl_mult * current_atr
    tp_dist = tp_mult * current_atr

    if impulse["direction"] == "long":
        sl_price = entry - sl_dist
        tp_price = entry + tp_dist
    else:
        sl_price = entry + sl_dist
        tp_price = entry - tp_dist

    dpp = instruments[ticker]["dollar_per_point"]
    n_ct = int(risk_usd / (sl_dist * dpp))
    if n_ct <= 0:
        return None

    return {
        "ticker": ticker,
        "direction": impulse["direction"],
        "entry": float(entry),
        "sl": float(sl_price),
        "tp": float(tp_price),
        "sl_dist": float(sl_dist),
        "tp_dist": float(tp_dist),
        "n_ct": int(n_ct),
        "risk_$": float(n_ct * sl_dist * dpp),
        "rr": float(tp_dist / sl_dist),
        "swing_low": impulse["swing_low"],
        "swing_high": impulse["swing_high"],
        "impulse_size": impulse["impulse_size"],
        "impulse_bars": impulse["impulse_bars"],
        "atr": float(current_atr),
        "pivot_low_idx": impulse["pivot_low_idx"],
        "pivot_high_idx": impulse["pivot_high_idx"],
    }
