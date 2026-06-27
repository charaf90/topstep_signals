"""Stratégie `opr-nq1` — Candidat A FIGÉ (PREREGISTRE_2026Q3), config LIVE TRADING.

⚠️ NOM DE FICHIER HISTORIQUE (`opr_nq1_shadow.py`) — ce module n'est PLUS du shadow :
il encode la config OPR-NQ1 causal-matinal réellement CÂBLÉE EN TRADING (décision
utilisateur ferme, override du verdict 🔴). Source de vérité UNIQUE du design, réutilisée
par le wrapper live `core/opr_nq1_causal.py` ET par `tools/backtest_vs_live.py` → fidélité
backtest↔live par construction (même code).

═══════════════════════════════════════════════════════════════════════════════
DESIGN FIGÉ (ne pas re-tuner — interdiction de p-hacking sur ≤ 2026-06)
═══════════════════════════════════════════════════════════════════════════════
  1. Base = `strategies.opr_v5_1.run_backtest` avec F2 DÉSACTIVÉ
     (f2_min_atr=None, f2_max_atr=None, f1/f3=None) → causal, no leak, == opr-v4 nu.
  2. SL/TP custom : sl_mult = OPR_NQ1_SL_MULT (0.225 ×ATR), tp_mult = OPR_NQ1_TP_MULT
     (0.3375 → r_target 1.5).
  3. Filtre horaire STRUCTUREL : on ne RETIENT un fill que si l'heure NY du FILL ∈
     [OPR_NQ1_ENTRY_HOUR_START_NY, OPR_NQ1_ENTRY_HOUR_END_NY) (défaut [9, 12)). Mirror
     live = annulation de tout pending non rempli à 12:00 NY.
  4. Sizing dédié : risk = OPR_NQ1_RISK_USD ($150) par trade — JAMAIS le nominal global.
     n_ct re-calculé, P&L re-scalé proportionnellement (le blow-through est PAR CONTRAT →
     le risk plus petit le scale linéairement = levier anti-blow-through).
  5. CIRCUIT-BREAKER quotidien : dans chaque jour NY (ordre de fill), on coupe dès que le
     cumul P&L re-sizé ≤ −OPR_NQ1_DAILY_LOSS_BREAKER_USD ($500). Le trade qui franchit le
     seuil est compté ; tous les suivants du jour sont coupés (NOT_FILLED).

Réf validation M1 : brouillon/scripts/opr_nq1_circuit_breaker.py (risk 150 / breaker 500
→ pire jour −$775, trailing −$1,384, breach daily 0 %, P(target) 96 %).

Les trades écartés par (3) ou (5) sont marqués NOT_FILLED (motif dans `opr_nq1_reject_reason`),
comme un filtre causal pré-fill. Tout paramètre vit dans config.py. Heures NY DST-aware
(zoneinfo). Seed projet.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

# Reproductibilité (convention projet)
np.random.seed(42)

from config import (
    INSTRUMENTS,
    OPR_NQ1_DAILY_LOSS_BREAKER_USD,
    OPR_NQ1_ENTRY_HOUR_END_NY,
    OPR_NQ1_ENTRY_HOUR_START_NY,
    OPR_NQ1_RISK_USD,
    OPR_NQ1_SL_MULT,
    OPR_NQ1_TICKER,
    OPR_NQ1_TP_MULT,
    OPR_TIMEZONE,
)
from strategies.opr_v5_1 import run_backtest as _opr_v5_1_run_backtest

# ── Identité de la stratégie ─────────────────────────────────────────────────
STRATEGY_ID = "opr-nq1"
TICKERS = [OPR_NQ1_TICKER]  # ["NQ1"]
CSV_SUFFIX = "_opr_nq1"
CSV_TIMEFRAME = "m15"

# ── Contrat moteur M1 (core/bt_engine) — calqué sur opr-v5.1 ─────────────────
SIGNAL_TF = "15min"  # OPR natif M15 (reconstruit depuis le M1)
INTRADAY_DAYCLOSE = True  # OPR ferme en fin de séance (jamais overnight)
SESSION_END_MIN = 16 * 60 + 30  # 16:30 NY (clôture US OPR)
RUN_MIN_WINDOW = (9 * 60, 16 * 60 + 31)  # session RTH NY
_TF_DELTA_OPR = pd.Timedelta(minutes=15)

# ── Grille d'optimisation : VIDE — design FIGÉ, aucune optimisation autorisée ──
PARAM_GRID: dict = {}

# ── Params figés Candidat A : F2 OFF + SL/TP custom ──────────────────────────
_OPR_NQ1_BASE_PARAMS = {
    "f1_min": None,
    "f1_max": None,
    "f2_min_atr": None,  # F2 OFF (borne basse) → causal
    "f2_max_atr": None,  # F2 OFF (borne haute)
    "f3_max": None,
    "sl_mult": OPR_NQ1_SL_MULT,  # 0.225
    "tp_mult": OPR_NQ1_TP_MULT,  # 0.3375
}

_NOT_FILLED = {
    "result": "NOT_FILLED",
    "pnl": 0.0,
    "fill_time": None,
    "exit_time": None,
    "exit": None,
}


def _fill_hour_ny(ts_raw, tz: ZoneInfo) -> int | None:
    """Heure NY (0-23) du FILL. None si timestamp absent/illisible.

    Réplique la convention du script de validation pré-enregistré (PREREGISTRE_2026Q3,
    brouillon/scripts/opr_nq1_finalists_validate.py) : le filtre horaire structurel
    porte sur l'heure NY du FILL (entrée), PAS du trigger — « entrées uniquement 9-12h
    NY / annuler tout pending non rempli à 12:00 NY ». Robuste au format de fill_time :
    NY tz-aware (chemin M15 natif) → tz_convert ; naïf → supposé UTC puis converti NY
    (convention codebase + script de validation `tz_localize("UTC").tz_convert(NY)`).
    """
    if ts_raw is None:
        return None
    try:
        if isinstance(ts_raw, float) and pd.isna(ts_raw):
            return None
    except (TypeError, ValueError):
        pass
    try:
        ts = pd.Timestamp(ts_raw)
    except (ValueError, TypeError):
        return None
    if ts is None or pd.isna(ts):
        return None
    ts = ts.tz_localize("UTC").tz_convert(tz) if ts.tz is None else ts.tz_convert(tz)
    return int(ts.hour)


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
    """Backtest Candidat A — post-traitement figé de opr-v5.1 (F2 off + SL/TP custom).

    Étapes (toutes causales, appliquées sur les trades opr-v5.1 déjà résolus) :
      1. base opr-v5.1 avec _OPR_NQ1_BASE_PARAMS ;
      2. filtre horaire [start, end) NY sur le FILL → hors fenêtre = NOT_FILLED ;
      3. re-sizing risk dédié ($150) + re-scaling du P&L ;
      4. circuit-breaker quotidien −$X (ordre de fill) → trades suivants = NOT_FILLED.

    `params` est ignoré (design FIGÉ) — accepté pour compat de signature registry/tools.
    """
    if ticker not in TICKERS:
        return pd.DataFrame()

    tz = ZoneInfo(OPR_TIMEZONE)

    base = _opr_v5_1_run_backtest(
        df_15m, ticker, tf=tf, params=dict(_OPR_NQ1_BASE_PARAMS), topstep_guard=topstep_guard
    )
    if base is None or len(base) == 0:
        return base if base is not None else pd.DataFrame()

    df = base.copy().reset_index(drop=True)
    if "opr_nq1_reject_reason" not in df.columns:
        df["opr_nq1_reject_reason"] = None
    df["strategy"] = "OPR_NQ1"

    dpp = float(INSTRUMENTS[ticker]["dollar_per_point"])
    h0 = int(OPR_NQ1_ENTRY_HOUR_START_NY)
    h1 = int(OPR_NQ1_ENTRY_HOUR_END_NY)
    breaker = OPR_NQ1_DAILY_LOSS_BREAKER_USD

    def _mark_not_filled(idx: int, reason: str):
        for k, v in _NOT_FILLED.items():
            if k in df.columns:
                df.at[idx, k] = v
        df.at[idx, "opr_nq1_reject_reason"] = reason

    # ── 1. Filtre horaire structurel sur le FILL (heure NY ∈ [h0, h1)) ───────
    # Canonical : « entrées uniquement 9-12h NY » = le FILL doit tomber dans la fenêtre
    # (mirror live = annuler tout pending non rempli à 12:00 NY). Filtrer le trigger
    # laisserait passer des fills d'après-midi (limite retouchée tard) → faux.
    for i in df.index:
        if df.at[i, "result"] == "NOT_FILLED":
            continue
        hr = _fill_hour_ny(df.at[i, "fill_time"] if "fill_time" in df.columns else None, tz)
        if hr is None or not (h0 <= hr < h1):
            _mark_not_filled(i, "outside_entry_window")

    # ── 2. Re-sizing risk dédié ($150) + re-scaling P&L (avant le breaker) ───
    # Le breaker borne le CUMUL re-sizé du jour → le re-sizing doit précéder.
    for i in df.index:
        if df.at[i, "result"] == "NOT_FILLED":
            continue
        sl_dist = float(df.at[i, "sl_dist"]) if df.at[i, "sl_dist"] else 0.0
        if sl_dist <= 0:
            continue
        n_old = int(df.at[i, "n_ct"]) if df.at[i, "n_ct"] else 0
        n_new = max(1, int(OPR_NQ1_RISK_USD / (sl_dist * dpp)))
        df.at[i, "n_ct"] = n_new
        if "risk_$" in df.columns:
            df.at[i, "risk_$"] = float(n_new * sl_dist * dpp)
        if n_old > 0 and "pnl" in df.columns:
            df.at[i, "pnl"] = float(df.at[i, "pnl"]) * (n_new / n_old)

    # ── 3. Circuit-breaker quotidien −$X (cumul re-sizé, ordre de fill) ───────
    # Mirror du script de validation : dans chaque jour NY, on accumule le P&L re-sizé
    # dans l'ordre de fill ; dès que cum ≤ −X le trade DÉCLENCHEUR est gardé (compté) et
    # tous les trades SUIVANTS du jour sont coupés (NOT_FILLED). En live, le daemon
    # applique le même seuil sur le cumul réalisé OPR-NQ1 du jour.
    if breaker is not None and breaker > 0:
        filled = df[df["result"] != "NOT_FILLED"].copy()
        if not filled.empty:
            sort_cols = ["date"] + (["fill_time"] if "fill_time" in df.columns else [])
            filled = filled.sort_values(sort_cols, kind="stable")
            for _day, grp in filled.groupby("date", sort=False):
                cum = 0.0
                stopped = False
                for i in grp.index:
                    if stopped:
                        _mark_not_filled(i, "daily_breaker")
                        continue
                    cum += float(df.at[i, "pnl"])
                    if cum <= -float(breaker):
                        stopped = True  # ce trade est compté ; les suivants du jour coupés

    return df


# ═════════════════════════════════════════════════════════════════════════════
# Contrat moteur M1 — emit_signals (core/bt_engine)
# ═════════════════════════════════════════════════════════════════════════════


def emit_signals(sig_df: pd.DataFrame, ticker: str, params: dict | None = None) -> list[dict]:
    """Émet les ordres Candidat A pour le moteur M1 (core/bt_engine).

    Réutilise `run_backtest` ci-dessus (filtre horaire + re-sizing + breaker déjà appliqués)
    et extrait les ordres RÉELLEMENT remplis → core/bt_engine résout fill + SL/TP à la
    minute (vérité de fill, P&L NET). Mirror exact de opr_v5_1.emit_signals.
    """
    trades = run_backtest(sig_df, ticker, params=params, topstep_guard=False)
    if trades is None or len(trades) == 0:
        return []
    arms: list[dict] = []
    for t in trades.to_dict("records"):
        if t.get("result") == "NOT_FILLED" or not t.get("fill_time"):
            continue
        fill_ts = pd.Timestamp(t["fill_time"])
        if fill_ts.tzinfo is not None:  # OPR peut renvoyer un ts NY tz-aware
            fill_ts = fill_ts.tz_convert("UTC").tz_localize(None)
        entry, sl, tp = float(t["entry"]), float(t["sl"]), float(t["tp"])
        arms.append(
            {
                "dir": t.get("dir") or t.get("direction"),
                "entry": entry,
                "sl": sl,
                "tp": tp,
                "sl_dist": float(t.get("sl_dist") or abs(entry - sl)),
                "tp_dist": float(t.get("tp_dist") or abs(entry - tp)),
                "rr": float(t.get("rr") or 0.0),
                "n_ct": int(t["n_ct"]),
                "regime": t.get("regime") or "OPR_NQ1",
                "arm_ts": fill_ts,
                "place_ts": fill_ts - _TF_DELTA_OPR,
                "timeout_ts": fill_ts + _TF_DELTA_OPR,
            }
        )
    return arms
