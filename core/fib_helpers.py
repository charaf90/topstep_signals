"""
Helpers de calcul Fibonacci — partagés par toutes les stratégies Fib.

Module pur, sans logique de stratégie ni état. Contient :
  • compute_ema / compute_atr / compute_adx   — indicateurs standards
  • detect_pivots                             — pivots locaux left/right
  • detect_trend                              — BULL / BEAR / RANGE selon EMA+ADX
  • find_last_impulse                         — dernière impulse alignée tendance
  • build_signal                              — construction du dict trade
                                                (sizing risque dollar fixe)

Extraction réalisée 2026-05-19 lors du nettoyage post-fib-v4 : ce module
remplace les helpers historiquement présents dans `core/strategy_fib.py`
(fib-v3 supprimée). `core/strategy_fib_v4.py` importe ses helpers d'ici.

Conventions :
  • Toutes les fonctions ont une signature explicite (paramètres → retour)
  • Aucun side-effect ; reproductibilité garantie
  • Compatibles indices et matières premières (sizing via INSTRUMENTS)
"""

import numpy as np
import pandas as pd

from config import INSTRUMENTS, RISK_PER_TRADE_USD

# ═════════════════════════════════════════════════════════════════════════════
# Indicateurs (implémentation standalone — pas de TA-Lib pour rester sans
# dépendance)
# ═════════════════════════════════════════════════════════════════════════════


def compute_ema(series: pd.Series, period: int) -> pd.Series:
    """EMA classique adjust=False (formule TradingView/MT5)."""
    return series.ewm(span=period, adjust=False).mean()


def compute_atr(df: pd.DataFrame, period: int) -> pd.Series:
    """ATR par moyenne simple sur le True Range — cohérent avec core/opr.py."""
    high = df["high"]
    low = df["low"]
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period).mean()


def compute_adx(df: pd.DataFrame, period: int) -> pd.Series:
    """ADX standard (Wilder simplifié avec rolling mean)."""
    high = df["high"]
    low = df["low"]
    prev_close = df["close"].shift(1)

    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    atr = tr.rolling(period).mean()
    plus_di = 100.0 * (plus_dm.rolling(period).mean() / atr.replace(0, np.nan))
    minus_di = 100.0 * (minus_dm.rolling(period).mean() / atr.replace(0, np.nan))

    di_sum = plus_di + minus_di
    dx = 100.0 * (plus_di - minus_di).abs() / di_sum.replace(0, np.nan)
    return dx.rolling(period).mean()


# ═════════════════════════════════════════════════════════════════════════════
# Détection des pivots (méthode left/right)
# ═════════════════════════════════════════════════════════════════════════════


def detect_pivots(df: pd.DataFrame, left: int, right: int) -> tuple[list[int], list[int]]:
    """
    Pivots high (max local) et low (min local).
    Confirmation : pivot[i] connu seulement à index i+right.
    """
    highs = df["high"].values
    lows = df["low"].values
    n = len(df)
    pivot_highs: list[int] = []
    pivot_lows: list[int] = []
    for i in range(left, n - right):
        h_window = highs[i - left : i + right + 1]
        l_window = lows[i - left : i + right + 1]
        if highs[i] == h_window.max() and highs[i] > highs[i - 1] and highs[i] > highs[i + 1]:
            pivot_highs.append(i)
        if lows[i] == l_window.min() and lows[i] < lows[i - 1] and lows[i] < lows[i + 1]:
            pivot_lows.append(i)
    return pivot_highs, pivot_lows


# ═════════════════════════════════════════════════════════════════════════════
# Détection de tendance (EMA + ADX)
# ═════════════════════════════════════════════════════════════════════════════


