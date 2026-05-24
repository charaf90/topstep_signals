# strategies/archive — Stratégies abandonnées

Les fichiers de ce dossier sont conservés pour traçabilité historique mais ne sont **plus exécutés**. Le registry (`core/registry.py`) ignore automatiquement ce sous-package via `pkgutil.iter_modules` (filtre `is_pkg=True`).

Pour le détail des décisions (motivations, métriques, leçons), voir [`docs/strategies_abandoned.md`](../../docs/strategies_abandoned.md) à la racine du repo.

## Inventaire

| Fichier | Statut | Motif d'abandon (résumé) |
|---|---|---|
| `opr_h4.py` | rejeté | Variant OPR sur H4 — performances OOS insuffisantes. |
| `kijun_pb.py` | rejeté | Pullback Kijun (Ichimoku) — bootstrap OOS sous seuil. |
| `smc_v1.py` | rejeté 2026-05-17 | SMC LuxAlgo multi-zones — PF OOS 0.95. |

## Note sur `strategies/opr_v5.py` (conservé hors archive)

Le module `opr_v5.py` n'est PAS archivé bien que la stratégie soit superseded par `opr_v5_1`. Raison : `strategies/opr_v5_1.py` (en prod) importe `_compute_features_for_signal` depuis `opr_v5` comme bibliothèque de helpers. Tant que ces helpers n'auront pas été inlinés dans `opr_v5_1.py` (ou déplacés dans `core/`), `opr_v5.py` doit rester accessible à l'import standard.

## Ne pas relancer ces stratégies sans nouvelle approche causale

Memory `feedback_strategies_abandoned_lessons` documente les pièges spécifiques de chaque cas (look-ahead intra-bar, sur-fitting, surface de paramètres trop large, etc.). Une simple reparamétrisation ne suffira pas.
