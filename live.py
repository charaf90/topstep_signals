#!/usr/bin/env python3
"""
Exécution live des stratégies OPR + Fib via l'API TopstepX / ProjectX.

Usage :
  python live.py --tick                     # un seul tick (dry-run par défaut)
  python live.py --tick --execute           # un tick réel
  python live.py --daemon                   # boucle toutes les 15 min (Ctrl+C pour stopper)
  python live.py --daemon --execute         # boucle réelle

Variables d'environnement (ou fichier .env) :
  PROJECTX_USERNAME   : email / login TopstepX
  PROJECTX_API_KEY    : clé API ProjectX
  PROJECTX_ACCOUNT_ID : identifiant numérique du compte (facultatif si un seul compte)

Options CLI :
  --ticker NQ1         : restreindre à un seul actif
  --strategy opr|fib|opr_fib : stratégie(s) à exécuter (défaut : opr_fib)
  --state  <chemin>    : fichier d'état JSON (défaut : state/live_state.json)
  --log-level DEBUG    : niveau de log
"""

import argparse
import atexit
import logging
import os
import signal
import sys
import time
from pathlib import Path

_PID_FILE = Path(__file__).parent / "state" / "live_daemon.pid"


_GRACEFUL_TIMEOUT = 10  # secondes avant SIGKILL si SIGTERM ignoré


def _acquire_pid_lock():
    """
    Garantit qu'une seule instance daemon tourne à tout moment.

    Si un ancien daemon est détecté (PID vivant) :
      → SIGTERM + attente jusqu'à _GRACEFUL_TIMEOUT secondes → SIGKILL si besoin.
    La nouvelle instance prend ensuite le relais.
    Lock périmé (process mort) : remplacé silencieusement.
    """
    if _PID_FILE.exists():
        try:
            old_pid = int(_PID_FILE.read_text().strip())
            os.kill(old_pid, 0)  # lève OSError si le process n'existe plus
            # Process vivant → on le remplace
            logging.warning(
                "Ancien daemon détecté (PID %d) — arrêt en cours (mise à jour)…",
                old_pid,
            )
            os.kill(old_pid, signal.SIGTERM)
            for _ in range(_GRACEFUL_TIMEOUT * 10):
                time.sleep(0.1)
                try:
                    os.kill(old_pid, 0)
                except OSError:
                    break  # process terminé
            else:
                # Toujours vivant après le délai → SIGKILL
                logging.warning("PID %d ne répond pas — SIGKILL", old_pid)
                try:
                    os.kill(old_pid, signal.SIGKILL)
                except OSError:
                    pass
            logging.info("Ancien daemon arrêté. Démarrage de la nouvelle instance.")
        except (ValueError, OSError):
            pass  # PID invalide ou process déjà mort → lock périmé

    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PID_FILE.write_text(str(os.getpid()))

    def _cleanup(*_):
        try:
            _PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    atexit.register(_cleanup)
    signal.signal(signal.SIGTERM, _cleanup)


# Support .env simple (pas de dépendance python-dotenv)
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for line in _env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())

from broker.live_runner import SessionRunner
from broker.projectx_client import ProjectXClient
from broker.telegram_bot import TelegramBot
from broker.vpn_check import check_no_vpn_on_topstep
from config import (
    LIVE_STATE_FILE,
    TELEGRAM_ENABLED,
    TELEGRAM_LEVEL_COMMANDS,
    TELEGRAM_LEVEL_REPORT,
    TELEGRAM_LEVEL_RISK,
    TELEGRAM_LEVEL_SYSTEM,
    TELEGRAM_LEVEL_TRADES,
)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Runner live TopstepX — OPR + Fib via ProjectX API")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--tick", action="store_true", help="Exécuter un seul tick puis quitter")
    mode.add_argument(
        "--daemon", action="store_true", help="Boucle toutes les 15 min (Ctrl+C pour stopper)"
    )

    p.add_argument("--execute", action="store_true", help="Passer des ordres réels (sinon dry-run)")
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
        "--state", type=str, default=LIVE_STATE_FILE, help="Chemin du fichier d'état JSON"
    )
    p.add_argument(
        "--account-id",
        type=int,
        default=None,
        help="Identifiant du compte (override PROJECTX_ACCOUNT_ID)",
    )
    p.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Niveau de log",
    )
    return p.parse_args()


