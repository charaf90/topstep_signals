"""
Tests d'intégration : runner + realtime + idempotence WS/polling.

Stratégie : on instancie un SessionRunner avec un client + telegram stubbés,
on remplace `self.rt` par un stub qui retourne des events à la demande, et on
appelle `_drain_realtime()` puis `_sync_broker()` pour vérifier que :

  1. Un trade event WS NE clôture PAS (déféré au REST) — les trades n'ont pas de
     customTag, l'attribution par contrat est erronée (double-compte / fantôme)
  2. _sync_broker (REST) clôture PAR TAG via orderId→customTag, sans double-compte
  3. Un position event size>0 sur PENDING → transition ACTIVE
  4. Un order event status cancel sur PENDING → transition CANCELLED
  5. End-to-end : WS (no-op close) puis polling REST → register_close une SEULE fois
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from broker.live_runner import (
    _ST_ACTIVE,
    _ST_CANCELLED,
    _ST_CLOSED,
    _ST_PENDING,
    SessionRunner,
)
from broker.projectx_realtime import RealtimeEvent

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


class StubRealtime:
    """Stub minimal pour self.rt — pas de threads, pas de réseau."""

    def __init__(self):
        self._events: list = []
        self._connected = True
        self._last_event_age_s = 0.0
        self.stop_called = 0

    def push(self, evt: RealtimeEvent):
        self._events.append(evt)

    def drain_events(self, max_events=500):
        out = self._events[:max_events]
        self._events = self._events[max_events:]
        return out

    def is_connected(self):
        return self._connected

    def health(self):
        return {
            "connected": self._connected,
            "queue_depth": len(self._events),
            "dropped_events": 0,
            "last_event_age_s": self._last_event_age_s,
            "disconnect_count": 0,
            "reconnect_attempt": 0,
        }

    def stop(self, timeout=5.0):
        self.stop_called += 1


@pytest.fixture
def runner(tmp_path):
    """SessionRunner avec client/telegram stubbés et rt remplaçable."""
    client = MagicMock()
    client.token = "fake_jwt"
    tg = MagicMock()

    # On instancie sans déclencher l'init realtime (flag config est False par
    # défaut) — puis on injecte notre stub manuellement
    runner = SessionRunner(
        client=client,
        account_id=42,
        state_file=str(tmp_path / "state.json"),
        dry_run=True,
        tickers=["NQ1"],
        strategy="opr",
        telegram=tg,
    )
    runner.tg = tg

    # État initial typique : un ordre PENDING placé
    runner.state = {
        "date": "2026-05-18",
        "placed_tags": {},
        "session_report_sent": None,
        "session_start_notified": None,
    }
    runner.rt = StubRealtime()
    return runner


def _make_pending_tag(runner, tag="OPR_NQ1_20260518_long_1"):
    """Place un ordre PENDING dans le state + register_open sur le RM."""
    info = {
        "status": _ST_PENDING,
        "order_id": 101,
        "ticker": "NQ1",
        "contract_id": "CON.F.US.MNQ.M26",
        "direction": "long",
        "entry": 17000.0,
        "n_ct": 1,
        "strategy": "OPR",
    }
    runner.state["placed_tags"][tag] = info
    runner.rm.register_open(tag, risk_usd=100.0)
    return tag, info


def _make_active_tag(runner, tag="OPR_NQ1_20260518_long_1"):
    """Place un tag ACTIVE (déjà fillé)."""
    tag, info = _make_pending_tag(runner, tag)
    info["status"] = _ST_ACTIVE
    info["fill_time"] = "2026-05-18T13:30:00Z"
    # Reflect on RM
    runner.rm.register_fill(tag, when=datetime.utcnow())
    return tag, info


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────


def _configure_close(runner, mapping, positions=None):
    """Configure le mock client pour que _sync_broker voie des clôtures.

    `mapping` : liste de (order_id, contract_id, pnl, size, customTag).
    """
    trades, orders = [], []
    for oid, cid, pnl, size, ctag in mapping:
        trades.append(
            {
                "orderId": oid,
                "contractId": cid,
                "profitAndLoss": pnl,
                "size": size,
                "voided": False,
                "creationTimestamp": "2026-05-18T14:00:00Z",
            }
        )
        orders.append({"id": oid, "customTag": ctag})
    runner.client.get_open_orders.return_value = []
    runner.client.get_positions.return_value = positions or []
    runner.client.get_trades_since.return_value = trades
    runner.client.get_orders_since.return_value = orders


def test_trade_event_with_pnl_is_deferred_to_rest(runner):
    """trade event WS NE clôture PAS : les trades n'ont pas de customTag → le lookup
    retombe sur le 1er tag du contrat (faux). Clôture déférée au sync REST."""
    tag, info = _make_active_tag(runner)

    runner.rt.push(
        RealtimeEvent(
            kind="trade",
            custom_tag=tag,
            contract_id=info["contract_id"],
            pnl=150.0,
        )
    )
    runner._drain_realtime()

    # La position reste ACTIVE — aucune attribution P&L côté WS
    assert info["status"] == _ST_ACTIVE
    assert runner.tg.notify_close.call_count == 0
    assert runner.rm.cum_pnl == 0.0
    assert tag in runner.rm.active_positions


def test_sync_broker_closes_by_tag(runner):
    """_sync_broker clôture un tag ACTIVE via son closing trade (orderId→customTag)."""
    tag, info = _make_active_tag(runner)
    cid = info["contract_id"]
    _configure_close(runner, [(777, cid, 150.0, 1, f"{tag}-SL")])

    runner._sync_broker(datetime.utcnow())

    assert info["status"] == _ST_CLOSED
    assert info["close_pnl"] == 150.0
    assert runner.rm.cum_pnl == 150.0
    assert runner.tg.notify_close.call_count == 1


def test_sync_broker_no_double_count_shared_contract(runner):
    """RÉGRESSION : 2 tags ACTIVE sur le MÊME contrat, 2 closing trades. Chaque tag
    reçoit SON P&L via customTag, pas la somme (bug historique = pnl combiné ×2)."""
    cid = "CON.F.US.MES.M26"
    a, ia = _make_active_tag(runner, tag="FIBFINE_MES1_x_short_a")
    b, ib = _make_active_tag(runner, tag="FIBFINE_MES1_x_short_b")
    ia["contract_id"] = ib["contract_id"] = cid
    _configure_close(
        runner,
        [
            (501, cid, 237.5, 1, f"{a}-TP"),
            (502, cid, 227.5, 1, f"{b}-TP"),
        ],
    )

    runner._sync_broker(datetime.utcnow())

    assert ia["status"] == _ST_CLOSED and ib["status"] == _ST_CLOSED
    assert ia["close_pnl"] == 237.5
    assert ib["close_pnl"] == 227.5
    # cum = 465, surtout PAS 930 (le double-comptage contrat-level historique)
    assert runner.rm.cum_pnl == 465.0
    assert runner.tg.notify_close.call_count == 2


def test_position_event_size_gt_0_triggers_fill(runner):
    """position size>0 sur tag PENDING → transition ACTIVE."""
    tag, info = _make_pending_tag(runner)

    runner.rt.push(
        RealtimeEvent(
            kind="position",
            contract_id=info["contract_id"],
            size=1,
        )
    )
    runner._drain_realtime()

    assert info["status"] == _ST_ACTIVE
    assert info["fill_time"] is not None
    assert runner.tg.notify_fill.call_count == 1
    assert tag in runner.rm.active_positions


def test_position_event_size_0_marks_close_seen(runner):
    """position size=0 sur tag ACTIVE → marque _close_seen, attend trade event."""
    tag, info = _make_active_tag(runner)

    runner.rt.push(
        RealtimeEvent(
            kind="position",
            contract_id=info["contract_id"],
            size=0,
        )
    )
    runner._drain_realtime()

    assert info.get("_close_seen") is True
    # Pas de transition CLOSED tant qu'on n'a pas le pnl
    assert info["status"] == _ST_ACTIVE


def test_order_event_cancel_status_triggers_cancel(runner):
    """order event avec status=3 (cancelled, confirmé smoke 2026-05-18)
    sur PENDING → CANCELLED."""
    tag, info = _make_pending_tag(runner)

    runner.rt.push(
        RealtimeEvent(
            kind="order",
            custom_tag=tag,
            contract_id=info["contract_id"],
            order_id=101,
            status=3,  # cancelled (confirmé via smoke test)
        )
    )
    runner._drain_realtime()

    assert info["status"] == _ST_CANCELLED
    # RM : risque pending libéré
    assert tag not in runner.rm.pending_orders


def test_order_event_status_active_does_not_cancel(runner):
    """status=1 (actif, non-terminal) ne doit PAS déclencher un cancel."""
    tag, info = _make_pending_tag(runner)

    runner.rt.push(
        RealtimeEvent(
            kind="order",
            custom_tag=tag,
            contract_id=info["contract_id"],
            order_id=101,
            status=1,  # actif/placed → on attend, pas de transition
        )
    )
    runner._drain_realtime()

    # Toujours PENDING — l'ordre est juste confirmé actif, pas terminé
    assert info["status"] == _ST_PENDING


def test_event_without_matching_tag_is_dropped(runner):
    """Un event sans tag matchable → drop silencieusement (pas de crash)."""
    runner.rt.push(
        RealtimeEvent(
            kind="trade",
            custom_tag="UNKNOWN_TAG",
            contract_id="UNKNOWN_CONTRACT",
            pnl=999.0,
        )
    )
    # Ne doit pas crasher
    runner._drain_realtime()
    assert runner.tg.notify_close.call_count == 0
    assert runner.rm.cum_pnl == 0.0


def test_sync_broker_market_close_via_exit_order_id(runner):
    """Clôture market : le closing trade peut ne pas porter de customTag (observé
    côté broker) → attribution via l'orderId mémorisé `_exit_order_id` au placement."""
    tag, info = _make_active_tag(runner)
    cid = info["contract_id"]
    info["_exit_order_id"] = 888  # posé par _close_position_market

    runner.client.get_open_orders.return_value = []
    runner.client.get_positions.return_value = []
    runner.client.get_trades_since.return_value = [
        {
            "orderId": 888,
            "contractId": cid,
            "profitAndLoss": -40.0,
            "size": 1,
            "voided": False,
            "creationTimestamp": "2026-05-18T14:00:00Z",
        }
    ]
    runner.client.get_orders_since.return_value = [{"id": 888, "customTag": None}]

    runner._sync_broker(datetime.utcnow())

    assert info["status"] == _ST_CLOSED
    assert info["close_pnl"] == -40.0
    assert runner.rm.cum_pnl == -40.0


