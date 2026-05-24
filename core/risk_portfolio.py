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

from config import (
    CHALLENGE_ADAPTIVE_SIZING_ENABLED,
    CHALLENGE_BYPASS_USER_DAILY_LIMIT,
    CHALLENGE_CONSISTENCY_BEST_DAY_MAX_USD,
    CHALLENGE_RESET_DAY,
    CONSEC_LOSS_PAUSE_DAYS,
    RISK_PER_TRADE_USD,
    TOPSTEP_DAILY_LOSS_MAX,
    TOPSTEP_PROFIT_TARGET,
    TOPSTEP_SAFETY_MULT,
    TOPSTEP_TRAILING_DD,
    USER_DAILY_LOSS_MAX,
    USER_MAX_OPEN_POSITIONS,
    USER_MAX_TRADES_PER_DAY,
)


@dataclass
class _Order:
    """Ordre limite armé ou position active — risque dollar nominal."""

    risk_usd: float
    opened_at: datetime | None = None
    metadata: dict = field(default_factory=dict)


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
    current_day: date | None = None

    # Streak de jours perdants consécutifs
    consec_loss_days: int = 0

    # Compteur de fills confirmés sur la journée courante
    daily_fills_count: int = 0

    # Ordres limites armés (en attente de fill)
    pending_orders: dict[str, _Order] = field(default_factory=dict)
    # Positions filées actives (en cours)
    active_positions: dict[str, _Order] = field(default_factory=dict)

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

    # Tracking du reset mensuel (challenge Topstep). Persistés dans live_state.json.
    last_reset_month: int | None = None
    last_reset_year: int | None = None
    last_monthly_reset_at: datetime | None = None

    # ────────────────────────────────────────────────────────────────────
    # Helpers internes
    # ────────────────────────────────────────────────────────────────────

    def _total_reserved_risk(self) -> float:
        """
        Risque cumulé des positions FILLED uniquement (active_positions).
        Les ordres pending limit ne consomment plus le slack — décision 2026-05-21
        après backtest extensif :
          • Pending ≠ exposition réelle (limit orders fillent à 30-50% taux moyen)
          • Double protection redondante avec Topstep DLL $950 hard en place
          • Bloquait les 3èmes signaux en attente (cf. session 2026-05-20)
          • Aucune cascade SL multi-trade détectée en backtest (analyse streaks
            cf. output/sl_streaks/report.md — edge persiste après N SL)
        Pour réactiver l'ancien comportement (worst-case pending+active) : ajouter
            + sum(o.risk_usd for o in self.pending_orders.values())
        """
        return sum(o.risk_usd for o in self.active_positions.values())

    def _maybe_roll_day(self, when: datetime | None = None):
        """
        Si la date 'when' diffère de current_day, archive le P&L du jour
        précédent (mise à jour du streak consécutif) et reset les compteurs
        journaliers. Vérifie aussi si un reset mensuel (challenge Topstep)
        doit s'appliquer.
        """
        d = (when or datetime.utcnow()).date()
        if self.current_day is None:
            self.current_day = d
            self._maybe_roll_month(when)
            return
        if d == self.current_day:
            self._maybe_roll_month(when)
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
        # Vérification reset mensuel après mise à jour du jour
        self._maybe_roll_month(when)

    def _maybe_roll_month(self, when: datetime | None = None) -> bool:
        """
        Reset mensuel du challenge Topstep : remet à zéro cum_pnl, peak_pnl
        et consec_loss_days quand on franchit CHALLENGE_RESET_DAY (jour 2 du
        mois par défaut). Idempotent grâce à last_reset_month/year.

        NE TOUCHE PAS aux positions ouvertes (pending_orders, active_positions)
        — celles-ci suivent leur cycle de vie normal.

        Retourne True si un reset a été effectué.
        """
        if not CHALLENGE_ADAPTIVE_SIZING_ENABLED:
            return False
        now = when or datetime.utcnow()
        d = now.date() if isinstance(now, datetime) else now
        if d.day < CHALLENGE_RESET_DAY:
            return False
        if self.last_reset_month == d.month and self.last_reset_year == d.year:
            return False
        # Exécute le reset
        self.cum_pnl = 0.0
        self.peak_pnl = 0.0
        self.consec_loss_days = 0
        self.last_reset_month = d.month
        self.last_reset_year = d.year
        self.last_monthly_reset_at = (
            now if isinstance(now, datetime) else datetime.combine(d, datetime.min.time())
        )
        return True

    # ────────────────────────────────────────────────────────────────────
    # API publique
    # ────────────────────────────────────────────────────────────────────

    def can_open(
        self,
        risk_usd: float = RISK_PER_TRADE_USD,
        when: datetime | None = None,
        tp_gain_usd: float | None = None,
    ) -> tuple[bool, str]:
        """
        Retourne (autorise, raison) avant d'armer un nouvel ordre limite.

        Args:
            risk_usd    : risque dollar du trade candidat (SL atteint).
            when        : timestamp de référence (défaut : maintenant).
            tp_gain_usd : gain dollar maximal si le TP est atteint, en valeur
                          absolue. Sert au check de cohérence 50% (Combine).
                          Optionnel : si None ou 0, le check est skippé
                          (rétrocompatibilité avec backtest / tests unitaires).

        Ordre des checks :
          1. Cap positions ACTIVES simultanément (USER_MAX_OPEN_POSITIONS)
          2. Cap fills journaliers (USER_MAX_TRADES_PER_DAY)
          3. Cap perte journalière réalisée (USER_DAILY_LOSS_MAX)
          4. Streak perdant (CONSEC_LOSS_PAUSE_DAYS)
          5. Slack Topstep daily + trailing DD (pire-cas tous pending + actif)
          6. Slack USER daily (optionnel, bypass challenge)
          7. Cohérence 50% Topstep — Combine uniquement (cf. tp_gain_usd)
        """
        self._maybe_roll_day(when)

        # 1. Cap positions actives (filées) simultanément
        n_active = len(self.active_positions)
        if self.user_max_open_positions > 0 and n_active >= self.user_max_open_positions:
            return False, (f"max_active_positions_{n_active}" f"/{self.user_max_open_positions}")

        # 2. Cap fills journaliers — inutile d'armer si la journée est pleine
        if (
            self.user_max_trades_per_day > 0
            and self.daily_fills_count >= self.user_max_trades_per_day
        ):
            return False, (
                f"daily_fills_cap_{self.daily_fills_count}" f"/{self.user_max_trades_per_day}"
            )

        # 3. Cap perte journalière sur P&L RÉALISÉ uniquement
        #    Bypassé en mode challenge adaptatif (seules les limites Topstep
        #    dures restent actives — cf. config CHALLENGE_BYPASS_USER_DAILY_LIMIT).
        challenge_bypass_user_daily = (
            CHALLENGE_ADAPTIVE_SIZING_ENABLED and CHALLENGE_BYPASS_USER_DAILY_LIMIT
        )
        if not challenge_bypass_user_daily and self.realized_day_pnl <= -self.user_daily_loss_max:
            return False, (
                f"user_daily_loss_realized_{self.realized_day_pnl:+.0f}_"
                f"below_{-self.user_daily_loss_max:.0f}"
            )

        # 4. Circuit breaker streak perdant
        if self.consec_loss_pause_days > 0 and self.consec_loss_days >= self.consec_loss_pause_days:
            return False, f"consec_loss_pause_{self.consec_loss_days}"

        # 5. Slacks Topstep (daily $1K + trailing $2K)
        #    Pire cas : tous les ordres en attente ET le nouvel ordre → SL
        reserved = self._total_reserved_risk()
        slack_daily = self.daily_loss_limit + self.realized_day_pnl - reserved
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

        # 6. Slack USER daily — pire cas : (reserved + nouveau trade) → tous SL
        #    garantit que même si tout le monde touche son SL simultanément,
        #    la perte réalisée ne dépasse pas user_daily_loss_max.
        #    Bypassé en mode challenge adaptatif.
        if self.user_daily_loss_max > 0 and not challenge_bypass_user_daily:
            slack_user = self.user_daily_loss_max + self.realized_day_pnl - reserved
            if slack_user < risk_usd:
                return False, (f"user_daily_slack_{slack_user:.0f}_below_{risk_usd:.0f}")

        # 7. Garde-fou règle de cohérence 50% Topstep — Combine uniquement.
        #    Tant que cum_pnl < profit_target, on est en phase d'évaluation
        #    et la règle 50% (best_day ≤ 50% du profit cumulé) peut nous coller
        #    une auto-augmentation du target. On bloque tout trade dont le
        #    TP plein pousserait le realized_day_pnl au-delà du seuil de garde.
        if (
            CHALLENGE_ADAPTIVE_SIZING_ENABLED
            and tp_gain_usd is not None
            and tp_gain_usd > 0
            and self.cum_pnl < self.profit_target
        ):
            rdp_post_tp = self.realized_day_pnl + float(tp_gain_usd)
            if rdp_post_tp > CHALLENGE_CONSISTENCY_BEST_DAY_MAX_USD:
                return False, (
                    f"consistency_50_cap_rdp_post_tp_{rdp_post_tp:.0f}_"
                    f"above_{CHALLENGE_CONSISTENCY_BEST_DAY_MAX_USD:.0f}"
                )

        return True, "ok"

    def register_open(
        self,
        trade_id: str,
        risk_usd: float,
        when: datetime | None = None,
        metadata: dict | None = None,
    ):
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

    def register_fill(self, trade_id: str, when: datetime | None = None) -> bool:
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

    def register_close(
        self, trade_id: str, pnl: float, when: datetime | None = None
    ) -> tuple[bool, str]:
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
            breaches.append(f"trailing_dd_breached_dd={self.cum_pnl - trail_floor:.0f}")
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

    def status(self) -> dict:
        """Snapshot lisible pour monitoring / logging."""
        reserved = self._total_reserved_risk()
        trail_floor = self.peak_pnl - self.trailing_dd_limit
        # Marge restante avant blocage règle 50% (en Combine uniquement)
        in_combine = self.cum_pnl < self.profit_target
        consistency_cap_remaining = (
            CHALLENGE_CONSISTENCY_BEST_DAY_MAX_USD - self.realized_day_pnl
            if in_combine and CHALLENGE_ADAPTIVE_SIZING_ENABLED
            else None
        )
        return {
            "cum_pnl": self.cum_pnl,
            "peak_pnl": self.peak_pnl,
            "realized_day_pnl": self.realized_day_pnl,
            "current_day": str(self.current_day) if self.current_day else None,
            "consec_loss_days": self.consec_loss_days,
            "daily_fills_count": self.daily_fills_count,
            "daily_fills_remaining": max(0, self.user_max_trades_per_day - self.daily_fills_count),
            "user_daily_loss_remaining": max(0.0, self.user_daily_loss_max + self.realized_day_pnl),
            "n_pending": len(self.pending_orders),
            "n_active": len(self.active_positions),
            "reserved_risk_usd": reserved,
            "slack_daily": (self.daily_loss_limit + self.realized_day_pnl - reserved),
            "slack_trail": self.cum_pnl - trail_floor - reserved,
            "target_remaining": self.profit_target - self.cum_pnl,
            # Challenge adaptatif — exposé pour core/adaptive_sizing.py
            "observed_trades_per_day": None,  # v1 : fallback dans la formule
            "last_reset_month": self.last_reset_month,
            "last_reset_year": self.last_reset_year,
            "last_monthly_reset_at": (
                self.last_monthly_reset_at.isoformat() if self.last_monthly_reset_at else None
            ),
            "challenge_bypass_user_daily": (
                CHALLENGE_ADAPTIVE_SIZING_ENABLED and CHALLENGE_BYPASS_USER_DAILY_LIMIT
            ),
            # Garde-fou cohérence 50% Topstep — None si hors Combine
            "consistency_cap_max": CHALLENGE_CONSISTENCY_BEST_DAY_MAX_USD,
            "consistency_cap_remaining": consistency_cap_remaining,
            "in_combine": in_combine,
        }
