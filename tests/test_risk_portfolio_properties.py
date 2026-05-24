"""Property-based tests pour core/risk_portfolio.PortfolioRiskManager.

Vérifie les invariants critiques du garde-fou portfolio (live) :
- limite daily realized respectée (USER_DAILY_LOSS_MAX, TOPSTEP_DAILY_LOSS_MAX)
- limite trailing DD jamais franchie par can_open
- max positions actives respecté
- max trades/jour respecté
- commutativité de register_close
- idempotence de reset()

NE PAS modifier core/risk_portfolio.py. Si un test révèle un bug, l'isoler
dans un commit séparé avec validation utilisateur (cf. invariant ROADMAP_SOLO).
"""

from __future__ import annotations

from datetime import datetime

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from core.risk_portfolio import PortfolioRiskManager

# Bornes raisonnables — ±$5000 couvre tous les scénarios trade-by-trade.
PNL = st.floats(min_value=-500.0, max_value=500.0, allow_nan=False, allow_infinity=False)
RISK = st.floats(min_value=10.0, max_value=300.0, allow_nan=False, allow_infinity=False)


def _build_rm(
    daily_loss_limit: float = 1000.0,
    trailing_dd_limit: float = 2000.0,
    user_daily_loss_max: float = 950.0,
    user_max_open_positions: int = 0,  # 0 = pas de cap actif
    user_max_trades_per_day: int = 0,
    consec_loss_pause_days: int = 0,
    safety_mult: float = 1.0,  # 1.0 = pas de marge — testable purement
) -> PortfolioRiskManager:
    """Construit un manager avec params reproductibles, indépendamment de config.py."""
    return PortfolioRiskManager(
        daily_loss_limit=daily_loss_limit,
        trailing_dd_limit=trailing_dd_limit,
        user_daily_loss_max=user_daily_loss_max,
        user_max_open_positions=user_max_open_positions,
        user_max_trades_per_day=user_max_trades_per_day,
        consec_loss_pause_days=consec_loss_pause_days,
        safety_mult=safety_mult,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Invariant 1 — limite daily (Topstep)
# ──────────────────────────────────────────────────────────────────────────────


class TestDailyLossInvariant:

    @given(rdp=st.floats(min_value=-2000.0, max_value=0.0), risk=RISK)
    @settings(
        max_examples=200, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    def test_rdp_atteint_limite_topstep_refuse(self, rdp, risk):
        """Si realized_day_pnl <= -daily_loss_limit, can_open refuse toujours."""
        rm = _build_rm(daily_loss_limit=1000.0)
        rm.realized_day_pnl = rdp
        assume(rdp <= -rm.daily_loss_limit)
        ok, _ = rm.can_open(risk_usd=risk)
        assert not ok

    @given(
        # On ne génère que les valeurs en-dessous du seuil — pas d'assume() coûteux
        rdp=st.floats(min_value=-2000.0, max_value=-950.0),
        risk=RISK,
    )
    @settings(max_examples=200, deadline=None)
    def test_user_daily_loss_realized_atteint_refuse(self, rdp, risk):
        """Si realized_day_pnl <= -user_daily_loss_max, refus (sauf bypass challenge)."""
        rm = _build_rm(user_daily_loss_max=950.0)
        rm.realized_day_pnl = rdp
        ok, _ = rm.can_open(risk_usd=risk)
        assert not ok


# ──────────────────────────────────────────────────────────────────────────────
# Invariant 2 — slack worst-case
# ──────────────────────────────────────────────────────────────────────────────


class TestSlackInvariant:

    @given(
        rdp=st.floats(min_value=-800.0, max_value=0.0),
        n_active=st.integers(min_value=0, max_value=5),
        risk_active=st.floats(min_value=50.0, max_value=200.0),
        new_risk=st.floats(min_value=50.0, max_value=300.0),
    )
    @settings(max_examples=200, deadline=None)
    def test_si_can_open_alors_pire_cas_respecte_daily(self, rdp, n_active, risk_active, new_risk):
        """Si can_open autorise, alors dans le pire cas (tous active + nouveau → SL),
        la perte journalière reste >= -daily_loss_limit (avec safety_mult=1.0)."""
        rm = _build_rm(safety_mult=1.0, user_max_open_positions=0)
        rm.realized_day_pnl = rdp
        # Simule des positions actives (sans passer par register_open/fill pour rester
        # déterministe et isolé)
        for i in range(n_active):
            rm.active_positions[f"trade_{i}"] = type("Order", (), {"risk_usd": risk_active})()
        ok, _ = rm.can_open(risk_usd=new_risk)
        if ok:
            # pire cas : tous les actifs + nouveau touchent SL
            worst_case_rdp = rdp - n_active * risk_active - new_risk
            assert worst_case_rdp >= -rm.daily_loss_limit - 1e-6, (
                f"can_open True mais worst-case rdp={worst_case_rdp} < -{rm.daily_loss_limit}"
                f"  (rdp={rdp}, n_active={n_active}, risk_active={risk_active}, new={new_risk})"
            )


# ──────────────────────────────────────────────────────────────────────────────
# Invariant 3 — max positions / max trades
# ──────────────────────────────────────────────────────────────────────────────


class TestMaxCapsInvariant:

    @given(
        n_active=st.integers(min_value=0, max_value=10),
        cap=st.integers(min_value=1, max_value=5),
    )
    @settings(max_examples=100, deadline=None)
    def test_max_open_positions_respecte(self, n_active, cap):
        """Si n_active >= user_max_open_positions, can_open refuse."""
        rm = _build_rm(user_max_open_positions=cap)
        for i in range(n_active):
            rm.active_positions[f"trade_{i}"] = type("Order", (), {"risk_usd": 50.0})()
        ok, _ = rm.can_open(risk_usd=100.0)
        if n_active >= cap:
            assert not ok

    @given(
        n_fills=st.integers(min_value=0, max_value=10),
        cap=st.integers(min_value=1, max_value=5),
    )
    @settings(max_examples=100, deadline=None)
    def test_max_trades_per_day_respecte(self, n_fills, cap):
        """Si daily_fills_count >= user_max_trades_per_day, can_open refuse."""
        rm = _build_rm(user_max_trades_per_day=cap)
        rm.daily_fills_count = n_fills
        ok, _ = rm.can_open(risk_usd=100.0)
        if n_fills >= cap:
            assert not ok


# ──────────────────────────────────────────────────────────────────────────────
# Invariant 4 — circuit breaker streak perdant
# ──────────────────────────────────────────────────────────────────────────────


class TestStreakInvariant:

    @given(
        streak=st.integers(min_value=0, max_value=20),
        pause=st.integers(min_value=1, max_value=10),
    )
    @settings(max_examples=100, deadline=None)
    def test_consec_loss_pause_respecte(self, streak, pause):
        """Si consec_loss_days >= consec_loss_pause_days, can_open refuse."""
        rm = _build_rm(consec_loss_pause_days=pause)
        rm.consec_loss_days = streak
        ok, _ = rm.can_open(risk_usd=100.0)
        if streak >= pause:
            assert not ok


# ──────────────────────────────────────────────────────────────────────────────
# Invariant 5 — commutativité register_close
# ──────────────────────────────────────────────────────────────────────────────


class TestRegisterCloseCommutativity:

    @given(pnls=st.lists(PNL, min_size=2, max_size=10))
    @settings(max_examples=100, deadline=None)
    def test_commutativite_cum_pnl(self, pnls):
        """register_close dans n'importe quel ordre → même cum_pnl final."""
        # Ordre 1
        rm1 = _build_rm()
        when = datetime(2026, 5, 22, 14, 0, 0)
        for i, p in enumerate(pnls):
            rm1.register_fill(f"t_{i}", when=when)  # pour incrémenter le compteur
            rm1.register_close(f"t_{i}", pnl=p, when=when)

        # Ordre inversé
        rm2 = _build_rm()
        for i, p in reversed(list(enumerate(pnls))):
            rm2.register_fill(f"t_{i}", when=when)
            rm2.register_close(f"t_{i}", pnl=p, when=when)

        assert abs(rm1.cum_pnl - rm2.cum_pnl) < 1e-9
        assert abs(rm1.realized_day_pnl - rm2.realized_day_pnl) < 1e-9


# ──────────────────────────────────────────────────────────────────────────────
# Invariant 6 — idempotence reset()
# ──────────────────────────────────────────────────────────────────────────────


class TestResetIdempotence:

    @given(pnls=st.lists(PNL, min_size=1, max_size=10))
    @settings(max_examples=50, deadline=None)
    def test_reset_double_egal_simple(self, pnls):
        """reset() suivi de reset() = un seul reset()."""
        rm = _build_rm()
        when = datetime(2026, 5, 22, 14, 0, 0)
        for i, p in enumerate(pnls):
            rm.register_fill(f"t_{i}", when=when)
            rm.register_close(f"t_{i}", pnl=p, when=when)
        rm.reset()
        state1 = (
            rm.cum_pnl,
            rm.peak_pnl,
            rm.realized_day_pnl,
            rm.consec_loss_days,
            rm.daily_fills_count,
            len(rm.pending_orders),
            len(rm.active_positions),
        )
        rm.reset()
        state2 = (
            rm.cum_pnl,
            rm.peak_pnl,
            rm.realized_day_pnl,
            rm.consec_loss_days,
            rm.daily_fills_count,
            len(rm.pending_orders),
            len(rm.active_positions),
        )
        assert state1 == state2

    def test_reset_remet_etat_neuf(self):
        """Après reset(), tous les compteurs et collections sont vides."""
        rm = _build_rm()
        when = datetime(2026, 5, 22, 14, 0, 0)
        rm.register_open("a", risk_usd=100.0, when=when)
        rm.register_fill("a", when=when)
        rm.register_close("a", pnl=50.0, when=when)
        rm.reset()
        assert rm.cum_pnl == 0.0
        assert rm.peak_pnl == 0.0
        assert rm.realized_day_pnl == 0.0
        assert rm.consec_loss_days == 0
        assert rm.daily_fills_count == 0
        assert rm.pending_orders == {}
        assert rm.active_positions == {}


# ──────────────────────────────────────────────────────────────────────────────
# Invariant 7 — peak_pnl est monotone non-décroissant
# ──────────────────────────────────────────────────────────────────────────────


class TestPeakPnlMonotone:

    @given(pnls=st.lists(PNL, min_size=1, max_size=15))
    @settings(max_examples=100, deadline=None)
    def test_peak_pnl_jamais_decroit(self, pnls):
        """peak_pnl est monotone non-décroissant : peak(t+1) >= peak(t)."""
        rm = _build_rm()
        when = datetime(2026, 5, 22, 14, 0, 0)
        last_peak = rm.peak_pnl
        for i, p in enumerate(pnls):
            rm.register_fill(f"t_{i}", when=when)
            rm.register_close(f"t_{i}", pnl=p, when=when)
            assert rm.peak_pnl >= last_peak - 1e-9
            last_peak = rm.peak_pnl


# ──────────────────────────────────────────────────────────────────────────────
# Invariant 8 — cum_pnl = somme exacte des pnls (déterminisme)
# ──────────────────────────────────────────────────────────────────────────────


class TestCumPnlConsistency:

    @given(pnls=st.lists(PNL, min_size=0, max_size=15))
    @settings(max_examples=100, deadline=None)
    def test_cum_pnl_egal_somme(self, pnls):
        """Somme des pnls register_close = cum_pnl final."""
        rm = _build_rm()
        when = datetime(2026, 5, 22, 14, 0, 0)
        for i, p in enumerate(pnls):
            rm.register_fill(f"t_{i}", when=when)
            rm.register_close(f"t_{i}", pnl=p, when=when)
        assert abs(rm.cum_pnl - sum(pnls)) < 1e-6


# ──────────────────────────────────────────────────────────────────────────────
# Sanity checks scénarios
# ──────────────────────────────────────────────────────────────────────────────


class TestSanityScenarios:

    def test_etat_neuf_autorise_trade_nominal(self):
        rm = _build_rm()
        ok, _ = rm.can_open(risk_usd=100.0)
        assert ok

    def test_pending_pas_compte_comme_active(self):
        """register_open arme un pending — n'affecte pas le cap n_active."""
        rm = _build_rm(user_max_open_positions=2)
        when = datetime(2026, 5, 22, 14, 0, 0)
        for i in range(5):
            rm.register_open(f"p_{i}", risk_usd=50.0, when=when)
        # Aucune position active → can_open OK
        ok, _ = rm.can_open(risk_usd=100.0)
        assert ok

    def test_fill_libere_pending_et_compte_active(self):
        rm = _build_rm()
        when = datetime(2026, 5, 22, 14, 0, 0)
        rm.register_open("x", risk_usd=100.0, when=when)
        assert len(rm.pending_orders) == 1
        assert len(rm.active_positions) == 0
        rm.register_fill("x", when=when)
        assert len(rm.pending_orders) == 0
        assert len(rm.active_positions) == 1
        assert rm.daily_fills_count == 1

    def test_cancel_libere_pending_pas_de_fill(self):
        rm = _build_rm()
        when = datetime(2026, 5, 22, 14, 0, 0)
        rm.register_open("x", risk_usd=100.0, when=when)
        rm.cancel_open("x")
        assert len(rm.pending_orders) == 0
        assert rm.daily_fills_count == 0

    def test_cancel_open_inconnu_retourne_false(self):
        """cancel_open sur un id absent → False (pas d'exception)."""
        rm = _build_rm()
        assert rm.cancel_open("inconnu") is False


# ──────────────────────────────────────────────────────────────────────────────
# Roll de jour — transitions de streak
# ──────────────────────────────────────────────────────────────────────────────


class TestRollDay:

    def test_jour_perdant_incrementé_streak(self):
        """Roll de jour avec rdp < 0 → consec_loss_days += 1."""
        rm = _build_rm()
        day1 = datetime(2026, 5, 22, 14, 0, 0)
        day2 = datetime(2026, 5, 23, 14, 0, 0)
        rm.register_fill("a", when=day1)
        rm.register_close("a", pnl=-50.0, when=day1)
        assert rm.realized_day_pnl == -50.0
        assert rm.consec_loss_days == 0
        # Roll vers le lendemain via can_open
        rm.can_open(risk_usd=100.0, when=day2)
        assert rm.consec_loss_days == 1
        assert rm.realized_day_pnl == 0.0  # reset journalier
        assert rm.daily_fills_count == 0

    def test_jour_gagnant_remet_streak_a_zero(self):
        """Roll de jour avec rdp > 0 → consec_loss_days = 0."""
        rm = _build_rm()
        rm.consec_loss_days = 3  # streak en cours
        day1 = datetime(2026, 5, 22, 14, 0, 0)
        day2 = datetime(2026, 5, 23, 14, 0, 0)
        rm._maybe_roll_day(day1)  # initialise current_day
        rm.register_fill("a", when=day1)
        rm.register_close("a", pnl=+100.0, when=day1)
        # Roll vers le lendemain
        rm._maybe_roll_day(day2)
        assert rm.consec_loss_days == 0

    def test_jour_neutre_garde_streak(self):
        """Roll de jour avec rdp == 0 → consec_loss_days inchangé."""
        rm = _build_rm()
        rm.consec_loss_days = 2
        day1 = datetime(2026, 5, 22, 14, 0, 0)
        day2 = datetime(2026, 5, 23, 14, 0, 0)
        rm._maybe_roll_day(day1)
        # Pas de fill ce jour
        rm._maybe_roll_day(day2)
        assert rm.consec_loss_days == 2


# ──────────────────────────────────────────────────────────────────────────────
# register_close — breach detection
# ──────────────────────────────────────────────────────────────────────────────


class TestBreachDetection:

    def test_daily_loss_breach_signale(self):
        """register_close qui pousse rdp ≤ -daily_loss_limit → (True, reason)."""
        rm = _build_rm(daily_loss_limit=1000.0)
        when = datetime(2026, 5, 22, 14, 0, 0)
        rm.register_fill("a", when=when)
        breach, reason = rm.register_close("a", pnl=-1000.0, when=when)
        assert breach
        assert "daily_loss_breached" in reason

    def test_trailing_dd_breach_signale(self):
        """register_close qui pousse cum_pnl sous trail_floor → (True, reason)."""
        rm = _build_rm(trailing_dd_limit=2000.0)
        when = datetime(2026, 5, 22, 14, 0, 0)
        # Construit un peak puis cum_pnl en-dessous du floor
        rm.cum_pnl = 1500.0
        rm.peak_pnl = 1500.0
        rm.register_fill("a", when=when)
        # Perte qui pousse cum_pnl à -500 → floor = 1500 - 2000 = -500, donc <= floor
        breach, reason = rm.register_close("a", pnl=-2000.0, when=when)
        assert breach
        assert "trailing_dd_breached" in reason

    def test_close_normal_pas_de_breach(self):
        rm = _build_rm()
        when = datetime(2026, 5, 22, 14, 0, 0)
        rm.register_fill("a", when=when)
        breach, reason = rm.register_close("a", pnl=50.0, when=when)
        assert not breach
        assert reason == "ok"
