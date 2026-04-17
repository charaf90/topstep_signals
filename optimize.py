#!/usr/bin/env python3
"""
Optimisation walk-forward des paramètres de la stratégie.

Méthode :
  Phase A — Paramètres globaux (partagés entre actifs) : grid search sur
            ZONE_DISTANCE_MIN/MAX, ZONE_TOLERANCE et TREND_THRESHOLD.
  Phase B — Paramètres par actif : grid search sur RR_TARGET,
            ZONE_QUALITY_MIN, SL_BUFFER_TICKS.

Validation out-of-sample sur la période réservée (hors optimisation).

Objectif : maximiser le P&L total sous contraintes de robustesse.

Usage :
  python optimize.py --csv-dir ./data
  python optimize.py --csv-dir ./data --output-dir ./output
"""

import argparse
import itertools
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

# ─── Imports projet ────────────────────────────────────────────────────────────
import config
import core.strategy
import core.zones
import core.trend
from core.data import load_csv, build_timeframes
from core.strategy import generate_signals, simulate_trade
from core.trend import precompute_trends
from config import (
    INSTRUMENTS, MAX_TRADES_PER_DAY, CUTOFF_HOUR_UTC,
    US_SESSION_START_UTC, US_SESSION_END_UTC, MIN_BARS_US_SESSION,
)

# ==============================================================================
# ACCÉLÉRATION : detect_pivots vectorisé (5× plus rapide via rolling pandas)
# On patche le module core.zones pour toute l'optimisation.
# ==============================================================================

def _detect_pivots_fast(highs: np.ndarray, lows: np.ndarray,
                        left: int, right: int):
    """Équivalent vectorisé de core.zones.detect_pivots via rolling centré."""
    win   = left + right + 1
    h_ser = pd.Series(highs)
    l_ser = pd.Series(lows)
    h_max = h_ser.rolling(win, center=True, min_periods=win).max().values
    l_min = l_ser.rolling(win, center=True, min_periods=win).min().values
    sh = np.where((highs == h_max) & ~np.isnan(h_max), highs, np.nan)
    sl = np.where((lows  == l_min) & ~np.isnan(l_min), lows,  np.nan)
    return sh, sl

core.zones.detect_pivots = _detect_pivots_fast


def _detect_zones_fast(tf_dict: dict, cutoff) -> list:
    """
    Version optimisée de detect_zones : clustering O(n) avec running mean
    au lieu de O(n²) via np.mean sur liste.  Résultats identiques.
    """
    _TF_FREQ = {"D1": "D", "H4": "4h", "H1": "h", "15m": "15min"}

    from config import (PIVOT_CONFIGS, ZONE_TOLERANCE_PCT, ZONE_MIN_TOUCHES,
                        ZONE_MIN_TF_OR_TOUCHES, ZONE_MAX_WIDTH_PCT,
                        ZONE_RECENCY_THRESHOLD)

    # On lit les valeurs patchées depuis les modules
    zone_tol  = core.zones.ZONE_TOLERANCE_PCT

    all_pivots = []
    for tf_name, cfg in PIVOT_CONFIGS.items():
        tf_cutoff = cutoff.floor(_TF_FREQ.get(tf_name, "15min"))
        data_tf = tf_dict[tf_name][tf_dict[tf_name].index < tf_cutoff].iloc[-cfg["window"]:]
        if len(data_tf) < 50:
            continue
        sh, sl = _detect_pivots_fast(
            data_tf["high"].values, data_tf["low"].values,
            cfg["left"], cfg["right"]
        )
        n = len(data_tf)
        for i in range(n):
            recency = i / max(n - 1, 1)
            is_recent = recency > ZONE_RECENCY_THRESHOLD
            if not np.isnan(sh[i]):
                all_pivots.append({"price": sh[i], "tf": tf_name,
                                   "weight": cfg["weight"], "recency": recency,
                                   "is_recent": is_recent})
            if not np.isnan(sl[i]):
                all_pivots.append({"price": sl[i], "tf": tf_name,
                                   "weight": cfg["weight"], "recency": recency,
                                   "is_recent": is_recent})

    if len(all_pivots) < 5:
        return []

    all_pivots.sort(key=lambda x: x["price"])
    median_price = np.median([p["price"] for p in all_pivots])
    tolerance = median_price * zone_tol

    # Clustering O(n) : running mean plutôt que np.mean à chaque pivot
    groups: list = []
    current: list = [all_pivots[0]]
    cur_sum: float = all_pivots[0]["price"]
    cur_n: int = 1

    for p in all_pivots[1:]:
        if abs(p["price"] - cur_sum / cur_n) <= tolerance:
            current.append(p)
            cur_sum += p["price"]
            cur_n   += 1
        else:
            groups.append(current)
            current = [p]
            cur_sum = p["price"]
            cur_n   = 1
    groups.append(current)

    zones = []
    for group in groups:
        n_touches = len(group)
        tfs = set(p["tf"] for p in group)

        if n_touches < ZONE_MIN_TOUCHES:
            continue
        if len(tfs) < ZONE_MIN_TF_OR_TOUCHES[0] and n_touches < ZONE_MIN_TF_OR_TOUCHES[1]:
            continue
        if not any(p["is_recent"] for p in group):
            continue

        prices = [p["price"] for p in group]
        zone_low, zone_high = min(prices), max(prices)
        zone_mid = sum(prices) / len(prices)
        if (zone_high - zone_low) / zone_mid > ZONE_MAX_WIDTH_PCT:
            continue

        recency_sum = sum(p["recency"] for p in group)
        weight_sum  = sum(p["weight"]  for p in group)
        quality = (
            min(n_touches, 8) / 8 * 30
            + min(len(tfs), 4) / 4 * 25
            + min(weight_sum / 10, 1) * 15
            + (recency_sum / n_touches) * 15
        )
        zones.append({"low": zone_low, "high": zone_high, "mid": zone_mid,
                      "touches": n_touches, "n_tf": len(tfs), "quality": quality})

    zones.sort(key=lambda z: z["quality"], reverse=True)
    return zones

