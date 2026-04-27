"""
Stratégie OPR (Opening Range Breakout) — module isolé.

Définition de la zone OPR :
  • La 1ère bougie 15min de la session US (US_SESSION_START_UTC) définit la
    zone OPR : opr_high = high de la bougie, opr_low = low.

Règles de génération de signal (sur les bougies suivantes de la session) :
  • Premier déclenchement après l'OPR : la 1ère bougie qui clôture hors zone
    place un ordre limite au niveau OPR du sens de sortie (limit BUY @ opr_high
    si close au-dessus, limit SELL @ opr_low si close en-dessous).
  • Re-déclenchements (continuation intraday) : une bougie qui ouvre DANS
    l'OPR puis clôture HORS de l'OPR replace un ordre limite dans le sens
    de sortie. Cela permet plusieurs entrées par session si le prix oscille.
  • Si la direction change entre deux déclenchements, l'ordre précédent
    encore en attente (non rempli) est annulé et remplacé.

Construction du trade :
  entry    = OPR_high (long) ou OPR_low (short)
  sl_dist  = OPR_range + buffer (le SL est de l'autre côté de l'OPR)
  tp_dist  = sl_dist × OPR_RR
  n_ct     = RISK_PER_TRADE_USD / (sl_dist × $/pt)

Filtres / garde-fous :
  • OPR_RANGE_MIN_PCT / OPR_RANGE_MAX_PCT : reject si le range OPR est trop
    petit (sl trop court) ou trop grand (sl trop large → 0 contrat).
  • SL_MINIMUM par actif respecté (cohérence cross-stratégie).
  • Optionnel : OPR_REQUIRE_TREND — n'autoriser qu'un long en BULL et qu'un
    short en BEAR (réutilise core/trend).
"""

import math
from typing import Dict, List, Optional, Tuple

import pandas as pd

from config import (
    INSTRUMENTS, SL_MINIMUM, RISK_PER_TRADE_USD,
    US_SESSION_START_UTC, US_SESSION_END_UTC,
    OPR_RR, OPR_RR_BY_TICKER, OPR_SL_BUFFER_TICKS, OPR_MAX_TRADES_PER_DAY,
    OPR_RANGE_MIN_PCT, OPR_RANGE_MAX_PCT,
    OPR_REQUIRE_TREND, OPR_ENABLED,
)


def _rr_for(ticker: str) -> float:
    """RR par actif (calibré walk-forward), fallback sur OPR_RR global."""
    return float(OPR_RR_BY_TICKER.get(ticker, OPR_RR))


# ─────────────────────────────────────────────────────────────────────────
# Détection de la zone OPR
# ─────────────────────────────────────────────────────────────────────────

def compute_opr(us_data: pd.DataFrame) -> Optional[Dict]:
    """
    La 1ère bougie de us_data définit la zone OPR.
    us_data doit être strictement dans la session US, indexée par datetime,
    avec colonnes open/high/low/close.
    """
    if len(us_data) < 2:
        return None
    bar = us_data.iloc[0]
    return {
        "high": float(bar["high"]),
        "low": float(bar["low"]),
        "mid": float((bar["high"] + bar["low"]) / 2),
        "range": float(bar["high"] - bar["low"]),
        "time": us_data.index[0],
    }


# ─────────────────────────────────────────────────────────────────────────
# Génération des triggers (signaux candidats avant fill)
# ─────────────────────────────────────────────────────────────────────────

