"""core/opr_nq1_causal.py — Signal LIVE OPR NQ1 causal-matinal (Candidat A, verdict 🔴).

═══════════════════════════════════════════════════════════════════════════════
RÔLE
═══════════════════════════════════════════════════════════════════════════════
Wrapper live du Candidat A pré-enregistré (strategie_futur/PREREGISTRE_2026Q3.md),
câblé en TRADING RÉEL sur décision utilisateur ferme (override du verdict 🔴). Chemin
SÉPARÉ d'opr-v5.1 (YM1) : strategy="OPR_NQ1", tag "OPRNQ1_…". NQ1 N'EST PAS dans
OPR_V5_1_LIVE_TICKERS.

Design (identique au backtest strategies/opr_nq1_shadow.py → fidélité par construction) :
  • F2 OFF : on appelle directement core.opr.run_opr_day (= opr-v4 nu, causal, no leak) ;
    AUCUN filtre F2 (ni borne basse "schéma A", ni borne haute).
  • SL/TP custom : sl_mult=OPR_NQ1_SL_MULT (0.225), tp_mult=OPR_NQ1_TP_MULT (0.3375) —
    injectés via override TEMPORAIRE des dicts config OPR_SL/TP_ATR_MULT["NQ1"] autour de
    l'appel run_opr_day (restauré dans `finally`). C'est EXACTEMENT le mécanisme du backtest
    (strategies.opr_v5_1.run_backtest) → mêmes prix SL/TP par construction. La clé "NQ1"
    n'est utilisée par AUCUN chemin OPR live (NQ1 hors univers prod) → override sans effet
    de bord sur opr-v5.1 (YM1, clé séparée) ni sur la prod. Daemon mono-thread → pas de
    réentrance.
  • Filtre horaire : on n'ÉMET un pending QUE si l'heure NY courante ∈
    [OPR_NQ1_ENTRY_HOUR_START_NY, OPR_NQ1_ENTRY_HOUR_END_NY) (défaut [9, 12)). L'annulation
    des pendings non remplis à 12:00 NY est gérée côté SessionRunner.
  • Sizing dédié : risk = OPR_NQ1_RISK_USD ($150) — n_ct/risk/gain recalculés.

Le circuit-breaker quotidien −$OPR_NQ1_DAILY_LOSS_BREAKER_USD est appliqué côté
SessionRunner (suivi du cumul réalisé OPR-NQ1 du jour), pas ici.

Idempotence : sans état. Le SessionRunner dédoublonne via `_already_placed(tag)`.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

# Reproductibilité (convention projet)
np.random.seed(42)

import config as cfg
from config import (
    INSTRUMENTS,
    OPR_NQ1_ENABLED,
    OPR_NQ1_ENTRY_HOUR_END_NY,
    OPR_NQ1_ENTRY_HOUR_START_NY,
    OPR_NQ1_RISK_USD,
    OPR_NQ1_SL_MULT,
    OPR_NQ1_TICKER,
    OPR_NQ1_TP_MULT,
    OPR_TIMEZONE,
)
from core import opr as _opr
from core.opr import run_opr_day

_NY_TZ = ZoneInfo(OPR_TIMEZONE)


def in_entry_window(now_utc: pd.Timestamp | None) -> bool:
    """Vrai si l'heure NY courante ∈ [START, END) — fenêtre d'émission des entrées."""
    import datetime as _dt

    now_utc = now_utc or _dt.datetime.utcnow()
    now_ny = now_utc.replace(tzinfo=_dt.UTC).astimezone(_NY_TZ)
    return OPR_NQ1_ENTRY_HOUR_START_NY <= now_ny.hour < OPR_NQ1_ENTRY_HOUR_END_NY


def get_opr_nq1_live_signal(
    df_15m: pd.DataFrame,
    day_ny: pd.Timestamp,
    now_utc,
) -> dict | None:
    """Signal live OPR NQ1 causal-matinal (F2 off + SL/TP custom + sizing $150).

    Args:
        df_15m  : DataFrame M15 (bars FERMÉES uniquement — causalité).
        day_ny  : Timestamp tz-aware NY à 00:00 (jour à jouer).
        now_utc : datetime UTC courant (pour le filtre horaire d'émission).

    Returns:
        dict signal "à placer" (avec tag, strategy="OPR_NQ1") si un trigger NQ1 causal est
        actif dans la fenêtre [9,12) NY et pas encore résolu ; sinon None. Aucun effet de
        bord (pas d'ordre, pas de RM).
    """
    if not OPR_NQ1_ENABLED:
        return None

    ticker = OPR_NQ1_TICKER

    # Filtre horaire d'ÉMISSION : aucun nouveau pending hors [9,12) NY.
    if not in_entry_window(now_utc):
        return None

    # ── Override TEMPORAIRE des mults SL/TP pour NQ1 (restauré en finally) ───
    # Mécanisme identique au backtest → prix SL/TP strictement identiques.
    sl_orig = cfg.OPR_SL_ATR_MULT.get(ticker)
    tp_orig = cfg.OPR_TP_ATR_MULT.get(ticker)
    try:
        cfg.OPR_SL_ATR_MULT[ticker] = OPR_NQ1_SL_MULT
        cfg.OPR_TP_ATR_MULT[ticker] = OPR_NQ1_TP_MULT
        _opr.OPR_SL_ATR_MULT[ticker] = OPR_NQ1_SL_MULT
        _opr.OPR_TP_ATR_MULT[ticker] = OPR_NQ1_TP_MULT

        # F2 OFF → run_opr_day nu (opr-v4), aucun filtre F2.
        signals, trades, _zone = run_opr_day(df_15m, ticker, day_ny)
    finally:
        cfg.OPR_SL_ATR_MULT[ticker] = sl_orig
        cfg.OPR_TP_ATR_MULT[ticker] = tp_orig
        _opr.OPR_SL_ATR_MULT[ticker] = sl_orig
        _opr.OPR_TP_ATR_MULT[ticker] = tp_orig

    if not signals:
        return None

    last_idx = len(signals) - 1
    last_sig = signals[last_idx]
    last_trade = trades[last_idx] if last_idx < len(trades) else {}

    # Signal "à placer" uniquement s'il n'a pas encore de résultat définitif
    # (None ou NOT_FILLED = pas encore fillé côté broker). Mirror _get_opr_signal.
    if last_trade.get("result") not in (None, "NOT_FILLED"):
        return None

    # ── Sizing dédié $150 (recalcul n_ct/risk/gain) ─────────────────────────
    sl_dist = float(last_sig["sl_dist"])
    if sl_dist <= 0:
        return None
    dpp = float(INSTRUMENTS[ticker]["dollar_per_point"])
    n_ct = max(1, int(OPR_NQ1_RISK_USD / (sl_dist * dpp)))
    risk = n_ct * sl_dist * dpp
    gain = n_ct * float(last_sig["tp_dist"]) * dpp

    date_str = day_ny.strftime("%Y%m%d")
    tag = f"OPRNQ1_{ticker}_{date_str}_{last_sig['direction']}_{last_idx}"

    sig = dict(last_sig)
    sig.update(
        {
            "strategy": "OPR_NQ1",
            "n_ct": int(n_ct),
            "risk": float(risk),
            "gain": float(gain),
            "tag": tag,
        }
    )
    return sig
