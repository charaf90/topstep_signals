"""
Stratégie SMC (Smart Money Concepts) — smc-v1.

Logique (inspirée de LuxAlgo Smart Money Concepts) :
  1. Détection des pivots swing (confirmation côté droit uniquement, méthode LuxAlgo)
  2. Suivi de structure : CHoCH (Change of Character) = retournement
       CHoCH bullish : tendance était BEAR, close passe au-dessus du dernier swing high
       CHoCH bearish : tendance était BULL, close passe en-dessous du dernier swing low
  3. Order Block : dans la plage [pivot source → bar CHoCH], on cherche
       - LONG  : barre avec le plus bas parsedLow  (zone de demande)
       - SHORT : barre avec le plus haut parsedHigh (zone d'offre)
       parsedHigh/Low = filtré volatilité : si range bougie ≥ vol_mult × ATR(200),
       les high/low sont inversés pour ignorer les mèches explosives.
  4. Ordre LIMIT positionné à entry_pct dans l'OB (0=bord, 0.5=milieu, 1=bord opposé)
  5. SL au-delà de l'OB + buffer ; TP = entry ± tp_atr_mult × ATR(14)
  6. Timeout ordre + fermeture forcée à 16h30 NY.

Killzone : NY Open (9h30–11h00 NY) uniquement en v1.
V1 : CHoCH uniquement, structure swing (50 barres).
V2 (à venir) : BOS, structure interne, multi-killzones.
"""

from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from zoneinfo import ZoneInfo

from config import (
    INSTRUMENTS, RISK_PER_TRADE_USD,
    SMC_TIMEZONE, SMC_KILLZONE_NY, SMC_ALL_KILLZONES,
    SMC_SWING_SIZE, SMC_INTERNAL_SIZE,
    SMC_ENTRY_PCT,
    SMC_SL_ATR_MULT_PER_TICKER,
    SMC_TP_ATR_MULT_PER_TICKER,
    SMC_ATR_PERIOD,
    SMC_OB_VOL_ATR_PERIOD, SMC_OB_VOL_MULT,
    SMC_ORDER_TIMEOUT_BARS,
    SMC_SESSION_END_NY,
    SMC_MAX_TRADES_PER_DAY,
    SMC_SIGNAL_TYPE,
    SMC_STRUCTURE_LEVEL,
    SMC_CHOCH_LOOKBACK_BARS,
)


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _ny_minutes(ts: pd.Timestamp) -> int:
    """Timestamp UTC naïf → minutes depuis minuit NY (DST-aware)."""
    ts_utc = ts.tz_localize("UTC") if ts.tz is None else ts
    ny = ts_utc.tz_convert(SMC_TIMEZONE)
    return ny.hour * 60 + ny.minute


