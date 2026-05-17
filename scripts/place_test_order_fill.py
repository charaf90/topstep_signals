#!/usr/bin/env python3
"""
Test FILL : place un LIMIT BUY MES marketable (au-dessus du prix), attend que
ça fille, garde la position 3s, puis market-sell pour fermer.

But : valider que les events GatewayUserPosition (size=1 puis 0) et
GatewayUserTrade (avec profitAndLoss) arrivent dans le WS et que mon parsing
de RealtimeEvent.size et .pnl est correct.

Sécurité :
  • Compte 50K SIM (PROJECTX_LIVE_MODE=False) → aucun risque financier réel
  • Brackets SL=-10 ticks ($12.50 max), TP=+10 ticks
  • Hold 3s → exposition minimale
  • Cleanup : cancel des brackets résiduels si présents

Risque P&L round-trip estimé : ±$5 sur 1 MES (spread + slippage)

Usage :
    python -m scripts.place_test_order_fill
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

    account_id = client.get_accounts()[0]["id"]
    print(f"✓ Account : {account_id}")

    contract = client.search_contract("MES", live=False)
    contract_id = contract.get("id") or contract.get("contractId")
    tick_size   = float(contract.get("tickSize", 0.25))
    print(f"✓ Contract : {contract_id}  tick_size={tick_size}")

    # ── Récup dernier prix M1 pour positionner le LIMIT au-dessus du marché ──
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=30)
    bars = client.get_bars(contract_id, start, end, unit=2, unit_number=1,
                            limit=10, live=False)
    if not bars:
        sys.exit("ERREUR : pas de bars MES")
    last_close = float(bars[-1]["c"])
    print(f"✓ Last MES close (M1) : {last_close}")

    # LIMIT BUY 5 ticks au-dessus → marketable (fill garanti sauf gap)
    limit_price = last_close + (5 * tick_size)
    limit_price = round(limit_price / tick_size) * tick_size
    print(f"✓ Limit BUY price : {limit_price} (+{5 * tick_size} pts au-dessus de close)")

    custom_tag = f"FILL_TEST_{int(time.time())}"
    print(f"\n→ Placement LIMIT BUY 1 MES @ {limit_price}  tag={custom_tag}")
    print(f"   SL bracket : -10 ticks (= -2.5 pts = ~$12.50 max loss)")
    print(f"   TP bracket : +10 ticks (= +2.5 pts = ~$12.50 max gain)")

    order_id = client.place_limit_order(
        account_id  = account_id,
        contract_id = contract_id,
        side        = 0,           # BUY
        size        = 1,
        limit_price = limit_price,
        sl_ticks    = -10,         # long → SL négatif
        tp_ticks    = 10,
        custom_tag  = custom_tag,
    )
    if not order_id:
        sys.exit("ERREUR : place_limit_order a échoué")
    print(f"✓ Order placé : id={order_id}")

    print(f"\n→ Sleep 3s — attente du fill et des events position/trade")
    time.sleep(3)

    # ── Cleanup : market close, puis cancel brackets résiduels ───────────
    print(f"\n→ Market SELL 1 MES pour clôturer la position")
    close_tag = f"FILL_TEST_CLOSE_{int(time.time())}"
    close_order_id = client.place_market_order(
        account_id  = account_id,
        contract_id = contract_id,
        side        = 1,           # SELL
        size        = 1,
        custom_tag  = close_tag,
    )
    print(f"✓ Close order : id={close_order_id}")

    print(f"\n→ Sleep 3s — attente des events closing trade")
    time.sleep(3)

    # ── Cleanup brackets résiduels (devraient être auto-cancelled mais bon) ──
    open_orders = client.get_open_orders(account_id)
    bracket_open = [
        o for o in open_orders
        if str(o.get("customTag", "")).startswith(custom_tag)
        or str(o.get("customTag", "")).startswith(close_tag)
    ]
    if bracket_open:
        print(f"\n→ {len(bracket_open)} brackets résiduels à canceller :")
        for o in bracket_open:
            oid = o.get("id")
            ok = client.cancel_order(account_id, oid)
            print(f"   cancel {oid} ({o.get('customTag')}) : {ok}")
    else:
        print(f"\n✓ Aucun bracket résiduel (auto-cancellation OK)")

    print(f"\n──────────────────────────────────────")
    print(f"Tag entry : {custom_tag}")
    print(f"Tag close : {close_tag}")
    print(f"Order ID  : {order_id}")
    print(f"Close ID  : {close_order_id}")
    print(f"──────────────────────────────────────")


if __name__ == "__main__":
    main()