def detect_trend(
    close: float, ema_fast: float, ema_slow: float, adx: float, adx_threshold: float
) -> str:
    """
    Retourne 'BULL', 'BEAR' ou 'RANGE'.
    BULL  : close > EMA_fast > EMA_slow ET ADX > seuil
    BEAR  : close < EMA_fast < EMA_slow ET ADX > seuil
    RANGE : sinon
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
# Détection de l'impulse (pivots récents + filtres ATR/durée/tendance)
# ═════════════════════════════════════════════════════════════════════════════


def find_last_impulse(
    df: pd.DataFrame,
    current_idx: int,
    pivot_highs: list[int],
    pivot_lows: list[int],
    atr_series: pd.Series,
    trend: str,
    pivot_right: int,
    min_impulse_atr: float,
    max_impulse_bars: int,
    impulse_lookback: int,
    fib_level: float = 0.50,
) -> dict | None:
    """
    Cherche la dernière impulse VALIDE alignée avec la tendance.

    `fib_level` = niveau de retracement Fibonacci pour le calcul de l'entrée.
    Valeurs typiques : 0.382, 0.50, 0.618. La clé "fib_50" du dict retourné
    contient le prix au niveau choisi (nom hérité de la version originale).
    """
    if trend not in ("BULL", "BEAR"):
        return None

    confirmation_offset = pivot_right + 1

    if trend == "BULL":
        confirmed_highs = [
            p
            for p in pivot_highs
            if p + confirmation_offset <= current_idx and current_idx - p <= impulse_lookback
        ]
        if not confirmed_highs:
            return None
        ph_idx = max(confirmed_highs)
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
        if impulse_size < min_impulse_atr * atr_at_pivot:
            return None
        if impulse_bars > max_impulse_bars:
            return None
        # Niveau Fib paramétrable (38.2 / 50 / 61.8…). 0.5 = mid, 0.618 = retracement profond.
        fib_price = swing_low + fib_level * impulse_size
        return {
            "direction": "long",
            "pivot_low_idx": int(pl_idx),
            "pivot_high_idx": int(ph_idx),
            "swing_low": swing_low,
            "swing_high": swing_high,
            "impulse_size": float(impulse_size),
            "impulse_bars": int(impulse_bars),
            "fib_50": float(
                fib_price
            ),  # clé conservée pour compat — contient le prix au niveau choisi
            "fib_level": float(fib_level),
            "atr_at_pivot": float(atr_at_pivot),
            "confirm_idx": int(ph_idx + pivot_right),
        }

    # BEAR
    confirmed_lows = [
        p
        for p in pivot_lows
        if p + confirmation_offset <= current_idx and current_idx - p <= impulse_lookback
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
    if impulse_size < min_impulse_atr * atr_at_pivot:
        return None
    if impulse_bars > max_impulse_bars:
        return None
    fib_price = swing_high - fib_level * impulse_size
    return {
        "direction": "short",
        "pivot_high_idx": int(ph_idx),
        "pivot_low_idx": int(pl_idx),
        "swing_low": swing_low,
        "swing_high": swing_high,
        "impulse_size": float(impulse_size),
        "impulse_bars": int(impulse_bars),
        "fib_50": float(fib_price),
        "fib_level": float(fib_level),
        "atr_at_pivot": float(atr_at_pivot),
        "confirm_idx": int(pl_idx + pivot_right),
    }


# ═════════════════════════════════════════════════════════════════════════════
# Construction du signal trade (sizing risque dollar fixe)
# ═════════════════════════════════════════════════════════════════════════════


def build_signal(
    impulse: dict,
    current_atr: float,
    ticker: str,
    sl_mult: float,
    tp_mult: float,
) -> dict | None:
    """Construit le dict signal — None si sizing impossible."""
    if current_atr is None or pd.isna(current_atr) or current_atr <= 0:
        return None
    tick = INSTRUMENTS[ticker]["tick_size"]

    def _tick(price):
        return round(round(price / tick) * tick, 10)

    entry = _tick(impulse["fib_50"])
    sl_dist = sl_mult * current_atr
    tp_dist = tp_mult * current_atr
    if impulse["direction"] == "long":
        sl_price = _tick(entry - sl_dist)
        tp_price = _tick(entry + tp_dist)
    else:
        sl_price = _tick(entry + sl_dist)
        tp_price = _tick(entry - tp_dist)
    dpp = INSTRUMENTS[ticker]["dollar_per_point"]
    sl_dist = abs(entry - sl_price)
    tp_dist = abs(entry - tp_price)
    n_ct = int(RISK_PER_TRADE_USD / (sl_dist * dpp)) if sl_dist > 0 else 0
    if n_ct <= 0:
        return None
    return {
        "ticker": ticker,
        "strategy": "FIB",
        "direction": impulse["direction"],
        "entry": float(entry),
        "sl": float(sl_price),
        "tp": float(tp_price),
        "sl_dist": float(sl_dist),
        "tp_dist": float(tp_dist),
        "n_ct": int(n_ct),
        "risk": float(n_ct * sl_dist * dpp),
        "rr": float(tp_dist / sl_dist),
        "swing_low": impulse["swing_low"],
        "swing_high": impulse["swing_high"],
        "impulse_size": impulse["impulse_size"],
        "impulse_bars": impulse["impulse_bars"],
        "atr": float(current_atr),
        "pivot_low_idx": impulse["pivot_low_idx"],
        "pivot_high_idx": impulse["pivot_high_idx"],
        # Champs neutres pour cohérence avec les schémas signal composite/OPR
        "quality": 0.0,
        "composite": 0.0,
        "alignment": 0.0,
        "atr_ratio": 0.0,
        "n_tf": 1,
        "touches": 0,
        "regime": "FIB",
        "zone_low": float(impulse["swing_low"]),
        "zone_high": float(impulse["swing_high"]),
        "tp_type": "atr",
    }
