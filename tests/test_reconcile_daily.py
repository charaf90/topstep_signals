"""Tests pour scripts/reconcile_daily.py — mocks de ProjectXClient.

Couvre :
- état conforme (no mismatch)
- positions discordantes (broker plus / state plus)
- ordres orphelins (broker / state)
- P&L hors tolérance
- erreurs API (capturées sans crash)
"""

from __future__ import annotations

from datetime import datetime

import pytest

from scripts.reconcile_daily import (
    PnLDiff,
    derive_open_orders_from_state,
    derive_open_positions_from_state,
    reconcile,
    sum_state_pnl_for_date,
)

# ──────────────────────────────────────────────────────────────────────────────
# Mock client minimal
# ──────────────────────────────────────────────────────────────────────────────


class MockClient:
    def __init__(
        self,
        positions: list[dict] | None = None,
        open_orders: list[dict] | None = None,
        trades: list[dict] | None = None,
    ):
        self._positions = positions or []
        self._open_orders = open_orders or []
        self._trades = trades or []

    def get_positions(self, account_id: int) -> list[dict]:
        return self._positions

    def get_open_orders(self, account_id: int) -> list[dict]:
        return self._open_orders

    def get_trades_since(self, account_id: int, start_dt: datetime) -> list[dict]:
        return self._trades


class FailingClient:
    def get_positions(self, account_id: int) -> list[dict]:
        raise RuntimeError("API down")

    def get_open_orders(self, account_id: int) -> list[dict]:
        raise RuntimeError("API down")

    def get_trades_since(self, account_id: int, start_dt: datetime) -> list[dict]:
        raise RuntimeError("API down")


# ──────────────────────────────────────────────────────────────────────────────
# Helpers de state pour tests
# ──────────────────────────────────────────────────────────────────────────────


def _state_one_position(
    cid: str = "CON.F.US.MNQ.M26", direction: str = "long", n_ct: int = 2
) -> dict:
    """State avec une position ouverte FILLED non close."""
    return {
        "date": "2026-05-22",
        "account_id": 12345,
        "placed_tags": {
            "OPR_NQ1_20260522_long": {
                "order_id": 100,
                "strategy": "OPR",
                "ticker": "NQ1",
                "contract_id": cid,
                "direction": direction,
                "n_ct": n_ct,
                "status": "FILLED",
                "placed_at": "2026-05-22T14:00:00Z",
                "fill_time": "2026-05-22T14:00:30Z",
                "close_pnl": None,
            },
        },
    }


def _state_one_open_order(order_id: int = 200) -> dict:
    return {
        "date": "2026-05-22",
        "account_id": 12345,
        "placed_tags": {
            "OPR_NQ1_20260522_long": {
                "order_id": order_id,
                "strategy": "OPR",
                "ticker": "NQ1",
                "contract_id": "CON.F.US.MNQ.M26",
                "direction": "long",
                "n_ct": 1,
                "status": "PLACED",
                "placed_at": "2026-05-22T14:00:00Z",
                "fill_time": None,
                "close_pnl": None,
            },
        },
    }


def _state_with_closed_pnl(date: str, pnl: float) -> dict:
    return {
        "date": date,
        "account_id": 12345,
        "placed_tags": {
            "OPR_NQ1_long": {
                "status": "FILLED",
                "contract_id": "CON.F.US.MNQ.M26",
                "direction": "long",
                "n_ct": 1,
                "placed_at": f"{date}T14:00:00Z",
                "fill_time": f"{date}T14:00:30Z",
                "close_pnl": pnl,
            },
        },
    }


# ──────────────────────────────────────────────────────────────────────────────
# Helpers de state — tests directs
# ──────────────────────────────────────────────────────────────────────────────


