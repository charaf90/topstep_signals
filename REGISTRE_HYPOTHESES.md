# Registre des hypothèses testées

> Trace des hypothèses déjà testées par le pipeline, pour **ne pas les re-tester** sous une autre forme.
> Consultation obligatoire à l'ÉTAPE 0 du pipeline.
>
> ⚠️ **RESET DES VERDICTS — 2026-06-17.** Le moteur de backtest maison (M15) ne résolvait pas
> correctement le **fill same-bar** : un SL touché dans la bougie même du fill d'un ordre limite était
> ignoré, et l'ordre fill/invalidation pouvait exclure les fills perdants. **Tous les verdicts chiffrés
> antérieurs (PF / BS / DSR) sont donc SUSPECTS** et remis à zéro. Nouvelle vérité = **backtesting.py
> sur barres M1** (`core/bt_engine.py`, fills résolus à la minute). On conserve ici : (a) la **liste**
> des hypothèses testées, (b) les **leçons mécaniques** (structure de marché, indépendantes du moteur).
> Les verdicts seront **reconstruits** par la campagne de re-validation M1 (stratégies prod d'abord).

---

## A. Red-flags structurels — mécanisme falsifié (NE PAS re-tester sans angle vraiment neuf)

> Rejets fondés sur la **structure de marché / l'absence d'edge par trade**, pas sur la résolution de
> fill → **indépendants du moteur** (le M1 ne les sauverait pas). À bloquer à l'ÉTAPE 0.

- **Fade-de-sweep sur indices US (4/4 🔴)** — liq-hook, exp-hunt, asian-sweep, sfp-fvg. Les sweeps de
  liquidité US **continuent** plus qu'ils ne reversent, **même confirmés par un BOS** (sfp-fvg).
- **Ichimoku canonique (2/2 🔴)** — opr-h4 (filtre cloud), kijun-pb (pullback Kijun + StochRSI). Aucun
  edge marginal non déjà capturé par les filtres existants.
- **Mean-reversion sur indices US** — gmr-v1 (fade EMA50), xmkt-rv / ymnq-div (RV inter-indices),
  arf-v4 (range asiatique). Les indices US **continuent** à la cassure ; seul l'OR a une lueur
  mean-reverting (non robuste). → tout détecteur de divergence YM/NQ = doublon à bloquer.
- **Open-drive-continuation (famille fermée)** — odc-v1 (3 bougies same-color, trop rare), odc-v2
  (drive élargi). Le pullback rate les vrais drives et fille ceux qui échouent (sélection adverse).
- **Gate de régime (EMA tendance / RSI momentum / ADX force) — falsifié M15 ET H1** — cycle-recalib
  trend gate + h1-regime-gate (0/4 strats prod). Un overlay de régime grossier **retire des trades**
  (n↓, variance↑) sans améliorer l'edge/trade ; tout gain IS s'évapore OOS. Ne pas re-tester un gate
  « TF supérieur + un indicateur de plus ».
- **Géométrie Fib nue sans filtre** — gp-v1 (golden pocket 0.618 sur 1ʳᵉ impulsion), fib-argrel-v1
  (niveau 61.8 nu). Le retracement profond nu = pas d'edge ; fib-v4 marche par son **filtre**, pas la
  géométrie shallow.
- **Break-retest hors 1ʳᵉ heure** — onr-v1 (range overnight), phc-v1 (range de midi). La mécanique
  break + retest LIMITE est bonne mais dépend de la **force du niveau** : seule l'IB (1ʳᵉ heure RTH,
  enchère du jour) est assez structurelle. Triade : onr 🔴 · phc 🔴 · **ib-retest (1ʳᵉ heure) = la base**.
- **Setups standalone divers (🔴)** — smc-v1 (empilement de zones SMC dilue le signal), vpc-v4 (volume
  profile reconstruit depuis M15, trop approximatif), pivot-rev-v1 (edge ML de classif ≠ edge trading),
  squeeze-v1 (la contraction prédit l'EXPANSION mais PAS la direction), gap-fill-v1 (entrée post-confirm
  = gap déjà comblé), eia-inv-v1 (event hebdo, n trop faible), spike-1m/opr_v10 (frictions M1 > micro-edge).
- **Autres 🔴 structurels** — fib-v3 (pending non invalidé après cassure du pivot d'impulsion → règle :
  invalider le pending si la zone source casse), pdh-pdl-retest-v1 (dilution multi-ticker), vwap-pb v1.1
  (seuil quantile figé IS, casse au drift de régime), opr-v5 (filtre **post-fill** non exécutable en live).

