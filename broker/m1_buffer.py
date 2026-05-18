"""
M1Buffer — reconstruction de bars M1 OHLCV à partir d'un flux de trades.

Consomme des `MarketEvent(kind="trade")` produits par
`ProjectXMarketRealtimeClient` et agrège incrémentalement en bars M1 par
contract_id. Stocke un historique borné (deque maxlen) + le bar courant en
formation.

═══════════════════════════════════════════════════════════════════════════════
SOURCE DE VÉRITÉ
═══════════════════════════════════════════════════════════════════════════════
- Les bars utilisent **uniquement** les MarketEvent(kind="trade") :
    bar.open   = price du premier trade de la minute
    bar.high   = max(price) sur la minute
    bar.low    = min(price) sur la minute
    bar.close  = price du dernier trade
    bar.volume = somme(volume) sur la minute
- Les MarketEvent(kind="quote") sont **ignorés** (cf. choix Phase C : match
  exact du backtest REST M1 qui n'utilise que des trades exécutés).
- Le timestamp d'agrégation est `ts_exchange` (le temps d'exécution côté broker)
  avec fallback `received_at` si absent. Buckets via `floor("1min")` UTC.

═══════════════════════════════════════════════════════════════════════════════
THREAD-SAFETY
═══════════════════════════════════════════════════════════════════════════════
Le buffer est conçu pour être consommé par le MAIN thread après chaque
`rt.drain_events()` (architecture stricte producteur/consommateur — le buffer
n'est PAS appelé depuis le thread WS). Un RLock léger reste en place pour
permettre des lectures de health/snapshot depuis un thread tiers (monitoring,
Telegram) sans risque.

═══════════════════════════════════════════════════════════════════════════════
GAP-FILL AU RECONNECT (à câbler par le caller)
═══════════════════════════════════════════════════════════════════════════════
SignalR n'a pas de replay → un outage WS = trades perdus. Le caller (runner)
doit, au reconnect, fetcher les bars M1 manquants via REST
`client.get_bars(..., unit=2, unit_number=1)` sur [last_seen_ts, now] et les
injecter via `inject_bars()`. Cette méthode flag les bars `source="REST"` pour
diagnostic.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Deque, Dict, List, Optional

from broker.projectx_market_realtime import MarketEvent


# ─────────────────────────────────────────────────────────────────────────────
# Bar M1 OHLCV
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class M1Bar:
    """
    Un bar M1 reconstruit depuis le flux de trades.

    Attributs :
        contract_id : "CON.F.US.MNQ.M26"
        start_ts    : datetime UTC, début de la minute (floor "1min")
        open/high/low/close : float, prix
        volume      : int, total contrats échangés
        n_ticks     : int, nombre de trades observés (diagnostic)
        closed      : bool, True si le bar est figé (minute passée)
        source      : "WS" (stream) | "REST" (gap-fill au reconnect)
    """
    contract_id: str
    start_ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    n_ticks: int = 0
    closed: bool = False
    source: str = "WS"

    @property
    def end_ts(self) -> datetime:
        return self.start_ts + timedelta(minutes=1)

    def as_dict(self) -> dict:
        """Pour sérialisation / debugging."""
        return {
            "contract_id": self.contract_id,
            "start_ts":    self.start_ts.isoformat(),
            "open":        self.open,
            "high":        self.high,
            "low":         self.low,
            "close":       self.close,
            "volume":      self.volume,
            "n_ticks":     self.n_ticks,
            "closed":      self.closed,
            "source":      self.source,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _floor_minute(ts: datetime) -> datetime:
    """Tronque un datetime UTC au début de sa minute."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    else:
        ts = ts.astimezone(timezone.utc)
    return ts.replace(second=0, microsecond=0)


# ─────────────────────────────────────────────────────────────────────────────
# M1Buffer
# ─────────────────────────────────────────────────────────────────────────────

