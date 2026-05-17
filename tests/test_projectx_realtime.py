"""
Tests unit pour ProjectXRealtimeClient.

Stratégie : monkey-patch `signalrcore.hub_connection_builder.HubConnectionBuilder`
avec un FakeHubConnectionBuilder qui retourne un FakeHubConnection contrôlable
depuis le test. On peut alors simuler `_on_open`, fire des events, et tester le
push dans la queue + drop-oldest + reconnect.

Tous les tests doivent tourner offline en < 100 ms chacun.
"""
from __future__ import annotations

import time
import threading
import pytest

from broker.projectx_realtime import ProjectXRealtimeClient, RealtimeEvent


# ─────────────────────────────────────────────────────────────────────────────
# Fake SignalR — patch via monkeypatch fixture
# ─────────────────────────────────────────────────────────────────────────────

class FakeHubConnection:
    """Imite l'API HubConnection de signalrcore 0.9.5."""

    def __init__(self):
        self.handlers = {}
        self._on_open_cb = None
        self._on_close_cb = None
        self._on_error_cb = None
        self._on_reconnect_cb = None
        self.started = False
        self.sent = []   # list of (method, args) pour vérifier les subscribes
        self.stop_called = 0

    # ── API consommée par notre client ──
    def on(self, event_name, callback):
        self.handlers[event_name] = callback

    def on_open(self, cb):       self._on_open_cb = cb
    def on_close(self, cb):      self._on_close_cb = cb
    def on_error(self, cb):      self._on_error_cb = cb
    def on_reconnect(self, cb):  self._on_reconnect_cb = cb

    def send(self, method, args):
        self.sent.append((method, list(args)))

    def start(self):
        self.started = True
        if self._on_open_cb is not None:
            self._on_open_cb()

    def stop(self):
        self.started = False
        self.stop_called += 1
        if self._on_close_cb is not None:
            self._on_close_cb()

    # ── Helpers pour les tests ──
    def fire(self, event_name, payload):
        """Simule un event broker incoming."""
        cb = self.handlers.get(event_name)
        if cb is not None:
            cb([payload])


class FakeBuilder:
    """Imite la chaîne fluent HubConnectionBuilder()...build()."""

    instances = []   # accumulateur des HubConnections créés (pour rebuild test)

    def __init__(self):
        self._connection = FakeHubConnection()
        FakeBuilder.instances.append(self._connection)

    def with_url(self, *_, **__):              return self
    def with_automatic_reconnect(self, *_, **__): return self
    def build(self):                            return self._connection

    @classmethod
    def reset(cls):
        cls.instances.clear()


@pytest.fixture
def patched_signalr(monkeypatch):
    """Remplace HubConnectionBuilder par notre fake."""
    FakeBuilder.reset()
    monkeypatch.setattr(
        "signalrcore.hub_connection_builder.HubConnectionBuilder",
        FakeBuilder,
    )
    yield FakeBuilder
    FakeBuilder.reset()


@pytest.fixture
def client(patched_signalr):
    """Client de test avec stop() automatique en teardown."""
    token_calls = []

    def provider():
        token_calls.append(time.monotonic())
        return f"fake_jwt_{len(token_calls)}"

    rt = ProjectXRealtimeClient(
        account_id=99999,
        token_provider=provider,
        hub_url="https://fake/hub",
        queue_maxsize=4,
        reconnect_delays=(0,),  # pas d'attente en test
        max_silence_s=0.5,      # 500 ms — pour tester zombie rapidement
        force_reauth_s=1e9,     # jamais en test
        market_open_check=lambda: True,
    )
    rt._token_calls = token_calls
    yield rt
    rt.stop(timeout=2.0)


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_subscribe_on_open_sends_4_methods(client, patched_signalr):
    """À l'open, le client doit envoyer les 4 subscriptions User Hub."""
    client.start()
    # Le start() de FakeHubConnection appelle _on_open_cb → notre _on_open
    hub = patched_signalr.instances[-1]
    methods = [m for m, _ in hub.sent]
    assert "SubscribeAccounts" in methods
    assert "SubscribeOrders" in methods
    assert "SubscribePositions" in methods
    assert "SubscribeTrades" in methods
    # Vérifie que l'account_id est bien passé
    for m, args in hub.sent:
        if m in ("SubscribeOrders", "SubscribePositions", "SubscribeTrades"):
            assert args == [99999], f"{m} appelé avec args={args}"


