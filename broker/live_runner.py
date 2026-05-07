"""
Runner de session live — exécution automatisée OPR + Fib sur Topstep via ProjectX.

Appeler `SessionRunner.run_tick()` toutes les 15 minutes (juste après la clôture
de chaque bougie M15, ex. à :01, :16, :31, :46).

Flux par tick :
  1. Chargement de l'état persistant (state/live_state.json)
  2. Roll de jour si nouvelle session (reset daily counters via PortfolioRiskManager)
  3. Fetch des barres 15m depuis l'API ProjectX
  4. Détection des signaux OPR (run_opr_day) et Fib (get_fib_live_signal)
  5. Filtre de corrélation (signal_selector.filter_correlated_signals)
  6. Pour chaque signal neuf → can_open → place_limit_order → register_open
  7. Sync broker : détection des fills/clôtures → register_fill / register_close
  8. Fin de session OPR (16h30 NY) → annulation ordres + clôture positions
  9. Sauvegarde état
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
from zoneinfo import ZoneInfo

from config import (
    INSTRUMENTS, RISK_PER_TRADE_USD,
    OPR_ENABLED, OPR_TIMEZONE, OPR_SESSION_END,
    FIB_ENABLED, FIB_MAX_HOLD_BARS,
    PROJECTX_SYMBOLS, PROJECTX_BARS_WARMUP, PROJECTX_LIVE_MODE,
    LIVE_STATE_FILE,
    YM1_ENABLED,
    USER_DAILY_LOSS_MAX, USER_MAX_TRADES_PER_DAY,
    CONSEC_LOSS_PAUSE_DAYS,
)
from core.opr import run_opr_day
from core.strategy_fib import get_fib_live_signal
from core.risk_portfolio import PortfolioRiskManager, _Order
from core.signal_selector import filter_correlated_signals
from core.event_logger import EventLogger
from broker.projectx_client import ProjectXClient
from broker.telegram_bot import TelegramBot

_log   = logging.getLogger(__name__)
_evlog = EventLogger("logs/trading_events.log")

_NY_TZ = ZoneInfo(OPR_TIMEZONE)

# Statuts d'un ordre dans le state
_ST_PENDING   = "PENDING"    # ordre limite pas encore filé
_ST_ACTIVE    = "ACTIVE"     # position ouverte (fill confirmé)
_ST_CANCELLED = "CANCELLED"  # annulé ou expiré sans fill
_ST_CLOSED    = "CLOSED"     # position fermée (TP/SL/TE/market)


# ─────────────────────────────────────────────────────────────────────────────
# Sérialisation / déserialisation du PortfolioRiskManager
# ─────────────────────────────────────────────────────────────────────────────

def _rm_to_dict(rm: PortfolioRiskManager) -> Dict:
    def _order_dict(o: _Order) -> Dict:
        return {
            "risk_usd":  o.risk_usd,
            "opened_at": o.opened_at.isoformat() if o.opened_at else None,
            "metadata":  o.metadata,
        }
    return {
        "cum_pnl":           rm.cum_pnl,
        "peak_pnl":          rm.peak_pnl,
        "realized_day_pnl":  rm.realized_day_pnl,
        "current_day":       rm.current_day.isoformat() if rm.current_day else None,
        "consec_loss_days":  rm.consec_loss_days,
        "daily_fills_count": rm.daily_fills_count,
        "pending_orders":    {k: _order_dict(v) for k, v in rm.pending_orders.items()},
        "active_positions":  {k: _order_dict(v) for k, v in rm.active_positions.items()},
    }


def _rm_from_dict(data: Dict) -> PortfolioRiskManager:
    rm = PortfolioRiskManager(
        cum_pnl          = float(data.get("cum_pnl", 0.0)),
        peak_pnl         = float(data.get("peak_pnl", 0.0)),
        realized_day_pnl = float(data.get("realized_day_pnl", 0.0)),
        consec_loss_days = int(data.get("consec_loss_days", 0)),
        daily_fills_count= int(data.get("daily_fills_count", 0)),
    )
    if data.get("current_day"):
        rm.current_day = date.fromisoformat(data["current_day"])

    def _load_order(d: Dict) -> _Order:
        opened_at = None
        if d.get("opened_at"):
            opened_at = datetime.fromisoformat(d["opened_at"])
        return _Order(risk_usd  = float(d.get("risk_usd", 0.0)),
                      opened_at = opened_at,
                      metadata  = d.get("metadata", {}))

    for k, v in data.get("pending_orders", {}).items():
        rm.pending_orders[k] = _load_order(v)
    for k, v in data.get("active_positions", {}).items():
        rm.active_positions[k] = _load_order(v)
    return rm


# ─────────────────────────────────────────────────────────────────────────────
# Conversion barres API → DataFrame (index UTC naïf — cohérent avec codebase)
# ─────────────────────────────────────────────────────────────────────────────

def _bars_to_df(bars: List[Dict]) -> pd.DataFrame:
    if not bars:
        return pd.DataFrame()
    df = pd.DataFrame(bars)
    df = df.rename(columns={"t": "datetime", "o": "open", "h": "high",
                             "l": "low",      "c": "close", "v": "volume"})
    df["datetime"] = (pd.to_datetime(df["datetime"], utc=True)
                      .dt.tz_localize(None))   # UTC naïf
    df = df.set_index("datetime").sort_index()
    df = df[["open", "high", "low", "close", "volume"]].astype(float)
    df["volume"] = df["volume"].astype(int)
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Conversion distance SL/TP points → ticks
# ─────────────────────────────────────────────────────────────────────────────

def _pts_to_ticks(distance_pts: float, ticker: str) -> int:
    tick = INSTRUMENTS[ticker]["tick_size"]
    return max(1, round(distance_pts / tick))


# ─────────────────────────────────────────────────────────────────────────────
# État "is OPR session over?"
# ─────────────────────────────────────────────────────────────────────────────

def _opr_session_over(now_utc: datetime) -> bool:
    """Vrai si l'heure NY courante est ≥ OPR_SESSION_END (16h30 NY)."""
    now_ny = now_utc.replace(tzinfo=timezone.utc).astimezone(_NY_TZ)
    h, m   = OPR_SESSION_END
    return (now_ny.hour, now_ny.minute) >= (h, m)