## B. Re-validation M1 des stratégies (campagne 2026-06-18)

> Re-jouées sur M1 honnête (`core/bt_engine`), params prod, fenêtre WF **décalée à la couverture
> M1** (IS 2025-02-16→2025-12-31, OOS 2026-01-01→2026-04-15). _Le fetch M1 profond broker est
> impossible (API plafonnée ~20 k barres) → on reste sur le M1 disque (2025-02-16→2026-06-05)._
> Moteur validé : calibration backtest↔live du 17 juin = MATCH exact (Δ = friction).

| ID | Verdict M1 | Détail |
|---|---|---|
| **opr-v5.1** (YM1) | ✅ **ROBUSTE** | OOS PF **2.97** ; bootstrap PF **99.8 %** ≥ Bonferroni 99.0 %, plateau ✓. L'edge F2 tient sur M1. |
| **fib-fine-v2** (NQ1+MES1) | ✅ **🟢 ROBUSTE** | OOS pool 1.95 ; bootstrap **100 %** ≥ Bonferroni, PSR 99.6 %, DSR 88 % ; **multifold 3/5** (recousu ~2.0, params 0.382/0.75 STABLES — folds 2-3 plus faibles = régime fin 2025). |
| **fib-fine-v2 NQ1 — re-optim tp_mult** (2026-06-19) | ✅ **PROMU** | Optuna **continu** min_imp×tp_mult (vrais leviers, plateau ✓ — vs sl_mult pic isolé). Sur NQ1 `min_imp` inopérant (n identique) → seul levier `tp_mult` **1.5→1.2**. Pré-enregistré (N_tests=1) : multifold **5/5 folds +** (recousu 3.25), OOS PF 3.98, **hold-out terminal 2026-04-15→06-18 PF 2.67 (+$766, n=21)**, bootstrap 100 % ≥ Bonferroni 95 %, P(DD>limite) 2.6 %. Gain = consistance (prod = 1 fold perdant) / −DD, −15 % P&L. **MES1 insauvable** (OOS PF ~1.15 quel que soit le param). ⚠️ Recherche large (100 trials) = DSR 8.6 % : NE PAS promouvoir sur l'exploration, seulement après confirmation pré-enregistrée. |
| **fib-fine-v2 NQ1 — ré-opt IS étendu** (2026-06-25) | ⚖️ **ARBITRAGE (pas d'auto-adoption)** | Ré-opt sur M1 NQ1 long (2022-12→2026-06), A/B isolant l'IS (OOS/HO = valid prod). L'IS long sélectionne **0.618/sl 1.0 ≠ prod 0.382/sl 1.5** = un AUTRE edge. **PROD = régime-spécifique** : PF/an 2023 **0.99** · 2024 1.10 · 2025 1.50 · 2026 **3.61** (le PF OOS 3.55 est un phénomène 2025-26). **RE-FIT 0.618/1.0 = STATIONNAIRE** : PF ~1.28-1.40 chaque année, +$5-8k/an, +de trades, plateau ✓, OOS récent modeste (1.28, 🟡). « Plus de données » ne dilue pas l'edge → révèle lequel est réel. Sur le régime récent PROD domine (P(target) 100 % vs 89 %, 26j vs 41j) **et tourne en live** → NE PAS changer mécaniquement. Décision = arbitrage user (statu quo + surveiller décroissance de régime, OU bascule robustesse). Illustre la leçon #10. **DEEPDIVE (2026-06-26)** : l'arbitrage PROD/REFIT etait une FAUSSE alternative — **fib_level 0.5 = sweet spot** (0.5/0.75 +$27.9k le + haut ; 0.5/1.0 +$24.2k ; positif chaque annee ET garde 2026 a ~2.4 PF). Le WF expanding honnete ne choisit JAMAIS le live 0.382/1.5. **Candidat a valider = 0.5/1.0** (robustesse+hold-out+@auditor pas faits). Sorties brouillon/output/fib_fine_ext_reopt/ (report.md + deepdive.md). |
| **fib-fine-v2 NQ1 — valid candidat 0.5/1.0** (2026-06-26) | 🟡 **VEILLE (@auditor : 🟢→🟡)** | Deep-lane sur données standard : OOS PF 2.31 (n=34, +$3,397, BS 100% → 🟢 mécanique) · hold-out 2.54 · bootstrap 99.8% ≥ Bonferroni · DSR 77% · MC DD Topstep-safe · multifold 4/5. **@auditor RÉTROGRADE 🟢→🟡** : (1) sélection OOS-informée (0.5 repéré sur heatmap incluant 2026) → OOS/HO non indépendants ; (2) le 🟢 repose sur le MÊME artefact bootstrap_pass_rate que le 🔴 de PROD, en sens inverse (CAND passe car net $3,397 > target $3,000) ; (3) DSR/Bonferroni n=9 optimiste (budget cumulé ≥50) ; (4) ranging 0.71 ; (5) plus de hold-out frais. Code SAIN (no leak). Conditions promotion : pré-enregistrer + confirmer sur fenêtre forward NEUVE (~Q3-2026) + DSR au budget cumulé + backtest_vs_live 0-div + portfolio_replay + @forge. Cf. leçon #10. **➡️ MIS EN LIVE le 2026-06-26 par DÉCISION USER (override du 🟡)** : config NQ1 0.382/1.5→0.500/1.0 + restart (PID 247604). Garde-fou risque combiné OK (MC DD P95 −$1062, breach 0%). Pari de diversification de régime ASSUMÉ ; conditions de valid propre restent dues → **MONITOR : PF NQ1 live < 1.2 sur trimestre glissant ⇒ rollback** (config NQ1→0.382/1.5 + restart). |
| **ib-retest-v3** (MES1+NQ1) | ⚠️ **MARGINAL** | Calib live exacte + OOS pool **1.65** (+$2 222) MAIS **IS breakeven** (0.89/0.95) ; robustesse M1 (grille 18) : bootstrap **88.8 % < Bonferroni 99.72 %**, PSR 86 %, **DSR 19.5 %** → ne passe PAS en re-optim, OOS probablement **régime-dépendant** (leçon #2). À trancher @auditor ; pas un 🟢 honnête sur M1. |
| **fib-v4.1** (MES1+NQ1) | 🔴 **ABANDONNÉE** | OOS pool **0.85 (−$827)**, IS **et** OOS faibles. L'edge M15 (causal inclus) était **un artefact same-bar** (11/29 trades M15 résolus à la bougie de fill). **Décision user 2026-06-18 : oublier, redévelopper FROM SCRATCH** comme nouvelle strat (cf. BACKLOG). ✅ **COUPÉE en live le 2026-06-19** (`FIB_V4_ENABLED=False`). |
| ~~fib-v4-tight~~ | 🔴 **ABANDONNÉE** | Variante de fib-v4 → caduque avec elle. |
| bos-fvg (inerte) | ⚠️ **MARGINAL M1** | OOS M1 **NQ1 1.16 / MES1 1.13** (vs prod M15 ~2.5 = artefact fill-bias confirmé) → **reste INERTE**, pas un candidat réactivation (PF < seuil VEILLE 1.2 sur NQ1). |
| zones-v1 | ⏳ à re-tester M1 | 🟡 M15 ; OOS frais M1 (M5/M1 params gelés étaient 🔴). |
| pdh-pdl-mes1-v1 | ⏳ à re-tester M1 | 🟡 M15. |
| **opr-NQ1 causal (F2 OFF) — ré-éval métrique forward** (2026-06-26) | 🟡 **REVIVÉ (ex-"fatal" = artefact)** | Le verdict « Topstep-FATAL » (MC DD pire-cas −$8.4k) était un artefact : MC-DD permutation TOUTE-fenêtre ignore l'arrêt au target. Re-jugé `topstep_forward_mc` (compte neuf $50k) : F2-OFF causal (honnête, NO LEAK) PF 1.45/+$50.6k mais pire JOUR −$3,042 → sizing actuel P(target) 71 % (breach **daily** 22 %). **Re-sizé ×0.30** (pire jour −$900) : **P(target) 94 %/90 %, breach daily 0 %**, jours→target ~130. ⇒ blocage = SIZING vs DLL $1000, PAS l'edge. OPR NQ1 CAUSAL redevient candidat 🟡 (vs F2 non-reproductible). Reste 🟡 : Bonferroni fail (98.8 %<99.90 %), queue grasse. Next = vrai re-run risk réduit + multifold + @auditor. Cf. leçon #10, brouillon/scripts/opr_nq1_metric_reeval.py. |
| **opr-rebuild (from scratch)** | 🔴 **PAS D'EDGE** | Reconstruction OPR de zéro (M1, Optuna continu, YM1+NQ1+MES1, 2026-06-19). Breakout nu = perdant (PF 0.74-0.93) ; design-tuning = breakeven (OOS 1.00) ; filtre range OR/ATR LIBRE = dégénère (n→1) ; filtre CONTRAINT = IS↑ pool 1.35 mais **OOS 0.55 (−$5 661)**. Le breakout OPR n'a pas d'edge standalone ; tout filtre en optim libre sur-fitte IS→OOS. **Améliorer OPR = @quant (feature causale + perm-test), pas Optuna libre.** Seul opr-v5.1 (YM1, F2 pré-enregistré) tient. |

## Leçons transversales (mécaniques — consulter avant tout nouveau test)

1. **L'edge est dans la QUALITÉ DU FILL**, pas la géométrie : cassure FRANCHE + retest LIMITE
   (ib-retest), filtre wick / expansion-vol data-driven (fib). Une entrée market (next-bar open) ne
   porte aucun edge de fill (gmr / squeeze / odc tous 🔴) → tout winner du système utilise une LIMITE.
2. **IS breakeven + OOS positif = artefact de régime**, pas un edge structurel.
3. **n < 30 OOS** → exiger bootstrap > Bonferroni + P&L réparti sur ≥ 4-5 mois, sinon rejet par sécurité.
4. **Multi-ticker** : tester single-ticker d'abord (la dilution peut MASQUER comme FABRIQUER un edge).
5. **Edge ML de classification ≠ edge de trading** — toujours backtester le PnL net.
6. **Seuils quantiles figés IS** cassent sur drift de régime structurel (vwap-pb v1.1).
7. **Espace Optuna riche (≥10 dims) à 100-200 trials = winner's curse** — détection : triplet plateau
   + multifold + **DSR au budget de tests CUMULÉ** ; plancher n_IS dans le score (sinon combo n=1 dégénère).
8. **« Pourquoi cet edge existerait-il encore ? »** — si la réponse honnête est floue, rétrograder.
9. **[M1 — fondement du reset] Le fill same-bar et l'ordre fill/invalidation décident du verdict** sur
   tout ordre LIMITE : le fill intra-bar doit PRÉCÉDER l'annulation d'un pending, et un SL peut toucher
   dans la bougie même du fill. C'est exactement ce que le moteur M15 ratait → **backtester en M1 only**.
10. **Le PF est un GATE d'edge, pas un critère de CLASSEMENT Topstep** (audit 2026-06-25). Aveugle à la
    fréquence et à la magnitude → une HF PF 1.2-1.5 robuste peut dominer une PF 1.9 sur **P(target avant
    breach)**. ⚠️ Le `bootstrap_pass_rate` du verdict (`core/metrics.compute_topstep`) **n'est PAS** une
    sim forward (permutation même-fenêtre sans remise). **✅ INTÉGRÉ (2026-06-26)** : `core/robustness.py`
    expose `topstep_forward_mc` + `topstep_utility` → bloc **« Utilité Topstep »** (P(target) + freq +
    expectancy/jour) dans tout `robustness_<id>.{json,md}`, à côté du PF ; verdict 🟢/🟡/🔴 INCHANGÉ (PF
    reste le gate, métrique informative). `portfolio_replay.challenge_outcome_mc` délègue (source unique).
    Cf. mémoire `pf-vs-topstep-utility-metric`.

---

## Outillage — décisions d'infrastructure (ne pas re-explorer sans changement de contexte)

- **Moteur de backtest = backtesting.py sur M1** (`core/bt_engine.py`), adopté **2026-06-17**. Remplace
  le moteur maison M15 (fill same-bar faux). Les signaux M15/M5 sont reconstruits depuis le M1 (features
  décalées à la **clôture** de barre = no leak), fills + SL/TP résolus à la **minute**. Sortie **HTML viz**
  nommée : `output/backtests/<id>__<ticker>__<tag>__m1.html`. _Note : le survey 2026-06-12 avait rejeté
  backtesting.py (« mono-actif, casserait le partage core/ ») ; objection LEVÉE — la live-équivalence est
  désormais garantie par la **calibration backtest↔live** (`tools/backtest_vs_live.py`, réconciliation M1),
  pas par le partage de code._
- **Optimisation = Optuna ≥ 4.0** (TPE seedé, score **IS-only**) — `optimize.py --search optuna`.
- **Filtres data-driven = scikit-learn** (rôle `@quant` : RF / LogReg + **TimeSeriesSplit** + permutation
  / **Bonferroni**).
- **Données M1** = `DATA_BACKTEST/{MES1,NQ1,YM1}_data_m1.csv` (indices uniquement ; **pas de M1 gold/oil**).
  Couverture disque 2025-02-16 → 2026-06-05 ; au-delà = fetch broker.
