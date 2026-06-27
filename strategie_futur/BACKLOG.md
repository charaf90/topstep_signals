# Backlog des stratégies à explorer

> **Source de vérité** des idées en attente de développement. Mis à jour quand une idée entre/sort de dev.
> Développer une idée → `@athena` (conseil) ou `/new-strategy`, puis MAJ la colonne **Statut**.
>
> ⚠️ **2026-06-17** — Tous les verdicts antérieurs sont **suspects** (moteur maison M15, fill same-bar
> faux) ; voir `REGISTRE_HYPOTHESES.md`. **Priorité courante = re-validation M1** (backtesting.py) des
> stratégies, **prod d'abord**. Le nouveau moteur résout par construction les biais de fill historiques
> (bos-fvg fill-bias, fib-v4 wick look-ahead) → ces anciens items P1 sont **absorbés** par la campagne M1.

---

## Légende

- **Priorité** : P1 (semaines à venir) / P2 (après P1) / P3 (intéressant, pas urgent)
- **Statut** : `Idée` / `À formaliser` / `Fiche prête` / `En dev` / `Re-validation M1` / `Verdict` (à reconstruire)

---

## Campagne de re-validation M1 (priorité courante)

| Priorité | Cible | Univers | Objet | Statut |
|---|---|---|---|---|
| — | opr-v5.1 (YM1) | indice | **✅ M1 ROBUSTE** : OOS PF 2.97, bootstrap 99.8 % ≥ Bonferroni, plateau ✓ | Re-validé M1 |
| — | fib-fine-v2 | NQ1, MES1 | **✅ M1 TIENT** : OOS pool 1.95, BS portefeuille 100 % (robustesse complète en cours) | Re-validé M1 |
| ⚖️ | fib-fine-v2 (NQ1) — ré-opt IS étendu 2022-26 | NQ1 | **ARBITRAGE user**. Deepdive (2026-06-26) : l'arbitrage PROD/REFIT était une fausse alternative — **fib_level 0.5 = sweet spot** (0.5/0.75 +$27.9k, 0.5/1.0 +$24.2k ; positif chaque année ET garde 2026 ~2.4 PF) ; le WF honnête ne choisit jamais le live 0.382/1.5. **Candidat à valider = 0.5/1.0** (robustesse single-OOS + multifold + hold-out + @auditor PAS faits sur 0.5). Statu quo prod = pari de régime actif. **Validé deep-lane (2026-06-26) → @auditor RÉTROGRADE 🟢→🟡** : sélection OOS-informée + 🟢 reposant sur l'artefact bootstrap_pass_rate + DSR optimiste + plus de hold-out frais. **Garder PROD live.** **➡️ MIS EN LIVE 2026-06-26 (décision user, override 🟡)** : NQ1 0.382/1.5→0.500/1.0, restart OK, risque combiné Topstep-safe. Conditions de valid propre restent dues (fenêtre forward Q3-2026 + backtest_vs_live 0-div). MONITOR : PF NQ1 live < 1.2/trimestre ⇒ rollback. Cf. REGISTRE §B + leçon #10. | 🔴 EN LIVE (pari assumé, à valider a posteriori) |
| **P1** | ib-retest-v3 | MES1, NQ1 | ⚠️ Calib live exacte + OOS pool 1.65, MAIS **IS breakeven** (0.89/0.95) → trancher robustesse + régime | Re-validation M1 |
| — | bos-fvg (inerte, disabled) | NQ1, MES1 | ⚠️ **MARGINAL M1** (NQ1 1.16 / MES1 1.13) → reste INERTE, pas de réactivation (PF < VEILLE) | Re-validé M1 |
| P2 | zones-v1 | NQ1, MES1, YM1 | Re-test M1 sur OOS frais (params M15 gelés ; M5/M1 à params figés étaient 🔴) | Idée |
| **P2** | **opr-NQ1-causal re-sizé** | NQ1 | OPR entièrement CAUSAL (F2 OFF, no leak) ressuscité : ex-"fatal" = artefact MC-DD-worst. Forward P(target) re-sizé ×0.30 = 90-94 %, breach daily 0 % (vs sizing actuel pire jour −$3042 = breach DLL). Edge causal sans le pb F2 ! **CANDIDAT TROUVÉ « F1 »** (2026-06-26, Optuna joint + valid étendue) : F2-OFF causal, sl 0.225×ATR + filtre matinal h<13 (9h-12h NY) + cap 2/jour. OOS 2025-26 n=231 PF 1.67 +$10.1k, **P(target) 94%, breach daily 0%, Bonferroni PASS au budget 150** (seul finaliste), per-année stable 1.33-1.74, multifold 4/4. **@auditor = 🔴** (2026-06-26) : F1 CUEILLI SUR L'OOS (objectif IS-only classait h<14 n°1, pas F1) + fragilité rasoir (±1h cutoff → breach daily 0%↔61% = bruit) + aucun hold-out frais. Mécanique causale OK, insight SL-large réel, mais config F1 sur-ajustée. Conditions : pré-enregistrer + forward Q3-2026 frais + robustesse cutoff (h<12 ET h<14 tiennent) + daily-safety par construction + DSR budget réel. **➡️ ACTIVÉ EN LIVE 2026-06-27 (décision user, override 🔴)** : OPR NQ1 causal-matinal (F2 off, sl0.225/r1.5, morning h<12, **risk$150 + circuit-breaker daily −$500** = daily-safe par construction) câblé (`core/opr_nq1_causal.py` + `broker/live_runner.py`, clé `OPR_NQ1` distincte) + `OPR_NQ1_ENABLED=True` + restart (PID 343609). 1ers trades lundi 9-12h ET. Edge NON prouvé (OOS-informé) = pari assumé ; forward Q3-2026 = confirmation a posteriori. Rollback = flag False + restart. MONITOR : 1ers fills + backtest_vs_live + PF live < 1.2/trim → rollback. Cf. PREREGISTRE_2026Q3.md, [[preregistre-2026q3]]. | 🔴 EN LIVE (pari assumé) |