def _setup_logging(level: str):
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s  %(levelname)-8s  %(name)s : %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _build_runner(args: argparse.Namespace) -> SessionRunner:
    username = os.environ.get("PROJECTX_USERNAME", "").strip()
    api_key = os.environ.get("PROJECTX_API_KEY", "").strip()
    if not username or not api_key:
        logging.critical(
            "PROJECTX_USERNAME et PROJECTX_API_KEY doivent être définis "
            "dans l'environnement ou le fichier .env"
        )
        sys.exit(1)

    client = ProjectXClient(username=username, api_key=api_key)
    if not client.login():
        logging.critical("Authentification ProjectX échouée — vérifier les credentials")
        sys.exit(1)

    # Résolution du compte
    _env_acct = os.environ.get("PROJECTX_ACCOUNT_ID", "").strip()
    account_id = args.account_id or (int(_env_acct) if _env_acct else 0)
    if not account_id:
        accounts = client.get_accounts(only_active=True)
        if not accounts:
            logging.critical("Aucun compte actif trouvé sur ce profil")
            sys.exit(1)
        account = accounts[0]
        account_id = account["id"]
        logging.info(
            "Compte : %s (id=%d, solde=%.2f $, canTrade=%s)",
            account.get("name", "?"),
            account_id,
            account.get("balance", 0.0),
            account.get("canTrade"),
        )

    tickers = [args.ticker] if args.ticker else None

    # ── Bot Telegram ──────────────────────────────────────────────────────
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    telegram = TelegramBot(
        token=tg_token,
        chat_id=tg_chat_id,
        enabled=TELEGRAM_ENABLED and bool(tg_token) and bool(tg_chat_id),
        level_trades=TELEGRAM_LEVEL_TRADES,
        level_risk=TELEGRAM_LEVEL_RISK,
        level_system=TELEGRAM_LEVEL_SYSTEM,
        level_report=TELEGRAM_LEVEL_REPORT,
        level_commands=TELEGRAM_LEVEL_COMMANDS,
    )

    # Détecte si le compte est simulé (challenge) ou live (funded)
    accounts = client.get_accounts(only_active=True)
    is_simulated = any(a.get("simulated", True) for a in accounts if a.get("id") == account_id)
    live_mode = not is_simulated
    log = logging.getLogger("live")
    log.info(
        "Mode compte : %s (live_mode=%s)",
        "SIMULÉ (challenge)" if not live_mode else "LIVE (funded)",
        live_mode,
    )

    return SessionRunner(
        client=client,
        account_id=account_id,
        state_file=args.state,
        dry_run=not args.execute,
        tickers=tickers,
        strategy=args.strategy,
        live_mode=live_mode,
        telegram=telegram,
    )


def main():
    args = _parse_args()
    _setup_logging(args.log_level)
    log = logging.getLogger("live")

    # Garde-fou anti-VPN sur le trafic Topstep (ROADMAP_SOLO invariant #8).
    # Refuse de démarrer si Tailscale est configuré en exit-node (IP publique
    # dans la plage 100.64.0.0/10). Best-effort si pas de réseau.
    check_no_vpn_on_topstep()

    if args.daemon:
        _acquire_pid_lock()

    if not args.execute:
        log.warning("Mode DRY-RUN actif — aucun ordre réel ne sera passé")
        log.warning("Ajoutez --execute pour passer en mode réel")

    runner = _build_runner(args)

    if args.tick:
        runner.run_tick()
        return

    # ── Mode démon : boucle toutes les 15 minutes ─────────────────────────
    log.info("Mode démon démarré — Ctrl+C pour arrêter")
    while True:
        try:
            runner.run_tick()
        except KeyboardInterrupt:
            log.info("Arrêt demandé (KeyboardInterrupt)")
            break
        except Exception as exc:
            log.error("Erreur non fatale dans run_tick : %s", exc, exc_info=True)

        # Attente jusqu'à la prochaine bougie M15 (:01, :16, :31, :46)
        # Pendant l'attente, on poll Telegram toutes les 30 s pour répondre
        # immédiatement aux commandes (/status, /risk, /pause…) sans attendre
        # le prochain tick.
        import datetime as _dt
        import zoneinfo as _zi

        _NY = _zi.ZoneInfo("America/New_York")

        now = _dt.datetime.now(_dt.UTC).replace(tzinfo=None)
        minutes_to_next = 15 - (now.minute % 15)
        # vpc-v4 production : signal capté le plus tôt après la clôture M15
        # pour minimiser le délai entre fermeture de bougie et placement d'ordre.
        # 15 s suffit pour que TopstepX publie la barre fermée (vérifié sur les
        # ticks récents). Si la barre attendue n'est pas dans le DF, le tick
        # suivant la rattrapera (idempotence par custom_tag).
        seconds_offset = 15
        wait = minutes_to_next * 60 - now.second + seconds_offset
        log.info("Prochaine exécution dans %d s", wait)

        elapsed = 0
        poll_interval = 30  # secondes entre deux checks Telegram
        while elapsed < wait:
            sleep_chunk = min(poll_interval, wait - elapsed)
            time.sleep(sleep_chunk)
            elapsed += sleep_chunk
            if elapsed < wait:
                try:
                    now_utc_poll = _dt.datetime.now(_dt.UTC).replace(tzinfo=None)
                    today_str = _dt.datetime.now(_dt.UTC).astimezone(_NY).date().isoformat()
                    now_ny_str = (
                        _dt.datetime.now(_dt.UTC).astimezone(_NY).strftime("%Y-%m-%d %H:%M NY")
                    )
                    # Drain WS d'abord (fast path, no-op si realtime désactivé)
                    runner._drain_realtime()
                    # Drain Market Hub (buffer M1 — Phase C, no-op si OFF)
                    runner._drain_market_realtime()
                    # Sync broker REST pour avoir l'état réel (fills, clôtures)
                    runner._sync_broker(now_utc_poll)
                    # Persiste immédiatement : fills/closes détectés ici
                    # seraient perdus si _load_state() tourne au prochain tick.
                    runner._save_state()
                    rm_snap = {
                        **runner.rm.status(),
                        **runner._get_broker_day_summary(now_utc_poll),
                    }
                    runner.tg.check_commands(
                        placed_tags=runner.state.get("placed_tags", {}),
                        rm_status=rm_snap,
                        now_ny=now_ny_str,
                        on_pause=lambda: runner.set_paused(True),
                        on_resume=lambda: runner.set_paused(False),
                        paused=runner.is_paused(),
                        today_str=today_str,
                    )
                except Exception as exc:
                    log.debug("Poll Telegram entre ticks : %s", exc)


if __name__ == "__main__":
    main()