def test_idempotence_ws_then_rest_sync(runner):
    """WS trade event (no-op close) puis _sync_broker REST (×2) → register_close 1×."""
    tag, info = _make_active_tag(runner)
    cid = info["contract_id"]

    # 1. WS pousse un trade event → ne clôture PAS (déféré au REST)
    runner.rt.push(RealtimeEvent(kind="trade", custom_tag=tag, contract_id=cid, pnl=200.0))
    runner._drain_realtime()
    assert info["status"] == _ST_ACTIVE
    assert runner.rm.cum_pnl == 0.0

    # 2. REST clôture, et un 2e passage est idempotent (status CLOSED → no-op)
    _configure_close(runner, [(777, cid, 200.0, 1, f"{tag}-TP")])
    runner._sync_broker(datetime.utcnow())
    runner._sync_broker(datetime.utcnow())

    assert info["status"] == _ST_CLOSED
    assert runner.rm.cum_pnl == 200.0
    assert runner.tg.notify_close.call_count == 1


def test_idempotence_rest_then_ws(runner):
    """Cas inverse : REST détecte d'abord, WS arrive ensuite → no-op."""
    tag, info = _make_active_tag(runner)

    # 1. _sync_broker REST détecte en premier
    now_utc = datetime.utcnow()
    runner._handle_close_transition(tag, info, pnl=80.0, now_utc=now_utc)
    assert info["status"] == _ST_CLOSED

    # 2. WS pousse le même event après
    runner.rt.push(
        RealtimeEvent(
            kind="trade",
            custom_tag=tag,
            contract_id=info["contract_id"],
            pnl=80.0,
        )
    )
    runner._drain_realtime()

    assert runner.rm.cum_pnl == 80.0
    assert runner.tg.notify_close.call_count == 1


