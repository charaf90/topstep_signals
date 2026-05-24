"""
Tests unit pour M1Buffer (Phase C).

Couvre :
- Agrégation OHLCV correcte sur une minute
- Rotation de bar au passage de minute
- Drop des trades out-of-order
- inject_bars (gap-fill REST) avec dédup vs WS
- flush_stale_bars (clôture forcée d'un bar dont la minute est passée)
- get_bars_since / get_recent_bars
- max_minutes deque bounded
- Quotes ignorées (bars = trades only)

Tous offline, < 10 ms par test.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from broker.m1_buffer import M1Bar, M1Buffer, _floor_minute
from broker.projectx_market_realtime import MarketEvent

CID = "CON.F.US.MNQ.M26"


def _trade(price: float, vol: int, ts: datetime, cid: str = CID) -> MarketEvent:
    return MarketEvent(
        kind="trade",
        contract_id=cid,
        ts_exchange=ts,
        price=price,
        volume=vol,
    )


def _quote(ts: datetime, cid: str = CID) -> MarketEvent:
    return MarketEvent(
        kind="quote",
        contract_id=cid,
        ts_exchange=ts,
        best_bid=100.0,
        best_ask=100.5,
    )


@pytest.fixture
def base_ts():
    return datetime(2026, 5, 18, 14, 30, 0, tzinfo=UTC)


@pytest.fixture
def buf():
    return M1Buffer(max_minutes=10)


# ─────────────────────────────────────────────────────────────────────────────
# Aggrégation
# ─────────────────────────────────────────────────────────────────────────────


def test_aggregation_ohlcv_within_one_minute(buf, base_ts):
    """3 trades dans la même minute → 1 bar avec OHLCV cohérent."""
    buf.consume(_trade(100.0, 1, base_ts.replace(second=5)))
    buf.consume(_trade(102.5, 3, base_ts.replace(second=20)))
    buf.consume(_trade(99.0, 2, base_ts.replace(second=45)))

    cur = buf.get_current_forming_bar(CID)
    assert cur is not None
    assert cur.open == 100.0
    assert cur.high == 102.5
    assert cur.low == 99.0
    assert cur.close == 99.0
    assert cur.volume == 6
    assert cur.n_ticks == 3
    assert not cur.closed
    assert buf.get_recent_bars(CID) == []  # rien de fermé encore


def test_bar_closes_on_minute_rollover(buf, base_ts):
    """Trade dans la minute suivante → bar précédent fermé."""
    buf.consume(_trade(100.0, 1, base_ts.replace(second=10)))
    buf.consume(_trade(105.0, 2, base_ts.replace(minute=31, second=5)))

    closed = buf.get_recent_bars(CID)
    assert len(closed) == 1
    assert closed[0].closed
    assert closed[0].close == 100.0
    assert closed[0].start_ts == base_ts  # minute 30

    cur = buf.get_current_forming_bar(CID)
    assert cur.open == 105.0
    assert cur.start_ts == base_ts.replace(minute=31)


def test_minute_with_no_trades_is_skipped(buf, base_ts):
    """Si aucun trade dans une minute, elle n'apparaît pas dans la deque."""
    # Trade en minute 30, puis minute 33 (saute 31, 32)
    buf.consume(_trade(100.0, 1, base_ts.replace(second=5)))
    buf.consume(_trade(101.0, 1, base_ts.replace(minute=33, second=0)))

    closed = buf.get_recent_bars(CID)
    # Seule la minute 30 a été fermée — pas de bars vides pour 31, 32
    assert len(closed) == 1
    assert closed[0].start_ts.minute == 30


def test_out_of_order_trade_dropped(buf, base_ts):
    """Trade plus ancien que le bar courant → drop + compteur incrémenté."""
    buf.consume(_trade(100.0, 1, base_ts.replace(minute=30, second=5)))
    buf.consume(_trade(101.0, 1, base_ts.replace(minute=31, second=5)))
    # Minute 30 est maintenant closed. Un trade rétro à minute 28 doit être drop.
    buf.consume(_trade(50.0, 99, base_ts.replace(minute=28, second=0)))

    assert buf.health()["trades_dropped"] == 1
    # Le bar minute 28 ne doit pas exister
    assert all(b.start_ts.minute != 28 for b in buf.get_recent_bars(CID))


