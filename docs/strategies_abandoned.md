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

## Pivot detector ML — Recherche clôturée 2026-05-18, tentative de stratégie rejetée 2026-05-26

### Concept

Détecteur de pivots prédictifs via Machine Learning (features ATR, ADX, volume, microstructure) sur Gold (MGC1). Modèle RandomForest entraîné à classifier les barres-pivots (label `argrelextrema(order=20)`).

### Étude ML amont (2026-05-18) — Edge de classification confirmé

MGC1 ×8.8 lift robuste sur 4 splits walk-forward (précision 28.39% ± 0.54%, vs base rate 3.23%). Filtre validé : `proba_RF ≥ p10% AND vol_rel ≥ 2.5 AND range_atr_ratio ≥ 1.5 AND hour_ny ≥ 6`. Documentation complète : `strategie_futur/pivot_detector_ml.md`.

### Tentative pivot-rev-v1 (2026-05-26) — Rejetée structurellement

Stratégie de retournement Gold M5 construite sur le détecteur ML :
- Entrée : confirmation J+1 par cassure du high/low de la bougie candidate (3 barres M5 = 15 min)
- Exit : triple barrier (SL=max(1×ATR, wick), TP=RR×SL, time=100 min)
- Validation : holdout pur IS=2025-10→2026-01, OOS=2026-02→2026-05

**Verdict** : 🔴 PF IS 0.97 sur best combo (grid 3³=27), arrêt précoce avant OOS (méthodologie anti p-hacking). Audit @auditor confirme : code clean, pas de bug, rejet structurel.

### Raison de l'abandon

**3 défauts structurels** documentés par audit :

1. **Trigger break-out trop strict** : 70% des signaux ML expirent en 15 min sans entrée (110 NOT_FILLED / 156 candidats).
2. **Mix TP/SL/TE défavorable** : 2 TP / 17 SL / 8 TE sur 27 fills → taux TP réel 7% (vs 28% attendu de la précision ML).
3. **Lift ML ≠ Edge trading** : la classification ×8.8 ne se traduit pas en exécution rentable — la traduction signal → execution coûte tout l'edge.

### Lessons learned

- **Edge de classification ML ≠ edge de trading**. Une précision OOS ML stable (×8.8 lift) ne garantit pas la rentabilité après traduction en règles d'entrée/sortie + frictions. Toujours backtester PnL avant de conclure qu'un signal ML est exploitable.
- **Confirmation par cassure introduit un biais de sélection adverse** sur les reversals : on n'entre que sur les signaux qui ont déjà bougé dans la direction prédite, perdant l'avantage de timing au pivot.
- **5 leviers identifiés par l'audit pour une V2 hypothétique** (non retenue) :
  (a) Trigger plus tolérant (entrée immédiate close ou CONFIRM_BARS=5-8)
  (b) RR adaptatif (TP=1.5×ATR ou trail BE+0.5R dès MFE=1R)
  (c) Inversion directionnelle propre (sans court-circuit `is_red → long` actuel)
  (d) Recalibrer label oracle (order=10 au lieu de 20)
  (e) Corriger leakage label frontière (split label_pivots IS/OOS séparément)

### Statut

**Concept tradable abandonné définitivement** (décision utilisateur 2026-05-26). Ne pas re-tenter sans nouvelle approche causale (orderflow, tick data, market profile en input du modèle).

### Archives techniques

- Étude ML : `output/archive/pivot_research/` (528 MB)
- Tentative v1 : `output/pivot-rev-v1/` (concept.md, summary.json, rapport.md, full/trades_v1.csv)
- Code v1 conservé pour référence : `strategies/pivot_rev.py`, section `PIVOT_REV_*` dans `config.py`, `scripts/holdout_pivot_rev.py`

---

## eia-inv-v1 (EIA Inventory Retest MCL1) — Rejetée 2026-05-26

### Concept

Stratégie event-driven mean-reversion sur MCL1 (Micro Crude Oil) exploitant la sur-réaction algos HFT au release du Weekly Petroleum Status Report (EIA, mercredi 10:30 NY). Entrée LIMIT contrarian à l'extremum du spike post-release, TP = retour à l'ancre `close(10:15 NY)`, SL = 1.5× spike, time barrier 60 min.

### Période de test

Sept 2024 → mai 2026 (IS sept 2024 → 2025-09-30, OOS 2025-10-01 → mai 2026).

### Résultats backtest (post-correction biais)

