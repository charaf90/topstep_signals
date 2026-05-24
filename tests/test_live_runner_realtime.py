"""
Tests d'intégration : runner + realtime + idempotence WS/polling.

Stratégie : on instancie un SessionRunner avec un client + telegram stubbés,
on remplace `self.rt` par un stub qui retourne des events à la demande, et on
appelle `_drain_realtime()` puis `_sync_broker()` pour vérifier que :

  1. Un trade event avec pnl sur tag ACTIVE → register_close 1× + Telegram 1×
  2. Un trade event dupliqué (même tag) → no-op
  3. Un position event size>0 sur PENDING → transition ACTIVE
  4. Un order event status cancel sur PENDING → transition CANCELLED
  5. End-to-end : WS détecte un close, puis polling REST détecte le même
     close → register_close appelé une SEULE fois (idempotence cross-path)
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


def test_trade_event_with_pnl_triggers_close(runner):
    """trade event avec pnl sur tag ACTIVE → register_close + notify_close 1×."""
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

    assert info["status"] == _ST_CLOSED
    assert info["close_pnl"] == 150.0
    # Telegram appelé exactement 1×
    assert runner.tg.notify_close.call_count == 1
    # RM état correct
    assert runner.rm.cum_pnl == 150.0
    assert tag not in runner.rm.active_positions


def test_duplicate_close_event_is_noop(runner):
    """Un 2e trade event sur tag déjà CLOSED ne refait pas register_close."""
    tag, info = _make_active_tag(runner)

    evt = RealtimeEvent(
        kind="trade",
        custom_tag=tag,
        contract_id=info["contract_id"],
        pnl=100.0,
    )
    runner.rt.push(evt)
    runner._drain_realtime()
    assert info["status"] == _ST_CLOSED
    assert runner.rm.cum_pnl == 100.0

    # 2e event identique
    runner.rt.push(evt)
    runner._drain_realtime()
    # Pas de double-comptabilisation
    assert runner.rm.cum_pnl == 100.0
    assert runner.tg.notify_close.call_count == 1


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


def test_fallback_lookup_by_contract_id(runner):
    """
    Un closing trade sans customTag mais avec contract_id matchant un tag ACTIVE
    doit déclencher la close transition (cf. _sync_broker L702-710).
    """
    tag, info = _make_active_tag(runner)

    runner.rt.push(
        RealtimeEvent(
            kind="trade",
            custom_tag=None,  # closing trade n'a souvent pas le customTag d'entrée
            contract_id=info["contract_id"],
            pnl=50.0,
        )
    )
    runner._drain_realtime()

    assert info["status"] == _ST_CLOSED
    assert info["close_pnl"] == 50.0


def test_idempotence_ws_then_rest_sync(runner):
    """
    End-to-end idempotence : WS détecte close, puis _sync_broker REST simule
    aussi le détecter → register_close appelé UNE seule fois.
    """
    tag, info = _make_active_tag(runner)

    # 1. WS détecte la close en premier
    runner.rt.push(
        RealtimeEvent(
            kind="trade",
            custom_tag=tag,
            contract_id=info["contract_id"],
            pnl=200.0,
        )
    )
    runner._drain_realtime()
    assert info["status"] == _ST_CLOSED
    assert runner.rm.cum_pnl == 200.0

    # 2. _sync_broker arrive 30s plus tard et "redétecte" la même close
    #    On simule en appelant directement le helper close (status est CLOSED → no-op)
    now_utc = datetime.utcnow()
    runner._handle_close_transition(tag, info, pnl=200.0, now_utc=now_utc)

    # Pas de double-comptabilisation
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
