"""
Stratégie OPR `opr-v2` — réécriture fidèle au PineScript de l'utilisateur.

Logique exacte (cf. CLAUDE.md → "Stratégie OPR") :

  1. Zone OPR = bougie 15m qui ouvre à 9h30 NY (America/New_York).
     `opr_high` / `opr_low` = high / low de cette bougie. Le fuseau NY gère
     automatiquement le passage été/hiver — la stratégie reste à 9h30 NY
     toute l'année (= 14h30 UTC en hiver, 13h30 UTC en été).

  2. Trigger pullback (uniquement après 9h45 NY et avant 16h30 NY) :
       LONG  : bougie qui clôture hors OPR avec `open < opr_high`
               et `close > opr_high`.
       SHORT : bougie qui clôture hors OPR avec `open > opr_low`
               et `close < opr_low`.
     Un trigger arme un ordre LIMIT placé pour la bougie suivante :
       LONG  → limit BUY  @ opr_high
       SHORT → limit SELL @ opr_low

  3. Une seule position à la fois — tant qu'une position est ouverte ou
     qu'un ordre limite est en attente, aucun nouveau trigger n'est armé.
     Si la direction change (nouveau trigger opposé) AVANT le fill, l'ordre
     précédent est remplacé.

  4. Stop-loss et take-profit en distance fixe en points (`OPR_SL_POINTS` /
     `OPR_TP_POINTS` par ticker). Pas de RR en fonction de la zone — le
     PineScript utilise des distances fixes (`stopPerInput`/`takePerInput`).

  5. À 16h30 NY, toute position encore ouverte est fermée au cours de la
     bougie courante (close all). Aucun nouvel ordre après cette heure.

Le module reste indépendant des modules `composite` (zones / scoring) afin
que les deux stratégies cohabitent.
"""

from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd

from config import (
    INSTRUMENTS, RISK_PER_TRADE_USD, OPR_ENABLED,
    OPR_TIMEZONE, OPR_WINDOW_START, OPR_WINDOW_END, OPR_SESSION_END,
    OPR_SL_POINTS, OPR_TP_POINTS, OPR_MAX_TRADES_PER_DAY,
)


# ─────────────────────────────────────────────────────────────────────────
# Helpers timezone
# ─────────────────────────────────────────────────────────────────────────

def _ny_session_view(df_15m: pd.DataFrame, day_ny: pd.Timestamp,
                     tz: ZoneInfo) -> Optional[pd.DataFrame]:
    """
    Renvoie une vue triée du DataFrame 15min couvrant la session US en
    heure NY pour `day_ny` (un Timestamp localisé NY à minuit) :
      • Index converti en heure NY (DatetimeIndex tz-aware).
      • Borné de 9h30 NY à 16h30 NY inclus.
      • None si l'index source est vide.
    """
    if df_15m.empty:
        return None

    # Le CSV est en UTC naïf — on localise puis convertit en NY.
    if df_15m.index.tz is None:
        idx_ny = df_15m.index.tz_localize("UTC").tz_convert(tz)
    else:
        idx_ny = df_15m.index.tz_convert(tz)

    df_ny = df_15m.copy()
    df_ny.index = idx_ny

    h_start, m_start = OPR_WINDOW_START
    h_end, m_end = OPR_SESSION_END
    session_start = day_ny.replace(hour=h_start, minute=m_start,
                                   second=0, microsecond=0)
    session_end = day_ny.replace(hour=h_end, minute=m_end,
                                 second=0, microsecond=0)

    mask = (df_ny.index >= session_start) & (df_ny.index <= session_end)
    return df_ny.loc[mask]


def _opr_bar(df_session_ny: pd.DataFrame) -> Optional[pd.Series]:
    """Retourne la bougie 15m qui ouvre à 9h30 NY (la 1ère de la session)."""
    if df_session_ny is None or df_session_ny.empty:
        return None
    h, m = OPR_WINDOW_START
    first = df_session_ny.iloc[0]
    if df_session_ny.index[0].hour != h or df_session_ny.index[0].minute != m:
        return None
    return first


# ─────────────────────────────────────────────────────────────────────────
# Trade builder
# ─────────────────────────────────────────────────────────────────────────

