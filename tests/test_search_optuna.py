"""Tests du backend de recherche Optuna (core/search_optuna.py).

Vérifie :
- build_search_space : normalisation PARAM_SPACE + fallback PARAM_GRID → cat
- run_optuna_search : déterminisme (seed fixe), invariant IS-only, sentinel None
- optimize(search=...) : parité grid ≡ auto (petite grille), n_strategies_tested
  honnête transmis à la robustesse (grid = taille grille, optuna = jeux évalués)
- run_multifold(search="optuna") : déterminisme inter-runs
"""

from __future__ import annotations

import pytest

pytest.importorskip("optuna")

from core.optimizer import _default_score, optimize
from core.search_optuna import build_search_space, run_optuna_search
from core.walkforward import run_multifold
from tests._mock_strategy import _build_mock_data, _build_mock_strategy

OOS_ARGS = {"is_end": "2025-09-30", "oos_start": "2025-10-01", "is_start": "2024-09-01"}


def _search(strategy, data, n_trials=25, seed=42):
    return run_optuna_search(
        strategy,
        data,
        _default_score,
        OOS_ARGS["is_end"],
        OOS_ARGS["oos_start"],
        OOS_ARGS["is_start"],
        None,
        n_trials,
        seed=seed,
    )


# ──────────────────────────────────────────────────────────────────────────────
# build_search_space
# ──────────────────────────────────────────────────────────────────────────────


class TestBuildSearchSpace:

    def test_fallback_param_grid_vers_categorielles(self):
        strategy = _build_mock_strategy()
        space = build_search_space(strategy)
        assert set(space) == set(strategy.PARAM_GRID)
        for key, spec in space.items():
            assert spec[0] == "cat"
            assert spec[1] == list(strategy.PARAM_GRID[key])

    def test_param_space_normalise(self):
        strategy = _build_mock_strategy(
            param_space={
                "sl_mult": ("float", 0.3, 2.5),
                "wick": ("float", 0.05, 0.8, "log"),
                "n_bars": ("int", 2, 10),
                "f2": ("cat", [None, 0.10, 0.15]),
            }
        )
        space = build_search_space(strategy)
        assert space["sl_mult"] == ("float", 0.3, 2.5, False)
        assert space["wick"] == ("float", 0.05, 0.8, True)
        assert space["n_bars"] == ("int", 2, 10)
        assert space["f2"] == ("cat", [None, 0.10, 0.15])

    def test_type_inconnu_leve(self):
        strategy = _build_mock_strategy(param_space={"x": ("bool", True, False)})
        with pytest.raises(ValueError, match="type inconnu"):
            build_search_space(strategy)


# ──────────────────────────────────────────────────────────────────────────────
# run_optuna_search — déterminisme, IS-only, sentinel None
# ──────────────────────────────────────────────────────────────────────────────


class TestRunOptunaSearch:

    def test_deterministe_meme_seed(self):
        """Deux runs seed=42 → mêmes trials (ordonnés) et même best."""
        strategy = _build_mock_strategy()
        data = _build_mock_data()
        o1 = _search(strategy, data)
        o2 = _search(strategy, data)
        assert o1["trials"] == o2["trials"]
        assert o1["n_evaluated"] == o2["n_evaluated"]
        best1 = max(o1["results"], key=lambda r: r["score"])
        best2 = max(o2["results"], key=lambda r: r["score"])
        assert best1["params"] == best2["params"]
        assert best1["score"] == best2["score"]

    def test_seeds_differents_sequences_differentes(self):
        strategy = _build_mock_strategy(
            param_space={"sl_mult": ("float", 0.5, 3.0), "tp_mult": ("float", 1.0, 5.0)}
        )
        data = _build_mock_data()
        o1 = _search(strategy, data, seed=42)
        o2 = _search(strategy, data, seed=43)
        assert o1["trials"] != o2["trials"]

    def test_invariant_is_only(self):
        """OOS modifié, IS identique → mêmes trials, mêmes scores, même best.

        Si le sampler (ou le score) lisait l'OOS, les séquences divergeraient.
        """
        data = _build_mock_data()
        s_base = _build_mock_strategy()
        s_oos_modifie = _build_mock_strategy(oos_pnl_offset=37.0)
        o1 = _search(s_base, data)
        o2 = _search(s_oos_modifie, data)
        # Trials : mêmes params suggérés, mêmes scores (IS-only)
        assert o1["trials"] == o2["trials"]
        best1 = max(o1["results"], key=lambda r: r["score"])
        best2 = max(o2["results"], key=lambda r: r["score"])
        assert best1["params"] == best2["params"]
        # ... mais l'OOS, lui, a bien bougé (sanity check du mock)
        assert best1["oos"]["pnl"] != best2["oos"]["pnl"]

    def test_fallback_grid_best_dans_la_grille(self):
        """Sans PARAM_SPACE, chaque param du best ∈ valeurs de PARAM_GRID."""
        strategy = _build_mock_strategy()
        data = _build_mock_data()
        out = _search(strategy, data)
        best = max(out["results"], key=lambda r: r["score"])
        for key, val in best["params"].items():
            assert val in strategy.PARAM_GRID[key]

    def test_categorielle_avec_none_decodee(self):
        """None encodé en sentinel pour optuna, décodé partout en sortie."""
        strategy = _build_mock_strategy(
            param_grid={"sl_mult": [1.0, 2.0], "f2_min_atr": [None, 0.10, 0.15]}
        )
        data = _build_mock_data()
        out = _search(strategy, data, n_trials=15)
        observed = set()
        for r in out["results"]:
            assert r["params"]["f2_min_atr"] in (None, 0.10, 0.15)
            observed.add(r["params"]["f2_min_atr"])
        for t in out["trials"]:
            assert t.get("f2_min_atr") != "__none__"
        # Avec 15 trials sur 6 combos, le None doit avoir été exploré
        assert None in observed

    def test_memoization_compte_jeux_distincts(self):
        """n_evaluated = jeux distincts ≤ n_trials (doublons TPE memoizés)."""
        strategy = _build_mock_strategy(param_grid={"sl_mult": [1.0, 2.0], "tp_mult": [1.5, 3.0]})
        data = _build_mock_data()
        out = _search(strategy, data, n_trials=30)
        assert out["n_evaluated"] <= 4  # 4 combos possibles seulement
        assert len(out["results"]) == out["n_evaluated"]
        assert len(out["trials"]) == 30


