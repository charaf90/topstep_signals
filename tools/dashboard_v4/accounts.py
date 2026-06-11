"""Registre des comptes — architecture multi-compte, UI mono-compte.

Chaque compte (challenge actuel, futur funded, 2e challenge en parallèle…)
est décrit par un ``AccountConfig`` : chemins state/logs, préfixe des
credentials .env, limites Topstep propres au compte.

Pour ajouter un compte : ajouter une entrée dans ``ACCOUNTS`` (et les
variables ``<PREFIX>_USERNAME`` / ``<PREFIX>_API_KEY`` dans .env si le
compte vit chez un autre login ProjectX). Le sélecteur de compte du
dashboard apparaît automatiquement dès que ``len(ACCOUNTS) > 1``.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402


@dataclass(frozen=True)
class AccountConfig:
    key: str  # identifiant interne ("ts50k_1")
    label: str  # affichage ("Topstep 50K #1")
    mode: str  # "challenge" | "funded"
    state_path: Path
    events_log: Path
    daemon_pid: Path
    daemon_log_glob: str  # relatif à ROOT
    env_prefix: str = "PROJECTX"  # → <PREFIX>_USERNAME / <PREFIX>_API_KEY
    broker_account_id: int | None = None  # None = 1er compte actif de l'API
    account_size: float = float(config.TOPSTEP_ACCOUNT_SIZE)
    profit_target: float = float(config.TOPSTEP_PROFIT_TARGET)
    daily_loss_max: float = float(config.TOPSTEP_DAILY_LOSS_MAX)
    trailing_dd: float = float(config.TOPSTEP_TRAILING_DD)
    user_daily_loss_max: float = float(config.USER_DAILY_LOSS_MAX)
    consistency_guardrail: float = float(config.CHALLENGE_CONSISTENCY_BEST_DAY_MAX_USD)
    extra: dict = field(default_factory=dict)


ACCOUNTS: dict[str, AccountConfig] = {
    "ts50k_1": AccountConfig(
        key="ts50k_1",
        label="Topstep 50K",
        mode="challenge",
        state_path=ROOT / "state" / "live_state.json",
        events_log=ROOT / "logs" / "trading_events.log",
        daemon_pid=ROOT / "state" / "live_daemon.pid",
        daemon_log_glob="logs/daemon_*.log",
    ),
}

DEFAULT_ACCOUNT = "ts50k_1"


def get_account(key: str | None) -> AccountConfig:
    return ACCOUNTS.get(key or DEFAULT_ACCOUNT, ACCOUNTS[DEFAULT_ACCOUNT])
