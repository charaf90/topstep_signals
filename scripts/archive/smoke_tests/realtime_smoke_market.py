#!/usr/bin/env python3
"""
Smoke test manuel du Market Hub ProjectX (Phase C — discovery des schemas).

Se connecte à `rtc.topstepx.com/hubs/market` avec un JWT obtenu via login,
souscrit aux quotes/trades/depth pour un ou plusieurs contrats, et imprime
les payloads BRUTS des events `GatewayQuote`, `GatewayTrade`, `GatewayDepth`.

But : valider AVANT de coder le parser que les structures réelles correspondent
à la doc. Pattern identique au smoke User Hub (`scripts/realtime_smoke.py`)
mais avec discovery dirigée vers les events Market Hub.

NOTE : sniffer pur lecture, ne MUTE PAS l'état du daemon.

Usage :
    python -m scripts.realtime_smoke_market --duration 300 --symbol MNQ
    python -m scripts.realtime_smoke_market --duration 60 --symbol MNQ,MYM
    python -m scripts.realtime_smoke_market --contract-id CON.F.US.MNQ.M26
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from collections import Counter, defaultdict
from typing import Dict, List, Optional

from broker.projectx_client import ProjectXClient


# ─────────────────────────────────────────────────────────────────────────────
# Credentials (identique au smoke User Hub)
# ─────────────────────────────────────────────────────────────────────────────

def _load_credentials() -> tuple:
    user = os.environ.get("PROJECTX_USERNAME")
    key = os.environ.get("PROJECTX_API_KEY")
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
        sys.exit(
            "ERREUR : PROJECTX_USERNAME et PROJECTX_API_KEY introuvables. "
            "Définir en ENV ou dans .env"
        )
    return user, key


# ─────────────────────────────────────────────────────────────────────────────
# Smoke runner — raw SignalR sans passer par ProjectXRealtimeClient
# ─────────────────────────────────────────────────────────────────────────────

class MarketSmokeRunner:
    """
    Connexion SignalR brute au Market Hub avec collecte des payloads.

    Volontairement minimaliste — pas de queue, pas de supervisor, juste un
    handler par event qui imprime le payload brut et le compte.
    """

    def __init__(self, hub_url: str, token_provider, contract_ids: List[str],
                 verbose: bool = True, max_print_per_kind: int = 20):
        self._hub_url = hub_url
        self._token_provider = token_provider
        self._contract_ids = contract_ids
        self._verbose = verbose
        self._max_print = max_print_per_kind

        self._connection = None
        self._lock = threading.RLock()
        self._connected = threading.Event()

        # Compteurs
        self._counts: Counter = Counter()
        self._print_counts: Counter = Counter()
        self._first_payload_per_kind: Dict[str, dict] = {}
        self._last_event_ts = time.monotonic()
        self._t0 = time.monotonic()

    # ── API ──────────────────────────────────────────────────────────────

    def start(self) -> None:
        from signalrcore.hub_connection_builder import HubConnectionBuilder

        token = self._token_provider()
        if not token:
            raise RuntimeError("token_provider() vide")
        url_with_token = f"{self._hub_url}?access_token={token}"

        conn = (
            HubConnectionBuilder()
            .with_url(
                url_with_token,
                options={
                    "access_token_factory": self._token_provider,
                    "skip_negotiation": False,
                },
            )
            .with_automatic_reconnect({
                "type": "raw",
                "keep_alive_interval": 10,
                "reconnect_interval": 5,
                "max_attempts": 5,
            })
            .build()
        )

        conn.on_open(self._on_open)
        conn.on_close(self._on_close)
        conn.on_error(self._on_error)

        # Event handlers Market Hub — noms à valider via le smoke.
        # On enregistre les 3 noms documentés + variantes plausibles pour
        # ne rien rater si le serveur émet une casse différente.
        for kind in ("GatewayQuote", "GatewayTrade", "GatewayDepth"):
            conn.on(kind, self._make_handler(kind))

        with self._lock:
            self._connection = conn
            conn.start()

    def stop(self) -> None:
        with self._lock:
            try:
                if self._connection is not None:
                    self._connection.stop()
            except Exception as exc:
                print(f"  WARN stop : {exc}")

    def is_connected(self) -> bool:
        return self._connected.is_set()

    def health(self) -> dict:
        now = time.monotonic()
        return {
            "connected": self._connected.is_set(),
            "uptime_s": now - self._t0,
            "last_event_age_s": now - self._last_event_ts,
            "counts": dict(self._counts),
        }

    # ── Hooks SignalR ────────────────────────────────────────────────────

    def _on_open(self, *_) -> None:
        try:
            self._connected.set()
            print(f"  ✓ Connecté au Market Hub. Subscribe pour {len(self._contract_ids)} contract(s)...")
            with self._lock:
                if self._connection is None:
                    return
                for cid in self._contract_ids:
                    # Méthodes documentées Market Hub
                    self._connection.send("SubscribeContractQuotes", [cid])
                    self._connection.send("SubscribeContractTrades", [cid])
                    self._connection.send("SubscribeContractMarketDepth", [cid])
                    print(f"     → SubscribeContractQuotes/Trades/MarketDepth({cid})")
        except Exception as exc:
            print(f"  ERREUR _on_open : {exc}")
            import traceback
            traceback.print_exc()

    def _on_close(self, *_) -> None:
        self._connected.clear()
        print(f"  ⚠ Déconnecté du Market Hub")

    def _on_error(self, err) -> None:
        print(f"  ✗ Erreur WS : {err}")

    # ── Event handlers ───────────────────────────────────────────────────

    def _make_handler(self, kind: str):
        def handler(args):
            try:
                self._last_event_ts = time.monotonic()
                self._counts[kind] += 1

                # Capture le premier payload brut de chaque kind pour analyse
                if kind not in self._first_payload_per_kind:
                    self._first_payload_per_kind[kind] = {
                        "args_type": type(args).__name__,
                        "args_len": len(args) if hasattr(args, "__len__") else None,
                        "raw": args,
                    }
                    print(f"\n  ★ PREMIER {kind} reçu :")
                    print(f"    type(args)={type(args).__name__}  "
                          f"len={len(args) if hasattr(args, '__len__') else '?'}")
                    print(f"    raw={json.dumps(args, default=str, indent=2)[:2000]}")
                    print()

                # Print throttlé : N premiers, puis sample 1/100
                count = self._counts[kind]
                should_print = (
                    self._verbose
                    and (count <= self._max_print or count % 100 == 0)
                )
                if should_print:
                    ts = time.strftime("%H:%M:%S")
                    print(f"[{ts}] {kind:<14} #{count:<6} "
                          f"raw={json.dumps(args, default=str)[:400]}")
            except Exception as exc:
                print(f"  ✗ Handler {kind} exception : {exc}")
                import traceback
                traceback.print_exc()

        return handler


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Smoke test realtime ProjectX Market Hub (Phase C)"
    )
    parser.add_argument("--duration", type=int, default=300,
                        help="Durée d'écoute en secondes (défaut 300)")
    parser.add_argument("--hub-url", type=str,
                        default="https://rtc.topstepx.com/hubs/market",
                        help="URL du Market Hub TopstepX")
    parser.add_argument("--symbol", type=str, default="MNQ",
                        help="Symbol(s) à souscrire, séparés par virgule (ex: MNQ,MYM)")
    parser.add_argument("--contract-id", type=str, default=None,
                        help="Contract ID explicite (bypass search). "
                             "Ex: CON.F.US.MNQ.M26")
    parser.add_argument("--max-print-per-kind", type=int, default=20,
                        help="Nombre max d'events à imprimer par kind avant sampling")
    parser.add_argument("--quiet", action="store_true",
                        help="N'imprime que les premiers payloads + heartbeats")
    args = parser.parse_args()

    # ── 1. Login REST + résolution contract_ids ──────────────────────────
    user, key = _load_credentials()
    print(f"→ Login ProjectX en tant que {user}...")
    client = ProjectXClient(user, key)
    if not client.login():
        sys.exit("ERREUR : login refusé")
    print(f"  ✓ JWT obtenu (premières 20 chars : {client.token[:20]}...)")

    contract_ids: List[str] = []
    if args.contract_id:
        contract_ids = [args.contract_id]
        print(f"  → contract_id explicite : {args.contract_id}")
    else:
        symbols = [s.strip().upper() for s in args.symbol.split(",") if s.strip()]
        for sym in symbols:
            c = client.search_contract(sym, live=False)
            if not c:
                print(f"  ⚠ Aucun contrat trouvé pour '{sym}' — skip")
                continue
            cid = c["id"]
            contract_ids.append(cid)
            print(f"  ✓ {sym} → {cid} ({c.get('description', '?')})")
        if not contract_ids:
            sys.exit("ERREUR : aucun contract_id résolu")

    # ── 2. Démarrer le smoke runner ─────────────────────────────────────
    print(f"\n→ Connexion SignalR à {args.hub_url}...")
    runner = MarketSmokeRunner(
        hub_url=args.hub_url,
        token_provider=lambda: client.token,
        contract_ids=contract_ids,
        verbose=not args.quiet,
        max_print_per_kind=args.max_print_per_kind,
    )
    runner.start()

    # Attendre la connexion (max 10 s)
    for _ in range(20):
        if runner.is_connected():
            break
        time.sleep(0.5)
    if not runner.is_connected():
        print("  ⚠ Pas encore connecté après 10s — continue quand même")

    # ── 3. Boucle de monitoring ─────────────────────────────────────────
    print(f"\n→ Écoute pendant {args.duration}s... (Ctrl-C pour stop)")
    print(f"{'─' * 78}\n")

    t0 = time.monotonic()
    last_heartbeat = t0
    try:
        while time.monotonic() - t0 < args.duration:
            time.sleep(2.0)
            now = time.monotonic()
            # Heartbeat toutes les 30 s
            if now - last_heartbeat >= 30:
                last_heartbeat = now
                h = runner.health()
                elapsed = h["uptime_s"]
                print(f"\n  [health t={elapsed:.0f}s] connected={h['connected']} "
                      f"last_event_age={h['last_event_age_s']:.0f}s")
                if h["counts"]:
                    for kind, n in sorted(h["counts"].items()):
                        rate = n / max(elapsed, 1.0)
                        print(f"           {kind:<14} count={n:<6} rate={rate:.1f}/s")
                else:
                    print(f"           ⚠ AUCUN EVENT reçu pour l'instant")
                print()
    except KeyboardInterrupt:
        print("\n  ⏸ Interrompu par l'utilisateur")

    # ── 4. Synthèse finale ──────────────────────────────────────────────
    print(f"\n{'─' * 78}")
    print(f"\n→ Synthèse finale :")
    h = runner.health()
    elapsed = h["uptime_s"]
    print(f"  Durée totale     : {elapsed:.0f}s")
    print(f"  Connecté à la fin : {h['connected']}")
    total = sum(h["counts"].values())
    print(f"  Events totaux     : {total}")
    for kind, n in sorted(h["counts"].items()):
        rate = n / max(elapsed, 1.0)
        print(f"    {kind:<14} : {n} ({rate:.1f}/s)")

    print(f"\n→ Premier payload de chaque kind (pour parser):")
    for kind, info in runner._first_payload_per_kind.items():
        print(f"\n  ── {kind} ──")
        print(f"  args_type : {info['args_type']}")
        print(f"  args_len  : {info['args_len']}")
        print(f"  raw       : {json.dumps(info['raw'], default=str, indent=4)}")

    print(f"\n→ Arrêt du runner...")
    runner.stop()
    print("  ✓ Stop OK")


if __name__ == "__main__":
    main()
