#!/usr/bin/env python3
"""
Place un ordre LIMIT TEST sur MES via l'API ProjectX, pour valider que les
events GatewayUserOrder/Position/Trade arrivent côté WebSocket (User Hub).

Sécurité :
  • LIMIT BUY 1 MES à low_récent - 100 ticks (≈ -25 pts) → ne fillera pas
  • Sleep 30s, puis cancel
  • Compte : SIM (account.simulated=True via API) → zéro risque financier réel
  • custom_tag = "SMOKE_TEST_<timestamp>" pour cross-référence avec le smoke

Usage :
    python -m scripts.place_test_order
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta, timezone

from broker.projectx_client import ProjectXClient


def _load_credentials() -> tuple:
    user = os.environ.get("PROJECTX_USERNAME")
    key  = os.environ.get("PROJECTX_API_KEY")
    if user and key:
        return user, key
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("PROJECTX_USERNAME="):
                    user = line.split("=", 1)[1].strip().strip('"')
                elif line.startswith("PROJECTX_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip('"')
    if not user or not key:
        sys.exit("ERREUR : credentials manquantes")
    return user, key


def main():
    user, key = _load_credentials()
    client = ProjectXClient(user, key)
    if not client.login():
        sys.exit("ERREUR : login refusé")
    print(f"✓ Login OK")

    accounts = client.get_accounts()
    if not accounts:
        sys.exit("ERREUR : aucun compte")
    account_id = accounts[0]["id"]
    print(f"✓ Account : {account_id} ({accounts[0].get('name')})")

    # ── Récup contrat MES (sim) ──────────────────────────────────────────
    contract = client.search_contract("MES", live=False)
    if not contract:
        sys.exit("ERREUR : contrat MES introuvable")
    contract_id = contract.get("id") or contract.get("contractId")
    tick_size   = float(contract.get("tickSize", 0.25))
    print(f"✓ Contract : {contract_id}  tick_size={tick_size}")

    # ── Récup prix récent pour positionner le LIMIT loin du marché ───────
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=2)
    bars = client.get_bars(contract_id, start, end, unit=2, unit_number=1,
                            limit=50, live=False)
    if not bars:
        sys.exit("ERREUR : pas de bars MES récents")
    last_low = float(bars[-1]["l"])
    print(f"✓ Last MES low (M1) : {last_low}")

    # LIMIT BUY 100 ticks (25 pts) sous le low → ne fillera pas
    limit_price = last_low - (100 * tick_size)
    limit_price = round(limit_price / tick_size) * tick_size  # snap au tick
    print(f"✓ Limit BUY price : {limit_price}  ({last_low - limit_price:.2f} pts sous le low)")

    custom_tag = f"SMOKE_TEST_{int(time.time())}"
    print(f"\n→ Placement LIMIT BUY 1 MES @ {limit_price} (tag={custom_tag})")

    # API ProjectX : SL ticks NÉGATIFS pour un long (SL sous l'entrée),
    # positifs pour un short. TP inverse. Erreur découverte au smoke v1.
    order_id = client.place_limit_order(
        account_id  = account_id,
        contract_id = contract_id,
        side        = 0,         # 0 = Buy
        size        = 1,
        limit_price = limit_price,
        sl_ticks    = -50,       # SL 50 ticks SOUS le LIMIT (long)
        tp_ticks    = 50,        # TP 50 ticks AU-DESSUS du LIMIT
        custom_tag  = custom_tag,
    )
    if not order_id:
        sys.exit("ERREUR : place_limit_order a retourné None")
    print(f"✓ Order placé : id={order_id}")

    print(f"\n→ Sleep 30s (events WS devraient arriver côté smoke)...")
    time.sleep(30)

    print(f"\n→ Cancel order {order_id}")
    ok = client.cancel_order(account_id, order_id)
    print(f"✓ Cancel : {ok}")

    print(f"\n──────────────────────────────────────")
    print(f"Tag à chercher dans le smoke : {custom_tag}")
    print(f"Order ID à chercher          : {order_id}")
    print(f"──────────────────────────────────────")


if __name__ == "__main__":
    main()