def test_order_event_pushes_to_queue(client, patched_signalr):
    """Un event GatewayUserOrder doit produire un RealtimeEvent dans la queue.

    Format ProjectX réel (confirmé via smoke 2026-05-18) :
    {"action": 1, "data": {<vrais champs>}}
    """
    client.start()
    hub = patched_signalr.instances[-1]
    hub.fire("GatewayUserOrder", {
        "action": 1,
        "data": {
            "id": 42, "contractId": "CON.F.US.MNQ.M26",
            "customTag": "OPR_NQ1_20260518_long_1",
            "status": 1, "type": 1, "side": 0, "size": 1,
        }
    })
    events = client.drain_events()
    assert len(events) == 1
    assert events[0].kind == "order"
    assert events[0].order_id == 42
    assert events[0].custom_tag == "OPR_NQ1_20260518_long_1"
    assert events[0].contract_id == "CON.F.US.MNQ.M26"
    assert events[0].status == 1


def test_trade_event_extracts_pnl(client, patched_signalr):
    """GatewayUserTrade avec profitAndLoss doit remplir RealtimeEvent.pnl."""
    client.start()
    hub = patched_signalr.instances[-1]
    hub.fire("GatewayUserTrade", {
        "action": 1,
        "data": {
            "orderId": 7, "contractId": "CON.F.US.MES.M26",
            "customTag": "TEST", "profitAndLoss": 123.45,
        }
    })
    events = client.drain_events()
    assert len(events) == 1
    assert events[0].kind == "trade"
    assert events[0].pnl == pytest.approx(123.45)


def test_position_event_extracts_size(client, patched_signalr):
    """GatewayUserPosition doit remplir RealtimeEvent.size (incluant 0 pour flat)."""
    client.start()
    hub = patched_signalr.instances[-1]
    hub.fire("GatewayUserPosition", {
        "action": 1,
        "data": {"contractId": "ABC", "size": 0}
    })
    events = client.drain_events()
    assert events[0].size == 0


def test_payload_envelope_unwrapping(client, patched_signalr):
    """L'enveloppe {action, data} doit être déballée par _extract_payload."""
    client.start()
    hub = patched_signalr.instances[-1]
    # Avec enveloppe
    hub.fire("GatewayUserOrder", {"action": 1, "data": {"id": 100}})
    # Sans enveloppe (compat ancien format / autre source)
    hub.fire("GatewayUserOrder", {"id": 200})
    events = client.drain_events()
    assert len(events) == 2
    assert events[0].order_id == 100   # wrappé
    assert events[1].order_id == 200   # raw


def test_queue_overflow_drops_oldest(client, patched_signalr):
    """Queue saturée (maxsize=4) → drop oldest, garde les 4 plus récents."""
    client.start()
    hub = patched_signalr.instances[-1]
    for i in range(10):
        hub.fire("GatewayUserOrder",
                  {"action": 1, "data": {"id": i, "contractId": "X"}})
    events = client.drain_events()
    assert len(events) == 4
    # Les 4 derniers : 6,7,8,9
    ids = [e.order_id for e in events]
    assert ids == [6, 7, 8, 9]
    assert client._dropped_events == 6


def test_handler_exception_does_not_crash(client, patched_signalr):
    """Un handler qui raise ne doit pas tuer le thread WS (try/except interne)."""
    client.start()
    hub = patched_signalr.instances[-1]
    # Payload corrompu (pas un dict) — l'extract _extract_payload doit retourner {}
    # et le handler doit gérer sans crasher
    hub.fire("GatewayUserOrder", None)
    hub.fire("GatewayUserOrder", "not-a-dict")
    # Le client est toujours fonctionnel
    hub.fire("GatewayUserOrder", {"action": 1, "data": {"id": 99}})
    events = client.drain_events()
    # Au moins le dernier event valide doit être dans la queue
    assert any(e.order_id == 99 for e in events)


def test_token_provider_called_on_start(client, patched_signalr):
    """token_provider doit être appelé au moins une fois à start()."""
    client.start()
    assert len(client._token_calls) >= 1


def test_stop_idempotent(client, patched_signalr):
    """stop() peut être appelé plusieurs fois sans crasher."""
    client.start()
    client.stop()
    client.stop()  # 2e stop : no-op
    assert not client.is_connected()


def test_health_snapshot(client, patched_signalr):
    """health() retourne un dict avec les clés attendues."""
    client.start()
    h = client.health()
    for key in ["connected", "queue_depth", "dropped_events",
                "last_event_age_s", "disconnect_count", "reconnect_attempt"]:
        assert key in h
    assert h["connected"] is True


def test_subscribe_resent_on_reconnect(client, patched_signalr):
    """on_reconnect est aussi câblé sur _on_open → re-subscribe automatique."""
    client.start()
    hub = patched_signalr.instances[-1]
    # Reset le compteur sent et simule un reconnect
    hub.sent.clear()
    if hub._on_reconnect_cb:
        hub._on_reconnect_cb()
    methods = [m for m, _ in hub.sent]
    assert "SubscribeAccounts" in methods
    assert "SubscribeOrders" in methods
