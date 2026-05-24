#!/usr/bin/env python3
"""
Smoke test manuel du client realtime ProjectX User Hub.

Se connecte à `rtc.topstepx.com/hubs/user` avec un JWT obtenu via login,
imprime les payloads bruts des events `GatewayUserOrder/Position/Trade/Account`
pendant N secondes, puis s'arrête proprement.

But : valider AVANT la promotion en prod que les schémas de payloads
correspondent aux extractions plausibles codées dans `_on_user_*`. Si les
champs réels diffèrent (ex: `Id` au lieu de `id`, `tag` au lieu de
`customTag`), ajuster `broker/projectx_realtime.py` AVANT le burn-in.

NOTE : ne MUTE PAS l'état du daemon — c'est un sniffer pur lecture.

Usage :
    # Avec les credentials dans .env / variables d'environnement habituelles :
    python -m scripts.realtime_smoke --duration 300

    # Avec credentials explicites :
    PROJECTX_USERNAME=foo PROJECTX_API_KEY=bar python -m scripts.realtime_smoke
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from broker.projectx_client import ProjectXClient
from broker.projectx_realtime import ProjectXRealtimeClient, RealtimeEvent


def _load_credentials() -> tuple:
    """Lit les credentials depuis ENV ou .env (fallback)."""
    user = os.environ.get("PROJECTX_USERNAME")
    key = os.environ.get("PROJECTX_API_KEY")
    if user and key:
        return user, key

    # Fallback : .env à la racine du projet
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
        sys.exit(
            "ERREUR : PROJECTX_USERNAME et PROJECTX_API_KEY introuvables. "
            "Définir en ENV ou dans .env"
        )
    return user, key


def main():
    parser = argparse.ArgumentParser(description="Smoke test realtime ProjectX User Hub")
    parser.add_argument(
        "--duration", type=int, default=300, help="Durée d'écoute en secondes (défaut 300)"
    )
    parser.add_argument(
        "--hub-url",
        type=str,
        default="https://rtc.topstepx.com/hubs/user",
        help="URL du hub (défaut User Hub TopstepX)",
    )
    parser.add_argument(
        "--account-id",
        type=int,
        default=None,
        help="Account ID (sinon auto-détecté via get_accounts)",
    )
    args = parser.parse_args()

    # ── 1. Login REST + récupération JWT et account_id ───────────────────
    user, key = _load_credentials()
    print(f"→ Login ProjectX en tant que {user}...")
    client = ProjectXClient(user, key)
    if not client.login():
        sys.exit("ERREUR : login refusé")
    print(f"  ✓ JWT obtenu (premières 20 chars : {client.token[:20]}...)")

    account_id = args.account_id
    if account_id is None:
        accounts = client.get_accounts()
        if not accounts:
            sys.exit("ERREUR : aucun compte actif")
        account_id = accounts[0]["id"]
        print(f"  ✓ Account auto-détecté : {account_id} ({accounts[0].get('name')})")

    # ── 2. Démarrer le client realtime ───────────────────────────────────
    print(f"→ Connexion SignalR à {args.hub_url}...")
    rt = ProjectXRealtimeClient(
        account_id=account_id,
        token_provider=lambda: client.token,
        hub_url=args.hub_url,
        queue_maxsize=2048,
        reconnect_delays=(0, 2, 5, 10),
        max_silence_s=60.0,
    )
    rt.start()

    # ── 3. Boucle de drain ────────────────────────────────────────────────
    print(f"→ Écoute pendant {args.duration}s... (Ctrl-C pour stop)\n")
    print(f"{'─' * 78}")

    t0 = time.monotonic()
    total = 0
    try:
        while time.monotonic() - t0 < args.duration:
            events: list[RealtimeEvent] = rt.drain_events(max_events=100)
            for evt in events:
                total += 1
                print(
                    f"[{evt.received_at.strftime('%H:%M:%S')}] "
                    f"{evt.kind.upper():<10} "
                    f"contract={evt.contract_id or '?':<25} "
                    f"order_id={evt.order_id} tag={evt.custom_tag} "
                    f"pnl={evt.pnl} size={evt.size} status={evt.status}"
                )
                # Payload brut pour analyse des champs disponibles (non tronqué)
                print(f"           RAW: {json.dumps(evt.payload, default=str)}")
            time.sleep(1.0)

            # Heartbeat health toutes les 30s
            if int(time.monotonic() - t0) % 30 == 0 and not events:
                h = rt.health()
                print(
                    f"  [health] connected={h['connected']} "
                    f"queue={h['queue_depth']} "
                    f"last_event_age={h['last_event_age_s']:.0f}s "
                    f"disconnects={h['disconnect_count']}"
                )
    except KeyboardInterrupt:
        print("\n  ⏸ Interrompu par l'utilisateur")

    # ── 4. Stop propre ────────────────────────────────────────────────────
    print(f"{'─' * 78}")
    print(f"\n→ Total events reçus : {total}")
    print("→ Arrêt du client realtime...")
    rt.stop(timeout=5.0)
    print("  ✓ Stop OK")

    # ── 5. Final health ──────────────────────────────────────────────────
    print(f"\nFinal health : {rt.health()}")


if __name__ == "__main__":
    main()