def _make_signal(
    ticker: str,
    direction: str,
    entry: float,
    opr_high: float,
    opr_low: float,
    trigger_time: pd.Timestamp,
) -> Optional[Dict]:
    """
    Construit le dict signal (entry/SL/TP/sizing) à partir d'un trigger
    armé. Retourne None si SL/TP invalides ou sizing nul.
    """
    inst = INSTRUMENTS[ticker]
    dpp = inst["dollar_per_point"]
    sl_pts = float(OPR_SL_POINTS.get(ticker, 10.0))
    tp_pts = float(OPR_TP_POINTS.get(ticker, 20.0))

    if sl_pts <= 0 or tp_pts <= 0:
        return None

    if direction == "long":
        sl_price = entry - sl_pts
        tp_price = entry + tp_pts
    else:
        sl_price = entry + sl_pts
        tp_price = entry - tp_pts

    # Sizing à risque fixe ($100 par défaut)
    n_ct = int(RISK_PER_TRADE_USD / (sl_pts * dpp))
    if n_ct <= 0:
        return None

    risk = n_ct * sl_pts * dpp
    gain = n_ct * tp_pts * dpp

    return {
        "ticker": ticker,
        "strategy": "OPR",
        "direction": direction,
        "entry": float(entry),
        "sl": float(sl_price),
        "tp": float(tp_price),
        "sl_dist": float(sl_pts),
        "tp_dist": float(tp_pts),
        "rr": round(tp_pts / sl_pts, 2),
        "n_ct": int(n_ct),
        "risk": float(risk),
        "gain": float(gain),
        "opr_high": float(opr_high),
        "opr_low": float(opr_low),
        "opr_range": float(opr_high - opr_low),
        "trigger_time": str(trigger_time),
        # Champs neutres pour compat avec le format signal "composite"
        "quality": 0.0,
        "composite": 0.0,
        "alignment": 0.0,
        "atr_ratio": 0.0,
        "n_tf": 1,
        "touches": 0,
        "regime": "OPR",
        "zone_low": float(opr_low),
        "zone_high": float(opr_high),
        "price_now": float(entry),
        "tp_type": "fixed",
    }


# ─────────────────────────────────────────────────────────────────────────
# Moteur de session (1 jour)
# ─────────────────────────────────────────────────────────────────────────

def _check_trigger(bar: pd.Series, opr_high: float, opr_low: float
                   ) -> Optional[str]:
    """Renvoie 'long', 'short' ou None pour la bougie courante."""
    o = float(bar["open"])
    c = float(bar["close"])
    if o < opr_high and c > opr_high:
        return "long"
    if o > opr_low and c < opr_low:
        return "short"
    return None


def _bar_hits(direction: str, level: float, bar: pd.Series) -> bool:
    """Vrai si le range de la bougie touche `level` dans le sens du fill."""
    return float(bar["low"]) <= level <= float(bar["high"])


