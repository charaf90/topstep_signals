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

    # ─────────────────────────────────────────────────────────────────────
    # Challenge — sizing adaptatif Topstep
    # ─────────────────────────────────────────────────────────────────────

    def sizing_decision(self, strategy: str, ticker: str,
                        risk_static: float, risk_adaptive,
                        risk_applied: float, factors: dict = None):
        """
        Log d'une décision de sizing adaptatif. risk_adaptive peut être None
        (mode désactivé). factors est un dict des facteurs intermédiaires
        utilisés par la formule (cum_pnl, days_left, lockin, etc.).
        """
        kw = {
            "static": f"${risk_static:.0f}",
            "applied": f"${risk_applied:.0f}",
        }
        if risk_adaptive is not None:
            kw["adaptive"] = f"${risk_adaptive:.0f}"
        if factors:
            for k in ("cum_pnl", "days_left", "distance_target",
                      "dd_cap", "daily_cap", "lockin"):
                v = factors.get(k)
                if v is not None:
                    kw[k] = (f"${v:.0f}" if k.endswith("_pnl") or k.endswith("_cap")
                             or k in ("distance_target",) else f"{v:.2f}")
        self._write("SIZING", f"[{ticker}] {strategy}", **kw)

    def monthly_reset(self, when_iso: str, prev_cum_pnl: float,
                      prev_peak_pnl: float):
        """Log du reset mensuel du challenge (réinit compte Topstep)."""
        self._write("RESET",
                    f"Reset mensuel challenge",
                    when=when_iso,
                    prev_cum=f"${prev_cum_pnl:+.0f}",
                    prev_peak=f"${prev_peak_pnl:+.0f}")

    def challenge_bypass(self, ticker: str, risk_applied: float,
                         reason: str = "USER_DAILY_LOSS_MAX bypassé"):
        """Notification du bypass de la limite utilisateur en mode challenge."""
        self._write("WARN",
                    f"[{ticker}] CHALLENGE bypass",
                    risk=f"${risk_applied:.0f}",
                    raison=reason)

    def error(self, context: str, exc):
        self._write("ERROR", f"Erreur système  {context}", detail=str(exc))

    def session_start(self, date_str: str, tickers: list, mode: str):
        self._write("SESSION", f"Démarrage  {date_str}",
                    actifs=",".join(tickers), mode=mode)

    def session_end(self, date_str: str, session_pnl: float, n_fills: int):
        self._write("SESSION", f"Fin  {date_str}",
                    session_pnl=f"${session_pnl:+.0f}", fills=n_fills)

    # ─────────────────────────────────────────────────────────────────────
    # Realtime (SignalR User Hub) — lifecycle et santé connexion WS
    # ─────────────────────────────────────────────────────────────────────
    # Le mode realtime est OFF par défaut (config PROJECTX_REALTIME_ENABLED).
    # Ces méthodes ne sont appelées que si le WS client est instancié.

    def realtime_starting(self):
        self._write("INFO", "Realtime WS démarrage")

    def realtime_connected(self):
        self._write("INFO", "Realtime WS connecté")

    def realtime_disconnected(self, reason: str = ""):
        self._write("WARN", "Realtime WS déconnecté", raison=reason or "?")

    def realtime_event(self, kind: str, tag: str = None):
        # Gated par PROJECTX_REALTIME_DEBUG_EVENTS dans le runner —
        # ne pas appeler en mode prod, sinon spamme le log.
        self._write("INFO", f"WS {kind}", tag=tag)

    def realtime_event_error(self, kind: str, err: str):
        self._write("ERROR", f"WS event {kind} échec", detail=err)

    def realtime_silence(self, age_s: float):
        self._write("WARN", "Realtime WS silencieux", age_s=f"{age_s:.0f}")

    def realtime_dropped(self, n: int):
        self._write("WARN", "Realtime queue saturée", dropped=n)
