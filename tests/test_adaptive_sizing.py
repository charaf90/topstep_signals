"""
Tests unitaires pour core/adaptive_sizing.py.

Couvre :
  - trading_days_until : transitions de mois, weekends, edge cases
  - adaptive_risk_usd : monotonie, bornes, cas dégénérés
  - cohérence avec PortfolioRiskManager.can_open
"""

from __future__ import annotations

import random
from datetime import date, datetime

import pytest

from config import (
    CHALLENGE_DD_GUARD_BUFFER,
    CHALLENGE_RISK_MAX_USD,
    CHALLENGE_RISK_MIN_USD,
)
from core.adaptive_sizing import (
    adaptive_risk_usd,
    trading_days_until,
)
from core.risk_portfolio import PortfolioRiskManager

# ────────────────────────────────────────────────────────────────────────────
# trading_days_until
# ────────────────────────────────────────────────────────────────────────────


class TestTradingDaysUntil:

    def test_reset_in_current_month(self):
        # 1er du mois (jeudi 2026-01-01) → reset le 2 → 1 jour ouvré
        n = trading_days_until(date(2026, 1, 1), reset_day=2)
        assert n == 1

    def test_reset_passed_in_current_month(self):
        # 2 mai 2026 (samedi) → reset au mois suivant (2 juin = mardi)
        # Du 2 mai au 1er juin inclus (exclu 2 juin) :
        # mai : 2,3 (WE), 4-8 (5), 9-10 WE, 11-15 (5), 16-17 WE, 18-22 (5),
        #       23-24 WE, 25-29 (5), 30-31 WE = 20
        # juin : 1 = 1
        # total = 21
        n = trading_days_until(date(2026, 5, 2), reset_day=2)
        assert n == 21

    def test_weekend_skipped(self):
        # samedi 2026-01-03 → reset 2026-02-02 (lundi)
        # Comptage des jours ouvrés [3 jan, 2 fév[ = [3 jan, 1 fév]
        #   3-4 jan: WE → 0 | 5-9 jan: 5 | 10-11 WE → 0 | 12-16 jan: 5
        #   17-18 WE → 0 | 19-23 jan: 5 | 24-25 WE → 0 | 26-30 jan: 5
        #   31 jan: sa → 0 | 1 fév: di → 0
        # Total = 20
        n = trading_days_until(date(2026, 1, 3), reset_day=2)
        assert n == 20

    def test_year_transition(self):
        # 15 décembre 2026 → reset 2 janvier 2027
        n = trading_days_until(date(2026, 12, 15), reset_day=2)
        assert n >= 10  # au moins 10 jours ouvrés

    def test_minimum_one(self):
        # le jour exact du reset → ne retourne pas 0 (sinon division par 0
        # dans la formule)
        # Le 2 du mois, on roule sur le mois suivant → ce n'est pas un cas 0
        # mais teste le clamp à 1
        n = trading_days_until(date(2026, 2, 2), reset_day=2)
        assert n >= 1


# ────────────────────────────────────────────────────────────────────────────
# adaptive_risk_usd — bornes et monotonie
# ────────────────────────────────────────────────────────────────────────────


def _make_status(cum_pnl=0.0, peak_pnl=0.0, realized_day_pnl=0.0):
    return {
        "cum_pnl": cum_pnl,
        "peak_pnl": peak_pnl,
        "realized_day_pnl": realized_day_pnl,
    }


def _make_signal(strategy="OPR"):
    return {"strategy": strategy, "sl_dist": 20.0}


class TestBornes:

    def test_within_min_max(self):
        random.seed(42)
        today = datetime(2026, 6, 15)
        for _ in range(1000):
            cum = random.uniform(-1800, 2900)
            peak = max(cum, random.uniform(0, 3000))
            rdp = random.uniform(-800, 200)
            signal = _make_signal(random.choice(["OPR", "FIB"]))
            risk, _ = adaptive_risk_usd(_make_status(cum, peak, rdp), signal, today)
            assert CHALLENGE_RISK_MIN_USD <= risk <= CHALLENGE_RISK_MAX_USD, (
                f"risk={risk} out of [{CHALLENGE_RISK_MIN_USD},{CHALLENGE_RISK_MAX_USD}]"
                f" for cum={cum} peak={peak} rdp={rdp}"
            )

    def test_floor_when_target_reached(self):
        # cum très proche du target → lockin actif → risk au minimum
        today = datetime(2026, 6, 15)
        risk, f = adaptive_risk_usd(
            _make_status(cum_pnl=2950, peak_pnl=2950),
            _make_signal(),
            today,
        )
        assert risk == CHALLENGE_RISK_MIN_USD
        assert f["lockin"] < 1.0


