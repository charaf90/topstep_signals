"""
Isolation globale des tests — AUCUNE écriture dans les artefacts de prod.

Constat 2026-06-10 : chaque run pytest écrivait les événements de ses fixtures
(FILL/CLÔTURE factices, phantom_heal, WS démarrage) dans le VRAI
logs/trading_events.log — tags fantaisistes (`OPR_NQ1_20260518_long_1` @17000,
`FIBFINE_MES1_x_short_a`) interprétés ensuite comme des anomalies live.

Mécanisme : EventLogger résout son chemin via la variable d'environnement
TRADING_EVENTS_LOG (cf. core/event_logger.py). Ce conftest la pose AVANT tout
import de module de test (les `_evlog = EventLogger()` au niveau module de
broker/live_runner.py sont créés après la collecte de conftest).

Telegram est déjà isolé par les tests eux-mêmes (MagicMock).
"""

import os
import tempfile

_EVLOG_DIR = tempfile.mkdtemp(prefix="evlog_pytest_")
# setdefault : un développeur peut surcharger explicitement s'il veut inspecter
# le journal produit par un test (export TRADING_EVENTS_LOG=/tmp/mon.log).
os.environ.setdefault("TRADING_EVENTS_LOG", os.path.join(_EVLOG_DIR, "trading_events.log"))
