"""Broker data — source de vérité ProjectX API.

L'utilisateur a soulevé : le state local est stale, les chiffres ne
collent pas avec le broker. Solution : lire directement l'API broker
pour balance / positions / trades historiques (avec fees + commissions
exacts).

Cache TTL court (30s) pour ne pas spammer l'API à chaque render Dash.
Login lazy au premier appel, re-auth automatique si token expire (géré
par ProjectXClient._maybe_reauth).

Architecture :
- BrokerData singleton (un seul login par process)
- Méthodes cachées : account_summary, positions, open_orders, trades_history
- equity_from_broker : reconstruit la courbe à partir des trades API
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

_log = logging.getLogger("broker_data")

# Charge .env si pas déjà fait
_env = ROOT / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())


class BrokerData:
    """Singleton wrapper autour de ProjectXClient avec cache TTL."""

    CACHE_TTL = 30  # secondes — équivalent au refresh dashboard

    def __init__(self):
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

            user = os.environ.get("PROJECTX_USERNAME", "").strip()
            key = os.environ.get("PROJECTX_API_KEY", "").strip()
            if not user or not key:
                self._last_error = "Crédentials ProjectX absents (.env)"
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
            # Retourne le cache stale si dispo
            if key in self._cache:
                return self._cache[key][1]
            return None

    # ── API publique ───────────────────────────────────────────────────────

    def account_summary(self) -> dict | None:
        """Retourne le 1er compte actif avec balance, canTrade, simulated."""
        if self.client is None:
            return None
        accounts = self._cached("accounts", lambda: self.client.get_accounts(only_active=True))
        if not accounts:
            return None
        a = accounts[0]
        self._account = a
        # Starting balance déduit : pour Combine 50K, balance - 50000 = P&L net
        starting = self._guess_starting_balance(a)
        balance = float(a.get("balance", 0.0))
        return {
            "id": a["id"],
            "name": a.get("name", "?"),
            "balance": balance,
            "starting_balance": starting,
            "cum_pnl_net": balance - starting,
            "can_trade": bool(a.get("canTrade", False)),
            "simulated": bool(a.get("simulated", True)),
        }

    def _guess_starting_balance(self, account: dict) -> float:
        """Devine le starting balance depuis le nom du compte (Combine size)."""
        name = (account.get("name") or "").upper()
        for size in (50, 100, 150, 250, 500):
            if f"{size}KTC" in name or f"{size}K-" in name or f"{size}K " in name:
                return size * 1000.0
        return 50000.0  # fallback Combine 50K

    def positions(self) -> list[dict]:
        if self.client is None or not self._account:
            self.account_summary()  # force account fetch
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
        """Retourne tous les trades exécutés sur les N derniers jours."""
        if not self._account:
            self.account_summary()
        if not self._account:
            return []
        start = datetime.now(UTC) - timedelta(days=days_back)
        # Cache plus long pour l'historique (5 min) — il bouge peu
        return (
            self._cached(
                f"trades_{days_back}d",
                lambda: self.client.get_trades_since(self._account["id"], start),
                ttl=300,
            )
            or []
        )

    def equity_curve_from_trades(self, days_back: int = 60) -> dict:
        """Reconstruit l'equity curve à partir des trades broker.

        Retourne {timestamps, equity_net, equity_gross, total_fees, total_pnl_net}.

        - equity_gross = cumul `profitAndLoss` (somme profits réalisés bruts)
        - equity_net   = equity_gross - cumul (fees + commissions)
        """
        trades = self.trades_history(days_back=days_back)
        if not trades:
            return {
                "timestamps": [],
                "equity_net": [],
                "equity_gross": [],
                "total_fees": 0.0,
                "total_pnl_gross": 0.0,
                "total_pnl_net": 0.0,
                "n_trades": 0,
            }

        # Tri par creationTimestamp
        sorted_trades = sorted(trades, key=lambda t: t.get("creationTimestamp", ""))

        timestamps, eq_gross, eq_net = [], [], []
        cum_gross = 0.0
        cum_fees = 0.0
        for t in sorted_trades:
            pnl = t.get("profitAndLoss")
            fees = float(t.get("fees", 0) or 0)
            comm = float(t.get("commissions", 0) or 0)
            cum_fees += fees + comm
            if pnl is not None:
                cum_gross += float(pnl)
            timestamps.append(t.get("creationTimestamp", ""))
            eq_gross.append(round(cum_gross, 2))
            eq_net.append(round(cum_gross - cum_fees, 2))

        return {
            "timestamps": timestamps,
            "equity_net": eq_net,
            "equity_gross": eq_gross,
            "total_fees": round(cum_fees, 2),
            "total_pnl_gross": round(cum_gross, 2),
            "total_pnl_net": round(cum_gross - cum_fees, 2),
            "n_trades": len(sorted_trades),
        }

    def trades_aggregated_by_contract(self, days_back: int = 60) -> dict[str, dict]:
        """Stats trades par contractId (équivalent strategy_stats côté broker).

        Note : on n'a pas la "stratégie" dans les trades broker, juste le
        contractId. On agrège donc par contrat.
        """
        trades = self.trades_history(days_back=days_back)
        by_contract: dict[str, dict] = {}
        for t in trades:
            cid = t.get("contractId", "?")
            pnl = t.get("profitAndLoss")
            fees = float(t.get("fees", 0) or 0)
            comm = float(t.get("commissions", 0) or 0)
            if cid not in by_contract:
                by_contract[cid] = {
                    "n": 0,
                    "pnl_gross": 0.0,
                    "pnl_net": 0.0,
                    "fees": 0.0,
                    "wins": 0,
                    "losses": 0,
                }
            d = by_contract[cid]
            d["fees"] += fees + comm
            if pnl is not None:
                d["n"] += 1
                d["pnl_gross"] += pnl
                d["pnl_net"] += pnl - fees - comm
                if pnl > 0:
                    d["wins"] += 1
                elif pnl < 0:
                    d["losses"] += 1
        # Compute derived
        for cid, d in by_contract.items():
            d["wr_pct"] = (d["wins"] / d["n"] * 100) if d["n"] else 0.0
            d["avg_pnl_net"] = (d["pnl_net"] / d["n"]) if d["n"] else 0.0
        return by_contract

    def realized_day_pnl(self) -> dict:
        """P&L réalisé du jour UTC actuel (somme profitAndLoss + frais des trades aujourd'hui)."""
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        trades = self.trades_history(days_back=2)
        day_trades = [t for t in trades if (t.get("creationTimestamp") or "").startswith(today)]
        gross = sum(float(t.get("profitAndLoss") or 0) for t in day_trades)
        fees = sum(
            float(t.get("fees", 0) or 0) + float(t.get("commissions", 0) or 0) for t in day_trades
        )
        # n trades fillés (closing trades sont ceux avec profitAndLoss non-None)
        n_closing = sum(1 for t in day_trades if t.get("profitAndLoss") is not None)
        n_total = len(day_trades)
        return {
            "today": today,
            "pnl_gross": round(gross, 2),
            "fees": round(fees, 2),
            "pnl_net": round(gross - fees, 2),
            "n_closing": n_closing,
            "n_total": n_total,
        }


# Singleton — un seul login par process
_broker_instance: BrokerData | None = None


def get_broker() -> BrokerData:
    global _broker_instance
    if _broker_instance is None:
        _broker_instance = BrokerData()
    return _broker_instance
