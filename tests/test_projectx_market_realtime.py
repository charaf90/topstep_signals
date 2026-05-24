"""
Tests unit pour ProjectXMarketRealtimeClient (Phase C).

Stratégie : monkey-patch `signalrcore.hub_connection_builder.HubConnectionBuilder`
avec une FakeBuilder qui retourne une FakeHubConnection contrôlable. Permet de
fire des events synthétiques au format Market Hub réel (confirmé via smoke
2026-05-18) et de vérifier le parsing + push queue + drop-oldest.

Tous les tests doivent tourner offline en < 100 ms chacun.
"""

from __future__ import annotations

import pytest

from broker.projectx_market_realtime import (
    ProjectXMarketRealtimeClient,
    _parse_ts,
)

# ─────────────────────────────────────────────────────────────────────────────
# Fake SignalR (clone du pattern test_projectx_realtime.py)
# ─────────────────────────────────────────────────────────────────────────────


class FakeHubConnection:
    def __init__(self):
        self.handlers = {}
        self._on_open_cb = None
        self._on_close_cb = None
        self._on_error_cb = None
        self._on_reconnect_cb = None
        self.started = False
        self.sent = []
        self.stop_called = 0

    def on(self, name, cb):
        self.handlers[name] = cb

    def on_open(self, cb):
        self._on_open_cb = cb

    def on_close(self, cb):
        self._on_close_cb = cb

    def on_error(self, cb):
        self._on_error_cb = cb

    def on_reconnect(self, cb):
        self._on_reconnect_cb = cb

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

    def fire(self, name, args):
        """Fire un event au format Market Hub : args = [cid_str, payload]."""
        cb = self.handlers.get(name)
        if cb is not None:
            cb(args)


class FakeBuilder:
    instances = []

    def __init__(self):
        self._connection = FakeHubConnection()
        FakeBuilder.instances.append(self._connection)

    def with_url(self, *_, **__):
        return self

    def with_automatic_reconnect(self, *_, **__):
        return self

    def build(self):
        return self._connection

    @classmethod
    def reset(cls):
        cls.instances.clear()


@pytest.fixture
def patched_signalr(monkeypatch):
    FakeBuilder.reset()
    monkeypatch.setattr(
        "signalrcore.hub_connection_builder.HubConnectionBuilder",
        FakeBuilder,
    )
    yield FakeBuilder
    FakeBuilder.reset()


@pytest.fixture
def client(patched_signalr):
    rt = ProjectXMarketRealtimeClient(
        contract_ids=["CON.F.US.MNQ.M26", "CON.F.US.MYM.M26"],
        token_provider=lambda: "fake_jwt",
        hub_url="https://fake/hub",
        queue_maxsize=8,
        reconnect_delays=(0,),
        max_silence_s=0.5,
        force_reauth_s=1e9,
        market_open_check=lambda: True,
    )
    yield rt
    rt.stop(timeout=2.0)


# ─────────────────────────────────────────────────────────────────────────────
# Tests subscribe
# ─────────────────────────────────────────────────────────────────────────────


def test_subscribe_on_open_sends_per_contract(client, patched_signalr):
    """À l'open, on doit envoyer Quotes+Trades pour CHAQUE contract_id."""
    client.start()
    hub = patched_signalr.instances[-1]
    methods = [(m, a) for m, a in hub.sent]
    # 2 contracts × 2 méthodes (Quotes + Trades) = 4 sends
    assert len(methods) == 4
    expected = {
        ("SubscribeContractQuotes", ("CON.F.US.MNQ.M26",)),
        ("SubscribeContractTrades", ("CON.F.US.MNQ.M26",)),
        ("SubscribeContractQuotes", ("CON.F.US.MYM.M26",)),
        ("SubscribeContractTrades", ("CON.F.US.MYM.M26",)),
    }
    assert {(m, tuple(a)) for m, a in methods} == expected


def test_subscribe_skips_depth(client, patched_signalr):
    """On ne souscrit PAS au market depth (Phase C choix : non utilisé)."""
    client.start()
    hub = patched_signalr.instances[-1]
    methods = [m for m, _ in hub.sent]
    assert "SubscribeContractMarketDepth" not in methods


def test_subscribe_can_disable_quotes(patched_signalr):
    rt = ProjectXMarketRealtimeClient(
        contract_ids=["X"],
        token_provider=lambda: "t",
        hub_url="https://fake/hub",
        subscribe_quotes=False,
        subscribe_trades=True,
    )
    try:
        rt.start()
        hub = patched_signalr.instances[-1]
        methods = [m for m, _ in hub.sent]
        assert "SubscribeContractTrades" in methods
        assert "SubscribeContractQuotes" not in methods
    finally:
        rt.stop(timeout=1.0)


