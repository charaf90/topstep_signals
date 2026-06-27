# Pré-enregistrement — test forward Q3-2026

> **Figé le 2026-06-26.** Document **tracké** (durable). But : geler des hypothèses AVANT que les
> données fraîches existent, pour que le test Q3-2026 soit un **vrai juge hors-échantillon**. C'est
> l'antidote au biais de **sélection-sur-OOS** qui a fait rétrograder 2 candidats cette session
> (fib-fine 0.5/1.0 ET OPR-F1, tous deux cueillis en regardant l'OOS — cf. @auditor).
>
> **Règle de discipline (non négociable)** : interdiction de re-tuner sur les données ≤ 2026-06.
> La fenêtre **Q3-2026 (juil→sep)** ne doit JAMAIS être regardée avant le test. Tout tuning
> supplémentaire sur l'historique actuel = ré-introduction de l'overfit. Test à lancer **début
> octobre 2026**, une fois les barres fraîches importées (`scripts/import_backtest_data.py`).

---

## Candidat A — OPR NQ1 ENTIÈREMENT CAUSAL, edge matinal

**Pourquoi a-priori** (pas issu d'un peek OOS) : l'edge OPR vit dans l'**auction d'ouverture RTH**
(9h30-12h ET) — confirmé edge par heure fort en IS **ET** OOS (9h 1.70/1.41 · 10h 1.40/2.42 ·
11h 1.23/1.78) ; le midi (12h) est instable, l'après-midi s'éteint. Le filtre matinal est donc choisi
sur **base structurelle**, pas pour esquiver des jours perdants.

**Design FIGÉ :**
- Base : `strategies/opr_v5_1` avec **F2 OFF** (`f2_min_atr=None`, `f2_max_atr=None`) → causal, no leak.
- `sl_mult = 0.225` (×ATR ; 4.5× base prod 0.05 — SL large = sizing risk-based cape la queue par trade).
- `r_target = 1.5` (→ `tp_mult = 0.3375`).
- **Filtre horaire STRUCTUREL : entrées uniquement 9h00-11h59 NY** (`hour_lt = 12`). Live = annuler tout
  pending non rempli à 12:00 NY.
- **Daily-safety PAR CONSTRUCTION** (découplée du cutoff) — **RÉVISÉE 2026-06-26** après que le backtest
  M1 a montré que `cap2 × $250` NE tient PAS (blow-through → pire jour −$1,787 > DLL) :
  - `risk/trade = $150` (le blow-through est PAR CONTRAT → réduire le risk le scale linéairement ;
    c'est le levier décisif, pas le cap).
  - **circuit-breaker daily** : stopper OPR-NQ1 pour la journée NY après cumul ≤ **−$500** (le daemon
    suit le cumul OPR-NQ1 du jour). Ceinture+bretelles avec le petit risk.
  - Résultat M1 (validation `brouillon/scripts/opr_nq1_circuit_breaker.py`) : OOS pire jour **−$775**
    (< DLL), trailing **−$1,384** (< $2000), **0% breach daily**, P(target) 96%, PF 1.86, +$10.0k.
  - ⚠️ Marge ~$225 sous la DLL ; la métrique sous-estime la queue → garder ce risk bas (ne pas remonter
    à $250). Cf. [[pf-vs-topstep-utility-metric]].

**Critères de SUCCÈS pré-enregistrés** (jugés sur Q3-2026 FRAIS, SANS aucun re-tuning) :
1. OOS PF ≥ **1.2** ET P&L > 0 ET n ≥ **40** sur la fenêtre fraîche.
2. **0 jour réel** au-delà de la DLL (−$1000) → la daily-safety par construction tient sur du jamais-vu.
3. **Robustesse du cutoff** (anti-razor) : `hour_lt=11` ET `hour_lt=13` doivent AUSSI donner PF ≥ 1.1 sur
   le frais. Si le résultat bascule selon ±1h → bruit → **rejet**.
4. **Robustesse du SL** : `sl_mult=0.18` ET `0.28` restent daily-safe & PF ≥ 1.1 sur le frais.
5. Avant toute idée de live : `tools/backtest_vs_live.py` = **0 divergence** (wiring annul-pending-12h-NY
   + cap 2 fills) + DSR/Bonferroni re-calculés au **budget de tests cumulé réel**.
- **Si échec d'un critère bloquant (1, 2, ou 3)** → OPR NQ1 reste EN PAUSE, edge causal réfuté sur frais.
- **Si succès** → summary + @auditor + @forge + confirmation utilisateur (rien d'automatique).

---

## Candidat B — fib-fine NQ1 0.5/1.0 (DÉJÀ EN LIVE, confirmation a posteriori)

⚠️ Celui-ci est **déjà live** depuis 2026-06-26 (décision utilisateur, override du 🟡 @auditor — pari de
diversification de régime). Le test Q3-2026 est une **confirmation a posteriori**, pas un go/no-go.

**Critère de surveillance pré-enregistré :**
- PF NQ1 **réalisé EN LIVE** ≥ **1.2** sur le trimestre glissant (Q3-2026).
- **Si < 1.2** → bascule de régime probable (type 2023) → **rollback** : `config.py` NQ1 → `0.382/1.5`
  + `scripts/restart_daemon.sh restart`.

---

## Protocole de test (début octobre 2026)
1. Importer les barres fraîches couvrant 2026-07-01 → 2026-09-30 (`scripts/import_backtest_data.py`).
2. **Candidat A** : adapter `brouillon/scripts/opr_nq1_finalists_validate.py` — params FIGÉS ci-dessus,
   fenêtre = Q3-2026 SEULE (jamais vue), évaluer les 5 critères. Aucune optimisation.
3. **Candidat B** : `tools/backtest_vs_live.py` + PnL live réel sur Q3-2026 vs critère.
4. Verdicts → REGISTRE_HYPOTHESES.md + mémoire. Promotion éventuelle = @auditor → @forge → user.

> Source de vérité des leçons : mémoires `pf-vs-topstep-utility-metric`, `nq1-extended-history-2022`,
> `optuna-winner-curse-selection`. Verdicts antérieurs (sur données ≤2026-06) = NON probants par
> sélection-OOS — ce pré-enregistrement est le seul chemin honnête restant.