class TestStateHelpers:

    def test_derive_open_positions_long(self):
        state = _state_one_position(direction="long", n_ct=2)
        pos = derive_open_positions_from_state(state)
        assert "CON.F.US.MNQ.M26" in pos
        assert pos["CON.F.US.MNQ.M26"]["signed_size"] == 2

    def test_derive_open_positions_short(self):
        state = _state_one_position(direction="short", n_ct=3)
        pos = derive_open_positions_from_state(state)
        assert pos["CON.F.US.MNQ.M26"]["signed_size"] == -3

    def test_derive_open_positions_closed_excluded(self):
        state = _state_one_position()
        # On ajoute un close_pnl → la position est close
        state["placed_tags"]["OPR_NQ1_20260522_long"]["close_pnl"] = 100.0
        pos = derive_open_positions_from_state(state)
        assert pos == {}

    def test_derive_open_positions_not_filled_excluded(self):
        state = _state_one_position()
        state["placed_tags"]["OPR_NQ1_20260522_long"]["status"] = "CANCELLED"
        pos = derive_open_positions_from_state(state)
        assert pos == {}

    def test_derive_open_orders(self):
        state = _state_one_open_order(order_id=999)
        orders = derive_open_orders_from_state(state)
        assert orders == {999}

    def test_derive_open_orders_filled_excluded(self):
        state = _state_one_open_order(order_id=999)
        state["placed_tags"]["OPR_NQ1_20260522_long"]["status"] = "FILLED"
        orders = derive_open_orders_from_state(state)
        assert orders == set()

    def test_sum_state_pnl_for_date(self):
        state = _state_with_closed_pnl("2026-05-22", pnl=125.5)
        total = sum_state_pnl_for_date(state, "2026-05-22")
        assert total == 125.5

    def test_sum_state_pnl_other_date_excluded(self):
        state = _state_with_closed_pnl("2026-05-22", pnl=125.5)
        total = sum_state_pnl_for_date(state, "2026-05-23")
        assert total == 0.0


# ──────────────────────────────────────────────────────────────────────────────
# PnLDiff
# ──────────────────────────────────────────────────────────────────────────────


class TestPnLDiff:

    def test_match_within_tolerance(self):
        d = PnLDiff(broker_pnl=100.50, state_pnl=100.30, tolerance_usd=1.0)
        assert d.delta == pytest.approx(0.2, abs=0.01)
        assert not d.is_mismatch

    def test_mismatch_above_tolerance(self):
        d = PnLDiff(broker_pnl=100.0, state_pnl=95.0, tolerance_usd=1.0)
        assert d.delta == 5.0
        assert d.is_mismatch

    def test_exact_match(self):
        d = PnLDiff(broker_pnl=100.0, state_pnl=100.0, tolerance_usd=1.0)
        assert d.delta == 0.0
        assert not d.is_mismatch


# ──────────────────────────────────────────────────────────────────────────────
# Reconcile — scénarios complets
# ──────────────────────────────────────────────────────────────────────────────


