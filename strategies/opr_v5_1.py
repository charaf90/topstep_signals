"""
Stratégie `opr-v5.1` — extension d'opr-v5 avec filtre INFÉRIEUR data-driven `f2_min_atr`.

Contexte :
  L'analyse data science approfondie (output/data_science_opr_v5.md, 1051 lignes)
  a mis en évidence un pattern majeur que le PARAM_GRID v5 n'avait pas testé :
  un filtre INFÉRIEUR sur F2 (`f2_excursion_atr ≥ ~0.15`). Les preuves de
  robustesse :
    • Grid search univarié : seuil f2≥0.15 → PF passe de 1.56 à 2.33 (kept_pnl conservé)
    • Random Forest importance : F2 dominante (0.69)
    • Logistic Regression : coef standardisé +0.55 sur F2 (effet positif P(win))
    • Decision Tree depth=2 (F2 seul) : trouve seuil 0.10-0.15 sans hint
    • Permutation test 10 000 itér : p < 0.0001 sur ALL/NQ1/YM1
    • Win Rate gap : ALL +22.6pp, NQ1 +20.5pp, YM1 +31.0pp
  Caveat MES1 : p=0.23, gap WR seulement +6.5pp → on s'attend à f2_min=None.

Architecture (identique v5) :
  Wrapper de `core.opr.run_opr_day()` (zone production protégée — ne pas
  modifier). On exécute v4 nativement puis on enrichit le DataFrame de trades
  des colonnes `f1_bars`, `f2_excursion_atr`, `f2_excursion_oprrange`,
  `f2_excursion_pts`, `f3_bars`. Quand un filtre rejette un trade, on le
  marque NOT_FILLED (équivalent causal au filtre pré-fill).

Nouveauté v5.1 :
  • Ajout d'un filtre `f2_min_atr` (borne INFÉRIEURE sur F2) — appliqué AVANT
    `f2_max_atr` dans `_apply_v5_1_filters`.
  • Nouveau motif de rejet : `F2_excursion_too_narrow`.

Causalité (zero look-ahead, hérité v5) :
  F1 utilise bougies sur (opr_ts_ny + 15min, trigger_time]   → tous ≤ trigger
  F2/F3 utilisent bougies sur [trigger_time, fill_time]      → tous ≤ fill
  Asserts algorithmiques dans _compute_features_for_signal (réutilisé tel quel).

Note frictions : core/opr.py retourne du P&L brut. opr-v5.1 conserve ce régime
pour comparaison apples-to-apples avec opr-v4 et opr-v5.

Voir config.py section "STRATÉGIE OPR v5.1" pour les paramètres.
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
    OPR_V5_1_STRATEGY_VERSION, OPR_V5_1_TICKERS,
    OPR_V5_1_F1_MIN, OPR_V5_1_F1_MAX,
    OPR_V5_1_F2_MIN_ATR, OPR_V5_1_F2_MAX_ATR,
    OPR_V5_1_F3_MAX,
    CUTOFF_HOUR_UTC,
    DAILY_STOP_AFTER_SL, CONSEC_LOSS_PAUSE_DAYS, DAILY_LOCKIN_THRESHOLD,
)
from core.opr import run_opr_day, _ny_session_view, _opr_bar, _compute_atr_daily
from core.risk_topstep import trade_allowed

# Réutilisation des helpers v5 (zone recherche, pas modifiée — strategies/opr_v5.py)
from strategies.opr_v5 import _compute_features_for_signal


# ── Identité de la stratégie ─────────────────────────────────────────────────
STRATEGY_ID   = OPR_V5_1_STRATEGY_VERSION          # "opr-v5.1"
TICKERS       = list(OPR_V5_1_TICKERS)             # ["MES1", "NQ1", "YM1"]
CSV_SUFFIX    = "_opr_v5_1"
CSV_TIMEFRAME = "m15"

# ── Grille d'optimisation (walk-forward via core/optimizer.py) ───────────────
# Grille RESSERRÉE par décision méthodologique :
#   • f2_min_atr (NOUVEAU) est la dimension centrale à valider par walk-forward.
#     Valeurs : None / 0.10 / 0.13 / 0.15 / 0.18
#     (0.10 = seuil DTree ALL, 0.13 = seuil DTree NQ1 + grid search NQ1 optimum,
#      0.15 = seuil DTree YM1 + grid search YM1 optimum, 0.18 = test +stringent)
#
#   • f1_max et f2_max_atr et f3_max : on RÉUTILISE les optima v5 walk-forward
#     déjà validés (cf config OPR_V5_F1_MAX / OPR_V5_F2_MAX_ATR / OPR_V5_F3_MAX),
#     comme baseline figée :
#         MES1 : f1_max=None, f2_max=None, f3_max=None  (v5 = v4 sur MES1)
#         NQ1  : f1_max=None, f2_max=0.5,  f3_max=None
#         YM1  : f1_max=10,   f2_max=1.0,  f3_max=None
#
#     Cette décision évite l'explosion combinatoire (3×5×3×3=135 → trop long)
#     et empêche un curve-fitting double : on isole strictement le test du
#     filtre data-driven f2_min_atr.
#
# Grille effective : 5 valeurs sur 1 dimension → 5 combos / ticker → 15 total
# Bonferroni : p_seuil = 0.05 / 5 = 0.01 → bootstrap ≥ 99 % requis.
#
# IMPORTANT : core/optimizer.py utilise PARAM_GRID pour ALL les tickers.
# Pour respecter les optima v5 figés ci-dessus, on passe une grille avec
# UNIQUEMENT f2_min_atr ; les autres params sont lus dans config par ticker
# (OPR_V5_1_F1_MAX, OPR_V5_1_F2_MAX_ATR, OPR_V5_1_F3_MAX) — ces dicts sont
# définis pour reprendre les optima v5 par ticker (cf config.py).
PARAM_GRID = {
    "f2_min_atr": [None, 0.10, 0.13, 0.15, 0.18],
}


# ═════════════════════════════════════════════════════════════════════════════
# Application des filtres v5.1 — extension du filtre v5 avec f2_min_atr
# ═════════════════════════════════════════════════════════════════════════════

def _apply_v5_1_filters(
    trade_row: Dict,
    features: Dict,
    f1_min: Optional[float],
    f1_max: Optional[float],
    f2_min_atr: Optional[float],
    f2_max_atr: Optional[float],
    f3_max: Optional[float],
) -> Tuple[Dict, Optional[str]]:
    """
    Applique les filtres v5.1 sur un trade enrichi. Si rejet, marque NOT_FILLED.

    Sémantique de rejet (ordre strict) :
      1. F1 borne basse  (F1_too_short)
      2. F1 borne haute  (F1_too_long)
      3. F2 borne basse  (F2_excursion_too_narrow) — NOUVEAU v5.1
      4. F2 borne haute  (F2_excursion_too_wide)   — hérité v5
      5. F3 borne haute  (F3_pullback_too_late)    — hérité v5

    F2/F3 ne s'appliquent que sur trades natifs filled (sinon F2/F3 = NaN).

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

    # ── F2 : borne BASSE (NOUVEAU v5.1) — appliquée AVANT borne haute ────────
    if f2_min_atr is not None and not pd.isna(f2_atr):
        if f2_atr < f2_min_atr:
            trade_row.update({
                "result": "NOT_FILLED", "pnl": 0.0,
                "fill_time": None, "exit_time": None, "exit": None,
            })
            return trade_row, "F2_excursion_too_narrow"

    # ── F2 : borne haute (excursion en ATR daily) — hérité v5 ────────────────
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
    Backtest OPR v5.1 — wrapper post-traitement de core.opr.run_opr_day().

    Params accepté (tous optionnels — si None, filtre désactivé pour ce ticker) :
        f1_min     : float    — borne basse F1
        f1_max     : float    — borne haute F1
        f2_min_atr : float    — borne basse F2 (excursion en ATR daily)  [NOUVEAU v5.1]
        f2_max_atr : float    — borne haute F2
        f3_max     : float    — borne haute F3
        sl_mult    : float    — surcharge OPR_SL_ATR_MULT[ticker]  (passe à v4)
        tp_mult    : float    — surcharge OPR_TP_ATR_MULT[ticker]  (passe à v4)

    Returns:
      DataFrame avec colonnes standard projet + colonnes v5/v5.1 additionnelles :
        date, dir, entry, sl, tp, sl_dist, tp_dist, rr, n_ct, result, pnl,
        fill_time, exit_time, exit, regime, strategy, trigger_time,
        ► f1_bars, f2_excursion_pts, f2_excursion_atr, f2_excursion_oprrange,
          f3_bars, v5_reject_reason
        (v5_reject_reason peut contenir le nouveau motif F2_excursion_too_narrow)
    """
    if ticker not in TICKERS:
        return pd.DataFrame()

    tz = ZoneInfo(OPR_TIMEZONE)

    # ── Résolution params v5.1 (priorité : params > config) ──────────────────
    p = params or {}
    f1_min     = p.get("f1_min",     OPR_V5_1_F1_MIN.get(ticker))
    f1_max     = p.get("f1_max",     OPR_V5_1_F1_MAX.get(ticker))
    f2_min_atr = p.get("f2_min_atr", OPR_V5_1_F2_MIN_ATR.get(ticker))  # NOUVEAU
    f2_max_atr = p.get("f2_max_atr", OPR_V5_1_F2_MAX_ATR.get(ticker))
    f3_max     = p.get("f3_max",     OPR_V5_1_F3_MAX.get(ticker))

    # ── Surcharge SL/TP v4 (optionnel) ───────────────────────────────────────
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
                continue
            opr_high = float(opr_bar["high"])
            opr_low  = float(opr_bar["low"])
            opr_ts_ny = opr_bar.name  # tz-aware NY
            atr_daily = _compute_atr_daily(df_15m, day_ny, OPR_ATR_PERIOD)
            if atr_daily is None:
                continue

            # ── Enrichir chaque trade des features v5 (réutilisé tel quel) ────
            day_trades: List[Dict] = []
            for sig, res in zip(signals, sim_results):
                trade_row = {
                    "date":         ds,
                    "strategy":     "OPR_V5_1",
                    "dir":          sig["direction"],
                    "entry":        sig["entry"],
                    "sl":           sig["sl"],
                    "tp":           sig["tp"],
                    "sl_dist":      sig["sl_dist"],
                    "tp_dist":      sig["tp_dist"],
                    "rr":           sig["rr"],
                    "n_ct":         sig["n_ct"],
                    "risk_$":       sig["risk"],
                    "regime":       sig.get("regime") or "OPR_V5_1",
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

                # Application filtres v5.1 (5 motifs possibles, dont F2_excursion_too_narrow)
                trade_row, reject_reason = _apply_v5_1_filters(
                    trade_row=trade_row,
                    features=features,
                    f1_min=f1_min, f1_max=f1_max,
                    f2_min_atr=f2_min_atr,
                    f2_max_atr=f2_max_atr,
                    f3_max=f3_max,
                )
                trade_row["v5_reject_reason"] = reject_reason

                day_trades.append(trade_row)

            # ── Circuit breakers daily (identique v5) ────────────────────────
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
        if apply_sltp_override:
            cfg.OPR_SL_ATR_MULT[ticker] = sl_orig
            cfg.OPR_TP_ATR_MULT[ticker] = tp_orig
            from core import opr as _opr
            _opr.OPR_SL_ATR_MULT[ticker] = sl_orig
            _opr.OPR_TP_ATR_MULT[ticker] = tp_orig

    return pd.DataFrame(trades_out)


# ═════════════════════════════════════════════════════════════════════════════
# Chart d'une journée (réutilise plot_day_analysis)
# ═════════════════════════════════════════════════════════════════════════════

def plot_day(
    df_15m: pd.DataFrame,
    ticker: str,
    date_str: str,
    day_trades: list,
    output_path: str,
):
    """Génère le chart OPR v5.1 d'une journée (layout identique OPR v4/v5)."""
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
            "tfs": ["OPR_V5_1"], "dominant_tf": "OPR_V5_1",
            "start_time": opr_bar.name.tz_convert("UTC").tz_localize(None),
        })

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
    Test de non-régression critique : avec filtres tous explicitement à None,
    opr-v5.1 doit produire exactement les mêmes trades qu'opr-v4 et opr-v5
    (avec filtres None).

    Usage :
        python -m strategies.opr_v5_1
    """
    import sys
    from core.data import load_csv, build_timeframes
    from strategies import opr as v4
    from strategies import opr_v5 as v5
    from strategies import opr_v5_1 as v51

    print("=" * 70)
    print("  TEST NON-RÉGRESSION opr-v5.1 (filtres None) vs v4 et v5")
    print("=" * 70)

    common_cols = ["date", "dir", "entry", "sl", "tp", "result", "pnl", "fill_time"]
    all_ok = True

    explicit_none_v5 = {
        "f1_min": None, "f1_max": None,
        "f2_max_atr": None, "f3_max": None,
    }
    explicit_none_v51 = {
        "f1_min": None, "f1_max": None,
        "f2_min_atr": None, "f2_max_atr": None, "f3_max": None,
    }

    for ticker in TICKERS:
        csv = f"data/{ticker}_data_m15.csv"
        df = load_csv(csv)
        tf = build_timeframes(df)

        t4 = v4.run_backtest(df, ticker, tf=tf, params=None, topstep_guard=False)
        t5 = v5.run_backtest(df, ticker, tf=tf, params=explicit_none_v5,
                             topstep_guard=False)
        t51 = v51.run_backtest(df, ticker, tf=tf, params=explicit_none_v51,
                               topstep_guard=False)

        t4_aligned  = t4[common_cols].reset_index(drop=True)
        t5_aligned  = t5[common_cols].reset_index(drop=True)
        t51_aligned = t51[common_cols].reset_index(drop=True)

        equal_4_51 = t4_aligned.equals(t51_aligned)
        equal_5_51 = t5_aligned.equals(t51_aligned)

        print(f"\n  {ticker}")
        print(f"    v4    trades : {len(t4):4d}  P&L = {t4['pnl'].sum():+10.2f}")
        print(f"    v5    trades : {len(t5):4d}  P&L = {t5['pnl'].sum():+10.2f}")
        print(f"    v5.1  trades : {len(t51):4d}  P&L = {t51['pnl'].sum():+10.2f}")
        print(f"    v4 == v5.1   : {equal_4_51}")
        print(f"    v5 == v5.1   : {equal_5_51}")

        if not equal_4_51:
            all_ok = False
            try:
                cmp = t4_aligned.compare(t51_aligned)
                print(f"    Diff v4 vs v5.1 (head 10) :\n{cmp.head(10)}")
            except Exception as e:
                print(f"    Diff impossible : {e}")
                print(f"    v4 shape : {t4_aligned.shape}")
                print(f"    v5.1 shape : {t51_aligned.shape}")

        if not equal_5_51:
            all_ok = False
            try:
                cmp = t5_aligned.compare(t51_aligned)
                print(f"    Diff v5 vs v5.1 (head 10) :\n{cmp.head(10)}")
            except Exception as e:
                print(f"    Diff impossible : {e}")

    print("\n" + "=" * 70)
    print(f"  RÉSULTAT GLOBAL : {'✅ PASS' if all_ok else '❌ FAIL'}")
    print("=" * 70)
    sys.exit(0 if all_ok else 1)
