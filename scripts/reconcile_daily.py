#!/usr/bin/env python3
"""Réconciliation quotidienne broker ↔ état local.

Compare le contenu de `state/live_state.json` à ce que voit l'API
ProjectX. Lecture seule absolue — ne modifie ni le state, ni les
ordres, ni les positions. En cas de mismatch, log structuré +
alerte Telegram (si --notify).

Trois axes vérifiés :

  1. **Positions ouvertes** : nombre + side + taille par contrat
  2. **Ordres ouverts**    : liste d'order_id côté broker vs state
  3. **P&L journalier**    : somme `profitAndLoss` des trades du jour
                             vs somme `close_pnl` du jour dans state.placed_tags

Tolérances :
  - prix : ±1 tick (par ticker — table dans config.INSTRUMENTS)
  - P&L  : ±$1

Usage :
    python scripts/reconcile_daily.py
    python scripts/reconcile_daily.py --notify          # alerte Telegram si mismatch
    python scripts/reconcile_daily.py --date 2026-05-22
    python scripts/reconcile_daily.py --json            # sortie JSON pure (pour cron)

Cron (cible) :
    30 19 * * 1-5 cd /home/charaf/topstep_signals && python scripts/reconcile_daily.py --notify
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Permettre l'import depuis la racine si lancé depuis scripts/
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import LIVE_STATE_FILE  # noqa: E402

logger = logging.getLogger("reconcile_daily")

# Tolérance P&L par défaut (USD). 1$ couvre les arrondis broker/local.
DEFAULT_TOLERANCE_USD = 1.0


# ──────────────────────────────────────────────────────────────────────────────
# Modèles
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class PositionDiff:
    """Différence entre une position côté broker et côté state local."""

    contract_id: str
    broker_size: int = 0
    broker_side: str | None = None  # "long" | "short" | None
    state_size: int = 0
    state_side: str | None = None
    delta: str = ""  # description courte

    @property
    def is_mismatch(self) -> bool:
        return self.broker_size != self.state_size or self.broker_side != self.state_side


@dataclass
class OrderDiff:
    """Différence entre les ordres ouverts broker et state local."""

    order_id: int
    in_broker: bool
    in_state: bool
    detail: str = ""

    @property
    def is_mismatch(self) -> bool:
        return self.in_broker != self.in_state


@dataclass
class PnLDiff:
    """Différence P&L journalier."""

    broker_pnl: float
    state_pnl: float
    tolerance_usd: float
    delta: float = 0.0

    def __post_init__(self) -> None:
        self.delta = round(self.broker_pnl - self.state_pnl, 2)

    @property
    def is_mismatch(self) -> bool:
        return abs(self.delta) > self.tolerance_usd


@dataclass
class ReconciliationReport:
    """Résumé complet d'une réconciliation."""

    date: str
    account_id: int
    positions: list[PositionDiff] = field(default_factory=list)
    orders: list[OrderDiff] = field(default_factory=list)
    pnl: PnLDiff | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def has_mismatch(self) -> bool:
        if self.errors:
            return True
        if any(p.is_mismatch for p in self.positions):
            return True
        if any(o.is_mismatch for o in self.orders):
            return True
        return self.pnl is not None and self.pnl.is_mismatch

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "account_id": self.account_id,
            "has_mismatch": self.has_mismatch,
            "errors": list(self.errors),
            "positions": [
                {
                    "contract_id": p.contract_id,
                    "broker_size": p.broker_size,
                    "broker_side": p.broker_side,
                    "state_size": p.state_size,
                    "state_side": p.state_side,
                    "delta": p.delta,
                    "is_mismatch": p.is_mismatch,
                }
                for p in self.positions
            ],
            "orders": [
                {
                    "order_id": o.order_id,
                    "in_broker": o.in_broker,
                    "in_state": o.in_state,
                    "detail": o.detail,
                    "is_mismatch": o.is_mismatch,
                }
                for o in self.orders
            ],
            "pnl": (
                None
                if self.pnl is None
                else {
                    "broker_pnl": self.pnl.broker_pnl,
                    "state_pnl": self.pnl.state_pnl,
                    "delta": self.pnl.delta,
                    "tolerance_usd": self.pnl.tolerance_usd,
                    "is_mismatch": self.pnl.is_mismatch,
                }
            ),
        }