def _build_signal(
    opr: Dict,
    direction: str,
    trigger_time: pd.Timestamp,
    ticker: str,
    rr: float,
) -> Optional[Dict]:
    """Construit un signal OPR à partir d'un trigger."""
    inst = INSTRUMENTS[ticker]
    dpp = inst["dollar_per_point"]
    tick = inst["tick_size"]
    sl_min = SL_MINIMUM[ticker]
    buffer = OPR_SL_BUFFER_TICKS * tick

    if direction == "long":
        entry = opr["high"]
        sl_price = opr["low"] - buffer
        sl_dist = entry - sl_price
    else:
        entry = opr["low"]
        sl_price = opr["high"] + buffer
        sl_dist = sl_price - entry

    # SL minimum par actif
    if sl_dist < sl_min:
        # Élargir le SL pour respecter le minimum
        if direction == "long":
            sl_price = entry - sl_min
        else:
            sl_price = entry + sl_min
        sl_dist = sl_min

    tp_dist = sl_dist * rr
    tp_price = entry + tp_dist if direction == "long" else entry - tp_dist

    # Sizing
    if sl_dist <= 0 or dpp <= 0:
        return None
    n_ct = int(RISK_PER_TRADE_USD / (sl_dist * dpp))
    if n_ct <= 0:
        return None

    risk = n_ct * sl_dist * dpp
    gain = n_ct * tp_dist * dpp

    return {
        "ticker": ticker,
        "strategy": "OPR",
        "direction": direction,
        "entry": entry,
        "sl": sl_price,
        "tp": tp_price,
        "sl_dist": sl_dist,
        "tp_dist": tp_dist,
        "rr": round(rr, 2),
        "n_ct": n_ct,
        "risk": risk,
        "gain": gain,
        "opr_high": opr["high"],
        "opr_low": opr["low"],
        "opr_range": opr["range"],
        "trigger_time": str(trigger_time),
        # Champs neutres pour compat avec le format signal "composite"
        "quality": 0.0,
        "composite": 0.0,
        "alignment": 0.0,
        "atr_ratio": 0.0,
        "n_tf": 1,
        "touches": 0,
        "regime": "OPR",
        "zone_low": opr["low"],
        "zone_high": opr["high"],
        "price_now": entry,
        "tp_type": "rr",
    }


def generate_opr_signals(
    us_data: pd.DataFrame,
    ticker: str,
    regime: Optional[str] = None,
    rr: Optional[float] = None,
    max_signals: int = 0,
) -> Tuple[List[Dict], Optional[Dict]]:
    """
    Génère la liste des signaux OPR pour une session US donnée.

    us_data : bougies 15m de la session (1ère bougie = OPR).
    regime  : régime composite optionnel ("BULL"/"BEAR"/"RANGE"). Si
              OPR_REQUIRE_TREND=True, filtre les signaux contre-tendance.
    rr      : risk-reward custom (override config OPR_RR).
    max_signals : plafond sur le nombre de signaux retournés (0 = pas de limite).

    Retourne (signaux, opr_zone). Les signaux sont triés par trigger_time.
    """
    if not OPR_ENABLED:
        return [], None

    opr = compute_opr(us_data)
    if opr is None or opr["range"] <= 0:
        return [], opr

    rr_used = rr if rr is not None else _rr_for(ticker)

    # Filtres range OPR (sur le prix de référence = mid)
    range_pct = opr["range"] / opr["mid"]
    if range_pct < OPR_RANGE_MIN_PCT:
        return [], opr
    if range_pct > OPR_RANGE_MAX_PCT:
        return [], opr

    signals: List[Dict] = []
    bars_after = us_data.iloc[1:]
    if len(bars_after) == 0:
        return [], opr

    first_trigger_done = False

    for ts, bar in bars_after.iterrows():
        bar_open = float(bar["open"])
        bar_close = float(bar["close"])

        close_above = bar_close > opr["high"]
        close_below = bar_close < opr["low"]
        if not (close_above or close_below):
            continue

        if not first_trigger_done:
            # Premier déclenchement : aucune contrainte sur l'open
            allowed = True
        else:
            # Continuation : la bougie doit avoir ouvert DANS l'OPR
            allowed = (opr["low"] <= bar_open <= opr["high"])

        if not allowed:
            continue

        direction = "long" if close_above else "short"

        # Filtre tendance optionnel
        if OPR_REQUIRE_TREND and regime is not None:
            if direction == "long" and regime == "BEAR":
                continue
            if direction == "short" and regime == "BULL":
                continue

        sig = _build_signal(opr, direction, ts, ticker, rr_used)
        if sig is None:
            continue
        signals.append(sig)
        first_trigger_done = True

    if max_signals > 0:
        signals = signals[:max_signals]

    return signals, opr