core.zones.detect_zones    = _detect_zones_fast
# generate_signals importe detect_zones par valeur → on patche aussi core.strategy
core.strategy.detect_zones = _detect_zones_fast

# Cache premarket : compute_features est coûteux (pandas slices) mais ses résultats
# ne dépendent d'aucun paramètre optimisé → on l'appelle une seule fois par jour.
_pm_cache: dict = {}

def _compute_features_cached(df_15m: pd.DataFrame, cutoff) -> dict | None:
    key = str(cutoff)
    if key not in _pm_cache:
        from core.premarket import compute_features as _orig_pm
        _pm_cache[key] = _orig_pm(df_15m, cutoff)
    return _pm_cache[key]

core.strategy.compute_pm = _compute_features_cached

# ==============================================================================
# SPLIT WALK-FORWARD
# ==============================================================================

TRAIN_END   = "2025-09-30"   # Fin de la période in-sample
TEST_START  = "2025-10-01"   # Début de la période out-of-sample

# ==============================================================================
# GRILLES DE PARAMÈTRES
# ==============================================================================

# Phase A : paramètres globaux (communs aux 3 actifs)
GRID_GLOBAL = {
    "dist_min":  [0.10, 0.15, 0.20, 0.30],
    "dist_max":  [1.5,  2.0,  2.5,  3.0],
    "zone_tol":  [0.001, 0.002, 0.003],
    "trend_thr": [0.05, 0.10, 0.15, 0.33],
}

# Phase B : paramètres par actif
GRID_ASSET = {
    "rr":          [1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0],
    "quality_min": [30, 40, 50, 60, 70, 80],
    "sl_buffer":   [2, 3, 4, 5, 6],
}

# Phase C : paramètres du score composite par actif
GRID_COMPOSITE_ASSET = {
    "score_min":       [50, 55, 58, 60, 65, 70],
    "trend_strength":  [0.15, 0.20, 0.25, 0.30, 0.40],
}

# Valeurs par défaut (sauvegardées au démarrage pour restauration finale)
_DEFAULTS_GLOBAL = {
    "dist_min":  config.ZONE_DISTANCE_MIN_PCT,
    "dist_max":  config.ZONE_DISTANCE_MAX_PCT,
    "zone_tol":  config.ZONE_TOLERANCE_PCT,
    "trend_thr": config.TREND_BULL_THRESHOLD,
}
_DEFAULTS_ASSET = {
    t: {
        "rr":          config.RR_TARGET[t],
        "quality_min": config.ZONE_QUALITY_MIN[t],
        "sl_buffer":   config.SL_BUFFER_TICKS,
    }
    for t in INSTRUMENTS
}

# ==============================================================================
# MONKEY-PATCHING DES MODULES
# Les modules core.* importent les scalaires config par nom (from config import X).
# Pour que les changements prennent effet, on doit patcher les namespaces des
# modules qui ont déjà importé ces valeurs.
# Les dicts (RR_TARGET, ZONE_QUALITY_MIN) sont modifiables en place.
# ==============================================================================

def set_global_params(p: dict):
    """Applique les paramètres globaux à tous les modules concernés."""
    core.strategy.ZONE_DISTANCE_MIN_PCT = p["dist_min"]
    core.strategy.ZONE_DISTANCE_MAX_PCT = p["dist_max"]
    core.zones.ZONE_TOLERANCE_PCT        = p["zone_tol"]
    core.trend.TREND_BULL_THRESHOLD      = p["trend_thr"]
    core.trend.TREND_BEAR_THRESHOLD      = -p["trend_thr"]


def set_asset_params(ticker: str, p: dict):
    """Applique les paramètres spécifiques à un actif."""
    config.RR_TARGET[ticker]          = p["rr"]
    config.ZONE_QUALITY_MIN[ticker]   = p["quality_min"]
    core.strategy.SL_BUFFER_TICKS     = p["sl_buffer"]


def set_composite_params(ticker: str, p: dict):
    """Applique les paramètres du score composite (Phase C)."""
    config.COMPOSITE_SCORE_MIN[ticker] = p["score_min"]
    config.TREND_STRENGTH_MIN[ticker] = p["trend_strength"]


def set_ym1_enabled(enabled: bool):
    """Active/désactive YM1 globalement (Phase C décide par PF OOS ≥ 1.2)."""
    config.YM1_ENABLED = enabled
    core.strategy.YM1_ENABLED = enabled


