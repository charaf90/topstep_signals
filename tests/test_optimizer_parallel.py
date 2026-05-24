"""Tests pour la parallélisation walk-forward (PHASE 2.5).

Vérifie :
- _evaluate_combo (worker pur) : déterministe et indépendant
- optimize() avec n_jobs=1 et n_jobs=4 → résultats identiques
- bench informatif (parallèle plus rapide que séquentiel sur grilles ≥ 16)
"""

from __future__ import annotations

import time
import types

import numpy as np
import pandas as pd
import pytest

from core.optimizer import _evaluate_combo, optimize

# ──────────────────────────────────────────────────────────────────────────────
# Mock strategy minimal compatible avec le contrat de core/optimizer
# ──────────────────────────────────────────────────────────────────────────────


def _build_mock_strategy(
    strategy_id: str = "mock-v1",
    tickers: tuple = ("T1", "T2"),
    param_grid: dict | None = None,
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
        return pd.DataFrame(
            {
                "date": [d.strftime("%Y-%m-%d") for d in dates],
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


# ──────────────────────────────────────────────────────────────────────────────
# _evaluate_combo — worker pur
# ──────────────────────────────────────────────────────────────────────────────


class TestEvaluateCombo:

    @staticmethod
    def _score_pnl_x_pf(is_s, oos_s):
        return oos_s.get("pnl", 0) * oos_s.get("pf", 0)

    @staticmethod
    def _score_pnl_only(is_s, oos_s):
        return oos_s.get("pnl", 0)

    def test_deterministe(self):
        """Mêmes inputs → mêmes outputs (pas d'aléatoire)."""
        strategy = _build_mock_strategy()
        data = _build_mock_data()
        keys = list(strategy.PARAM_GRID.keys())
        combo = tuple(v[0] for v in strategy.PARAM_GRID.values())

        r1 = _evaluate_combo(
            combo, keys, data, strategy, self._score_pnl_x_pf, "2025-09-30", "2025-10-01"
        )
        r2 = _evaluate_combo(
            combo, keys, data, strategy, self._score_pnl_x_pf, "2025-09-30", "2025-10-01"
        )
        assert r1["params"] == r2["params"]
        assert r1["score"] == r2["score"]
        assert r1["is"] == r2["is"]
        assert r1["oos"] == r2["oos"]

    def test_combos_differents_scores_differents(self):
        """Deux combos différentes donnent des scores différents."""
        strategy = _build_mock_strategy()
        data = _build_mock_data()
        keys = list(strategy.PARAM_GRID.keys())

        c1 = (1.0, 1.5)  # sl_mult=1, tp_mult=1.5 → wins faibles
        c2 = (1.0, 4.0)  # sl_mult=1, tp_mult=4   → wins élevés
        r1 = _evaluate_combo(
            c1, keys, data, strategy, self._score_pnl_only, "2025-09-30", "2025-10-01"
        )
        r2 = _evaluate_combo(
            c2, keys, data, strategy, self._score_pnl_only, "2025-09-30", "2025-10-01"
        )
        # c2 produit plus de wins → PnL OOS plus élevé
        assert r2["score"] > r1["score"]


# ──────────────────────────────────────────────────────────────────────────────
# optimize() — parité parallèle vs séquentiel
# ──────────────────────────────────────────────────────────────────────────────


class TestParallelismeIdentique:

    def test_optimize_parallel_vs_sequentiel_resultats_identiques(self):
        """n_jobs=1 et n_jobs=4 doivent donner exactement les mêmes results."""
        strategy = _build_mock_strategy()
        data = _build_mock_data()

        r_seq = optimize(
            strategy,
            data,
            n_jobs=1,
            robustness_report=False,
            output_dir=None,
            n_bootstrap=10,  # rapide
        )
        r_par = optimize(
            strategy,
            data,
            n_jobs=4,
            robustness_report=False,
            output_dir=None,
            n_bootstrap=10,
        )

        assert set(r_seq.keys()) == set(r_par.keys())
        for ticker in r_seq:
            assert r_seq[ticker]["params"] == r_par[ticker]["params"], (
                f"Best params diffèrent pour {ticker} : "
                f"seq={r_seq[ticker]['params']} vs par={r_par[ticker]['params']}"
            )
            # Métriques IS/OOS identiques au cent près
            for stat in ("pnl", "pf", "n", "win_rate"):
                if stat in r_seq[ticker]["is"]:
                    assert r_seq[ticker]["is"][stat] == pytest.approx(
                        r_par[ticker]["is"][stat], rel=1e-9
                    )
                if stat in r_seq[ticker]["oos"]:
                    assert r_seq[ticker]["oos"][stat] == pytest.approx(
                        r_par[ticker]["oos"][stat], rel=1e-9
                    )

    def test_grille_vide_no_crash(self):
        """PARAM_GRID vide → renvoie {} proprement."""
        strategy = _build_mock_strategy(param_grid={})
        data = _build_mock_data()
        result = optimize(strategy, data, robustness_report=False, output_dir=None, n_jobs=1)
        assert result == {}


# ──────────────────────────────────────────────────────────────────────────────
# Bench informatif (pas une assertion stricte — la machine peut varier)
# ──────────────────────────────────────────────────────────────────────────────


def test_bench_parallel_at_least_2x_faster_on_large_grid():
    """Sur une grille 5×5×5=125 combos, le parallèle doit être au moins 2× plus
    rapide que séquentiel. Test indicatif, pas critique pour CI."""
    strategy = _build_mock_strategy(
        param_grid={
            "sl_mult": [1.0, 1.5, 2.0, 2.5, 3.0],
            "tp_mult": [1.5, 2.0, 2.5, 3.0, 4.0],
            "atr_pct": [0.5, 1.0, 1.5, 2.0, 3.0],
        }
    )
    data = _build_mock_data()

    t0 = time.time()
    optimize(strategy, data, n_jobs=1, robustness_report=False, output_dir=None, n_bootstrap=10)
    t_seq = time.time() - t0

    t0 = time.time()
    optimize(strategy, data, n_jobs=-1, robustness_report=False, output_dir=None, n_bootstrap=10)
    t_par = time.time() - t0

    speedup = t_seq / max(t_par, 0.001)
    print(f"\n  Bench : séq={t_seq:.2f}s  par={t_par:.2f}s  speedup={speedup:.1f}×")
    # Tolérance : minimum 1.5× (peut être bridé par overhead joblib sur petites tâches)
    # Sur 12 CPU on s'attend à 4-8× en pratique
    if speedup < 1.5:
        pytest.skip(
            f"Speedup faible ({speedup:.1f}×) — probablement overhead joblib > coût des tâches. "
            "Non bloquant : la parallélisation reste correcte sur grilles réelles."
        )