# ─────────────────────────────────────────────────────────────────────────
# Simulation
# ─────────────────────────────────────────────────────────────────────────

def _find_fill_after(
    us_data: pd.DataFrame, entry: float, direction: str, start_ts: pd.Timestamp,
) -> int:
    """Index (positionnel) de la 1ère bougie ≥ start_ts qui touche entry."""
    sub = us_data[us_data.index >= start_ts]
    if sub.empty:
        return -1
    base_pos = us_data.index.get_loc(sub.index[0])
    for i, (_, bar) in enumerate(sub.iterrows()):
        if direction == "long" and bar["low"] <= entry <= bar["high"]:
            return base_pos + i
        if direction == "short" and bar["low"] <= entry <= bar["high"]:
            return base_pos + i
    return -1


def simulate_opr_trade(us_data: pd.DataFrame, signal: Dict, dpp: float) -> Dict:
    """
    Simule un trade OPR sur la session US.

    L'ordre limite est armé à partir de signal["trigger_time"]. La règle
    "même bougie" (TP autorisé seulement si la bougie de fill va dans le
    sens du trade) est conservée pour cohérence avec simulate_trade
    classique.
    """
    entry = signal["entry"]
    sl = signal["sl"]
    tp = signal["tp"]
    direction = signal["direction"]
    n_ct = signal["n_ct"]
    trigger_time = pd.Timestamp(signal["trigger_time"])

    fill_idx = _find_fill_after(us_data, entry, direction, trigger_time)
    if fill_idx < 0:
        return {
            "result": "NOT_FILLED", "pnl": 0, "exit": None,
            "fill_time": None, "exit_time": None,
        }

    fill_time = us_data.index[fill_idx]

    bar = us_data.iloc[fill_idx]
    bougie_haussiere = bar["close"] >= bar["open"]
    result = None
    exit_price = None
    exit_time = None

    if direction == "long":
        if bar["low"] <= sl:
            result, exit_price, exit_time = "SL", sl, us_data.index[fill_idx]
        elif bougie_haussiere and bar["high"] >= tp:
            result, exit_price, exit_time = "TP", tp, us_data.index[fill_idx]
    else:
        if bar["high"] >= sl:
            result, exit_price, exit_time = "SL", sl, us_data.index[fill_idx]
        elif (not bougie_haussiere) and bar["low"] <= tp:
            result, exit_price, exit_time = "TP", tp, us_data.index[fill_idx]

    if result is None:
        for i in range(fill_idx + 1, len(us_data)):
            bar = us_data.iloc[i]
            if direction == "long":
                if bar["low"] <= sl:
                    result, exit_price, exit_time = "SL", sl, us_data.index[i]; break
                if bar["high"] >= tp:
                    result, exit_price, exit_time = "TP", tp, us_data.index[i]; break
            else:
                if bar["high"] >= sl:
                    result, exit_price, exit_time = "SL", sl, us_data.index[i]; break
                if bar["low"] <= tp:
                    result, exit_price, exit_time = "TP", tp, us_data.index[i]; break

    if result is None:
        result = "TE"
        exit_price = float(us_data["close"].iloc[-1])
        exit_time = us_data.index[-1]

    pnl_pts = (exit_price - entry) if direction == "long" else (entry - exit_price)
    pnl = n_ct * pnl_pts * dpp

    return {
        "result": result, "pnl": pnl, "exit": exit_price,
        "fill_time": str(fill_time), "exit_time": str(exit_time),
    }
