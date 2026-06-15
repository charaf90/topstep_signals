"""Mock stratégie + données partagés par les tests de l'optimiseur.

Extrait de tests/test_optimizer_parallel.py (logique inchangée) pour être
réutilisé par tests/test_search_optuna.py. Ajouts neutres par défaut :
`param_space` (contrat PARAM_SPACE optionnel) et `oos_pnl_offset` (modifie
UNIQUEMENT les trades OOS ≥ 2025-10-01 — sert au test d'invariant IS-only).
"""

from __future__ import annotations

import types

import numpy as np
import pandas as pd


def _build_mock_strategy(
    strategy_id: str = "mock-v1",
    tickers: tuple = ("T1", "T2"),
    param_grid: dict | None = None,
    param_space: dict | None = None,
    oos_pnl_offset: float = 0.0,
):
    """Construit un module mock avec un run_backtest déterministe."""
    if param_grid is None:
        param_grid = {"sl_mult": [1.0, 1.5, 2.0, 2.5], "tp_mult": [1.5, 2.0, 3.0, 4.0]}

    def run_backtest(df_15m, ticker, tf=None, params=None, topstep_guard=False):
        """Génère un DataFrame de trades déterministe basé sur (ticker, params).

        Le PnL dépend des params de façon non-triviale pour que le score change
        selon la combo choisie. Aucun aléatoire — strictement reproductible.
        """
        params = params or {}
        sl_m = params.get("sl_mult", 1.0)
        tp_m = params.get("tp_mult", 2.0)
        n = 100
        # Saut déterministe : PF varie avec sl/tp
        wins = int(n * (0.5 + 0.05 * (tp_m / sl_m - 1.0)))
        wins = max(0, min(n, wins))
        # Génère des dates IS + OOS
        dates = list(pd.date_range("2025-01-01", periods=n // 2, freq="D")) + list(
            pd.date_range("2025-11-01", periods=n - n // 2, freq="D")
        )
        # PnL : wins gagnent +100, losses perdent -50
        pnls = ([100.0] * wins + [-50.0] * (n - wins))[:n]
        # Hash ticker pour distinguer les tickers (mais déterministe)
        offset = sum(ord(c) for c in ticker) % 10
        pnls = [p + offset for p in pnls]
        dates_str = [d.strftime("%Y-%m-%d") for d in dates]
        if oos_pnl_offset:
            # Ne touche QUE l'OOS — l'IS reste strictement identique.
            pnls = [
                p + (oos_pnl_offset if ds >= "2025-10-01" else 0.0)
                for p, ds in zip(pnls, dates_str)
            ]
        return pd.DataFrame(
            {
                "date": dates_str,
                "pnl": pnls,
                "result": ["TP"] * wins + ["SL"] * (n - wins),
                "dir": ["long"] * n,
                "n_ct": [1] * n,
            }
        )

    mod = types.SimpleNamespace(
        STRATEGY_ID=strategy_id,
        TICKERS=list(tickers),
        PARAM_GRID=param_grid,
        run_backtest=run_backtest,
    )
    if param_space is not None:
        mod.PARAM_SPACE = param_space
    return mod


def _build_mock_data(tickers=("T1", "T2"), n=200):
    """Construit un dict {ticker: (df_15m, tf)} minimal pour optimize()."""
    out = {}
    for t in tickers:
        idx = pd.date_range("2024-12-01", periods=n, freq="15min", tz="UTC")
        df = pd.DataFrame(
            {
                "open": np.linspace(100, 110, n),
                "high": np.linspace(101, 111, n),
                "low": np.linspace(99, 109, n),
                "close": np.linspace(100, 110, n),
                "volume": [1000] * n,
            },
            index=idx,
        )
        out[t] = (df, None)
    return out
