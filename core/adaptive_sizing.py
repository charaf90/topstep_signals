"""
Sizing adaptatif pour le challenge Topstep mensuel.

Le risque par trade est calculé dynamiquement selon :
  (a) distance au profit target (TOPSTEP_PROFIT_TARGET)
  (b) slack DD (trailing) et slack Topstep daily
  (c) jours ouvrés restants avant la réinit (CHALLENGE_RESET_DAY)
  (d) edge OOS static par stratégie (CHALLENGE_STRAT_EDGE × CHALLENGE_STRAT_BOOST)

Fonctions pures, aucun I/O, aucune dépendance sur le live runner.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from typing import Any

from config import (
    CHALLENGE_DAY_PROFIT_HARD_CAP_USD,
    CHALLENGE_DAY_PROFIT_SOFT_CAP_USD,
    CHALLENGE_DD_GUARD_BUFFER,
    CHALLENGE_EXPECTED_TRADES_PER_DAY_FALLBACK,
    CHALLENGE_LOCKIN_START_USD,
    CHALLENGE_RESET_DAY,
    CHALLENGE_RISK_MAX_USD,
    CHALLENGE_RISK_MIN_USD,
    CHALLENGE_STRAT_BOOST,
    CHALLENGE_STRAT_EDGE,
    CHALLENGE_TIME_PRESSURE_GAMMA,
    TOPSTEP_DAILY_LOSS_MAX,
    TOPSTEP_PROFIT_TARGET,
    TOPSTEP_TRAILING_DD,
)


def trading_days_until(today: datetime | date, reset_day: int = CHALLENGE_RESET_DAY) -> int:
    """
    Nombre de jours ouvrés (lun-ven) entre `today` (inclus) et le prochain
    `reset_day` du mois (exclus). Jours fériés US non gérés en v1.

    Si today.day < reset_day → reset dans le mois courant.
    Sinon → reset au mois suivant.
    Retourne au minimum 1 (évite division par 0).
    """
    if isinstance(today, datetime):
        today = today.date()

    if today.day < reset_day:
        # Reset dans le mois courant
        next_reset = today.replace(day=reset_day)
    else:
        # Reset au mois suivant — gérer le passage décembre → janvier
        if today.month == 12:
            next_reset = date(today.year + 1, 1, reset_day)
        else:
            next_reset = date(today.year, today.month + 1, reset_day)
        # Clamp si le reset_day dépasse le nombre de jours du mois cible
        last_day = calendar.monthrange(next_reset.year, next_reset.month)[1]
        if reset_day > last_day:
            next_reset = next_reset.replace(day=last_day)

    n = 0
    d = today
    while d < next_reset:
        if d.weekday() < 5:  # lundi=0, vendredi=4
            n += 1
        d += timedelta(days=1)
    return max(1, n)


def compute_factors(
    rm_status: dict[str, Any],
    signal: dict[str, Any],
    today: datetime,
) -> dict[str, float]:
    """
    Calcule tous les facteurs intermédiaires nécessaires au sizing adaptatif.
    Utilisé à la fois pour le calcul du risque et le logging.
    """
    cum_pnl = float(rm_status.get("cum_pnl", 0.0))
    peak_pnl = max(float(rm_status.get("peak_pnl", 0.0)), cum_pnl)
    rdp = float(rm_status.get("realized_day_pnl", 0.0))

    days_left = trading_days_until(today, reset_day=CHALLENGE_RESET_DAY)
    distance_target = max(1.0, TOPSTEP_PROFIT_TARGET - cum_pnl)

    # Caps de protection — limites Topstep dures uniquement
    slack_trail = max(0.0, cum_pnl - (peak_pnl - TOPSTEP_TRAILING_DD))
    slack_daily_topstep = max(0.0, TOPSTEP_DAILY_LOSS_MAX + rdp)  # rdp négatif si perte
    dd_cap = slack_trail / CHALLENGE_DD_GUARD_BUFFER
    daily_cap = slack_daily_topstep / CHALLENGE_DD_GUARD_BUFFER

    # Edge static par stratégie
    strategy = signal.get("strategy", "OPR")
    e_strat = float(CHALLENGE_STRAT_EDGE.get(strategy, 0.30))
    boost = float(CHALLENGE_STRAT_BOOST.get(strategy, 1.0))

    n_per_day = rm_status.get("observed_trades_per_day")
    if n_per_day is None or n_per_day <= 0:
        n_per_day = CHALLENGE_EXPECTED_TRADES_PER_DAY_FALLBACK
    n_per_day = float(n_per_day)

    gamma = float(CHALLENGE_TIME_PRESSURE_GAMMA)
    target_risk = distance_target / (max(1, days_left) ** gamma * n_per_day * e_strat)

    # Lock-in : réduit le risque quand on approche l'objectif
    # Démarre à CHALLENGE_LOCKIN_START_USD, descend linéairement à 0.25 au target
    if cum_pnl < CHALLENGE_LOCKIN_START_USD:
        lockin = 1.0
    else:
        span = max(1.0, TOPSTEP_PROFIT_TARGET - CHALLENGE_LOCKIN_START_USD)
        lockin = max(0.25, (TOPSTEP_PROFIT_TARGET - cum_pnl) / span)

    # Day-progress damping : amortit le risque quand la journée a déjà bien
    # gagné — anticipe la règle de cohérence 50% (best_day ≤ $1500). Entre
    # SOFT_CAP et HARD_CAP, interpolation linéaire 1.0 → 0.25. Au-delà du
    # HARD_CAP, la borne 0.25 reste (le hard-stop "no new trade" est géré
    # dans core/risk_portfolio.py via tp_gain_usd).
    if rdp <= CHALLENGE_DAY_PROFIT_SOFT_CAP_USD:
        day_progress = 1.0
    elif rdp >= CHALLENGE_DAY_PROFIT_HARD_CAP_USD:
        day_progress = 0.25
    else:
        span_dp = max(1.0, CHALLENGE_DAY_PROFIT_HARD_CAP_USD - CHALLENGE_DAY_PROFIT_SOFT_CAP_USD)
        progress = (rdp - CHALLENGE_DAY_PROFIT_SOFT_CAP_USD) / span_dp
        day_progress = max(0.25, 1.0 - 0.75 * progress)

    return {
        "cum_pnl": cum_pnl,
        "peak_pnl": peak_pnl,
        "realized_day_pnl": rdp,
        "days_left": float(days_left),
        "distance_target": distance_target,
        "slack_trail": slack_trail,
        "slack_daily_topstep": slack_daily_topstep,
        "dd_cap": dd_cap,
        "daily_cap": daily_cap,
        "e_strat": e_strat,
        "boost": boost,
        "n_per_day": n_per_day,
        "target_risk": target_risk,
        "lockin": lockin,
        "day_progress": day_progress,
        "strategy": strategy,
    }


def adaptive_risk_usd(
    rm_status: dict[str, Any],
    signal: dict[str, Any],
    today: datetime,
) -> tuple[float, dict[str, float]]:
    """
    Calcule le risque par trade adaptatif (USD).

    Retourne (risk_usd, factors) où factors contient tous les facteurs
    intermédiaires pour audit/logging.

    Le risque retourné est :
      - clampé à [CHALLENGE_RISK_MIN_USD, CHALLENGE_RISK_MAX_USD]
      - capé par dd_cap et daily_cap (protection vs limites Topstep dures)
      - réduit par lockin si on approche le profit target
      - amplifié par boost si la stratégie a un edge OOS plus élevé
    """
    f = compute_factors(rm_status, signal, today)

    raw = (
        min(f["target_risk"] * f["boost"], f["dd_cap"], f["daily_cap"])
        * f["lockin"]
        * f["day_progress"]
    )
    risk = float(max(CHALLENGE_RISK_MIN_USD, min(CHALLENGE_RISK_MAX_USD, raw)))

    f["raw_risk"] = float(raw)
    f["risk_applied"] = risk
    return risk, f
