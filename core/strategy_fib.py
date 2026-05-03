"""
Stratégie Fibonacci 50% retracement (M15) — version production.

Extraite et adaptée de `draft_fibo_50/` après validation walk-forward.
Logique métier complète (cf. CLAUDE.md → "Stratégie Fibonacci") :

  1. Tendance multi-critères : EMA50/EMA200 alignement + ADX(14) > 20
  2. Détection impulse : pivots left/right=8 + filtres ATR ≥ MIN_IMPULSE_ATR
                        + durée ≤ MAX_IMPULSE_BARS, alignée tendance
  3. Entrée LIMIT au niveau Fibonacci 50% du dernier impulse
  4. SL / TP en multiplicateurs ATR (per-ticker, calibrés walk-forward)
  5. Filtre trigger appliqué à l'armement (per-ticker, calibré walk-forward)
  6. Position fermée au timeout (MAX_HOLD_BARS) si SL/TP non atteint

Le module est INDÉPENDANT de core/strategy.py (composite) et core/opr.py.
"""

from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from config import (
    INSTRUMENTS, RISK_PER_TRADE_USD,
    FIB_ENABLED,
    FIB_ATR_PERIOD, FIB_EMA_FAST_PERIOD, FIB_EMA_SLOW_PERIOD,
    FIB_ADX_PERIOD, FIB_ADX_TREND_THRESHOLD,
    FIB_PIVOT_LEFT, FIB_PIVOT_RIGHT,
    FIB_MIN_IMPULSE_ATR, FIB_MAX_IMPULSE_BARS, FIB_IMPULSE_LOOKBACK,
    FIB_ORDER_TIMEOUT_BARS, FIB_MAX_HOLD_BARS,
    FIB_LEVEL_PER_TICKER,
    FIB_SL_ATR_MULT_PER_TICKER, FIB_TP_ATR_MULT_PER_TICKER,
    FIB_MIN_IMPULSE_ATR_PER_TICKER,
    FIB_TRIGGER_FILTERS_PER_TICKER,
    US_SESSION_START_UTC, US_SESSION_END_UTC,
)


# ═════════════════════════════════════════════════════════════════════════════
# Indicateurs (implémentation standalone — pas de TA-Lib pour rester sans dépendance)
# ═════════════════════════════════════════════════════════════════════════════

def compute_ema(series: pd.Series, period: int) -> pd.Series:
    """EMA classique adjust=False (formule TradingView/MT5)."""
    return series.ewm(span=period, adjust=False).mean()


def compute_atr(df: pd.DataFrame, period: int) -> pd.Series:
    """ATR par moyenne simple sur le True Range — cohérent avec core/opr.py."""
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
    """ADX standard (Wilder simplifié avec rolling mean)."""
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
    return dx.rolling(period).mean()


# ═════════════════════════════════════════════════════════════════════════════
# Détection des pivots (méthode left/right)
# ═════════════════════════════════════════════════════════════════════════════

def detect_pivots(df: pd.DataFrame, left: int, right: int
                  ) -> Tuple[List[int], List[int]]:
    """
    Pivots high (max local) et low (min local).
    Confirmation : pivot[i] connu seulement à index i+right.
    """
    highs = df["high"].values
    lows = df["low"].values
    n = len(df)
    pivot_highs: List[int] = []
    pivot_lows: List[int] = []
    for i in range(left, n - right):
        h_window = highs[i - left:i + right + 1]
        l_window = lows[i - left:i + right + 1]
        if highs[i] == h_window.max() and highs[i] > highs[i - 1] and highs[i] > highs[i + 1]:
            pivot_highs.append(i)
        if lows[i] == l_window.min() and lows[i] < lows[i - 1] and lows[i] < lows[i + 1]:
            pivot_lows.append(i)
    return pivot_highs, pivot_lows


# ═════════════════════════════════════════════════════════════════════════════
# Détection de tendance (EMA + ADX)
# ═════════════════════════════════════════════════════════════════════════════