class TestReconcile:

    def test_etat_conforme(self):
        """Broker et state racontent la même histoire → no mismatch."""
        state = _state_one_position()
        client = MockClient(
            positions=[
                {"contractId": "CON.F.US.MNQ.M26", "type": 0, "size": 2},
            ],
            open_orders=[],
            trades=[],
        )
        report = reconcile(state, client, "2026-05-22")
        assert not report.has_mismatch
        assert len(report.positions) == 1
        assert all(not p.is_mismatch for p in report.positions)
        assert report.pnl is not None and not report.pnl.is_mismatch

    def test_position_broker_plus_que_state(self):
        """Broker = 3 contrats, state = 2 → mismatch détecté."""
        state = _state_one_position(n_ct=2)
        client = MockClient(
            positions=[
                {"contractId": "CON.F.US.MNQ.M26", "type": 0, "size": 3},
            ],
        )
        report = reconcile(state, client, "2026-05-22")
        assert report.has_mismatch
        pos = report.positions[0]
        assert pos.is_mismatch
        assert pos.broker_size == 3
        assert pos.state_size == 2

    def test_position_side_inverse(self):
        """Broker = short, state = long → mismatch."""
        state = _state_one_position(direction="long", n_ct=2)
        client = MockClient(
            positions=[
                {"contractId": "CON.F.US.MNQ.M26", "type": 1, "size": 2},
            ],
        )
        report = reconcile(state, client, "2026-05-22")
        assert report.has_mismatch
        pos = report.positions[0]
        assert pos.broker_side == "short"
        assert pos.state_side == "long"

    def test_ordre_orphelin_broker(self):
        """Ordre ouvert côté broker mais inconnu en local → mismatch."""
        state = _state_one_position()  # pas d'ordre ouvert
        client = MockClient(
            positions=[{"contractId": "CON.F.US.MNQ.M26", "type": 0, "size": 2}],
            open_orders=[{"id": 999, "contractId": "CON.F.US.MNQ.M26"}],
        )
        report = reconcile(state, client, "2026-05-22")
        assert report.has_mismatch
        orphan = next(o for o in report.orders if o.order_id == 999)
        assert orphan.in_broker
        assert not orphan.in_state
        assert "orphelin broker" in orphan.detail

    def test_ordre_orphelin_state(self):
        """Ordre marqué ouvert en local mais broker l'a fermé/annulé → mismatch."""
        state = _state_one_open_order(order_id=555)
        client = MockClient(
            positions=[],
            open_orders=[],  # broker ne le voit plus
        )
        report = reconcile(state, client, "2026-05-22")
        assert report.has_mismatch
        orphan = next(o for o in report.orders if o.order_id == 555)
        assert not orphan.in_broker
        assert orphan.in_state
        assert "orphelin state" in orphan.detail

    def test_pnl_hors_tolerance(self):
        """P&L broker $200, state $50 → mismatch (Δ = 150, tol = 1)."""
        state = _state_with_closed_pnl("2026-05-22", pnl=50.0)
        client = MockClient(
            positions=[],
            trades=[
                {"profitAndLoss": 200.0, "creationTimestamp": "2026-05-22T14:30:00Z"},
            ],
        )
        report = reconcile(state, client, "2026-05-22", tolerance_usd=1.0)
        assert report.has_mismatch
        assert report.pnl is not None
        assert report.pnl.broker_pnl == 200.0
        assert report.pnl.state_pnl == 50.0
        assert report.pnl.delta == 150.0

    def test_pnl_dans_tolerance(self):
        """P&L broker $100.5, state $100, tol $1 → OK."""
        state = _state_with_closed_pnl("2026-05-22", pnl=100.0)
        client = MockClient(
            trades=[
                {"profitAndLoss": 100.5, "creationTimestamp": "2026-05-22T14:30:00Z"},
            ],
        )
        report = reconcile(state, client, "2026-05-22", tolerance_usd=1.0)
        assert report.pnl is not None and not report.pnl.is_mismatch

    def test_api_failure_does_not_crash(self):
        """Une API qui plante → erreurs capturées, report.has_mismatch=True."""
        state = _state_one_position()
        client = FailingClient()
        report = reconcile(state, client, "2026-05-22")
        assert report.has_mismatch
        assert len(report.errors) >= 1
        assert all("API down" in e for e in report.errors)

    def test_aucune_position_ouverte_pas_de_diff(self):
        """State et broker tous les deux vides → no mismatch."""
        state = {"date": "2026-05-22", "account_id": 12345, "placed_tags": {}}
        client = MockClient()
        report = reconcile(state, client, "2026-05-22")
        assert not report.has_mismatch

    def test_trade_aller_ignore(self):
        """Trade ouverture (profitAndLoss=None) doit être ignoré."""
        state = {"date": "2026-05-22", "account_id": 12345, "placed_tags": {}}
        client = MockClient(
            trades=[
                {"profitAndLoss": None, "creationTimestamp": "2026-05-22T14:00:00Z"},
                {"profitAndLoss": 50.0, "creationTimestamp": "2026-05-22T14:30:00Z"},
            ],
        )
        report = reconcile(state, client, "2026-05-22")
        assert report.pnl is not None
        assert report.pnl.broker_pnl == 50.0  # le None est ignoré