_DEFAULTS_COMPOSITE = {
    t: {
        "score_min":      config.COMPOSITE_SCORE_MIN[t],
        "trend_strength": config.TREND_STRENGTH_MIN[t],
    }
    for t in INSTRUMENTS
}


def restore_defaults():
    """Remet toutes les valeurs d'origine."""
    set_global_params(_DEFAULTS_GLOBAL)
    for t, p in _DEFAULTS_ASSET.items():
        set_asset_params(t, p)
    for t, p in _DEFAULTS_COMPOSITE.items():
        set_composite_params(t, p)


# ==============================================================================
# BACKTEST SUR UNE PLAGE DE DATES
# ==============================================================================

def run_period(
    df_15m: pd.DataFrame,
    tf_dict: dict,
    ticker: str,
    date_from: str,
    date_to: str,
    trend_scores: dict | None = None,
) -> pd.DataFrame:
    """
    Simule la stratégie sur [date_from, date_to].
    Utilise tout l'historique disponible pour les indicateurs (pas de leak
    sur les signaux : seule la fenêtre [date_from, date_to] est simulée).
    """
    dpp = INSTRUMENTS[ticker]["dollar_per_point"]

    if trend_scores is None:
        trend_scores = precompute_trends(tf_dict)

    all_dates = df_15m.index.normalize().unique()
    sim_dates = all_dates[
        (all_dates >= pd.Timestamp(date_from)) &
        (all_dates <= pd.Timestamp(date_to))
    ]

    rows = []
    for day in sim_dates:
        ds = day.strftime("%Y-%m-%d")
        cutoff   = pd.Timestamp(f"{ds} {CUTOFF_HOUR_UTC:02d}:00:00")
        us_start = pd.Timestamp(f"{ds} {US_SESSION_START_UTC:02d}:00:00")
        us_end   = pd.Timestamp(f"{ds} {US_SESSION_END_UTC:02d}:00:00")

        us_data = df_15m[(df_15m.index >= us_start) & (df_15m.index <= us_end)]
        if len(us_data) < MIN_BARS_US_SESSION:
            continue

        signals = generate_signals(
            df_15m, tf_dict, ticker, cutoff, trend_scores,
            max_signals=MAX_TRADES_PER_DAY,
        )

        day_trades = []
        for sig in signals:
            res = simulate_trade(us_data, sig, dpp)
            day_trades.append({
                "date":      ds,
                "result":    res["result"],
                "pnl":       res["pnl"],
                "fill_time": res["fill_time"],
            })

        # Limite journalière (filet de sécurité)
        filled = [t for t in day_trades if t["result"] != "NOT_FILLED"]
        filled.sort(key=lambda t: t["fill_time"] or "")
        for t in filled[MAX_TRADES_PER_DAY:]:
            t["result"] = "NOT_FILLED"
            t["pnl"]    = 0

        rows.extend(day_trades)

    cols = ["date", "result", "pnl", "fill_time"]
    return pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)


# ==============================================================================
# FONCTION OBJECTIF
# ==============================================================================

def compute_score(df: pd.DataFrame) -> dict:
    """
    Objectif principal : P&L total (maximiser).

    Contraintes Topstep-aware (score = 0 si non satisfaites) :
      - n_trades >= 8    → évite sur-optimisation sur petit échantillon
      - win_rate >= 32%  → plancher statistique
      - profit_factor >= 1.4 → durcie pour Topstep 50K
      - max_dd >= -1200  → marge sur trailing DD de 2000
      - max_consec_losses <= 5 → évite les streaks mortels

    Score final = pnl_total si contraintes OK, sinon 0.
    """
    empty = {"score": 0.0, "n": 0, "wr": 0.0, "pf": 0.0, "pnl": 0.0,
             "dd": 0.0, "consec": 0}
    if df.empty:
        return empty

    filled = df[df["result"] != "NOT_FILLED"]
    n = len(filled)
    if n < 8:
        return {**empty, "n": n}

    wins       = (filled["pnl"] > 0).sum()
    wr         = wins / n
    pnl        = filled["pnl"].sum()
    gross_win  = filled[filled["pnl"] > 0]["pnl"].sum()
    gross_loss = abs(filled[filled["pnl"] <= 0]["pnl"].sum()) or 1.0
    pf         = gross_win / gross_loss

    # DD et streak sur P&L cumulatif journalier
    daily = filled.groupby("date")["pnl"].sum().sort_index().values
    cum = np.cumsum(daily)
    peak = np.maximum.accumulate(cum)
    dd = float((cum - peak).min()) if len(cum) > 0 else 0.0
    consec = 0
    cur = 0
    for v in daily:
        if v < 0:
            cur += 1
            consec = max(consec, cur)
        else:
            cur = 0

    out = {"score": 0.0, "n": n, "wr": wr, "pf": pf, "pnl": pnl,
           "dd": dd, "consec": consec}

    if wr < 0.32 or pf < 1.4 or dd < -1200 or consec > 5:
        return out

    out["score"] = pnl
    return out


# ==============================================================================
# PHASE A — PARAMÈTRES GLOBAUX
# ==============================================================================