def detect_trend(close: float, ema_fast: float, ema_slow: float,
                 adx: float, adx_threshold: float) -> str:
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
    df: pd.DataFrame, current_idx: int,
    pivot_highs: List[int], pivot_lows: List[int],
    atr_series: pd.Series, trend: str,
    pivot_right: int, min_impulse_atr: float,
    max_impulse_bars: int, impulse_lookback: int,
    fib_level: float = 0.50,
) -> Optional[Dict]:
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
            p for p in pivot_highs
            if p + confirmation_offset <= current_idx
            and current_idx - p <= impulse_lookback
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
            "swing_low": swing_low, "swing_high": swing_high,
            "impulse_size": float(impulse_size),
            "impulse_bars": int(impulse_bars),
            "fib_50": float(fib_price),    # clé conservée pour compat — contient le prix au niveau choisi
            "fib_level": float(fib_level),
            "atr_at_pivot": float(atr_at_pivot),
            "confirm_idx": int(ph_idx + pivot_right),
        }

    # BEAR
    confirmed_lows = [
        p for p in pivot_lows
        if p + confirmation_offset <= current_idx
        and current_idx - p <= impulse_lookback
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
        "swing_low": swing_low, "swing_high": swing_high,
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
    impulse: Dict, current_atr: float, ticker: str,
    sl_mult: float, tp_mult: float,
) -> Optional[Dict]:
    """Construit le dict signal — None si sizing impossible."""
    if current_atr is None or pd.isna(current_atr) or current_atr <= 0:
        return None
    entry = impulse["fib_50"]
    sl_dist = sl_mult * current_atr
    tp_dist = tp_mult * current_atr
    if impulse["direction"] == "long":
        sl_price = entry - sl_dist
        tp_price = entry + tp_dist
    else:
        sl_price = entry + sl_dist
        tp_price = entry - tp_dist
    dpp = INSTRUMENTS[ticker]["dollar_per_point"]
    n_ct = int(RISK_PER_TRADE_USD / (sl_dist * dpp))
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
        "quality": 0.0, "composite": 0.0, "alignment": 0.0,
        "atr_ratio": 0.0, "n_tf": 1, "touches": 0, "regime": "FIB",
        "zone_low": float(impulse["swing_low"]),
        "zone_high": float(impulse["swing_high"]),
        "tp_type": "atr",
    }


# ═════════════════════════════════════════════════════════════════════════════
# Moteur de backtest (boucle barre par barre)
# ═════════════════════════════════════════════════════════════════════════════

