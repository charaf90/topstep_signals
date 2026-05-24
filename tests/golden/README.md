# tests/golden/ — Golden master tests

Ces fichiers figent la sortie exacte des backtests `opr`, `opr_v5_1`, et
`fib_v4` sur les données présentes dans `data/`. Ils servent de filet
de sécurité contre les régressions silencieuses.

## Comment ça marche

- `tests/test_golden_master.py` recalcule le baseline et le compare à
  `<strategy>_baseline.json` champ par champ.
- Toute différence (1 trade en plus/moins, 1 cent d'écart sur le PnL,
  date ou direction modifiée) fait échouer le test.

## Quand mettre à jour les baselines

**Uniquement** quand un changement de stratégie est volontaire et
documenté (bump de version, fix de bug, recalibrage walk-forward).

```bash
# 1. Vérifier qu'on est sur la branche dédiée à ce changement
git status

# 2. Régénérer
./tests/golden/regenerate.sh

# 3. Inspecter le diff (sanity check)
git diff tests/golden/

# 4. Commit dédié, séparé des changements de code
git add tests/golden/
git commit -m "test(golden): update baselines after <description du changement>"
```

## Quand NE PAS mettre à jour

- Quand on n'a pas modifié `core/opr*.py`, `core/strategy_fib*.py`,
  `core/fib_helpers.py`, `core/data.py`, `core/backtester.py`, ni les
  CSV de `data/`. Si le golden master casse sans cause connue, c'est
  un bug, pas un changement de baseline.
- Quand on doute. Préférer demander que mettre à jour à l'aveugle.

## Reproductibilité

Versions pandas/numpy pinnées dans `pyproject.toml` (`pandas>=3.0,<3.1`,
`numpy>=2.4,<2.5`). Si tu changes ces contraintes, régénère les
baselines.
