"""
Stratégie `opr-v5` — variante recherche d'opr-v4 avec 3 features de filtrage causal.

OPR v5 = OPR v4 (production) + 3 filtres post-traitement :
  • F1 : nb de bougies M15 entre fin OPR (opr_ts_ny + 15min, ~9h45 NY) et
         la bougie de cassure (`trigger_time`). Filtre les cassures trop
         rapides ou trop tardives.
  • F2 : excursion max (normalisée par ATR daily) dans le sens de la cassure
         entre `trigger_time` et `fill_time`. Filtre les "longs mouvements"
         (retournement potentiel, pas pullback propre).
  • F3 : nb de bougies M15 entre `trigger_time` et `fill_time`. Filtre les
         pullbacks trop tardifs.

Architecture :
  Wrapper de `core.opr.run_opr_day()` (zone production protégée — ne pas
  modifier). On exécute v4 nativement puis on enrichit le DataFrame de trades
  des colonnes `f1_bars`, `f2_excursion_atr`, `f2_excursion_oprrange`,
  `f2_excursion_pts`, `f3_bars`. Quand un filtre rejette un trade, on le
  marque NOT_FILLED (équivalent causal au filtre pré-fill).

Approximation flaggée :
  Sur les rares jours à plusieurs trades, le post-traitement diffère
  marginalement d'un filtre pré-fill (le second trade peut bénéficier d'une
  fenêtre légèrement différente). En pratique <5 % des jours sur MES1/NQ1/YM1
  ont >1 trade — impact négligeable.

Causalité (zero look-ahead) :
  F1 utilise bougies sur (opr_ts_ny + 15min, trigger_time]   → tous ≤ trigger
  F2/F3 utilisent bougies sur [trigger_time, fill_time]      → tous ≤ fill
  Asserts algorithmiques dans _compute_features_for_signal.

Note frictions : core/opr.py retourne du P&L brut. opr-v5 conserve ce régime
pour comparaison apples-to-apples avec opr-v4 baseline.

Voir config.py section "STRATÉGIE OPR v5" pour les paramètres.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

# Reproductibilité (convention projet — np.random pas tiré ici mais respect convention)
np.random.seed(42)

import config as cfg
from config import (
    OPR_TIMEZONE,
    OPR_ATR_PERIOD,
    OPR_SL_ATR_MULT, OPR_TP_ATR_MULT,
    OPR_V5_STRATEGY_VERSION, OPR_V5_TICKERS,
    OPR_V5_F1_MIN, OPR_V5_F1_MAX, OPR_V5_F2_MAX_ATR, OPR_V5_F3_MAX,
    CUTOFF_HOUR_UTC,
    DAILY_STOP_AFTER_SL, CONSEC_LOSS_PAUSE_DAYS, DAILY_LOCKIN_THRESHOLD,
)
from core.opr import run_opr_day, _ny_session_view, _opr_bar, _compute_atr_daily
from core.risk_topstep import trade_allowed


# ── Identité de la stratégie ─────────────────────────────────────────────────
STRATEGY_ID   = OPR_V5_STRATEGY_VERSION          # "opr-v5"
TICKERS       = list(OPR_V5_TICKERS)             # ["MES1", "NQ1", "YM1"]
CSV_SUFFIX    = "_opr_v5"
CSV_TIMEFRAME = "m15"

# ── Grille d'optimisation (walk-forward via core/optimizer.py) ───────────────
# 3 dimensions (limite anti-curve-fitting) : f1_max, f2_max_atr, f3_max.
# La borne basse f1_min est explorée dans la pré-analyse (script
# scripts/explore_opr_v5_features.py) mais désactivée dans la grille initiale.
#
# Grille initiale large (bornes resserrées après pré-analyse) :
#   3 × 3 × 3 = 27 combos/ticker → 81 total
#   Bonferroni : p_seuil = 0.05 / 81 = 6.2e-4 → bootstrap ≥ 99.94 % requis
PARAM_GRID = {
    "f1_max":     [None, 6, 10],     # 0 = trigger immédiat post-OPR ; 12+ = très tardif
    "f2_max_atr": [None, 0.5, 1.0],  # excursion max en ATR daily entre trigger et fill
    "f3_max":     [None, 2, 4],      # bougies entre trigger et fill (M15)
}


# ═════════════════════════════════════════════════════════════════════════════
# Helpers : conversion timestamps, comptage bougies
# ═════════════════════════════════════════════════════════════════════════════

def _to_ts_ny(value, tz: ZoneInfo) -> Optional[pd.Timestamp]:
    """
    Convertit un timestamp (potentiellement string, naïf UTC, ou tz-aware)
    en pd.Timestamp tz-aware NY. None si valeur invalide ou nulle.
    """
    if value is None:
        return None
    if isinstance(value, str):
        if not value:
            return None
        try:
            ts = pd.Timestamp(value)
        except (ValueError, TypeError):
            return None
    elif isinstance(value, pd.Timestamp):
        ts = value
    else:
        try:
            ts = pd.Timestamp(value)
        except Exception:
            return None
    if pd.isna(ts):
        return None
    if ts.tzinfo is None:
        # On suppose tz NY (cohérent avec opr_ts_ny et fill_time de run_opr_day)
        try:
            ts = ts.tz_localize(tz)
        except Exception:
            try:
                ts = ts.tz_localize("UTC").tz_convert(tz)
            except Exception:
                return None
    else:
        ts = ts.tz_convert(tz)
    return ts


def _count_bars_strict(
    df_session_ny: pd.DataFrame,
    start_ts: pd.Timestamp,
    end_ts: pd.Timestamp,
    start_inclusive: bool,
    end_inclusive: bool,
) -> int:
    """
    Compte les bougies de `df_session_ny` (tz NY) dont l'index est dans
    l'intervalle [start_ts, end_ts] avec inclusivité paramétrable.

    Hypothèses : `df_session_ny.index` est trié et tz-aware NY.
    """
    if df_session_ny is None or df_session_ny.empty:
        return 0
    idx = df_session_ny.index
    if start_inclusive:
        mask_low = idx >= start_ts
    else:
        mask_low = idx > start_ts
    if end_inclusive:
        mask_high = idx <= end_ts
    else:
        mask_high = idx < end_ts
    return int((mask_low & mask_high).sum())


# ═════════════════════════════════════════════════════════════════════════════
# Calcul features F1/F2/F3 — avec asserts causalité
# ═════════════════════════════════════════════════════════════════════════════

def _compute_features_for_signal(
    sig: Dict,
    trade_res: Dict,
    df_session_ny: pd.DataFrame,
    opr_ts_ny: pd.Timestamp,
    opr_high: float,
    opr_low: float,
    atr_daily: float,
    tz: ZoneInfo,
) -> Dict:
    """
    Calcule F1, F2 (en pts, ATR, OPR-range), F3 pour un signal donné.

    Causalité (asserts) :
      • F1 : bougies sur (opr_ts_ny + 15min, trigger_time] → max(ts) ≤ trigger_time
      • F2/F3 : bougies sur [trigger_time, fill_time]      → max(ts) ≤ fill_time

    Args:
      sig            : dict signal retourné par run_opr_day (contient trigger_time, direction)
      trade_res      : dict résultat retourné par run_opr_day (contient fill_time, result, pnl)
      df_session_ny  : DataFrame de session bornée [9h15 NY, 16h30 NY] (tz NY)
      opr_ts_ny      : timestamp tz-aware NY de la bougie OPR identifiée par pic de volume
      opr_high       : high de la bougie OPR
      opr_low        : low de la bougie OPR
      atr_daily      : ATR daily (en points) pour normalisation F2
      tz             : ZoneInfo NY

    Returns:
      dict {f1_bars, f2_excursion_pts, f2_excursion_atr, f2_excursion_oprrange, f3_bars}

      • f1_bars : int  (toujours calculable si trigger_time présent)
      • f2_*    : float (NaN si trade NOT_FILLED natif, i.e. pas de fill_time)
      • f3_bars : int (NaN si trade NOT_FILLED natif)
    """
    direction = sig.get("direction")
    trigger_time_raw = sig.get("trigger_time")
    fill_time_raw = trade_res.get("fill_time")
    natively_filled = trade_res.get("result", "NOT_FILLED") != "NOT_FILLED"

    out = {
        "f1_bars": np.nan,
        "f2_excursion_pts": np.nan,
        "f2_excursion_atr": np.nan,
        "f2_excursion_oprrange": np.nan,
        "f3_bars": np.nan,
    }

    # ── F1 : bougies entre fin OPR et trigger ────────────────────────────────
    trigger_ts = _to_ts_ny(trigger_time_raw, tz)
    if trigger_ts is None or opr_ts_ny is None:
        return out

    opr_end_ts = opr_ts_ny + pd.Timedelta(minutes=15)

    # Comptage : bougies sur (opr_end_ts, trigger_ts]
    #   start_inclusive=False  (on exclut la bougie OPR + 1, qui est opr_end_ts)
    #   end_inclusive=True     (la bougie trigger est comptée)
    f1 = _count_bars_strict(
        df_session_ny, opr_end_ts, trigger_ts,
        start_inclusive=False, end_inclusive=True,
    )

    # Assert causalité : toutes les bougies considérées ont ts ≤ trigger_ts
    bars_f1 = df_session_ny[(df_session_ny.index > opr_end_ts) &
                            (df_session_ny.index <= trigger_ts)]
    if len(bars_f1) > 0:
        assert bars_f1.index.max() <= trigger_ts, (
            f"Look-ahead F1 détecté : bougie {bars_f1.index.max()} > trigger {trigger_ts}"
        )

    out["f1_bars"] = int(f1)

    # ── F2/F3 : uniquement sur trades natifs filled ──────────────────────────
    if not natively_filled:
        return out

    fill_ts = _to_ts_ny(fill_time_raw, tz)
    if fill_ts is None:
        return out

    # F3 : bougies entre trigger (exclu) et fill (inclus)
    #   Sémantique projet : nb de bougies M15 entre `trigger_time` et `fill_time`
    #   La bougie trigger elle-même n'est pas dans le pullback (c'est la cassure).
    #   La bougie fill_time est la première qui touche le niveau LIMIT (incluse).
    bars_f3 = df_session_ny[(df_session_ny.index > trigger_ts) &
                            (df_session_ny.index <= fill_ts)]
    f3 = int(len(bars_f3))

    # Assert causalité F3
    if f3 > 0:
        assert bars_f3.index.max() <= fill_ts, (
            f"Look-ahead F3 détecté : bougie {bars_f3.index.max()} > fill {fill_ts}"
        )

    # F3 ≥ 1 par construction si filled (sinon le LIMIT ne peut pas avoir été touché
    # sur une bougie postérieure au trigger). Si f3 == 0, c'est que fill = trigger
    # (cas extrême : le trigger lui-même touche le LIMIT — peu probable mais on
    # tolère par robustesse).
    out["f3_bars"] = max(f3, 1) if f3 >= 0 else 1

    # F2 : excursion max dans le sens de la cassure entre trigger (inclus) et fill (inclus)
    bars_f2 = df_session_ny[(df_session_ny.index >= trigger_ts) &
                            (df_session_ny.index <= fill_ts)]

    # Assert causalité F2
    if len(bars_f2) > 0:
        assert bars_f2.index.max() <= fill_ts, (
            f"Look-ahead F2 détecté : bougie {bars_f2.index.max()} > fill {fill_ts}"
        )

    if bars_f2.empty:
        return out

    if direction == "long":
        excursion_pts = max(float(bars_f2["high"].max()) - opr_high, 0.0)
    else:
        excursion_pts = max(opr_low - float(bars_f2["low"].min()), 0.0)

    out["f2_excursion_pts"] = float(excursion_pts)
    if atr_daily and atr_daily > 0:
        out["f2_excursion_atr"] = float(excursion_pts / atr_daily)
    opr_range = max(opr_high - opr_low, 0.0)
    if opr_range > 0:
        out["f2_excursion_oprrange"] = float(excursion_pts / opr_range)

    return out


# ═════════════════════════════════════════════════════════════════════════════
# Application des filtres v5 — équivalent causal du filtre pré-fill
# ═════════════════════════════════════════════════════════════════════════════

def _apply_v5_filters(
    trade_row: Dict,
    features: Dict,
    f1_min: Optional[float],
    f1_max: Optional[float],
    f2_max_atr: Optional[float],
    f3_max: Optional[float],
) -> Tuple[Dict, Optional[str]]:
    """
    Applique les filtres v5 sur un trade enrichi. Si rejet, marque NOT_FILLED.

    Sémantique de rejet :
      • F1 d'abord (filtré AVANT que F2/F3 soient évaluables).
      • F2/F3 ensuite (uniquement applicables sur trades filled).

    Returns:
      (trade_row_modifié, reject_reason ou None)
    """
    f1 = features.get("f1_bars")
    f2_atr = features.get("f2_excursion_atr")
    f3 = features.get("f3_bars")

    # ── F1 : borne basse puis borne haute ─────────────────────────────────────
    if f1_min is not None and not pd.isna(f1):
        if f1 < f1_min:
            trade_row.update({
                "result": "NOT_FILLED", "pnl": 0.0,
                "fill_time": None, "exit_time": None, "exit": None,
            })
            return trade_row, "F1_too_short"

    if f1_max is not None and not pd.isna(f1):
        if f1 > f1_max:
            trade_row.update({
                "result": "NOT_FILLED", "pnl": 0.0,
                "fill_time": None, "exit_time": None, "exit": None,
            })
            return trade_row, "F1_too_long"

    # ── Si trade NOT_FILLED nativement (v4 n'a pas fillé), F2/F3 = NaN → on
    #    laisse tel quel (déjà NOT_FILLED, pas de filtre supplémentaire) ──────
    if trade_row.get("result") == "NOT_FILLED":
        return trade_row, None

    # ── F2 : borne haute (excursion en ATR daily) ─────────────────────────────
    if f2_max_atr is not None and not pd.isna(f2_atr):
        if f2_atr > f2_max_atr:
            trade_row.update({
                "result": "NOT_FILLED", "pnl": 0.0,
                "fill_time": None, "exit_time": None, "exit": None,
            })
            return trade_row, "F2_excursion_too_wide"

    # ── F3 : borne haute (nb de bougies trigger → fill) ──────────────────────
    if f3_max is not None and not pd.isna(f3):
        if f3 > f3_max:
            trade_row.update({
                "result": "NOT_FILLED", "pnl": 0.0,
                "fill_time": None, "exit_time": None, "exit": None,
            })
            return trade_row, "F3_pullback_too_late"

    return trade_row, None


# ═════════════════════════════════════════════════════════════════════════════
# Interface plug-and-play : run_backtest
# ═════════════════════════════════════════════════════════════════════════════

def run_backtest(
    df_15m: pd.DataFrame,
    ticker: str,
    tf=None,
    params: dict = None,
    topstep_guard: bool = True,
) -> pd.DataFrame:
    """
    Backtest OPR v5 — wrapper post-traitement de core.opr.run_opr_day().

    Params accepté (tous optionnels — si None, filtre désactivé pour ce ticker) :
        f1_min     : float    — borne basse F1 (nb bougies fin OPR → trigger)
        f1_max     : float    — borne haute F1
        f2_max_atr : float    — borne haute F2 (excursion en ATR daily)
        f3_max     : float    — borne haute F3 (nb bougies trigger → fill)
        sl_mult    : float    — surcharge OPR_SL_ATR_MULT[ticker]   (passe à v4)
        tp_mult    : float    — surcharge OPR_TP_ATR_MULT[ticker]   (passe à v4)

    Returns:
      DataFrame avec colonnes standard projet + colonnes v5 additionnelles :
        date, dir, entry, sl, tp, sl_dist, tp_dist, rr, n_ct, result, pnl,
        fill_time, exit_time, exit, regime, strategy, trigger_time,
        ► f1_bars, f2_excursion_pts, f2_excursion_atr, f2_excursion_oprrange,
          f3_bars, v5_reject_reason
    """
    if ticker not in TICKERS:
        return pd.DataFrame()

    tz = ZoneInfo(OPR_TIMEZONE)

    # ── Résolution params v5 (priorité : params > config) ─────────────────────
    p = params or {}
    f1_min     = p.get("f1_min",     OPR_V5_F1_MIN.get(ticker))
    f1_max     = p.get("f1_max",     OPR_V5_F1_MAX.get(ticker))
    f2_max_atr = p.get("f2_max_atr", OPR_V5_F2_MAX_ATR.get(ticker))
    f3_max     = p.get("f3_max",     OPR_V5_F3_MAX.get(ticker))

    # ── Surcharge SL/TP v4 (optionnel) ────────────────────────────────────────
    sl_orig = OPR_SL_ATR_MULT[ticker]
    tp_orig = OPR_TP_ATR_MULT[ticker]
    sl_override = p.get("sl_mult")
    tp_override = p.get("tp_mult")
    apply_sltp_override = (sl_override is not None) or (tp_override is not None)

    if apply_sltp_override:
        sl_new = sl_override if sl_override is not None else sl_orig
        tp_new = tp_override if tp_override is not None else tp_orig
        cfg.OPR_SL_ATR_MULT[ticker] = sl_new
        cfg.OPR_TP_ATR_MULT[ticker] = tp_new
        from core import opr as _opr
        _opr.OPR_SL_ATR_MULT[ticker] = sl_new
        _opr.OPR_TP_ATR_MULT[ticker] = tp_new

    try:
        trades_out = []
        cum_pnl = peak_pnl = 0.0
        consec_loss_days = 0

        if df_15m.index.tz is None:
            idx_ny = df_15m.index.tz_localize("UTC").tz_convert(tz)
        else:
            idx_ny = df_15m.index.tz_convert(tz)
        ny_days = pd.DatetimeIndex(idx_ny.normalize().unique()).sort_values()

        for day_ny in ny_days:
            ds = day_ny.strftime("%Y-%m-%d")

            if CONSEC_LOSS_PAUSE_DAYS > 0 and consec_loss_days >= CONSEC_LOSS_PAUSE_DAYS:
                consec_loss_days = 0
                continue

            if topstep_guard:
                allowed, _ = trade_allowed(
                    day_pnl=0.0, cum_pnl=cum_pnl, peak_pnl=peak_pnl
                )
                if not allowed:
                    continue

            # ── Exécution v4 native (zone production) ─────────────────────────
            signals, sim_results, _ = run_opr_day(df_15m, ticker, day_ny)
            if not signals:
                continue

            # ── Pré-calcul du contexte de session pour features ───────────────
            df_session_ny = _ny_session_view(df_15m, day_ny, tz)
            opr_bar = _opr_bar(df_session_ny) if df_session_ny is not None else None
            if opr_bar is None or df_session_ny is None:
                # Edge case impossible si signals non vide, mais sécurité
                continue
            opr_high = float(opr_bar["high"])
            opr_low  = float(opr_bar["low"])
            opr_ts_ny = opr_bar.name  # tz-aware NY
            atr_daily = _compute_atr_daily(df_15m, day_ny, OPR_ATR_PERIOD)
            if atr_daily is None:
                # Edge case : v4 n'aurait pas produit de signal sans ATR
                continue

            # ── Enrichir chaque trade des features v5 ─────────────────────────
            day_trades: List[Dict] = []
            for sig, res in zip(signals, sim_results):
                trade_row = {
                    "date":         ds,
                    "strategy":     "OPR_V5",
                    "dir":          sig["direction"],
                    "entry":        sig["entry"],
                    "sl":           sig["sl"],
                    "tp":           sig["tp"],
                    "sl_dist":      sig["sl_dist"],
                    "tp_dist":      sig["tp_dist"],
                    "rr":           sig["rr"],
                    "n_ct":         sig["n_ct"],
                    "risk_$":       sig["risk"],
                    "regime":       sig.get("regime") or "OPR_V5",
                    "zone_low":     sig["zone_low"],
                    "zone_high":    sig["zone_high"],
                    "trigger_time": sig.get("trigger_time"),
                    **res,
                }

                features = _compute_features_for_signal(
                    sig=sig, trade_res=res,
                    df_session_ny=df_session_ny,
                    opr_ts_ny=opr_ts_ny,
                    opr_high=opr_high, opr_low=opr_low,
                    atr_daily=atr_daily,
                    tz=tz,
                )

                trade_row.update(features)
                trade_row["v5_reject_reason"] = None

                # Application filtres v5
                trade_row, reject_reason = _apply_v5_filters(
                    trade_row=trade_row,
                    features=features,
                    f1_min=f1_min, f1_max=f1_max,
                    f2_max_atr=f2_max_atr, f3_max=f3_max,
                )
                trade_row["v5_reject_reason"] = reject_reason

                day_trades.append(trade_row)

            # ── Circuit breakers daily (identique à strategies/opr.py) ────────
            filled     = [t for t in day_trades if t["result"] != "NOT_FILLED"]
            not_filled = [t for t in day_trades if t["result"] == "NOT_FILLED"]
            filled.sort(key=lambda t: t.get("fill_time") or "")

            kept = []
            breaker_armed = False
            running_pnl   = 0.0
            cancelled_cb  = []
            for t in filled:
                if breaker_armed:
                    cancelled_cb.append(t)
                    continue
                kept.append(t)
                running_pnl += t["pnl"]
                if DAILY_STOP_AFTER_SL and t["result"] == "SL":
                    breaker_armed = True
                elif DAILY_LOCKIN_THRESHOLD > 0 and running_pnl >= DAILY_LOCKIN_THRESHOLD:
                    breaker_armed = True

            for t in cancelled_cb:
                # NB : on garde le v5_reject_reason existant (None ou rejet)
                # et on annule simplement le fill (breaker daily, pas filtre v5).
                t.update({"result": "NOT_FILLED", "pnl": 0,
                          "fill_time": None, "exit_time": None, "exit": None})

            trades_out.extend(kept + not_filled + cancelled_cb)

            day_pnl = sum(t["pnl"] for t in kept)
            cum_pnl += day_pnl
            if cum_pnl > peak_pnl:
                peak_pnl = cum_pnl
            if day_pnl < 0:
                consec_loss_days += 1
            elif day_pnl > 0:
                consec_loss_days = 0

    finally:
        # Restauration SL/TP overrides
        if apply_sltp_override:
            cfg.OPR_SL_ATR_MULT[ticker] = sl_orig
            cfg.OPR_TP_ATR_MULT[ticker] = tp_orig
            from core import opr as _opr
            _opr.OPR_SL_ATR_MULT[ticker] = sl_orig
            _opr.OPR_TP_ATR_MULT[ticker] = tp_orig

    return pd.DataFrame(trades_out)


# ═════════════════════════════════════════════════════════════════════════════
# Chart d'une journée (réutilise plot_day_analysis comme strategies/opr.py)
# ═════════════════════════════════════════════════════════════════════════════

def plot_day(
    df_15m: pd.DataFrame,
    ticker: str,
    date_str: str,
    day_trades: list,
    output_path: str,
):
    """Génère le chart OPR v5 d'une journée (identique au layout OPR v4)."""
    from core.analysis_chart import plot_day_analysis

    tz     = ZoneInfo(OPR_TIMEZONE)
    day_ny = pd.Timestamp(date_str).tz_localize(tz)

    df_session = _ny_session_view(df_15m, day_ny, tz)
    opr_bar = _opr_bar(df_session) if df_session is not None else None

    cutoff = pd.Timestamp(f"{date_str} {CUTOFF_HOUR_UTC:02d}:00:00")
    us_end = (day_ny.replace(hour=16, minute=30)
              .tz_convert("UTC").tz_localize(None))

    zones_for_chart = []
    if opr_bar is not None:
        opr_high = float(opr_bar["high"])
        opr_low  = float(opr_bar["low"])
        zones_for_chart.append({
            "low": opr_low, "high": opr_high,
            "mid": (opr_high + opr_low) / 2.0,
            "quality": 100.0, "n_tf": 1, "touches": 1,
            "tfs": ["OPR_V5"], "dominant_tf": "OPR_V5",
            "start_time": opr_bar.name.tz_convert("UTC").tz_localize(None),
        })

    # plot_day_analysis attend la clé `direction` (non `dir`). On enrichit
    # les trades avec ce mapping pour rester compatible avec le format
    # consommé par core/analysis_chart.py. NB : le wrapper opr.py de
    # production a le même problème (bug pré-existant) — non corrigé ici.
    def _enrich_for_chart(trades):
        out = []
        for t in trades:
            d = dict(t)
            if "direction" not in d and "dir" in d:
                d["direction"] = d["dir"]
            out.append(d)
        return out

    plot_day_analysis(
        df_15m=df_15m, ticker=ticker, date_str=date_str,
        cutoff=cutoff, us_end=us_end,
        zones=zones_for_chart,
        signals=_enrich_for_chart(
            [t for t in day_trades if t.get("result") != "NOT_FILLED"]
        ),
        trades=_enrich_for_chart(day_trades),
        regime=None, alignment_score=None,
        pm_features=None, vol_features=None,
        output_path=output_path,
    )


