"""Property-based tests pour core/risk_topstep.trade_allowed.

Vérifie les invariants fondamentaux du garde-fou Topstep par trade :
- monotonie (plus on est près des limites, moins on autorise)
- cohérence avec la limite daily
- cohérence avec la limite trailing DD
- impact de la safety_mult
- déterminisme

NE PAS modifier core/risk_topstep.py. Si un test révèle un bug, l'isoler
dans un commit séparé avec validation utilisateur (cf. invariant ROADMAP_SOLO).
"""

from __future__ import annotations

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from config import (
    RISK_PER_TRADE_USD,
    TOPSTEP_DAILY_LOSS_MAX,
    TOPSTEP_SAFETY_MULT,
    TOPSTEP_TRAILING_DD,
)
from core.risk_topstep import trade_allowed

# Bornes raisonnables pour le fuzzing — ±$10 000 couvre tous les scénarios réalistes.
DOLLARS = st.floats(min_value=-10_000.0, max_value=10_000.0, allow_nan=False, allow_infinity=False)
POS_DOLLARS = st.floats(min_value=0.0, max_value=10_000.0, allow_nan=False, allow_infinity=False)
RISK = st.floats(min_value=1.0, max_value=2000.0, allow_nan=False, allow_infinity=False)


# ──────────────────────────────────────────────────────────────────────────────
# Invariants fonctionnels — trade_allowed pure
# ──────────────────────────────────────────────────────────────────────────────


class TestTradeAllowedInvariants:

    @given(day_pnl=DOLLARS, cum_pnl=DOLLARS, peak_pnl=POS_DOLLARS, risk=RISK)
    @settings(max_examples=300, deadline=None)
    def test_deterministe(self, day_pnl, cum_pnl, peak_pnl, risk):
        """Mêmes arguments → même résultat (pas d'aléatoire)."""
        a1 = trade_allowed(day_pnl, cum_pnl, peak_pnl, risk)
        a2 = trade_allowed(day_pnl, cum_pnl, peak_pnl, risk)
        assert a1 == a2

    @given(day_pnl=DOLLARS, cum_pnl=DOLLARS, peak_pnl=POS_DOLLARS, risk=RISK)
    @settings(max_examples=300, deadline=None)
    def test_invariant_daily_loss_hard_floor(self, day_pnl, cum_pnl, peak_pnl, risk):
        """Si day_pnl <= -DAILY_LOSS_MAX, slack daily <= 0 → trade refusé."""
        # remaining_daily = DAILY_LOSS_MAX + day_pnl ; si day_pnl ≤ -DAILY_LOSS_MAX
        # alors remaining_daily ≤ 0 et le slack min(daily, trail) ≤ 0 ≤ threshold
        assume(day_pnl <= -TOPSTEP_DAILY_LOSS_MAX)
        ok, _ = trade_allowed(day_pnl, cum_pnl, peak_pnl, risk)
        assert not ok

    @given(day_pnl=DOLLARS, cum_pnl=DOLLARS, peak_pnl=POS_DOLLARS, risk=RISK)
    @settings(max_examples=300, deadline=None)
    def test_invariant_trailing_dd_hard_floor(self, day_pnl, cum_pnl, peak_pnl, risk):
        """Si cum_pnl - (peak - trailing_dd) <= 0, trade refusé (slack trail <= 0)."""
        trail_floor = peak_pnl - TOPSTEP_TRAILING_DD
        assume(cum_pnl <= trail_floor)
        ok, _ = trade_allowed(day_pnl, cum_pnl, peak_pnl, risk)
        assert not ok

    @given(
        day_pnl=DOLLARS,
        peak_pnl=POS_DOLLARS,
        risk=RISK,
        delta=st.floats(min_value=0.01, max_value=5000.0, allow_nan=False),
    )
    @settings(max_examples=300, deadline=None)
    def test_monotonie_cum_pnl_croissant(self, day_pnl, peak_pnl, risk, delta):
        """À paramètres fixes, augmenter cum_pnl ne peut que rendre allowed=True ou stable."""
        cum_low = -500.0
        cum_high = cum_low + delta
        # peak doit être ≥ max(cum_pnl) pour rester cohérent
        peak = max(peak_pnl, cum_high)
        a_low, _ = trade_allowed(day_pnl, cum_low, peak, risk)
        a_high, _ = trade_allowed(day_pnl, cum_high, peak, risk)
        # Si a_low autorise, a_high doit l'autoriser aussi (slack ne peut que croître)
        if a_low:
            assert a_high

    @given(
        cum_pnl=DOLLARS,
        peak_pnl=POS_DOLLARS,
        risk=RISK,
        delta=st.floats(min_value=0.01, max_value=5000.0, allow_nan=False),
    )
    @settings(max_examples=300, deadline=None)
    def test_monotonie_day_pnl_croissant(self, cum_pnl, peak_pnl, risk, delta):
        """À paramètres fixes, augmenter day_pnl ne peut que rendre allowed=True ou stable."""
        day_low = -500.0
        day_high = day_low + delta
        a_low, _ = trade_allowed(day_low, cum_pnl, peak_pnl, risk)
        a_high, _ = trade_allowed(day_high, cum_pnl, peak_pnl, risk)
        if a_low:
            assert a_high

    @given(
        day_pnl=DOLLARS,
        cum_pnl=DOLLARS,
        peak_pnl=POS_DOLLARS,
        risk_low=st.floats(min_value=1.0, max_value=500.0),
        delta=st.floats(min_value=1.0, max_value=500.0),
    )
    @settings(max_examples=300, deadline=None)
    def test_monotonie_risk_decroissant(self, day_pnl, cum_pnl, peak_pnl, risk_low, delta):
        """À paramètres fixes, augmenter le risque ne peut que durcir la décision."""
        risk_high = risk_low + delta
        a_high, _ = trade_allowed(day_pnl, cum_pnl, peak_pnl, risk_high)
        a_low, _ = trade_allowed(day_pnl, cum_pnl, peak_pnl, risk_low)
        # Si on autorise un gros risque, on autorise nécessairement le petit
        if a_high:
            assert a_low

    @given(day_pnl=DOLLARS, cum_pnl=DOLLARS, peak_pnl=POS_DOLLARS, risk=RISK)
    @settings(max_examples=300, deadline=None)
    def test_reason_format(self, day_pnl, cum_pnl, peak_pnl, risk):
        """`reason` est toujours une string non vide."""
        _, reason = trade_allowed(day_pnl, cum_pnl, peak_pnl, risk)
        assert isinstance(reason, str)
        assert len(reason) > 0
        # Si refusé, raison commence par 'topstep_slack' (format documenté)
        ok, reason = trade_allowed(day_pnl, cum_pnl, peak_pnl, risk)
        if not ok:
            assert "topstep_slack" in reason or "ok" not in reason