def run_fib_backtest(df_15m: pd.DataFrame, ticker: str,
                     fib_level: Optional[float] = None,
                     sl_mult: Optional[float] = None,
                     tp_mult: Optional[float] = None,
                     min_imp: Optional[float] = None,
                     apply_filter: bool = True) -> pd.DataFrame:
    """
    Backtest complet de la stratégie Fib pour un ticker.
    Retourne un DataFrame de trades clos (mêmes colonnes que les autres
    stratégies pour cohérence d'agrégation portefeuille).

    Paramètres surchargeables (sinon valeurs config.py per-ticker) :
      fib_level   : niveau Fibonacci. None → FIB_LEVEL_PER_TICKER[ticker]
                    (défaut 0.382). Alternatives explicites : 0.50, 0.618…
      sl_mult     : SL en multiples d'ATR
      tp_mult     : TP en multiples d'ATR
      min_imp     : taille minimale impulse en multiples d'ATR
      apply_filter: appliquer le filtre trigger configuré (défaut True)
    """
    if not FIB_ENABLED:
        return pd.DataFrame()

    if fib_level is None:
        fib_level = FIB_LEVEL_PER_TICKER.get(ticker, 0.50)
    if sl_mult is None:
        sl_mult = FIB_SL_ATR_MULT_PER_TICKER[ticker]
    if tp_mult is None:
        tp_mult = FIB_TP_ATR_MULT_PER_TICKER[ticker]
    if min_imp is None:
        min_imp = FIB_MIN_IMPULSE_ATR_PER_TICKER.get(ticker, FIB_MIN_IMPULSE_ATR)

    df = df_15m.copy()
    df["atr"] = compute_atr(df, FIB_ATR_PERIOD)
    df["ema_fast"] = compute_ema(df["close"], FIB_EMA_FAST_PERIOD)
    df["ema_slow"] = compute_ema(df["close"], FIB_EMA_SLOW_PERIOD)
    df["adx"] = compute_adx(df, FIB_ADX_PERIOD)

    pivot_highs, pivot_lows = detect_pivots(df, FIB_PIVOT_LEFT, FIB_PIVOT_RIGHT)

    hours = df.index.hour
    in_session = (hours >= US_SESSION_START_UTC) & (hours < US_SESSION_END_UTC)

    n = len(df)
    trades = []
    pending = None
    position = None
    last_impulse_key = None
    warmup = max(FIB_EMA_SLOW_PERIOD, 250)

    filter_cfg = FIB_TRIGGER_FILTERS_PER_TICKER.get(ticker)

    for i in range(warmup, n):
        bar = df.iloc[i]

        # 1. Gestion position ouverte
        if position is not None:
            exit_price, exit_reason = None, None
            if position["direction"] == "long":
                hit_sl = bar["low"] <= position["sl"]
                hit_tp = bar["high"] >= position["tp"]
            else:
                hit_sl = bar["high"] >= position["sl"]
                hit_tp = bar["low"] <= position["tp"]
            if hit_sl and hit_tp:
                bull_bar = bar["close"] >= bar["open"]
                if position["direction"] == "long":
                    exit_price, exit_reason = (
                        (position["tp"], "TP") if bull_bar else (position["sl"], "SL")
                    )
                else:
                    exit_price, exit_reason = (
                        (position["tp"], "TP") if not bull_bar else (position["sl"], "SL")
                    )
            elif hit_sl:
                exit_price, exit_reason = position["sl"], "SL"
            elif hit_tp:
                exit_price, exit_reason = position["tp"], "TP"

            bars_held = i - position["fill_idx"]
            if exit_reason is None and bars_held >= FIB_MAX_HOLD_BARS:
                exit_price, exit_reason = float(bar["close"]), "TE"

            if exit_reason is not None:
                pnl_pts = (
                    (exit_price - position["entry"]) if position["direction"] == "long"
                    else (position["entry"] - exit_price)
                )
                dpp = INSTRUMENTS[position["ticker"]]["dollar_per_point"]
                pnl = position["n_ct"] * pnl_pts * dpp
                trades.append({
                    **{k: v for k, v in position.items() if k != "pending_idx"},
                    "exit": float(exit_price),
                    "exit_idx": int(i),
                    "exit_time": str(df.index[i]),
                    "result": exit_reason,
                    "pnl": float(pnl),
                    "bars_held": int(bars_held),
                    "date": df.index[i].strftime("%Y-%m-%d"),
                })
                position = None
            continue

        # 2. Check fill ordre limite
        if pending is not None:
            level = pending["entry"]
            if bar["low"] <= level <= bar["high"]:
                position = {
                    **{k: v for k, v in pending.items() if k != "pending_idx"},
                    "fill_idx": int(i),
                    "fill_time": str(df.index[i]),
                }
                pending = None
                # Vérif immédiate SL/TP sur bougie de fill
                if position["direction"] == "long":
                    hit_sl = bar["low"] <= position["sl"]
                    hit_tp = bar["high"] >= position["tp"]
                else:
                    hit_sl = bar["high"] >= position["sl"]
                    hit_tp = bar["low"] <= position["tp"]
                exit_price, exit_reason = None, None
                if hit_sl and hit_tp:
                    exit_price, exit_reason = position["sl"], "SL"  # conservateur
                elif hit_sl:
                    exit_price, exit_reason = position["sl"], "SL"
                elif hit_tp:
                    exit_price, exit_reason = position["tp"], "TP"
                if exit_reason is not None:
                    pnl_pts = (
                        (exit_price - position["entry"]) if position["direction"] == "long"
                        else (position["entry"] - exit_price)
                    )
                    dpp = INSTRUMENTS[position["ticker"]]["dollar_per_point"]
                    pnl = position["n_ct"] * pnl_pts * dpp
                    trades.append({
                        **position,
                        "exit": float(exit_price),
                        "exit_idx": int(i),
                        "exit_time": str(df.index[i]),
                        "result": exit_reason,
                        "pnl": float(pnl),
                        "bars_held": 0,
                        "date": df.index[i].strftime("%Y-%m-%d"),
                    })
                    position = None
                continue
            # Pas de fill : check timeout
            bars_pending = i - pending["pending_idx"]
            if bars_pending >= FIB_ORDER_TIMEOUT_BARS:
                pending = None

        # 3. Génération nouveau signal
        if pending is not None or position is not None:
            continue
        if not bool(in_session[i]):
            continue
        atr_now = bar["atr"]
        if pd.isna(atr_now) or atr_now <= 0:
            continue

        trend = detect_trend(
            float(bar["close"]), float(bar["ema_fast"]), float(bar["ema_slow"]),
            float(bar["adx"]) if not pd.isna(bar["adx"]) else np.nan,
            FIB_ADX_TREND_THRESHOLD
        )
        if trend == "RANGE":
            continue

        impulse = find_last_impulse(
            df, i, pivot_highs, pivot_lows, df["atr"], trend,
            FIB_PIVOT_RIGHT, min_imp, FIB_MAX_IMPULSE_BARS, FIB_IMPULSE_LOOKBACK,
            fib_level=fib_level,
        )
        if impulse is None:
            continue

        impulse_key = (impulse["direction"], impulse["pivot_low_idx"],
                       impulse["pivot_high_idx"])
        if impulse_key == last_impulse_key:
            continue

        # Si prix a déjà passé fib_50 dans le sens du trade, on rate la zone
        if impulse["direction"] == "long" and bar["close"] <= impulse["fib_50"]:
            continue
        if impulse["direction"] == "short" and bar["close"] >= impulse["fib_50"]:
            continue

        sig = build_signal(impulse, float(atr_now), ticker, sl_mult, tp_mult)
        if sig is None:
            continue

        # Features additionnelles (calculées au moment de l'armement)
        sig["bars_since_confirm"] = int(i - impulse["confirm_idx"])
        sig["adx_at_arm"] = float(bar["adx"]) if not pd.isna(bar["adx"]) else float("nan")
        if i >= 3 and not pd.isna(df["adx"].iloc[i - 3]):
            sig["adx_slope_3"] = float(bar["adx"] - df["adx"].iloc[i - 3])
        else:
            sig["adx_slope_3"] = float("nan")
        sig["ema_stack_atr"] = float((bar["ema_fast"] - bar["ema_slow"]) / atr_now)
        if impulse["direction"] == "long":
            sig["price_extension_atr"] = float((bar["close"] - impulse["fib_50"]) / atr_now)
        else:
            sig["price_extension_atr"] = float((impulse["fib_50"] - bar["close"]) / atr_now)
        sig["impulse_velocity_atr"] = float(
            impulse["impulse_size"] / max(impulse["impulse_bars"], 1) / atr_now
        )
        sig["impulse_size_atr"] = float(impulse["impulse_size"] / impulse["atr_at_pivot"])
        if i >= 10:
            recent_std = float(df["close"].iloc[i - 10:i].std())
            sig["recent_vol_atr"] = recent_std / atr_now
        else:
            sig["recent_vol_atr"] = float("nan")
        sig["session_hour_utc"] = int(df.index[i].hour)
        sig["pending_idx"] = int(i)
        sig["pending_time"] = str(df.index[i])
        sig["impulse_confirm_idx"] = impulse["confirm_idx"]
        sig["trend"] = trend

        # Marque l'impulse comme vue (même si rejetée par le filtre)
        last_impulse_key = impulse_key
        sig["fib_level"] = float(fib_level)

        # ── Filtre trigger walk-forward (per-ticker) ──
        if apply_filter and filter_cfg is not None:
            feat_val = sig.get(filter_cfg["feature"])
            if feat_val is None or pd.isna(feat_val):
                continue
            thresh = float(filter_cfg["threshold"])
            if filter_cfg["direction"] == "gt":
                if feat_val <= thresh:
                    continue
            else:
                if feat_val >= thresh:
                    continue

        pending = sig

    return pd.DataFrame(trades)
