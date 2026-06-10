"""
Runner de session live — exécution automatisée OPR + Fib sur Topstep via ProjectX.

Appeler `SessionRunner.run_tick()` toutes les 15 minutes (juste après la clôture
de chaque bougie M15, ex. à :01, :16, :31, :46).

Flux par tick :
  1. Chargement de l'état persistant (state/live_state.json)
  2. Roll de jour si nouvelle session (reset daily counters via PortfolioRiskManager)
  3. Fetch des barres 15m depuis l'API ProjectX
  4. Détection des signaux OPR (run_opr_day), Fib v3 (get_fib_live_signal)
     et Fib v4 (get_fib_v4_live_signal — MES1/NQ1/MGC1 depuis 2026-05-19,
     gère états PENDING/CANCEL/CANCEL_FILL/ACTIVE)
  5. Filtre de corrélation (signal_selector.filter_correlated_signals)
  6. Pour chaque signal neuf → can_open → place_limit_order → register_open
  7. Sync broker : détection des fills/clôtures → register_fill / register_close
  8. Fin de session OPR (16h30 NY) → annulation ordres + clôture positions
  9. Sauvegarde état
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from broker.m1_buffer import M1Buffer
from broker.projectx_client import ProjectXClient
from broker.projectx_market_realtime import ProjectXMarketRealtimeClient
from broker.projectx_realtime import ProjectXRealtimeClient, RealtimeEvent
from broker.telegram_bot import TelegramBot
from config import (
    BOS_FVG_ENABLED,
    BOS_FVG_LIVE_M5_WARMUP_BARS,
    BOS_FVG_LIVE_TICKERS,
    BOS_FVG_MAX_HOLD_BARS_M5,
    BOS_FVG_SESSION_END,
    CHALLENGE_ADAPTIVE_SIZING_ENABLED,
    CHALLENGE_BYPASS_USER_DAILY_LIMIT,
    CHALLENGE_NOTIFY_OVERRIDE_THRESHOLD_USD,
    FIB_FINE_ENABLED,
    FIB_FINE_LIVE_M5_WARMUP_BARS,
    FIB_FINE_LIVE_TICKERS,
    FIB_FINE_STRUCT_PER_TF,
    FIB_MAX_HOLD_BARS,
    FIB_V4_ENABLED,
    FIB_V4_TICKERS,
    INSTRUMENTS,
    LIVE_STATE_FILE,
    OPR_ENABLED,
    OPR_SESSION_END,
    OPR_TIMEZONE,
    OPR_V4_LIVE_TICKERS,
    OPR_V5_1_LIVE_TICKERS,
    OPR_V5_1_USE_M1_BUFFER,
    PROJECTX_BARS_WARMUP,
    PROJECTX_MARKET_REALTIME_BUFFER_MINUTES,
    PROJECTX_MARKET_REALTIME_ENABLED,
    PROJECTX_MARKET_REALTIME_FORCE_REAUTH_S,
    PROJECTX_MARKET_REALTIME_HUB_URL,
    PROJECTX_MARKET_REALTIME_MAX_SILENCE_S,
    PROJECTX_MARKET_REALTIME_QUEUE_MAXSIZE,
    PROJECTX_MARKET_REALTIME_RECONNECT_DELAYS,
    PROJECTX_REALTIME_ALERT_OUTAGE_S,
    PROJECTX_REALTIME_DEBUG_EVENTS,
    PROJECTX_REALTIME_ENABLED,
    PROJECTX_REALTIME_FORCE_REAUTH_S,
    PROJECTX_REALTIME_HUB_URL,
    PROJECTX_REALTIME_MAX_SILENCE_S,
    PROJECTX_REALTIME_QUEUE_MAXSIZE,
    PROJECTX_REALTIME_RECONNECT_DELAYS,
    PROJECTX_SYMBOLS,
    RISK_PER_TRADE_USD,
    TOPSTEP_ACCOUNT_SIZE,
    USER_DAILY_LOSS_MAX,
    USER_MAX_TRADES_PER_DAY,
    YM1_ENABLED,
)
from core.adaptive_sizing import adaptive_risk_usd
from core.bos_fvg import get_bos_fvg_live_signal
from core.event_logger import EventLogger
from core.fib_fine_v2 import get_fib_fine_live_signal
from core.opr_v5_1 import get_opr_v5_1_live_signals
from core.risk_portfolio import PortfolioRiskManager, _Order
from core.signal_selector import filter_correlated_signals
from core.strategy_fib_v4 import get_fib_v4_live_signal

_log = logging.getLogger(__name__)
_evlog = EventLogger("logs/trading_events.log")

_NY_TZ = ZoneInfo(OPR_TIMEZONE)

# Statuts d'un ordre dans le state
_ST_PENDING = "PENDING"  # ordre limite pas encore filé
_ST_ACTIVE = "ACTIVE"  # position ouverte (fill confirmé)
_ST_CANCELLED = "CANCELLED"  # annulé ou expiré sans fill
_ST_CLOSED = "CLOSED"  # position fermée (TP/SL/TE/market)

# Suffixes de customTag des ordres bracket / clôture (hérités du tag d'entrée).
# Un ordre d'entrée porte customTag = <tag> ; ProjectX crée les brackets avec
# customTag = <tag>-SL / <tag>-TP, et nos clôtures market avec <tag>-EXIT.
_BRACKET_SUFFIXES = ("-SL", "-TP", "-EXIT")


def _owner_tag(custom_tag: str | None) -> str | None:
    """Retire le suffixe bracket (-SL/-TP/-EXIT) d'un customTag → tag propriétaire.

    Permet de rattacher un closing trade (qui porte l'orderId d'un bracket) au tag
    d'entrée exact, au lieu de l'attribuer au niveau du contrat (source du
    double-comptage quand plusieurs stratégies tradent le même contrat).
    """
    if not custom_tag:
        return None
    for suf in _BRACKET_SUFFIXES:
        if custom_tag.endswith(suf):
            return custom_tag[: -len(suf)]
    return custom_tag


def _build_close_attribution(
    trades: list[dict], orders: list[dict], exit_order_ids: dict[int, str] | None = None
) -> dict[str, list[dict]]:
    """Indexe les closing trades (profitAndLoss ≠ None, non voided) PAR TAG propriétaire.

    Résolution de l'owner : `trade.orderId → order.customTag → _owner_tag`. Pour les
    clôtures market (dont le customTag peut ne pas être persisté côté broker), on
    rabat sur `exit_order_ids` (orderId du market order → tag, connu au placement).

    Renvoie `{tag: [closing_trade, ...]}`. Chaque closing trade est attribué à AU
    PLUS un tag → plus de double-comptage.
    """
    tag_of_order = {o.get("id"): _owner_tag(o.get("customTag")) for o in orders}
    exit_order_ids = exit_order_ids or {}
    by_tag: dict[str, list[dict]] = {}
    for t in trades:
        if t.get("profitAndLoss") is None or t.get("voided"):
            continue
        oid = t.get("orderId")
        owner = tag_of_order.get(oid) or exit_order_ids.get(oid)
        if owner:
            by_tag.setdefault(owner, []).append(t)
    return by_tag


# ─────────────────────────────────────────────────────────────────────────────
# Sérialisation / déserialisation du PortfolioRiskManager
# ─────────────────────────────────────────────────────────────────────────────


def _rm_to_dict(rm: PortfolioRiskManager) -> dict:
    def _order_dict(o: _Order) -> dict:
        return {
            "risk_usd": o.risk_usd,
            "opened_at": o.opened_at.isoformat() if o.opened_at else None,
            "metadata": o.metadata,
        }

    return {
        "cum_pnl": rm.cum_pnl,
        "peak_pnl": rm.peak_pnl,
        "realized_day_pnl": rm.realized_day_pnl,
        "current_day": rm.current_day.isoformat() if rm.current_day else None,
        "consec_loss_days": rm.consec_loss_days,
        "daily_fills_count": rm.daily_fills_count,
        "pending_orders": {k: _order_dict(v) for k, v in rm.pending_orders.items()},
        "active_positions": {k: _order_dict(v) for k, v in rm.active_positions.items()},
        # Challenge — état du reset mensuel (persistant)
        "last_reset_month": rm.last_reset_month,
        "last_reset_year": rm.last_reset_year,
        "last_monthly_reset_at": (
            rm.last_monthly_reset_at.isoformat() if rm.last_monthly_reset_at else None
        ),
    }


def _rm_from_dict(data: dict) -> PortfolioRiskManager:
    rm = PortfolioRiskManager(
        cum_pnl=float(data.get("cum_pnl", 0.0)),
        peak_pnl=float(data.get("peak_pnl", 0.0)),
        realized_day_pnl=float(data.get("realized_day_pnl", 0.0)),
        consec_loss_days=int(data.get("consec_loss_days", 0)),
        daily_fills_count=int(data.get("daily_fills_count", 0)),
    )
    if data.get("current_day"):
        rm.current_day = date.fromisoformat(data["current_day"])

    def _load_order(d: dict) -> _Order:
        opened_at = None
        if d.get("opened_at"):
            opened_at = datetime.fromisoformat(d["opened_at"])
        return _Order(
            risk_usd=float(d.get("risk_usd", 0.0)),
            opened_at=opened_at,
            metadata=d.get("metadata", {}),
        )

    for k, v in data.get("pending_orders", {}).items():
        rm.pending_orders[k] = _load_order(v)
    for k, v in data.get("active_positions", {}).items():
        rm.active_positions[k] = _load_order(v)
    # Challenge — restauration du reset mensuel
    if data.get("last_reset_month") is not None:
        rm.last_reset_month = int(data["last_reset_month"])
    if data.get("last_reset_year") is not None:
        rm.last_reset_year = int(data["last_reset_year"])
    if data.get("last_monthly_reset_at"):
        rm.last_monthly_reset_at = datetime.fromisoformat(data["last_monthly_reset_at"])
    return rm


# ─────────────────────────────────────────────────────────────────────────────
# Conversion barres API → DataFrame (index UTC naïf — cohérent avec codebase)
# ─────────────────────────────────────────────────────────────────────────────


def _bars_to_df(bars: list[dict]) -> pd.DataFrame:
    if not bars:
        return pd.DataFrame()
    df = pd.DataFrame(bars)
    df = df.rename(
        columns={"t": "datetime", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}
    )
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True).dt.tz_localize(None)  # UTC naïf
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
    now_ny = now_utc.replace(tzinfo=UTC).astimezone(_NY_TZ)
    h, m = OPR_SESSION_END
    return (now_ny.hour, now_ny.minute) >= (h, m)


def _is_trading_session(now_utc: datetime) -> bool:
    """
    Vrai si les futures CME US sont potentiellement ouverts.
    Faux le samedi (toute la journée) et le dimanche avant 18h NY
    (réouverture du marché à 18h00 NY le dimanche soir).
    """
    now_ny = now_utc.replace(tzinfo=UTC).astimezone(_NY_TZ)
    weekday = now_ny.weekday()  # 0=lundi … 5=samedi, 6=dimanche
    if weekday == 5:  # samedi : fermé toute la journée
        return False
    if weekday == 6:  # dimanche : ouverture à 18h00 NY
        return (now_ny.hour, now_ny.minute) >= (18, 0)
    return True


def _current_day_ny(now_utc: datetime) -> date:
    return now_utc.replace(tzinfo=UTC).astimezone(_NY_TZ).date()


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
      strategy   : "opr_fib" (seul mode restant après cleanup 2026-05-19)
    """

    def __init__(
        self,
        client: ProjectXClient,
        account_id: int,
        state_file: str = LIVE_STATE_FILE,
        dry_run: bool = True,
        tickers: list[str] | None = None,
        strategy: str = "opr_fib",
        live_mode: bool = False,  # défaut safe ; live.py:_build_runner override depuis account.simulated
        telegram: TelegramBot | None = None,
    ):
        self.client = client
        self.account_id = account_id
        self.state_file = Path(state_file)
        self.dry_run = dry_run
        self.strategy = strategy
        self.live_mode = live_mode  # False = simulated (challenge), True = funded
        self.tickers = tickers or self._default_tickers()
        self.state: dict = {}
        self.rm: PortfolioRiskManager = PortfolioRiskManager()
        self.tg: TelegramBot = telegram or TelegramBot("", "")  # noop si absent

        # ── Client realtime SignalR (fast path, hybride avec polling REST) ──
        # OFF par défaut (PROJECTX_REALTIME_ENABLED) → instancié uniquement
        # si flag activé en config. Polling REST 30 s reste autoritatif.
        self.rt: ProjectXRealtimeClient | None = None
        self._rt_error_count_hour: int = 0
        self._rt_error_hour_start: float = 0.0
        if PROJECTX_REALTIME_ENABLED:
            try:
                self.rt = ProjectXRealtimeClient(
                    account_id=self.account_id,
                    token_provider=lambda: self.client.token,
                    hub_url=PROJECTX_REALTIME_HUB_URL,
                    queue_maxsize=PROJECTX_REALTIME_QUEUE_MAXSIZE,
                    reconnect_delays=PROJECTX_REALTIME_RECONNECT_DELAYS,
                    max_silence_s=PROJECTX_REALTIME_MAX_SILENCE_S,
                    force_reauth_s=PROJECTX_REALTIME_FORCE_REAUTH_S,
                )
                _evlog.realtime_starting()
                self.rt.start()
            except Exception as exc:
                _log.error("Realtime: init échouée (%s) — fallback REST seul", exc, exc_info=True)
                self.rt = None

        # ── Client Market Hub realtime + buffer M1 (Phase C) ────────────────
        # OFF par défaut (PROJECTX_MARKET_REALTIME_ENABLED). Init différée car
        # les contract_ids ne sont résolus qu'en début de session via
        # _ensure_contracts. Cf _maybe_start_market_realtime() appelé dans
        # run_tick après _ensure_contracts. Le buffer M1 alimente OPR v5.1
        # pour mesurer F2 intra-bar M15 (fidélité cible 90 %+).
        self.market_rt: ProjectXMarketRealtimeClient | None = None
        self.m1_buffer: M1Buffer | None = None
        self._market_rt_error_count_hour: int = 0
        self._market_rt_error_hour_start: float = 0.0

        # Challenge — anti-spam de la notif "bypass actif" (1×/jour max)
        self._last_bypass_notify_day: date | None = None
        # Anti-spam damping day_progress < 1.0 (1×/jour max)
        self._last_damping_notify_day: date | None = None
        # Anti-spam warning cap consistency à 80% (1×/jour max)
        self._last_consistency_warn_day: date | None = None
        # Anti-spam log "OPR <ticker> en veille" (1×/session par ticker)
        self._opr_skipped_notified: set = set()

    # ─────────────────────────────────────────────────────────────────────
    # Tickers actifs
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def _default_tickers() -> list[str]:
        tickers = ["NQ1", "MES1"]
        if YM1_ENABLED:
            tickers.append("YM1")
        # Tickers couverts par fib-v4 (MGC1 ajouté 2026-05-19, MES1/NQ1 déjà inclus).
        if FIB_V4_ENABLED:
            for t in FIB_V4_TICKERS:
                if t not in tickers:
                    tickers.append(t)
        # Tickers couverts par fib-fine-v2 (NQ1/MES1 déjà inclus ; flag OFF par défaut).
        if FIB_FINE_ENABLED:
            for t in FIB_FINE_LIVE_TICKERS:
                if t not in tickers:
                    tickers.append(t)
        # Tickers couverts par bos-fvg-v1 (NQ1/MES1 déjà inclus ; promu 2026-06-03).
        if BOS_FVG_ENABLED:
            for t in BOS_FVG_LIVE_TICKERS:
                if t not in tickers:
                    tickers.append(t)
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
            "date": None,
            "account_id": self.account_id,
            "contracts": {},
            "placed_tags": {},
            "session_report_sent": None,  # date YYYY-MM-DD du dernier rapport envoyé
            "session_start_notified": None,  # date YYYY-MM-DD du dernier start notifié
            "telegram_update_id": -1,
            "paused": False,  # commandé via Telegram /pause /resume
        }
        self.rm = PortfolioRiskManager()

    # ─────────────────────────────────────────────────────────────────────
    # Pause / resume (persistant via state)
    # ─────────────────────────────────────────────────────────────────────

    def is_paused(self) -> bool:
        return bool(self.state.get("paused", False))

    def set_paused(self, paused: bool):
        """Met à jour l'état de pause et persiste immédiatement."""
        self.state["paused"] = bool(paused)
        try:
            self._save_state()
        except Exception as exc:
            _log.warning("Échec persistance pause=%s : %s", paused, exc)
        _log.info("Bot %s par commande Telegram", "EN PAUSE" if paused else "REPRIS")
        _evlog._write("SYSTEM", f"Bot {'pausé' if paused else 'repris'} via Telegram")

    def _save_state(self):
        """Persiste l'état dans le fichier JSON."""
        self.state["risk_state"] = _rm_to_dict(self.rm)
        self.state["account_id"] = self.account_id
        self.state["telegram_update_id"] = self.tg.current_update_id()
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
        needs_refresh = self.state.get("date") != today or not all(
            t in contracts for t in self.tickers
        )
        if not needs_refresh:
            return

        _log.info("Découverte des contrats front-month (live_mode=%s)…", self.live_mode)
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
        self.state["date"] = today

    def _contract_id(self, ticker: str) -> str | None:
        return self.state.get("contracts", {}).get(ticker)

    # ─────────────────────────────────────────────────────────────────────
    # Fetch barres
    # ─────────────────────────────────────────────────────────────────────

    def _fetch_bars(self, ticker: str, n_bars: int = PROJECTX_BARS_WARMUP) -> pd.DataFrame | None:
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
            contract_id=cid,
            start_dt=start,
            end_dt=now,
            unit=2,  # Minute
            unit_number=15,
            limit=n_bars,
            live=self.live_mode,
            include_partial=False,
        )
        if not bars:
            _log.warning("Aucune barre reçue pour %s", ticker)
            return None

        df = _bars_to_df(bars)
        _log.debug(
            "%s : %d barres 15m chargées (de %s à %s)", ticker, len(df), df.index[0], df.index[-1]
        )
        return df

    def _fetch_bars_m5(
        self, ticker: str, n_bars: int = FIB_FINE_LIVE_M5_WARMUP_BARS
    ) -> pd.DataFrame | None:
        """
        Fetch les n_bars dernières barres M5 depuis l'API ProjectX (fib-fine-v2).

        Calqué sur `_fetch_bars` mais en unité M5 (unit=2, unit_number=5). Source
        DÉDIÉE fib-fine (FIB_FINE_LIVE_BARS_SOURCE="rest") — aucune dépendance au
        M1Buffer (le filtre causal atr_ratio_sl ne lit que des barres M5 closes).
        Retourne un DataFrame UTC naïf de barres CLÔTURÉES (include_partial=False).
        """
        cid = self._contract_id(ticker)
        if cid is None:
            _log.error("Pas de contractId pour %s — impossible de fetcher M5", ticker)
            return None

        now = datetime.utcnow()
        # ~12 barres M5/heure, ~23 h de marché/jour → ~276 barres/jour. On remonte
        # large pour couvrir n_bars (warmup EMA200 M5 + pivots).
        start = now - timedelta(days=max(1, n_bars // 200))
        bars = self.client.get_bars(
            contract_id=cid,
            start_dt=start,
            end_dt=now,
            unit=2,  # Minute
            unit_number=5,
            limit=n_bars,
            live=self.live_mode,
            include_partial=False,
        )
        if not bars:
            _log.warning("Aucune barre M5 reçue pour %s", ticker)
            return None

        df = _bars_to_df(bars)
        _log.debug(
            "%s : %d barres M5 chargées (de %s à %s)", ticker, len(df), df.index[0], df.index[-1]
        )
        return df

    # ─────────────────────────────────────────────────────────────────────
    # Détection des signaux stratégie
    # ─────────────────────────────────────────────────────────────────────

    def _get_opr_signal(self, df: pd.DataFrame, ticker: str, day_ny: pd.Timestamp) -> dict | None:
        """
        Joue run_opr_day (v4) ou get_opr_v5_1_live_signals (v5.1, schéma A
        entrée différée) selon le mapping config.OPR_V5_1_LIVE_TICKERS,
        et retourne le dernier signal non encore filé.

        Routing :
          - MES1 → pass-through v4 (edge ML F2 non significatif p=0.23)
          - NQ1, YM1 → v5.1 (filtre F2 actif, p<0.0001, BS=100 %)

        Le wrapper get_opr_v5_1_live_signals gère le routing en interne :
        pour les tickers hors OPR_V5_1_LIVE_TICKERS, il appelle directement
        run_opr_day → comportement v4 strict.

        Retourne None si :
          • OPR désactivé, aucun trigger aujourd'hui, ou dernier trade clos.
        """
        if not OPR_ENABLED:
            return None

        # Phase C : si le buffer M1 est dispo ET le flag config ON, on passe
        # le buffer + contract_id au wrapper v5.1 pour mesurer F2 intra-bar.
        # Sinon comportement actuel (M15 strict) — pass-through transparent.
        contract_id = self._contract_id(ticker)
        m1_buffer = (
            self.m1_buffer
            if (OPR_V5_1_USE_M1_BUFFER and self.m1_buffer is not None and contract_id)
            else None
        )

        signals, trades, _zone = get_opr_v5_1_live_signals(
            df,
            ticker,
            day_ny,
            m1_buffer=m1_buffer,
            contract_id=contract_id if m1_buffer is not None else None,
        )
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
        tag = f"OPR_{ticker}_{date_str}_{last_sig['direction']}_{last_idx}"
        return {**last_sig, "tag": tag}

    def _get_fib_v4_signal(self, df: pd.DataFrame, ticker: str) -> dict | None:
        """
        Appelle get_fib_v4_live_signal et retourne le signal ou une action
        d'invalidation intra-bar.

        Retourne :
          • None si rien d'actif (ou ticker hors FIB_V4_TICKERS, ou ACTIVE
            géré côté broker via brackets, ou erreur).
          • dict signal "à placer" si state == "PENDING" (avec tag).
          • dict {"_action": "cancel", "tag": ...} si state == "CANCEL"
            → annulation de l'ordre limite pendant phase pending (pivot break
            intra-bar M1).
          • dict {"_action": "cancel_fill", "tag": ...} si state == "CANCEL_FILL"
            → wick excessive intra-bar M1 : annuler limit OU fermer position
            si déjà fillée.

        Le ticker est aussi re-filtré par FIB_V4_TICKERS côté core, donc
        c'est défense-en-profondeur.
        """
        if not FIB_V4_ENABLED:
            return None
        if ticker not in FIB_V4_TICKERS:
            return None

        # M1 buffer requis pour les évaluations intra-bar (CANCEL/CANCEL_FILL).
        # Si indisponible, fib-v4 retombe en mode M15-close uniquement (dégradé).
        contract_id = self._contract_id(ticker)
        m1_buffer = self.m1_buffer if (self.m1_buffer is not None and contract_id) else None

        try:
            live_state = get_fib_v4_live_signal(
                df,
                ticker,
                m1_buffer=m1_buffer,
                contract_id=contract_id if m1_buffer is not None else None,
            )
        except Exception as exc:
            _log.error("get_fib_v4_live_signal %s échoué : %s", ticker, exc)
            return None

        if live_state is None:
            return None

        state = live_state["state"]
        sig = live_state["signal"]
        imp_key = live_state["impulse_key"]
        today = _current_day_ny(datetime.utcnow()).strftime("%Y%m%d")
        tag = f"FIBV4_{ticker}_{today}_{imp_key}"

        if state == "PENDING":
            return {**sig, "tag": tag}

        if state == "CANCEL":
            return {
                "_action": "cancel",
                "tag": tag,
                "ticker": ticker,
                "reason": live_state.get("reason", "fib-v4 invalidation"),
            }

        if state == "CANCEL_FILL":
            return {
                "_action": "cancel_fill",
                "tag": tag,
                "ticker": ticker,
                "reason": live_state.get("reason", "fib-v4 wick excess"),
            }

        # ACTIVE : broker gère via brackets — rien à faire ici
        return None

    def _get_fib_fine_signal(self, ticker: str) -> dict | None:
        """
        Appelle get_fib_fine_live_signal (fib-fine-v2, M5) et retourne le signal
        ou une action d'annulation.

        Calqué sur `_get_fib_v4_signal` mais :
          • barres M5 fetchées en REST dédié (_fetch_bars_m5), pas le df M15.
          • strategy="FIB_FINE" posé par core.fib_fine_v2 → chemins SÉPARÉS de
            fib-v4 (event_logger, signal_selector, time-stop _sync_broker).
          • pas d'état "CANCEL_FILL" (filtre wick neutralisé en v2, filtre causal).

        Retourne :
          • None si rien d'actif (flag OFF, ticker hors univers, ACTIVE géré
            broker-side, ou erreur).
          • dict signal "à placer" si state == "PENDING" (avec tag FIBFINE_...).
          • dict {"_action": "cancel", "tag": ...} si state == "CANCEL"
            (pivot break sur M5 closes pendant la phase pending).
        """
        if not FIB_FINE_ENABLED:
            return None
        if ticker not in FIB_FINE_LIVE_TICKERS:
            return None

        try:
            df_m5 = self._fetch_bars_m5(ticker)
        except Exception as exc:
            _log.error("_fetch_bars_m5 %s échoué : %s", ticker, exc)
            return None
        if df_m5 is None or df_m5.empty:
            return None

        try:
            live_state = get_fib_fine_live_signal(df_m5, ticker)
        except Exception as exc:
            _log.error("get_fib_fine_live_signal %s échoué : %s", ticker, exc)
            return None

        if live_state is None:
            return None

        state = live_state["state"]
        sig = live_state["signal"]
        imp_key = live_state["impulse_key"]
        today = _current_day_ny(datetime.utcnow()).strftime("%Y%m%d")
        tag = f"FIBFINE_{ticker}_{today}_{imp_key}"

        if state == "PENDING":
            return {**sig, "tag": tag}

        if state == "CANCEL":
            return {
                "_action": "cancel",
                "tag": tag,
                "ticker": ticker,
                "reason": live_state.get("reason", "fib-fine invalidation"),
            }

        # ACTIVE : broker gère via brackets — rien à faire ici
        return None

    def _get_bos_fvg_signal(self, ticker: str) -> dict | None:
        """
        Appelle get_bos_fvg_live_signal (bos-fvg-v1, M5) et retourne le signal
        ou une action d'annulation. Calqué sur `_get_fib_fine_signal` :
          • barres M5 fetchées en REST dédié (_fetch_bars_m5), pas le df M15.
          • strategy="BOS_FVG" posé par core.bos_fvg → chemins SÉPARÉS (event_logger,
            signal_selector, time-stop _sync_broker).
          • entrée LIMIT @ CE (fill honnête) ; pas de "CANCEL_FILL".

        Retourne :
          • None si rien d'actif (flag OFF, ticker hors univers, ACTIVE géré
            broker-side, ou erreur).
          • dict signal "à placer" si state == "PENDING" (avec tag BOSFVG_...).
          • dict {"_action": "cancel", "tag": ...} si state == "CANCEL"
            (invalidation FVG sur M5 closes pendant la phase pending).
        """
        if not BOS_FVG_ENABLED:
            return None
        if ticker not in BOS_FVG_LIVE_TICKERS:
            return None

        try:
            df_m5 = self._fetch_bars_m5(ticker, BOS_FVG_LIVE_M5_WARMUP_BARS)
        except Exception as exc:
            _log.error("_fetch_bars_m5 %s échoué : %s", ticker, exc)
            return None
        if df_m5 is None or df_m5.empty:
            return None

        try:
            live_state = get_bos_fvg_live_signal(df_m5, ticker)
        except Exception as exc:
            _log.error("get_bos_fvg_live_signal %s échoué : %s", ticker, exc)
            return None

        if live_state is None:
            return None

        state = live_state["state"]
        sig = live_state["signal"]
        imp_key = live_state["impulse_key"]
        today = _current_day_ny(datetime.utcnow()).strftime("%Y%m%d")
        tag = f"BOSFVG_{ticker}_{today}_{imp_key}"

        if state == "PENDING":
            return {**sig, "tag": tag}

        if state == "CANCEL":
            return {
                "_action": "cancel",
                "tag": tag,
                "ticker": ticker,
                "reason": live_state.get("reason", "bos-fvg invalidation FVG"),
            }

        # ACTIVE : broker gère via brackets — rien à faire ici
        return None

    # ─────────────────────────────────────────────────────────────────────
    # Placement d'ordre
    # ─────────────────────────────────────────────────────────────────────

    def _already_placed(self, tag: str) -> bool:
        """Vrai si l'ordre avec ce tag a déjà été envoyé au broker."""
        return tag in self.state.get("placed_tags", {})

    def _mark_tag_failed(self, tag: str, signal: dict, cid: str):
        """
        Enregistre le tag comme CANCELLED pour empêcher tout retry.
        Appelé quand le placement échoue côté API — le broker peut avoir
        l'ordre ou non, mais on ne doit pas retenter indéfiniment.
        """
        self.state.setdefault("placed_tags", {})[tag] = {
            "order_id": None,
            "strategy": signal.get("strategy", "?"),
            "ticker": signal["ticker"],
            "contract_id": cid,
            "direction": signal["direction"],
            "entry": float(signal["entry"]),
            "sl": float(signal["sl"]),
            "tp": float(signal["tp"]),
            "n_ct": int(signal["n_ct"]),
            "risk": float(signal.get("risk", 0)),
            "status": _ST_CANCELLED,
            "placed_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "fill_time": None,
            "close_pnl": None,
        }

    def _find_order_id_by_tag(self, tag: str) -> int | None:
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

    def _place_order(self, signal: dict) -> bool:
        """
        Vérifie can_open, place le limit order avec brackets SL/TP, et
        enregistre l'ordre dans le risk manager et le state.

        Retourne True si l'ordre est passé (ou simulé en dry_run).
        """
        tag = signal["tag"]
        ticker = signal["ticker"]
        cid = self._contract_id(ticker)
        if cid is None:
            _log.error("Pas de contractId pour %s — ordre non placé", ticker)
            return False

        # ── Sizing adaptatif (challenge Topstep) ──────────────────────────
        risk_static = float(signal.get("risk", RISK_PER_TRADE_USD))
        risk_adaptive = None
        sizing_factors = None
        if CHALLENGE_ADAPTIVE_SIZING_ENABLED:
            try:
                rm_status = self.rm.status()
                risk_adaptive, sizing_factors = adaptive_risk_usd(
                    rm_status, signal, datetime.utcnow()
                )
                # Recalcul n_ct depuis le sl_dist du signal et le dpp du contrat
                sl_dist = float(signal["sl_dist"])
                dpp = float(INSTRUMENTS[ticker]["dollar_per_point"])
                n_ct_new = max(1, int(risk_adaptive / (sl_dist * dpp)))
                # Risque effectif réajusté à la granularité du contrat entier
                risk_effective = n_ct_new * sl_dist * dpp
                signal["n_ct"] = n_ct_new
                signal["risk"] = risk_effective
                risk = risk_effective
                _evlog.sizing_decision(
                    signal.get("strategy", "?"),
                    ticker,
                    risk_static,
                    risk_adaptive,
                    risk_effective,
                    factors=sizing_factors,
                )
                # Notification Telegram si override significatif
                if risk_effective > CHALLENGE_NOTIFY_OVERRIDE_THRESHOLD_USD:
                    bypass_lbl = (
                        "USER_DAILY_LOSS_MAX bypassé"
                        if CHALLENGE_BYPASS_USER_DAILY_LIMIT
                        else "limite utilisateur active"
                    )
                    msg = (
                        f"⚠️ [CHALLENGE] Override risk: ${risk_effective:.0f} "
                        f"(static ${risk_static:.0f}, {bypass_lbl}) — "
                        f"days_left={int(sizing_factors['days_left'])}, "
                        f"distance=${sizing_factors['distance_target']:.0f}, "
                        f"lockin={sizing_factors['lockin']:.2f}"
                    )
                    self.tg.send_warning(msg)
                # Notification "première fois du jour" du bypass actif
                today = datetime.utcnow().date()
                if CHALLENGE_BYPASS_USER_DAILY_LIMIT and self._last_bypass_notify_day != today:
                    _evlog.challenge_bypass(ticker, risk_effective)
                    self._last_bypass_notify_day = today

                # Alerte 1× / jour : damping day_progress kick-in (< 1.0)
                day_prog = sizing_factors.get("day_progress", 1.0)
                if day_prog < 1.0 and self._last_damping_notify_day != today:
                    from config import (
                        CHALLENGE_DAY_PROFIT_HARD_CAP_USD,
                        CHALLENGE_DAY_PROFIT_SOFT_CAP_USD,
                    )

                    rdp_now = sizing_factors.get("realized_day_pnl", 0.0)
                    self.tg.notify_day_damping_active(
                        rdp_now,
                        CHALLENGE_DAY_PROFIT_SOFT_CAP_USD,
                        CHALLENGE_DAY_PROFIT_HARD_CAP_USD,
                        day_prog,
                    )
                    self._last_damping_notify_day = today

                # Alerte 1× / jour : 80% du cap cohérence 50% atteint
                from config import CHALLENGE_CONSISTENCY_BEST_DAY_MAX_USD

                rdp_now = sizing_factors.get("realized_day_pnl", 0.0)
                if (
                    rdp_now >= 0.80 * CHALLENGE_CONSISTENCY_BEST_DAY_MAX_USD
                    and rdp_now < CHALLENGE_CONSISTENCY_BEST_DAY_MAX_USD
                    and self._last_consistency_warn_day != today
                ):
                    self.tg.notify_consistency_warning(
                        rdp_now,
                        CHALLENGE_CONSISTENCY_BEST_DAY_MAX_USD,
                    )
                    self._last_consistency_warn_day = today
            except Exception as exc:
                _log.error("[%s] sizing adaptatif échoué : %s — fallback static", ticker, exc)
                _evlog.error("adaptive_sizing", exc)
                risk = risk_static
                _evlog.sizing_decision(
                    signal.get("strategy", "?"),
                    ticker,
                    risk_static,
                    None,
                    risk_static,
                )
        else:
            risk = risk_static
            _evlog.sizing_decision(
                signal.get("strategy", "?"),
                ticker,
                risk_static,
                None,
                risk_static,
            )

        # ── Vérification risk manager ─────────────────────────────────────
        # Calcul du gain potentiel au TP — sert au check de cohérence 50%
        # Topstep (Combine) côté risk manager.
        try:
            _tp_dist = float(signal["tp_dist"])
            _dpp = float(INSTRUMENTS[ticker]["dollar_per_point"])
            _n_ct = int(signal.get("n_ct", 1))
            tp_gain_usd = abs(_tp_dist * _dpp * _n_ct)
        except (KeyError, TypeError, ValueError):
            tp_gain_usd = None  # skip du check 50% si données manquantes

        ok, reason = self.rm.can_open(
            risk_usd=risk,
            when=datetime.utcnow(),
            tp_gain_usd=tp_gain_usd,
        )
        if not ok:
            _log.info("[%s] %s BLOQUÉ par risk manager : %s", ticker, tag, reason)
            _evlog.risk_blocked(ticker, tag, reason)
            self.tg.notify_risk_blocked(ticker, tag, reason)
            if "consec_loss_pause" in reason:
                _evlog.consec_loss_pause(self.rm.consec_loss_days)
                self.tg.notify_consec_loss_pause(self.rm.consec_loss_days)
            return False

        # ── Conversion direction → side ───────────────────────────────────
        side = 0 if signal["direction"] == "long" else 1  # 0=Buy, 1=Sell

        # Auto OCO Brackets : SL négatif pour long (sous entrée), positif pour short
        _sl = _pts_to_ticks(signal["sl_dist"], ticker)
        _tp = _pts_to_ticks(signal["tp_dist"], ticker)
        sl_ticks = -_sl if side == 0 else _sl
        tp_ticks = _tp if side == 0 else -_tp
        n_ct = int(signal["n_ct"])
        entry = float(signal["entry"])

        _log.info(
            "[%s] SIGNAL %s %s @ %.4f (SL=%d t, TP=%d t, n=%d)",
            ticker,
            signal.get("strategy", "?"),
            signal["direction"].upper(),
            entry,
            sl_ticks,
            tp_ticks,
            n_ct,
        )
        _evlog.signal(
            ticker,
            signal.get("strategy", "?"),
            signal["direction"],
            entry,
            signal["sl"],
            signal["tp"],
            signal.get("rr", 0),
            n_ct,
        )
        signal["_sl_ticks"] = sl_ticks
        signal["_tp_ticks"] = tp_ticks
        self.tg.notify_signal(signal)

        if self.dry_run:
            _log.info("  [DRY-RUN] ordre non envoyé")
            order_id = -1  # sentinelle pour dry run
        else:
            try:
                order_id = self.client.place_limit_order(
                    account_id=self.account_id,
                    contract_id=cid,
                    side=side,
                    size=n_ct,
                    limit_price=entry,
                    sl_ticks=sl_ticks,
                    tp_ticks=tp_ticks,
                    custom_tag=tag,
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
                        f"place_limit_order {tag}", "API a refusé l'ordre (orderId=None)"
                    )
                    self._mark_tag_failed(tag, signal, cid)
                    return False

        _evlog.order_placed(tag, order_id, dry_run=self.dry_run)
        self.tg.notify_order_placed(tag, signal, order_id, dry_run=self.dry_run)

        # ── Mise à jour état ──────────────────────────────────────────────
        self.rm.register_open(
            trade_id=tag,
            risk_usd=risk,
            when=datetime.utcnow(),
            metadata={
                "ticker": ticker,
                "order_id": order_id,
                "strategy": signal.get("strategy", "?"),
            },
        )
        self.state.setdefault("placed_tags", {})[tag] = {
            "order_id": order_id,
            "strategy": signal.get("strategy", "?"),
            "ticker": ticker,
            "contract_id": cid,
            "direction": signal["direction"],
            "entry": entry,
            "sl": float(signal["sl"]),
            "tp": float(signal["tp"]),
            "n_ct": n_ct,
            "risk": risk,
            "status": _ST_PENDING,
            "placed_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "fill_time": None,
            "close_pnl": None,
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
        open_ids = {o["id"] for o in open_orders}

        # ── Positions ouvertes actuelles ──────────────────────────────────
        positions = self.client.get_positions(self.account_id)
        pos_by_cid = {p["contractId"]: p for p in positions}

        # ── Trades + ordres du jour (P&L de clôture, attribution par tag) ──
        day_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
            hours=5
        )  # marge DST
        trades_today = self.client.get_trades_since(self.account_id, day_start)
        orders_today = self.client.get_orders_since(self.account_id, day_start)
        # Attribution PAR TAG via le customTag des ordres (bracket <tag>-SL/-TP,
        # clôture market <tag>-EXIT) + fallback sur l'orderId des clôtures market
        # connues (`_exit_order_id`). Un closing trade est attribué à AU PLUS un
        # tag → corrige le double-comptage qui sommait par contractId.
        exit_order_ids = {
            int(i["_exit_order_id"]): tag for tag, i in placed.items() if i.get("_exit_order_id")
        }
        closing_by_tag = _build_close_attribution(trades_today, orders_today, exit_order_ids)

        # ── Traitement par tag ────────────────────────────────────────────
        for tag, info in list(placed.items()):
            status = info.get("status")
            order_id = info.get("order_id", -1)

            if status in (_ST_CANCELLED, _ST_CLOSED):
                continue  # déjà traité

            if status == _ST_PENDING:
                if order_id in open_ids:
                    continue  # toujours en attente — rien à faire
                # L'ordre n'est plus ouvert → fill ou annulation ?
                # Fill = un opening trade (profitAndLoss None) porte cet order_id
                # d'entrée. Signal définitif, non ambigu (vs contractId netté).
                filled = any(
                    t.get("orderId") == order_id
                    and t.get("profitAndLoss") is None
                    and not t.get("voided")
                    for t in trades_today
                )
                if filled or info["contract_id"] in pos_by_cid:
                    self._handle_fill_transition(tag, info, now_utc)
                else:
                    self._handle_cancel_transition(tag, info, "expiré sans fill")
                continue

            if status == _ST_ACTIVE:
                # 1) Clôture détectée PAR TAG via customTag (autoritatif, sans
                #    double-comptage) : closing trade(s) <tag>-SL/-TP/-EXIT ≥ n_ct.
                tag_closes = closing_by_tag.get(tag, [])
                closed_sz = sum(int(t.get("size", 0)) for t in tag_closes)
                if tag_closes and closed_sz >= int(info.get("n_ct", 1)):
                    pnl = sum(float(t["profitAndLoss"]) for t in tag_closes)
                    self._handle_close_transition(tag, info, pnl, now_utc)
                    continue
                # 2) Position encore ouverte → time-stops éventuels (clôture market).
                #    `_exit_sent` (posé par _close_position_market) évite de renvoyer
                #    un market order à chaque sync en attendant le closing trade.
                if not info.get("_exit_sent"):
                    strat = info.get("strategy")
                    ft_iso = info.get("fill_time")
                    if ft_iso and strat == "FIB":
                        fill_dt = datetime.fromisoformat(ft_iso.replace("Z", "+00:00")).replace(
                            tzinfo=None
                        )
                        elapsed_bars = int((now_utc - fill_dt).total_seconds() / 900)
                        if elapsed_bars >= FIB_MAX_HOLD_BARS:
                            self._close_position_market(tag, info, now_utc)
                    elif ft_iso and strat == "FIB_FINE":
                        # Time-stop fib-fine-v2 (intraday) : barre M5 = 300 s, et
                        # max_hold_bars M5 dédié (cf. FIB_FINE_STRUCT_PER_TF["m5"]).
                        # Fidèle au backtest qui produit un TE après max_hold_bars
                        # OU au changement de jour NY.
                        fill_dt = datetime.fromisoformat(ft_iso.replace("Z", "+00:00")).replace(
                            tzinfo=None
                        )
                        elapsed_bars = int((now_utc - fill_dt).total_seconds() / 300)
                        max_hold = FIB_FINE_STRUCT_PER_TF["m5"]["max_hold_bars"]
                        new_day = _current_day_ny(now_utc) != _current_day_ny(fill_dt)
                        if elapsed_bars >= max_hold or new_day:
                            self._close_position_market(tag, info, now_utc)
                    elif ft_iso and strat == "BOS_FVG":
                        # Time-stop bos-fvg-v2 (intraday) : close à la cloche RTH
                        # (BOS_FVG_SESSION_END NY), au changement de jour NY, OU après
                        # BOS_FVG_MAX_HOLD_BARS_M5 barres M5 écoulées depuis le fill
                        # (mirror du max_hold fib-fine ci-dessus). NB off-by-one assumé :
                        # le backtest mesure en index de barre (j - fill_idx), le live en
                        # secondes écoulées depuis un fill potentiellement intra-bar →
                        # dérive ±1 barre, sans impact (plateau N∈[3,9], PF>2 partout).
                        fill_dt = datetime.fromisoformat(ft_iso.replace("Z", "+00:00")).replace(
                            tzinfo=None
                        )
                        now_ny = now_utc.replace(tzinfo=UTC).astimezone(_NY_TZ)
                        e_h, e_m = BOS_FVG_SESSION_END
                        past_close = (now_ny.hour * 60 + now_ny.minute) >= (e_h * 60 + e_m)
                        new_day = _current_day_ny(now_utc) != _current_day_ny(fill_dt)
                        elapsed_bars = int((now_utc - fill_dt).total_seconds() / 300)
                        if past_close or new_day or elapsed_bars >= BOS_FVG_MAX_HOLD_BARS_M5:
                            self._close_position_market(tag, info, now_utc)
                # Position encore ouverte (aucun closing trade attribué à ce tag) →
                # on réessaiera au prochain sync. Plus d'attribution contrat-level
                # (source du double-comptage) : la clôture passe par closing_by_tag.
                continue

    # ─────────────────────────────────────────────────────────────────────
    # Helpers idempotents de transition d'état
    # ─────────────────────────────────────────────────────────────────────
    # Extraits de `_sync_broker` pour permettre le double-path :
    #   - polling REST 30 s (path historique, autoritatif)
    #   - WebSocket User Hub (`_apply_realtime_event`, fast path < 1 s)
    #
    # Idempotence garantie par la garde `info["status"]` en tête de chaque
    # helper : un même transition détectée par les deux paths ne déclenche
    # `register_fill/close/cancel_open` qu'une seule fois côté RM, et
    # ne notifie Telegram qu'une seule fois. C'est la pierre angulaire du
    # mode hybride : on peut appeler chaque helper plusieurs fois sans risque.

    def _handle_fill_transition(self, tag: str, info: dict, now_utc: datetime):
        """Transition PENDING → ACTIVE. No-op si status != PENDING."""
        if info.get("status") != _ST_PENDING:
            return

        _log.info("[%s] FILL confirmé — position ouverte", tag)
        info["status"] = _ST_ACTIVE
        info["fill_time"] = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        self.rm.register_fill(tag, when=now_utc)
        status_snap = self.rm.status()
        _evlog.fill(
            tag,
            info["ticker"],
            info["direction"],
            info["entry"],
            status_snap["daily_fills_count"],
            self.rm.user_max_trades_per_day,
        )
        self.tg.notify_fill(
            tag,
            info,
            fills_today=status_snap["daily_fills_count"],
            fills_max=self.rm.user_max_trades_per_day,
        )
        self._maybe_warn_daily_limit(status_snap)

    def _handle_cancel_transition(self, tag: str, info: dict, reason: str = "expiré sans fill"):
        """Transition PENDING → CANCELLED. No-op si status != PENDING."""
        if info.get("status") != _ST_PENDING:
            return

        _log.info("[%s] Ordre annulé / expiré sans fill (%s)", tag, reason)
        _evlog.order_cancelled(tag, reason)
        info["status"] = _ST_CANCELLED
        self.rm.cancel_open(tag)

    def _handle_close_transition(self, tag: str, info: dict, pnl: float, now_utc: datetime):
        """Transition ACTIVE → CLOSED. No-op si status != ACTIVE."""
        if info.get("status") != _ST_ACTIVE:
            return

        _log.info("[%s] Position clôturée — P&L = %+.2f $", tag, pnl)
        info["status"] = _ST_CLOSED
        info["close_pnl"] = pnl
        breached, reason = self.rm.register_close(tag, pnl, when=now_utc)
        status_snap = self.rm.status()
        _evlog.close(
            tag, info["ticker"], pnl, status_snap["realized_day_pnl"], status_snap["cum_pnl"]
        )
        self.tg.notify_close(
            tag,
            info,
            pnl,
            session_pnl=status_snap["realized_day_pnl"],
            cum_pnl=status_snap["cum_pnl"],
        )
        if breached:
            _log.critical("LIMITE TOPSTEP FRANCHIE : %s", reason)
            _evlog.risk_breach(reason)
            self.tg.notify_risk_breach(reason)
        else:
            self._maybe_warn_daily_limit(status_snap)

    def _heal_phantom_positions(self, now_utc: datetime) -> bool:
        """Self-heal one-shot au démarrage : purge de `rm.active_positions` les tags
        sans position ouverte correspondante côté broker (fantômes laissés par une
        clôture non réconciliée — ils réservaient du slack à tort).

        Sécurité : ne purge une position RÉCENTE (ouverte aujourd'hui) que si le
        broker répond une liste NON VIDE (un broker momentanément vide ne doit pas
        faire purger une position légitime du jour). Les positions d'un jour passé
        sont purgées même si le broker renvoie vide (fantômes overnight certains).
        NE touche PAS cum_pnl/peak (recalage = action délibérée, cf. brouillon
        correct_live_state.py). Retourne True si la passe a pu s'exécuter.
        """
        contracts = self.state.get("contracts", {})
        if not contracts:
            return False  # contrats pas encore résolus → retenter au prochain tick
        try:
            positions = self.client.get_positions(self.account_id)
        except Exception as exc:
            _log.warning("self-heal fantômes : get_positions échoué (%s) — retry", exc)
            return False

        open_cids = {p.get("contractId") for p in positions}
        day_ny = _current_day_ny(now_utc)
        removed, freed, kept_prudence = [], 0.0, []
        for tag, order in list(self.rm.active_positions.items()):
            ticker = (order.metadata or {}).get("ticker")
            cid = contracts.get(ticker)
            if cid in open_cids:
                continue  # position réelle confirmée par le broker → garder
            opened = order.opened_at.date() if order.opened_at else None
            stale = opened is None or opened < day_ny
            if positions or stale:
                freed += float(order.risk_usd)
                self.rm.active_positions.pop(tag, None)
                if tag in self.state.get("placed_tags", {}):
                    self.state["placed_tags"][tag]["status"] = _ST_CLOSED
                removed.append(tag)
            else:
                kept_prudence.append(tag)  # récent + broker vide → prudence, on garde

        if kept_prudence:
            _log.warning(
                "self-heal : %d position(s) sans contrepartie broker MAIS récente(s) "
                "et broker vide → gardée(s) par prudence : %s",
                len(kept_prudence),
                kept_prudence,
            )
        if removed:
            _log.warning(
                "self-heal : %d position(s) fantôme(s) purgée(s), slack libéré $%.0f : %s",
                len(removed),
                freed,
                removed,
            )
            try:
                _evlog.error("phantom_heal", f"{len(removed)} fantômes, ${freed:.0f} libérés")
            except Exception:
                pass
            try:
                self.tg.notify_system_error(
                    "phantom_heal",
                    f"Démarrage : {len(removed)} position(s) fantôme(s) purgée(s), "
                    f"slack libéré ${freed:.0f}. Tags : {', '.join(removed)}",
                )
            except Exception:
                pass
        return True

    # ─────────────────────────────────────────────────────────────────────
    # Helpers fib-v4 — invalidation intra-bar (CANCEL / CANCEL_FILL)
    # ─────────────────────────────────────────────────────────────────────

    def _handle_fib_v4_cancel(self, action: dict):
        """
        fib-v4 invalidation pendant la phase PENDING (pivot break intra-bar M1) :
        annule l'ordre limite côté broker et passe le tag local en CANCELLED.

        No-op si :
          • le tag n'existe pas dans state["placed_tags"] (signal pas placé)
          • le tag est déjà CANCELLED/ACTIVE/CLOSED (état terminal local)
        """
        tag = action["tag"]
        placed = self.state.get("placed_tags", {})
        info = placed.get(tag)
        if info is None or info.get("status") != _ST_PENDING:
            return

        order_id = info.get("order_id", -1)
        if not self.dry_run and order_id and order_id > 0:
            try:
                self.client.cancel_order(self.account_id, order_id)
            except Exception as exc:
                _log.warning("[%s] cancel_order API échec : %s", tag, exc)

        self._handle_cancel_transition(
            tag,
            info,
            reason=action.get("reason", "fib-v4 invalidation intra-bar"),
        )

    def _handle_fib_v4_cancel_fill(self, action: dict, now_utc: datetime):
        """
        fib-v4 wick excessive intra-bar (washout détecté en M1) :
          • Si l'ordre est encore PENDING → délégue à _handle_fib_v4_cancel
          • Si l'ordre est ACTIVE (déjà fillé) → close market immédiat

        Le P&L sera réconcilié par le sync broker au prochain tick (REST ou WS).
        """
        tag = action["tag"]
        placed = self.state.get("placed_tags", {})
        info = placed.get(tag)
        if info is None:
            return

        status = info.get("status")
        if status == _ST_PENDING:
            self._handle_fib_v4_cancel(action)
        elif status == _ST_ACTIVE:
            _log.info(
                "[%s] fib-v4 cancel_fill → close market (%s)",
                tag,
                action.get("reason", "wick excess"),
            )
            self._close_position_market(tag, info, now_utc)
            _evlog.order_cancelled(
                tag,
                action.get("reason", "fib-v4 wick excess intra-bar"),
            )

    # ─────────────────────────────────────────────────────────────────────
    # Helper fib-fine-v2 — invalidation pivot break (CANCEL)
    # ─────────────────────────────────────────────────────────────────────

    def _handle_fib_fine_cancel(self, action: dict):
        """
        fib-fine-v2 invalidation pendant la phase PENDING (pivot break mesuré sur
        barres M5 closes) : annule l'ordre limite côté broker et passe le tag local
        en CANCELLED.

        Calqué sur `_handle_fib_v4_cancel`. No-op si :
          • le tag n'existe pas dans state["placed_tags"] (signal pas placé)
          • le tag n'est pas PENDING (état terminal local)
        """
        tag = action["tag"]
        placed = self.state.get("placed_tags", {})
        info = placed.get(tag)
        if info is None or info.get("status") != _ST_PENDING:
            return

        order_id = info.get("order_id", -1)
        if not self.dry_run and order_id and order_id > 0:
            try:
                self.client.cancel_order(self.account_id, order_id)
            except Exception as exc:
                _log.warning("[%s] cancel_order API échec : %s", tag, exc)

        self._handle_cancel_transition(
            tag,
            info,
            reason=action.get("reason", "fib-fine invalidation pivot break"),
        )

    def _handle_bos_fvg_cancel(self, action: dict):
        """
        bos-fvg-v1 invalidation pendant la phase PENDING (close au-delà du FVG sur
        barres M5 closes) : annule l'ordre limite côté broker et passe le tag local
        en CANCELLED. Calqué sur `_handle_fib_fine_cancel`. No-op si le tag n'existe
        pas ou n'est pas PENDING.
        """
        tag = action["tag"]
        placed = self.state.get("placed_tags", {})
        info = placed.get(tag)
        if info is None or info.get("status") != _ST_PENDING:
            return

        order_id = info.get("order_id", -1)
        if not self.dry_run and order_id and order_id > 0:
            try:
                self.client.cancel_order(self.account_id, order_id)
            except Exception as exc:
                _log.warning("[%s] cancel_order API échec : %s", tag, exc)

        self._handle_cancel_transition(
            tag,
            info,
            reason=action.get("reason", "bos-fvg invalidation FVG"),
        )

    # ─────────────────────────────────────────────────────────────────────
    # Realtime — drain + dispatch events SignalR
    # ─────────────────────────────────────────────────────────────────────
    # Le drain est appelé :
    #   • Au début de chaque run_tick() — pour que le _sync_broker REST qui
    #     suit voie déjà l'état mis à jour par le WS
    #   • Au début de chaque micro-sync 30 s (live.py:272) — pour réagir
    #     aux fills/closes en quasi-temps réel
    #
    # `_apply_realtime_event` route l'event vers l'un des 3 helpers
    # idempotents. Si le WS et le polling détectent le même event, le 2e
    # appel est un no-op (status check en tête de chaque helper).

    def _drain_realtime(self):
        """Drain non-bloquant de la queue WS. Sans effet si realtime désactivé."""
        if self.rt is None:
            return
        # Maintenance compteur d'erreurs horaire (auto-disable >10 err/h)
        now_mono = __import__("time").monotonic()
        if self._rt_error_hour_start == 0.0 or (now_mono - self._rt_error_hour_start) > 3600:
            self._rt_error_hour_start = now_mono
            self._rt_error_count_hour = 0

        try:
            events = self.rt.drain_events(max_events=500)
        except Exception as exc:
            _log.exception("Realtime: drain_events échoué : %s", exc)
            return

        if not events:
            self._check_realtime_outage()
            return

        for evt in events:
            try:
                self._apply_realtime_event(evt)
                if PROJECTX_REALTIME_DEBUG_EVENTS:
                    _evlog.realtime_event(evt.kind, tag=evt.custom_tag)
            except Exception as exc:
                self._rt_error_count_hour += 1
                _log.exception("Realtime: apply_event(%s) échoué : %s", evt.kind, exc)
                _evlog.realtime_event_error(evt.kind, str(exc))

        # Auto-disable si > 10 erreurs/heure
        if self._rt_error_count_hour > 10:
            _log.critical("Realtime: > 10 erreurs/h — auto-disable WS")
            self.tg.notify_system_error(
                "realtime_auto_disabled",
                f"WS désactivé après {self._rt_error_count_hour} erreurs/h. Polling REST continue.",
            )
            try:
                self.rt.stop()
            except Exception:
                pass
            self.rt = None
            self.state["realtime_auto_disabled_until"] = (
                __import__("datetime").datetime.utcnow() + __import__("datetime").timedelta(hours=1)
            ).isoformat()

    def _check_realtime_outage(self):
        """Alerte Telegram si WS down > seuil ET marché ouvert (throttle 1 h)."""
        if self.rt is None:
            return
        h = self.rt.health()
        if h["connected"]:
            return
        if h["last_event_age_s"] < PROJECTX_REALTIME_ALERT_OUTAGE_S:
            return
        # Anti-flap : 1 alerte / heure max
        from datetime import datetime as _dt

        last = self.state.get("realtime_last_alert_ts")
        now = _dt.utcnow()
        if last:
            try:
                last_dt = _dt.fromisoformat(last)
                if (now - last_dt).total_seconds() < 3600:
                    return
            except Exception:
                pass
        self.state["realtime_last_alert_ts"] = now.isoformat()
        _evlog.realtime_disconnected(f"WS down {h['last_event_age_s']:.0f}s, polling REST intact")
        try:
            self.tg.notify_system_error(
                "realtime_outage",
                f"WS down {h['last_event_age_s']:.0f}s, REST polling intact.",
            )
        except Exception as exc:
            _log.debug("Telegram outage notify échoué : %s", exc)

    # ─────────────────────────────────────────────────────────────────────
    # Market Hub realtime (Phase C — buffer M1 pour F2 intra-bar M15)
    # ─────────────────────────────────────────────────────────────────────

    def _maybe_start_market_realtime(self) -> None:
        """
        Démarre lazily le client Market Hub + buffer M1 dès que les
        contract_ids sont résolus (post `_ensure_contracts`). Sans effet si :
          - PROJECTX_MARKET_REALTIME_ENABLED=False
          - déjà démarré
          - aucun contract_id résolu

        Auto-disable a posteriori géré par `_drain_market_realtime`.
        """
        if not PROJECTX_MARKET_REALTIME_ENABLED:
            return
        if self.market_rt is not None:
            return
        contract_ids = [v for v in (self.state.get("contracts") or {}).values() if v]
        if not contract_ids:
            _log.warning("MarketRT: aucun contract_id résolu, skip start")
            return
        try:
            self.m1_buffer = M1Buffer(max_minutes=PROJECTX_MARKET_REALTIME_BUFFER_MINUTES)
            self.market_rt = ProjectXMarketRealtimeClient(
                contract_ids=contract_ids,
                token_provider=lambda: self.client.token,
                hub_url=PROJECTX_MARKET_REALTIME_HUB_URL,
                queue_maxsize=PROJECTX_MARKET_REALTIME_QUEUE_MAXSIZE,
                reconnect_delays=PROJECTX_MARKET_REALTIME_RECONNECT_DELAYS,
                max_silence_s=PROJECTX_MARKET_REALTIME_MAX_SILENCE_S,
                force_reauth_s=PROJECTX_MARKET_REALTIME_FORCE_REAUTH_S,
            )
            self.market_rt.start()
            _log.info("MarketRT: démarré pour %d contracts", len(contract_ids))
        except Exception as exc:
            _log.error("MarketRT: init échouée (%s) — fallback M15", exc, exc_info=True)
            self.market_rt = None
            self.m1_buffer = None

    def _drain_market_realtime(self) -> None:
        """
        Drain non-bloquant du Market Hub → alimente le buffer M1. Idempotent.
        Auto-disable si > 10 erreurs/h (même pattern que `_drain_realtime`).
        """
        if self.market_rt is None or self.m1_buffer is None:
            return
        # Maintenance du compteur horaire
        import time as _time

        now_mono = _time.monotonic()
        if (
            self._market_rt_error_hour_start == 0.0
            or (now_mono - self._market_rt_error_hour_start) > 3600
        ):
            self._market_rt_error_hour_start = now_mono
            self._market_rt_error_count_hour = 0

        try:
            events = self.market_rt.drain_events(max_events=5000)
        except Exception as exc:
            _log.exception("MarketRT: drain_events échoué : %s", exc)
            self._market_rt_error_count_hour += 1
            events = []

        for evt in events:
            try:
                self.m1_buffer.consume(evt)
            except Exception as exc:
                self._market_rt_error_count_hour += 1
                _log.exception("MarketRT: buffer.consume échoué : %s", exc)

        # Force-close des bars dont la minute est passée (utile en début/fin
        # de session où aucun trade ne déclencherait la rotation automatique).
        try:
            self.m1_buffer.flush_stale_bars()
        except Exception as exc:
            _log.exception("MarketRT: flush_stale_bars échoué : %s", exc)

        # Auto-disable si > 10 erreurs/heure
        if self._market_rt_error_count_hour > 10:
            _log.critical("MarketRT: > 10 erreurs/h — auto-disable")
            try:
                self.tg.notify_system_error(
                    "market_realtime_auto_disabled",
                    f"Market Hub désactivé après "
                    f"{self._market_rt_error_count_hour} erreurs/h. "
                    "OPR v5.1 retombe sur M15.",
                )
            except Exception:
                pass
            try:
                self.market_rt.stop()
            except Exception:
                pass
            self.market_rt = None
            self.m1_buffer = None

    def _apply_realtime_event(self, evt: RealtimeEvent):
        """
        Dispatch un event WS vers le bon helper de transition.

        Lookup du tag : d'abord par `customTag`, sinon fallback par
        `contract_id` + status (même logique que `_sync_broker` L702-710 :
        les closing trades n'embarquent pas toujours le customTag d'entrée).

        Idempotence : les helpers _handle_*_transition checkent le status
        au début, donc un event répété ou en concurrence avec le polling
        REST est silencieusement ignoré.
        """
        from datetime import datetime as _dt

        now_utc = _dt.utcnow()
        placed = self.state.get("placed_tags", {})

        # ── Lookup du tag ────────────────────────────────────────────────
        tag = None
        info = None
        if evt.custom_tag and evt.custom_tag in placed:
            tag = evt.custom_tag
            info = placed[tag]
        elif evt.contract_id:
            # Fallback : chercher un tag PENDING/ACTIVE sur ce contractId
            for t, i in placed.items():
                if str(i.get("contract_id")) == evt.contract_id and i.get("status") in (
                    _ST_PENDING,
                    _ST_ACTIVE,
                ):
                    tag, info = t, i
                    break
        if tag is None or info is None:
            return  # event sans tag matchable (account / contract inconnu)

        # ── Dispatch par kind ────────────────────────────────────────────
        if evt.kind == "trade" and evt.pnl is not None:
            # Closing trade : NE PAS attribuer le P&L ici. Les trades ne portent
            # pas de customTag → le lookup ci-dessus retombe sur le 1er tag du
            # contrat (faux quand plusieurs stratégies partagent le contrat, et
            # fige le mauvais tag en CLOSED, bloquant la correction REST). La
            # clôture + le P&L réel PAR TAG sont réconciliés par `_sync_broker`
            # (closing_by_tag via orderId→customTag), path REST autoritatif.
            return

        if evt.kind == "position":
            if evt.size is None:
                return
            if evt.size == 0:
                # Position flat sans pnl encore — on attend le trade event
                # qui suivra (le polling REST 30 s rattrape sinon)
                info["_close_seen"] = True
                return
            if evt.size > 0 and info.get("status") == _ST_PENDING:
                # Fill confirmé via WS avant que le poll REST n'arrive
                self._handle_fill_transition(tag, info, now_utc)
            return

        if evt.kind == "order":
            # Mapping status codes ProjectX (confirmé via smoke test 2026-05-18) :
            #   1 = actif / placed (entry order, après placement)
            #   2 = filled (ordre exécuté — pour l'entry, on s'appuie sur le
            #       Position event size>0 plutôt qu'écouter status=2)
            #   3 = cancelled (cible de notre handler)
            #   6 = market in-flight (transitionnel, dure ~100ms → pas terminal)
            #   8 = bracket en attente (SL/TP juste après placement entry)
            # status 5 (rejected) supposé mais non observé au smoke
            # → inclus par robustesse, à valider au burn-in si vu.
            TERMINAL_CANCEL = (3, 5)  # cancelled / rejected (PAS 6 = in-flight)
            if evt.status in TERMINAL_CANCEL and info.get("status") == _ST_PENDING:
                self._handle_cancel_transition(tag, info, f"WS status={evt.status}")
            return

        # account → ignoré pour v1 (diagnostic uniquement)

    # ─────────────────────────────────────────────────────────────────────
    # Clôture forcée d'une position (Fib timeout ou fin de session OPR)
    # ─────────────────────────────────────────────────────────────────────

    def _close_position_market(self, tag: str, info: dict, now_utc: datetime):
        """Envoie un ordre Market pour clôturer la position.

        Ne fixe PAS `status=CLOSED` en live : la clôture (et son P&L RÉEL) est
        réconciliée par `_sync_broker` via le closing trade de l'ordre market
        (customTag `<tag>-EXIT`, ou en repli son orderId mémorisé dans
        `_exit_order_id`). `_exit_sent` empêche de renvoyer un market order tant
        que le closing trade n'est pas vu.
        """
        cid = info["contract_id"]
        n_ct = int(info["n_ct"])
        # Pour clôturer : sens inverse de l'entrée
        close_side = 1 if info["direction"] == "long" else 0  # Sell / Buy

        _log.info("[%s] Clôture forcée market (%s×%d)", tag, cid, n_ct)
        info["_exit_sent"] = True
        if self.dry_run:
            info["status"] = _ST_CLOSED  # pas de broker à réconcilier en simu
            return
        exit_oid = self.client.place_market_order(
            account_id=self.account_id,
            contract_id=cid,
            side=close_side,
            size=n_ct,
            custom_tag=f"{tag}-EXIT"[:64],
        )
        if exit_oid:
            info["_exit_order_id"] = exit_oid

    # ─────────────────────────────────────────────────────────────────────
    # Alertes limites approchantes
    # ─────────────────────────────────────────────────────────────────────

    def _get_broker_day_summary(self, now_utc: datetime) -> dict:
        """
        Résumé réel du compte depuis le broker.

        P&L journalier = solde courant - solde d'ouverture de session.
        Méthode infaillible : capture TOUT (P&L, frais, trades non trackés).
        """
        try:
            accounts = self.client.get_accounts()
            balance = next(
                (float(a["balance"]) for a in accounts if a.get("id") == self.account_id), None
            )

            opening_balance = self.state.get("opening_balance")
            if balance is not None and opening_balance is not None:
                day_pnl = balance - opening_balance
            else:
                day_pnl = None

            # Trades du jour pour le compte des fills
            day_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
                hours=5
            )
            trades = self.client.get_trades_since(self.account_id, day_start)
            valid = [t for t in trades if not t.get("voided")]
            closes = [t for t in valid if t.get("profitAndLoss") is not None]
            total_fees = sum(float(t.get("fees", 0)) for t in valid)

            open_positions = self.client.get_positions(self.account_id)

            return {
                "broker_balance": round(balance, 2) if balance is not None else None,
                "broker_day_pnl": round(day_pnl, 2) if day_pnl is not None else None,
                "broker_day_fees": round(total_fees, 2),
                "broker_fills": len(closes),
                "broker_n_open": len(open_positions),
            }
        except Exception as exc:
            _log.debug("_get_broker_day_summary échoué : %s", exc)
            return {}

    def _maybe_warn_daily_limit(self, status: dict):
        """Envoie une alerte si la perte journalière dépasse 80 % de la limite."""
        day_pnl = status.get("realized_day_pnl", 0.0)
        limit = float(USER_DAILY_LOSS_MAX)
        if day_pnl < 0 and abs(day_pnl) / limit >= 0.80:
            fills_r = status.get("daily_fills_remaining", USER_MAX_TRADES_PER_DAY)
            _evlog.daily_warning(day_pnl, limit)
            self.tg.notify_daily_limit_warning(day_pnl, limit, int(fills_r))

    def _close_all_pending_and_active(
        self, strategy_filter: str | None = None, now_utc: datetime | None = None
    ):
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

            status = info.get("status")
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
        now_utc = datetime.utcnow()
        _log.info(
            "═══ Tick %s UTC ══════════════════════════════════", now_utc.strftime("%Y-%m-%d %H:%M")
        )

        # ── 1. Chargement état ────────────────────────────────────────────
        self._load_state()

        # ── 1b. Vérification marché ouvert ────────────────────────────────
        if not _is_trading_session(now_utc):
            now_ny_str = (
                now_utc.replace(tzinfo=UTC).astimezone(_NY_TZ).strftime("%Y-%m-%d %H:%M NY")
            )
            _log.info("Marché fermé (week-end) — tick ignoré (%s)", now_ny_str)
            # Telegram reste actif pour répondre aux /status sans trader
            self.tg.check_commands(
                placed_tags=self.state.get("placed_tags", {}),
                rm_status=self.rm.status(),
                now_ny=now_ny_str,
                on_pause=lambda: self.set_paused(True),
                on_resume=lambda: self.set_paused(False),
                paused=self.is_paused(),
                today_str=_current_day_ny(now_utc).isoformat(),
            )
            self._save_state()
            return

        # ── 2. Découverte contrats front-month ────────────────────────────
        try:
            self._ensure_contracts()
        except Exception as exc:
            _log.error("_ensure_contracts échoué : %s", exc)
            _evlog.error("ensure_contracts", exc)
            self.tg.notify_system_error("ensure_contracts", exc)
            self._save_state()
            return

        # ── 2b. Phase C : démarrer Market Hub WS dès que les contracts ────
        # sont résolus. Idempotent — no-op si déjà actif ou flag OFF.
        self._maybe_start_market_realtime()

        # ── 3. Synchronisation broker (fills / clôtures) ──────────────────
        # 3a. Drain WS d'abord — le polling REST qui suit voit l'état à jour.
        #     Aucun effet si PROJECTX_REALTIME_ENABLED=False (self.rt is None).
        self._drain_realtime()
        # 3a-bis. Drain Market Hub (buffer M1 alimenté). Sans effet si
        # PROJECTX_MARKET_REALTIME_ENABLED=False.
        self._drain_market_realtime()

        # 3b. Polling REST autoritatif (autour de 30 s en micro-sync via live.py)
        try:
            self._sync_broker(now_utc)
        except Exception as exc:
            _log.error("_sync_broker échoué : %s", exc)
            _evlog.error("sync_broker", exc)
            self.tg.notify_system_error("sync_broker", exc)

        # 3c. Self-heal one-shot au démarrage : purge les positions fantômes
        #     (active_positions sans contrepartie broker → slack réservé à tort).
        #     Garde d'instance → s'exécute une seule fois par vie du daemon, et
        #     retente tant qu'elle n'a pas pu tourner (contrats/broker indispo).
        if not getattr(self, "_phantom_healed", False):
            if self._heal_phantom_positions(now_utc):
                self._phantom_healed = True

        # ── 4. Commandes Telegram entrantes (/status) — toujours actif ──────
        today_str = _current_day_ny(now_utc).isoformat()
        now_ny_str = now_utc.replace(tzinfo=UTC).astimezone(_NY_TZ).strftime("%Y-%m-%d %H:%M NY")
        rm_snap = {**self.rm.status(), **self._get_broker_day_summary(now_utc)}
        self.tg.check_commands(
            placed_tags=self.state.get("placed_tags", {}),
            rm_status=rm_snap,
            now_ny=now_ny_str,
            on_pause=lambda: self.set_paused(True),
            on_resume=lambda: self.set_paused(False),
            paused=self.is_paused(),
            today_str=today_str,
        )

        # ── 5. Fin de session OPR ? ───────────────────────────────────────
        if _opr_session_over(now_utc):
            _log.info("Session OPR terminée (16h30 NY) — clôture en cours")
            self._close_all_pending_and_active(strategy_filter="OPR", now_utc=now_utc)
            if self.state.get("session_report_sent") != today_str:
                snap = self.rm.status()
                broker_snap = self._get_broker_day_summary(now_utc)
                merged = {**snap, **broker_snap}
                pnl_report = (
                    broker_snap.get("broker_day_pnl")
                    if broker_snap.get("broker_day_pnl") is not None
                    else snap.get("realized_day_pnl", 0.0)
                )
                fills_report = (
                    broker_snap.get("broker_fills")
                    if broker_snap.get("broker_fills") is not None
                    else snap.get("daily_fills_count", 0)
                )
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
                balance = next(
                    (float(a["balance"]) for a in accounts if a.get("id") == self.account_id), None
                )
                if balance is not None:
                    day_start_ob = now_utc.replace(
                        hour=0, minute=0, second=0, microsecond=0
                    ) - timedelta(hours=5)
                    trades_ob = self.client.get_trades_since(self.account_id, day_start_ob)
                    valid_ob = [t for t in trades_ob if not t.get("voided")]
                    today_pnl = sum(
                        float(t["profitAndLoss"])
                        for t in valid_ob
                        if t.get("profitAndLoss") is not None
                    )
                    today_fees = sum(float(t.get("fees", 0)) for t in valid_ob)
                    opening = balance - today_pnl + today_fees
                    self.state["opening_balance"] = opening
                    self.state["opening_balance_date"] = today_str
                    _log.info(
                        "Solde d'ouverture recalculé : %.2f $ (balance=%.2f, pnl=%.2f, fees=%.2f)",
                        opening,
                        balance,
                        today_pnl,
                        today_fees,
                    )

                    # Réconciliation du cum_pnl RM depuis le solde broker.
                    # Le solde broker est la source de vérité : il inclut tous
                    # les trades (même ceux dont le P&L n'a pas été capté par le
                    # RM à cause d'un bug ou d'un redémarrage).
                    # On utilise opening_balance (solde en début de session, donc
                    # hors trades intra-jour) pour ne pas polluer avec de
                    # l'unrealized P&L.
                    cum_broker = round(opening - TOPSTEP_ACCOUNT_SIZE, 2)
                    if abs(cum_broker - self.rm.cum_pnl) > 1.0:
                        _log.warning(
                            "Réconciliation cum_pnl : RM=%.2f$ → broker=%.2f$ "
                            "(opening=%.2f, start=%.2f)",
                            self.rm.cum_pnl,
                            cum_broker,
                            opening,
                            TOPSTEP_ACCOUNT_SIZE,
                        )
                        self.rm.cum_pnl = cum_broker
                        self.rm.peak_pnl = max(self.rm.peak_pnl, cum_broker)
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
        new_signals: list[dict] = []

        for ticker in self.tickers:
            try:
                df = self._fetch_bars(ticker)
            except Exception as exc:
                _log.error("_fetch_bars %s échoué : %s", ticker, exc)
                self.tg.notify_system_error(f"fetch_bars {ticker}", exc)
                continue
            if df is None or df.empty:
                self.tg.notify_system_error(f"fetch_bars {ticker}", "Aucune barre reçue")
                continue

            # OPR (v5.1 NQ1/YM1 + v4 legacy via OPR_V4_LIVE_TICKERS).
            # Un ticker absent des deux listes est skippé (ex: MES1 mis en
            # veille le 2026-05-21 — PF 1.16 drag confirmé sur 20 mois).
            if OPR_ENABLED:
                if ticker not in OPR_V5_1_LIVE_TICKERS and ticker not in OPR_V4_LIVE_TICKERS:
                    if ticker not in self._opr_skipped_notified:
                        _evlog._write(
                            "INFO",
                            f"[{ticker}] OPR en veille",
                            raison="hors OPR_V5_1_LIVE_TICKERS et OPR_V4_LIVE_TICKERS",
                        )
                        self._opr_skipped_notified.add(ticker)
                else:
                    sig = self._get_opr_signal(df, ticker, day_ny)
                    if sig and not self._already_placed(sig["tag"]):
                        new_signals.append(sig)

            # fib-v4 — MES1, NQ1, MGC1 uniquement (cf. FIB_V4_TICKERS).
            # Plus aucun fallback fib-v3 : la stratégie historique a été
            # supprimée lors du nettoyage 2026-05-19 (cf. docs/strategies_abandoned.md).
            if FIB_V4_ENABLED and ticker in FIB_V4_TICKERS:
                sig = self._get_fib_v4_signal(df, ticker)
                if sig is not None:
                    action = sig.get("_action")
                    if action == "cancel":
                        self._handle_fib_v4_cancel(sig)
                    elif action == "cancel_fill":
                        self._handle_fib_v4_cancel_fill(sig, now_utc=datetime.utcnow())
                    elif not self._already_placed(sig["tag"]):
                        new_signals.append(sig)

            # fib-fine-v2 — NQ1, MES1 (cf. FIB_FINE_LIVE_TICKERS ; flag OFF par
            # défaut). Barres M5 fetchées en REST dédié (_fetch_bars_m5) — n'utilise
            # PAS le df M15 ci-dessus. Chemin entièrement séparé d'OPR/fib-v4.
            if FIB_FINE_ENABLED and ticker in FIB_FINE_LIVE_TICKERS:
                sig = self._get_fib_fine_signal(ticker)
                if sig is not None:
                    action = sig.get("_action")
                    if action == "cancel":
                        self._handle_fib_fine_cancel(sig)
                    elif not self._already_placed(sig["tag"]):
                        new_signals.append(sig)

            # bos-fvg-v1 — NQ1, MES1 (cf. BOS_FVG_LIVE_TICKERS ; promu 2026-06-03).
            # Barres M5 fetchées en REST dédié (_fetch_bars_m5) — n'utilise PAS le
            # df M15 ci-dessus. Chemin entièrement séparé d'OPR/fib-v4/fib-fine.
            if BOS_FVG_ENABLED and ticker in BOS_FVG_LIVE_TICKERS:
                sig = self._get_bos_fvg_signal(ticker)
                if sig is not None:
                    action = sig.get("_action")
                    if action == "cancel":
                        self._handle_bos_fvg_cancel(sig)
                    elif not self._already_placed(sig["tag"]):
                        new_signals.append(sig)

        # ── 8. Filtre de corrélation ──────────────────────────────────────
        if len(new_signals) > 1:
            before = len(new_signals)
            new_signals = filter_correlated_signals(new_signals)
            if len(new_signals) < before:
                _log.info("Filtre corrélation : %d → %d signal(s)", before, len(new_signals))

        # ── 9. Placement des ordres ───────────────────────────────────────
        if self.is_paused():
            if new_signals:
                _log.info("Bot en pause — %d signal(s) ignoré(s) ce tick", len(new_signals))
                _evlog._write("SYSTEM", f"Pause active — {len(new_signals)} signal(s) ignoré(s)")
        else:
            for sig in new_signals:
                self._place_order(sig)

        # ── 10. Résumé risk manager ───────────────────────────────────────
        status = self.rm.status()
        _log.info(
            "Risk : cum=%.0f$ | jour=%.0f$ | fills=%d/%d | slack daily=%.0f$ trail=%.0f$",
            status["cum_pnl"],
            status["realized_day_pnl"],
            status["daily_fills_count"],
            self.rm.user_max_trades_per_day,
            status["slack_daily"],
            status["slack_trail"],
        )

        # ── 11. Sauvegarde ────────────────────────────────────────────────
        self._save_state()
        _log.info("Tick terminé")
