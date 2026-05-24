#!/usr/bin/env python3
"""Entrypoint shadow — équivalent live.py mais avec dry_run absolu.

Le shadow runner tourne en parallèle du daemon live, observe le même flux
WebSocket, applique la logique de décision, mais ne poste jamais d'ordres
réels. State et logs strictement séparés du live :

  state/shadow_state.json   (au lieu de state/live_state.json)
  logs/shadow_events.log    (au lieu de logs/trading_events.log)
  state/shadow_daemon.pid   (PID lock dédié — pas de conflit avec le live)

Usage :
  python shadow.py --tick                # un seul tick
  python shadow.py --daemon              # boucle toutes les 15 min
  python shadow.py --daemon --ticker NQ1 # restreint à un ticker

⚠️  Le shadow runner ne fait JAMAIS de check anti-VPN (contrairement à
live.py) car il n'envoie aucun ordre Topstep. C'est juste un observateur.

⚠️  Pour activer Tailscale exit-node sans casser le live, désactive
d'abord ce runner shadow (et le live, surtout).
"""

from __future__ import annotations

import argparse
import atexit
import logging
import os
import signal
import sys
import time
from pathlib import Path

_PID_FILE = Path(__file__).parent / "state" / "shadow_daemon.pid"
_GRACEFUL_TIMEOUT = 10


def _acquire_pid_lock():
    """PID lock dédié shadow (séparé du PID lock live)."""
    if _PID_FILE.exists():
        try:
            old_pid = int(_PID_FILE.read_text().strip())
            os.kill(old_pid, 0)
            logging.warning("Ancien shadow daemon détecté (PID %d) — arrêt...", old_pid)
            os.kill(old_pid, signal.SIGTERM)
            for _ in range(_GRACEFUL_TIMEOUT * 10):
                time.sleep(0.1)
                try:
                    os.kill(old_pid, 0)
                except OSError:
                    break
            else:
                logging.warning("Shadow PID %d sans réponse — SIGKILL", old_pid)
                try:
                    os.kill(old_pid, signal.SIGKILL)
                except OSError:
                    pass
        except (ValueError, OSError):
            pass

    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PID_FILE.write_text(str(os.getpid()))

    def _cleanup(*_):
        try:
            _PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    atexit.register(_cleanup)
    signal.signal(signal.SIGTERM, _cleanup)


# Support .env simple
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for line in _env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())

from broker.projectx_client import ProjectXClient  # noqa: E402
from broker.shadow_runner import build_shadow_runner, configure_shadow_logging  # noqa: E402
from config import SHADOW_LOG_FILE, SHADOW_STATE_FILE  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Shadow runner — exécute la logique live sans poster d'ordres"
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--tick", action="store_true", help="Un seul tick puis quitter")
    mode.add_argument("--daemon", action="store_true", help="Boucle toutes les 15 min")

    p.add_argument(
        "--ticker", type=str, default=None, help="Restreindre à un seul actif (NQ1, MES1, YM1)"
    )
    p.add_argument(
        "--strategy",
        type=str,
        default="opr_fib_vpc",
        choices=["opr", "fib", "vpc", "opr_fib", "opr_fib_vpc"],
        help="Stratégie(s) à exécuter",
    )
    p.add_argument(
        "--state",
        type=str,
        default=SHADOW_STATE_FILE,
        help=f"Chemin du fichier d'état shadow (défaut : {SHADOW_STATE_FILE})",
    )
    p.add_argument(
        "--log-file",
        type=str,
        default=SHADOW_LOG_FILE,
        help=f"Chemin du log shadow (défaut : {SHADOW_LOG_FILE})",
    )
    p.add_argument(
        "--account-id",
        type=int,
        default=None,
        help="Override PROJECTX_ACCOUNT_ID",
    )
    p.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    configure_shadow_logging(args.log_level, args.log_file)
    log = logging.getLogger("shadow")

    if args.daemon:
        _acquire_pid_lock()

    # Crédentials
    username = os.environ.get("PROJECTX_USERNAME", "").strip()
    api_key = os.environ.get("PROJECTX_API_KEY", "").strip()
    if not username or not api_key:
        log.critical("PROJECTX_USERNAME et PROJECTX_API_KEY doivent être définis (.env)")
        return 1

    client = ProjectXClient(username=username, api_key=api_key)
    if not client.login():
        log.critical("Authentification ProjectX échouée")
        return 1

    _env_acct = os.environ.get("PROJECTX_ACCOUNT_ID", "").strip()
    account_id = args.account_id or (int(_env_acct) if _env_acct else 0)
    if not account_id:
        accounts = client.get_accounts(only_active=True)
        if not accounts:
            log.critical("Aucun compte actif trouvé")
            return 1
        account_id = accounts[0]["id"]
        log.info("Compte shadow : id=%d", account_id)

    tickers = [args.ticker] if args.ticker else None
    runner = build_shadow_runner(
        client=client,
        account_id=account_id,
        state_file=args.state,
        tickers=tickers,
        strategy=args.strategy,
        telegram=None,  # pas de notification Telegram en shadow par défaut
    )
    log.info("Shadow runner prêt — dry_run=%s state=%s", runner.dry_run, runner.state_file)

    if args.tick:
        runner.run_tick()
        return 0

    log.info("Shadow daemon démarré — Ctrl+C pour arrêter")
    while True:
        try:
            runner.run_tick()
        except KeyboardInterrupt:
            log.info("Arrêt demandé (KeyboardInterrupt)")
            break
        except Exception as exc:
            log.error("Erreur non fatale dans run_tick : %s", exc, exc_info=True)

        # Attente jusqu'à la prochaine bougie M15 (:01, :16, :31, :46)
        from datetime import UTC, datetime, timedelta  # noqa: PLC0415

        now = datetime.now(UTC)
        next_minute = (now.minute // 15 + 1) * 15 + 1
        if next_minute >= 60:
            target = (now + timedelta(hours=1)).replace(minute=1, second=0, microsecond=0)
        else:
            target = now.replace(minute=next_minute, second=0, microsecond=0)
        sleep_s = max(1, (target - now).total_seconds())
        log.debug("Sleep %.0fs jusqu'à %s", sleep_s, target.strftime("%H:%M:%S"))
        time.sleep(sleep_s)

    return 0


if __name__ == "__main__":
    sys.exit(main())
