#!/usr/bin/env python3
"""
account_status.py — état RÉEL du compte Topstep, directement depuis l'API broker.

Pourquoi : la compta locale (state/live_state.json, logs) DÉRIVE de la vérité
broker — cas documenté en mémoire projet, re-constaté le 2026-06-10 :
local cum=$1950 / jour=+$204 vs broker balance $52,097 / jour +$177. Causes
typiques : fees non décomptés (le daemon somme `profitAndLoss` SANS `fees`),
fills non attribués, trades manuels. Obtenir l'état d'un compte doit être
UNE commande qui interroge le broker — jamais une lecture de log.

Usage :
  python tools/account_status.py            # vérité broker + écart vs local

Affiche : balance, cum P&L (balance − 50K), P&L du jour de trading
(profitAndLoss − fees, jour futures 23:00 UTC), positions/ordres ouverts,
distances aux limites Topstep, et les DELTAS vs state/live_state.json.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from config import TOPSTEP_ACCOUNT_SIZE, TOPSTEP_DAILY_LOSS_MAX, TOPSTEP_TRAILING_DD

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


def _client():
    from broker.projectx_client import ProjectXClient

    username = os.environ.get("PROJECTX_USERNAME")
    api_key = os.environ.get("PROJECTX_API_KEY")
    if not username or not api_key:
        raise SystemExit("PROJECTX_USERNAME / PROJECTX_API_KEY manquants (cf. .env projet).")
    c = ProjectXClient(username=username, api_key=api_key)
    if not c.login():
        raise SystemExit("Authentification ProjectX échouée.")
    return c


def trading_day_start(now: datetime) -> datetime:
    """Début du jour de trading futures : 23:00 UTC (17:00 CT, reset Topstep)."""
    anchor = now.replace(hour=23, minute=0, second=0, microsecond=0)
    return anchor if now >= anchor else anchor - timedelta(days=1)


def main():
    now = datetime.now(UTC)
    client = _client()

    accounts = client.get_accounts()
    if not accounts:
        raise SystemExit("Aucun compte actif retourné par l'API.")
    acct = accounts[0]
    account_id = int(acct["id"])
    balance = float(acct.get("balance", 0.0))
    cum_broker = balance - float(TOPSTEP_ACCOUNT_SIZE)

    day_start = trading_day_start(now)
    trades = client.get_trades_since(account_id, day_start.replace(tzinfo=None))
    closings = [t for t in trades if t.get("profitAndLoss") is not None and not t.get("voided")]
    pnl_gross = sum(float(t["profitAndLoss"]) for t in closings)
    fees = sum(float(t.get("fees") or 0.0) for t in trades if not t.get("voided"))
    day_net = pnl_gross - fees

    positions = client.get_positions(account_id)
    open_orders = client.get_open_orders(account_id)

    dll_remaining = TOPSTEP_DAILY_LOSS_MAX + min(0.0, day_net)
    target_remaining = max(0.0, 3000.0 - cum_broker)

    print(f"═══ COMPTE TOPSTEP (vérité broker) — {now:%Y-%m-%d %H:%M} UTC ═══")
    print(f"  Compte        : {acct.get('name', account_id)}  (canTrade={acct.get('canTrade')})")
    print(f"  Balance       : ${balance:,.2f}")
    print(
        f"  Cum P&L       : ${cum_broker:+,.2f}   (target $3,000 — reste ${target_remaining:,.0f})"
    )
    print(
        f"  Jour (futures): ${day_net:+,.2f} net   "
        f"(brut ${pnl_gross:+,.2f} − fees ${fees:,.2f}, "
        f"{len(closings)} clôtures depuis {day_start:%d %Hh%M} UTC)"
    )
    print(f"  DLL restant   : ${dll_remaining:,.0f} / ${TOPSTEP_DAILY_LOSS_MAX}")
    print(f"  Positions     : {len(positions)} ouverte(s) · {len(open_orders)} ordre(s) en attente")
    for p in positions:
        print(f"    • {p.get('contractId')} size={p.get('size')} avg={p.get('averagePrice')}")

    # ── Comparaison avec la compta locale ────────────────────────────────────
    state_path = Path("state/live_state.json")
    if state_path.exists():
        rs = json.loads(state_path.read_text(encoding="utf-8")).get("risk_state", {})
        cum_local = float(rs.get("cum_pnl", 0.0))
        day_local = float(rs.get("realized_day_pnl", 0.0))
        peak_local = float(rs.get("peak_pnl", 0.0))
        d_cum = cum_local - cum_broker
        d_day = day_local - day_net
        print("\n═══ COMPTA LOCALE (state/live_state.json) vs BROKER ═══")
        print(f"  cum local ${cum_local:+,.2f}  vs broker ${cum_broker:+,.2f}  → Δ ${d_cum:+,.2f}")
        print(f"  jour local ${day_local:+,.2f} vs broker ${day_net:+,.2f}  → Δ ${d_day:+,.2f}")
        flag = "✅ cohérent (< $50)" if abs(d_cum) < 50 and abs(d_day) < 50 else "⚠️ DÉSYNC > $50"
        print(f"  {flag}")
        if abs(d_day - fees) < 5:
            print(
                "  ℹ️  L'écart jour ≈ les fees : le RM local compte le P&L brut "
                "(profitAndLoss sans fees)."
            )
        # Trailing : le floor dépend du PEAK — approximation depuis le peak local.
        floor = max(peak_local, cum_broker) - TOPSTEP_TRAILING_DD
        print(
            f"\n  Trailing (approx, peak local ${peak_local:+,.0f}) : "
            f"floor ${floor:+,.0f} → slack ${cum_broker - floor:,.0f} / ${TOPSTEP_TRAILING_DD}"
        )
    else:
        print("\n  (pas de state local — comparaison impossible)")


if __name__ == "__main__":
    main()
