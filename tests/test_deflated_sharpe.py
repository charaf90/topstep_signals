"""Tests pour core/robustness.deflated_sharpe_ratio.

DSR (Bailey & López de Prado 2014) — propriétés vérifiées :
- n_tests=1 → DSR ≡ PSR(0) (cas particulier)
- n_tests croissant → DSR décroissant (pénalité data snooping)
- Variance fournie vs estimée → résultats cohérents
- Robustesse aux inputs dégénérés
"""

from __future__ import annotations

import numpy as np
import pytest

from core.robustness import (
    _expected_max_sharpe,
    deflated_sharpe_ratio,
    probabilistic_sharpe_ratio,
)

# ──────────────────────────────────────────────────────────────────────────────
# _expected_max_sharpe — formule extreme value theory
# ──────────────────────────────────────────────────────────────────────────────


class TestExpectedMaxSharpe:

    def test_n_tests_1_donne_zero(self):
        """E[max{SR}] = 0 quand on n'a qu'une configuration testée."""
        assert _expected_max_sharpe(n_tests=1, variance_of_sr_estimates=0.01) == 0.0

    def test_variance_zero_donne_zero(self):
        """Sans variance entre les SRs, pas de correction."""
        assert _expected_max_sharpe(n_tests=100, variance_of_sr_estimates=0.0) == 0.0

    def test_croissant_avec_n_tests(self):
        """Plus on teste de configurations, plus le seuil monte (data snooping)."""
        v = 0.01
        small = _expected_max_sharpe(n_tests=10, variance_of_sr_estimates=v)
        large = _expected_max_sharpe(n_tests=1000, variance_of_sr_estimates=v)
        assert large > small > 0

    def test_proportionnel_a_sqrt_variance(self):
        """E[max{SR}] est linéaire en √V."""
        n = 100
        emax_v1 = _expected_max_sharpe(n_tests=n, variance_of_sr_estimates=0.01)
        emax_v4 = _expected_max_sharpe(n_tests=n, variance_of_sr_estimates=0.04)
        # Variance ×4 → √V ×2 → emax ×2
        assert emax_v4 == pytest.approx(2.0 * emax_v1, rel=1e-9)


# ──────────────────────────────────────────────────────────────────────────────
# deflated_sharpe_ratio — propriétés
# ──────────────────────────────────────────────────────────────────────────────


class TestDsr:

    @staticmethod
    def _generate_pnls(n: int = 200, mean: float = 5.0, std: float = 50.0, seed: int = 42):
        """Génère une série synthétique avec PnL de gain moyen positif."""
        rng = np.random.default_rng(seed)
        return rng.normal(loc=mean, scale=std, size=n)

    def test_n_tests_1_equivalent_psr(self):
        """DSR avec n_tests=1 doit donner le même résultat que PSR(0)."""
        pnls = self._generate_pnls()
        # Pour avoir l'équivalence stricte, il faut variance_of_sr_estimates=0
        # (sinon E[max] = 0 par notre formule quand n_tests=1, ce qui marche aussi)
        psr_result = probabilistic_sharpe_ratio(pnls, sr_target=0.0)
        dsr_result = deflated_sharpe_ratio(pnls, n_tests=1)
        assert dsr_result["dsr"] == pytest.approx(psr_result["psr"], rel=1e-9)

    def test_dsr_decroissant_avec_n_tests(self):
        """DSR doit décroître quand on teste plus de configurations."""
        pnls = self._generate_pnls()
        dsr_1 = deflated_sharpe_ratio(pnls, n_tests=1)["dsr"]
        dsr_10 = deflated_sharpe_ratio(pnls, n_tests=10)["dsr"]
        dsr_100 = deflated_sharpe_ratio(pnls, n_tests=100)["dsr"]
        assert dsr_1 >= dsr_10 >= dsr_100

    def test_sr_target_croissant_avec_n_tests(self):
        """sr_target_deflated croît avec n_tests (pénalité plus forte)."""
        pnls = self._generate_pnls()
        st_10 = deflated_sharpe_ratio(pnls, n_tests=10)["sr_target_deflated"]
        st_100 = deflated_sharpe_ratio(pnls, n_tests=100)["sr_target_deflated"]
        st_1000 = deflated_sharpe_ratio(pnls, n_tests=1000)["sr_target_deflated"]
        assert st_10 < st_100 < st_1000

    def test_variance_fournie_change_resultat(self):
        """variance_of_sr_estimates fourni explicitement → cohérent avec _expected_max_sharpe."""
        pnls = self._generate_pnls()
        d_default = deflated_sharpe_ratio(pnls, n_tests=50)
        d_explicit = deflated_sharpe_ratio(pnls, n_tests=50, variance_of_sr_estimates=0.05)
        assert d_default["dsr"] != d_explicit["dsr"]
        # Plus la variance explicite est grande, plus le sr_target monte
        assert d_explicit["sr_target_deflated"] > d_default["sr_target_deflated"]

    def test_trop_peu_d_observations(self):
        """< 30 trades → erreur (pas assez stable statistiquement)."""
        pnls = np.array([1.0, 2.0, -3.0, 4.0])
        result = deflated_sharpe_ratio(pnls, n_tests=5)
        assert "error" in result

    def test_ecart_type_nul(self):
        """Tous les PnL identiques → erreur."""
        pnls = np.full(100, 10.0)
        result = deflated_sharpe_ratio(pnls, n_tests=5)
        assert "error" in result

    def test_champs_complets(self):
        """Le dict retourné contient tous les champs documentés."""
        pnls = self._generate_pnls()
        result = deflated_sharpe_ratio(pnls, n_tests=10)
        expected_keys = {
            "n",
            "n_tests",
            "sharpe_per_trade",
            "sharpe_annualized",
            "skewness",
            "kurtosis",
            "variance_of_sr_estimates",
            "sr_target_deflated",
            "dsr",
            "dsr_pct",
        }
        assert expected_keys.issubset(set(result.keys()))
        assert 0.0 <= result["dsr"] <= 1.0
        assert result["dsr_pct"] == pytest.approx(result["dsr"] * 100, rel=1e-9)

    def test_pnls_perdants_dsr_faible(self):
        """Si la stratégie perd en moyenne, DSR doit être faible (< 50%)."""
        rng = np.random.default_rng(42)
        losing_pnls = rng.normal(loc=-5.0, scale=50.0, size=200)
        result = deflated_sharpe_ratio(losing_pnls, n_tests=10)
        assert result["dsr_pct"] < 50.0

    def test_pnls_gagnants_dsr_eleve(self):
        """Strat gagnante avec n_tests=1 → DSR > 80%."""
        rng = np.random.default_rng(42)
        winning_pnls = rng.normal(loc=20.0, scale=50.0, size=200)
        result = deflated_sharpe_ratio(winning_pnls, n_tests=1)
        assert result["dsr_pct"] > 80.0


