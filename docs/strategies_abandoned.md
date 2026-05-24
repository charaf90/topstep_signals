# Stratégies abandonnées — Historique des tentatives

Document de référence détaillée — **consultation manuelle uniquement** (pas chargé par défaut, économie tokens).

Pour les leçons compactes : voir `.claude/projects/.../memory/feedback_strategies_abandoned_lessons.md`.

---

## fib-v3 (Fibonacci v3) — Supprimée 2026-05-19

### Concept

Retracement Fibonacci 0.382 post-impulsion. Détection d'une impulsion (pivot left/right 8 barres, taille ≥ 1.5 ATR), placement d'un ordre limite au niveau 38.2% du retracement. SL/TP en multiplicateurs ATR per-ticker. Filtre trigger calibré walk-forward fib-v2 (bars_since_confirm pour MES1/YM1, adx_at_arm pour NQ1).

### Période de test

Sept 2024 → mai 2026 (IS+OOS).

### Résultats backtest

- PF OOS 1.56 (modeste)
- n_oos 113-235 selon période
- DD P95 -$840, P99 -$1002

### Raison de l'abandon

1. **Trades illogiques observés** : ordre limite restait actif même quand le prix avait cassé le pivot d'impulse, alors que la thèse de rebond était structurellement morte.
2. PF OOS insuffisant pour conserver le créneau face à fib-v4 (PF OOS 4.97 portfolio).
3. Pas d'invalidation pendant pending → trades subis plutôt que choisis.

### Lessons learned

- Toute stratégie de retracement DOIT inclure une invalidation explicite si la zone source est cassée pendant le pending.
- Le filtre data-driven (wick excess) découvert en fib-v4 (p<0.001) montre qu'un edge dominant peut exister sur la qualité du fill lui-même — à creuser systématiquement avec @quant sur tout setup limite.

### Remplacée par

`fib-v4` (cf. `output/rapport_fib-v4.md`). Helpers Fib partagés : `core/fib_helpers.py`.

### Archives

`output/archive/fib_v3/` : `backtest_*_fib.csv` (3), `robustness_fib-v3.{md,json}`, `compare_fib_v3_v4.md`.

---

## VPC (Volume Profile Confluence — vpc-v4) — Supprimée 2026-05-19

### Concept

Approximation du Volume Profile journalier (POC, VAH, VAL, HVN, LVN) à partir des barres M15 de la session cash NY veille (9h30-16h NY). 3 setups hiérarchiques :
1. **OPEN_OUTSIDE** — open hors du Value Area veille → entrée market dans le sens du gap, SL au bord du VA opposé, TP = 2×SL.
2. **BREAKOUT_RETEST** — cassure de VAH/VAL avec retest sur volume confirmé.
3. **HVN_REBOUND** — rebond sur HVN intermédiaire dans le sens de la tendance.

### Période de test

Mar 2025 → mar 2026 (8 mois OOS).

### Résultats backtest

- PF OOS < 1.0 sur portefeuille
- Bootstrap stationnaire faible
- Stratégie active uniquement sur MES1 (NQ1 retiré car risque par contrat > USER_DAILY_LOSS_MAX $200)

### Raison de l'abandon

1. 3 setups empilés sans signal régime → comportement opportuniste sans cohérence.
2. Les conditions POC/VAH/VAL/HVN/LVN reposent sur une approximation du volume profile depuis M15 (pas le vrai tick), réduisant la précision.
3. Le gap fade fonctionnait certains régimes (ranging) mais perdait en trending.

### Lessons learned

- Avant de superposer plusieurs setups dans une stratégie, valider chacun séparément avec PF ≥ 1.5 OOS.
- Le Volume Profile depuis M15 reconstruit est trop approximatif pour des décisions broker — il faudrait un vrai tick stream.

### Si ré-essai un jour

Un pré-filtre régime causal (ADX vs ATR, ou détection trending/ranging robuste) avant tout setup. Sinon abandon.

---

## ARF (Asian Range Failure — arf-v4) — Supprimée 2026-05-19

### Concept

Pendant la session asiatique (19h-02h NY), un range se forme. Pendant Londres (02h-05h NY), on attend une fausse cassure du Asia_High/Low (cassure puis retour dans le range) → ordre limite contre-tendance au niveau extrême. SL au-delà du range + buffer ATR. TP en multiple R:R.

### Période de test

Mai 2026 (tests structurels avant promotion).

### Résultats backtest