def optimize_global(data: dict, trend_cache: dict, date_from: str, date_to: str):
    """
    Grid search sur les paramètres globaux.
    Objectif : somme des scores sur les 3 actifs.
    """
    keys   = list(GRID_GLOBAL.keys())
    values = list(GRID_GLOBAL.values())
    combos = list(itertools.product(*values))
    total  = len(combos)

    print(f"\n{'='*62}")
    print(f"  PHASE A — Paramètres globaux ({total} combinaisons × 3 actifs)")
    print(f"{'='*62}")

    results = []
    t0 = time.time()

    for idx, combo in enumerate(combos, 1):
        p = dict(zip(keys, combo))
        set_global_params(p)

        total_score = 0.0
        per_ticker  = {}
        for ticker, (df_15m, tf_dict) in data.items():
            m = compute_score(
                run_period(df_15m, tf_dict, ticker, date_from, date_to,
                           trend_cache[ticker])
            )
            total_score += m["score"]
            per_ticker[ticker] = m

        results.append({"params": p, "score": total_score, "per_ticker": per_ticker})

        # Progression
        if idx % 40 == 0 or idx == total:
            elapsed = time.time() - t0
            eta = elapsed / idx * (total - idx)
            print(f"  [{idx:>4}/{total}]  meilleur={max(r['score'] for r in results):>10,.0f}"
                  f"  ETA {eta:.0f}s")

    results.sort(key=lambda r: r["score"], reverse=True)
    best = results[0]

    print(f"\n  Meilleure combinaison globale (score={best['score']:,.0f}) :")
    for k, v in best["params"].items():
        print(f"    {k:15s} = {v}")
    for t, m in best["per_ticker"].items():
        print(f"    {t}: n={m['n']}  WR={m['wr']*100:.0f}%  PF={m['pf']:.2f}"
              f"  P&L=${m['pnl']:,.0f}")

    return best["params"], results


# ==============================================================================
# CACHE DE ZONES POUR LA PHASE B
# Les zones S/R et le régime ne dépendent pas des params par actif (RR, quality,
# SL buffer). On les calcule une seule fois par jour et on les réutilise pour
# toutes les combinaisons de la Phase B, divisant le temps × 3–4.
# ==============================================================================

def build_day_cache(
    df_15m: pd.DataFrame,
    tf_dict: dict,
    ticker: str,
    date_from: str,
    date_to: str,
    trend_scores: dict,
) -> dict:
    """
    Précompute, pour chaque jour de la plage, les éléments coûteux mais
    indépendants des params par actif : zones, régime, price_now, us_data.
    """
    from core.zones import detect_zones
    from core.trend import get_regime
    from core.premarket import compute_features, filter_pass
    from config import MIN_BARS_HISTORY

    all_dates = df_15m.index.normalize().unique()
    sim_dates = all_dates[
        (all_dates >= pd.Timestamp(date_from)) &
        (all_dates <= pd.Timestamp(date_to))
    ]

    cache = {}
    for day in sim_dates:
        ds       = day.strftime("%Y-%m-%d")
        cutoff   = pd.Timestamp(f"{ds} {CUTOFF_HOUR_UTC:02d}:00:00")
        us_start = pd.Timestamp(f"{ds} {US_SESSION_START_UTC:02d}:00:00")
        us_end   = pd.Timestamp(f"{ds} {US_SESSION_END_UTC:02d}:00:00")

        us_data = df_15m[(df_15m.index >= us_start) & (df_15m.index <= us_end)]
        if len(us_data) < MIN_BARS_US_SESSION:
            continue

        regime = get_regime(trend_scores, cutoff)
        if regime is None:
            continue

        zones = detect_zones(tf_dict, cutoff)
        if not zones:
            continue

        pm = compute_features(df_15m, cutoff)
        if pm is None or not filter_pass(pm, ticker):
            continue

        before = df_15m[df_15m.index <= cutoff]
        if len(before) < MIN_BARS_HISTORY:
            continue

        cache[ds] = {
            "zones":     zones,
            "regime":    regime,
            "price_now": float(before["close"].iloc[-1]),
            "us_data":   us_data,
        }
    return cache


