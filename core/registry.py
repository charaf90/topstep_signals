"""
Découverte automatique des stratégies — plug-and-play.

Toute stratégie placée dans `strategies/<nom>.py` est automatiquement
disponible via `python backtest.py --strategy <nom>` et `python optimize.py
--strategy <nom>`, sans modification manuelle d'un REGISTRY.

Contrat minimal d'un module stratégie pour être détecté :
    - exposer `STRATEGY_ID` (str)
    - exposer `TICKERS` (list[str])
    - exposer `run_backtest(df_15m, ticker, tf=None, params=None,
                            topstep_guard=True) -> pd.DataFrame`

Optionnel :
    - `PARAM_GRID` (dict[str, list]) — requis pour l'optimisation
    - `plot_day(...)` — requis pour `--plot`
    - `CSV_SUFFIX`, `CSV_TIMEFRAME`

Modules ignorés : ceux dont le nom commence par `_` ou qui n'exposent pas
le contrat minimal.
"""

from __future__ import annotations

import importlib
import pkgutil

_STRATEGIES_PACKAGE = "strategies"


def _has_min_contract(module) -> bool:
    return (
        hasattr(module, "STRATEGY_ID")
        and hasattr(module, "TICKERS")
        and callable(getattr(module, "run_backtest", None))
    )


def discover_strategies() -> dict[str, str]:
    """
    Scanne le package `strategies/` et retourne {nom_court: chemin_module}.

    Le nom_court est le nom de fichier sans extension (ex: "opr"), c'est
    ce qu'on attend en CLI `--strategy opr`.

    Chargement tolérant aux erreurs : un module qui plante au chargement
    est ignoré avec un avertissement (pas de crash global).
    """
    out: dict[str, str] = {}
    try:
        pkg = importlib.import_module(_STRATEGIES_PACKAGE)
    except ImportError:
        return out

    for finder, name, is_pkg in pkgutil.iter_modules(pkg.__path__):
        if name.startswith("_") or is_pkg:
            continue
        full = f"{_STRATEGIES_PACKAGE}.{name}"
        try:
            mod = importlib.import_module(full)
        except Exception as exc:
            print(f"  [registry] {full} ignoré ({type(exc).__name__}: {exc})")
            continue
        if _has_min_contract(mod):
            out[name] = full
    return out


def list_strategy_names() -> list[str]:
    """Retourne la liste triée des noms de stratégie disponibles."""
    return sorted(discover_strategies().keys())


def load_strategy(name: str):
    """
    Importe le module stratégie associé au nom court donné.
    Lève ImportError si le nom est inconnu.
    """
    reg = discover_strategies()
    if name not in reg:
        avail = ", ".join(reg) or "(aucune)"
        raise ImportError(f"Stratégie inconnue : '{name}'. Disponibles : {avail}")
    return importlib.import_module(reg[name])