def test_drain_handles_empty_queue(runner):
    """drain sur queue vide → no-op silencieux."""
    runner._drain_realtime()
    assert runner.tg.notify_close.call_count == 0
    assert runner.tg.notify_fill.call_count == 0


# ─────────────────────────────────────────────────────────────────────────────
# Helpers purs d'attribution par tag (cœur du correctif anti-double-comptage)
# ─────────────────────────────────────────────────────────────────────────────


def test_owner_tag_strips_bracket_suffixes():
    from broker.live_runner import _owner_tag

    assert _owner_tag("FIBFINE_MES1_x-TP") == "FIBFINE_MES1_x"
    assert _owner_tag("FIBFINE_MES1_x-SL") == "FIBFINE_MES1_x"
    assert _owner_tag("FIBFINE_MES1_x-EXIT") == "FIBFINE_MES1_x"
    assert _owner_tag("FIBFINE_MES1_x") == "FIBFINE_MES1_x"  # entrée, pas de suffixe
    assert _owner_tag(None) is None
    assert _owner_tag("") is None


def _add_active_rm_position(runner, tag, ticker, risk, opened_at):
    """Ajoute une position ACTIVE dans le RM (avec metadata) + opened_at fixé."""
    runner.rm.register_open(tag, risk_usd=risk, metadata={"ticker": ticker})
    runner.rm.register_fill(tag)
    runner.rm.active_positions[tag].opened_at = opened_at


