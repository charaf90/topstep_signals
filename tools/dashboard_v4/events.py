"""Parser de logs/trading_events.log — funnel signaux par stratégie.

Format d'une ligne (cf. core/event_logger.py) :
    ``YYYY-MM-DD HH:MM:SS UTC  [TYPE    ]  message``

Types : SIGNAL · ORDRE · FILL · CLÔTURE · ANNULÉ · BLOQUÉ · SESSION · INFO ·
ERROR · SIZING. Gisement inexploité par le v3 : on en tire le funnel
SIGNAL → ORDRE → FILL / ANNULÉ / BLOQUÉ par stratégie (taux de not-filled).

Parse incrémental : on mémorise (taille, événements) par fichier et on ne
relit que la queue ajoutée ; re-parse complet seulement si le fichier a
rétréci (rotation). Lecture seule absolue.
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.dashboard_v4.datasource import (  # noqa: E402
    STRATEGY_KEYS,
    infer_strategy_from_tag,
    normalize_strategy,
)

_LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC\s+\[(\S+)\s*\]\s+(.*)$")
# SIGNAL : "[MES1] FIB_FINE LONG @ 7311.0  —  sl=…"
_SIGNAL_RE = re.compile(r"^\[(\w+)\]\s+(\S+)\s+(LONG|SHORT)\b")
# Tag dans ORDRE/FILL/CLÔTURE/ANNULÉ/BLOQUÉ : "FIBFINE_MES1_20260611_long_…"
_TAG_RE = re.compile(r"\b((?:OPR|FIB\w*|BOS\w*|VPC)\w*_\w+_\d{8}\S*)")

# cache module-level : path → (size, events)
_cache: dict[str, tuple[int, list[dict]]] = {}


def _parse_lines(text: str) -> list[dict]:
    events = []
    for line in text.splitlines():
        m = _LINE_RE.match(line)
        if not m:
            continue
        ts, level, msg = m.groups()
        strategy = ""
        tag = ""
        if level == "SIGNAL":
            sm = _SIGNAL_RE.match(msg)
            if sm:
                strategy = normalize_strategy(sm.group(2))
        else:
            tm = _TAG_RE.search(msg)
            if tm:
                tag = tm.group(1)
                strategy = infer_strategy_from_tag(tag)
        events.append({"ts": ts, "level": level, "msg": msg, "strategy": strategy, "tag": tag})
    return events


def read_events(path: Path) -> list[dict]:
    """Tous les événements du log, avec parse incrémental."""
    key = str(path)
    try:
        size = path.stat().st_size
    except OSError:
        return []
    cached = _cache.get(key)
    if cached:
        old_size, old_events = cached
        if size == old_size:
            return old_events
        if size > old_size:
            try:
                with path.open("rb") as f:
                    f.seek(old_size)
                    new_text = f.read().decode("utf-8", errors="replace")
                events = old_events + _parse_lines(new_text)
                _cache[key] = (size, events)
                return events
            except OSError:
                return old_events
        # fichier a rétréci (rotation) → re-parse complet
    try:
        events = _parse_lines(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return []
    _cache[key] = (size, events)
    return events


def funnel_stats(events: list[dict], since: str | None = None) -> dict[str, dict]:
    """Funnel par stratégie : signaux → ordres → fills / annulés / bloqués.

    ``since`` : date ISO "YYYY-MM-DD" optionnelle (borne basse incluse).
    ``fill_rate`` = fills / ordres ; ``not_filled_rate`` = annulés / ordres.

    Déduplication par TAG unique pour ORDRE/FILL/ANNULÉ/BLOQUÉ : un même tag
    peut être loggé plusieurs fois (réconciliations, restarts du daemon) —
    compter les lignes brutes donnait des taux > 1. SIGNAL n'a pas de tag →
    compte brut.
    """
    signals: dict[str, int] = {k: 0 for k in STRATEGY_KEYS}
    tag_sets: dict[str, dict[str, set]] = {k: defaultdict(set) for k in STRATEGY_KEYS}
    level_field = {
        "ORDRE": "orders",
        "FILL": "fills",
        "ANNULÉ": "cancelled",
        "BLOQUÉ": "blocked",
    }
    for ev in events:
        if since and ev["ts"][:10] < since:
            continue
        strat = ev["strategy"]
        if strat not in STRATEGY_KEYS:
            continue
        if ev["level"] == "SIGNAL":
            signals[strat] += 1
            continue
        field = level_field.get(ev["level"])
        if field and ev["tag"]:
            tag_sets[strat][field].add(ev["tag"])
    out: dict[str, dict] = {}
    for strat in STRATEGY_KEYS:
        c = {f: len(s) for f, s in tag_sets[strat].items()}
        orders = c.get("orders", 0)
        fills = c.get("fills", 0)
        cancelled = c.get("cancelled", 0)
        out[strat] = {
            "signals": signals[strat],
            "orders": orders,
            "fills": fills,
            "cancelled": cancelled,
            "blocked": c.get("blocked", 0),
            "fill_rate": min(1.0, fills / orders) if orders else None,
            "not_filled_rate": min(1.0, cancelled / orders) if orders else None,
        }
    return out


def last_error(events: list[dict]) -> dict | None:
    for ev in reversed(events):
        if ev["level"] == "ERROR":
            return ev
    return None


def last_ws_event_ts(events: list[dict]) -> str | None:
    """Timestamp du dernier événement, quel qu'il soit (proxy fraîcheur flux)."""
    return events[-1]["ts"] if events else None


def events_today(events: list[dict], day: str) -> list[dict]:
    return [e for e in events if e["ts"][:10] == day]