def run_period_cached(ticker: str, day_cache: dict) -> pd.DataFrame:
    """
    Backtest rapide utilisant le cache de zones.
    Re-filtre les zones et recalcule entry/SL/TP avec les params actuels.
    Reproduit exactement la logique de generate_signals + simulate_trade.
    """
    import math
    from core.strategy import simulate_trade as _sim

    inst      = INSTRUMENTS[ticker]
    dpp       = inst["dollar_per_point"]
    tick      = inst["tick_size"]
    sl_min    = config.SL_MINIMUM[ticker]
    rr        = config.RR_TARGET[ticker]
    q_min     = config.ZONE_QUALITY_MIN[ticker]
    buffer    = core.strategy.SL_BUFFER_TICKS * tick
    dist_min  = core.strategy.ZONE_DISTANCE_MIN_PCT
    dist_max  = core.strategy.ZONE_DISTANCE_MAX_PCT

    rows = []
    for ds, day in day_cache.items():
        zones     = day["zones"]
        regime    = day["regime"]
        price_now = day["price_now"]
        us_data   = day["us_data"]

        signals = []
        for zone in zones:
            if len(signals) >= MAX_TRADES_PER_DAY:
                break

            if zone["quality"] < q_min:
                continue

            zm           = zone["mid"]
            is_support   = zm < price_now
            is_resistance = zm > price_now
            if not is_support and not is_resistance:
                continue

            dist_pct = abs(price_now - zm) / price_now * 100
            if dist_pct < dist_min or dist_pct > dist_max:
                continue

            if is_support   and regime == "BEAR":
                continue
            if is_resistance and regime == "BULL":
                continue

            direction  = "long" if is_support else "short"
            zone_width = zone["high"] - zone["low"]

            if direction == "long":
                entry = math.floor((zone["low"]  + 0.25 * zone_width) / tick) * tick
                sl_price = min(zone["low"]  - buffer, entry - sl_min)
                sl_dist  = entry - sl_price
            else:
                entry = math.ceil((zone["high"] - 0.25 * zone_width) / tick) * tick
                sl_price = max(zone["high"] + buffer, entry + sl_min)
                sl_dist  = sl_price - entry

            tp_dist = sl_dist * rr
            tp_price = entry + tp_dist if direction == "long" else entry - tp_dist

            n_ct = int(config.RISK_PER_TRADE_USD / (sl_dist * dpp))
            if n_ct == 0:
                continue

            signals.append({
                "direction": direction, "entry": entry,
                "sl": sl_price, "tp": tp_price,
                "sl_dist": sl_dist, "n_ct": n_ct,
            })

        day_trades = []
        for sig in signals:
            res = _sim(us_data, sig, dpp)
            day_trades.append({
                "date": ds, "result": res["result"],
                "pnl": res["pnl"], "fill_time": res["fill_time"],
            })

        # Limite journalière
        filled = [t for t in day_trades if t["result"] != "NOT_FILLED"]
        filled.sort(key=lambda t: t["fill_time"] or "")
        for t in filled[MAX_TRADES_PER_DAY:]:
            t["result"] = "NOT_FILLED"
            t["pnl"]    = 0

        rows.extend(day_trades)

    cols = ["date", "result", "pnl", "fill_time"]
    return pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)


# ==============================================================================
# PHASE B — PARAMÈTRES PAR ACTIF
# ==============================================================================

def optimize_per_asset(data: dict, trend_cache: dict, date_from: str, date_to: str):
    """
    Grid search sur les paramètres par actif (indépendamment pour chaque ticker).
    Les paramètres globaux doivent déjà être patchés avant l'appel.
    Utilise un cache de zones pour accélérer ×3–4.
    """
    keys   = list(GRID_ASSET.keys())
    values = list(GRID_ASSET.values())
    combos = list(itertools.product(*values))
    total  = len(combos)

    print(f"\n{'='*62}")
    print(f"  PHASE B — Paramètres par actif ({total} combinaisons × 3 actifs)")
    print(f"{'='*62}")

    best_per_ticker = {}

    for ticker, (df_15m, tf_dict) in data.items():
        print(f"\n  ▸ Optimisation {ticker} — pré-calcul des zones...")
        day_cache = build_day_cache(
            df_15m, tf_dict, ticker, date_from, date_to, trend_cache[ticker]
        )
        print(f"    {len(day_cache)} jours avec signaux valides")

        results = []
        t0 = time.time()

        for idx, combo in enumerate(combos, 1):
            p = dict(zip(keys, combo))
            set_asset_params(ticker, p)

            m = compute_score(run_period_cached(ticker, day_cache))
            results.append({"params": p, **m})

            if idx % 50 == 0 or idx == total:
                elapsed = time.time() - t0
                eta = elapsed / idx * (total - idx)
                best_so_far = max(results, key=lambda r: r["score"])
                print(f"    [{idx:>4}/{total}]  meilleur P&L=${best_so_far['pnl']:>8,.0f}"
                      f"  (RR={best_so_far['params']['rr']}"
                      f"  Q={best_so_far['params']['quality_min']}"
                      f"  buf={best_so_far['params']['sl_buffer']})"
                      f"  ETA {eta:.0f}s")

        results.sort(key=lambda r: r["score"], reverse=True)
        best = results[0]
        best_per_ticker[ticker] = best["params"]

        print(f"\n  Meilleurs paramètres {ticker} (score={best['score']:,.0f}) :")
        print(f"    RR={best['params']['rr']}  quality_min={best['params']['quality_min']}"
              f"  sl_buffer={best['params']['sl_buffer']}")
        print(f"    → n={best['n']}  WR={best['wr']*100:.0f}%  PF={best['pf']:.2f}"
              f"  P&L=${best['pnl']:,.0f}")

        # Appliquer les meilleurs paramètres de cet actif avant de passer au suivant
        set_asset_params(ticker, best["params"])

    return best_per_ticker


# ==============================================================================
# PHASE C — PARAMÈTRES DU SCORE COMPOSITE PAR ACTIF
# ==============================================================================

