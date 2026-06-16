"""Source de vérité UNIQUE du portefeuille LIVE — partagée par les outils de
backtest/replay (`tools/portfolio_replay.py`, `tools/backtest_vs_live.py`).

Lit les flags/univers/sizing de `config.py` → une seule définition, pas de dérive
entre outils. Chaque entrée décrit comment backtester une stratégie live à
l'identique de la prod.

Champs par stratégie :
  module       : nom auto-découvert par core.registry.load_strategy (strategies/<module>.py)
  tickers      : univers LIVE (depuis les *_LIVE_TICKERS / *_TICKERS de config)
  params       : overrides nécessaires à la fidélité live (None = défauts config = prod)
  enabled      : flag *_ENABLED (False = présent mais inerte en live, ex. bos-fvg)
  strategy_key : clé d'identité côté live (`state.placed_tags[*]["strategy"]`) pour la
                 jointure backtest ↔ live (FIB / FIB_FINE / IB_RETEST / OPR / BOS_FVG)
"""

from __future__ import annotations

from config import (
    BOS_FVG_ENABLED,
    BOS_FVG_LIVE_TICKERS,
    BOS_FVG_RISK_USD,
    FIB_FINE_ENABLED,
    FIB_FINE_LIVE_TICKERS,
    FIB_V4_ENABLED,
    FIB_V4_TICKERS,
    IB_RETEST_ENABLED,
    IB_RETEST_TICKERS,
    OPR_V4_LIVE_TICKERS,
    OPR_V5_1_LIVE_TICKERS,
)

# Définition brute (toutes les stratégies du portefeuille live, ENABLED ou non).
_SPECS = {
    "opr_v5_1": {
        "tickers": list(OPR_V5_1_LIVE_TICKERS),
        "params": None,
        "enabled": bool(OPR_V5_1_LIVE_TICKERS),
        "strategy_key": "OPR",
    },
    "opr": {  # opr-v4 (pass-through) — univers live OPR_V4_LIVE_TICKERS (souvent vide)
        "tickers": list(OPR_V4_LIVE_TICKERS),
        "params": None,
        "enabled": bool(OPR_V4_LIVE_TICKERS),
        "strategy_key": "OPR",
    },
    "fib_v4": {
        "tickers": list(FIB_V4_TICKERS),
        "params": None,
        "enabled": bool(FIB_V4_ENABLED),
        "strategy_key": "FIB",
    },
    "fib_fine": {
        "tickers": list(FIB_FINE_LIVE_TICKERS),
        "params": None,  # défaut = FIB_FINE_RISK_PER_TRADE_USD ($130), fidèle live
        "enabled": bool(FIB_FINE_ENABLED),
        "strategy_key": "FIB_FINE",
    },
    "bos_fvg": {
        "tickers": list(BOS_FVG_LIVE_TICKERS),
        "params": {"risk_usd": float(BOS_FVG_RISK_USD)},  # live $150 ≠ défaut recherche $200
        "enabled": bool(BOS_FVG_ENABLED),
        "strategy_key": "BOS_FVG",
    },
    "ib_retest": {
        "tickers": list(IB_RETEST_TICKERS),
        "params": None,  # défaut = config.IB_RETEST_* (prod 0.5/0.5/1.5), via strategies/ib_retest.py
        "enabled": bool(IB_RETEST_ENABLED),
        "strategy_key": "IB_RETEST",
    },
}


def live_portfolio_specs(include_disabled: bool = False) -> dict[str, dict]:
    """Retourne les specs du portefeuille live.

    include_disabled=False (défaut) : seules les stratégies ENABLED avec un univers
    non vide (= ce qui trade réellement en live). True : inclut les inertes (bos-fvg).
    """
    out: dict[str, dict] = {}
    for name, spec in _SPECS.items():
        if not spec["tickers"]:
            continue
        if not spec["enabled"] and not include_disabled:
            continue
        out[name] = dict(spec)
    return out