# ──────────────────────────────────────────────────────────────────────────────
# Tests scénarios concrets (sanity checks)
# ──────────────────────────────────────────────────────────────────────────────


class TestScenariosConcrets:

    def test_etat_neuf_autorise(self):
        """État zéro (compte neuf) → trade nominal autorisé."""
        ok, reason = trade_allowed(
            day_pnl=0.0, cum_pnl=0.0, peak_pnl=0.0, risk_per_trade=RISK_PER_TRADE_USD
        )
        # Avec DAILY_LOSS_MAX et TRAILING_DD défaut, le slack est largement positif
        assert ok, f"refusé alors que compte neuf : {reason}"

    def test_au_seuil_daily(self):
        """Day_pnl = -DAILY_LOSS_MAX + threshold → exactement à la limite, refusé."""
        threshold = RISK_PER_TRADE_USD * TOPSTEP_SAFETY_MULT
        day_pnl = -TOPSTEP_DAILY_LOSS_MAX + threshold - 0.01
        ok, _ = trade_allowed(day_pnl, 0.0, 0.0, RISK_PER_TRADE_USD)
        assert not ok

    def test_au_seuil_trailing(self):
        """cum_pnl exactement au trailing floor → refusé."""
        peak = 1000.0
        cum = peak - TOPSTEP_TRAILING_DD  # exactement au floor
        ok, _ = trade_allowed(day_pnl=0.0, cum_pnl=cum, peak_pnl=peak, risk_per_trade=100.0)
        assert not ok

    def test_marge_confortable(self):
        """Avec marge des deux côtés → autorisé."""
        ok, _ = trade_allowed(
            day_pnl=-100.0,  # bien au-dessus de la limite daily
            cum_pnl=500.0,  # cum_pnl > peak - trailing_dd
            peak_pnl=500.0,
            risk_per_trade=100.0,
        )
        assert ok


# ──────────────────────────────────────────────────────────────────────────────
# Stress numérique
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("risk", [1.0, 50.0, 100.0, 200.0, 500.0])
def test_decision_change_au_passage_du_seuil(risk):
    """Au seuil exact, le passage d'une fraction de cent fait basculer la décision."""
    threshold = risk * TOPSTEP_SAFETY_MULT
    # slack = threshold + epsilon → autorisé
    day_pnl_ok = -TOPSTEP_DAILY_LOSS_MAX + threshold + 1.0
    ok_above, _ = trade_allowed(day_pnl_ok, 0.0, 0.0, risk)
    # slack = threshold - epsilon → refusé
    day_pnl_ko = -TOPSTEP_DAILY_LOSS_MAX + threshold - 1.0
    ok_below, _ = trade_allowed(day_pnl_ko, 0.0, 0.0, risk)
    assert ok_above
    assert not ok_below