# ──────────────────────────────────────────────────────────────────────────────
# optimize() — dispatch backend + n_strategies_tested
# ──────────────────────────────────────────────────────────────────────────────


class TestOptimizeDispatch:

    def test_auto_equivaut_grid_sur_petite_grille(self):
        """Grille 16 ≤ OPTIMIZER_GRID_MAX_COMBOS → auto = grid, bit-à-bit."""
        strategy = _build_mock_strategy()
        data = _build_mock_data()
        common = dict(n_jobs=1, robustness_report=False, output_dir=None, n_bootstrap=10)
        r_grid = optimize(strategy, data, search="grid", **common)
        r_auto = optimize(strategy, data, search="auto", **common)
        assert set(r_grid) == set(r_auto)
        for ticker in r_grid:
            assert r_grid[ticker]["params"] == r_auto[ticker]["params"]
            assert r_grid[ticker]["is"] == r_auto[ticker]["is"]
            assert r_grid[ticker]["oos"] == r_auto[ticker]["oos"]

    def test_n_strategies_tested_grid_vs_optuna(self, monkeypatch):
        """Bonferroni/DSR : grid reçoit |grille|, optuna le nb de jeux évalués."""
        captured = {}

        def fake_rfr(trades, n_strategies_tested, topstep_dd_remaining, seed):
            captured["n"] = n_strategies_tested
            return {}

        monkeypatch.setattr("core.robustness.run_full_robustness", fake_rfr)
        monkeypatch.setattr("core.robustness.format_summary_markdown", lambda r: "")

        strategy = _build_mock_strategy()  # grille 4×4 = 16
        data = _build_mock_data()
        common = dict(n_jobs=1, robustness_report=True, output_dir=None, n_bootstrap=10)

        optimize(strategy, data, search="grid", **common)
        assert captured["n"] == 16

        n_trials = 20
        optimize(strategy, data, search="optuna", n_trials=n_trials, **common)
        assert 1 <= captured["n"] <= n_trials

    def test_optuna_deterministe_via_optimize(self):
        """optimize(search='optuna') : 2 runs → mêmes best params par ticker."""
        strategy = _build_mock_strategy()
        data = _build_mock_data()
        common = dict(
            n_jobs=1, robustness_report=False, output_dir=None, n_bootstrap=10, n_trials=15
        )
        r1 = optimize(strategy, data, search="optuna", **common)
        r2 = optimize(strategy, data, search="optuna", **common)
        assert set(r1) == set(r2)
        for ticker in r1:
            assert r1[ticker]["params"] == r2[ticker]["params"]


# ──────────────────────────────────────────────────────────────────────────────
# run_multifold — backend optuna
# ──────────────────────────────────────────────────────────────────────────────


class TestMultifoldOptuna:

    def test_multifold_optuna_deterministe(self):
        strategy = _build_mock_strategy()
        data = _build_mock_data(tickers=("T1",))
        common = dict(n_jobs=1, output_dir=None, search="optuna", n_trials=15)
        r1 = run_multifold(strategy, data, **common)
        r2 = run_multifold(strategy, data, **common)
        assert set(r1) == set(r2)
        for ticker in r1:
            params1 = [f["params"] for f in r1[ticker]["folds"]]
            params2 = [f["params"] for f in r2[ticker]["folds"]]
            assert params1 == params2
            assert r1[ticker]["stitched"] == r2[ticker]["stitched"]