- PF baseline insuffisant
- Sur MGC1 : 1 seul signal en 49 jours (illiquidité asiatique sur Gold)
- Sur MCL1 : 4 signaux en 28 jours, PF=0.55 — les cassures sont des **continuations**, pas des retournements (cf. note historique config.py:776)

### Raison de l'abandon

1. Inadaptée structurellement aux matières premières (Gold, Crude) — leur dynamique asiatique est de la continuation, pas du failure.
2. Sur indices US, les volumes asiatiques sont trop faibles pour générer un range statistiquement exploitable.
3. `ARF_ENABLED = False` n'a jamais été flippé à True faute de validation OOS.

### Lessons learned

- Avant de présumer qu'un setup de "fausse cassure" marche, vérifier que le marché cible présente bien la dynamique mean-reversion. Crude et Gold sont des marchés news-driven = continuation à la cassure.

### Si ré-essai un jour

Restreindre aux indices US (MES1/NQ1/YM1). Ajouter filtre macro (skip jours FOMC/CPI/NFP). Et tester en mode pure dry-run 6 mois minimum avant promotion.

---

## SMC v1 (Smart Money Concepts LuxAlgo) — Rejetée 2026-05-17

### Concept

Détection multi-zones (Order Block, Fair Value Gap, Breaker) sur M15 selon protocole LuxAlgo. Entrée à la mitigation de la zone proche de la liquidité (HH/LL).

### Période de test

Mar 2025 → mai 2026 (tests exhaustifs).

### Résultats backtest

- PF OOS 0.95 portefeuille (sous le seuil 1.0)
- Multi-actifs MES/NQ/YM testés
- Multi-zones testées séparément + combinées

### Raison de l'abandon

Pas d'edge statistique robuste. La logique multi-zones empilées dilue le signal au lieu de le concentrer.

### Référence

Mémoire dédiée : [[smc-v1-rejected-2026-05-17]].

### Archives

`output/archive/smc_v1/` (incluant ancien dossier `output/smc-v1/` migré).

---

## opr_h4-v1 — Recherche non promue

### Concept

Variante OPR sur timeframe H4 au lieu de M15. Tester si la cassure de la première barre H4 (9h-13h NY) génère un signal exploitable.

### Résultats

- Edge non significatif vs OPR M15 (opr-v4)
- Nombre de trades trop faible (1 par jour max)
- Variance dominante

### Archives

`output/archive/opr_h4/` : `backtest_*_opr_h4.csv` (3), `robustness_opr_h4-v1.{md,json}`, `rapport_opr_h4.md`.

---

## kijun_pb-v1 — Recherche non promue

### Concept

Pullback sur la ligne Kijun (Ichimoku, période 26). Détection de pullback profond + bougie de rejet sur Kijun, entrée dans le sens de la tendance.

### Résultats

Pas de signal robuste sur indices micro. Setup trop générique sans filtre spécifique.

### Archives

`output/archive/kijun_pb/` : `backtest_NQ1_kijun_pb.csv`, `robustness_kijun_pb-v1.{md,json}`, `rapport_kijun_pb.md`.

---

## Pivot detector ML — Recherche clôturée 2026-05-18 (résultat préservé)

⚠️ **Pas une stratégie abandonnée** — étude clôturée avec résultat positif, en attente d'intégration future en stratégie de trading.

### Concept

Détecteur de pivots prédictifs via Machine Learning (features ATR, ADX, volume, divergence, retracement) sur Gold (MGC1).

### Résultats

MGC1 ×8.8 lift robuste sur les pivots ML vs baseline aléatoire. Documentation complète : `strategie_futur/pivot_detector_ml.md`.

### Statut

En attente d'intégration en stratégie de trading (sera potentiellement la base d'un futur "Gold pivot ML strategy"). NE PAS supprimer.

### Archives techniques

`output/archive/pivot_research/` (528 MB de pivot_research_combo, _diag, _div, _div_v2, _h1, _wf).

---

## Conclusion

Les stratégies abandonnées ci-dessus n'apparaissent plus dans le code production. Leurs artefacts sont archivés dans `output/archive/<strategy>/` et leurs scripts one-shot dans `scripts/archive/`.

**Pour proposer une nouvelle stratégie qui ressemble** à l'une de celles-ci : citer explicitement les apprentissages avant de relancer le pipeline `/new-strategy` (cf. `.claude/projects/.../memory/feedback_strategies_abandoned_lessons.md`).
