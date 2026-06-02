# Registre des hypothèses testées et falsifiées

> Trace des hypothèses formulées (utilisateur, littérature, `BACKLOG.md`, variante d'un edge prod) et **testées rigoureusement** par le pipeline gated, qu'elles aient été validées 🟢 ou rejetées 🔴.
>
> **Objectif** : éviter de re-tester sous une autre forme une hypothèse déjà invalidée. Capitaliser la connaissance accumulée → le hit-rate futur monte.
>
> **Écriture obligatoire** : à l'étape CAPITALISATION du pipeline, **toute** stratégie testée
> (🟢/🟡/🔴) ajoute 1 entrée ici. Consultation obligatoire à l'ÉTAPE 0 (ne pas re-tester un mort).

---

## Format

Chaque entrée doit contenir :
- `ID` : identifiant court (H4, ICT-OB-v1, etc.)
- `Date` : YYYY-MM-DD du test
- `Source` : utilisateur / littérature / `BACKLOG.md` / variante edge prod (OPR, Fib)
- `Verdict` : 🟢 PRODUCTION / 🟡 VEILLE / 🔴 REJET
- `Spec` : description courte de l'hypothèse
- `Métriques` : PF OOS, n_trades, bootstrap (ce qui a justifié le verdict)
- `Leçon` : ce qu'on retient, **surtout si rejet**

---

## Hypothèses rejetées 🔴

### kijun_pb-v1 — Kijun pullback bidirectionnel (autonome)

- **Date** : 2026-05-15
- **Source** : `@chartist` en mode `idea` (rapport du 2026-05-15, hypothèses H1+H1bis combinées)
- **Verdict** : 🟡 revendiqué → 🔴 **rétrogradé par `@auditor`**
- **Spec** : Stratégie autonome bidirectionnelle. Entrée LONG quand prix franchement hors cloud Ichimoku 15m (au-dessus + BUFFER × ATR), Kijun en pente positive, pullback récent sur la Kijun, retracement résolu (close > Kijun), cross StochRSI %K>%D depuis survente (%K[i-2] < 30). SHORT symétrique. Trigger : limit @ close[i-1]. SL sous low pullback + buffer ATR. TP en multiple ATR.
- **Métriques (OOS NQ1)** :
  - PF OOS = 1.49 (à 0.01 du seuil 🟢)
  - P&L OOS = +$1 119 sur n=26
  - **Bootstrap 74.7 %** vs Bonferroni (24 tests) requérant 99.79 % → **FAIL**
  - IS quasi-breakeven (PF=1.00, P&L=-$2 sur n=40)
  - Concentration P&L : +$1 717 sur 2 mois (nov+fév), reste OOS = -$598
  - Régimes : trending ✅ (PF 1.67), ranging ❌ (PF 0.91), neutral ❌ (PF 0.72)
  - Sans jours macro (n=3) : non_macro_day P&L = -$126
- **Artefacts** : `strategies/kijun_pb.py`, `output/rapport_kijun_pb.md`, `output/robustness_kijun_pb-v1.{json,md}`, `output/backtest_NQ1_kijun_pb.csv`
- **Leçon** :
  1. **PF OOS apparent ≠ edge réel**. PF 1.49 sur n=26 peut être du bruit, surtout si bootstrap fail Bonferroni.
  2. **L'edge OOS sans edge IS est suspect**. Si la stratégie est breakeven en IS et profitable en OOS, c'est probablement un régime favorable d'OOS, pas un edge structurel.
  3. **Concentration mensuelle = fragilité**. Une stratégie qui dépend de 2 bons mois sur 6 OOS n'est pas robuste.
  4. **Dépendance aux jours macro non revendiquée**. Si `EXCLUDE_MACRO_DAYS = False` mais la rentabilité OOS dépend de 3 jours macro, l'edge n'est pas généralisable.
  5. **Multi-ticker après échec = aggravation du multiple-testing**. Ne pas relancer en MES1/YM1 pour "voir si ça généralise" — c'est du data dredging.
  6. **Pattern Ichimoku canonique = méfiance**. Comme H4, kijun_pb exploite l'Ichimoku — indicateur ultra-documenté. Probable que l'edge soit arbitré.

### H4 — Filtre Ichimoku Cloud "hors cloud" sur OPR

- **Date** : 2026-05-15
- **Source** : `@chartist` en mode `idea` (14 charts hebdomadaires NQ1 + 1 panoramique D1)
- **Verdict** : 🔴 REJET (confirmé par `@auditor`)
- **Spec** : Ne déclencher les signaux OPR que si le prix est franchement hors du cloud Ichimoku 15m (`close[i-1] > cloud_high + BUFFER × ATR` pour LONG, symétrique short). 1 paramètre `OPR_H4_BUFFER_ATR ∈ {0.0, 0.3, 0.5, 0.8}`.
- **Métriques (OOS portfolio)** :
  - PF opr-v4 baseline = 1.52
  - PF opr_h4 (BUFFER optimisé per-ticker) = 1.46 → **−4 %**
  - PF OPR pur sans filtre = 1.53 → effet net H4 isolé = **−4.6 %**
  - Sweep monotone descendant 1.53 → 1.47 → 1.40 → 1.41 (pas de cloche)
- **Artefacts** : `strategies/opr_h4.py`, `output/rapport_opr_h4.md`, `output/robustness_opr_h4-v1.{json,md}`
- **Leçon** :
  1. **Voir un pattern ≠ trouver un edge non exploité.** Le pattern visuel « prix dans le cloud = chop » était corrélé à des semaines difficiles, mais **déjà capturé** par les filtres existants d'opr-v4 (`OPR_MIN_EXCURSION_ATR`, `OPR_MAX_VOL_ZSCORE`). Ajouter H4 par-dessus = redondance qui supprime des trades légitimes.
  2. **Sweep monotone = paramètre nuisible.** Pas une fenêtre à explorer plus finement.
  3. **Test biaisé EN FAVEUR de H4** (optimisation per-ticker du buffer) : même avec ce biais, H4 perd −4.6 %. Argument *a fortiori*.
  4. **Ne pas re-tester sous une autre forme** : variantes "cloud thickness filter", "cloud bullish/bearish align" sont des reformulations de la même idée. Si on les considère, justifier pourquoi elles ne tombent pas dans le même piège.

---

## Hypothèses validées 🟢

*(aucune pour l'instant — à compléter au fil des promotions production)*

---

## Hypothèses en VEILLE 🟡

*(aucune pour l'instant)*

---

## Hypothèses non encore testées (backlog)

Issues du rapport `@chartist idea` 2026-05-15 sur NQ1 :

- ~~**H1 + H1bis** — Kijun pullback bidirectionnel~~ → **TESTÉ 🔴** (cf. kijun_pb-v1 ci-dessus)
- **H2** — Post-macro continuation 1-2 jours après FOMC/CPI. Observé 2/2 sur les semaines macro de l'échantillon (sample size critique, à tester sur ≥ 20 dates macro). **Statut : non testé.** ⚠️ La leçon Ichimoku (cf. H4 et kijun_pb-v1) ne s'applique pas ici — H2 n'utilise pas Ichimoku.

---

## Leçons transversales (à consulter à l'ÉTAPE 0, avant tout nouveau test)

1. **Patterns canoniques ultra-documentés = méfiance (RED-FLAG ÉTAPE 0)** : 2/2 tests Ichimoku ont échoué (H4 filtre cloud, kijun_pb-v1 Kijun pullback). Indicateur trop documenté → arbitré. Privilégier des edges moins canoniques ou des variantes d'edges déjà prouvés en prod (OPR/Fib).
2. **Sample size critique** : tout test sur n < 30 (et a fortiori < 50) requiert une exigence supplémentaire (PSR fiable, bootstrap au-dessus de Bonferroni, P&L réparti sur ≥ 4-5 mois). Si ces garanties manquent, rejeter par sécurité.
3. **IS breakeven + OOS positif = signal d'alerte** : si une stratégie n'a pas d'edge en IS, son edge OOS est probablement un artefact de régime favorable, pas un edge structurel.
4. **Multi-ticker après échec = data dredging** : ne JAMAIS tester sur MES1/YM1 une hypothèse rejetée sur NQ1 dans l'espoir qu'elle marche "ailleurs". C'est aggraver le multiple-testing.
5. **Question "pourquoi cet edge existerait-il encore"** : si la réponse honnête est "ce n'est pas clair", la stratégie devrait être rétrogradée par sécurité, peu importe les métriques.
