"""
Intégration Telegram — 3 niveaux :

  Niveau 1 (Unidirectionnel) :
    • Alertes trades     : signal détecté, ordre placé, fill, clôture (TP/SL/TE)
    • Alertes risk       : blocage RM, pause consec-loss, limites approchantes,
                           breach Topstep
    • Monitoring système : erreurs API, perte de connexion, rejets d'ordre

  Niveau 2 (Automatisé) :
    • Bilan de session   : envoyé automatiquement à 16h30 NY

  Niveau 3 (Bidirectionnel) :
    • /status            : état instantané du portefeuille (positions, P&L, slack)

Toutes les méthodes `notify_*` / `send_*` sont silencieuses sur erreur réseau :
elles loggent mais ne propagent pas — le trading loop n'est jamais interrompu
par une panne Telegram.
"""

import logging
import time
from datetime import datetime
from typing import Dict, List, Optional

import requests

_log = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/{method}"

# ─────────────────────────────────────────────────────────────────────────────
# Formatters (fonctions pures — faciles à tester / surcharger)
# ─────────────────────────────────────────────────────────────────────────────

def _esc(text: str) -> str:
    """Échappe les caractères spéciaux HTML Telegram."""
    return (str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


def fmt_signal(signal: Dict) -> str:
    strat = _esc(signal.get("strategy", "?").upper())
    ticker = _esc(signal.get("ticker", "?"))
    direction = "LONG 🔼" if signal.get("direction") == "long" else "SHORT 🔽"
    entry  = signal.get("entry", 0)
    sl     = signal.get("sl", 0)
    tp     = signal.get("tp", 0)
    sl_d   = signal.get("sl_dist", 0)
    tp_d   = signal.get("tp_dist", 0)
    rr     = signal.get("rr", 0)
    n_ct   = signal.get("n_ct", 0)
    risk   = signal.get("risk", 0)
    return (
        f"⚡ <b>Signal détecté</b>\n"
        f"<b>{strat} {direction} {ticker}</b>\n"
        f"Entry : <code>{entry:.2f}</code>\n"
        f"SL : <code>{sl:.2f}</code>  ({sl_d:.2f} pts)\n"
        f"TP : <code>{tp:.2f}</code>  (+{tp_d:.2f} pts)\n"
        f"RR : {rr:.2f} | {n_ct} ct | risque ${risk:.0f}"
    )


def fmt_order_placed(tag: str, signal: Dict, order_id: int,
                     dry_run: bool = False) -> str:
    strat  = _esc(signal.get("strategy", "?").upper())
    ticker = _esc(signal.get("ticker", "?"))
    direct = "LONG 🔼" if signal.get("direction") == "long" else "SHORT 🔽"
    entry  = signal.get("entry", 0)
    sl_t   = signal.get("_sl_ticks", "?")
    tp_t   = signal.get("_tp_ticks", "?")
    n_ct   = signal.get("n_ct", 0)
    sim    = " <i>[DRY-RUN]</i>" if dry_run else ""
    return (
        f"📋 <b>Ordre placé</b>{sim}\n"
        f"<b>{strat} {direct} {ticker}</b>\n"
        f"Entry : <code>{entry:.2f}</code>\n"
        f"SL : {sl_t} ticks | TP : {tp_t} ticks\n"
        f"{n_ct} contrat(s) | id #{order_id}"
    )


def fmt_fill(tag: str, info: Dict, fills_today: int, fills_max: int) -> str:
    strat  = _esc(info.get("strategy", "?").upper())
    ticker = _esc(info.get("ticker", "?"))
    direct = "LONG 🔼" if info.get("direction") == "long" else "SHORT 🔽"
    entry  = info.get("entry", 0)
    return (
        f"🟢 <b>FILL confirmé</b>\n"
        f"<b>{strat} {direct} {ticker}</b>\n"
        f"Prix : <code>{entry:.2f}</code>\n"
        f"Fills aujourd'hui : {fills_today}/{fills_max}"
    )


def fmt_close(tag: str, info: Dict, pnl: float,
              session_pnl: float, cum_pnl: float) -> str:
    ticker = _esc(info.get("ticker", "?"))
    strat  = _esc(info.get("strategy", "?").upper())
    direct = "LONG" if info.get("direction") == "long" else "SHORT"
    result = info.get("status", "CLOSED")
    # Icône selon le résultat (déduit du P&L car on n'a pas le result explicite ici)
    if pnl > 0:
        icon = "✅ TP"
    elif pnl < 0:
        icon = "❌ SL"
    else:
        icon = "⏱ TE"
    sign  = "+" if pnl >= 0 else ""
    s_sign = "+" if session_pnl >= 0 else ""
    c_sign = "+" if cum_pnl >= 0 else ""
    return (
        f"{icon} — <b>{ticker}</b> ({strat} {direct})\n"
        f"P&amp;L trade : <b>{sign}{pnl:.2f} $</b>\n"
        f"Session : {s_sign}{session_pnl:.2f} $ | Cum : {c_sign}{cum_pnl:.2f} $"
    )


def fmt_risk_blocked(ticker: str, tag: str, reason: str) -> str:
    return (
        f"⛔ <b>Ordre bloqué</b>\n"
        f"{_esc(ticker)} — {_esc(tag.split('_')[0])}\n"
        f"<i>{_esc(reason)}</i>"
    )


def fmt_risk_breach(reason: str) -> str:
    return (
        f"🚨 <b>LIMITE TOPSTEP FRANCHIE</b>\n"
        f"<code>{_esc(reason)}</code>\n"
        f"<b>Arrêt automatique des ordres.</b>"
    )


def fmt_daily_limit_warning(realized: float, limit: float,
                             fills_remaining: int) -> str:
    pct = abs(realized) / limit * 100
    return (
        f"⚠️ <b>Limite journalière approchante</b>\n"
        f"Perte réalisée : <b>{realized:+.2f} $</b> / {-limit:.0f} $ ({pct:.0f}%)\n"
        f"Fills restants autorisés : {fills_remaining}"
    )


def fmt_consec_loss_pause(days: int) -> str:
    return (
        f"⏸ <b>Pause automatique</b>\n"
        f"{days} jours perdants consécutifs — session suivante ignorée.\n"
        f"<i>(CONSEC_LOSS_PAUSE_DAYS={days})</i>"
    )


def fmt_system_error(context: str, error: str) -> str:
    return (
        f"🔴 <b>Erreur système</b>\n"
        f"Contexte : <code>{_esc(context)}</code>\n"
        f"<code>{_esc(str(error)[:300])}</code>"
    )


def fmt_session_start(date_str: str, tickers: List[str],
                      rm_status: Dict) -> str:
    cum   = rm_status.get("cum_pnl", 0.0)
    target= rm_status.get("target_remaining", 0.0)
    slack_d = rm_status.get("slack_daily", 0.0)
    slack_t = rm_status.get("slack_trail", 0.0)
    streak  = rm_status.get("consec_loss_days", 0)
    sign  = "+" if cum >= 0 else ""
    ticker_str = " | ".join(tickers)
    return (
        f"🔔 <b>Démarrage session</b> — {_esc(date_str)}\n"
        f"Actifs : {_esc(ticker_str)}\n"
        f"Cum challenge : {sign}{cum:.2f} $ | Restant : {target:.2f} $\n"
        f"Slack daily : {slack_d:.0f} $ | Trail : {slack_t:.0f} $"
        + (f"\n⚠️ Streak perdant : {streak} jour(s)" if streak > 0 else "")
    )


def fmt_session_report(date_str: str, placed_tags: Dict,
                       rm_status: Dict) -> str:
    # Statistiques des trades du jour (depuis placed_tags — best effort)
    closed = [v for v in placed_tags.values()
              if v.get("status") in ("CLOSED",) and
              _is_today(v.get("placed_at", ""), date_str)]
    n_fills_rm = sum(1 for v in placed_tags.values()
                     if v.get("fill_time") and _is_today(v.get("fill_time", ""), date_str))
    n_tp     = sum(1 for v in closed if (v.get("close_pnl") or 0) > 0)
    n_sl     = sum(1 for v in closed if (v.get("close_pnl") or 0) < 0)
    n_te     = sum(1 for v in closed if (v.get("close_pnl") or 0) == 0)

    # Données broker réelles (prioritaires sur le RM interne)
    broker_pnl  = rm_status.get("broker_day_pnl")
    broker_fees = rm_status.get("broker_day_fees")
    broker_bal  = rm_status.get("broker_balance")
    broker_fill = rm_status.get("broker_fills")

    pnl_day = broker_pnl  if broker_pnl  is not None else rm_status.get("realized_day_pnl", 0.0)
    n_fills = broker_fill if broker_fill is not None else n_fills_rm

    cum     = rm_status.get("cum_pnl", 0.0)
    target  = rm_status.get("target_remaining", 0.0)
    slack_d = rm_status.get("slack_daily", 0.0)
    slack_t = rm_status.get("slack_trail", 0.0)

    sign_d = "+" if pnl_day >= 0 else ""
    sign_c = "+" if cum >= 0 else ""
    icon_d = "✅" if pnl_day >= 0 else "🔴"

    # Ligne frais broker si disponible
    fees_line = (f"  (frais : -{broker_fees:.2f} $)\n" if broker_fees is not None else "")
    bal_line  = (f"Solde compte : {broker_bal:,.2f} $\n" if broker_bal is not None else "")

    return (
        f"📊 <b>Bilan session</b> — {_esc(date_str)}\n"
        f"{'─'*24}\n"
        f"Fills : {n_fills} | TP : {n_tp} | SL : {n_sl} | TE : {n_te}\n"
        f"P&amp;L session : {icon_d} <b>{sign_d}{pnl_day:.2f} $</b>\n"
        f"{fees_line}"
        f"{bal_line}"
        f"{'─'*24}\n"
        f"Cum challenge : <b>{sign_c}{cum:.2f} $</b>\n"
        f"Objectif restant : {target:.2f} $\n"
        f"Slack daily : {slack_d:.0f} $ | Trail : {slack_t:.0f} $"
    )


def fmt_status(placed_tags: Dict, rm_status: Dict, now_ny: str) -> str:
    pending  = [(t, v) for t, v in placed_tags.items()
                if v.get("status") == "PENDING"]
    active   = [(t, v) for t, v in placed_tags.items()
                if v.get("status") == "ACTIVE"]
    cum      = rm_status.get("cum_pnl", 0.0)
    day_pnl  = rm_status.get("realized_day_pnl", 0.0)
    fills_d  = rm_status.get("daily_fills_count", 0)
    fills_max= rm_status.get("user_max_trades_per_day", 3)
    fills_r  = rm_status.get("daily_fills_remaining", fills_max - fills_d)
    slack_d  = rm_status.get("slack_daily", 0.0)
    slack_t  = rm_status.get("slack_trail", 0.0)
    target   = rm_status.get("target_remaining", 0.0)
    streak   = rm_status.get("consec_loss_days", 0)

    pos_lines = ""
    for tag, v in active:
        ticker = v.get("ticker", "?")
        d      = "LONG 🔼" if v.get("direction") == "long" else "SHORT 🔽"
        strat  = v.get("strategy", "?")
        entry  = v.get("entry", 0)
        pos_lines += f"  • {_esc(strat)} {_esc(ticker)} {d} @ {entry:.2f}\n"

    ord_lines = ""
    for tag, v in pending:
        ticker = v.get("ticker", "?")
        d      = "LONG 🔼" if v.get("direction") == "long" else "SHORT 🔽"
        strat  = v.get("strategy", "?")
        entry  = v.get("entry", 0)
        ord_lines += f"  • {_esc(strat)} {_esc(ticker)} {d} @ {entry:.2f}\n"

    broker_balance = rm_status.get("broker_balance")
    broker_pnl     = rm_status.get("broker_day_pnl")
    broker_fees    = rm_status.get("broker_day_fees")
    broker_fills   = rm_status.get("broker_fills")
    broker_open    = rm_status.get("broker_n_open")

    sign_d = "+" if day_pnl >= 0 else ""
    sign_c = "+" if cum >= 0 else ""
    s = (
        f"📈 <b>État portefeuille</b>\n"
        f"<i>{_esc(now_ny)}</i>\n"
        f"{'─'*24}\n"
    )
    if active:
        s += f"<b>Positions ({len(active)}) :</b>\n{pos_lines}"
    else:
        s += "Positions : aucune\n"
    if pending:
        s += f"<b>Ordres en attente ({len(pending)}) :</b>\n{ord_lines}"
    else:
        s += "Ordres : aucun\n"
    s += f"{'─'*24}\n"

    # Données broker réelles (source de vérité)
    if broker_pnl is not None:
        b_sign = "+" if broker_pnl >= 0 else ""
        bal_line = (f"  Solde compte : <b>{broker_balance:,.2f} $</b>\n"
                    if broker_balance is not None else "")
        s += (
            f"📊 <b>Broker (réel) :</b>\n"
            f"{bal_line}"
            f"  P&amp;L net jour : <b>{b_sign}{broker_pnl:.2f} $</b>"
            f"  <i>(frais : -{broker_fees:.2f} $)</i>\n"
            f"  Trades fermés : {broker_fills} | Positions : {broker_open}\n"
            f"{'─'*24}\n"
        )

    s += (
        f"Système — P&amp;L : {sign_d}{day_pnl:.2f} $ | Cum : {sign_c}{cum:.2f} $\n"
        f"Slack daily : {slack_d:.0f} $ | Trail : {slack_t:.0f} $\n"
        f"Objectif restant : {target:.2f} $"
    )
    if streak > 0:
        s += f"\n⚠️ Streak perdant : {streak} jour(s)"
    return s


def _is_today(iso_str: str, date_str: str) -> bool:
    """Vérifie si un timestamp ISO commence par la date donnée (YYYY-MM-DD)."""
    return iso_str.startswith(date_str) if iso_str else False


# ─────────────────────────────────────────────────────────────────────────────
# Client Telegram
# ─────────────────────────────────────────────────────────────────────────────

class TelegramBot:
    """
    Client Telegram léger (synchrone, pas de dépendance python-telegram-bot).

    Toutes les méthodes `notify_*` et `send_*` capturent les exceptions réseau
    et les loggent — le trading loop n'est jamais interrompu par Telegram.
    """

    def __init__(self, token: str, chat_id: str,
                 enabled: bool = True,
                 level_trades:  bool = True,
                 level_risk:    bool = True,
                 level_system:  bool = True,
                 level_report:  bool = True,
                 level_commands:bool = True):
        self.token          = token
        self.chat_id        = str(chat_id)
        self.enabled        = enabled and bool(token) and bool(chat_id)
        self.level_trades   = level_trades
        self.level_risk     = level_risk
        self.level_system   = level_system
        self.level_report   = level_report
        self.level_commands = level_commands
        self._session       = requests.Session()
        self._last_update_id: int = -1   # pour getUpdates polling
        if self.enabled:
            _log.info("TelegramBot actif (chat_id=%s)", self.chat_id)
        else:
            _log.info("TelegramBot désactivé (token ou chat_id manquant)")

    # ─────────────────────────────────────────────────────────────────────
    # Transport bas niveau
    # ─────────────────────────────────────────────────────────────────────

    def _url(self, method: str) -> str:
        return _API.format(token=self.token, method=method)

    def send(self, text: str) -> bool:
        """
        Envoie un message HTML. Retourne True si OK.
        Ne propage jamais d'exception.
        """
        if not self.enabled:
            return False
        try:
            r = self._session.post(
                self._url("sendMessage"),
                json={
                    "chat_id":    self.chat_id,
                    "text":       text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=8,
            )
            data = r.json()
            if not data.get("ok"):
                _log.warning("Telegram sendMessage: %s", data.get("description", "?"))
                return False
            return True
        except Exception as exc:
            _log.warning("Telegram sendMessage échoué : %s", exc)
            return False

    def _get_updates(self) -> List[Dict]:
        """Poll getUpdates (non-bloquant, timeout=0). Retourne [] en cas d'erreur."""
        if not self.enabled or not self.level_commands:
            return []
        try:
            params = {
                "offset":  self._last_update_id + 1,
                "limit":   10,
                "timeout": 0,
            }
            r = self._session.get(self._url("getUpdates"),
                                  params=params, timeout=5)
            data = r.json()
            if not data.get("ok"):
                return []
            updates = data.get("result", [])
            if updates:
                self._last_update_id = updates[-1]["update_id"]
            return updates
        except Exception as exc:
            _log.debug("Telegram getUpdates échoué : %s", exc)
            return []

    # ─────────────────────────────────────────────────────────────────────
    # Niveau 1 — Alertes trades
    # ─────────────────────────────────────────────────────────────────────

    def notify_signal(self, signal: Dict):
        if self.enabled and self.level_trades:
            self.send(fmt_signal(signal))

    def notify_order_placed(self, tag: str, signal: Dict, order_id: int,
                            dry_run: bool = False):
        if self.enabled and self.level_trades:
            self.send(fmt_order_placed(tag, signal, order_id, dry_run))

    def notify_fill(self, tag: str, info: Dict,
                    fills_today: int, fills_max: int):
        if self.enabled and self.level_trades:
            self.send(fmt_fill(tag, info, fills_today, fills_max))

    def notify_close(self, tag: str, info: Dict, pnl: float,
                     session_pnl: float, cum_pnl: float):
        if self.enabled and self.level_trades:
            self.send(fmt_close(tag, info, pnl, session_pnl, cum_pnl))

    # ─────────────────────────────────────────────────────────────────────
    # Niveau 1 — Alertes risk
    # ─────────────────────────────────────────────────────────────────────

    def notify_risk_blocked(self, ticker: str, tag: str, reason: str):
        if self.enabled and self.level_risk:
            self.send(fmt_risk_blocked(ticker, tag, reason))

    def notify_risk_breach(self, reason: str):
        if self.enabled and self.level_risk:
            self.send(fmt_risk_breach(reason))

    def notify_daily_limit_warning(self, realized: float, limit: float,
                                   fills_remaining: int):
        """Envoyé quand on dépasse 80% de la limite journalière."""
        if self.enabled and self.level_risk:
            self.send(fmt_daily_limit_warning(realized, limit, fills_remaining))

    def notify_consec_loss_pause(self, days: int):
        if self.enabled and self.level_risk:
            self.send(fmt_consec_loss_pause(days))

    # ─────────────────────────────────────────────────────────────────────
    # Niveau 1 — Monitoring système
    # ─────────────────────────────────────────────────────────────────────

    def notify_system_error(self, context: str, error):
        if self.enabled and self.level_system:
            self.send(fmt_system_error(context, error))

    def notify_session_start(self, date_str: str, tickers: List[str],
                              rm_status: Dict):
        if self.enabled and self.level_system:
            self.send(fmt_session_start(date_str, tickers, rm_status))

    # ─────────────────────────────────────────────────────────────────────
    # Niveau 2 — Bilan de session
    # ─────────────────────────────────────────────────────────────────────

    def send_session_report(self, date_str: str, placed_tags: Dict,
                            rm_status: Dict):
        if self.enabled and self.level_report:
            self.send(fmt_session_report(date_str, placed_tags, rm_status))

    # ─────────────────────────────────────────────────────────────────────
    # Niveau 3 — Commandes entrantes (/status)
    # ─────────────────────────────────────────────────────────────────────

    def check_commands(self, placed_tags: Dict, rm_status: Dict,
                       now_ny: str) -> int:
        """
        Interroge getUpdates, traite les commandes /status reçues.
        Retourne le nombre de commandes traitées.

        Appeler une fois par tick (non-bloquant).
        """
        if not self.enabled or not self.level_commands:
            return 0

        updates  = self._get_updates()
        handled  = 0
        for update in updates:
            msg = update.get("message") or update.get("edited_message")
            if not msg:
                continue
            text = (msg.get("text") or "").strip().lower()
            # On répond uniquement depuis le chat autorisé
            from_chat = str(msg.get("chat", {}).get("id", ""))
            if from_chat != self.chat_id:
                _log.debug("Message d'un chat inconnu (%s) ignoré", from_chat)
                continue
            if text.startswith("/status"):
                self.send(fmt_status(placed_tags, rm_status, now_ny))
                handled += 1
            elif text.startswith("/help"):
                self.send(
                    "📖 <b>Commandes disponibles</b>\n"
                    "/status — État instantané du portefeuille\n"
                    "/help   — Ce message"
                )
                handled += 1

        return handled

    def restore_update_id(self, update_id: int):
        """Restaure l'offset depuis l'état persistant au démarrage."""
        if update_id >= 0:
            self._last_update_id = update_id

    def current_update_id(self) -> int:
        return self._last_update_id