# ─────────────────────────────────────────────────────────────────────────────
# Tests parsing GatewayQuote
# ─────────────────────────────────────────────────────────────────────────────


def test_quote_full_payload_parsed(client, patched_signalr):
    """Quote complet : tous les champs mappés correctement."""
    client.start()
    hub = patched_signalr.instances[-1]
    hub.fire(
        "GatewayQuote",
        [
            "CON.F.US.MNQ.M26",
            {
                "symbol": "F.US.MNQ",
                "lastPrice": 29118.5,
                "bestBid": 29117.75,
                "bestAsk": 29118.5,
                "volume": 543600,
                "lastUpdated": "2026-05-18T07:06:30.66+00:00",
                "timestamp": "2026-05-18T07:06:30.57+00:00",
                "contract": "CON.F.US.MNQ.M26",
            },
        ],
    )
    events = client.drain_events()
    assert len(events) == 1
    e = events[0]
    assert e.kind == "quote"
    assert e.contract_id == "CON.F.US.MNQ.M26"
    assert e.best_bid == 29117.75
    assert e.best_ask == 29118.5
    assert e.last_price == 29118.5
    assert e.ts_exchange is not None
    assert e.ts_exchange.year == 2026


def test_quote_partial_payload_parsed(client, patched_signalr):
    """Quote partiel (que bid/ask) : last_price=None, pas de crash."""
    client.start()
    hub = patched_signalr.instances[-1]
    hub.fire(
        "GatewayQuote",
        [
            "CON.F.US.MNQ.M26",
            {
                "symbol": "F.US.MNQ",
                "bestBid": 29117.75,
                "lastUpdated": "2026-05-18T07:06:31+00:00",
                "timestamp": "2026-05-18T07:06:30+00:00",
                "contract": "CON.F.US.MNQ.M26",
            },
        ],
    )
    events = client.drain_events()
    assert len(events) == 1
    assert events[0].best_bid == 29117.75
    assert events[0].best_ask is None
    assert events[0].last_price is None


# ─────────────────────────────────────────────────────────────────────────────
# Tests parsing GatewayTrade (LISTE batched)
# ─────────────────────────────────────────────────────────────────────────────


def test_trade_batch_explodes_into_n_events(client, patched_signalr):
    """GatewayTrade avec 3 trades dans la liste → 3 MarketEvents."""
    client.start()
    hub = patched_signalr.instances[-1]
    hub.fire(
        "GatewayTrade",
        [
            "CON.F.US.MNQ.M26",
            [
                {
                    "symbolId": "F.US.MNQ",
                    "price": 29118.5,
                    "volume": 1,
                    "timestamp": "2026-05-18T07:06:30.876+00:00",
                    "type": 0,
                    "contractId": "CON.F.US.MNQ.M26",
                },
                {
                    "symbolId": "F.US.MNQ",
                    "price": 29118.75,
                    "volume": 2,
                    "timestamp": "2026-05-18T07:06:31.0+00:00",
                    "type": 1,
                    "contractId": "CON.F.US.MNQ.M26",
                },
                {
                    "symbolId": "F.US.MNQ",
                    "price": 29119.0,
                    "volume": 1,
                    "timestamp": "2026-05-18T07:06:31.1+00:00",
                    "type": 0,
                    "contractId": "CON.F.US.MNQ.M26",
                },
            ],
        ],
    )
    events = client.drain_events()
    assert len(events) == 3
    assert all(e.kind == "trade" for e in events)
    assert [e.price for e in events] == [29118.5, 29118.75, 29119.0]
    assert [e.volume for e in events] == [1, 2, 1]
    assert [e.aggressor for e in events] == [0, 1, 0]


def test_trade_batch_filters_null_entries(client, patched_signalr):
    """Si la liste contient des null/dict invalides, ils sont skip."""
    client.start()
    hub = patched_signalr.instances[-1]
    hub.fire(
        "GatewayTrade",
        [
            "CON.F.US.MNQ.M26",
            [
                {
                    "price": 100.0,
                    "volume": 1,
                    "timestamp": "2026-05-18T07:06:30+00:00",
                    "contractId": "CON.F.US.MNQ.M26",
                },
                None,
                {
                    "price": 101.0,
                    "volume": 2,
                    "timestamp": "2026-05-18T07:06:31+00:00",
                    "contractId": "CON.F.US.MNQ.M26",
                },
            ],
        ],
    )
    events = client.drain_events()
    assert len(events) == 2


