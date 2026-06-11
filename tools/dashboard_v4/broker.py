"""Broker data v4 — source de vérité ProjectX, une instance PAR COMPTE.

Reprend le cache TTL + l'appariement LIFO du v3, avec trois évolutions :
- instances par ``AccountConfig`` (fin du singleton ``accounts[0]`` aveugle) ;
- ``clear_cache()`` pour le bouton « Rafraîchir broker » de l'onglet Sys ;
- ``day_net_futures()`` : P&L du JOUR DE TRADING futures (pivot 23:00 UTC,
  comme tools/account_status.py) au lieu du jour UTC calendaire.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.dashboard_v4.accounts import AccountConfig  # noqa: E402

_log = logging.getLogger("broker_v4")

# Charge .env si pas déjà fait (mêmes clés que le daemon)
_env = ROOT / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())


def trading_day_start(now: datetime | None = None) -> datetime:
    """Début du jour de trading futures : 23:00 UTC (17:00 CT, reset Topstep)."""
    now = now or datetime.now(UTC)
    anchor = now.replace(hour=23, minute=0, second=0, microsecond=0)
    return anchor if now >= anchor else anchor - timedelta(days=1)


class BrokerData:
    """Wrapper ProjectXClient avec cache TTL, lié à UN AccountConfig."""

    CACHE_TTL = 30  # secondes — aligné sur le refresh dashboard

    def __init__(self, acc: AccountConfig):
        self.acc = acc
        self._client = None
        self._account: dict | None = None
        self._cache: dict[str, tuple[float, Any]] = {}
        self._last_error: str | None = None

    # ── lazy client ─────────────────────────────────────────────────────────

    @property
    def client(self):
        if self._client is None:
            self._login()
        return self._client

    def _login(self) -> bool:
        try:
            from broker.projectx_client import ProjectXClient  # noqa: PLC0415

            user = os.environ.get(f"{self.acc.env_prefix}_USERNAME", "").strip()
            key = os.environ.get(f"{self.acc.env_prefix}_API_KEY", "").strip()
            if not user or not key:
                self._last_error = f"Crédentials {self.acc.env_prefix}_* absents (.env)"
                _log.warning(self._last_error)
                return False
            c = ProjectXClient(user, key)
            if not c.login():
                self._last_error = "Échec login ProjectX"
                _log.error(self._last_error)
                return False
            self._client = c
            return True
        except Exception as exc:
            self._last_error = f"Login ProjectX exception : {exc}"
            _log.error(self._last_error, exc_info=True)
            return False

    @property
    def is_connected(self) -> bool:
        return self._client is not None

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def clear_cache(self) -> None:
        """Invalide tout le cache (bouton « Rafraîchir broker »)."""
        self._cache.clear()

    # ── cache helper ────────────────────────────────────────────────────────

    def _cached(self, key: str, fn: Callable[[], Any], ttl: int | None = None) -> Any:
        ttl = ttl or self.CACHE_TTL
        now = time.time()
        if key in self._cache:
            t, v = self._cache[key]
            if now - t < ttl:
                return v
        try:
            v = fn()
            self._cache[key] = (now, v)
            return v
        except Exception as exc:
            self._last_error = f"{key}: {exc}"
            _log.error("Broker API error %s : %s", key, exc, exc_info=True)
            if key in self._cache:  # cache stale plutôt que rien
                return self._cache[key][1]
            return None

    # ── API publique ───────────────────────────────────────────────────────

    def account_summary(self) -> dict | None:
        """Compte ciblé par ``acc.broker_account_id`` (ou 1er compte actif)."""
        if self.client is None:
            return None
        accounts = self._cached("accounts", lambda: self.client.get_accounts(only_active=True))
        if not accounts:
            return None
        a = None
        if self.acc.broker_account_id is not None:
            a = next((x for x in accounts if x.get("id") == self.acc.broker_account_id), None)
        if a is None:
            a = accounts[0]
        self._account = a
        balance = float(a.get("balance", 0.0))
        starting = self.acc.account_size
        return {
            "id": a["id"],
            "name": a.get("name", "?"),
            "balance": balance,
            "starting_balance": starting,
            "cum_pnl_net": balance - starting,
            "can_trade": bool(a.get("canTrade", False)),
            "simulated": bool(a.get("simulated", True)),
        }

    def positions(self) -> list[dict]:
        if self.client is None or not self._account:
            self.account_summary()
        if not self._account:
            return []
        return (
            self._cached("positions", lambda: self.client.get_positions(self._account["id"])) or []
        )

    def open_orders(self) -> list[dict]:
        if not self._account:
            self.account_summary()
        if not self._account:
            return []
        return (
            self._cached("open_orders", lambda: self.client.get_open_orders(self._account["id"]))
            or []
        )

    def trades_history(self, days_back: int = 60) -> list[dict]:
        """Tous les trades exécutés sur les N derniers jours (cache 5 min)."""
        if not self._account:
            self.account_summary()
        if not self._account:
            return []
        start = datetime.now(UTC) - timedelta(days=days_back)
        return (
            self._cached(
                f"trades_{days_back}d",
                lambda: self.client.get_trades_since(self._account["id"], start),
                ttl=300,
            )
            or []
        )

    def paired_trades(self, days_back: int = 60) -> list[dict]:
        """Apparie OPENING + CLOSING par contractId en LIFO temporel (cf. v3).

        Hypothèse LIFO : pour un même contractId, l'opening le plus récent se
        ferme en premier — exact pour 1 open / 1 close, approximatif pour des
        partiels (ProjectX ne donne pas le mapping explicite).
        """
        trades = self.trades_history(days_back=days_back)
        if not trades:
            return []
        sorted_trades = sorted(trades, key=lambda t: t.get("creationTimestamp", ""))
        open_stack: dict[str, list[dict]] = {}
        pairs: list[dict] = []
        for t in sorted_trades:
            cid = t.get("contractId", "?")
            if t.get("profitAndLoss") is None:
                open_stack.setdefault(cid, []).append(t)
                continue
            opens = open_stack.get(cid, [])
            opening = opens.pop() if opens else None
            fees = float(t.get("fees", 0) or 0) + float(t.get("commissions", 0) or 0)
            opening_fees = (
                (float(opening.get("fees", 0) or 0) + float(opening.get("commissions", 0) or 0))
                if opening
                else 0.0
            )
            pnl_gross = float(t["profitAndLoss"])
            if opening is not None:
                direction = "LONG" if opening.get("side") == 0 else "SHORT"
                entry_price = opening.get("price")
                n_ct = opening.get("size") or t.get("size", 0)
            else:
                direction = "SHORT" if t.get("side") == 0 else "LONG"
                entry_price = None
                n_ct = t.get("size", 0)
            pairs.append(
                {
                    "contract_id": cid,
                    "direction": direction,
                    "n_ct": int(n_ct),
                    "entry_price": entry_price,
                    "exit_price": t.get("price"),
                    "pnl_gross": pnl_gross,
                    "fees": fees + opening_fees,
                    "pnl_net": pnl_gross - fees - opening_fees,
                    "open_time": opening.get("creationTimestamp") if opening else None,
                    "close_time": t.get("creationTimestamp"),
                    "order_id_open": opening.get("orderId") if opening else None,
                    "order_id_close": t.get("orderId"),
                }
            )
        return pairs

    def day_net_futures(self) -> dict:
        """P&L net du JOUR DE TRADING futures courant (pivot 23:00 UTC).

        Même définition que tools/account_status.py — évite l'incohérence du
        jour UTC calendaire qui coupe la session du soir en deux.
        """
        start = trading_day_start()
        trades = self.trades_history(days_back=3)
        day_trades = [
            t
            for t in trades
            if not t.get("voided")
            and (t.get("creationTimestamp") or "") >= start.strftime("%Y-%m-%dT%H:%M:%S")
        ]
        gross = sum(float(t.get("profitAndLoss") or 0) for t in day_trades)
        fees = sum(
            float(t.get("fees", 0) or 0) + float(t.get("commissions", 0) or 0) for t in day_trades
        )
        n_closing = sum(1 for t in day_trades if t.get("profitAndLoss") is not None)
        return {
            "day_start": start.isoformat(),
            "pnl_gross": round(gross, 2),
            "fees": round(fees, 2),
            "pnl_net": round(gross - fees, 2),
            "n_closing": n_closing,
        }

    def daily_net_pnl(self, days_back: int = 60) -> dict[str, float]:
        """P&L NET par jour calendaire UTC (date du closing trade)."""
        trades = self.trades_history(days_back=days_back)
        out: dict[str, float] = {}
        for t in trades:
            pnl = t.get("profitAndLoss")
            if pnl is None:
                continue
            day = (t.get("creationTimestamp") or "")[:10]
            if not day:
                continue
            fees = float(t.get("fees", 0) or 0) + float(t.get("commissions", 0) or 0)
            out[day] = round(out.get(day, 0.0) + float(pnl) - fees, 2)
        return out


# ── instances par compte ─────────────────────────────────────────────────────
_instances: dict[str, BrokerData] = {}


def get_broker(acc: AccountConfig) -> BrokerData:
    if acc.key not in _instances:
        _instances[acc.key] = BrokerData(acc)
    return _instances[acc.key]