- **IS PF 0.98** (n=28 fills sur 39 signaux, NF 28%, P&L -$21)
- **OOS PF 2.16** (n=18 fills sur 22 signaux, NF 18%, P&L +$530)
- Inversion IS→OOS sur petit échantillon = bruit favorable, pas edge persistant
- Direction : LONG PF 1.17 / SHORT PF 1.44 (sample <25 chacun, non significatif)

### Raison de l'abandon

1. **Critère d'arrêt précoce projet déclenché** : IS PF 0.98 < seuil 1.0.
2. **Fréquence event-driven hebdo incompatible avec pipeline statistique projet** : 61 signaux sur 20 mois → ~3 trades/mois → atteindre n=100 fills statistiquement robuste nécessiterait ~5 ans de live (jamais en backtest sur fenêtre disponible).
3. **2e rejet consécutif** (après pivot-rev-v1) → décision Athena de stopper pour éviter biais d'engagement (sunk cost).

### Lessons learned (valeur réelle de l'exercice)

- **Biais M15 fill intra-bar EIA** : premier run autorisait fill DANS la barre EIA elle-même (10:30-10:45 NY) → PF artificiel 31, WR 95%. Corrigé par contrainte `i_fill ≥ i_eia + 1` (entrée seulement après fermeture barre EIA). **Pattern transversal à tous events scheduled** : sur tout backtest M15 avec event intra-bar, le fill doit être autorisé strictement APRÈS la fermeture de la barre événement.
- **Stratégies event-driven low-freq** (1 event/semaine) ne sont pas compatibles avec le pipeline statistique projet (seuil n_oos ≥ 50). Réserver aux acteurs avec horizon multi-années ou data alternative (intraday tick-by-tick autour du release).
- **Approche méthodologique solide** : @researcher avait anticipé le biais M15, @new-strategy l'a détecté et corrigé proprement avant de finaliser. L'intégrité méthodologique a été préservée — c'est la valeur de l'exercice.

### Ne pas re-tester sans

- Données tick-by-tick autour du release EIA (vraie exécution sans biais M15)
- OU horizon de données multi-années (5+ ans) pour atteindre n=100 fills
- OU acceptation explicite que le verdict 🟢 projet ne sera jamais atteignable et passage en shadow mode dérogatoire (audit code obligatoire + kill-switch 5 SL consécutifs)

### Archives

`output/archive/eia-inv-v1/` : concept.md, quant_catalog.md, summary.json, rapport.md, full/trades_v1.csv (61 trades, 22 cols), README.md.

Code conservé : `strategies/eia_inv.py` (510 lignes), `config.py` section `EIA_INV_*` (lignes 1057-1119).

---

## gap-fill-v1 (Gap Fill Open NY) — Rejetée 2026-05-26

### Concept

Stratégie mean-reversion sur le gap d'ouverture NY : si le cours ouvre avec un gap ≥ `GAP_MIN_PCT` vs la clôture RTH veille (16h00 NY), entrée dans le sens opposé au gap (fade) pour cibler un retour vers le Prior Session Close. Détection sur 1ère barre M15 NY (09:30), entrée LIMIT sur bougie suivante, SL = ATR-based, TP = distance résiduelle au PSC.

### Marché cible

MES1, NQ1, YM1.

### Période de test

Sept 2024 → mai 2026 (IS sept 2024 → 2025-09-30, OOS 2025-10-01 → mai 2026).

### Résultats backtest

- **IS PF 0.54** — meilleur combo IS : +$37 total.
- Pas de combo positif NQ1 / YM1.
- Arrêt précoce (IS PF < 1.0) → OOS non évalué.

### Raison de l'abandon

**Défaut mécanique structurel non corrigeable par tuning :**

Après confirmation sur la 1ère barre M15, la distance résiduelle au PSC (TP effectif) est trop petite vs le SL ATR-based. RR médian ≈ 0.5 systématiquement. Le problème vient de l'entrée post-confirmation : le gap est déjà partiellement comblé sur la 1ère barre, laissant trop peu de chemin vers le TP.

### Lessons learned

- **L'entrée post-confirmation gap est une erreur d'architecture** : on entre après que le gap s'est déjà partiellement comblé sur la 1ère barre, donc le TP résiduel est petit. Il faut soit entrer AVANT confirmation (entrée pre-marché LIMIT au open), soit redéfinir le TP (ex: 50% du gap au lieu du PSC complet).
- **Gap fill ≠ PSC reversion** : le gap se comble souvent partiellement, pas nécessairement jusqu'au PSC — un TP à 50% du gap serait plus réaliste.