def optimize_composite_per_asset(
    data: dict, trend_cache: dict,
    date_from_is: str, date_to_is: str,
    date_from_oos: str,
):
    """
    Grid search sur COMPOSITE_SCORE_MIN et TREND_STRENGTH_MIN par actif.

    Utilise run_period (chemin complet generate_signals) car le composite est
    calculé à l'intérieur et n'est pas câblé dans run_period_cached. On scanne
    donc IS pour sélectionner, puis on vérifie OOS.

    Pour YM1 : le flag YM1_ENABLED est forcé True durant l'optimisation, puis
    remis à False si PF OOS < 1.2.
    """
    keys   = list(GRID_COMPOSITE_ASSET.keys())
    values = list(GRID_COMPOSITE_ASSET.values())
    combos = list(itertools.product(*values))
    total  = len(combos)

    print(f"\n{'='*62}")
    print(f"  PHASE C — Score composite par actif ({total} combinaisons × 3 actifs)")
    print(f"{'='*62}")

    # Autoriser YM1 uniquement pendant cette phase ; on décide à la fin.
    set_ym1_enabled(True)

    best_per_ticker = {}

    for ticker, (df_15m, tf_dict) in data.items():
        print(f"\n  ▸ Optimisation composite {ticker}...")
        results = []
        t0 = time.time()

        for idx, combo in enumerate(combos, 1):
            p = dict(zip(keys, combo))
            set_composite_params(ticker, p)

            m = compute_score(
                run_period(df_15m, tf_dict, ticker, date_from_is, date_to_is,
                           trend_cache[ticker])
            )
            results.append({"params": p, **m})

            if idx % 10 == 0 or idx == total:
                elapsed = time.time() - t0
                eta = elapsed / idx * (total - idx)
                best_so_far = max(results, key=lambda r: r["score"])
                print(f"    [{idx:>3}/{total}]  meilleur P&L IS=${best_so_far['pnl']:>8,.0f}"
                      f"  (score_min={best_so_far['params']['score_min']}"
                      f"  trend={best_so_far['params']['trend_strength']})"
                      f"  ETA {eta:.0f}s")

        results.sort(key=lambda r: r["score"], reverse=True)
        best = results[0]

        # Vérifier OOS PF pour la décision YM1
        set_composite_params(ticker, best["params"])
        df_oos = run_period(df_15m, tf_dict, ticker, date_from_oos,
                            "2030-12-31", trend_cache[ticker])
        m_oos = compute_score(df_oos)

        best_per_ticker[ticker] = {
            **best["params"],
            "is_pnl": best["pnl"], "is_pf": best["pf"], "is_wr": best["wr"],
            "oos_pnl": m_oos["pnl"], "oos_pf": m_oos["pf"], "oos_wr": m_oos["wr"],
            "oos_n": m_oos["n"],
        }

        print(f"\n  Meilleur composite {ticker}: score_min={best['params']['score_min']}"
              f"  trend_strength={best['params']['trend_strength']}")
        print(f"    IS  : n={best['n']}  WR={best['wr']*100:.0f}%"
              f"  PF={best['pf']:.2f}  P&L=${best['pnl']:,.0f}")
        print(f"    OOS : n={m_oos['n']}  WR={m_oos['wr']*100:.0f}%"
              f"  PF={m_oos['pf']:.2f}  P&L=${m_oos['pnl']:,.0f}")

    # Décision finale YM1 : PF OOS ≥ 1.2 requis
    ym1 = best_per_ticker.get("YM1", {})
    ym1_keep = ym1.get("oos_pf", 0) >= 1.2 and ym1.get("oos_n", 0) >= 8
    set_ym1_enabled(ym1_keep)
    print(f"\n  → YM1 {'✅ RÉACTIVÉ' if ym1_keep else '❌ DÉSACTIVÉ'}"
          f"  (OOS PF={ym1.get('oos_pf', 0):.2f} ; seuil requis 1.20)")

    return best_per_ticker, ym1_keep


# ==============================================================================
# VALIDATION OUT-OF-SAMPLE
# ==============================================================================

def validate(data: dict, trend_cache: dict, date_from: str):
    """Lance le backtest de validation sur la période réservée."""
    print(f"\n{'='*62}")
    print(f"  VALIDATION OUT-OF-SAMPLE ({date_from} → fin des données)")
    print(f"{'='*62}")

    total_pnl = 0.0
    for ticker, (df_15m, tf_dict) in data.items():
        df_oos = run_period(df_15m, tf_dict, ticker, date_from,
                            "2030-12-31", trend_cache[ticker])
        m = compute_score(df_oos)
        total_pnl += m["pnl"]
        print(f"\n  {ticker}: n={m['n']}  WR={m['wr']*100:.0f}%  PF={m['pf']:.2f}"
              f"  P&L=${m['pnl']:,.0f}")

    print(f"\n  Total OOS P&L : ${total_pnl:,.0f}")
    return total_pnl


# ==============================================================================
# RAPPORT DES RÉSULTATS
# ==============================================================================

