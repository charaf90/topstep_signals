"""
core/opr_v5_1.py — Implémentation LIVE d'OPR v5.1 avec schéma A (entrée différée).

═══════════════════════════════════════════════════════════════════════════════
PROBLÈME RÉSOLU
═══════════════════════════════════════════════════════════════════════════════
Le backtest `strategies/opr_v5_1.py` applique le filtre `f2_min_atr` POST-FILL :
il compute F2 sur [trigger_ts, fill_ts] (inclut la bougie de fill), puis marque
un trade NOT_FILLED si F2 < seuil. En backtest c'est causalement OK — F2 est
connu au moment du fill — mais EN LIVE on ne peut pas dé-filler une position
déjà ouverte sur le broker.

═══════════════════════════════════════════════════════════════════════════════
SCHÉMA A — Entrée différée
═══════════════════════════════════════════════════════════════════════════════
Au trigger (détection de cassure OPR), NE PAS placer le LIMIT immédiatement.
À chaque tick subséquent, recompute running F2 = max excursion DEPUIS le
trigger jusqu'aux bougies déjà fermées. Quand F2 ≥ seuil ticker-spécifique,
le signal est émis et le LIMIT est placé via le flow normal.

Causalité : tous les bars utilisés sont strictement ≤ "now" (close des bars
déjà fermées). Zéro look-ahead.

═══════════════════════════════════════════════════════════════════════════════
FIDÉLITÉ ATTENDUE
═══════════════════════════════════════════════════════════════════════════════
Mesurée via scripts/live_eq_v5_1.py (M15) : **74 %** (vs backtest post-fill).
La perte de 26 % vient des cas où le push de cassure se produit DANS la bougie
M15 du fill — non distinguable en M15 pur. Phase A (M1 polling) pourrait
améliorer cette fidélité, à déterminer en burn-in.

PF live attendu (brut OOS NQ1+YM1) : ~2.11 (vs 2.50 backtest).
PF live attendu (net) : ~1.81 (vs 2.15 backtest).
DD live OOS estimé : -$455 (sous limite Topstep).

═══════════════════════════════════════════════════════════════════════════════
IDEMPOTENCE
═══════════════════════════════════════════════════════════════════════════════
Sans état. Le `trigger_time` dans le signal v4 est déterministe à partir des
bars M15 (la première bougie qui casse l'OPR). Running F2 est monotone
croissant (max sur fenêtre élargie). Donc :
  - Tick avant que F2 atteigne le seuil : aucun signal émis
  - Tick où F2 ≥ seuil pour la première fois : signal émis, LIMIT placé
  - Ticks suivants : signal toujours émis, `_already_placed(tag)` évite la
    double-placement côté SessionRunner

═══════════════════════════════════════════════════════════════════════════════
ROUTING PER-TICKER
═══════════════════════════════════════════════════════════════════════════════
Cf config.OPR_V5_1_LIVE_TICKERS. Tickers absents de cette liste reçoivent un
pass-through vers `core.opr.run_opr_day` (v4). Aujourd'hui :
  - MES1 : v4 (edge ML F2 non significatif p=0.23)
  - NQ1, YM1 : v5.1 (filtre F2 actif, p<0.0001)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

# Reproductibilité (convention projet)
np.random.seed(42)

from config import (
    OPR_TIMEZONE,
    OPR_V5_1_LIVE_TICKERS,
    OPR_V5_1_F2_MIN_ATR,
)
from core.opr import run_opr_day, _ny_session_view

if TYPE_CHECKING:
    # Couplage faible : on n'importe le type qu'à la vérif statique.
    # Le module reste utilisable sans broker.m1_buffer installé.
    from broker.m1_buffer import M1Buffer


def get_opr_v5_1_live_signals(
    df_15m: pd.DataFrame,
    ticker: str,
    day_ny: pd.Timestamp,
    m1_buffer: Optional["M1Buffer"] = None,
    contract_id: Optional[str] = None,
) -> Tuple[List[Dict], List[Dict], Optional[Dict]]:
    """
    Variante live d'OPR v5.1 avec schéma A (entrée différée).

    Wrapper de `core.opr.run_opr_day` qui filtre les signals selon le running
    F2 calculé depuis le `trigger_time` jusqu'aux bars déjà fermées. Un signal
    n'est renvoyé QUE quand F2 ≥ seuil_ticker — donc le LIMIT n'est placé que
    quand le push de cassure est confirmé.

    Args:
        df_15m   : DataFrame M15 (UTC ou tz-aware), bars FERMÉES uniquement
                   pour respecter la causalité.
        ticker   : "MES1" | "NQ1" | "YM1".
        day_ny   : Timestamp tz-aware NY à 00:00 (jour à jouer).
        m1_buffer    : (Phase C, optionnel) `broker.m1_buffer.M1Buffer` alimenté
                       par le Market Hub WS. Si fourni avec `contract_id` et
                       qu'au moins 1 bar M1 existe depuis le trigger, F2 est
                       calculé sur les bars M1 (closed + forming) pour capter
                       les pushes intra-bar M15. Sinon fallback M15 strict.
        contract_id  : (Phase C, optionnel) clé du buffer M1 pour ce ticker
                       (ex: "CON.F.US.MNQ.M26"). Ignoré si m1_buffer is None.

    Returns:
        Même format que `run_opr_day` : `(signals, trades, opr_zone)`.
        signals/trades filtrés sur la condition F2_running ≥ seuil. Lorsque
        le filtrage M1 est appliqué, le signal est annoté `v5_1_f2_source =
        "M1_buffer"` (sinon "M15") pour traçabilité.
    """
    # 1. Run v4 pour détecter trigger + obtenir le signal
    signals, trades, opr_zone = run_opr_day(df_15m, ticker, day_ny)

    # 2. Pass-through pour tickers hors v5.1 (MES1 reste sur v4)
    if ticker not in OPR_V5_1_LIVE_TICKERS:
        return signals, trades, opr_zone

    threshold = OPR_V5_1_F2_MIN_ATR.get(ticker)
    if threshold is None:
        # Seuil non configuré pour ce ticker → comportement v4
        return signals, trades, opr_zone

    if not signals:
        return signals, trades, opr_zone

    # 3. Récupérer la vue session NY (utilisée pour les bars post-trigger)
    tz = ZoneInfo(OPR_TIMEZONE)
    df_session = _ny_session_view(df_15m, day_ny, tz)
    if df_session is None or len(df_session) == 0:
        return [], [], opr_zone

    # 4. Filtrer chaque signal selon le running F2
    filtered_signals: List[Dict] = []
    filtered_trades: List[Dict] = []

    for sig, trade in zip(signals, trades):
        trigger_ts_raw = sig.get("trigger_time")
        if not trigger_ts_raw:
            continue   # signal sans trigger_time — skip (ne devrait pas arriver)

        trigger_ts = pd.Timestamp(trigger_ts_raw)
        if trigger_ts.tz is None:
            trigger_ts = trigger_ts.tz_localize(tz)
        else:
            trigger_ts = trigger_ts.tz_convert(tz)

        # Bars post-trigger (toutes celles déjà fermées, INCLUS la bougie
        # du trigger elle-même). Causal : ne contient que des timestamps
        # ≤ "now" puisque df_15m ne contient que des bars fermées.
        bars_post_trigger = df_session[df_session.index >= trigger_ts]
        if len(bars_post_trigger) == 0:
            # Le trigger vient juste d'arriver, aucune bougie postérieure
            # n'est encore fermée → on attend le prochain tick
            continue

        opr_high = float(sig["opr_high"])
        opr_low  = float(sig["opr_low"])
        atr_daily = float(sig["atr_daily"])
        direction = sig["direction"]

        if atr_daily <= 0:
            continue   # protection division par zéro

        # Source des extrêmes pour F2 running : M1 buffer si dispo + ≥1 bar
        # depuis le trigger, sinon fallback M15 strict (comportement initial).
        # Le buffer M1 est alimenté par le Market Hub WS (Phase C) ; les bars
        # closed + forming sont inclus pour capter les pushes intra-bar M15.
        m1_bars = None
        if m1_buffer is not None and contract_id is not None:
            bars = m1_buffer.get_bars_since(
                contract_id, trigger_ts.to_pydatetime(),
                include_forming=True,
            )
            if bars:
                m1_bars = bars

        if direction == "long":
            if m1_bars is not None:
                post_high = max(b.high for b in m1_bars)
            else:
                post_high = float(bars_post_trigger["high"].max())
            excursion_pts = max(post_high - opr_high, 0.0)
        else:
            if m1_bars is not None:
                post_low = min(b.low for b in m1_bars)
            else:
                post_low = float(bars_post_trigger["low"].min())
            excursion_pts = max(opr_low - post_low, 0.0)

        f2_running = excursion_pts / atr_daily

        if f2_running >= threshold:
            # Push confirmé → on peut placer le LIMIT
            # Annoter le signal pour traçabilité (utile au logging)
            sig = dict(sig)   # copie pour éviter mutation cross-call
            sig["v5_1_f2_running_at_emit"] = float(f2_running)
            sig["v5_1_f2_threshold"] = float(threshold)
            sig["v5_1_f2_source"] = "M1_buffer" if m1_bars is not None else "M15"
            filtered_signals.append(sig)
            filtered_trades.append(trade)
        # Sinon : F2 pas encore suffisant → on attend (silence, pas d'émission)

    return filtered_signals, filtered_trades, opr_zone