def run_opr_day(df_15m: pd.DataFrame, ticker: str,
                day_ny: pd.Timestamp) -> Tuple[List[Dict], List[Dict],
                                               Optional[Dict]]:
    """
    Joue la session OPR d'un jour donné suivant le PineScript.

    Args:
        df_15m  : DataFrame 15min (index naïf UTC ou tz-aware).
        ticker  : "MES1" | "NQ1" | "YM1".
        day_ny  : Timestamp tz-aware (NY) à 00:00 — désigne le jour à jouer.

    Returns:
        (signals, trades, opr_zone)
          • signals : liste des signaux générés (1 dict / trigger armé →
                      ordre placé). Inclut les ordres NON remplis.
          • trades  : résultats de simulation (1:1 avec signals).
                      result ∈ {"TP", "SL", "TE", "NOT_FILLED"}.
          • opr_zone : dict {high, low, mid, range, time} ou None si la
                       bougie 9h30 NY est introuvable.
    """
    if not OPR_ENABLED:
        return [], [], None

    tz = ZoneInfo(OPR_TIMEZONE)
    df_session = _ny_session_view(df_15m, day_ny, tz)
    if df_session is None or len(df_session) < 2:
        return [], [], None

    opr_bar = _opr_bar(df_session)
    if opr_bar is None:
        return [], [], None

    opr_high = float(opr_bar["high"])
    opr_low = float(opr_bar["low"])
    if opr_high <= opr_low:
        return [], [], None

    # `time_ny`  : timestamp tz-aware NY de la bougie OPR (9h30 NY).
    # `time_utc` : même bougie convertie en UTC naïf (utilisable pour
    #              indexer le DataFrame source dans le chart d'analyse).
    opr_ts_ny = df_session.index[0]
    opr_zone = {
        "high": opr_high,
        "low": opr_low,
        "mid": (opr_high + opr_low) / 2.0,
        "range": opr_high - opr_low,
        "time": opr_ts_ny,
        "time_ny": opr_ts_ny,
        "time_utc": opr_ts_ny.tz_convert("UTC").tz_localize(None),
    }

    inst = INSTRUMENTS[ticker]
    dpp = inst["dollar_per_point"]

    h_end, m_end = OPR_WINDOW_END
    h_close, m_close = OPR_SESSION_END
    win_end_t = day_ny.replace(hour=h_end, minute=m_end,
                               second=0, microsecond=0)
    session_end_t = day_ny.replace(hour=h_close, minute=m_close,
                                   second=0, microsecond=0)

    # État de la session
    pending: Optional[Dict] = None       # ordre limite armé (en attente de fill)
    position: Optional[Dict] = None      # position ouverte (entry/SL/TP)
    signals: List[Dict] = []
    trades: List[Dict] = []
    n_fills = 0

    bars = df_session  # déjà bornée [9:30, 16:30]
    timestamps = bars.index

    for i, ts in enumerate(timestamps):
        bar = bars.iloc[i]
        # 1) La 1ère bougie est l'OPR — pas de trigger ni de fill ici.
        if i == 0:
            continue

        # 2) Si la position est ouverte : vérifier SL / TP sur cette bougie
        if position is not None:
            direction = position["direction"]
            entry = position["entry"]
            sl = position["sl"]
            tp = position["tp"]
            n_ct = position["n_ct"]

            hit_sl = (bar["low"] <= sl <= bar["high"]) if direction == "long" \
                else (bar["low"] <= sl <= bar["high"])
            hit_tp = (bar["low"] <= tp <= bar["high"]) if direction == "long" \
                else (bar["low"] <= tp <= bar["high"])

            # Précis : pour un long, SL = entry - X (en bas), TP = entry + Y
            # (en haut). hit_sl ⇔ low ≤ sl ; hit_tp ⇔ high ≥ tp.
            if direction == "long":
                hit_sl = float(bar["low"]) <= sl
                hit_tp = float(bar["high"]) >= tp
            else:
                hit_sl = float(bar["high"]) >= sl
                hit_tp = float(bar["low"]) <= tp

            result = None
            exit_price = None
            if hit_sl and hit_tp:
                # Les deux touchés sur la même bougie → on tranche par
                # direction de la bougie (TP autorisé seulement si la
                # bougie va dans le sens du trade).
                bull = float(bar["close"]) >= float(bar["open"])
                if direction == "long":
                    result, exit_price = ("TP", tp) if bull else ("SL", sl)
                else:
                    result, exit_price = ("TP", tp) if not bull else ("SL", sl)
            elif hit_sl:
                result, exit_price = "SL", sl
            elif hit_tp:
                result, exit_price = "TP", tp

            # 16h30 NY : on clôture la position au close si toujours ouverte.
            if result is None and ts >= session_end_t:
                result = "TE"
                exit_price = float(bar["close"])

            if result is not None:
                pnl_pts = (exit_price - entry) if direction == "long" \
                    else (entry - exit_price)
                pnl = n_ct * pnl_pts * dpp
                trade = {
                    "result": result,
                    "pnl": float(pnl),
                    "exit": float(exit_price),
                    "fill_time": str(position["fill_time"]),
                    "exit_time": str(ts),
                }
                trades.append({"_signal_idx": position["signal_idx"],
                               **trade})
                position = None  # libère le slot pour un nouveau trigger
                # On continue l'itération : pas de nouvel ordre sur la
                # bougie d'exit (cohérent PineScript : la décision suivante
                # se prend sur la bougie d'après).
                continue

            # Position toujours ouverte : on n'arme rien d'autre, on passe.
            continue

        # 3) Pas de position ouverte. Tentative de fill de l'ordre limite
        #    pendant en utilisant la bougie courante.
        if pending is not None:
            level = pending["entry"]
            direction = pending["direction"]
            # Avant 9h45 NY, on n'autorise pas le fill (cohérent avec
            # `validTradeTime > oprEnd`). En pratique pending ne peut
            # exister qu'après le 1er trigger qui se produit lui-même
            # post-9h45, donc cette garde est défensive.
            if ts <= win_end_t:
                pending = None
            elif ts >= session_end_t:
                # Plus de fill possible après la cloche : ordre annulé.
                pending = None
            elif _bar_hits(direction, level, bar):
                # Fill au niveau OPR. On ouvre la position immédiatement.
                if n_fills >= OPR_MAX_TRADES_PER_DAY:
                    pending = None
                else:
                    sig = pending["signal"]
                    sig_idx = pending["signal_idx"]
                    position = {
                        "direction": direction,
                        "entry": sig["entry"],
                        "sl": sig["sl"],
                        "tp": sig["tp"],
                        "n_ct": sig["n_ct"],
                        "fill_time": ts,
                        "signal_idx": sig_idx,
                    }
                    n_fills += 1
                    pending = None

                    # Vérifie immédiatement si SL/TP est touché sur la
                    # bougie de fill (même règle que ci-dessus).
                    sl = position["sl"]
                    tp = position["tp"]
                    if direction == "long":
                        hit_sl = float(bar["low"]) <= sl
                        hit_tp = float(bar["high"]) >= tp
                    else:
                        hit_sl = float(bar["high"]) >= sl
                        hit_tp = float(bar["low"]) <= tp

                    result = None
                    exit_price = None
                    if hit_sl and hit_tp:
                        bull = float(bar["close"]) >= float(bar["open"])
                        if direction == "long":
                            result, exit_price = ("TP", tp) if bull else ("SL", sl)
                        else:
                            result, exit_price = ("TP", tp) if not bull else ("SL", sl)
                    elif hit_sl:
                        result, exit_price = "SL", sl
                    elif hit_tp:
                        result, exit_price = "TP", tp

                    if result is not None:
                        pnl_pts = (exit_price - position["entry"]) \
                            if direction == "long" \
                            else (position["entry"] - exit_price)
                        pnl = position["n_ct"] * pnl_pts * dpp
                        trades.append({
                            "_signal_idx": position["signal_idx"],
                            "result": result,
                            "pnl": float(pnl),
                            "exit": float(exit_price),
                            "fill_time": str(position["fill_time"]),
                            "exit_time": str(ts),
                        })
                        position = None
                    continue  # fill traité, on passe à la bougie suivante

        # 4) Pas de position, pas de fill sur cette bougie : on cherche un
        #    nouveau trigger pullback. Uniquement après 9h45 NY et avant
        #    16h30 NY (et tant qu'il n'y a ni position ni ordre en attente).
        if position is not None or pending is not None:
            continue
        if ts <= win_end_t or ts >= session_end_t:
            continue
        if n_fills >= OPR_MAX_TRADES_PER_DAY:
            continue

        trig = _check_trigger(bar, opr_high, opr_low)
        if trig is None:
            continue

        entry_level = opr_high if trig == "long" else opr_low
        sig = _make_signal(
            ticker=ticker,
            direction=trig,
            entry=entry_level,
            opr_high=opr_high,
            opr_low=opr_low,
            trigger_time=ts,
        )
        if sig is None:
            continue

        sig_idx = len(signals)
        signals.append(sig)
        pending = {
            "direction": trig,
            "entry": entry_level,
            "signal": sig,
            "signal_idx": sig_idx,
            "armed_at": ts,
        }

    # 5) Fin de boucle :
    #    - Position encore ouverte → fermée au close de la dernière bougie.
    #    - Ordre limite encore en attente → marqué NOT_FILLED.
    if position is not None:
        last_bar = bars.iloc[-1]
        exit_price = float(last_bar["close"])
        direction = position["direction"]
        pnl_pts = (exit_price - position["entry"]) if direction == "long" \
            else (position["entry"] - exit_price)
        pnl = position["n_ct"] * pnl_pts * dpp
        trades.append({
            "_signal_idx": position["signal_idx"],
            "result": "TE",
            "pnl": float(pnl),
            "exit": float(exit_price),
            "fill_time": str(position["fill_time"]),
            "exit_time": str(timestamps[-1]),
        })
        position = None

    if pending is not None:
        trades.append({
            "_signal_idx": pending["signal_idx"],
            "result": "NOT_FILLED",
            "pnl": 0.0,
            "exit": None,
            "fill_time": None,
            "exit_time": None,
        })
        pending = None

    # On garantit qu'à chaque signal correspond un trade (1:1 par index).
    indexed = {t["_signal_idx"]: t for t in trades}
    out_trades: List[Dict] = []
    for i in range(len(signals)):
        t = indexed.get(i, {
            "result": "NOT_FILLED", "pnl": 0.0,
            "exit": None, "fill_time": None, "exit_time": None,
        })
        # Nettoyage : on retire la clé interne
        t = {k: v for k, v in t.items() if k != "_signal_idx"}
        out_trades.append(t)

    return signals, out_trades, opr_zone
