"""
Sélection de l'actif prioritaire quand plusieurs tickers déclenchent le même
signal simultanément.

MES1, NQ1, YM1 sont très corrélés — prendre les trois en même temps triple
le risque corrélé. La règle : si N assets déclenchent la même stratégie dans
la même fenêtre temporelle (même bougie M15), on ne retient que le meilleur
selon le classement historique OOS.

Classements OOS par stratégie (walk-forward Dec 2024 → Mar 2026) :
  - Composite : NQ1 (PF 1.87) > MES1 (PF 1.39) > YM1 (désactivé)
  - OPR       : NQ1 (score 6979) > YM1 (score 6835) > MES1 (score 1510)
                où score = OOS_PF × OOS_P&L
  - Fib       : NQ1 (Sharpe 18.67) > YM1 (Sharpe 7.11) > MES1 (Sharpe 4.51)

Usage :
    from core.signal_selector import pick_best_ticker

    signals = [
        {"strategy": "opr", "ticker": "MES1", ...},
        {"strategy": "opr", "ticker": "NQ1",  ...},
        {"strategy": "opr", "ticker": "YM1",  ...},
    ]
    best = pick_best_ticker(signals)  # retourne le signal NQ1
"""

from __future__ import annotations
from typing import List, Dict, Optional

# Rang 1 = meilleur. Basés sur OOS walk-forward (Dec 2024 → Mar 2026).
# Mettre à jour après chaque re-calibration walk-forward.
TICKER_PRIORITY: Dict[str, Dict[str, int]] = {
    "composite": {"NQ1": 1, "MES1": 2, "YM1": 3},
    "opr":       {"NQ1": 1, "YM1": 2, "MES1": 3},
    "fib":       {"NQ1": 1, "YM1": 2, "MES1": 3},
}

# Score OOS de référence (utilisé pour info / logging uniquement)
TICKER_OOS_SCORE: Dict[str, Dict[str, float]] = {
    "composite": {"NQ1": 1.87, "MES1": 1.39, "YM1": 0.0},
    "opr":       {"NQ1": 6979.5, "YM1": 6835.0, "MES1": 1510.0},
    "fib":       {"NQ1": 18.67, "YM1": 7.11, "MES1": 4.51},
}


def pick_best_ticker(signals: List[Dict],
                     strategy_key: Optional[str] = None) -> Optional[Dict]:
    """
    Parmi une liste de signaux (même stratégie, tickers différents), retourne
    le signal de l'actif le mieux classé OOS.

    Si strategy_key n'est pas fourni, il est lu depuis signal["strategy"].
    Si plusieurs signaux ont le même rang (cas inhabituel), retourne le premier.
    Retourne None si la liste est vide.
    """
    if not signals:
        return None
    if len(signals) == 1:
        return signals[0]

    key = strategy_key or signals[0].get("strategy", "")
    priority = TICKER_PRIORITY.get(key, {})

    def _rank(sig: Dict) -> int:
        return priority.get(sig.get("ticker", ""), 999)

    return min(signals, key=_rank)


def filter_correlated_signals(signals: List[Dict]) -> List[Dict]:
    """
    Filtre une liste de signaux mixtes (stratégies et tickers) en garantissant
    que pour chaque stratégie, un seul actif est retenu si plusieurs ont été
    déclenchés simultanément.

    Les signaux de stratégies différentes (ex. OPR sur NQ1 + Fib sur MES1)
    ne sont PAS filtrés entre eux — la corrélation joue essentiellement sur
    les signaux de même type déclenchés en même temps.

    Retourne la liste filtrée (au plus un signal par stratégie par direction).
    """
    if not signals:
        return []

    # Regroupe par (stratégie, direction) — deux signaux de même stratégie
    # mais directions opposées (ex. OPR long NQ1 / OPR short YM1) restent
    # indépendants car la corrélation n'est pas additive cross-direction.
    groups: Dict[str, List[Dict]] = {}
    for sig in signals:
        strat = sig.get("strategy", "unknown")
        direction = sig.get("direction", "")
        group_key = f"{strat}_{direction}"
        groups.setdefault(group_key, []).append(sig)

    result = []
    for group_key, group_signals in groups.items():
        strat = group_signals[0].get("strategy", "")
        best = pick_best_ticker(group_signals, strategy_key=strat)
        if best is not None:
            result.append(best)

    return result