# ─────────────────────────────────────────────────────────────────────────────
# Filtrage des non-trades
# ─────────────────────────────────────────────────────────────────────────────


def test_quote_events_ignored(buf, base_ts):
    """Les quotes ne contribuent pas au bar M1 (choix Phase C)."""
    buf.consume(_quote(base_ts.replace(second=5)))
    buf.consume(_quote(base_ts.replace(second=15)))
    assert buf.get_current_forming_bar(CID) is None
    assert buf.health()["trades_consumed"] == 0


def test_trade_without_price_ignored(buf, base_ts):
    evt = MarketEvent(kind="trade", contract_id=CID, ts_exchange=base_ts, price=None, volume=1)
    buf.consume(evt)
    assert buf.get_current_forming_bar(CID) is None


# ─────────────────────────────────────────────────────────────────────────────
# inject_bars (gap-fill REST)
# ─────────────────────────────────────────────────────────────────────────────


def test_inject_bars_marks_REST(buf, base_ts):
    bars = [
        M1Bar(
            contract_id=CID,
            start_ts=base_ts.replace(minute=20),
            open=100.0,
            high=101.0,
            low=99.5,
            close=100.5,
            volume=50,
        ),
        M1Bar(
            contract_id=CID,
            start_ts=base_ts.replace(minute=21),
            open=100.5,
            high=102.0,
            low=100.0,
            close=101.5,
            volume=80,
        ),
    ]
    n = buf.inject_bars(bars)
    assert n == 2
    out = buf.get_recent_bars(CID, n=10)
    assert len(out) == 2
    assert all(b.source == "REST" for b in out)
    assert all(b.closed for b in out)


def test_inject_dedup_priority_to_WS(buf, base_ts):
    """Si un bar WS existe déjà pour start_ts donné, le REST est skip."""
    # Crée d'abord un bar WS
    buf.consume(_trade(100.0, 1, base_ts.replace(second=5)))
    buf.consume(_trade(101.0, 1, base_ts.replace(minute=31, second=5)))
    # Maintenant minute 30 est closed (WS)

    # Tentative d'injection sur minute 30 (qui existe déjà en WS)
    rest_bar = M1Bar(
        contract_id=CID,
        start_ts=base_ts.replace(minute=30),
        open=999.0,
        high=999.0,
        low=999.0,
        close=999.0,
        volume=99,
    )
    n = buf.inject_bars([rest_bar])
    assert n == 0  # skip — déjà présent
    out = buf.get_recent_bars(CID)
    assert len(out) == 1
    assert out[0].open == 100.0  # WS conservé
    assert out[0].source == "WS"


def test_inject_bars_sorted_after_mixed_insert(buf, base_ts):
    """Inject en désordre puis tri auto."""
    bars = [
        M1Bar(
            contract_id=CID,
            start_ts=base_ts.replace(minute=25),
            open=2,
            high=2,
            low=2,
            close=2,
            volume=1,
        ),
        M1Bar(
            contract_id=CID,
            start_ts=base_ts.replace(minute=20),
            open=1,
            high=1,
            low=1,
            close=1,
            volume=1,
        ),
        M1Bar(
            contract_id=CID,
            start_ts=base_ts.replace(minute=22),
            open=1.5,
            high=1.5,
            low=1.5,
            close=1.5,
            volume=1,
        ),
    ]
    buf.inject_bars(bars)
    out = buf.get_recent_bars(CID, n=10)
    minutes = [b.start_ts.minute for b in out]
    assert minutes == [20, 22, 25]


# ─────────────────────────────────────────────────────────────────────────────
# flush_stale_bars
# ─────────────────────────────────────────────────────────────────────────────


def test_flush_stale_closes_bar_when_minute_passed(buf, base_ts):
    buf.consume(_trade(100.0, 1, base_ts.replace(second=5)))
    # Force la clôture en passant un now_utc dans la minute suivante
    n = buf.flush_stale_bars(now_utc=base_ts.replace(minute=31, second=30))
    assert n == 1
    assert buf.get_current_forming_bar(CID) is None
    closed = buf.get_recent_bars(CID)
    assert len(closed) == 1
    assert closed[0].closed