## Idées neuves en attente

| Priorité | ID | Idée | Marché | Hypothèse / Edge | Statut |
|---|---|---|---|---|---|
| P2 | **fib-v4-rebuild** | Retracement Fibonacci continuation — **REDÉVELOPPEMENT FROM SCRATCH sur M1** (fib-v4.1 abandonnée : son edge M15 était un artefact same-bar) | MES1, NQ1 | Repartir de zéro : pipeline gated M1, ne pas réutiliser les params/le code M15. Tester si un retracement Fib + filtre de qualité de fill tient sur M1 honnête. Décision user 2026-06-18. | Idée |
| P3 | pdh-pdl-mes1-v1 | PDH/PDL breaker MES1 seul + macro filter | MES1 | Verdict 🟡 M15 (suspect) ; re-tester M1, n_oos plus grand attendu | Idée |
| ✅ | **métrique-topstep-utility** (INFRA) | Le PF est un GATE, pas un classement Topstep | — | **FAIT (2026-06-26)** : `core/robustness.py` → `topstep_forward_mc` + `topstep_utility` câblés dans `run_full_robustness` → bloc « Utilité Topstep » (P(target) + freq + expectancy/jour) dans tout `robustness_<id>.{json,md}`. Verdict PF INCHANGÉ (informatif). `portfolio_replay.challenge_outcome_mc` délègue (source unique). Tests verts. Reste optionnel (décision séparée) : faire de P(target) un GATE dans `core/metrics.verdict`. | Implémenté |

> **Direction prometteuse pour les idées neuves** (archétype gagnant du système, cf. REGISTRE leçon #1) :
> **cassure franche + retest LIMITE** sur un niveau structurel fort, et/ou un **filtre data-driven de
> qualité de fill** (`@quant`). Éviter les entrées market et les setups nus sans filtre.

_(Idées rejetées au mécanisme — fade-de-sweep, mean-reversion indices, gate de régime, open-drive,
géométrie Fib nue, break-retest hors 1ʳᵉ heure — = red-flags à bloquer à l'ÉTAPE 0 : voir REGISTRE §A.)_