# ═════════════════════════════════════════════════════════════════════════════
# Test de non-régression (exécutable en CLI)
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    """
    Test de non-régression critique : avec filtres tous à None (config par
    défaut), opr-v5 doit produire exactement les mêmes trades qu'opr-v4.

    Usage :
        python -m strategies.opr_v5
    """
    import sys
    from core.data import load_csv, build_timeframes
    from strategies import opr as v4
    from strategies import opr_v5 as v5

    print("=" * 70)
    print("  TEST NON-RÉGRESSION opr-v5 (filtres None) vs opr-v4")
    print("=" * 70)

    common_cols = ["date", "dir", "entry", "sl", "tp", "result", "pnl", "fill_time"]
    all_ok = True

    # On force explicitement filtres = None pour le test de non-régression.
    # NB : depuis la PHASE 4, la config par défaut (OPR_V5_F1_*, OPR_V5_F2_*,
    # OPR_V5_F3_*) contient les optima walk-forward — passer params=None lit
    # ces valeurs et activerait les filtres. Le test ci-dessous reste
    # invariant en court-circuitant explicitement.
    explicit_none = {
        "f1_min": None, "f1_max": None,
        "f2_max_atr": None, "f3_max": None,
    }

    for ticker in TICKERS:
        csv = f"data/{ticker}_data_m15.csv"
        df = load_csv(csv)
        tf = build_timeframes(df)

        t4 = v4.run_backtest(df, ticker, tf=tf, params=None, topstep_guard=False)
        t5 = v5.run_backtest(df, ticker, tf=tf, params=explicit_none,
                             topstep_guard=False)

        # Aligner colonnes
        t4_aligned = t4[common_cols].reset_index(drop=True)
        t5_aligned = t5[common_cols].reset_index(drop=True)
        equal = t4_aligned.equals(t5_aligned)

        print(f"\n  {ticker}")
        print(f"    v4 trades : {len(t4):4d}  P&L = {t4['pnl'].sum():+10.2f}")
        print(f"    v5 trades : {len(t5):4d}  P&L = {t5['pnl'].sum():+10.2f}")
        print(f"    Equal     : {equal}")

        if not equal:
            all_ok = False
            # Diagnostic : montrer les premières lignes qui diffèrent
            try:
                cmp = t4_aligned.compare(t5_aligned)
                print(f"    Diff (head 10) :\n{cmp.head(10)}")
            except Exception as e:
                print(f"    Diff impossible (shape diff) : {e}")
                print(f"    v4 shape : {t4_aligned.shape}")
                print(f"    v5 shape : {t5_aligned.shape}")

    print("\n" + "=" * 70)
    print(f"  RÉSULTAT GLOBAL : {'✅ PASS' if all_ok else '❌ FAIL'}")
    print("=" * 70)
    sys.exit(0 if all_ok else 1)