# ──────────────────────────────────────────────────────────────────────────────
# Intégration dans run_full_robustness
# ──────────────────────────────────────────────────────────────────────────────


def test_run_full_robustness_inclut_dsr():
    """Le pipeline complet doit retourner une clé `dsr` correctement formée."""
    import pandas as pd

    from core.robustness import run_full_robustness

    rng = np.random.default_rng(42)
    n = 100
    trades = pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=n, freq="D").strftime("%Y-%m-%d"),
            "pnl": rng.normal(loc=10.0, scale=50.0, size=n),
            "result": ["TP"] * n,
            "dir": ["long"] * n,
            "regime": ["trending"] * n,
        }
    )
    out = run_full_robustness(trades, n_strategies_tested=16, seed=42)
    assert "dsr" in out
    assert "error" not in out["dsr"]
    assert out["dsr"]["n_tests"] == 16


# ──────────────────────────────────────────────────────────────────────────────
# Intégration dans augment_verdict
# ──────────────────────────────────────────────────────────────────────────────


def test_augment_verdict_flag_dsr_faible():
    """Si DSR < 95% → verdict 🟢 rétrogradé en 🟡."""
    from core.metrics import augment_verdict

    robustness = {
        "bootstrap_pf": {"p_above_threshold": 100.0, "n_trades": 100},
        "bonferroni": {"bootstrap_threshold_pct": 99.0},
        "psr": {"psr_pct": 99.0},
        "dsr": {"dsr_pct": 60.0},  # < 95%
        "monte_carlo_dd": {"dd_topstep_breach_pct": 1.0},
        "regime_stress": [],
    }
    verdict, flags = augment_verdict("🟢", robustness)
    assert verdict == "🟡"
    assert any("DSR" in f for f in flags)


def test_augment_verdict_dsr_eleve_pas_de_flag():
    """DSR ≥ 95% → pas de drapeau levé."""
    from core.metrics import augment_verdict

    robustness = {
        "bootstrap_pf": {"p_above_threshold": 100.0, "n_trades": 100},
        "bonferroni": {"bootstrap_threshold_pct": 99.0},
        "psr": {"psr_pct": 99.0},
        "dsr": {"dsr_pct": 98.0},
        "monte_carlo_dd": {"dd_topstep_breach_pct": 1.0},
        "regime_stress": [],
    }
    verdict, flags = augment_verdict("🟢", robustness)
    assert verdict == "🟢"
    assert not any("DSR" in f for f in flags)
