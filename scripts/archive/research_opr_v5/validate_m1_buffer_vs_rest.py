#!/usr/bin/env python3
"""
Validation de cohérence M1Buffer (stream WS) vs REST `get_bars` (Phase C §7.2).

Démarre un `ProjectXMarketRealtimeClient` + `M1Buffer` sur MNQ pendant N minutes,
puis fetch les bars M1 sur la même fenêtre via REST `get_bars(unit=2, unit_number=1)`.
Compare minute par minute : open, high, low, close, volume.

Critère §7.2 du handoff : delta acceptable ≤ 1 tick sur high/low.

Usage :
    python -m scripts.validate_m1_buffer_vs_rest --duration 5      # 5 min
    python -m scripts.validate_m1_buffer_vs_rest --duration 30     # 30 min
    python -m scripts.validate_m1_buffer_vs_rest --symbol MYM      # autre symbole

Output : table comparative + verdict global.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List

from broker.projectx_client import ProjectXClient
from broker.projectx_market_realtime import ProjectXMarketRealtimeClient
from broker.m1_buffer import M1Buffer, M1Bar


# Tick sizes ProjectX par root symbol (utilisés pour le critère "≤ 1 tick")
TICK_SIZE_BY_ROOT = {
    "MNQ": 0.25,
    "MES": 0.25,
    "MYM": 1.0,
    "NQ":  0.25,
    "ES":  0.25,
    "YM":  1.0,
}


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
        sys.exit("ERREUR : credentials PROJECTX_USERNAME / PROJECTX_API_KEY introuvables")
    return user, key


def _parse_rest_bars(bars: List[Dict]) -> Dict[datetime, dict]:
    """Convertit les bars REST en dict indexé par start_ts UTC floor minute."""
    out = {}
    for b in bars:
        t_raw = b.get("t")
        if not t_raw:
            continue
        try:
            ts = datetime.fromisoformat(t_raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ts = ts.astimezone(timezone.utc).replace(second=0, microsecond=0)
        out[ts] = {
            "open":   float(b.get("o", 0.0)),
            "high":   float(b.get("h", 0.0)),
            "low":    float(b.get("l", 0.0)),
            "close":  float(b.get("c", 0.0)),
            "volume": int(b.get("v", 0)),
        }
    return out


def _compare_bars(ws_bar: M1Bar, rest: dict, tick_size: float) -> dict:
    """Retourne les deltas WS vs REST pour 1 bar."""
    return {
        "delta_open":   ws_bar.open  - rest["open"],
        "delta_high":   ws_bar.high  - rest["high"],
        "delta_low":    ws_bar.low   - rest["low"],
        "delta_close":  ws_bar.close - rest["close"],
        "delta_volume": ws_bar.volume - rest["volume"],
        "high_within_1_tick": abs(ws_bar.high - rest["high"]) <= tick_size + 1e-9,
        "low_within_1_tick":  abs(ws_bar.low - rest["low"])   <= tick_size + 1e-9,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Validation M1Buffer (WS) vs REST get_bars"
    )
    parser.add_argument("--duration", type=int, default=5,
                        help="Durée d'écoute en minutes (défaut 5)")
    parser.add_argument("--symbol", type=str, default="MNQ",
                        help="Symbol à valider (défaut MNQ)")
    args = parser.parse_args()

    # ── 1. Login + résolution contract ────────────────────────────────────
    user, key = _load_credentials()
    print(f"→ Login ProjectX en tant que {user}...")
    client = ProjectXClient(user, key)
    if not client.login():
        sys.exit("ERREUR : login refusé")
    c = client.search_contract(args.symbol, live=False)
    if not c:
        sys.exit(f"ERREUR : pas de contract pour {args.symbol}")
    cid = c["id"]
    root = args.symbol.upper().lstrip("/")
    tick_size = TICK_SIZE_BY_ROOT.get(root, 0.25)
    print(f"  ✓ {args.symbol} → {cid} (tick={tick_size})")

    # ── 2. Démarrer WS + buffer ──────────────────────────────────────────
    print(f"\n→ Démarrage client + buffer M1...")
    rt = ProjectXMarketRealtimeClient(
        contract_ids=[cid],
        token_provider=lambda: client.token,
        hub_url="https://rtc.topstepx.com/hubs/market",
        queue_maxsize=50_000,
        max_silence_s=60.0,
    )
    buf = M1Buffer(max_minutes=max(args.duration * 2, 60))
    rt.start()

    for _ in range(20):
        if rt.is_connected():
            break
        time.sleep(0.5)
    if not rt.is_connected():
        rt.stop()
        sys.exit("ERREUR : connexion WS impossible")
    print(f"  ✓ Connecté, drain {args.duration} min...")

    # ── 3. Drain en boucle ───────────────────────────────────────────────
    start_utc = datetime.now(timezone.utc)
    deadline = time.monotonic() + args.duration * 60
    n_drain = 0
    while time.monotonic() < deadline:
        events = rt.drain_events(max_events=5000)
        for evt in events:
            buf.consume(evt)
        n_drain += 1
        # Force-close des bars dont la minute est passée
        buf.flush_stale_bars()
        # Progress tous les 30 s
        if n_drain % 30 == 0:
            health = buf.health()
            print(f"  [t={n_drain}s] trades_consumed={health['trades_consumed']}  "
                  f"bars_closed={health['bars_closed']}  "
                  f"rt_health={rt.health()['connected']}")
        time.sleep(1.0)
    # Flush final
    buf.flush_stale_bars()
    end_utc = datetime.now(timezone.utc)
    print(f"  ✓ Drain terminé. start={start_utc.isoformat()} end={end_utc.isoformat()}")

    ws_bars = buf.get_recent_bars(cid, n=10_000, include_forming=False)
    print(f"  ✓ Buffer : {len(ws_bars)} bars M1 fermés via WS")

    # ── 4. Fetch REST sur la même fenêtre ────────────────────────────────
    print(f"\n→ Fetch REST get_bars(unit=2, unit_number=1) sur la fenêtre...")
    # On élargit légèrement la fenêtre pour être sûr de capter tous les bars
    rest_start = start_utc - timedelta(minutes=2)
    rest_end   = end_utc + timedelta(minutes=2)
    rest_raw = client.get_bars(
        contract_id=cid,
        start_dt=rest_start,
        end_dt=rest_end,
        unit=2,            # Minute
        unit_number=1,     # M1
        limit=2000,
        live=False,
        include_partial=False,
    )
    rest_by_ts = _parse_rest_bars(rest_raw)
    print(f"  ✓ REST : {len(rest_by_ts)} bars M1 retournés")

    # ── 5. Comparaison bar par bar ───────────────────────────────────────
    print(f"\n{'─' * 80}")
    print(f"COMPARAISON BAR PAR BAR (WS vs REST)")
    print(f"{'─' * 80}")
    print(f"{'minute_utc':<22} {'WS OHLC':<36} {'REST OHLC':<36} {'Δh':>8} {'Δl':>8}")
    print(f"{'-' * 22} {'-' * 36} {'-' * 36} {'-' * 8} {'-' * 8}")

    common_count = 0
    high_match_1tick = 0
    low_match_1tick  = 0
    perfect_match    = 0
    total_volume_diff = 0
    deltas = []

    for ws_bar in ws_bars:
        ts = ws_bar.start_ts
        if ts not in rest_by_ts:
            print(f"{ts.strftime('%Y-%m-%d %H:%M'):<22} ⚠ Bar WS absent du REST")
            continue
        rest = rest_by_ts[ts]
        d = _compare_bars(ws_bar, rest, tick_size)
        deltas.append(d)
        common_count += 1
        if d["high_within_1_tick"]: high_match_1tick += 1
        if d["low_within_1_tick"]:  low_match_1tick += 1
        if (d["delta_open"] == 0 and d["delta_high"] == 0
                and d["delta_low"] == 0 and d["delta_close"] == 0):
            perfect_match += 1
        total_volume_diff += abs(d["delta_volume"])

        ws_str   = f"O{ws_bar.open:.2f} H{ws_bar.high:.2f} L{ws_bar.low:.2f} C{ws_bar.close:.2f}"
        rest_str = f"O{rest['open']:.2f} H{rest['high']:.2f} L{rest['low']:.2f} C{rest['close']:.2f}"
        print(f"{ts.strftime('%Y-%m-%d %H:%M'):<22} {ws_str:<36} {rest_str:<36} "
              f"{d['delta_high']:>+8.2f} {d['delta_low']:>+8.2f}")

    # Bars REST sans pendant WS (probablement avant la start_utc effective)
    ws_ts_set = {b.start_ts for b in ws_bars}
    rest_only = [ts for ts in rest_by_ts if ts not in ws_ts_set
                 and start_utc <= ts <= end_utc]
    for ts in sorted(rest_only):
        print(f"{ts.strftime('%Y-%m-%d %H:%M'):<22} ⚠ Bar REST absent du WS (loupé)")

    # ── 6. Verdict ───────────────────────────────────────────────────────
    print(f"\n{'─' * 80}")
    print(f"VERDICT")
    print(f"{'─' * 80}")
    print(f"  Bars WS fermés    : {len(ws_bars)}")
    print(f"  Bars REST         : {len(rest_by_ts)}")
    print(f"  Bars en commun    : {common_count}")
    print(f"  Bars REST manquants au WS : {len(rest_only)}")
    if common_count > 0:
        print(f"  High match ≤1 tick : {high_match_1tick}/{common_count} "
              f"({100 * high_match_1tick / common_count:.1f}%)")
        print(f"  Low  match ≤1 tick : {low_match_1tick}/{common_count} "
              f"({100 * low_match_1tick / common_count:.1f}%)")
        print(f"  OHLC perfect match : {perfect_match}/{common_count} "
              f"({100 * perfect_match / common_count:.1f}%)")
        print(f"  Σ |Δvolume|       : {total_volume_diff}")

        # Critère acceptation handoff §7.2 : ≤ 1 tick sur high/low
        accepted = (high_match_1tick / common_count >= 0.95
                    and low_match_1tick / common_count >= 0.95)
        print(f"\n  Critère ≥95% match ≤1 tick high/low : "
              f"{'✓ OK' if accepted else '✗ ÉCHEC'}")
    else:
        print(f"  ⚠ Aucun bar en commun — vérifier la fenêtre temporelle")

    # ── 7. Stop propre ────────────────────────────────────────────────────
    print(f"\n→ Arrêt du runner...")
    rt.stop(timeout=5)
    print("  ✓ Stop OK")


if __name__ == "__main__":
    main()
