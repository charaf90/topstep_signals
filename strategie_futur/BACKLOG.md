# Backlog des stratégies à explorer

> **Source de vérité** pour les idées de stratégies en attente de développement.
> Mis à jour manuellement quand une idée est ajoutée, sortie en dev, ou abandonnée.
>
> Pour développer une idée → la passer en `@athena` ou `/new-strategy` puis mettre à jour la colonne **Statut**.
> Stratégies abandonnées → déplacer la ligne vers `docs/strategies_abandoned.md` avec leçon compacte.

---

## Légende

- **Priorité** : P1 (à pousser dans les prochaines semaines) / P2 (à creuser après P1) / P3 (idée intéressante, pas urgente)
- **Statut** : `Idée` (brut) / `À formaliser` (règles à préciser avant @athena) / `Fiche prête` (fichier dédié dans `strategie_futur/`) / `En dev` (passé en @athena ou @new-strategy) / `Verdict 🟢🟡🔴` (résultat)

---

## Backlog actif

| Priorité | ID | Idée | Marché cible | Hypothèse / Edge | Source | Statut | Note |
|---|---|---|---|---|---|---|---|
| ~~P1~~ | ~~eia_inventory_retest~~ | ~~EIA Inventory Retest~~ | ~~MCL1~~ | ~~Spike post-annonce retracé~~ | ~~Convergence L2-9, L3-8~~ | **🔴 Rejeté 2026-05-26** | IS PF 0.98 < 1.0 (arrêt précoce). Fréquence event-driven hebdo incompatible avec n_oos ≥ 50 projet (61 signaux sur 20 mois). Biais M15 détecté/corrigé en dev (leçon transversale). Cf. `docs/strategies_abandoned.md` |
| P1 | vwap_pullback_portfolio | VWAP pullback en tendance | NQ1 + MGC1 (MCL1 exclu) | Mean-reversion vers VWAP intraday en tendance forte (M15) | Convergence 3 listes externes (L1-8, L2-6, L3-5) | **🟡 VEILLE QUALIFIÉE FINAL (2026-05-26)** | PF OOS 1.51 bootstrap 99% n=131. Pipeline complet : v1 baseline → quant patch v2 (rollback Bonferroni-fail) → PHASE 4 WF optim (0/81 Bonferroni, baseline reste optimum) → PHASE 5 stress régime (62% P&L sur trending, fragile) → v1.1 SKIP_VOL_H (rollback drift régime MGC1 +148%). Aucune promotion live. À re-évaluer dans 3-6 mois sur nouvelles data OOS si régime stabilisé. Code conservé `strategies/vwap_pb.py`. |
| ~~P1~~ | ~~pivot_detector_ml~~ | ~~Détecteur de pivots ML (MGC1)~~ | ~~MGC1 (M5)~~ | ~~ML précision 28%~~ | ~~Étude clôturée 2026-05-18~~ | **🔴 Rejeté 2026-05-26** | Tentative `pivot-rev-v1` rejetée structurellement (PF IS 0.97, 70% expiration trigger). Audit confirme code clean. Edge ML ≠ edge trading. Cf. `docs/strategies_abandoned.md` |
| ~~P2~~ | ~~gap_fill_open_ny~~ | ~~Gap Fill open NY~~ | ~~MES1, NQ1, YM1~~ | ~~60-70% des gaps < 0.5% comblés le jour même~~ | ~~L2-8, L3-6~~ | **🔴 Rejeté 2026-05-26** | IS PF 0.54, OOS PF 0.89. Défaut structurel : après confirmation sur 1ère barre M15, TP résiduel trop petit vs SL ATR → RR médian 0.5. Meilleur combo IS : +$37. Pas de combo positif NQ1/YM1. Leçon : edge gap-fill n'existe pas avec mécanique d'entrée marché post-confirmation. V2 hypothétique : entrée LIMIT + TP 50% gap. Cf. `docs/strategies_abandoned.md` |
| ~~P2~~ | ~~pdh_pdl_retest~~ | ~~PDH/PDL retest après cassure~~ | ~~MES1, NQ1, YM1, MGC1~~ | ~~Cassure PDH/PDL → retest support/résistance~~ | ~~L1-4 (Breaker ICT), L3-3~~ | **🔴 Portfolio rejeté 2026-05-27** | OOS PF 1.22, Bootstrap portfolio 24.8%. YM1/NQ1 drainent. Cf. `docs/strategies_abandoned.md` |
| P2-MES1 | pdh_pdl_mes1 | PDH/PDL Retest MES1-only | MES1 | Même concept, 1 ticker, sans dilution multi-ticker | Extraction de pdh_pdl_retest v1 | **🟡 VEILLE QUALIFIÉE 2026-05-27** | OOS PF 1.63, P&L +$3,255, n=67, bootstrap 98.2% (< Bonferroni 99.94% par 1.74 pp). DSR 28.3%. 3/4 params aux bords de grille. Filtre macro à activer (PF macro_day 0.83). Params optimaux : bb=2, sl=0.5, rr=1.5, bars=16. Re-éval août 2026 si n_oos ~90. Code : `strategies/pdh_pdl_retest.py --ticker MES1`. |
| P3 | asian_range_sweep | Asian range fake breakout | MCL1, MGC1 | Range Asie (22h-08h GMT) → fausse cassure à l'ouverture Londres → reversal | Convergence 3 listes (L1-6, L2-4, L3-10) | Backlog | MGC1 actif en Asie, MCL1 fakeout amplifié |
| P3 | ib_retest_opr_variant | Initial Balance retest (variante OPR) | MES1, NQ1, YM1 | Au lieu d'entrer sur la cassure IB, attendre pullback sur la borne IB | L3-4 (Market Profile) | Backlog | Pourrait améliorer le winrate d'OPR sans casser la version actuelle |
| P3 | golden_pocket_first_impulse | Golden Pocket 61.8% sur 1ère impulsion | Tous | Fib calculé sur la 1ère vague impulsive du jour (≠ swing structure de fib-v4) | L3-6 | Backlog | Variante à tester en concurrent de fib-v4 |