class M1Buffer:
    """
    Buffer M1 in-memory par contract_id.

    Usage :
        buf = M1Buffer(max_minutes=120)
        for evt in rt.drain_events():
            buf.consume(evt)
        bars = buf.get_recent_bars("CON.F.US.MNQ.M26", n=15)
        current = buf.get_current_forming_bar("CON.F.US.MNQ.M26")

    Le buffer est sans état persistant : à chaque démarrage, il commence vide.
    Le gap-fill REST au reconnect (cf. docstring module) est de la responsabilité
    du caller.
    """

    def __init__(self, max_minutes: int = 120):
        if max_minutes < 2:
            raise ValueError("max_minutes doit être >= 2")
        self._max_minutes = int(max_minutes)
        self._lock = threading.RLock()

        # Par contract_id : deque[M1Bar fermés] + bar courant
        self._closed_bars: Dict[str, Deque[M1Bar]] = {}
        self._current_bar: Dict[str, M1Bar] = {}

        # Diagnostic
        self._trades_consumed   = 0
        self._trades_dropped    = 0  # ts antérieur au bar courant déjà fermé
        self._bars_closed       = 0
        self._last_event_ts: Optional[datetime] = None

    # ─────────────────────────────────────────────────────────────────────
    # Ingestion
    # ─────────────────────────────────────────────────────────────────────

    def consume(self, evt: MarketEvent) -> None:
        """
        Ingère un MarketEvent. Ignore tout sauf kind="trade" avec price/volume.

        Comportement :
        - Trade dans la minute du bar courant → update OHLCV
        - Trade dans une minute postérieure → close bar courant + ouvre nouveau
        - Trade plus ancien que le bar courant (out-of-order) → drop +
          incrémente `trades_dropped` (diagnostic)
        """
        if evt.kind != "trade":
            return
        if evt.price is None or evt.volume is None:
            return

        # Choisir le timestamp : ts_exchange en priorité, fallback received_at
        raw_ts = evt.ts_exchange or evt.received_at
        if raw_ts is None:
            return
        bucket = _floor_minute(raw_ts)
        cid = evt.contract_id

        with self._lock:
            self._last_event_ts = raw_ts
            self._trades_consumed += 1

            current = self._current_bar.get(cid)

            # Cas 1 : pas de bar courant → ouvrir
            if current is None:
                self._current_bar[cid] = M1Bar(
                    contract_id=cid,
                    start_ts=bucket,
                    open=float(evt.price),
                    high=float(evt.price),
                    low=float(evt.price),
                    close=float(evt.price),
                    volume=int(evt.volume),
                    n_ticks=1,
                    closed=False,
                    source="WS",
                )
                return

            # Cas 2 : trade dans le bucket courant → update OHLCV
            if bucket == current.start_ts:
                price = float(evt.price)
                current.high = max(current.high, price)
                current.low  = min(current.low, price)
                current.close = price
                current.volume += int(evt.volume)
                current.n_ticks += 1
                return

            # Cas 3 : trade dans une minute postérieure → close + nouveau bar
            if bucket > current.start_ts:
                # Close bar(s) intermédiaire(s) — il peut y avoir des trous
                # (minutes sans aucun trade). On flush juste le bar courant ;
                # les minutes vides ne sont PAS créées (intentionnel — match
                # le comportement REST `get_bars` qui ne renvoie pas de bars
                # vides).
                self._close_current_bar(cid)
                self._current_bar[cid] = M1Bar(
                    contract_id=cid,
                    start_ts=bucket,
                    open=float(evt.price),
                    high=float(evt.price),
                    low=float(evt.price),
                    close=float(evt.price),
                    volume=int(evt.volume),
                    n_ticks=1,
                    closed=False,
                    source="WS",
                )
                return

            # Cas 4 : trade out-of-order (antérieur au bar courant) → drop
            self._trades_dropped += 1

    def flush_stale_bars(self, now_utc: Optional[datetime] = None) -> int:
        """
        Force la clôture du bar courant si sa minute est terminée.

        À appeler périodiquement par le runner (ex: à chaque tick 30s) pour
        garantir que les bars sont disponibles à la lecture même en l'absence
        de nouveau trade qui aurait déclenché la rotation automatique.

        Retourne le nombre de bars clôturés.
        """
        now = now_utc or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        current_bucket = _floor_minute(now)

        n_closed = 0
        with self._lock:
            for cid, bar in list(self._current_bar.items()):
                if bar.start_ts < current_bucket:
                    self._close_current_bar(cid)
                    n_closed += 1
        return n_closed

    def inject_bars(self, bars: List[M1Bar]) -> int:
        """
        Injecte des bars (typiquement issus d'un gap-fill REST au reconnect).

        Les bars injectés sont marqués `source="REST"`, `closed=True`, et
        insérés en place dans la deque historique. Si un bar avec le même
        start_ts existe déjà (issu du WS), il est CONSERVÉ (priorité au stream
        live) et le bar REST est ignoré. Retourne le nombre de bars ajoutés.
        """
        added = 0
        with self._lock:
            for src in bars:
                cid = src.contract_id
                dq = self._closed_bars.setdefault(
                    cid, deque(maxlen=self._max_minutes)
                )
                # Skip si déjà présent (priorité au WS)
                if any(b.start_ts == src.start_ts for b in dq):
                    continue
                # Marquer comme REST et closed
                injected = replace(src, source="REST", closed=True)
                dq.append(injected)
                added += 1
            # Re-trier par start_ts pour rester monotonic (rare mais possible
            # si gap-fill arrive après quelques bars WS)
            for cid in {b.contract_id for b in bars}:
                dq = self._closed_bars.get(cid)
                if dq is not None:
                    sorted_list = sorted(dq, key=lambda b: b.start_ts)
                    dq.clear()
                    dq.extend(sorted_list)
        return added

    # ─────────────────────────────────────────────────────────────────────
    # Lecture
    # ─────────────────────────────────────────────────────────────────────

    def get_current_forming_bar(self, contract_id: str) -> Optional[M1Bar]:
        """Bar en cours de formation (closed=False). None si aucun trade vu."""
        with self._lock:
            bar = self._current_bar.get(contract_id)
            return replace(bar) if bar is not None else None

    def get_recent_bars(self, contract_id: str, n: int = 30,
                        include_forming: bool = False) -> List[M1Bar]:
        """
        Retourne les `n` derniers bars (closed) du plus ancien au plus récent.

        Si `include_forming=True` et qu'un bar courant existe, il est ajouté en
        fin de liste avec `closed=False`.
        """
        with self._lock:
            dq = self._closed_bars.get(contract_id, deque())
            out = list(dq)[-n:] if n > 0 else list(dq)
            out = [replace(b) for b in out]  # copies défensives
            if include_forming:
                cur = self._current_bar.get(contract_id)
                if cur is not None:
                    out.append(replace(cur))
            return out

    def get_bars_since(self, contract_id: str, since_utc: datetime,
                       include_forming: bool = False) -> List[M1Bar]:
        """Bars dont start_ts >= since_utc (croissant)."""
        if since_utc.tzinfo is None:
            since_utc = since_utc.replace(tzinfo=timezone.utc)
        with self._lock:
            dq = self._closed_bars.get(contract_id, deque())
            out = [replace(b) for b in dq if b.start_ts >= since_utc]
            if include_forming:
                cur = self._current_bar.get(contract_id)
                if cur is not None and cur.start_ts >= since_utc:
                    out.append(replace(cur))
            return out

    def contracts(self) -> List[str]:
        """Liste des contract_ids actuellement observés (closed ou en cours)."""
        with self._lock:
            return sorted(
                set(self._closed_bars.keys()) | set(self._current_bar.keys())
            )

    def health(self) -> dict:
        """Snapshot santé pour monitoring."""
        with self._lock:
            return {
                "contracts":         self.contracts(),
                "closed_bars":       {cid: len(dq) for cid, dq in self._closed_bars.items()},
                "has_forming":       {cid: True for cid in self._current_bar.keys()},
                "trades_consumed":   self._trades_consumed,
                "trades_dropped":    self._trades_dropped,
                "bars_closed":       self._bars_closed,
                "last_event_ts":     self._last_event_ts.isoformat() if self._last_event_ts else None,
                "max_minutes":       self._max_minutes,
            }

    # ─────────────────────────────────────────────────────────────────────
    # Interne
    # ─────────────────────────────────────────────────────────────────────

    def _close_current_bar(self, cid: str) -> None:
        """Marque le bar courant comme closed et le push dans la deque."""
        bar = self._current_bar.pop(cid, None)
        if bar is None:
            return
        bar.closed = True
        dq = self._closed_bars.setdefault(
            cid, deque(maxlen=self._max_minutes)
        )
        dq.append(bar)
        self._bars_closed += 1
