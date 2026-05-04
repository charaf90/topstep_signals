"""
Garde-fou Topstep PORTEFEUILLE — vue globale chronologique sur toutes les
stratégies et tous les actifs.

Différence vs `risk_topstep.py` (per-stratégie / per-ticker) :
  - `risk_topstep` : utilisé en backtest, chaque stratégie maintient ses propres
    `cum_pnl`, `peak_pnl`, `day_pnl`. Cohérent en backtest car les boucles
    sont séquentielles et non-overlap entre stratégies.
  - `risk_portfolio` : à utiliser en LIVE quand 3 stratégies × 3 actifs
    peuvent armer / fill simultanément. Maintient un état GLOBAL et bloque
    tout nouveau trade qui pourrait, dans le pire cas, dépasser les limites
    Topstep en supposant que TOUTES les positions ouvertes touchent leur SL.

Distinction ordres limites / fills :
  - `register_open`  → arm un ordre limite (risque réservé, AUCUN compteur fill)
  - `register_fill`  → ordre exécuté (position active, incrémente daily_fills_count)
  - `cancel_open`    → ordre expiré sans fill (libère le slack, AUCUN compteur)
  - `register_close` → position clôturée (met à jour cum_pnl)

Ainsi 10 ordres limites peuvent être armés simultanément sans consommer le
cap journalier de 3 fills — seuls les 3 premiers fills bloquent les suivants.

Flux d'utilisation :
    rm = PortfolioRiskManager()
    ok, reason = rm.can_open(risk_usd=100)
    if ok:
        broker.place_limit_order(...)
        rm.register_open(trade_id="...", risk_usd=100)
    # Quand l'ordre est exécuté (fill confirmé par le broker) :
    rm.register_fill(trade_id="...")
    # Quand la position est fermée :
    rm.register_close(trade_id="...", pnl=42.0)
    # Si l'ordre expire sans fill :
    rm.cancel_open(trade_id="...")

Le module est pure-python (pas d'I/O) — l'utilisateur (live runner) gère
la persistance d'état entre runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, Optional, Tuple

from config import (
    TOPSTEP_DAILY_LOSS_MAX, TOPSTEP_TRAILING_DD, TOPSTEP_PROFIT_TARGET,
    TOPSTEP_SAFETY_MULT, RISK_PER_TRADE_USD, CONSEC_LOSS_PAUSE_DAYS,
    USER_DAILY_LOSS_MAX, USER_MAX_TRADES_PER_DAY, USER_MAX_OPEN_POSITIONS,
)


@dataclass
class _Order:
    """Ordre limite armé ou position active — risque dollar nominal."""
    risk_usd: float
    opened_at: Optional[datetime] = None
    metadata: Dict = field(default_factory=dict)


@dataclass
class PortfolioRiskManager:
    """
    État du portefeuille pour Topstep 50K (configurable).

    Deux pools d'ordres sont maintenus séparément :
      - pending_orders  : ordres limites armés, pas encore filés
      - active_positions: positions filées, en cours de détention

    Logique de blocage (can_open) :
      1. Cap positions ACTIVES simultanées (USER_MAX_OPEN_POSITIONS)
      2. Cap fills journaliers (USER_MAX_TRADES_PER_DAY) — seuls les fills comptent
      3. Cap perte journalière réalisée (USER_DAILY_LOSS_MAX)
      4. Streak perdant (CONSEC_LOSS_PAUSE_DAYS)
      5. Slack Topstep daily + trailing DD (pire-cas : tous pending + active → SL)

    slack_daily = TOPSTEP_DAILY_LOSS_MAX + realized_day_pnl − (pending_risk + active_risk)
    slack_trail = cum_pnl − (peak_pnl − TOPSTEP_TRAILING_DD) − (pending_risk + active_risk)
    autorise si min(slack_daily, slack_trail) ≥ risk_usd × TOPSTEP_SAFETY_MULT
    """

    # Capital tracking — démarrent à zéro au début du challenge
    cum_pnl: float = 0.0
    peak_pnl: float = 0.0
    realized_day_pnl: float = 0.0
    current_day: Optional[date] = None

    # Streak de jours perdants consécutifs
    consec_loss_days: int = 0

    # Compteur de fills confirmés sur la journée courante
    daily_fills_count: int = 0

    # Ordres limites armés (en attente de fill)
    pending_orders: Dict[str, _Order] = field(default_factory=dict)
    # Positions filées actives (en cours)
    active_positions: Dict[str, _Order] = field(default_factory=dict)

    # Constantes Topstep (overridables pour tests)
    daily_loss_limit: float = float(TOPSTEP_DAILY_LOSS_MAX)
    trailing_dd_limit: float = float(TOPSTEP_TRAILING_DD)
    profit_target: float = float(TOPSTEP_PROFIT_TARGET)
    safety_mult: float = float(TOPSTEP_SAFETY_MULT)
    consec_loss_pause_days: int = int(CONSEC_LOSS_PAUSE_DAYS)

    # Contraintes UTILISATEUR (plus strictes que Topstep)
    user_daily_loss_max: float = float(USER_DAILY_LOSS_MAX)
    user_max_trades_per_day: int = int(USER_MAX_TRADES_PER_DAY)
    user_max_open_positions: int = int(USER_MAX_OPEN_POSITIONS)

    # ────────────────────────────────────────────────────────────────────
    # Helpers internes
    # ────────────────────────────────────────────────────────────────────

    def _total_reserved_risk(self) -> float:
        """Risque cumulé pire-cas : tous pending + tous active → SL."""
        return (sum(o.risk_usd for o in self.pending_orders.values())
                + sum(o.risk_usd for o in self.active_positions.values()))

    def _maybe_roll_day(self, when: Optional[datetime] = None):
        """
        Si la date 'when' diffère de current_day, archive le P&L du jour
        précédent (mise à jour du streak consécutif) et reset les compteurs
        journaliers.
        """
        d = (when or datetime.utcnow()).date()
        if self.current_day is None:
            self.current_day = d
            return
        if d == self.current_day:
            return
        # Roll de jour
        if self.realized_day_pnl < 0:
            self.consec_loss_days += 1
        elif self.realized_day_pnl > 0:
            self.consec_loss_days = 0
        # day_pnl == 0 (pas de fill) : streak inchangé
        self.realized_day_pnl = 0.0
        self.daily_fills_count = 0
        self.current_day = d

    # ────────────────────────────────────────────────────────────────────
    # API publique
    # ────────────────────────────────────────────────────────────────────

    def can_open(self, risk_usd: float = RISK_PER_TRADE_USD,
                 when: Optional[datetime] = None) -> Tuple[bool, str]:
        """
        Retourne (autorise, raison) avant d'armer un nouvel ordre limite.

        Ordre des checks :
          1. Cap positions ACTIVES simultanément (USER_MAX_OPEN_POSITIONS)
          2. Cap fills journaliers (USER_MAX_TRADES_PER_DAY)
          3. Cap perte journalière réalisée (USER_DAILY_LOSS_MAX)
          4. Streak perdant (CONSEC_LOSS_PAUSE_DAYS)
          5. Slack Topstep daily + trailing DD (pire-cas tous pending + actif)
        """
        self._maybe_roll_day(when)

        # 1. Cap positions actives (filées) simultanément
        n_active = len(self.active_positions)
        if (self.user_max_open_positions > 0
                and n_active >= self.user_max_open_positions):
            return False, (
                f"max_active_positions_{n_active}"
                f"/{self.user_max_open_positions}"
            )

        # 2. Cap fills journaliers — inutile d'armer si la journée est pleine
        if (self.user_max_trades_per_day > 0
                and self.daily_fills_count >= self.user_max_trades_per_day):
            return False, (
                f"daily_fills_cap_{self.daily_fills_count}"
                f"/{self.user_max_trades_per_day}"
            )

        # 3. Cap perte journalière sur P&L RÉALISÉ uniquement
        if self.realized_day_pnl <= -self.user_daily_loss_max:
            return False, (
                f"user_daily_loss_realized_{self.realized_day_pnl:+.0f}_"
                f"below_{-self.user_daily_loss_max:.0f}"
            )

        # 4. Circuit breaker streak perdant
        if (self.consec_loss_pause_days > 0
                and self.consec_loss_days >= self.consec_loss_pause_days):
            return False, f"consec_loss_pause_{self.consec_loss_days}"

        # 5. Slacks Topstep (daily $1K + trailing $2K)
        #    Pire cas : tous les ordres en attente ET le nouvel ordre → SL
        reserved = self._total_reserved_risk()
        slack_daily = (
            self.daily_loss_limit + self.realized_day_pnl - reserved
        )
        trail_floor = self.peak_pnl - self.trailing_dd_limit
        slack_trail = self.cum_pnl - trail_floor - reserved

        slack = min(slack_daily, slack_trail)
        threshold = risk_usd * self.safety_mult

        if slack < threshold:
            return False, (
                f"slack_{slack:.0f}_below_{threshold:.0f}_"
                f"(daily={slack_daily:.0f},trail={slack_trail:.0f},"
                f"reserved={reserved:.0f})"
            )
        return True, "ok"

    def register_open(self, trade_id: str, risk_usd: float,
                      when: Optional[datetime] = None,
                      metadata: Optional[Dict] = None):
        """
        Enregistre un ordre limite armé.
        Réserve le risque dollar dans le pool pending — NE compte PAS
        comme un fill journalier. Appeler register_fill() quand le fill
        est confirmé par le broker.
        """
        self._maybe_roll_day(when)
        self.pending_orders[trade_id] = _Order(
            risk_usd=float(risk_usd),
            opened_at=when,
            metadata=metadata or {},
        )

    def register_fill(self, trade_id: str,
                      when: Optional[datetime] = None) -> bool:
        """
        Confirme le fill d'un ordre limite.
        Déplace trade_id de pending_orders → active_positions et
        incrémente daily_fills_count. Retourne False si l'id est inconnu.
        """
        self._maybe_roll_day(when)
        if trade_id in self.pending_orders:
            order = self.pending_orders.pop(trade_id)
            self.active_positions[trade_id] = order
        elif trade_id not in self.active_positions:
            # trade_id inconnu — on crée une position fantôme sans risque réservé
            self.active_positions[trade_id] = _Order(risk_usd=0.0, opened_at=when)
        self.daily_fills_count += 1
        return True

    def register_close(self, trade_id: str, pnl: float,
                       when: Optional[datetime] = None) -> Tuple[bool, str]:
        """
        Enregistre la clôture d'une position.
        Met à jour cum_pnl, peak_pnl, realized_day_pnl.
        Retourne (breach, reason) si une limite Topstep est franchie.
        """
        self._maybe_roll_day(when)
        # Retire de la position active (ou pending si jamais fill confirmé)
        self.active_positions.pop(trade_id, None)
        self.pending_orders.pop(trade_id, None)

        self.cum_pnl += float(pnl)
        self.realized_day_pnl += float(pnl)
        if self.cum_pnl > self.peak_pnl:
            self.peak_pnl = self.cum_pnl

        breaches = []
        if self.realized_day_pnl <= -self.daily_loss_limit:
            breaches.append(f"daily_loss_breached_{self.realized_day_pnl:.0f}")
        trail_floor = self.peak_pnl - self.trailing_dd_limit
        if self.cum_pnl <= trail_floor:
            breaches.append(
                f"trailing_dd_breached_dd={self.cum_pnl - trail_floor:.0f}"
            )
        if breaches:
            return True, ";".join(breaches)
        return False, "ok"

    def cancel_open(self, trade_id: str) -> bool:
        """
        Annule un ordre limite expiré sans fill.
        Libère le risque réservé — NE modifie PAS daily_fills_count.
        """
        if trade_id in self.pending_orders:
            self.pending_orders.pop(trade_id)
            return True
        return False

    def reset(self):
        """Remise à zéro complète (début d'un nouveau challenge)."""
        self.cum_pnl = 0.0
        self.peak_pnl = 0.0
        self.realized_day_pnl = 0.0
        self.current_day = None
        self.consec_loss_days = 0
        self.daily_fills_count = 0
        self.pending_orders.clear()
        self.active_positions.clear()

    def status(self) -> Dict:
        """Snapshot lisible pour monitoring / logging."""
        reserved = self._total_reserved_risk()
        trail_floor = self.peak_pnl - self.trailing_dd_limit
        return {
            "cum_pnl": self.cum_pnl,
            "peak_pnl": self.peak_pnl,
            "realized_day_pnl": self.realized_day_pnl,
            "current_day": str(self.current_day) if self.current_day else None,
            "consec_loss_days": self.consec_loss_days,
            "daily_fills_count": self.daily_fills_count,
            "daily_fills_remaining": max(
                0, self.user_max_trades_per_day - self.daily_fills_count
            ),
            "user_daily_loss_remaining": max(
                0.0, self.user_daily_loss_max + self.realized_day_pnl
            ),
            "n_pending": len(self.pending_orders),
            "n_active": len(self.active_positions),
            "reserved_risk_usd": reserved,
            "slack_daily": (
                self.daily_loss_limit + self.realized_day_pnl - reserved
            ),
            "slack_trail": self.cum_pnl - trail_floor - reserved,
            "target_remaining": self.profit_target - self.cum_pnl,
        }