---

## Filtrage initial — stratégies écartées d'office (pour mémoire)

Issues des 3 listes externes consultées le 2026-05-26, non retenues :

| Stratégie | Raison de l'écart |
|---|---|
| Pivots S1/R1 | Trop redondant avec d'autres concepts de niveaux pivots |
| Midnight Price NY | Pas de règle d'invalidation claire |
| Reversal fin session EU 17h30 | Anecdotique, non quantifiable |
| ORB cassure standalone | Doublon avec OPR v5.1 |
| Retracement 50% pivot ORB | Doublon avec Fib v4 (variant du même edge) |
| Chiffres ronds / FVG comblement | Dépendants d'eyeballing, peu quantifiables en 15m |
| London Fix Reversal Oil (16h GMT) | Concept faible sur futures US, pas de support inter-sources |

---

## vwap-pb-v1 — Pipeline complet exécuté 2026-05-26 ✅

Toutes phases exécutées, verdict final **🟡 VEILLE QUALIFIÉE** (pas de promotion live).

- [x] PHASE 2-3 baseline v1 : PF OOS 1.51, bootstrap 99%, n=131
- [x] @quant discover v2 : patch rollback (Bonferroni-fail empirique, +0.05 PF vs +0.50 promis, -$1,202 P&L)
- [x] PHASE 4 WF optim 81 combos : 0/81 Bonferroni strict, DSR 28.7%, baseline reste optimum (best 1.37 < 1.51)
- [x] PHASE 5 stress régime : 2/3 régimes profitables, 62% P&L sur trending, MC DD breach Topstep
- [x] v1.1 SKIP_VOL_H : 🔴 rollback (drift régime MGC1 +148%, élimine 100% trades MGC1 OOS)
- [x] Audit @auditor : code clean, 0 alerte bloquante
- [ ] **À faire si reprise future** : tester sur nouvelles data OOS dans 3-6 mois si régime Gold stabilisé
- [ ] **Tâche infra** : fix bug numpy `core/chart.py` (bloque chartist audit futurs)

## Convention d'ajout d'une idée

1. Ajouter une ligne au tableau **Backlog actif** avec un `ID` en `snake_case`
2. Si l'idée mérite plus que 1 ligne (hypothèse complexe, références multiples) : créer `strategie_futur/<id>.md` et passer le statut à `Fiche prête`
3. Quand l'idée part en dev : passer le statut à `En dev` + lien vers le run @athena
4. Verdict final → mettre à jour le statut et soit promouvoir en prod, soit déplacer en `docs/strategies_abandoned.md`
