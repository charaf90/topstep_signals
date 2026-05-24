"""Shadow runner — exécute la logique du live sans poster d'ordres réels.

PHASE 3.2 ROADMAP_SOLO. Permet de tester en parallèle du daemon live :
  - une variation de logique (ex: vol-targeting on, nouvelle stratégie)
  - sans toucher au compte Topstep ni au state du live

Architecture : réutilise exactement `broker.live_runner.SessionRunner` mais
forcé en `dry_run=True`, avec un `state_file` et un `log_file` séparés.
Aucun code dupliqué — le shadow exécute la même logique de décision que
le live, mais ne place jamais d'ordres réels (dry_run gate dans
`_place_order_with_retry` et `cancel_order`).

Pour le lancer :
    python shadow.py --daemon
    # ou via le wrapper tmux
    ./tools/launch_shadow.sh start

Pour comparer ex-post avec le live :
    python tools/shadow_vs_live.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

# Importer config en premier (utilisé par les autres modules)
from config import (
    SHADOW_LOG_FILE,
    SHADOW_STATE_FILE,
)

_log = logging.getLogger("shadow_runner")


def build_shadow_runner(
    client: Any,
    account_id: int,
    state_file: str | None = None,
    tickers: list[str] | None = None,
    strategy: str = "opr_fib_vpc",
    telegram: Any = None,
) -> Any:
    """Construit un SessionRunner forcé en mode shadow.

    INVARIANT : dry_run=True forcé en dur (pas d'override possible).
    Tout chemin d'écriture broker (place_limit_order, cancel_order, etc.)
    est bypassé via la garde `if not self.dry_run` dans le SessionRunner.

    Args:
        client      : ProjectXClient déjà authentifié
        account_id  : id du compte (lecture WS et bars, jamais d'écriture)
        state_file  : override (défaut : SHADOW_STATE_FILE)
        tickers     : sous-set à observer (défaut : auto-détecté)
        strategy    : stratégie à exécuter (mêmes valeurs que live.py)
        telegram    : TelegramBot optionnel (souvent désactivé en shadow)

    Returns:
        SessionRunner configuré en mode shadow.
    """
    # Import différé pour éviter une dépendance circulaire à l'import
    from broker.live_runner import SessionRunner  # noqa: PLC0415

    state_path = state_file or SHADOW_STATE_FILE
    runner = SessionRunner(
        client=client,
        account_id=account_id,
        state_file=state_path,
        dry_run=True,  # FORCÉ EN DUR — INVARIANT SHADOW
        tickers=tickers,
        strategy=strategy,
        live_mode=False,
        telegram=telegram,
    )
    # Sanity check : on refuse de retourner un runner si dry_run a été modifié
    if runner.dry_run is not True:
        raise RuntimeError(
            "Shadow runner construit avec dry_run != True. "
            "C'est une violation d'invariant — refus de continuer."
        )
    _log.info(
        "Shadow runner construit : state=%s tickers=%s strategy=%s",
        runner.state_file,
        runner.tickers,
        runner.strategy,
    )
    return runner


def configure_shadow_logging(log_level: str = "INFO", log_file: str | None = None) -> None:
    """Configure un logging dédié shadow — fichier séparé du live.

    Le formatter inclut le prefix [SHADOW] pour faciliter le grep dans les
    logs combinés.
    """
    target = log_file or SHADOW_LOG_FILE
    Path(target).parent.mkdir(parents=True, exist_ok=True)

    handlers: list[logging.Handler] = [
        logging.FileHandler(target, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
    formatter = logging.Formatter(
        fmt="%(asctime)s  %(levelname)-8s  [SHADOW]  %(name)s : %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    for h in handlers:
        h.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(getattr(logging, log_level))
    # Remove any previously installed handlers (idempotent)
    root.handlers.clear()
    for h in handlers:
        root.addHandler(h)