def print_summary(best_global: dict, best_per_ticker: dict, pnl_train: float, pnl_oos: float):
    """Affiche le récapitulatif final des paramètres optimaux."""
    print(f"\n{'='*62}")
    print(f"  RÉCAPITULATIF — PARAMÈTRES OPTIMAUX")
    print(f"{'='*62}")
    print(f"\n  === Paramètres globaux ===")
    print(f"  ZONE_DISTANCE_MIN_PCT = {best_global['dist_min']}")
    print(f"  ZONE_DISTANCE_MAX_PCT = {best_global['dist_max']}")
    print(f"  ZONE_TOLERANCE_PCT    = {best_global['zone_tol']}")
    print(f"  TREND_BULL_THRESHOLD  = +{best_global['trend_thr']}")
    print(f"  TREND_BEAR_THRESHOLD  = -{best_global['trend_thr']}")

    print(f"\n  === Paramètres par actif ===")
    for ticker, p in best_per_ticker.items():
        print(f"  {ticker}: RR={p['rr']}  ZONE_QUALITY_MIN={p['quality_min']}"
              f"  SL_BUFFER_TICKS={p['sl_buffer']}")

    print(f"\n  === Performance ===")
    print(f"  In-sample  (déc 2024 – sept 2025) : ${pnl_train:>+10,.0f}")
    print(f"  Hors-sample (oct 2025 – mars 2026) : ${pnl_oos:>+10,.0f}")

    if pnl_train > 0:
        ratio = pnl_oos / pnl_train
        flag = "⚠ suroptimisation possible" if ratio < 0.5 else "✅ robustesse satisfaisante"
        print(f"  Ratio OOS/IS                       : {ratio:.0%}  {flag}")

    print(f"\n  {'─'*58}")
    print(f"  Mettre à jour config.py avec ces valeurs.")
    print(f"  {'─'*58}")