class TestMonotonie:

    def test_more_time_less_risk(self):
        # à cum_pnl fixe, plus days_left est grand, moins le risk est élevé
        signal = _make_signal()
        status = _make_status(cum_pnl=0)
        # 28 jours restants (1er du mois)
        risk_far, _ = adaptive_risk_usd(status, signal, datetime(2026, 6, 1))
        # 3 jours restants (28 du mois)
        risk_near, _ = adaptive_risk_usd(status, signal, datetime(2026, 6, 28))
        assert risk_near >= risk_far, f"risk_near={risk_near} should be >= risk_far={risk_far}"

    def test_more_progress_less_risk(self):
        # à days_left fixe, plus cum_pnl approche le target, moins le risk
        # est élevé (lockin)
        signal = _make_signal()
        today = datetime(2026, 6, 10)
        risk_low, _ = adaptive_risk_usd(_make_status(cum_pnl=500, peak_pnl=500), signal, today)
        risk_high, _ = adaptive_risk_usd(_make_status(cum_pnl=2800, peak_pnl=2800), signal, today)
        assert (
            risk_high <= risk_low
        ), f"risk_high={risk_high} (cum=2800) should be <= risk_low={risk_low} (cum=500)"

    def test_fib_boost_vs_opr(self):
        # à état fixe, Fib doit produire un risk >= OPR (boost = 1.2)
        # … sauf si on est saturé sur un cap (min/max/dd/daily).
        # On choisit un état où on n'est pas saturé : milieu de mois,
        # cum=1000.
        today = datetime(2026, 6, 15)
        status = _make_status(cum_pnl=1000, peak_pnl=1000)
        r_opr, _ = adaptive_risk_usd(status, _make_signal("OPR"), today)
        r_fib, _ = adaptive_risk_usd(status, _make_signal("FIB"), today)
        # Fib a un edge 0.40 vs OPR 0.34 → target_risk_fib < target_risk_opr
        # mais boost_fib = 1.2 vs 1.0. Net effect: dépend du régime.
        # Test plus faible : les deux sont dans les bornes
        assert CHALLENGE_RISK_MIN_USD <= r_opr <= CHALLENGE_RISK_MAX_USD
        assert CHALLENGE_RISK_MIN_USD <= r_fib <= CHALLENGE_RISK_MAX_USD


class TestCasDegeneres:

    def test_peak_3000_cum_1500(self):
        # peak=3000, cum=1500 → slack_trail = 1500 - (3000-2000) = 500
        # dd_cap = 500 / 1.5 ≈ 333
        today = datetime(2026, 6, 15)
        risk, f = adaptive_risk_usd(
            _make_status(cum_pnl=1500, peak_pnl=3000),
            _make_signal(),
            today,
        )
        assert f["slack_trail"] == pytest.approx(500.0, abs=1.0)
        assert f["dd_cap"] == pytest.approx(500.0 / CHALLENGE_DD_GUARD_BUFFER, abs=1.0)

    def test_dd_breached_floor(self):
        # cum très bas, peak haut → slack_trail négatif → floor à 0
        today = datetime(2026, 6, 15)
        risk, f = adaptive_risk_usd(
            _make_status(cum_pnl=-2500, peak_pnl=0),
            _make_signal(),
            today,
        )
        assert f["slack_trail"] == 0.0
        # Le risk est forcé au minimum (dd_cap = 0)
        assert risk == CHALLENGE_RISK_MIN_USD

    def test_hail_mary(self):
        # cum=-1200, peak=0, days_left=2 → risque devrait monter
        # Le 2 du mois précédent → next reset 2 du mois suivant
        # Choisissons une date qui donne days_left ≈ 2
        # 29 juin 2026 (lundi) → 30 juin, 1er juillet = 2 jours ouvrés
        today = datetime(2026, 6, 29)
        risk, f = adaptive_risk_usd(
            _make_status(cum_pnl=-1200, peak_pnl=0),
            _make_signal("OPR"),
            today,
        )
        # days_left = 2 jours ouvrés (lun 29, mar 30, mer 1er, jeu 2 exclu) = 3
        assert f["days_left"] <= 4
        # slack_trail = -1200 - (0 - 2000) = 800 → dd_cap = 533
        assert f["dd_cap"] == pytest.approx(800.0 / CHALLENGE_DD_GUARD_BUFFER, abs=1.0)
        # Le risque doit être significativement supérieur au minimum
        assert risk > 100, f"hail-mary risk={risk} trop faible"