def _current_day_ny(now_utc: datetime) -> date:
    return now_utc.replace(tzinfo=timezone.utc).astimezone(_NY_TZ).date()


# ─────────────────────────────────────────────────────────────────────────────
# SessionRunner
# ─────────────────────────────────────────────────────────────────────────────

class SessionRunner:
    """
    Orchestre les stratégies OPR + Fib sur le broker ProjectX.

    Paramètres :
      client     : ProjectXClient authentifié
      account_id : identifiant du compte TopstepX
      state_file : chemin du fichier JSON d'état (créé si absent)
      dry_run    : si True, simule sans passer d'ordres réels
      tickers    : liste de tickers actifs (None = tous activés en config)
      strategy   : "opr_fib" | "opr" | "fib"
    """

    def __init__(
        self,
        client:     ProjectXClient,
        account_id: int,
        state_file: str = LIVE_STATE_FILE,
        dry_run:    bool = True,
        tickers:    Optional[List[str]] = None,
        strategy:   str = "opr_fib",
        live_mode:  bool = PROJECTX_LIVE_MODE,
        telegram:   Optional[TelegramBot] = None,
    ):
        self.client     = client
        self.account_id = account_id
        self.state_file = Path(state_file)
        self.dry_run    = dry_run
        self.strategy   = strategy
        self.live_mode  = live_mode   # False = simulated (challenge), True = funded
        self.tickers    = tickers or self._default_tickers()
        self.state: Dict = {}
        self.rm:  PortfolioRiskManager = PortfolioRiskManager()
        self.tg: TelegramBot = telegram or TelegramBot("", "")  # noop si absent

    # ─────────────────────────────────────────────────────────────────────
    # Tickers actifs
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def _default_tickers() -> List[str]:
        tickers = ["NQ1", "MES1"]
        if YM1_ENABLED:
            tickers.append("YM1")
        return tickers

    # ─────────────────────────────────────────────────────────────────────
    # État persistant
    # ─────────────────────────────────────────────────────────────────────

    def _load_state(self):
        """Charge l'état depuis le fichier JSON. Initialise si absent."""
        if self.state_file.exists():
            try:
                raw = self.state_file.read_text(encoding="utf-8")
                self.state = json.loads(raw)
                self.rm = _rm_from_dict(self.state.get("risk_state", {}))
                # Restaure l'offset Telegram getUpdates
                tg_offset = self.state.get("telegram_update_id", -1)
                self.tg.restore_update_id(tg_offset)
                return
            except Exception as exc:
                _log.warning("Lecture state échouée (%s) — réinitialisation", exc)
        # État vide
        self.state = {
            "date":                  None,
            "account_id":            self.account_id,
            "contracts":             {},
            "placed_tags":           {},
            "session_report_sent":   None,   # date YYYY-MM-DD du dernier rapport envoyé
            "session_start_notified":None,   # date YYYY-MM-DD du dernier start notifié
            "telegram_update_id":    -1,
        }
        self.rm = PortfolioRiskManager()

    def _save_state(self):
        """Persiste l'état dans le fichier JSON."""
        self.state["risk_state"]        = _rm_to_dict(self.rm)
        self.state["account_id"]        = self.account_id
        self.state["telegram_update_id"]= self.tg.current_update_id()
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(
            json.dumps(self.state, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    # ─────────────────────────────────────────────────────────────────────
    # Contrats front-month (découverte + cache dans state)
    # ─────────────────────────────────────────────────────────────────────

    def _ensure_contracts(self):
        """
        Découvre les contractIds front-month pour chaque ticker actif.
        Cache le résultat dans self.state["contracts"] pour ne pas
        interroger l'API à chaque tick.
        Rafraîchit si le state date d'un autre jour (roll du contrat).
        """
        today = _current_day_ny(datetime.utcnow()).isoformat()
        contracts = self.state.get("contracts", {})
        needs_refresh = (
            self.state.get("date") != today
            or not all(t in contracts for t in self.tickers)
        )
        if not needs_refresh:
            return

        _log.info("Découverte des contrats front-month (live_mode=%s)…",
                  self.live_mode)
        for ticker in self.tickers:
            symbol = PROJECTX_SYMBOLS.get(ticker)
            if symbol is None:
                _log.warning("Pas de symbole ProjectX configuré pour %s", ticker)
                continue
            contract = self.client.search_contract(symbol, live=self.live_mode)
            if contract is None:
                _log.error("Contrat introuvable pour %s (%s)", ticker, symbol)
                continue
            # L'API retourne : {"id": "CON.F.US.MES.M26", "name": "MES", …}
            # On stocke l'id brut (string ou int selon la version API).
            contracts[ticker] = contract.get("id") or contract.get("contractId")
            _log.info("  %s → %s", ticker, contracts[ticker])

        self.state["contracts"] = contracts
        self.state["date"]      = today

    def _contract_id(self, ticker: str) -> Optional[str]:
        return self.state.get("contracts", {}).get(ticker)

    # ─────────────────────────────────────────────────────────────────────
    # Fetch barres
    # ─────────────────────────────────────────────────────────────────────

    def _fetch_bars(self, ticker: str, n_bars: int = PROJECTX_BARS_WARMUP
                    ) -> Optional[pd.DataFrame]:
        """
        Fetch les n_bars dernières barres 15m depuis l'API ProjectX.
        Retourne un DataFrame UTC naïf (cohérent avec tout le codebase).
        """
        cid = self._contract_id(ticker)
        if cid is None:
            _log.error("Pas de contractId pour %s — impossible de fetcher", ticker)
            return None

        now = datetime.utcnow()
        # Remonte suffisamment loin pour couvrir n_bars barres de marché
        # (~2 barres/heure, ~8h/jour → 1 mois ≈ 960 barres; on prend large).
        start = now - timedelta(days=max(1, n_bars // 25))
        bars = self.client.get_bars(
            contract_id     = cid,
            start_dt        = start,
            end_dt          = now,
            unit            = 2,          # Minute
            unit_number     = 15,
            limit           = n_bars,
            live            = self.live_mode,
            include_partial = False,
        )
        if not bars:
            _log.warning("Aucune barre reçue pour %s", ticker)
            return None

        df = _bars_to_df(bars)
        _log.debug("%s : %d barres 15m chargées (de %s à %s)",
                   ticker, len(df), df.index[0], df.index[-1])
        return df

    # ─────────────────────────────────────────────────────────────────────
    # Détection des signaux stratégie
    # ─────────────────────────────────────────────────────────────────────

    def _get_opr_signal(self, df: pd.DataFrame, ticker: str,
                        day_ny: pd.Timestamp) -> Optional[Dict]:
        """
        Joue run_opr_day sur les barres du jour et retourne le dernier signal
        non encore filé (si existant), sous la forme d'un dict avec le champ
        supplémentaire "tag" utilisé pour l'idempotence.

        Retourne None si :
          • OPR désactivé, aucun trigger aujourd'hui, ou dernier trade clos.
        """
        if not OPR_ENABLED:
            return None

        signals, trades, _zone = run_opr_day(df, ticker, day_ny)
        if not signals:
            return None

        last_idx = len(signals) - 1
        last_sig = signals[last_idx]
        last_trade = trades[last_idx] if last_idx < len(trades) else {}

        # Le signal est "à placer" uniquement s'il n'a pas encore de résultat
        # définitif (i.e. le trade est encore NOT_FILLED ou n'existe pas encore
        # côté broker — c'est le cas juste après le trigger, avant le fill).
        if last_trade.get("result") not in (None, "NOT_FILLED"):
            return None  # déjà fermé (TP/SL/TE)

        date_str = day_ny.strftime("%Y%m%d")
        tag = f"OPR_{ticker}_{date_str}_{last_sig['direction']}"
        return {**last_sig, "tag": tag}

    def _get_fib_signal(self, df: pd.DataFrame, ticker: str) -> Optional[Dict]:
        """
        Appelle get_fib_live_signal et retourne le signal si état PENDING.
        (État ACTIVE = position déjà filée, gérée par le broker.)
        """
        if not FIB_ENABLED:
            return None

        live_state = get_fib_live_signal(df, ticker)
        if live_state is None:
            return None
        if live_state["state"] != "PENDING":
            return None   # ACTIVE : broker gère les brackets

        sig = live_state["signal"]
        imp_key = live_state["impulse_key"]
        today = _current_day_ny(datetime.utcnow()).strftime("%Y%m%d")
        tag = f"FIB_{ticker}_{today}_{imp_key}"
        return {**sig, "tag": tag}

    # ─────────────────────────────────────────────────────────────────────
    # Placement d'ordre
    # ─────────────────────────────────────────────────────────────────────

    def _already_placed(self, tag: str) -> bool:
        """Vrai si l'ordre avec ce tag a déjà été envoyé au broker."""
        return tag in self.state.get("placed_tags", {})

    def _mark_tag_failed(self, tag: str, signal: Dict, cid: str):
        """
        Enregistre le tag comme CANCELLED pour empêcher tout retry.
        Appelé quand le placement échoue côté API — le broker peut avoir
        l'ordre ou non, mais on ne doit pas retenter indéfiniment.
        """
        self.state.setdefault("placed_tags", {})[tag] = {
            "order_id":    None,
            "strategy":    signal.get("strategy", "?"),
            "ticker":      signal["ticker"],
            "contract_id": cid,
            "direction":   signal["direction"],
            "entry":       float(signal["entry"]),
            "sl":          float(signal["sl"]),
            "tp":          float(signal["tp"]),
            "n_ct":        int(signal["n_ct"]),
            "risk":        float(signal.get("risk", 0)),
            "status":      _ST_CANCELLED,
            "placed_at":   datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "fill_time":   None,
            "close_pnl":   None,
        }

    def _find_order_id_by_tag(self, tag: str) -> Optional[int]:
        """
        Cherche un ordre ouvert correspondant au custom tag.
        Utilisé pour récupérer l'orderId quand le placement a échoué côté API
        mais que le broker a quand même créé l'ordre (ex: custom tag already in use).
        """
        try:
            orders = self.client.get_open_orders(self.account_id)
            for o in orders:
                if o.get("customTag") == tag or o.get("tag") == tag:
                    return int(o["id"])
        except Exception as exc:
            _log.warning("_find_order_id_by_tag(%s) échoué : %s", tag, exc)
        return None

    def _place_order(self, signal: Dict) -> bool:
        """
        Vérifie can_open, place le limit order avec brackets SL/TP, et
        enregistre l'ordre dans le risk manager et le state.

        Retourne True si l'ordre est passé (ou simulé en dry_run).
        """
        tag    = signal["tag"]
        ticker = signal["ticker"]
        cid    = self._contract_id(ticker)
        if cid is None:
            _log.error("Pas de contractId pour %s — ordre non placé", ticker)
            return False

        # ── Vérification risk manager ─────────────────────────────────────
        risk = float(signal.get("risk", RISK_PER_TRADE_USD))
        ok, reason = self.rm.can_open(risk_usd=risk, when=datetime.utcnow())
        if not ok:
            _log.info("[%s] %s BLOQUÉ par risk manager : %s", ticker, tag, reason)
            _evlog.risk_blocked(ticker, tag, reason)
            self.tg.notify_risk_blocked(ticker, tag, reason)
            if "consec_loss_pause" in reason:
                _evlog.consec_loss_pause(self.rm.consec_loss_days)
                self.tg.notify_consec_loss_pause(self.rm.consec_loss_days)
            return False

        # ── Conversion direction → side ───────────────────────────────────
        side = 0 if signal["direction"] == "long" else 1    # 0=Buy, 1=Sell

        # Auto OCO Brackets : SL négatif pour long (sous entrée), positif pour short
        _sl = _pts_to_ticks(signal["sl_dist"], ticker)
        _tp = _pts_to_ticks(signal["tp_dist"], ticker)
        sl_ticks = -_sl if side == 0 else _sl
        tp_ticks =  _tp if side == 0 else -_tp
        n_ct     = int(signal["n_ct"])
        entry    = float(signal["entry"])

        _log.info("[%s] SIGNAL %s %s @ %.4f (SL=%d t, TP=%d t, n=%d)",
                  ticker, signal.get("strategy", "?"),
                  signal["direction"].upper(), entry,
                  sl_ticks, tp_ticks, n_ct)
        _evlog.signal(ticker, signal.get("strategy", "?"),
                      signal["direction"], entry,
                      signal["sl"], signal["tp"],
                      signal.get("rr", 0), n_ct)
        signal["_sl_ticks"] = sl_ticks
        signal["_tp_ticks"] = tp_ticks
        self.tg.notify_signal(signal)

        if self.dry_run:
            _log.info("  [DRY-RUN] ordre non envoyé")
            order_id = -1     # sentinelle pour dry run
        else:
            try:
                order_id = self.client.place_limit_order(
                    account_id  = self.account_id,
                    contract_id = cid,
                    side        = side,
                    size        = n_ct,
                    limit_price = entry,
                    sl_ticks    = sl_ticks,
                    tp_ticks    = tp_ticks,
                    custom_tag  = tag,
                )
            except Exception as exc:
                _log.error("  Exception lors du placement pour %s : %s", tag, exc)
                _evlog.order_failed(tag, str(exc))
                self.tg.notify_system_error(f"place_limit_order {tag}", exc)
                # Sécurité : marquer le tag pour éviter une boucle infinie
                self._mark_tag_failed(tag, signal, cid)
                return False
            if order_id is None:
                # L'API a retourné success=False — le broker a peut-être quand
                # même créé l'ordre (ex: "custom tag already in use").
                # On cherche l'ordre par son tag dans les ordres ouverts.
                recovered_id = self._find_order_id_by_tag(tag)
                if recovered_id is not None:
                    _log.info("  Ordre récupéré par tag : id=%s", recovered_id)
                    order_id = recovered_id
                else:
                    _log.error("  Placement échoué pour %s (API refus)", tag)
                    _evlog.order_failed(tag, "API refus (orderId=None)")
                    self.tg.notify_system_error(
                        f"place_limit_order {tag}", "API a refusé l'ordre (orderId=None)")
                    self._mark_tag_failed(tag, signal, cid)
                    return False

        _evlog.order_placed(tag, order_id, dry_run=self.dry_run)
        self.tg.notify_order_placed(tag, signal, order_id, dry_run=self.dry_run)

        # ── Mise à jour état ──────────────────────────────────────────────
        self.rm.register_open(
            trade_id = tag,
            risk_usd = risk,
            when     = datetime.utcnow(),
            metadata = {"ticker": ticker, "order_id": order_id,
                        "strategy": signal.get("strategy", "?")},
        )
        self.state.setdefault("placed_tags", {})[tag] = {
            "order_id":   order_id,
            "strategy":   signal.get("strategy", "?"),
            "ticker":     ticker,
            "contract_id": cid,
            "direction":  signal["direction"],
            "entry":      entry,
            "sl":         float(signal["sl"]),
            "tp":         float(signal["tp"]),
            "n_ct":       n_ct,
            "risk":       risk,
            "status":     _ST_PENDING,
            "placed_at":  datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "fill_time":  None,
            "close_pnl":  None,
        }
        return True

    # ─────────────────────────────────────────────────────────────────────
    # Synchronisation avec le broker
    # ─────────────────────────────────────────────────────────────────────

    def _sync_broker(self, now_utc: datetime):
        """
        Réconcilie l'état local avec le broker :
          • Ordres PENDING dont l'order_id n'est plus dans les ordres ouverts
            → fill détecté ou annulé → mise à jour du RM
          • Positions qui ont disparu → close détecté → mise à jour du RM

        Le P&L de clôture est récupéré via l'endpoint /api/Trade/search.
        """
        placed = self.state.get("placed_tags", {})
        if not placed:
            return

        # ── Ordres ouverts actuels ────────────────────────────────────────
        open_orders = self.client.get_open_orders(self.account_id)
        open_ids    = {o["id"] for o in open_orders}

        # ── Positions ouvertes actuelles ──────────────────────────────────
        positions    = self.client.get_positions(self.account_id)
        pos_by_cid   = {p["contractId"]: p for p in positions}

        # ── Trades du jour (pour récupérer les P&L de clôture) ───────────
        day_start = (now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
                     - timedelta(hours=5))  # marge DST
        trades_today = self.client.get_trades_since(self.account_id, day_start)
        # Index par orderId → P&L de clôture (profitAndLoss ≠ None)
        closing_pnl: Dict[int, float] = {}
        for t in trades_today:
            if t.get("profitAndLoss") is not None and not t.get("voided"):
                closing_pnl[t["orderId"]] = float(t["profitAndLoss"])

        # ── Traitement par tag ────────────────────────────────────────────
        for tag, info in list(placed.items()):
            status   = info.get("status")
            order_id = info.get("order_id", -1)

            if status in (_ST_CANCELLED, _ST_CLOSED):
                continue   # déjà traité

            if status == _ST_PENDING:
                if order_id in open_ids:
                    continue   # toujours en attente — rien à faire
                # L'ordre n'est plus dans les ordres ouverts
                cid = info["contract_id"]
                if cid in pos_by_cid:
                    # Fill confirmé : une position est ouverte pour ce contrat
                    _log.info("[%s] FILL confirmé — position ouverte", tag)
                    info["status"]    = _ST_ACTIVE
                    info["fill_time"] = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
                    self.rm.register_fill(tag, when=now_utc)
                    status_snap = self.rm.status()
                    _evlog.fill(tag, info["ticker"], info["direction"],
                                info["entry"],
                                status_snap["daily_fills_count"],
                                self.rm.user_max_trades_per_day)
                    self.tg.notify_fill(
                        tag, info,
                        fills_today = status_snap["daily_fills_count"],
                        fills_max   = self.rm.user_max_trades_per_day,
                    )
                    self._maybe_warn_daily_limit(status_snap)
                else:
                    # Pas de position → ordre annulé ou expiré sans fill
                    _log.info("[%s] Ordre annulé / expiré sans fill", tag)
                    _evlog.order_cancelled(tag, "expiré sans fill")
                    info["status"] = _ST_CANCELLED
                    self.rm.cancel_open(tag)
                continue

            if status == _ST_ACTIVE:
                cid = info["contract_id"]
                if cid in pos_by_cid:
                    if (info.get("strategy") == "FIB"
                            and info.get("fill_time")):
                        fill_dt = datetime.fromisoformat(
                            info["fill_time"].replace("Z", "+00:00")
                        ).replace(tzinfo=None)
                        elapsed_bars = int(
                            (now_utc - fill_dt).total_seconds() / 900
                        )
                        if elapsed_bars >= FIB_MAX_HOLD_BARS:
                            self._close_position_market(tag, info, now_utc)
                    continue

                # Position disparue → clôturée (TP/SL/TE/market)
                pnl = closing_pnl.get(order_id, 0.0)
                _log.info("[%s] Position clôturée — P&L = %+.2f $", tag, pnl)
                info["status"]    = _ST_CLOSED
                info["close_pnl"] = pnl
                breached, reason  = self.rm.register_close(tag, pnl,
                                                            when=now_utc)
                status_snap = self.rm.status()
                _evlog.close(tag, info["ticker"], pnl,
                             status_snap["realized_day_pnl"],
                             status_snap["cum_pnl"])
                self.tg.notify_close(
                    tag, info, pnl,
                    session_pnl = status_snap["realized_day_pnl"],
                    cum_pnl     = status_snap["cum_pnl"],
                )
                if breached:
                    _log.critical("LIMITE TOPSTEP FRANCHIE : %s", reason)
                    _evlog.risk_breach(reason)
                    self.tg.notify_risk_breach(reason)
                else:
                    self._maybe_warn_daily_limit(status_snap)

    # ─────────────────────────────────────────────────────────────────────
    # Clôture forcée d'une position (Fib timeout ou fin de session OPR)
    # ─────────────────────────────────────────────────────────────────────

    def _close_position_market(self, tag: str, info: Dict,
                                now_utc: datetime):
        """Envoie un ordre Market pour clôturer la position et met à jour l'état."""
        ticker = info["ticker"]
        cid    = info["contract_id"]
        n_ct   = int(info["n_ct"])
        # Pour clôturer : sens inverse de l'entrée
        close_side = 1 if info["direction"] == "long" else 0   # Sell / Buy

        _log.info("[%s] Clôture forcée market (%s×%d)", tag, cid, n_ct)
        if not self.dry_run:
            self.client.place_market_order(
                account_id  = self.account_id,
                contract_id = cid,
                side        = close_side,
                size        = n_ct,
                custom_tag  = f"CLOSE_{tag}"[:64],
            )
        info["status"] = _ST_CLOSED

    # ─────────────────────────────────────────────────────────────────────
    # Alertes limites approchantes
    # ─────────────────────────────────────────────────────────────────────

    def _get_broker_day_summary(self, now_utc: datetime) -> Dict:
        """
        Résumé réel du compte depuis le broker.

        P&L journalier = solde courant - solde d'ouverture de session.
        Méthode infaillible : capture TOUT (P&L, frais, trades non trackés).
        """
        try:
            accounts = self.client.get_accounts()
            balance  = next(
                (float(a["balance"]) for a in accounts
                 if a.get("id") == self.account_id), None
            )

            opening_balance = self.state.get("opening_balance")
            if balance is not None and opening_balance is not None:
                day_pnl = balance - opening_balance
            else:
                day_pnl = None

            # Trades du jour pour le compte des fills
            day_start = (now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
                         - timedelta(hours=5))
            trades    = self.client.get_trades_since(self.account_id, day_start)
            valid     = [t for t in trades if not t.get("voided")]
            closes    = [t for t in valid if t.get("profitAndLoss") is not None]
            total_fees = sum(float(t.get("fees", 0)) for t in valid)

            open_positions = self.client.get_positions(self.account_id)

            return {
                "broker_balance":  round(balance, 2) if balance is not None else None,
                "broker_day_pnl":  round(day_pnl, 2) if day_pnl is not None else None,
                "broker_day_fees": round(total_fees, 2),
                "broker_fills":    len(closes),
                "broker_n_open":   len(open_positions),
            }
        except Exception as exc:
            _log.debug("_get_broker_day_summary échoué : %s", exc)
            return {}

    def _maybe_warn_daily_limit(self, status: Dict):
        """Envoie une alerte si la perte journalière dépasse 80 % de la limite."""
        day_pnl  = status.get("realized_day_pnl", 0.0)
        limit    = float(USER_DAILY_LOSS_MAX)
        if day_pnl < 0 and abs(day_pnl) / limit >= 0.80:
            fills_r = status.get("daily_fills_remaining",
                                 USER_MAX_TRADES_PER_DAY)
            _evlog.daily_warning(day_pnl, limit)
            self.tg.notify_daily_limit_warning(day_pnl, limit, int(fills_r))

    def _close_all_pending_and_active(self, strategy_filter: Optional[str] = None,
                                      now_utc: Optional[datetime] = None):
        """
        Fin de session OPR (16h30 NY) :
          • Annule les ordres limites encore pendants
          • Ferme les positions encore ouvertes au marché
        Optionnel : filtrer par stratégie ("OPR" ou "FIB").
        """
        placed = self.state.get("placed_tags", {})
        now_utc = now_utc or datetime.utcnow()

        for tag, info in list(placed.items()):
            strat = info.get("strategy", "")
            if strategy_filter and strat != strategy_filter:
                continue

            status   = info.get("status")
            order_id = info.get("order_id", -1)

            if status == _ST_PENDING:
                _log.info("[%s] Annulation ordre fin de session", tag)
                _evlog.order_cancelled(tag, "fin de session")
                if not self.dry_run and order_id > 0:
                    self.client.cancel_order(self.account_id, order_id)
                info["status"] = _ST_CANCELLED
                self.rm.cancel_open(tag)

            elif status == _ST_ACTIVE:
                self._close_position_market(tag, info, now_utc)
                _evlog.order_cancelled(tag, "clôture forcée fin de session")
                self.rm.register_close(tag, 0.0, when=now_utc)

    # ─────────────────────────────────────────────────────────────────────
    # Tick principal
    # ─────────────────────────────────────────────────────────────────────

    def run_tick(self):
        """
        Méthode principale — à appeler toutes les 15 minutes.

        Idempotente : plusieurs appels sur le même tick ne créent pas
        de doublons grâce aux custom_tags.
        """
        now_utc  = datetime.utcnow()
        _log.info("═══ Tick %s UTC ══════════════════════════════════",
                  now_utc.strftime("%Y-%m-%d %H:%M"))

        # ── 1. Chargement état ────────────────────────────────────────────
        self._load_state()

        # ── 2. Découverte contrats front-month ────────────────────────────
        try:
            self._ensure_contracts()
        except Exception as exc:
            _log.error("_ensure_contracts échoué : %s", exc)
            _evlog.error("ensure_contracts", exc)
            self.tg.notify_system_error("ensure_contracts", exc)
            self._save_state()
            return

        # ── 3. Synchronisation broker (fills / clôtures) ──────────────────
        try:
            self._sync_broker(now_utc)
        except Exception as exc:
            _log.error("_sync_broker échoué : %s", exc)
            _evlog.error("sync_broker", exc)
            self.tg.notify_system_error("sync_broker", exc)

        # ── 4. Commandes Telegram entrantes (/status) — toujours actif ──────
        today_str  = _current_day_ny(now_utc).isoformat()
        now_ny_str = (now_utc.replace(tzinfo=timezone.utc)
                      .astimezone(_NY_TZ)
                      .strftime("%Y-%m-%d %H:%M NY"))
        rm_snap = {**self.rm.status(), **self._get_broker_day_summary(now_utc)}
        self.tg.check_commands(
            placed_tags = self.state.get("placed_tags", {}),
            rm_status   = rm_snap,
            now_ny      = now_ny_str,
        )

        # ── 5. Fin de session OPR ? ───────────────────────────────────────
        if _opr_session_over(now_utc):
            _log.info("Session OPR terminée (16h30 NY) — clôture en cours")
            self._close_all_pending_and_active(strategy_filter="OPR",
                                               now_utc=now_utc)
            if self.state.get("session_report_sent") != today_str:
                snap        = self.rm.status()
                broker_snap = self._get_broker_day_summary(now_utc)
                merged      = {**snap, **broker_snap}
                pnl_report  = (broker_snap.get("broker_day_pnl")
                               if broker_snap.get("broker_day_pnl") is not None
                               else snap.get("realized_day_pnl", 0.0))
                fills_report = (broker_snap.get("broker_fills")
                                if broker_snap.get("broker_fills") is not None
                                else snap.get("daily_fills_count", 0))
                _evlog.session_end(today_str, pnl_report, fills_report)
                self.tg.send_session_report(
                    today_str,
                    self.state.get("placed_tags", {}),
                    merged,
                )
                self.state["session_report_sent"] = today_str
            self._save_state()
            _log.info("Tick terminé (post-session)")
            return

        # ── 6. Jour NY courant ────────────────────────────────────────────
        day_ny = pd.Timestamp(now_utc, tz="UTC").tz_convert(_NY_TZ).normalize()

        # Calcule le solde d'ouverture une fois par jour.
        # On reconstruit depuis les trades du jour pour être correct même si le
        # daemon redémarre en cours de session (opening = balance - pnl_net_jour).
        if self.state.get("opening_balance_date") != today_str:
            try:
                accounts = self.client.get_accounts()
                balance  = next(
                    (float(a["balance"]) for a in accounts
                     if a.get("id") == self.account_id), None
                )
                if balance is not None:
                    day_start_ob = (now_utc.replace(hour=0, minute=0,
                                                    second=0, microsecond=0)
                                    - timedelta(hours=5))
                    trades_ob = self.client.get_trades_since(
                        self.account_id, day_start_ob)
                    valid_ob  = [t for t in trades_ob if not t.get("voided")]
                    today_pnl = sum(float(t["profitAndLoss"])
                                    for t in valid_ob
                                    if t.get("profitAndLoss") is not None)
                    today_fees = sum(float(t.get("fees", 0)) for t in valid_ob)
                    opening = balance - today_pnl + today_fees
                    self.state["opening_balance"]      = opening
                    self.state["opening_balance_date"] = today_str
                    _log.info(
                        "Solde d'ouverture recalculé : %.2f $ "
                        "(balance=%.2f, pnl=%.2f, fees=%.2f)",
                        opening, balance, today_pnl, today_fees)
            except Exception as exc:
                _log.warning("Impossible de calculer le solde d'ouverture : %s", exc)

        if self.state.get("session_start_notified") != today_str:
            mode = "LIVE" if self.live_mode else "SIMULÉ"
            _evlog.session_start(today_str, self.tickers, mode)
            self.tg.notify_session_start(
                today_str,
                self.tickers,
                self.rm.status(),
            )
            self.state["session_start_notified"] = today_str

        # ── 7. Fetch barres + génération signaux ──────────────────────────
        new_signals: List[Dict] = []

        for ticker in self.tickers:
            try:
                df = self._fetch_bars(ticker)
            except Exception as exc:
                _log.error("_fetch_bars %s échoué : %s", ticker, exc)
                self.tg.notify_system_error(f"fetch_bars {ticker}", exc)
                continue
            if df is None or df.empty:
                self.tg.notify_system_error(
                    f"fetch_bars {ticker}", "Aucune barre reçue")
                continue

            if self.strategy in ("opr_fib", "opr") and OPR_ENABLED:
                sig = self._get_opr_signal(df, ticker, day_ny)
                if sig and not self._already_placed(sig["tag"]):
                    new_signals.append(sig)

            if self.strategy in ("opr_fib", "fib") and FIB_ENABLED:
                sig = self._get_fib_signal(df, ticker)
                if sig and not self._already_placed(sig["tag"]):
                    new_signals.append(sig)

        # ── 8. Filtre de corrélation ──────────────────────────────────────
        if len(new_signals) > 1:
            before = len(new_signals)
            new_signals = filter_correlated_signals(new_signals)
            if len(new_signals) < before:
                _log.info("Filtre corrélation : %d → %d signal(s)",
                           before, len(new_signals))

        # ── 9. Placement des ordres ───────────────────────────────────────
        for sig in new_signals:
            self._place_order(sig)

        # ── 10. Résumé risk manager ───────────────────────────────────────
        status = self.rm.status()
        _log.info(
            "Risk : cum=%.0f$ | jour=%.0f$ | fills=%d/%d | "
            "slack daily=%.0f$ trail=%.0f$",
            status["cum_pnl"], status["realized_day_pnl"],
            status["daily_fills_count"], self.rm.user_max_trades_per_day,
            status["slack_daily"], status["slack_trail"],
        )

        # ── 11. Sauvegarde ────────────────────────────────────────────────
        self._save_state()
        _log.info("Tick terminé")
