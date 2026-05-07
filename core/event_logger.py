"""
EventLogger — journal d'événements trading persistant.

Écrit dans logs/trading_events.log (append, flush immédiat).
Survit aux redémarrages du daemon — ne perd aucun événement.

Seuls les événements significatifs sont enregistrés :
  SIGNAL   → signal détecté
  ORDRE    → ordre limite placé au broker
  FILL     → fill confirmé (position ouverte)
  CLÔTURE  → position fermée (TP/SL/TE/market) avec P&L
  ANNULÉ   → ordre annulé (fin session, timeout, erreur)
  BLOQUÉ   → signal rejeté par le risk manager
  SESSION  → démarrage / fin de session
  WARN     → limite journalière approchée, pause consec-loss
  ERROR    → erreur système (API, réseau, parsing)
  CRITICAL → limite Topstep franchie
"""

from datetime import datetime, timezone
from pathlib import Path
import logging

_py_log = logging.getLogger("event_logger")


class EventLogger:

    def __init__(self, path: str = "logs/trading_events.log"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ─────────────────────────────────────────────────────────────────────
    # Écriture bas niveau
    # ─────────────────────────────────────────────────────────────────────

    def _write(self, level: str, event: str, **kwargs):
        now     = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        details = "  ".join(f"{k}={v}" for k, v in kwargs.items() if v is not None)
        line    = f"{now}  [{level:<8}]  {event}"
        if details:
            line += f"  —  {details}"
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as exc:
            _py_log.error("EventLogger write failed: %s", exc)

    # ─────────────────────────────────────────────────────────────────────
    # Événements métier
    # ─────────────────────────────────────────────────────────────────────

    def signal(self, ticker: str, strategy: str, direction: str,
               entry: float, sl: float, tp: float, rr: float, n_ct: int):
        self._write("SIGNAL",
                    f"[{ticker}] {strategy} {direction.upper()} @ {entry}",
                    sl=sl, tp=tp, rr=rr, n_ct=n_ct)

    def order_placed(self, tag: str, order_id, dry_run: bool = False):
        mode = "DRY-RUN" if dry_run else "LIVE"
        self._write("ORDRE", f"Placé  {tag}",
                    order_id=order_id, mode=mode)

    def order_failed(self, tag: str, reason: str):
        self._write("ERROR", f"Placement échoué  {tag}", raison=reason)

    def order_cancelled(self, tag: str, reason: str = "fin de session"):
        self._write("ANNULÉ", tag, raison=reason)

    def fill(self, tag: str, ticker: str, direction: str,
             entry: float, fills_today: int, fills_max: int):
        self._write("FILL",
                    f"[{ticker}] {tag}  {direction.upper()} @ {entry}",
                    fills=f"{fills_today}/{fills_max}")

    def close(self, tag: str, ticker: str, pnl: float,
              session_pnl: float, cum_pnl: float):
        self._write("CLÔTURE",
                    f"[{ticker}] {tag}",
                    pnl=f"${pnl:+.0f}",
                    session=f"${session_pnl:+.0f}",
                    cum=f"${cum_pnl:+.0f}")

    def risk_blocked(self, ticker: str, tag: str, reason: str):
        self._write("BLOQUÉ", f"[{ticker}] {tag}", raison=reason)

    def risk_breach(self, reason: str):
        self._write("CRITICAL", "LIMITE TOPSTEP FRANCHIE", raison=reason)

    def daily_warning(self, day_pnl: float, limit: float):
        self._write("WARN", "Limite journalière approchée",
                    pnl=f"${day_pnl:.0f}",
                    limite=f"${limit:.0f}",
                    pct=f"{abs(day_pnl)/limit*100:.0f}%")

    def consec_loss_pause(self, n_days: int):
        self._write("WARN",
                    f"Pause consec-loss activée ({n_days} jours perdants)")

    def error(self, context: str, exc):
        self._write("ERROR", f"Erreur système  {context}", detail=str(exc))

    def session_start(self, date_str: str, tickers: list, mode: str):
        self._write("SESSION", f"Démarrage  {date_str}",
                    actifs=",".join(tickers), mode=mode)

    def session_end(self, date_str: str, session_pnl: float, n_fills: int):
        self._write("SESSION", f"Fin  {date_str}",
                    session_pnl=f"${session_pnl:+.0f}", fills=n_fills)
