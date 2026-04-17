"""
Garde-fou Topstep : refuse un trade si perdre le risque nominal pousserait
le compte au-delà des limites du challenge (daily loss ou trailing DD).

N'opère PAS de sizing dynamique — la taille reste fixée par RISK_PER_TRADE_USD.
Utilisé uniquement par le backtest (boucle jour-par-jour). En live, le trader
surveille lui-même ses limites côté broker.
"""

from config import (
    TOPSTEP_DAILY_LOSS_MAX, TOPSTEP_TRAILING_DD,
    RISK_PER_TRADE_USD, TOPSTEP_SAFETY_MULT,
)


def trade_allowed(
    day_pnl: float,
    cum_pnl: float,
    peak_pnl: float,
    risk_per_trade: float = RISK_PER_TRADE_USD,
) -> tuple[bool, str]:
    """
    Retourne (autorise, motif).

    Args:
        day_pnl   : P&L déjà réalisé sur la journée (<0 si pertes).
        cum_pnl   : P&L cumulé total.
        peak_pnl  : Sommet historique du cum_pnl (trailing reference).
        risk_per_trade : Risque nominal d'un trade ($).

    Un trade est autorisé si perdre le risque nominal laisserait encore de la marge
    avant la limite daily ET avant la trailing DD, avec une marge de sécurité.
    """
    remaining_daily = TOPSTEP_DAILY_LOSS_MAX + day_pnl
    trailing_floor = peak_pnl - TOPSTEP_TRAILING_DD
    remaining_trail = cum_pnl - trailing_floor
    slack = min(remaining_daily, remaining_trail)
    threshold = risk_per_trade * TOPSTEP_SAFETY_MULT

    if slack < threshold:
        return False, f"topstep_slack_{slack:.0f}"
    return True, "ok"