### V2 hypothétique (non retenue)

Entrée LIMIT dès le pre-market + TP à 50% gap. Structurellement différent — à traiter comme une nouvelle stratégie avec @researcher.

### Archives

`output/gap-fill-v1/` : summary.json, rapport.md, full/trades_v1.csv, full/robustness.json.

Code conservé : `strategies/gap_fill.py` (avec sections `GAP_*` dans `config.py`).

---

## pdh-pdl-retest-v1 (PDH/PDL Retest après cassure — portfolio) — Rejeté 2026-05-27

### Concept

Stratégie ICT Breaker Block : après cassure du PDH (Previous Day High) ou PDL (Previous Day Low) avec une amplitude ≥ `MIN_BREAK_ATR`, attendre le retest du niveau cassé (now S/R inversé) pour entrer dans le sens de la cassure. Entrée LIMIT dans la zone de retest [PDH ± ZONE_ATR_LOW, PDH ± ZONE_ATR_HIGH], SL en dessous/dessus, TP en multiple R:R. Filtre gap exclusion (jour de gap > GAP_EXCLUSION_ATR : skip). Filtre macro (FOMC/CPI/NFP).

### Marché cible

MES1, NQ1, YM1, MGC1 (portfolio 4 tickers).

### Période de test

Sept 2024 → mai 2026 (IS sept 2024 → 2025-09-30, OOS 2025-10-01 → mai 2026).

### Résultats backtest (portfolio)

| Ticker | PF OOS | n_oos | Bootstrap | P&L OOS |
|---|---|---|---|---|
| MES1 | 1.61 | 67 | 99.7% | +$3,255 |
| NQ1 | 1.05 | 44 | <1% | +$207 |
| YM1 | 0.74 | 78 | <1% | -$1,948 |
| MGC1 | 1.37 | 18 | n/a | +$234 |
| **Portfolio** | **1.22** | 207 | **24.8%** | +$1,748 |

Params optimaux IS-sélectionnés : `break_buffer_ticks=2, sl_atr_mult=0.5, tp_rr=1.5, max_retest_bars=16`.

### Raison de l'abandon

1. **Dilution multi-ticker** : YM1 (PF 0.74) et NQ1 (PF 1.05, BS <1%) drainent les gains de MES1 (PF 1.61, BS 99.7%).
2. **Bootstrap portfolio 24.8%** < seuil 50% 🟡 — rejet automatique.
3. YM1 structurellement adverse sur ce concept : pattern PDH/PDL cassure→retest moins fiable sur les indices large-cap avec beaucoup de bruit intraday.

### Variante conservée en veille

`pdh-pdl-mes1-v1` (MES1 seul, macro filter actif) : OOS PF 1.75, n=60, BS 99.7%, DD -$626. 🟡 VEILLE QUALIFIÉE — re-évaluer août 2026 si n_oos ~90. Code : `strategies/pdh_pdl_retest.py` avec `--ticker MES1`.

### Lessons learned

- **La dilution multi-ticker peut masquer un edge réel** : MES1 seul avait un edge solide (BS 99.7%) que le portfolio entier cachait (BS 24.8%). Tester systématiquement en single-ticker avant portfolio.
- **PDH/PDL breaker fonctionne différemment selon liquidité** : sur NQ1/YM1 (contrats plus tradés), les retests PDH/PDL sont souvent absorbés par les market makers avant d'atteindre la zone limite → taux de fill bas + sélection adverse.
- **3/4 params optimaux aux bords de grille** → signal de sur-ajustement probable. DSR 35.9% (snooping correction) confirme que le signal net est plus modeste que le PF brut suggère.

### Archives

`output/pdh-pdl-retest-v1/` : full/trades_v1.csv (portfolio), charts/.

Code conservé : `strategies/pdh_pdl_retest.py` (746 lignes), section `PDH_PDL_*` dans `config.py`. Variante MES1 réutilisable : `python backtest.py --strategy pdh_pdl_retest --ticker MES1`.

---

## Conclusion

Les stratégies abandonnées ci-dessus n'apparaissent plus dans le code production. Leurs artefacts sont archivés dans `output/archive/<strategy>/` et leurs scripts one-shot dans `scripts/archive/`.

**Pour proposer une nouvelle stratégie qui ressemble** à l'une de celles-ci : citer explicitement les apprentissages avant de relancer le pipeline `/new-strategy` (cf. `.claude/projects/.../memory/feedback_strategies_abandoned_lessons.md`).