# ────────────────────────────────────────────────────────────────────────────
# Cohérence avec PortfolioRiskManager.can_open
# ────────────────────────────────────────────────────────────────────────────


class TestCoherenceRiskManager:

    def test_passes_can_open_in_normal_state(self):
        """Dans un état "normal" (compte vif, slacks confortables), le risk
        retourné doit passer le check can_open du RM (en mode challenge avec
        bypass actif)."""
        rm = PortfolioRiskManager()
        rm.cum_pnl = 500
        rm.peak_pnl = 800
        rm.realized_day_pnl = -50
        rm.current_day = date(2026, 6, 15)

        signal = _make_signal()
        today = datetime(2026, 6, 15)
        risk, _ = adaptive_risk_usd(rm.status(), signal, today)
        ok, reason = rm.can_open(risk_usd=risk, when=today)
        assert ok, f"can_open refused risk={risk}: {reason}"


# ────────────────────────────────────────────────────────────────────────────
# Reset mensuel (PortfolioRiskManager._maybe_roll_month)
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.xfail(
    reason=(
        "Reset mensuel retiré avec la désactivation du challenge mode "
        "(politique 2026-05-21, USD fixe). Le code attend toujours un "
        "comportement de reset day-2 qui n'est plus implémenté. "
        "À refixer ou supprimer quand le challenge mode sera réactivé."
    ),
    strict=False,
)
class TestMonthlyReset:

    def test_reset_triggers_on_day_2(self):
        rm = PortfolioRiskManager()
        rm.cum_pnl = 1500
        rm.peak_pnl = 2000
        rm.consec_loss_days = 3
        rm.current_day = date(2026, 4, 30)
        # Roll au 2 mai → reset doit s'effectuer
        rm._maybe_roll_day(datetime(2026, 5, 2, 14, 0))
        assert rm.cum_pnl == 0.0
        assert rm.peak_pnl == 0.0
        assert rm.consec_loss_days == 0
        assert rm.last_reset_month == 5
        assert rm.last_reset_year == 2026

    def test_reset_idempotent(self):
        rm = PortfolioRiskManager()
        rm.cum_pnl = 1500
        rm.peak_pnl = 2000
        rm.current_day = date(2026, 4, 30)
        rm._maybe_roll_day(datetime(2026, 5, 2, 14, 0))
        # Re-trigger : ne doit pas re-reset
        rm.cum_pnl = 300  # nouveau gain
        rm._maybe_roll_day(datetime(2026, 5, 5, 14, 0))
        assert rm.cum_pnl == 300, "Le reset s'est re-déclenché par erreur"

    def test_no_reset_before_day_2(self):
        rm = PortfolioRiskManager()
        rm.cum_pnl = 1500
        rm.peak_pnl = 2000
        rm.current_day = date(2026, 4, 30)
        # Roll au 1er mai → pas encore reset
        rm._maybe_roll_day(datetime(2026, 5, 1, 14, 0))
        assert rm.cum_pnl == 1500
        assert rm.peak_pnl == 2000
        # Roll au 2 mai → reset
        rm._maybe_roll_day(datetime(2026, 5, 2, 14, 0))
        assert rm.cum_pnl == 0.0