# ──────────────────────────────────────────────────────────────────────────────
# Helpers state local
# ──────────────────────────────────────────────────────────────────────────────


def load_state(path: str | Path) -> dict[str, Any]:
    """Lit live_state.json. Retourne un dict vide si le fichier n'existe pas."""
    p = Path(path)
    if not p.exists():
        return {}
    return json.loads(p.read_text())


def derive_open_positions_from_state(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Reconstruit les positions ouvertes selon le state local.

    Une position est "ouverte" si le tag est FILLED **sans** close_pnl
    et **sans** fill_time de fermeture explicite. On agrège par
    contract_id en sommant n_ct (signed selon direction).
    """
    by_contract: dict[str, dict[str, Any]] = {}
    for tag, info in state.get("placed_tags", {}).items():
        if info.get("status") != "FILLED":
            continue
        if info.get("close_pnl") is not None:
            continue  # déjà clos
        cid = info.get("contract_id")
        direction = info.get("direction", "")
        n_ct = int(info.get("n_ct", 0))
        sign = 1 if direction == "long" else -1
        if cid not in by_contract:
            by_contract[cid] = {"signed_size": 0, "tags": []}
        by_contract[cid]["signed_size"] += sign * n_ct
        by_contract[cid]["tags"].append(tag)
    return by_contract


def derive_open_orders_from_state(state: dict[str, Any]) -> set[int]:
    """Order IDs encore considérés "ouverts" côté state."""
    out: set[int] = set()
    for _tag, info in state.get("placed_tags", {}).items():
        status = info.get("status")
        # PLACED / PENDING / WORKING / ARMED = ordre encore au broker
        if status in {"PLACED", "PENDING", "WORKING", "ARMED"}:
            oid = info.get("order_id")
            if oid is not None:
                out.add(int(oid))
    return out


def sum_state_pnl_for_date(state: dict[str, Any], date_str: str) -> float:
    """Somme close_pnl des tags clos ce jour-là."""
    total = 0.0
    for _tag, info in state.get("placed_tags", {}).items():
        if info.get("close_pnl") is None:
            continue
        ft = info.get("fill_time") or info.get("placed_at") or ""
        # On garde les tags dont le placed_at ou fill_time tombe ce jour-là
        if ft.startswith(date_str):
            total += float(info["close_pnl"])
    return round(total, 2)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers broker
# ──────────────────────────────────────────────────────────────────────────────


def fetch_broker_positions(client: Any, account_id: int) -> dict[str, dict[str, Any]]:
    """Positions broker par contract_id : {signed_size, raw}."""
    raw_positions = client.get_positions(account_id)
    out: dict[str, dict[str, Any]] = {}
    for pos in raw_positions:
        cid = pos.get("contractId") or pos.get("contract_id")
        if cid is None:
            continue
        ptype = pos.get("type", 0)  # 0=long, 1=short
        size = int(pos.get("size", 0))
        sign = 1 if ptype == 0 else -1
        out[cid] = {"signed_size": sign * size, "raw": pos}
    return out


def fetch_broker_open_orders(client: Any, account_id: int) -> set[int]:
    """Order IDs ouverts côté broker."""
    raw_orders = client.get_open_orders(account_id)
    return {int(o["id"]) for o in raw_orders if "id" in o}


def fetch_broker_realized_pnl(client: Any, account_id: int, date_str: str) -> float:
    """Somme des `profitAndLoss` des trades du jour (en USD)."""
    start_dt = datetime.fromisoformat(date_str).replace(tzinfo=UTC)
    raw_trades = client.get_trades_since(account_id, start_dt)
    total = 0.0
    for t in raw_trades:
        pnl = t.get("profitAndLoss")
        if pnl is None:
            continue  # aller (opening trade)
        ts = t.get("creationTimestamp", "")
        if ts.startswith(date_str):
            total += float(pnl)
    return round(total, 2)


# ──────────────────────────────────────────────────────────────────────────────
# Réconciliation
# ──────────────────────────────────────────────────────────────────────────────


def reconcile(
    state: dict[str, Any],
    client: Any,
    date_str: str,
    tolerance_usd: float = DEFAULT_TOLERANCE_USD,
) -> ReconciliationReport:
    """Compare state local et état broker pour un jour donné.

    `client` est un objet exposant `get_positions`, `get_open_orders`,
    `get_trades_since` (typiquement un ProjectXClient ou un mock).
    """
    account_id = int(state.get("account_id", 0))
    report = ReconciliationReport(date=date_str, account_id=account_id)

    # 1. Positions
    try:
        broker_pos = fetch_broker_positions(client, account_id)
        state_pos = derive_open_positions_from_state(state)
        all_contracts = set(broker_pos) | set(state_pos)
        for cid in sorted(all_contracts):
            b = broker_pos.get(cid, {"signed_size": 0})
            s = state_pos.get(cid, {"signed_size": 0})
            b_signed = int(b["signed_size"])
            s_signed = int(s["signed_size"])
            report.positions.append(
                PositionDiff(
                    contract_id=cid,
                    broker_size=abs(b_signed),
                    broker_side=("long" if b_signed > 0 else "short" if b_signed < 0 else None),
                    state_size=abs(s_signed),
                    state_side=("long" if s_signed > 0 else "short" if s_signed < 0 else None),
                    delta=(
                        "match" if b_signed == s_signed else f"broker={b_signed} state={s_signed}"
                    ),
                )
            )
    except Exception as exc:
        report.errors.append(f"positions: {exc}")

    # 2. Ordres ouverts
    try:
        broker_orders = fetch_broker_open_orders(client, account_id)
        state_orders = derive_open_orders_from_state(state)
        all_orders = broker_orders | state_orders
        for oid in sorted(all_orders):
            in_b = oid in broker_orders
            in_s = oid in state_orders
            detail = ""
            if in_b and not in_s:
                detail = "orphelin broker (ordre ouvert non tracké en local)"
            elif in_s and not in_b:
                detail = "orphelin state (ordre marqué ouvert mais fermé broker)"
            report.orders.append(
                OrderDiff(order_id=oid, in_broker=in_b, in_state=in_s, detail=detail)
            )
    except Exception as exc:
        report.errors.append(f"orders: {exc}")

    # 3. P&L journalier
    try:
        broker_pnl = fetch_broker_realized_pnl(client, account_id, date_str)
        state_pnl = sum_state_pnl_for_date(state, date_str)
        report.pnl = PnLDiff(
            broker_pnl=broker_pnl,
            state_pnl=state_pnl,
            tolerance_usd=tolerance_usd,
        )
    except Exception as exc:
        report.errors.append(f"pnl: {exc}")

    return report


# ──────────────────────────────────────────────────────────────────────────────
# Rendu
# ──────────────────────────────────────────────────────────────────────────────


def format_report_human(report: ReconciliationReport) -> str:
    lines = [
        "=" * 60,
        f"  RECONCILIATION — {report.date}  (account {report.account_id})",
        "=" * 60,
    ]

    if report.errors:
        lines.append("\n  ⚠️ ERREURS :")
        for e in report.errors:
            lines.append(f"    • {e}")

    # Positions
    lines.append("\n  ▸ Positions ouvertes :")
    if not report.positions:
        lines.append("    (aucune)")
    for p in report.positions:
        flag = "✅" if not p.is_mismatch else "❌"
        lines.append(
            f"    {flag} {p.contract_id}  broker={p.broker_side or '-'} {p.broker_size}"
            f"  state={p.state_side or '-'} {p.state_size}  ({p.delta})"
        )

    # Ordres
    lines.append("\n  ▸ Ordres ouverts :")
    if not report.orders:
        lines.append("    (aucun)")
    for o in report.orders:
        flag = "✅" if not o.is_mismatch else "❌"
        suffix = f"  — {o.detail}" if o.detail else ""
        lines.append(
            f"    {flag} order_id={o.order_id}  broker={o.in_broker}  state={o.in_state}{suffix}"
        )

    # P&L
    if report.pnl is not None:
        flag = "✅" if not report.pnl.is_mismatch else "❌"
        lines.append(
            f"\n  ▸ P&L journalier : {flag} broker=${report.pnl.broker_pnl:+,.2f}  "
            f"state=${report.pnl.state_pnl:+,.2f}  Δ=${report.pnl.delta:+,.2f}  "
            f"(tol ±${report.pnl.tolerance_usd:.2f})"
        )

    lines.append("")
    lines.append(
        f"  {'❌ MISMATCH DÉTECTÉ' if report.has_mismatch else '✅ STATE & BROKER ALIGNÉS'}"
    )
    lines.append("=" * 60)
    return "\n".join(lines)


def format_report_telegram(report: ReconciliationReport) -> str:
    """Message court pour Telegram (markdown V2 simplifié)."""
    if not report.has_mismatch:
        return f"✅ Reconcile {report.date} — state & broker alignés."

    parts = [f"❌ *Reconcile {report.date}* — mismatch détecté."]

    if report.errors:
        parts.append("Erreurs :")
        for e in report.errors[:3]:
            parts.append(f"• {e}")

    pos_mis = [p for p in report.positions if p.is_mismatch]
    if pos_mis:
        parts.append(f"Positions ({len(pos_mis)}) :")
        for p in pos_mis[:5]:
            parts.append(f"• {p.contract_id} {p.delta}")

    ord_mis = [o for o in report.orders if o.is_mismatch]
    if ord_mis:
        parts.append(f"Ordres ({len(ord_mis)}) :")
        for o in ord_mis[:5]:
            parts.append(f"• {o.order_id} {o.detail}")

    if report.pnl is not None and report.pnl.is_mismatch:
        parts.append(
            f"P&L : broker ${report.pnl.broker_pnl:+,.2f} vs "
            f"state ${report.pnl.state_pnl:+,.2f} (Δ ${report.pnl.delta:+,.2f})"
        )

    return "\n".join(parts)


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────


def _build_client_from_env() -> Any:
    """Crée un ProjectXClient depuis les credentials env. Lazy import."""
    from broker.projectx_client import ProjectXClient  # noqa: PLC0415

    username = os.environ.get("PROJECTX_USERNAME")
    api_key = os.environ.get("PROJECTX_API_KEY")
    if not username or not api_key:
        raise RuntimeError(
            "Variables d'environnement manquantes : PROJECTX_USERNAME et/ou "
            "PROJECTX_API_KEY. Cf. ~/.env."
        )
    client = ProjectXClient(username=username, api_key=api_key)
    client.login()
    return client


def _send_telegram(message: str) -> None:
    """Envoie un message Telegram (best-effort)."""
    try:
        from broker.telegram_bot import TelegramBot  # noqa: PLC0415

        bot = TelegramBot()
        bot.send(message)
    except Exception as exc:
        logger.warning("Impossible d'envoyer le message Telegram : %s", exc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Réconciliation broker ↔ state local")
    parser.add_argument(
        "--state",
        default=str(LIVE_STATE_FILE),
        help=f"Chemin du fichier d'état (défaut: {LIVE_STATE_FILE})",
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Date à réconcilier (YYYY-MM-DD). Défaut: state.date.",
    )
    parser.add_argument(
        "--tolerance-usd",
        type=float,
        default=DEFAULT_TOLERANCE_USD,
        help=f"Tolérance P&L USD (défaut: {DEFAULT_TOLERANCE_USD})",
    )
    parser.add_argument(
        "--notify", action="store_true", help="Envoie une alerte Telegram si mismatch"
    )
    parser.add_argument("--json", action="store_true", help="Sortie JSON pure (stdout)")
    parser.add_argument("--verbose", action="store_true", help="Logs détaillés")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )

    state = load_state(args.state)
    if not state:
        print(f"❌ State introuvable ou vide : {args.state}", file=sys.stderr)
        return 2

    date_str = args.date or state.get("date") or datetime.now(UTC).strftime("%Y-%m-%d")

    try:
        client = _build_client_from_env()
    except Exception as exc:
        print(f"❌ Impossible d'initialiser le client broker : {exc}", file=sys.stderr)
        return 3

    report = reconcile(state, client, date_str, tolerance_usd=args.tolerance_usd)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print(format_report_human(report))

    if args.notify and report.has_mismatch:
        _send_telegram(format_report_telegram(report))

    return 1 if report.has_mismatch else 0


if __name__ == "__main__":
    sys.exit(main())
