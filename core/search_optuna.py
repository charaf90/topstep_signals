"""
Backend de recherche Optuna (TPE) pour l'optimiseur walk-forward.

Alternative au grid search exhaustif de core/optimizer.py pour les grandes
grilles ou les espaces continus. Réutilise `_evaluate_combo` comme objectif :
le score reste STRICTEMENT in-sample — l'OOS n'est jamais montré au sampler
(même invariant méthodologique que `_default_score`).

Dépendance optionnelle : optuna>=4.0 (groupe pip « research ») — import lazy,
ce module n'est JAMAIS importé par broker/ ni le chemin live.

Contrat optionnel côté module stratégie :
    PARAM_SPACE = {
        "sl_mult":      ("float", 0.3, 2.5),
        "wick_max_atr": ("float", 0.05, 0.8, "log"),
        "n_bars":       ("int", 2, 10),
        "f2_min_atr":   ("cat", [None, 0.10, 0.15]),
    }
Sans PARAM_SPACE, fallback automatique : chaque clé de PARAM_GRID devient une
catégorielle → exploration adaptative (sous-ensemble) de la grille existante.

Déterminisme : TPESampler(seed=...) + trials séquentiels (n_jobs=1). Le TPE est
un sampler séquentiel — le trial k dépend des résultats 1..k-1 ; toute
parallélisation des trials casserait la reproductibilité. La parallélisation
se fait AU NIVEAU des tickers (studies indépendantes), dans core/optimizer.py.
"""

from __future__ import annotations

import time
import warnings

# Sentinel pour None dans les catégorielles : optuna émet un warning sur les
# choix à types mixtes (ex. [None, 0.10, 0.15]) → encodage/décodage explicite.
_NONE_SENTINEL = "__none__"


def _require_optuna():
    try:
        import optuna
    except ImportError as exc:
        raise ImportError(
            "Le backend de recherche 'optuna' nécessite le package optionnel "
            "optuna>=4.0 (groupe « research ») : pip install 'optuna>=4.0'"
        ) from exc
    return optuna


def build_search_space(strategy) -> dict[str, tuple]:
    """Espace de recherche normalisé d'un module stratégie.

    PARAM_SPACE si défini, sinon fallback PARAM_GRID → tout en catégorielles.

    Returns:
        {param: ("float", lo, hi, log: bool) | ("int", lo, hi) | ("cat", [choix])}
    """
    space = getattr(strategy, "PARAM_SPACE", None)
    if not space:
        grid = getattr(strategy, "PARAM_GRID", {})
        if not grid:
            raise ValueError(
                f"[{getattr(strategy, 'STRATEGY_ID', '?')}] ni PARAM_SPACE ni PARAM_GRID défini."
            )
        return {k: ("cat", list(v)) for k, v in grid.items()}

    norm: dict[str, tuple] = {}
    for key, spec in space.items():
        kind = spec[0]
        if kind == "float":
            lo, hi = float(spec[1]), float(spec[2])
            log = len(spec) > 3 and spec[3] == "log"
            norm[key] = ("float", lo, hi, log)
        elif kind == "int":
            norm[key] = ("int", int(spec[1]), int(spec[2]))
        elif kind == "cat":
            norm[key] = ("cat", list(spec[1]))
        else:
            raise ValueError(f"PARAM_SPACE[{key!r}] : type inconnu {kind!r} (float|int|cat)")
    return norm


def _encode(value):
    return _NONE_SENTINEL if value is None else value


def _decode(value):
    return None if value == _NONE_SENTINEL else value


def _suggest_params(trial, space: dict[str, tuple]) -> dict:
    """Tire un jeu de paramètres (ordre des clés stable, sentinel None décodé)."""
    params = {}
    for key, spec in space.items():
        if spec[0] == "float":
            _, lo, hi, log = spec
            params[key] = trial.suggest_float(key, lo, hi, log=log)
        elif spec[0] == "int":
            params[key] = trial.suggest_int(key, spec[1], spec[2])
        else:  # cat
            choices = [_encode(c) for c in spec[1]]
            params[key] = _decode(trial.suggest_categorical(key, choices))
    return params


def run_optuna_search(
    strategy,
    dfs: dict,
    score_fn,
    is_end: str,
    oos_start: str,
    is_start: str,
    oos_end: str | None,
    n_trials: int,
    seed: int = 42,
    label: str = "",
) -> dict:
    """Recherche TPE des paramètres — sortie plug-compatible avec le chemin grid.

    Args:
        dfs : {ticker: (df_15m, tf)} — même forme que dans optimize().
        score_fn : score IS-only (jamais l'OOS), cf. _default_score.

    Returns:
        {
          "results":     liste de {"params", "is", "oos", "score"} — MÊME format
                         que le chemin grid (combos uniques réellement évaluées,
                         trials sans trade exclus, comme le filtrage `r is not None`),
          "trials":      historique pour audit [{number, state, score, **params}],
          "n_evaluated": nb de jeux de params DISTINCTS évalués (compte honnête
                         pour Bonferroni/DSR — les doublons re-suggérés par le
                         TPE ne sont pas des hypothèses supplémentaires),
          "elapsed":     durée (s),
        }
    """
    optuna = _require_optuna()
    from core.optimizer import _evaluate_combo  # import tardif — évite le cycle optimizer↔search

    optuna.logging.set_verbosity(optuna.logging.WARNING)

    space = build_search_space(strategy)
    keys = list(space.keys())
    cache: dict[tuple, dict | None] = {}  # memoization : le TPE re-suggère des doublons

    def objective(trial):
        params = _suggest_params(trial, space)
        combo = tuple(params[k] for k in keys)
        if combo not in cache:
            cache[combo] = _evaluate_combo(
                combo, keys, dfs, strategy, score_fn, is_end, oos_start, is_start, oos_end
            )
        r = cache[combo]
        if r is None:
            raise optuna.TrialPruned()  # aucun trade — ne concourt pas
        return r["score"]

    t0 = time.time()
    with warnings.catch_warnings():
        # multivariate=True : stable en pratique, flaggé "experimental" par optuna
        warnings.filterwarnings("ignore", category=optuna.exceptions.ExperimentalWarning)
        sampler = optuna.samplers.TPESampler(seed=seed, multivariate=True)
    study = optuna.create_study(direction="maximize", sampler=sampler)
    # n_jobs=1 OBLIGATOIRE : trials séquentiels = déterminisme (voir docstring module).
    study.optimize(objective, n_trials=n_trials, n_jobs=1, show_progress_bar=False)
    elapsed = time.time() - t0

    trials = []
    for t in study.trials:
        row = {"number": t.number, "state": t.state.name, "score": t.value}
        row.update({k: _decode(v) for k, v in t.params.items()})
        trials.append(row)

    results = [r for r in cache.values() if r is not None]
    return {
        "results": results,
        "trials": trials,
        "n_evaluated": len(cache),
        "elapsed": elapsed,
    }
