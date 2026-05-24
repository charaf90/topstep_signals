# Archives `scripts/`

Scripts one-shot : pipelines de recherche clôturée, validations post-déploiement, études ponctuelles, ou stratégies abandonnées. Conservés pour reproductibilité historique — ne pas exécuter sans contexte.

## `research_pivot/` — Détecteur de pivots ML

Recherche clôturée 2026-05-18 (cf. `strategie_futur/pivot_detector_ml.md`). Résultats consolidés dans `output/archive/pivot_research/`.

- `research_pivot_combo.py` — combinaisons multi-features
- `research_pivot_diagnostic.py` — diagnostic baseline
- `research_pivot_divergence.py` / `_v2.py` — détection divergences momentum
- `research_pivot_h1.py` — variante timeframe H1
- `research_pivot_nq1.py` — focus NQ1
- `research_pivot_validate.py` — validation OOS
- `research_pivot_wf.py` — walk-forward
- `run_pivot_grid.sh`, `run_pivot_h1_grid.sh`, `run_pivot_wf_grid.sh` — orchestration des grids

## `research_fib_v4/` — Pipeline développement fib-v4

Scripts exécutés lors du développement 2026-05-19. fib-v4 promue en prod, résultats dans `output/rapport_fib-v4.md`.

- `baseline_fib_v4.py` — Phase 1 : 30 cellules baseline
- `explore_fib_v4_features.py` — Phase 3a : analyse par déciles
- `optimize_fib_v4.py` — Phase 4 : walk-forward IS/OOS
- `data_science_fib_v4.py` — pipeline RF + permutation features
- `stress_fib_v4.py` — stress tests par régime / Monte Carlo

## `research_opr_v5/` — Pipeline développement opr-v5 / v5.1

Scripts exécutés lors des développements OPR v5 puis v5.1 (promu 2026-05-18 sur NQ1+YM1).

- `explore_opr_v5_features.py` — analyse features F1/F2/F3
- `stress_opr_v5.py` / `stress_opr_v5_1.py` — stress tests par régime
- `optimize_v5_1_no_mes1.py` — optim ad-hoc excluant MES1 (MES1 reste sur v4 pass-through)
- `live_eq_v5_1_m5.py` — variante M5 du live-equivalence (M15 conservé en prod)
- `live_eq_compare_m15_m5.py` — comparaison M15 vs M5
- `validate_m1_buffer_vs_rest.py` — validation Phase C M1Buffer après promotion

## `smoke_tests/` — Validations infra post-déploiement Phases B/C

One-shots de vérification SignalR User Hub / Market Hub après l'intégration realtime.

- `place_test_order.py` — LIMIT non-fillable + cancel → valide events GatewayUserOrder
- `place_test_order_fill.py` — LIMIT marketable hold 3s → valide GatewayUserPosition + Trade
- `realtime_smoke.py` — smoke User Hub
- `realtime_smoke_market.py` — smoke Market Hub (streaming quotes/bars)

## `risk_studies/` — Études risque ad-hoc

Études exécutées en lien avec la définition de la policy risque (cf. memory `project_risk_policy_2026-05-21`, `project_challenge_mode`, `project_risk_consistency_50_2026-05-20`).

- `backtest_losses_distribution.py` — distribution des pertes consécutives
- `backtest_period_stats.py` — stats par période (mois, régime)
- `backtest_risk_comparison.py` — comparaison politiques de risque
- `backtest_sl_streaks.py` — analyse des streaks de SL

## `comparisons/` — Comparaisons one-shot

- `compare_fib_v3_v4.py` — comparaison v3 vs v4 (fib-v3 supprimée, script désactivé)
- `compare_v4_v5.py` — comparaison opr-v4 vs opr-v5 (validation pré-promotion v5.1)
- `compare_v4_v5_v5_1.py` — extension comparant les 3 versions OPR

## `simulations/` — Simulations one-shot

- `simulate_challenge_mode.py` — validation du mode challenge adaptatif (2026-05-18)
- `simulate_option_a_v5_1.py` — simulation entrée différée schéma A pour OPR v5.1

---

## Scripts maintenus à la racine `scripts/` (4)

Ces scripts sont des **templates / utilitaires actifs** explicitement référencés par CLAUDE.md ou par les agents (forge, auditor, quant, skill new-strategy) :

| Script | Référence | Rôle |
|---|---|---|
| `import_backtest_data.py` | `CLAUDE.md` | Pipeline d'import `DATA_BACKTEST/` → `data/` |
| `live_eq_v5_1.py` | `auditor.md`, `SKILL.md` | Template live-equivalence à dupliquer pour toute nouvelle strat |
| `data_science_opr_v5.py` | `quant.md` | Modèle de pipeline data science (RF, permutation, decision tree) |
| `generate_robustness_fib_v4.py` | `forge.md` | Template de génération `output/robustness_<id>.{md,json}` |
