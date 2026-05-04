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

Conçu pour être appelé avant chaque placement d'ordre :

    rm = PortfolioRiskManager()
    ok, reason = rm.can_open(risk_usd=100)
    if ok:
        broker.place_order(...)
        rm.register_open(trade_id="...", risk_usd=100)
    # Plus tard, après fill exit :
    rm.register_close(trade_id="...", pnl=42.0, exit_time=ts)

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
class _OpenTrade:
    """Position ouverte ou ordre limite armé — risque dollar nominal."""
    risk_usd: float
    opened_at: Optional[datetime] = None
    metadata: Dict = field(default_factory=dict)


@dataclass
class PortfolioRiskManager:
    """
    État du portefeuille pour Topstep 50K (configurable).

    Logique de blocage :
      slack_daily   = TOPSTEP_DAILY_LOSS_MAX + realized_day_pnl − sum(open_risks)
      slack_trail   = cum_pnl − (peak_pnl − TOPSTEP_TRAILING_DD) − sum(open_risks)
      slack         = min(slack_daily, slack_trail)
      autorise si slack ≥ risk_usd × TOPSTEP_SAFETY_MULT

    `sum(open_risks)` = risque cumulé des positions ouvertes ET des ordres
    limites armés non encore filés. Ainsi 3 stratégies armant en parallèle ne
    peuvent pas dépasser globalement le daily $1K.
    """

    # Capital tracking — démarrent à zéro au début du challenge
    cum_pnl: float = 0.0
    peak_pnl: float = 0.0
    realized_day_pnl: float = 0.0
    current_day: Optional[date] = None

    # Streak de jours perdants consécutifs (mêmes règles que risk_topstep.py)
    consec_loss_days: int = 0

    # Compteur de fills sur la journée courante (cap MAX_TRADES_PER_DAY)
    daily_trades_count: int = 0

    # Trades / orders ouverts (par identifiant unique)
    open_trades: Dict[str, _OpenTrade] = field(default_factory=dict)

    # Constantes Topstep (overridables par le caller pour tests)
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

    def _sum_open_risks(self) -> float:
        return sum(t.risk_usd for t in self.open_trades.values())

    def _maybe_roll_day(self, when: Optional[datetime] = None):
        """
        Si la date 'when' diffère de current_day, archive le P&L du jour
        précédent (mise à jour du streak consécutif) et reset realized_day_pnl.
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
        # day_pnl == 0 (pas de trade) : streak inchangé
        self.realized_day_pnl = 0.0
        self.daily_trades_count = 0
        self.current_day = d

    # ────────────────────────────────────────────────────────────────────
    # API publique
    # ────────────────────────────────────────────────────────────────────

    def can_open(self, risk_usd: float = RISK_PER_TRADE_USD,
                 when: Optional[datetime] = None
                 ) -> Tuple[bool, str]:
        """
        Retourne (autorise, raison) avant d'armer un nouvel ordre / position.

        Ordre des checks :
          1. Cap positions ouvertes simultanément (USER_MAX_OPEN_POSITIONS)
          2. Cap nombre de fills journaliers (USER_MAX_TRADES_PER_DAY)
          3. Cap perte journalière utilisateur (USER_DAILY_LOSS_MAX) — déjà atteinte
          4. Streak perdant (CONSEC_LOSS_PAUSE_DAYS)
          5. Slack daily Topstep + slack trailing DD
        """
        self._maybe_roll_day(when)

        # 1. Cap positions ouvertes simultanément
        n_open = len(self.open_trades)
        if (self.user_max_open_positions > 0
                and n_open >= self.user_max_open_positions):
            return False, (
                f"max_open_positions_{n_open}"
                f"/{self.user_max_open_positions}"
            )

        # 2. Cap trades/jour (compteur des fills déjà effectués + des armements
        #    en attente — un ordre limite arme une "trade slot")
        if (self.user_max_trades_per_day > 0
                and self.daily_trades_count >= self.user_max_trades_per_day):
            return False, (
                f"daily_trades_cap_{self.daily_trades_count}"
                f"/{self.user_max_trades_per_day}"
            )

        # 2. Cap perte journalière utilisateur — sur P&L RÉALISÉ uniquement
        # (interprétation classique "stop-loss journée" : on arrête quand on a
        # effectivement perdu N$, pas sur un pire cas hypothétique des positions
        # ouvertes — ce qui bloquerait dès la 2e position parallèle).
        if self.realized_day_pnl <= -self.user_daily_loss_max:
            return False, (
                f"user_daily_loss_realized_{self.realized_day_pnl:+.0f}_"
                f"below_{-self.user_daily_loss_max:.0f}"
            )

        # 3. Circuit breaker streak perdant
        if (self.consec_loss_pause_days > 0
                and self.consec_loss_days >= self.consec_loss_pause_days):
            return False, f"consec_loss_pause_{self.consec_loss_days}"

        # 4. Slacks Topstep (daily $1K + trailing $2K)
        open_risk = self._sum_open_risks()
        slack_daily = (
            self.daily_loss_limit + self.realized_day_pnl - open_risk
        )
        trail_floor = self.peak_pnl - self.trailing_dd_limit
        slack_trail = self.cum_pnl - trail_floor - open_risk

        slack = min(slack_daily, slack_trail)
        threshold = risk_usd * self.safety_mult

        if slack < threshold:
            return False, (
                f"slack_{slack:.0f}_below_{threshold:.0f}_"
                f"(daily={slack_daily:.0f},trail={slack_trail:.0f},"
                f"open_risk={open_risk:.0f})"
            )
        return True, "ok"

    def register_open(self, trade_id: str, risk_usd: float,
                      when: Optional[datetime] = None,
                      metadata: Optional[Dict] = None):
        """Enregistre un ordre armé / position ouverte (occupe du slack +
        incrémente le compteur de trades du jour)."""
        self._maybe_roll_day(when)
        self.open_trades[trade_id] = _OpenTrade(
            risk_usd=float(risk_usd),
            opened_at=when,
            metadata=metadata or {},
        )
        self.daily_trades_count += 1

    def register_close(self, trade_id: str, pnl: float,
                       when: Optional[datetime] = None) -> Tuple[bool, str]:
        """
        Enregistre la clôture d'un trade. Met à jour cum_pnl, peak_pnl,
        realized_day_pnl. Retourne (breach, reason) si une limite est franchie.
        """
        self._maybe_roll_day(when)
        if trade_id in self.open_trades:
            self.open_trades.pop(trade_id)
        # Si trade_id inconnu, on accepte le P&L quand même (pas d'erreur dure)
        self.cum_pnl += float(pnl)
        self.realized_day_pnl += float(pnl)
        if self.cum_pnl > self.peak_pnl:
            self.peak_pnl = self.cum_pnl

        # Détection breach a posteriori (informatif)
        breaches = []
        if self.realized_day_pnl <= -self.daily_loss_limit:
            breaches.append(f"daily_loss_breached_{self.realized_day_pnl:.0f}")
        trail_floor = self.peak_pnl - self.trailing_dd_limit
        if self.cum_pnl <= trail_floor:
            breaches.append(f"trailing_dd_breached_dd={self.cum_pnl - trail_floor:.0f}")
        if breaches:
            return True, ";".join(breaches)
        return False, "ok"

    def cancel_open(self, trade_id: str) -> bool:
        """Libère le slack pour un ordre limite annulé sans fill."""
        if trade_id in self.open_trades:
            self.open_trades.pop(trade_id)
            return True
        return False

    def reset(self):
        """Remise à zéro complète (début d'un nouveau challenge)."""
        self.cum_pnl = 0.0
        self.peak_pnl = 0.0
        self.realized_day_pnl = 0.0
        self.current_day = None
        self.consec_loss_days = 0
        self.daily_trades_count = 0
        self.open_trades.clear()

    def status(self) -> Dict:
        """Snapshot lisible pour monitoring / logging."""
        open_risk = self._sum_open_risks()
        trail_floor = self.peak_pnl - self.trailing_dd_limit
        return {
            "cum_pnl": self.cum_pnl,
            "peak_pnl": self.peak_pnl,
            "realized_day_pnl": self.realized_day_pnl,
            "current_day": str(self.current_day) if self.current_day else None,
            "consec_loss_days": self.consec_loss_days,
            "daily_trades_count": self.daily_trades_count,
            "daily_trades_remaining": max(
                0, self.user_max_trades_per_day - self.daily_trades_count
            ),
            "user_daily_loss_remaining": max(
                0.0, self.user_daily_loss_max + self.realized_day_pnl
            ),
            "n_open": len(self.open_trades),
            "open_risk_usd": open_risk,
            "slack_daily": (
                self.daily_loss_limit + self.realized_day_pnl - open_risk
            ),
            "slack_trail": self.cum_pnl - trail_floor - open_risk,
            "target_remaining": self.profit_target - self.cum_pnl,
        }