def test_heal_phantom_purges_stale_without_broker_position(runner):
    """Position ACTIVE ancienne sans contrepartie broker → purgée (slack libéré)."""
    runner.state["contracts"] = {"MES1": "CON.F.US.MES.M26"}
    now = datetime(2026, 6, 9, 18, 0, 0)
    _add_active_rm_position(
        runner, "BOSFVG_MES1_old", "MES1", 150.0, datetime(2026, 6, 4, 14, 0, 0)
    )
    runner.client.get_positions.return_value = []  # broker : aucune position

    assert runner._heal_phantom_positions(now) is True
    assert "BOSFVG_MES1_old" not in runner.rm.active_positions


def test_heal_phantom_keeps_real_position(runner):
    """Position confirmée par le broker (même contrat) → conservée."""
    runner.state["contracts"] = {"MES1": "CON.F.US.MES.M26"}
    now = datetime(2026, 6, 9, 18, 0, 0)
    _add_active_rm_position(
        runner, "BOSFVG_MES1_real", "MES1", 150.0, datetime(2026, 6, 4, 14, 0, 0)
    )
    runner.client.get_positions.return_value = [{"contractId": "CON.F.US.MES.M26", "size": 1}]

    runner._heal_phantom_positions(now)
    assert "BOSFVG_MES1_real" in runner.rm.active_positions


def test_heal_phantom_keeps_recent_when_broker_empty(runner):
    """Position du JOUR sans contrepartie ET broker vide → gardée par prudence."""
    runner.state["contracts"] = {"MES1": "CON.F.US.MES.M26"}
    now = datetime(2026, 6, 9, 18, 0, 0)
    _add_active_rm_position(
        runner, "BOSFVG_MES1_today", "MES1", 150.0, datetime(2026, 6, 9, 15, 0, 0)
    )
    runner.client.get_positions.return_value = []  # broker momentanément vide

    runner._heal_phantom_positions(now)
    assert "BOSFVG_MES1_today" in runner.rm.active_positions


def test_heal_phantom_skips_when_contracts_unresolved(runner):
    """Contrats pas encore résolus → la passe ne s'exécute pas (retry)."""
    runner.state["contracts"] = {}
    _add_active_rm_position(runner, "BOSFVG_MES1_x", "MES1", 150.0, datetime(2026, 6, 4, 14, 0, 0))
    assert runner._heal_phantom_positions(datetime(2026, 6, 9, 18, 0, 0)) is False
    assert "BOSFVG_MES1_x" in runner.rm.active_positions  # rien purgé


def test_build_close_attribution_ignores_opening_and_voided():
    from broker.live_runner import _build_close_attribution

    trades = [
        {"orderId": 1, "profitAndLoss": 237.5, "size": 1, "voided": False},
        {"orderId": 2, "profitAndLoss": 227.5, "size": 1, "voided": False},
        {"orderId": 3, "profitAndLoss": None, "size": 1, "voided": False},  # opening → ignoré
        {"orderId": 4, "profitAndLoss": 99.0, "size": 1, "voided": True},  # voided → ignoré
        {"orderId": 5, "profitAndLoss": -40.0, "size": 1, "voided": False},  # market → fallback
    ]
    orders = [
        {"id": 1, "customTag": "A-TP"},
        {"id": 2, "customTag": "B-SL"},
        {"id": 5, "customTag": None},
    ]
    by_tag = _build_close_attribution(trades, orders, exit_order_ids={5: "C"})
    assert by_tag == {"A": [trades[0]], "B": [trades[1]], "C": [trades[4]]}
