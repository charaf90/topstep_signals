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

        Un trade broker arrive en 2 entries : OPENING (profitAndLoss=None) et
        CLOSING (profitAndLoss=valeur). On accumule les frais des deux mais on
        ne crée un POINT sur la courbe que sur le closing — sinon la courbe
        descend artificiellement quand un trade est ouvert (cum_fees augmente
        avant que cum_gross n'augmente), créant des "dents" trompeuses.

        Retourne {timestamps, equity_net, equity_gross, total_fees, n_trades}
        où n_trades = nombre de TRADES CLOS (round-trips), pas d'entries broker.
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
        n_closing = 0
        for t in sorted_trades:
            pnl = t.get("profitAndLoss")
            fees = float(t.get("fees", 0) or 0)
            comm = float(t.get("commissions", 0) or 0)
            # On accumule TOUJOURS les frais (opening + closing en payent chacun)
            cum_fees += fees + comm
            if pnl is None:
                continue  # OPENING : pas de point sur la courbe
            # CLOSING : un point à ce timestamp
            cum_gross += float(pnl)
            n_closing += 1
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
            "n_trades": n_closing,  # round-trips, pas d'entries
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

    def paired_trades(self, days_back: int = 60) -> list[dict]:
        """Apparie les OPENING + CLOSING par contractId en LIFO temporel.

        Retourne une liste de trades complets [{opening, closing, direction,
        n_ct, entry_price, exit_price, pnl_net, pnl_gross, fees}, ...].

        Hypothèse : pour un même contractId, l'OPENING le plus récent est le
        premier à se fermer (LIFO). Pour 1 broker open + 1 broker close c'est
        évident ; pour des positions partielles ça peut être inexact mais le
        broker ProjectX ne donne pas explicitement le mapping.
        """
        trades = self.trades_history(days_back=days_back)
        if not trades:
            return []
        sorted_trades = sorted(trades, key=lambda t: t.get("creationTimestamp", ""))
        open_stack: dict[str, list[dict]] = {}  # contractId → openings non closés
        pairs: list[dict] = []
        for t in sorted_trades:
            cid = t.get("contractId", "?")
            if t.get("profitAndLoss") is None:
                open_stack.setdefault(cid, []).append(t)
                continue
            # CLOSING : dépile en LIFO
            opens = open_stack.get(cid, [])
            opening = opens.pop() if opens else None
            fees = float(t.get("fees", 0) or 0) + float(t.get("commissions", 0) or 0)
            opening_fees = (
                (float(opening.get("fees", 0) or 0) + float(opening.get("commissions", 0) or 0))
                if opening
                else 0.0
            )
            pnl_gross = float(t["profitAndLoss"])
            # Direction = celle de l'opening (BUY → LONG, SELL → SHORT)
            if opening is not None:
                direction = "LONG" if opening.get("side") == 0 else "SHORT"
                entry_price = opening.get("price")
                n_ct = opening.get("size") or t.get("size", 0)
            else:
                # Pas d'opening matché — on déduit de l'inverse du closing side
                direction = "SHORT" if t.get("side") == 0 else "LONG"
                entry_price = None
                n_ct = t.get("size", 0)
            pairs.append(
                {
                    "opening": opening,
                    "closing": t,
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

    def daily_net_pnl(self, days_back: int = 60) -> dict[str, float]:
        """P&L NET par jour (date du closing trade) — source de vérité broker.

        Utilisé pour le « best day » (consistency) et le peak : on agrège les
        ``profitAndLoss`` des closing trades moins frais+commissions, groupés par
        date de ``creationTimestamp``. Évite l'incohérence du state local (brut,
        sans frais, sujet aux doublons d'enregistrement).
        """
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


# Singleton — un seul login par process
_broker_instance: BrokerData | None = None


def get_broker() -> BrokerData:
    global _broker_instance
    if _broker_instance is None:
        _broker_instance = BrokerData()
    return _broker_instance