def _compute_atr(df: pd.DataFrame, period: int) -> pd.Series:
    high  = df["high"]
    low   = df["low"]
    pc    = df["close"].shift(1)
    tr = pd.concat([high - low, (high - pc).abs(), (low - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def _compute_parsed_highs_lows(
    df: pd.DataFrame,
    atr_period: int = SMC_OB_VOL_ATR_PERIOD,
    vol_mult: float = SMC_OB_VOL_MULT,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Highs/lows filtrés par volatilité (méthode LuxAlgo).
    Pour les bougies dont range ≥ vol_mult × ATR, on inverse high↔low
    afin d'éviter de créer des OB sur des mèches excessives.
    """
    atr      = _compute_atr(df, atr_period).values
    rng      = (df["high"] - df["low"]).values
    high_vol = rng >= vol_mult * np.where(np.isnan(atr), np.inf, atr)

    highs = df["high"].values
    lows  = df["low"].values
    parsed_highs = np.where(high_vol, lows,  highs)
    parsed_lows  = np.where(high_vol, highs, lows)
    return parsed_highs, parsed_lows


def _ny_day_view(df_15m: pd.DataFrame, day_ny: pd.Timestamp) -> pd.DataFrame:
    tz = ZoneInfo(SMC_TIMEZONE)
    if day_ny.tz is None:
        day_ny = day_ny.tz_localize(tz)
    start = day_ny.replace(hour=0,  minute=0,  second=0, microsecond=0).tz_convert("UTC").tz_localize(None)
    end   = day_ny.replace(hour=23, minute=59, second=59, microsecond=0).tz_convert("UTC").tz_localize(None)
    return df_15m[(df_15m.index >= start) & (df_15m.index <= end)]


# ══════════════════════════════════════════════════════════════════════════════
# Détection de structure (BOS / CHoCH) et Order Blocks
# ══════════════════════════════════════════════════════════════════════════════

def _scan_structure(
    df: pd.DataFrame,
    size: int,
    parsed_highs: np.ndarray,
    parsed_lows: np.ndarray,
    atr_series: pd.Series,
    signal_type: str = "choch",
    struct_label: str = "swing",
) -> List[Dict]:
    """
    Scanne bar par bar pour détecter BOS/CHoCH et identifier les OB associés.

    Méthode pivot LuxAlgo (right-side uniquement) :
      Pivot high à i-size confirmé à la bar i si high[i-size] > max(high[i-size+1:i+1])
      Pivot low  à i-size confirmé à la bar i si low[i-size]  < min(low[i-size+1:i+1])

    signal_type : "choch" | "bos" | "both"

    Retourne une liste de dicts :
      {type, direction, level, level_idx, ob_idx, ob_high, ob_low, bar_idx, atr}
    """
    n      = len(df)
    highs  = df["high"].values
    lows   = df["low"].values
    closes = df["close"].values

    swing_high_level   = np.nan
    swing_high_idx     = -1
    swing_high_crossed = False
    swing_low_level    = np.nan
    swing_low_idx      = -1
    swing_low_crossed  = False
    trend_bias = 0   # 0=inconnu, 1=bullish, -1=bearish

    events = []

    for i in range(size, n):
        pivot_idx   = i - size
        right_highs = highs[pivot_idx + 1 : i + 1]
        right_lows  = lows[pivot_idx + 1  : i + 1]
        if len(right_highs) == 0:
            continue

        # ── Mise à jour des pivots ───────────────────────────────────────
        if highs[pivot_idx] > right_highs.max():
            swing_high_level   = highs[pivot_idx]
            swing_high_idx     = pivot_idx
            swing_high_crossed = False

        if lows[pivot_idx] < right_lows.min():
            swing_low_level   = lows[pivot_idx]
            swing_low_idx     = pivot_idx
            swing_low_crossed = False

        close_i    = closes[i]
        close_prev = closes[i - 1]
        atr_val    = float(atr_series.iloc[i]) if not pd.isna(atr_series.iloc[i]) else 0.0

        # ── Cassure haussière ────────────────────────────────────────────
        if (not np.isnan(swing_high_level)
                and not swing_high_crossed
                and close_prev <= swing_high_level
                and close_i > swing_high_level):

            struct_type = "CHoCH" if trend_bias == -1 else ("BOS" if trend_bias == 1 else "CHoCH")
            trend_bias  = 1
            swing_high_crossed = True

            include = (signal_type == "both"
                       or (signal_type == "choch" and struct_type == "CHoCH")
                       or (signal_type == "bos"   and struct_type == "BOS"))

            if include and swing_high_idx >= 0:
                ob_slice = parsed_lows[swing_high_idx : i]
                if len(ob_slice) > 0:
                    ob_local = int(np.argmin(ob_slice))
                    ob_idx   = swing_high_idx + ob_local
                    events.append({
                        "type":         struct_type,
                        "direction":    "long",
                        "level":        float(swing_high_level),
                        "level_idx":    int(swing_high_idx),
                        "ob_idx":       int(ob_idx),
                        "ob_high":      float(parsed_highs[ob_idx]),
                        "ob_low":       float(parsed_lows[ob_idx]),
                        "bar_idx":      int(i),
                        "atr":          atr_val,
                        "struct_label": struct_label,
                        "struct_size":  size,
                    })

        # ── Cassure baissière ────────────────────────────────────────────
        if (not np.isnan(swing_low_level)
                and not swing_low_crossed
                and close_prev >= swing_low_level
                and close_i < swing_low_level):

            struct_type = "CHoCH" if trend_bias == 1 else ("BOS" if trend_bias == -1 else "CHoCH")
            trend_bias  = -1
            swing_low_crossed = True

            include = (signal_type == "both"
                       or (signal_type == "choch" and struct_type == "CHoCH")
                       or (signal_type == "bos"   and struct_type == "BOS"))

            if include and swing_low_idx >= 0:
                ob_slice = parsed_highs[swing_low_idx : i]
                if len(ob_slice) > 0:
                    ob_local = int(np.argmax(ob_slice))
                    ob_idx   = swing_low_idx + ob_local
                    events.append({
                        "type":         struct_type,
                        "direction":    "short",
                        "level":        float(swing_low_level),
                        "level_idx":    int(swing_low_idx),
                        "ob_idx":       int(ob_idx),
                        "ob_high":      float(parsed_highs[ob_idx]),
                        "ob_low":       float(parsed_lows[ob_idx]),
                        "bar_idx":      int(i),
                        "atr":          atr_val,
                        "struct_label": struct_label,
                        "struct_size":  size,
                    })

    return events


# ══════════════════════════════════════════════════════════════════════════════
# Construction du signal
# ══════════════════════════════════════════════════════════════════════════════

def _build_signal(
    event: Dict,
    atr_val: float,
    ticker: str,
    entry_pct: float,
    sl_atr_mult: float,
    tp_atr_mult: float,
) -> Optional[Dict]:
    """Construit le dict signal à partir d'un événement de structure."""
    inst = INSTRUMENTS[ticker]
    tick = inst["tick_size"]
    dpp  = inst["dollar_per_point"]

    def _t(p): return round(round(p / tick) * tick, 10)

    direction = event["direction"]
    ob_high   = event["ob_high"]
    ob_low    = event["ob_low"]
    ob_size   = ob_high - ob_low

    if ob_size <= 0 or atr_val <= 0:
        return None

    if direction == "long":
        entry = _t(ob_low  + entry_pct * ob_size)
        sl    = _t(ob_low  - sl_atr_mult * atr_val)
    else:
        entry = _t(ob_high - entry_pct * ob_size)
        sl    = _t(ob_high + sl_atr_mult * atr_val)

    sl_dist = abs(entry - sl)
    if sl_dist <= 0:
        return None

    tp_dist = tp_atr_mult * atr_val
    tp      = _t(entry + tp_dist) if direction == "long" else _t(entry - tp_dist)
    tp_dist = abs(entry - tp)

    n_ct = int(RISK_PER_TRADE_USD / (sl_dist * dpp))
    if n_ct <= 0:
        return None

    return {
        "ticker":       ticker,
        "strategy":     "SMC",
        "direction":    direction,
        "entry":        float(entry),
        "sl":           float(sl),
        "tp":           float(tp),
        "sl_dist":      float(sl_dist),
        "tp_dist":      float(tp_dist),
        "rr":           round(tp_dist / sl_dist, 2),
        "n_ct":         int(n_ct),
        "risk":         float(n_ct * sl_dist * dpp),
        "ob_high":      float(ob_high),
        "ob_low":       float(ob_low),
        "struct_type":  event["type"],
        "struct_level": float(event["level"]),
        "zone_low":     float(ob_low),
        "zone_high":    float(ob_high),
        "regime":       event["type"],
    }


# ══════════════════════════════════════════════════════════════════════════════
# Validation OB + simulation
# ══════════════════════════════════════════════════════════════════════════════

def _is_ob_valid(event: Dict, closes: np.ndarray, from_idx: int, to_idx: int) -> bool:
    """
    Vérifie que l'OB n'a pas été invalidé (cassé en clôture) entre from_idx et to_idx.
    OB long  invalidé si close < ob_low.
    OB short invalidé si close > ob_high.
    """
    ob_low  = event["ob_low"]
    ob_high = event["ob_high"]
    for i in range(from_idx, min(to_idx, len(closes))):
        if event["direction"] == "long"  and closes[i] < ob_low:
            return False
        if event["direction"] == "short" and closes[i] > ob_high:
            return False
    return True


def _simulate_signal(
    signal: Dict,
    df: pd.DataFrame,
    start_idx: int,
    end_idx_session: int,
    timeout_bars: int,
    dpp: float,
) -> Dict:
    """Simule le cycle de vie d'un ordre limite (fill → SL/TP/TE/NOT_FILLED)."""
    direction = signal["direction"]
    entry = signal["entry"]
    sl    = signal["sl"]
    tp    = signal["tp"]

    last_arm = min(start_idx + timeout_bars, end_idx_session)
    fill_idx = -1
    for i in range(start_idx + 1, last_arm + 1):
        if i >= len(df):
            break
        bar = df.iloc[i]
        if direction == "long"  and bar["low"]  <= entry: fill_idx = i; break
        if direction == "short" and bar["high"] >= entry: fill_idx = i; break

    if fill_idx < 0:
        return {"result": "NOT_FILLED", "pnl": 0.0,
                "fill_time": None, "exit_time": None, "exit": None}

    fill_time = str(df.index[fill_idx])
    result = exit_price = exit_time = None
    bar  = df.iloc[fill_idx]
    bull = bar["close"] >= bar["open"]

    if direction == "long":
        if bar["low"] <= sl:
            result, exit_price, exit_time = "SL", sl, df.index[fill_idx]
        elif bull and bar["high"] >= tp:
            result, exit_price, exit_time = "TP", tp, df.index[fill_idx]
    else:
        if bar["high"] >= sl:
            result, exit_price, exit_time = "SL", sl, df.index[fill_idx]
        elif (not bull) and bar["low"] <= tp:
            result, exit_price, exit_time = "TP", tp, df.index[fill_idx]

    if result is None:
        for i in range(fill_idx + 1, end_idx_session + 1):
            if i >= len(df):
                break
            bar = df.iloc[i]
            if direction == "long":
                if bar["low"]  <= sl: result, exit_price, exit_time = "SL", sl, df.index[i]; break
                if bar["high"] >= tp: result, exit_price, exit_time = "TP", tp, df.index[i]; break
            else:
                if bar["high"] >= sl: result, exit_price, exit_time = "SL", sl, df.index[i]; break
                if bar["low"]  <= tp: result, exit_price, exit_time = "TP", tp, df.index[i]; break

    if result is None:
        ei = min(end_idx_session, len(df) - 1)
        result, exit_price, exit_time = "TE", float(df.iloc[ei]["close"]), df.index[ei]

    pnl_pts = (exit_price - signal["entry"]) if direction == "long" else (signal["entry"] - exit_price)
    return {
        "result":    result,
        "pnl":       float(signal["n_ct"] * pnl_pts * dpp),
        "fill_time": fill_time,
        "exit_time": str(exit_time),
        "exit":      float(exit_price),
    }


# ══════════════════════════════════════════════════════════════════════════════
# API publique : run_smc_day
# ══════════════════════════════════════════════════════════════════════════════

def run_smc_day(
    df_15m: pd.DataFrame,
    ticker: str,
    day_ny: pd.Timestamp,
    params: Optional[Dict] = None,
) -> Tuple[List[Dict], List[Dict]]:
    """
    Exécute la stratégie SMC pour un jour NY donné.

    params (override config) :
      entry_pct        : float  — position dans l'OB (0–1)
      tp_atr_mult      : float  — multiplicateur ATR pour le TP
      sl_buffer_ticks  : int    — buffer SL au-delà de l'OB
      structure_level  : str    — "swing" | "internal" | "both"
      signal_type      : str    — "choch" | "bos" | "both"
      timeout_bars     : int
      max_trades       : int
      choch_lookback   : int    — barres max depuis le CHoCH pour rester actif

    Returns (signals, sim_results) — listes appariées.
    """
    p = params or {}
    inst = INSTRUMENTS[ticker]
    tick = inst["tick_size"]
    dpp  = inst["dollar_per_point"]

    entry_pct     = float(p.get("entry_pct",       SMC_ENTRY_PCT))
    tp_mult       = float(p.get("tp_atr_mult",     SMC_TP_ATR_MULT_PER_TICKER.get(ticker, 2.0)))
    sl_mult       = float(p.get("sl_atr_mult",     SMC_SL_ATR_MULT_PER_TICKER.get(ticker, 0.5)))
    struct_level  = p.get("structure_level",        SMC_STRUCTURE_LEVEL)
    signal_type   = p.get("signal_type",            SMC_SIGNAL_TYPE)
    timeout       = int(p.get("timeout_bars",       SMC_ORDER_TIMEOUT_BARS))
    max_trades    = int(p.get("max_trades",         SMC_MAX_TRADES_PER_DAY))
    lookback      = int(p.get("choch_lookback",     SMC_CHOCH_LOOKBACK_BARS))

    kz_h_s, kz_m_s, kz_h_e, kz_m_e = SMC_KILLZONE_NY
    kz_start = kz_h_s * 60 + kz_m_s
    kz_end   = kz_h_e * 60 + kz_m_e

    # ── Fenêtre de données : jour + contexte historique ──────────────────────
    day_view = _ny_day_view(df_15m, day_ny)
    if len(day_view) == 0:
        return [], []

    first_global  = df_15m.index.get_loc(day_view.index[0])
    context_start = max(0, first_global - 300)

    # Tronquer au dernier bar de la session courante (+ petit buffer pour la
    # détection de fin de session). Sans ça, _scan_structure parcourt tout le
    # dataset à chaque appel → O(n_total) au lieu de O(n_jour + contexte).
    tz = ZoneInfo(SMC_TIMEZONE)
    day_ny_tz = day_ny.tz_localize(tz) if day_ny.tz is None else day_ny
    h_end, m_end = SMC_SESSION_END_NY
    end_utc = day_ny_tz.replace(hour=h_end, minute=m_end).tz_convert("UTC").tz_localize(None)
    after_global = np.searchsorted(df_15m.index, end_utc, side="right")
    context_end  = min(after_global + 5, len(df_15m))  # +5 barres de marge

    df_work         = df_15m.iloc[context_start:context_end].copy()
    day_start_local = first_global - context_start

    after   = df_work.index >= end_utc
    end_idx_session = int(np.argmax(after)) - 1 if after.any() else len(df_work) - 1
    if end_idx_session <= day_start_local:
        return [], []

    # ── Indicateurs ──────────────────────────────────────────────────────────
    atr_series                = _compute_atr(df_work, SMC_ATR_PERIOD)
    parsed_highs, parsed_lows = _compute_parsed_highs_lows(df_work)
    closes                    = df_work["close"].values

    # ── Scan de structure ─────────────────────────────────────────────────────
    scan_sizes = []
    if struct_level in ("swing", "both"):
        scan_sizes.append((SMC_SWING_SIZE, "swing"))
    if struct_level in ("internal", "both"):
        scan_sizes.append((SMC_INTERNAL_SIZE, "internal"))

    all_events: List[Dict] = []
    for sz, label in scan_sizes:
        all_events.extend(
            _scan_structure(df_work, sz, parsed_highs, parsed_lows,
                            atr_series, signal_type, label)
        )
    all_events.sort(key=lambda e: e["bar_idx"])

    # ── Killzones actives ─────────────────────────────────────────────────────
    # killzones : None → défaut config | "all" → toutes | str nommée → une seule
    #             dict → custom | list[str] → plusieurs nommées
    kz_param = p.get("killzones", None)
    if kz_param is None:
        active_killzones = {"ny_open": (kz_h_s, kz_m_s, kz_h_e, kz_m_e)}
    elif isinstance(kz_param, str) and kz_param == "all":
        active_killzones = SMC_ALL_KILLZONES
    elif isinstance(kz_param, str) and kz_param in SMC_ALL_KILLZONES:
        active_killzones = {kz_param: SMC_ALL_KILLZONES[kz_param]}
    elif isinstance(kz_param, list):
        active_killzones = {k: SMC_ALL_KILLZONES[k] for k in kz_param
                            if k in SMC_ALL_KILLZONES}
    elif isinstance(kz_param, dict):
        active_killzones = kz_param
    else:
        active_killzones = {"ny_open": (kz_h_s, kz_m_s, kz_h_e, kz_m_e)}

    # Pré-calcul des plages en minutes NY par killzone
    kz_ranges = {
        name: (h_s * 60 + m_s, h_e * 60 + m_e)
        for name, (h_s, m_s, h_e, m_e) in active_killzones.items()
    }

    # ── Boucle killzone ───────────────────────────────────────────────────────
    signals:     List[Dict] = []
    sim_results: List[Dict] = []
    used_ob_keys = set()
    n_emitted    = 0

    for i in range(day_start_local, end_idx_session + 1):
        if n_emitted >= max_trades:
            break

        kz_name = next(
            (name for name, (s, e) in kz_ranges.items()
             if s <= _ny_minutes(df_work.index[i]) < e),
            None,
        )
        if kz_name is None:
            continue

        # Événement le plus récent avant cette barre dans le lookback
        candidates = [e for e in all_events
                      if e["bar_idx"] < i and (i - e["bar_idx"]) <= lookback]
        if not candidates:
            continue
        event = candidates[-1]

        ob_key = (event["ob_idx"], event["direction"])
        if ob_key in used_ob_keys:
            continue

        # OB non invalidé
        if not _is_ob_valid(event, closes, event["bar_idx"], i):
            continue

        # L'entrée doit être un retracement (pas déjà dépassé)
        close_now = float(df_work["close"].iloc[i])
        ob_mid    = event["ob_low"] + entry_pct * (event["ob_high"] - event["ob_low"])
        if event["direction"] == "long"  and ob_mid >= close_now:
            continue
        if event["direction"] == "short" and ob_mid <= close_now:
            continue

        atr_val = float(atr_series.iloc[i]) if not pd.isna(atr_series.iloc[i]) else 0.0
        sig = _build_signal(event, atr_val, ticker, entry_pct, sl_mult, tp_mult)
        if sig is None:
            continue

        sim = _simulate_signal(sig, df_work, i, end_idx_session, timeout, dpp)

        sig["trigger_time"] = str(df_work.index[i])
        sig["killzone"]     = kz_name
        sig["struct_label"] = event.get("struct_label", "swing")
        sig["struct_size"]  = event.get("struct_size", SMC_SWING_SIZE)
        signals.append(sig)
        sim_results.append(sim)
        used_ob_keys.add(ob_key)
        n_emitted += 1

    return signals, sim_results