def test_flush_stale_keeps_bar_if_still_in_minute(buf, base_ts):
    buf.consume(_trade(100.0, 1, base_ts.replace(second=5)))
    # now_utc dans la même minute → pas de clôture
    n = buf.flush_stale_bars(now_utc=base_ts.replace(second=55))
    assert n == 0
    assert buf.get_current_forming_bar(CID) is not None


# ─────────────────────────────────────────────────────────────────────────────
# Lecture
# ─────────────────────────────────────────────────────────────────────────────


def test_get_bars_since(buf, base_ts):
    """get_bars_since filtre correctement par start_ts."""
    for m in range(5):
        buf.consume(_trade(100.0 + m, 1, base_ts.replace(minute=30 + m, second=5)))
    # Le dernier bar n'est pas fermé (minute 34 en cours)
    threshold = base_ts.replace(minute=32)
    bars = buf.get_bars_since(CID, threshold)
    minutes = [b.start_ts.minute for b in bars]
    assert minutes == [32, 33]  # 30 et 31 < threshold, 34 = en cours (non inclus)

    # Avec include_forming
    bars_inc = buf.get_bars_since(CID, threshold, include_forming=True)
    minutes_inc = [b.start_ts.minute for b in bars_inc]
    assert minutes_inc == [32, 33, 34]


def test_max_minutes_bounded(base_ts):
    """deque maxlen = max_minutes → les vieux bars sont évincés."""
    buf = M1Buffer(max_minutes=3)
    for m in range(7):
        buf.consume(_trade(100.0 + m, 1, base_ts.replace(minute=m, second=5)))
    # On a 7 minutes de trades. Minutes 0-5 sont fermées (6 bars closed), mais
    # maxlen=3 → seuls les 3 derniers gardés. Minute 6 = en cours.
    closed = buf.get_recent_bars(CID, n=10)
    assert len(closed) == 3
    minutes = [b.start_ts.minute for b in closed]
    assert minutes == [3, 4, 5]


def test_get_recent_bars_include_forming(buf, base_ts):
    buf.consume(_trade(100.0, 1, base_ts.replace(second=5)))
    buf.consume(_trade(101.0, 1, base_ts.replace(minute=31, second=5)))

    closed_only = buf.get_recent_bars(CID, include_forming=False)
    assert len(closed_only) == 1

    with_forming = buf.get_recent_bars(CID, include_forming=True)
    assert len(with_forming) == 2
    assert not with_forming[-1].closed


# ─────────────────────────────────────────────────────────────────────────────
# Multi-contract
# ─────────────────────────────────────────────────────────────────────────────


def test_multiple_contracts_isolated(buf, base_ts):
    """Deux contracts → buffers indépendants."""
    buf.consume(_trade(100.0, 1, base_ts.replace(second=5), cid="A"))
    buf.consume(_trade(200.0, 1, base_ts.replace(second=10), cid="B"))
    buf.consume(_trade(101.0, 1, base_ts.replace(second=15), cid="A"))

    cur_a = buf.get_current_forming_bar("A")
    cur_b = buf.get_current_forming_bar("B")
    assert cur_a.close == 101.0 and cur_a.volume == 2
    assert cur_b.close == 200.0 and cur_b.volume == 1
    assert set(buf.contracts()) == {"A", "B"}


def test_empty_buffer_returns_empty(buf):
    assert buf.get_recent_bars(CID) == []
    assert buf.get_bars_since(CID, datetime(2026, 1, 1, tzinfo=UTC)) == []
    assert buf.get_current_forming_bar(CID) is None
    assert buf.contracts() == []


# ─────────────────────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────────────────────


def test_floor_minute_naive_assumes_utc(base_ts):
    naive = base_ts.replace(tzinfo=None, second=42)
    f = _floor_minute(naive)
    assert f.tzinfo is not None
    assert f.second == 0


def test_max_minutes_must_be_positive():
    with pytest.raises(ValueError):
        M1Buffer(max_minutes=1)