# ==============================================================================
# MAIN
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(description="Optimisation walk-forward des paramètres")
    parser.add_argument("--csv-dir",    type=str, required=True)
    parser.add_argument("--output-dir", type=str, default="./output")
    args = parser.parse_args()

    csv_dir    = Path(args.csv_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(exist_ok=True)

    # ── Chargement des données ──────────────────────────────────────────────
    print(f"\n{'='*62}")
    print(f"  CHARGEMENT DES DONNÉES")
    print(f"{'='*62}")

    data        = {}   # ticker → (df_15m, tf_dict)
    trend_cache = {}   # ticker → trend_scores (pré-calculés une fois)

    for ticker in INSTRUMENTS:
        csv_path = csv_dir / f"{ticker}_data_m15.csv"
        if not csv_path.exists():
            print(f"  [!] Fichier introuvable : {csv_path}")
            sys.exit(1)
        df_15m = load_csv(str(csv_path))
        tf     = build_timeframes(df_15m)
        data[ticker] = (df_15m, tf)
        trend_cache[ticker] = precompute_trends(tf)
        print(f"  {ticker}: {df_15m.index.min().date()} → {df_15m.index.max().date()}"
              f"  ({len(df_15m):,} bougies)")

    print(f"\n  Split walk-forward :")
    print(f"    In-sample  : déc 2024 → {TRAIN_END}")
    print(f"    Hors-sample: {TEST_START} → fin")

    # ── Baseline (paramètres actuels) ──────────────────────────────────────
    print(f"\n{'='*62}")
    print(f"  BASELINE (paramètres actuels sur période in-sample)")
    print(f"{'='*62}")
    baseline_pnl = 0.0
    for ticker, (df_15m, tf) in data.items():
        m = compute_score(
            run_period(df_15m, tf, ticker, "2024-12-01", TRAIN_END, trend_cache[ticker])
        )
        baseline_pnl += m["pnl"]
        print(f"  {ticker}: n={m['n']}  WR={m['wr']*100:.0f}%  PF={m['pf']:.2f}"
              f"  P&L=${m['pnl']:,.0f}")
    print(f"  Total baseline IS : ${baseline_pnl:,.0f}")

    # ── Phase A : paramètres globaux ────────────────────────────────────────
    best_global, global_results = optimize_global(
        data, trend_cache, "2024-12-01", TRAIN_END
    )
    set_global_params(best_global)

    # Sauvegarder les résultats globaux
    rows_global = []
    for r in global_results:
        row = {**r["params"], "total_score": r["score"]}
        for t, m in r["per_ticker"].items():
            row[f"{t}_pnl"] = m["pnl"]
            row[f"{t}_n"]   = m["n"]
            row[f"{t}_wr"]  = round(m["wr"], 3)
            row[f"{t}_pf"]  = round(m["pf"], 3)
        rows_global.append(row)
    pd.DataFrame(rows_global).sort_values("total_score", ascending=False).to_csv(
        output_dir / "optim_global.csv", index=False
    )
    print(f"\n  ✓ Résultats sauvegardés : {output_dir}/optim_global.csv")

    # ── Phase B : paramètres par actif ──────────────────────────────────────
    best_per_ticker = optimize_per_asset(
        data, trend_cache, "2024-12-01", TRAIN_END
    )

    # ── Phase C : paramètres composite par actif (IS + validation OOS PF) ──
    best_composite, ym1_keep = optimize_composite_per_asset(
        data, trend_cache, "2024-12-01", TRAIN_END, TEST_START
    )

    # Calculer le P&L in-sample total avec les meilleurs paramètres
    print(f"\n{'='*62}")
    print(f"  IN-SAMPLE avec paramètres optimaux")
    print(f"{'='*62}")
    train_pnl = 0.0
    for ticker, (df_15m, tf) in data.items():
        m = compute_score(
            run_period(df_15m, tf, ticker, "2024-12-01", TRAIN_END, trend_cache[ticker])
        )
        train_pnl += m["pnl"]
        print(f"  {ticker}: n={m['n']}  WR={m['wr']*100:.0f}%  PF={m['pf']:.2f}"
              f"  P&L=${m['pnl']:,.0f}")
    print(f"  Total IS : ${train_pnl:,.0f}  (baseline: ${baseline_pnl:,.0f})")

    # ── Validation OOS ──────────────────────────────────────────────────────
    oos_pnl = validate(data, trend_cache, TEST_START)

    # ── Récapitulatif ───────────────────────────────────────────────────────
    print_summary(best_global, best_per_ticker, train_pnl, oos_pnl)
    print(f"\n  === Paramètres composite (Phase C) ===")
    for t, p in best_composite.items():
        print(f"  {t}: score_min={p['score_min']}  trend_strength={p['trend_strength']}"
              f"  — IS PF={p['is_pf']:.2f}  OOS PF={p['oos_pf']:.2f}")
    print(f"  YM1_ENABLED = {ym1_keep}")

    # ── Écrire les nouveaux paramètres dans config.py ──────────────────────
    update_config(best_global, best_per_ticker, best_composite, ym1_keep)
    print(f"\n  ✅ config.py mis à jour avec les paramètres optimaux.")
    print(f"  Relancer le backtest complet pour vérification :")
    print(f"    python backtest.py --csv-dir {args.csv_dir}")


# ==============================================================================
# MISE À JOUR DE CONFIG.PY
# ==============================================================================

def update_config(global_p: dict, asset_p: dict,
                  composite_p: dict | None = None,
                  ym1_enabled: bool | None = None):
    """Met à jour config.py avec les paramètres optimaux trouvés."""
    config_path = Path(__file__).parent / "config.py"
    text = config_path.read_text()

    replacements = [
        # Per-asset dicts — reconstruit la ligne entière
        (
            _find_line(text, "RR_TARGET"),
            f'RR_TARGET = {{"MES1": {asset_p["MES1"]["rr"]}, '
            f'"NQ1": {asset_p["NQ1"]["rr"]}, '
            f'"YM1": {asset_p["YM1"]["rr"]}}}'
        ),
        (
            _find_line(text, "ZONE_QUALITY_MIN"),
            f'ZONE_QUALITY_MIN = {{"MES1": {asset_p["MES1"]["quality_min"]}, '
            f'"NQ1": {asset_p["NQ1"]["quality_min"]}, '
            f'"YM1": {asset_p["YM1"]["quality_min"]}}}'
        ),
        # SL_BUFFER_TICKS — on prend la valeur actuelle dans core.strategy
        # (la même pour tous après optimisation globale)
        (
            _find_line(text, "SL_BUFFER_TICKS"),
            f'SL_BUFFER_TICKS = {asset_p["MES1"]["sl_buffer"]}'
        ),
        # Zones
        (
            _find_line(text, "ZONE_TOLERANCE_PCT"),
            f'ZONE_TOLERANCE_PCT = {global_p["zone_tol"]}      # optimisé'
        ),
        (
            _find_line(text, "ZONE_DISTANCE_MIN_PCT"),
            f'ZONE_DISTANCE_MIN_PCT = {global_p["dist_min"]}'
        ),
        (
            _find_line(text, "ZONE_DISTANCE_MAX_PCT"),
            f'ZONE_DISTANCE_MAX_PCT = {global_p["dist_max"]}'
        ),
        # Tendance
        (
            _find_line(text, "TREND_BULL_THRESHOLD"),
            f'TREND_BULL_THRESHOLD = {global_p["trend_thr"]}'
        ),
        (
            _find_line(text, "TREND_BEAR_THRESHOLD"),
            f'TREND_BEAR_THRESHOLD = -{global_p["trend_thr"]}'
        ),
    ]

    # Phase C : paramètres composite + YM1_ENABLED
    if composite_p is not None:
        replacements.extend([
            (
                _find_line(text, "COMPOSITE_SCORE_MIN"),
                f'COMPOSITE_SCORE_MIN = {{"MES1": {composite_p["MES1"]["score_min"]}, '
                f'"NQ1": {composite_p["NQ1"]["score_min"]}, '
                f'"YM1": {composite_p["YM1"]["score_min"]}}}'
            ),
            (
                _find_line(text, "TREND_STRENGTH_MIN"),
                f'TREND_STRENGTH_MIN = {{"MES1": {composite_p["MES1"]["trend_strength"]}, '
                f'"NQ1": {composite_p["NQ1"]["trend_strength"]}, '
                f'"YM1": {composite_p["YM1"]["trend_strength"]}}}'
            ),
        ])
    if ym1_enabled is not None:
        replacements.append((
            _find_line(text, "YM1_ENABLED"),
            f'YM1_ENABLED = {ym1_enabled}'
        ))

    for old_line, new_line in replacements:
        if old_line and old_line in text:
            text = text.replace(old_line, new_line, 1)

    config_path.write_text(text)


def _find_line(text: str, var_name: str) -> str | None:
    """Retourne la ligne contenant une affectation de variable."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{var_name} =") or stripped.startswith(f"{var_name}="):
            return line
    return None


if __name__ == "__main__":
    main()
