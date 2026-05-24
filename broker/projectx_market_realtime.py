"""
ProjectXMarketRealtimeClient — client SignalR temps réel du Market Hub ProjectX.

Maintient une connexion WebSocket à `rtc.topstepx.com/hubs/market` et pousse les
events `GatewayQuote` et `GatewayTrade` (pour chaque `contract_id` souscrit) dans
une queue thread-safe que le runner peut drainer pour alimenter un `M1Buffer`.

═══════════════════════════════════════════════════════════════════════════════
DIFFÉRENCES VS USER HUB (broker/projectx_realtime.py)
═══════════════════════════════════════════════════════════════════════════════
1. **Pas de wrapper `{action, data}`** : les events Market Hub arrivent sous la
   forme `args = [contract_id_str, payload]` (liste de 2 éléments). Confirmé via
   `scripts/realtime_smoke_market.py` le 2026-05-18.

2. **Subscribe par contract_id** : on appelle `SubscribeContractQuotes(cid)`
   et `SubscribeContractTrades(cid)` pour chaque contract, vs `SubscribeOrders/
   Positions/Trades(account_id)` côté User Hub.

3. **Volume d'events >> User Hub** : ~28 evt/s mesuré sur 1 contract pré-RTH.
   En RTH avec NQ1+YM1, attendre 100-500 evt/s. Conséquences :
     - queue_maxsize plus grand (10k par défaut vs 2k User Hub)
     - max_silence_s plus court (60s vs 180s)
     - Ne JAMAIS logger chaque event par défaut

4. **GatewayTrade et GatewayDepth sont des LISTES batched** : chaque event
   peut contenir N trades. Le client EXPLODE en N `MarketEvent(kind="trade")`
   pour simplifier le consommateur (1 event = 1 trade). On skip Depth pour
   Phase C (non utilisé par le buffer M1).

5. **GatewayQuote peut être PARTIELLE** : la plupart des updates ne contiennent
   que `bestBid/bestAsk` (sans `lastPrice/volume`). Le buffer M1 utilise les
   trades uniquement pour OHLCV → pas de state merging nécessaire ici. Les
   quotes restent disponibles pour monitoring/alerting séparé.

═══════════════════════════════════════════════════════════════════════════════
PATTERN PRODUCTEUR / CONSOMMATEUR STRICT (identique Phase B)
═══════════════════════════════════════════════════════════════════════════════
- Handlers `_on_*` tournent sur le thread signalrcore — ne mutent QUE la queue
- Le runner (main thread) consomme via `drain_events()`
- Aucune mutation cross-thread hors-queue
- Idempotence côté consommateur (M1Buffer keyed par timestamp UTC.floor("1min"))

═══════════════════════════════════════════════════════════════════════════════
ROBUSTESSE
═══════════════════════════════════════════════════════════════════════════════
- Reconnect lib (`with_automatic_reconnect`) pour blips transitoires
- Supervisor thread (10 s) :
    • Reconnect forcé si déconnecté > 60 s
    • Détection zombie : si connecté mais silencieux > max_silence_s pendant
      le marché → force rebuild
    • Re-auth périodique 22 h pour rafraîchir le JWT
- Queue saturée → drop-oldest (un tick stale est inutile)
- `_lock = RLock` obligatoire (cf. broker/projectx_realtime.py:127 — deadlock
  observé avec Lock non-réentrant en Phase B)
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

_log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Event normalisé (1 trade ou 1 quote = 1 MarketEvent)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class MarketEvent:
    """
    Event Market Hub parsé, prêt à être consommé par le runner / M1Buffer.

    Champs (kind="trade"):
        contract_id  : "CON.F.US.MNQ.M26"
        price        : float — prix exécuté
        volume       : int — contrats échangés
        ts_exchange  : datetime UTC — timestamp côté exchange
        aggressor    : Optional[int] — type ProjectX (0/1), à mapper buy/sell
                       si nécessaire (non utilisé par le buffer M1).

    Champs (kind="quote"):
        contract_id  : "CON.F.US.MNQ.M26"
        best_bid     : Optional[float] — bid si présent dans cette update
        best_ask     : Optional[float] — ask si présent
        last_price   : Optional[float] — dernier trade reporté par le broker
        ts_exchange  : datetime UTC

    `payload` garde la version brute pour debug et nouveaux champs futurs.
    """

    kind: str  # "trade" | "quote"
    contract_id: str
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    ts_exchange: datetime | None = None
    payload: dict = field(default_factory=dict)

    # Champs trade
    price: float | None = None
    volume: int | None = None
    aggressor: int | None = None

    # Champs quote
    best_bid: float | None = None
    best_ask: float | None = None
    last_price: float | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Helpers de parsing
# ─────────────────────────────────────────────────────────────────────────────


def _parse_ts(s) -> datetime | None:
    """Parse un timestamp ISO 8601 tz-aware en datetime UTC. None si invalide."""
    if not s:
        return None
    if isinstance(s, datetime):
        return s.astimezone(UTC) if s.tzinfo else s.replace(tzinfo=UTC)
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    # Filtre les timestamps "0001-01-01" (sentinel ProjectX = N/A)
    if dt.year < 2000:
        return None
    return dt.astimezone(UTC)


# ─────────────────────────────────────────────────────────────────────────────
# Client realtime
# ─────────────────────────────────────────────────────────────────────────────


class ProjectXMarketRealtimeClient:
    """
    Client SignalR du Market Hub ProjectX.

    Usage typique :
        client = ProjectXClient(...)
        rt = ProjectXMarketRealtimeClient(
            contract_ids   = ["CON.F.US.MNQ.M26", "CON.F.US.MYM.M26"],
            token_provider = lambda: client.token,
            hub_url        = "https://rtc.topstepx.com/hubs/market",
        )
        rt.start()
        # ... dans la boucle runner :
        for evt in rt.drain_events(max_events=2000):
            m1_buffer.consume(evt)
        rt.stop()

    Note importante : `token_provider` DOIT être un callable (et non un string)
    pour que la re-authentification 23h soit transparente — chaque rebuild de
    connexion appelle `token_provider()` qui passe par `_maybe_reauth()` REST.
    """

    def __init__(
        self,
        contract_ids: Sequence[str],
        token_provider: Callable[[], str],
        hub_url: str,
        queue_maxsize: int = 10_000,
        reconnect_delays: tuple = (0, 2, 5, 10, 30, 60, 120),
        max_silence_s: float = 60.0,
        force_reauth_s: float = 22 * 3600,
        market_open_check: Callable[[], bool] | None = None,
        subscribe_quotes: bool = True,
        subscribe_trades: bool = True,
    ):
        if not contract_ids:
            raise ValueError("contract_ids ne peut pas être vide")

        self._contract_ids = list(contract_ids)
        self._token_provider = token_provider
        self._hub_url = hub_url
        self._market_open = market_open_check or (lambda: True)
        self._sub_quotes = bool(subscribe_quotes)
        self._sub_trades = bool(subscribe_trades)

        # Politique reconnect/zombie
        self._reconnect_delays = tuple(reconnect_delays) or (5,)
        self._max_silence_s = float(max_silence_s)
        self._force_reauth_s = float(force_reauth_s)

        # Queue d'events (drop-oldest si saturée).
        self._queue: queue.Queue = queue.Queue(maxsize=int(queue_maxsize))

        # État connexion. RLock car _on_open est appelé synchroniquement depuis
        # start() et a besoin de re-prendre le lock pour _connection.send().
        # Reproduction du pattern broker/projectx_realtime.py:127-132 (sinon
        # deadlock observé en Phase B avec threading.Lock).
        self._connection = None
        self._lock = threading.RLock()
        self._connected = threading.Event()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        # Télémétrie
        self._last_event_ts = time.monotonic()
        self._last_open_ts = 0.0
        self._last_close_ts = 0.0
        self._last_drop_log_ts = 0.0
        self._disconnect_count = 0
        self._reconnect_attempt = 0
        self._dropped_events = 0
        self._trade_count = 0
        self._quote_count = 0

    # ─────────────────────────────────────────────────────────────────────
    # API publique
    # ─────────────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Démarre la connexion WS et le supervisor thread. Idempotent."""
        if self._thread is not None and self._thread.is_alive():
            _log.debug("MarketRT: start() appelé alors que déjà démarré")
            return

        self._stop_event.clear()
        self._build_connection_and_start()
        self._thread = threading.Thread(
            target=self._supervisor_loop,
            name="projectx-market-realtime-supervisor",
            daemon=True,
        )
        self._thread.start()
        _log.info(
            "MarketRT: client démarré (hub=%s, contracts=%d)",
            self._hub_url,
            len(self._contract_ids),
        )

    def stop(self, timeout: float = 5.0) -> None:
        """Arrête proprement le supervisor et la connexion WS."""
        self._stop_event.set()
        with self._lock:
            try:
                if self._connection is not None:
                    self._connection.stop()
            except Exception as exc:
                _log.warning("MarketRT: stop() — erreur _connection.stop : %s", exc)
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        _log.info("MarketRT: client arrêté")

    def is_connected(self) -> bool:
        return self._connected.is_set()

    def drain_events(self, max_events: int = 2000) -> list[MarketEvent]:
        """Drain non-bloquant. Retourne jusqu'à `max_events` events FIFO."""
        out: list[MarketEvent] = []
        for _ in range(max_events):
            try:
                out.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return out

    def health(self) -> dict:
        """Snapshot santé pour Telegram / Event logger."""
        now = time.monotonic()
        last_evt = self._last_event_ts or now
        return {
            "connected": self._connected.is_set(),
            "queue_depth": self._queue.qsize(),
            "dropped_events": self._dropped_events,
            "last_event_age_s": now - last_evt,
            "disconnect_count": self._disconnect_count,
            "reconnect_attempt": self._reconnect_attempt,
            "trade_count": self._trade_count,
            "quote_count": self._quote_count,
            "contract_ids": list(self._contract_ids),
        }

    # ─────────────────────────────────────────────────────────────────────
    # Construction connexion + handlers SignalR
    # ─────────────────────────────────────────────────────────────────────

    def _build_connection_and_start(self) -> None:
        """Construit la HubConnection avec le JWT frais et la démarre."""
        from signalrcore.hub_connection_builder import HubConnectionBuilder

        token = self._token_provider()
        if not token:
            raise RuntimeError("MarketRT: token_provider() a retourné un token vide")

        url_with_token = f"{self._hub_url}?access_token={token}"

        connection = (
            HubConnectionBuilder()
            .with_url(
                url_with_token,
                options={
                    "access_token_factory": self._token_provider,
                    "skip_negotiation": False,
                },
            )
            .with_automatic_reconnect(
                {
                    "type": "raw",
                    "keep_alive_interval": 10,
                    "reconnect_interval": 5,
                    # max_attempts=-1 : désactive le reconnect AUTO de signalrcore.
                    # Cf. broker/projectx_realtime.py — fix bug double-reconnect /
                    # HTTP 429. Notre supervisor (boucle 10 s) gère tous les
                    # reconnects. Keep_alive_interval reste actif (handler séparé).
                    "max_attempts": -1,
                }
            )
            .build()
        )

        # Lifecycle hooks
        connection.on_open(self._on_open)
        connection.on_close(self._on_close)
        connection.on_error(self._on_error)
        connection.on_reconnect(self._on_open)  # safety : ré-subscribe au reconnect

        # Event handlers Market Hub
        connection.on("GatewayQuote", self._on_quote)
        connection.on("GatewayTrade", self._on_trade)
        # NB: GatewayDepth volontairement non câblé (non utilisé Phase C)

        with self._lock:
            self._connection = connection
            connection.start()

    def _rebuild_and_start(self) -> None:
        """Détruit la connexion courante et en recrée une avec un JWT frais."""
        self._reconnect_attempt += 1
        with self._lock:
            try:
                if self._connection is not None:
                    self._connection.stop()
            except Exception as exc:
                _log.debug("MarketRT: stop pré-rebuild : %s", exc)
            self._connection = None
        try:
            self._build_connection_and_start()
            _log.info("MarketRT: rebuild OK (tentative #%d)", self._reconnect_attempt)
        except Exception as exc:
            _log.error("MarketRT: rebuild échoué (#%d) : %s", self._reconnect_attempt, exc)

    # ─── Hooks SignalR ────────────────────────────────────────────────────

    def _on_open(self, *_) -> None:
        try:
            self._connected.set()
            self._last_open_ts = time.monotonic()
            self._last_event_ts = self._last_open_ts  # reset zombie watchdog
            with self._lock:
                if self._connection is None:
                    return
                for cid in self._contract_ids:
                    if self._sub_quotes:
                        self._connection.send("SubscribeContractQuotes", [cid])
                    if self._sub_trades:
                        self._connection.send("SubscribeContractTrades", [cid])
            _log.info(
                "MarketRT: connecté & subscriptions envoyées " "(quotes=%s trades=%s contracts=%s)",
                self._sub_quotes,
                self._sub_trades,
                self._contract_ids,
            )
        except Exception as exc:
            _log.exception("MarketRT: _on_open échoué : %s", exc)

    def _on_close(self, *_) -> None:
        self._connected.clear()
        self._last_close_ts = time.monotonic()
        self._disconnect_count += 1
        _log.warning("MarketRT: déconnecté (count=%d)", self._disconnect_count)

    def _on_error(self, err) -> None:
        _log.error("MarketRT: erreur WS : %s", err)

    # ─── Event handlers métier ────────────────────────────────────────────
    #
    # Les handlers tournent sur le thread de réception signalrcore. Ils doivent :
    # (1) être wrappés try/except, (2) être bornés en durée, (3) ne pas muter
    # autre chose que la queue. Si une exception remonte, signalrcore peut la
    # swallow silencieusement → log explicite.
    #
    # Format args confirmé par smoke 2026-05-18 :
    #   args = [contract_id_str, payload]   où payload = dict (Quote) ou list (Trade)

    def _on_quote(self, args) -> None:
        """
        GatewayQuote → MarketEvent(kind="quote").
        Payload = dict possiblement PARTIEL (que bestBid/bestAsk parfois).
        """
        try:
            cid, payload = self._unpack(args)
            if cid is None or not isinstance(payload, dict):
                return
            ts = _parse_ts(payload.get("timestamp")) or _parse_ts(payload.get("lastUpdated"))
            evt = MarketEvent(
                kind="quote",
                contract_id=cid,
                ts_exchange=ts,
                payload=payload,
                best_bid=payload.get("bestBid"),
                best_ask=payload.get("bestAsk"),
                last_price=payload.get("lastPrice"),
            )
            self._quote_count += 1
            self._push(evt)
        except Exception as exc:
            _log.exception("MarketRT: _on_quote parse failed : %s", exc)

    def _on_trade(self, args) -> None:
        """
        GatewayTrade → N × MarketEvent(kind="trade").
        Payload = list[dict] batched (parfois avec null à filtrer).
        """
        try:
            cid, payload = self._unpack(args)
            if cid is None:
                return
            # Le payload Trade est une LISTE de trades batched
            trades = payload if isinstance(payload, list) else [payload]
            for raw_trade in trades:
                if not isinstance(raw_trade, dict):
                    continue  # skip None / valeurs aberrantes
                price = raw_trade.get("price")
                vol = raw_trade.get("volume")
                if price is None or vol is None:
                    continue
                ts = _parse_ts(raw_trade.get("timestamp"))
                # On préfère le contract_id du payload si présent (cohérence),
                # sinon on retombe sur le cid du wrapper args[0].
                contract_id = str(raw_trade.get("contractId") or cid)
                evt = MarketEvent(
                    kind="trade",
                    contract_id=contract_id,
                    ts_exchange=ts,
                    payload=raw_trade,
                    price=float(price),
                    volume=int(vol),
                    aggressor=raw_trade.get("type"),
                )
                self._trade_count += 1
                self._push(evt)
        except Exception as exc:
            _log.exception("MarketRT: _on_trade parse failed : %s", exc)

    @staticmethod
    def _unpack(args):
        """
        Désempaquète args Market Hub : `[contract_id_str, payload]`.

        Important : différent du User Hub qui utilise `{"action": N, "data": {...}}`.
        Pour Market Hub, args est une liste de 2 éléments (confirmé via smoke
        2026-05-18). On gère aussi le cas où signalrcore wrap (rare mais
        défensif).
        """
        if not args:
            return None, None
        # Cas attendu : list[str, payload]
        if isinstance(args, list):
            if len(args) >= 2 and isinstance(args[0], str):
                return args[0], args[1]
            if len(args) == 1:
                # Cas dégradé : un seul élément
                return None, args[0]
        # Cas défensif : déjà déballé
        if isinstance(args, dict):
            cid = args.get("contractId") or args.get("contract")
            return (str(cid) if cid else None), args
        return None, None

    # ─── Back-pressure : queue saturée → drop-oldest ──────────────────────

    def _push(self, evt: MarketEvent) -> None:
        self._last_event_ts = time.monotonic()
        try:
            self._queue.put_nowait(evt)
            return
        except queue.Full:
            pass
        # Saturée : drop l'oldest pour conserver le plus récent
        self._dropped_events += 1
        try:
            self._queue.get_nowait()
        except queue.Empty:
            pass
        try:
            self._queue.put_nowait(evt)
        except queue.Full:
            pass
        # Log throttlé (1× par minute) — anti-spam en cas de saturation continue
        now = time.monotonic()
        if now - self._last_drop_log_ts > 60:
            self._last_drop_log_ts = now
            _log.warning("MarketRT: queue saturée, drop oldest (total=%d)", self._dropped_events)

    # ─────────────────────────────────────────────────────────────────────
    # Supervisor thread — détection zombie + reconnect forcé + reauth périodique
    # ─────────────────────────────────────────────────────────────────────

    def _supervisor_loop(self) -> None:
        """
        Boucle 10 s qui surveille l'état de la connexion (cf. Phase B pattern).

        Trois cas déclenchent un `_rebuild_and_start()` :
          1. Déconnecté > 60 s SANS recovery lib (lib peut abandonner sur
             401/JWT expiré) → rebuild avec backoff
          2. Connecté mais silencieux > max_silence_s pendant marché ouvert →
             zombie TCP → rebuild
          3. Connexion ouverte depuis force_reauth_s → rebuild pour JWT frais
        """
        backoff_idx = 0
        while not self._stop_event.is_set():
            # Sleep 10 s par tranches de 1 s pour réagir vite à un stop()
            for _ in range(10):
                if self._stop_event.is_set():
                    return
                time.sleep(1.0)

            now = time.monotonic()
            try:
                # Cas 1 : déconnecté longuement → rebuild avec backoff
                if not self._connected.is_set():
                    age = now - self._last_close_ts if self._last_close_ts else 0
                    if age > 60:
                        delay = self._reconnect_delays[
                            min(backoff_idx, len(self._reconnect_delays) - 1)
                        ]
                        _log.info("MarketRT: déconnecté %.0fs — rebuild dans %ds", age, delay)
                        if delay > 0:
                            time.sleep(delay)
                        if self._stop_event.is_set():
                            return
                        self._rebuild_and_start()
                        backoff_idx += 1
                    continue

                # Cas 2 : zombie (connecté mais silencieux pendant marché ouvert)
                silence = now - self._last_event_ts
                if silence > self._max_silence_s and self._market_open():
                    _log.warning(
                        "MarketRT: silence %.0fs > max %.0fs → rebuild forcé",
                        silence,
                        self._max_silence_s,
                    )
                    self._rebuild_and_start()
                    backoff_idx = 0
                    continue

                # Cas 3 : re-auth périodique
                if self._last_open_ts > 0 and (now - self._last_open_ts) > self._force_reauth_s:
                    _log.info(
                        "MarketRT: re-auth périodique (open depuis %.0fh)",
                        (now - self._last_open_ts) / 3600,
                    )
                    self._rebuild_and_start()
                    backoff_idx = 0
                    continue

                # Tout va bien — reset le backoff
                backoff_idx = 0

            except Exception as exc:
                _log.exception("MarketRT: supervisor exception : %s", exc)
