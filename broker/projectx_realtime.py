"""
ProjectXRealtimeClient — client SignalR temps réel du User Hub ProjectX.

Maintient une connexion WebSocket à `rtc.topstepx.com/hubs/user` et pousse les
events `GatewayUserOrder/Position/Trade/Account` dans une queue thread-safe que
le `SessionRunner` draine au début de chaque tick (et de chaque micro-sync 30 s).

═══════════════════════════════════════════════════════════════════════════════
ARCHITECTURE : producteur / consommateur strict
═══════════════════════════════════════════════════════════════════════════════
La lib `signalrcore` spawn ses propres threads (réception WebSocket, reconnect).
Nos handlers `_on_user_*` tournent sur ces threads — ils ne font QUE pousser
dans `_queue`. Le runner (thread principal) consomme via `drain_events()` et
mute l'état `placed_tags` / PortfolioRiskManager. Aucune mutation cross-thread
hors de la queue.

═══════════════════════════════════════════════════════════════════════════════
IMPORTANT — pas de replay
═══════════════════════════════════════════════════════════════════════════════
SignalR n'a pas de mécanisme de replay : les events émis pendant un outage du
WS sont définitivement perdus. C'est exactement pourquoi le polling REST 30 s
(`SessionRunner._sync_broker`) reste autoritatif côté runner — il rattrape ce
que le WS aurait raté. Idempotence côté runner : `placed_tags[tag].status`
empêche les double-transitions.

═══════════════════════════════════════════════════════════════════════════════
ROBUSTESSE
═══════════════════════════════════════════════════════════════════════════════
- Reconnect lib (`with_automatic_reconnect`) gère les blips transitoires
- Supervisor thread (boucle 10 s) gère :
    • Reconnect forcé si déconnecté > 60 s sans recovery lib
    • Détection zombie TCP : si connecté mais silencieux > max_silence_s
      pendant les heures de marché → force rebuild
    • Re-auth périodique tous les 22 h pour rafraîchir le JWT bearer
- `_rebuild_and_start()` est le SEUL endroit qui touche `_connection`, sous lock
- Queue saturée : drop-oldest (les updates stales sont inutiles)
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, List, Optional

_log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Event normalisé pour transit cross-thread (producteur WS → consommateur runner)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RealtimeEvent:
    """
    Event SignalR parsé, prêt à être consommé par le runner.

    `payload` garde la version brute pour fallback (cas où on découvre un nouveau
    champ utile pendant le burn-in). Les autres attributs sont les extractions
    plausibles d'après la sémantique REST ProjectX — à valider via
    `scripts/realtime_smoke.py` qui imprime les payloads bruts.
    """
    kind: str                                  # "order" | "position" | "trade" | "account"
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    payload: dict = field(default_factory=dict)
    contract_id: Optional[str] = None
    order_id: Optional[int] = None
    custom_tag: Optional[str] = None
    pnl: Optional[float] = None                # uniquement pour trades clôturants
    size: Optional[int] = None                 # pour position events (0 = flat)
    status: Optional[int] = None               # pour order events (code broker)


# ─────────────────────────────────────────────────────────────────────────────
# Client realtime
# ─────────────────────────────────────────────────────────────────────────────

class ProjectXRealtimeClient:
    """
    Client SignalR du User Hub ProjectX.

    Usage typique :
        client = ProjectXClient(...)
        rt = ProjectXRealtimeClient(
            account_id=12345,
            token_provider=lambda: client.token,      # property refresh-aware
            hub_url="https://rtc.topstepx.com/hubs/user",
        )
        rt.start()
        # ... plus tard, dans le runner :
        for evt in rt.drain_events(max_events=500):
            apply(evt)
        rt.stop()

    `token_provider` DOIT être un callable (et non une string) pour que la
    re-authentification 23 h soit transparente : à chaque rebuild de connexion,
    on appelle `token_provider()` qui passe par `_maybe_reauth()` côté REST.
    """

    def __init__(
        self,
        account_id: int,
        token_provider: Callable[[], str],
        hub_url: str,
        queue_maxsize: int = 2048,
        reconnect_delays: tuple = (0, 2, 5, 10, 30, 60, 120),
        max_silence_s: float = 180.0,
        force_reauth_s: float = 22 * 3600,
        market_open_check: Optional[Callable[[], bool]] = None,
    ):
        # Identité et providers
        self._account_id     = int(account_id)
        self._token_provider = token_provider
        self._hub_url        = hub_url
        self._market_open    = market_open_check or (lambda: True)

        # Politique reconnect/zombie
        self._reconnect_delays = tuple(reconnect_delays) or (5,)
        self._max_silence_s    = float(max_silence_s)
        self._force_reauth_s   = float(force_reauth_s)

        # Queue d'events (drop-oldest si saturée)
        self._queue: queue.Queue = queue.Queue(maxsize=int(queue_maxsize))

        # État connexion (cross-thread, protégé par _lock pour _connection).
        # RLock car _on_open peut être appelé synchroniquement depuis start()
        # et a besoin de re-prendre le lock pour send() les subscriptions.
        self._connection = None
        self._lock        = threading.RLock()
        self._connected   = threading.Event()
        self._stop_event  = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Télémétrie (lectures simples par drain — pas de lock nécessaire,
        # int writes sont atomiques en CPython)
        self._last_event_ts    = time.monotonic()
        self._last_open_ts     = 0.0
        self._last_close_ts    = 0.0
        self._last_drop_log_ts = 0.0
        self._disconnect_count = 0
        self._reconnect_attempt = 0
        self._dropped_events   = 0

    # ─────────────────────────────────────────────────────────────────────
    # API publique
    # ─────────────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Démarre la connexion WS et le supervisor thread. Idempotent."""
        if self._thread is not None and self._thread.is_alive():
            _log.debug("Realtime: start() appelé alors que déjà démarré")
            return

        self._stop_event.clear()
        self._build_connection_and_start()
        self._thread = threading.Thread(
            target=self._supervisor_loop,
            name="projectx-realtime-supervisor",
            daemon=True,
        )
        self._thread.start()
        _log.info("Realtime: client démarré (hub=%s)", self._hub_url)

    def stop(self, timeout: float = 5.0) -> None:
        """Arrête proprement le supervisor et la connexion WS."""
        self._stop_event.set()
        with self._lock:
            try:
                if self._connection is not None:
                    self._connection.stop()
            except Exception as exc:
                _log.warning("Realtime: stop() — erreur _connection.stop : %s", exc)
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        _log.info("Realtime: client arrêté")

    def is_connected(self) -> bool:
        return self._connected.is_set()

    def drain_events(self, max_events: int = 500) -> List[RealtimeEvent]:
        """Drain non-bloquant. Retourne jusqu'à `max_events` events FIFO."""
        out: List[RealtimeEvent] = []
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
            "connected":          self._connected.is_set(),
            "queue_depth":        self._queue.qsize(),
            "dropped_events":     self._dropped_events,
            "last_event_age_s":   now - last_evt,
            "disconnect_count":   self._disconnect_count,
            "reconnect_attempt":  self._reconnect_attempt,
        }

    # ─────────────────────────────────────────────────────────────────────
    # Construction connexion + handlers SignalR
    # ─────────────────────────────────────────────────────────────────────

    def _build_connection_and_start(self) -> None:
        """Construit la HubConnection avec le JWT frais et la démarre."""
        # Import lazy : si la lib n'est pas installée, le module est encore
        # importable (le runner peut décider de désactiver realtime à l'init).
        from signalrcore.hub_connection_builder import HubConnectionBuilder

        token = self._token_provider()
        if not token:
            raise RuntimeError("Realtime: token_provider() a retourné un token vide")

        url_with_token = f"{self._hub_url}?access_token={token}"

        connection = (
            HubConnectionBuilder()
            .with_url(
                url_with_token,
                options={
                    "access_token_factory": self._token_provider,
                    "skip_negotiation":     False,
                },
            )
            .with_automatic_reconnect({
                "type":               "raw",
                "keep_alive_interval": 10,
                "reconnect_interval":  5,
                # max_attempts=-1 : désactive le reconnect AUTO de signalrcore
                # (lève ValueError au 1er essai → la lib n'enchaîne pas). Le
                # keep_alive_interval reste actif (géré par ConnectionStateChecker,
                # indépendant du ReconnectionHandler). Notre supervisor (boucle
                # 10 s + zombie detection) devient le SEUL responsable du
                # reconnect → plus de course double-rebuild / HTTP 429.
                # Fix bug observé en burn-in 2026-05-18 (95 erreurs / 6 min).
                "max_attempts":        -1,
            })
            .build()
        )

        # Lifecycle hooks
        connection.on_open(self._on_open)
        connection.on_close(self._on_close)
        connection.on_error(self._on_error)
        connection.on_reconnect(self._on_open)  # safety : ré-subscribe au reconnect

        # Event handlers — noms exacts à confirmer via smoke test
        connection.on("GatewayUserOrder",    self._on_user_order)
        connection.on("GatewayUserPosition", self._on_user_position)
        connection.on("GatewayUserTrade",    self._on_user_trade)
        connection.on("GatewayUserAccount",  self._on_user_account)

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
                _log.debug("Realtime: stop pré-rebuild : %s", exc)
            self._connection = None
        # build_and_start a son propre _lock — on relâche d'abord
        try:
            self._build_connection_and_start()
            _log.info("Realtime: rebuild OK (tentative #%d)", self._reconnect_attempt)
        except Exception as exc:
            _log.error("Realtime: rebuild échoué (#%d) : %s",
                       self._reconnect_attempt, exc)

    # ─── Hooks SignalR ────────────────────────────────────────────────────

    def _on_open(self, *_) -> None:
        try:
            self._connected.set()
            self._last_open_ts = time.monotonic()
            self._last_event_ts = self._last_open_ts  # reset zombie watchdog
            with self._lock:
                if self._connection is not None:
                    self._connection.send("SubscribeAccounts",  [])
                    self._connection.send("SubscribeOrders",    [self._account_id])
                    self._connection.send("SubscribePositions", [self._account_id])
                    self._connection.send("SubscribeTrades",    [self._account_id])
            _log.info("Realtime: connecté & subscriptions envoyées (account=%d)",
                      self._account_id)
        except Exception as exc:
            _log.exception("Realtime: _on_open échoué : %s", exc)

    def _on_close(self, *_) -> None:
        self._connected.clear()
        self._last_close_ts = time.monotonic()
        self._disconnect_count += 1
        _log.warning("Realtime: déconnecté (count=%d)", self._disconnect_count)

    def _on_error(self, err) -> None:
        _log.error("Realtime: erreur WS : %s", err)

    # ─── Event handlers métier (push dans la queue) ───────────────────────
    #
    # IMPORTANT : ces handlers tournent sur le thread de réception signalrcore.
    # Tout doit être : (1) wrappé try/except, (2) borné en durée, (3) sans
    # mutation hors-queue. Si une exception remonte, signalrcore peut la
    # swallow silencieusement selon la version → log explicite.
    #
    # Les noms de champs (customTag, contractId, profitAndLoss…) sont les
    # extractions plausibles d'après la sémantique REST. À confirmer via
    # `scripts/realtime_smoke.py` qui imprime les payloads bruts.

    def _on_user_order(self, args) -> None:
        try:
            p = self._extract_payload(args)
            evt = RealtimeEvent(
                kind="order",
                payload=p,
                contract_id=str(p.get("contractId")) if p.get("contractId") else None,
                order_id=p.get("id") or p.get("orderId"),
                custom_tag=p.get("customTag") or p.get("tag"),
                status=p.get("status"),
            )
            self._push(evt)
        except Exception as exc:
            _log.exception("Realtime: _on_user_order parse failed : %s", exc)

    def _on_user_position(self, args) -> None:
        try:
            p = self._extract_payload(args)
            evt = RealtimeEvent(
                kind="position",
                payload=p,
                contract_id=str(p.get("contractId")) if p.get("contractId") else None,
                size=p.get("size"),
            )
            self._push(evt)
        except Exception as exc:
            _log.exception("Realtime: _on_user_position parse failed : %s", exc)

    def _on_user_trade(self, args) -> None:
        try:
            p = self._extract_payload(args)
            pnl = p.get("profitAndLoss")
            evt = RealtimeEvent(
                kind="trade",
                payload=p,
                contract_id=str(p.get("contractId")) if p.get("contractId") else None,
                order_id=p.get("orderId"),
                custom_tag=p.get("customTag") or p.get("tag"),
                pnl=float(pnl) if pnl is not None else None,
            )
            self._push(evt)
        except Exception as exc:
            _log.exception("Realtime: _on_user_trade parse failed : %s", exc)

    def _on_user_account(self, args) -> None:
        try:
            p = self._extract_payload(args)
            self._push(RealtimeEvent(kind="account", payload=p))
        except Exception as exc:
            _log.exception("Realtime: _on_user_account parse failed : %s", exc)

    @staticmethod
    def _extract_payload(args) -> dict:
        """
        signalrcore passe les args sous forme list[dict]. ProjectX encapsule
        les events dans {"action": <int>, "data": {<vrais champs>}} —
        confirmé via scripts/realtime_smoke.py le 2026-05-18.
        On retourne `data` si présent, sinon le raw (robustesse au cas où).
        """
        if not args:
            return {}
        raw = args[0] if isinstance(args, list) else args
        if not isinstance(raw, dict):
            return {}
        # Déballer l'enveloppe ProjectX {action, data}
        if "data" in raw and isinstance(raw["data"], dict):
            return raw["data"]
        return raw

    # ─── Back-pressure : queue saturée → drop-oldest ──────────────────────

    def _push(self, evt: RealtimeEvent) -> None:
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
            # Ne devrait pas arriver après un get_nowait, mais safe
            pass
        # Log throttlé (1× par minute)
        now = time.monotonic()
        if now - self._last_drop_log_ts > 60:
            self._last_drop_log_ts = now
            _log.warning("Realtime: queue saturée, drop oldest (total=%d)",
                         self._dropped_events)

    # ─────────────────────────────────────────────────────────────────────
    # Supervisor thread — détection zombie + reconnect forcé + reauth périodique
    # ─────────────────────────────────────────────────────────────────────

    def _supervisor_loop(self) -> None:
        """
        Boucle 10 s qui surveille l'état de la connexion.

        Trois cas déclenchent un `_rebuild_and_start()` :
          1. Déconnecté depuis > 60 s SANS que la lib ait rebuilé (la lib peut
             abandonner en cas d'auth invalide — on prend le relais avec backoff)
          2. Connecté mais silencieux > max_silence_s pendant marché ouvert
             (zombie TCP : socket apparait up, broker ne pousse plus)
          3. Connexion ouverte depuis force_reauth_s (22 h) : rebuild pour
             obtenir un JWT frais (le broker peut révoquer après 24 h)
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
                        _log.info("Realtime: déconnecté %.0fs — rebuild dans %ds",
                                  age, delay)
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
                        "Realtime: silence %.0fs > max %.0fs → rebuild forcé",
                        silence, self._max_silence_s,
                    )
                    self._rebuild_and_start()
                    backoff_idx = 0  # reset backoff après succès apparent
                    continue

                # Cas 3 : re-auth périodique
                if self._last_open_ts > 0 and \
                        (now - self._last_open_ts) > self._force_reauth_s:
                    _log.info("Realtime: re-auth périodique (open depuis %.0fh)",
                              (now - self._last_open_ts) / 3600)
                    self._rebuild_and_start()
                    backoff_idx = 0
                    continue

                # Tout va bien — reset le backoff
                backoff_idx = 0

            except Exception as exc:
                _log.exception("Realtime: supervisor exception : %s", exc)