def test_trade_missing_price_or_volume_skipped(client, patched_signalr):
    """Trade sans price ou volume → skip silencieux."""
    client.start()
    hub = patched_signalr.instances[-1]
    hub.fire(
        "GatewayTrade",
        [
            "CON.F.US.MNQ.M26",
            [
                {"price": 100.0, "timestamp": "2026-05-18T07:06:30+00:00"},  # pas de volume
                {"volume": 1, "timestamp": "2026-05-18T07:06:31+00:00"},  # pas de price
                {
                    "price": 102.0,
                    "volume": 1,
                    "timestamp": "2026-05-18T07:06:32+00:00",
                    "contractId": "X",
                },
            ],
        ],
    )
    events = client.drain_events()
    assert len(events) == 1
    assert events[0].price == 102.0


def test_trade_contract_id_from_payload(client, patched_signalr):
    """Le contract_id du payload (champ contractId) prime sur le wrapper args[0]."""
    client.start()
    hub = patched_signalr.instances[-1]
    hub.fire(
        "GatewayTrade",
        [
            "WRAPPER_CID",
            [
                {
                    "price": 1.0,
                    "volume": 1,
                    "timestamp": "2026-05-18T07:00:00+00:00",
                    "contractId": "PAYLOAD_CID",
                }
            ],
        ],
    )
    events = client.drain_events()
    assert events[0].contract_id == "PAYLOAD_CID"


# ─────────────────────────────────────────────────────────────────────────────
# Tests queue back-pressure
# ─────────────────────────────────────────────────────────────────────────────


def test_queue_drops_oldest_when_full(client, patched_signalr):
    """Queue maxsize=8 : si on push 12, on garde les 8 plus récents."""
    client.start()
    hub = patched_signalr.instances[-1]
    for i in range(12):
        hub.fire(
            "GatewayTrade",
            [
                "X",
                [
                    {
                        "price": float(i),
                        "volume": 1,
                        "timestamp": "2026-05-18T07:00:00+00:00",
                        "contractId": "X",
                    }
                ],
            ],
        )
    events = client.drain_events(max_events=20)
    assert len(events) == 8
    # On a perdu les 4 plus anciens (prices 0,1,2,3)
    assert events[0].price == 4.0
    assert events[-1].price == 11.0
    assert client.health()["dropped_events"] == 4


# ─────────────────────────────────────────────────────────────────────────────
# Tests _unpack (différentes formes de args)
# ─────────────────────────────────────────────────────────────────────────────


def test_unpack_list_str_payload():
    cid, p = ProjectXMarketRealtimeClient._unpack(["X", {"a": 1}])
    assert cid == "X" and p == {"a": 1}


def test_unpack_empty_returns_none():
    cid, p = ProjectXMarketRealtimeClient._unpack(None)
    assert cid is None and p is None
    cid, p = ProjectXMarketRealtimeClient._unpack([])
    assert cid is None and p is None


def test_unpack_defensive_dict():
    """Si signalrcore déballe et passe directement un dict avec contractId."""
    cid, p = ProjectXMarketRealtimeClient._unpack({"contractId": "Y", "price": 5})
    assert cid == "Y"
    assert p == {"contractId": "Y", "price": 5}


# ─────────────────────────────────────────────────────────────────────────────
# Tests helpers
# ─────────────────────────────────────────────────────────────────────────────


def test_parse_ts_iso_with_tz():
    ts = _parse_ts("2026-05-18T07:06:30.876+00:00")
    assert ts is not None
    assert ts.year == 2026 and ts.month == 5 and ts.second == 30


def test_parse_ts_filters_sentinel():
    """ProjectX utilise '0001-01-01T00:00:00' comme sentinel N/A."""
    assert _parse_ts("0001-01-01T00:00:00+00:00") is None


def test_parse_ts_none_and_empty():
    assert _parse_ts(None) is None
    assert _parse_ts("") is None


# ─────────────────────────────────────────────────────────────────────────────
# Lifecycle
# ─────────────────────────────────────────────────────────────────────────────


def test_stop_idempotent(client, patched_signalr):
    """Appeler stop() 2× ne doit pas crasher."""
    client.start()
    client.stop(timeout=1.0)
    client.stop(timeout=1.0)  # ne doit pas raise
    hub = patched_signalr.instances[-1]
    assert hub.stop_called >= 1


def test_contract_ids_required():
    with pytest.raises(ValueError):
        ProjectXMarketRealtimeClient(
            contract_ids=[],
            token_provider=lambda: "t",
            hub_url="https://fake",
        )
