---
name: new-strategy
description: Développement complet et autonome d'une stratégie de trading intraday — analyse du concept, implémentation, backtest, optimisation walk-forward avec correction multiple-testing, stress tests, Monte Carlo, charts et rapport. À invoquer uniquement sur commande explicite de l'utilisateur.
disable-model-invocation: true
argument-hint: "[description de la stratégie]"
allowed-tools: Bash Read Write Edit Grep
effort: max
context: fork
agent: general-purpose
---

ultrathink

# Ingénierie complète d'une stratégie intraday — topstep_signals

## Contexte projet

Versions en production :
!`grep -E "(OPR|FIB_V4)_STRATEGY_VERSION|FIB_V4_ENABLED|OPR_V5_1_LIVE_TICKERS" config.py`

Données CSV (TradingView, historique long) :
!`ls data/`

Stratégies découvertes (plug-and-play) :
!`python -c "from core.registry import list_strategy_names; print(list_strategy_names())"`

État live :
!`cat state/live_state.json 2>/dev/null | head -30 || echo "Pas de session live active"`

## Benchmark production (mise à jour 2026-05-19)

| Stratégie       | PF OOS | n_oos | Bootstrap | Actifs                |
|-----------------|--------|-------|-----------|-----------------------|
| OPR opr-v5.1    | élevé  | ~95+  | 100 %     | NQ1·YM1               |
| OPR opr-v4      | 1.36   | 121   | (legacy)  | MES1 (pass-through)   |
| Fib fib-v4      | **5.06** (portfolio) | 70 | **100 %** | MES1·NQ1·MGC1 |

Toute nouvelle stratégie doit battre **PF OOS ≥ 1.5** ET bootstrap stationnaire ≥ 80 %
avec n_oos ≥ 20 pour passer en 🟢 (cf. critères verdict).

**Contraintes Topstep :** daily loss −$1 000 · trailing DD −$2 000 · target +$3 000
**Walk-forward sur actifs standards (dates fixes) :** IS = sept 2024 → 2025-09-30 | OOS = 2025-10-01 → mai 2026
**Walk-forward sur nouveaux actifs (data_fetcher) :** voir PHASE 1.5 — adapté à la fenêtre disponible.
**Frictions à modéliser :** `SLIPPAGE_TICKS_PER_TICKER` + `COMMISSION_RT_PER_CONTRACT` depuis `config.py` (déjà calibrés).

---

## Architecture — règle absolue

```
RECHERCHE (zone de travail de ce skill)        →  strategies/<strategy_id>.py
INFRA PARTAGÉE (réutiliser, ne pas dupliquer)  →  core/metrics, core/backtester, core/optimizer
PRODUCTION (NE PAS TOUCHER)                    →  core/opr.py, core/strategy_fib.py, broker/
```

**Ce skill travaille exclusivement dans `strategies/` et `config.py`. Aucun fichier `core/` ou `broker/` n'est modifié sans confirmation explicite de l'utilisateur.** La promotion en production est un workflow séparé (cf. PHASE 8).

---

## Outils existants à utiliser

- **`core/metrics.py`** : verdict 🟢🟡🔴 automatique, métriques standardisées, bootstrap. **Ne pas réécrire ces fonctions** — les importer.
- **`core/backtester.py`** : runner universel, génère 10 day charts aléatoires si `--plot` + 5 charts portfolio mutualisés (equity, DD underwater, monthly heatmap, hourly distribution, correlation rolling) via `generate_portfolio_charts()`. **Ne pas dupliquer ces fonctions dans `strategies/`.**
- **`core/optimizer.py`** : walk-forward universel + rapport de décision auto + **pipeline robustesse complet en bout de course** (Bonferroni, PSR, Monte-Carlo DD, stress par régime, worst-case clustering) → écrit `output/robustness_<id>.{json,md}`. Les analyses ajoutées viennent **en plus**, pas à la place.
- **`core/robustness.py`** : module statistique réutilisable (`run_full_robustness(trades, n_strategies_tested, topstep_dd_remaining)`). Auto-test : `python -m core.robustness`.
- **`core/registry.py`** : auto-discovery des stratégies — placer un fichier dans `strategies/<nom>.py` suffit, pas besoin d'éditer `backtest.py`/`optimize.py`. Tester avec `python backtest.py --list`.
- **`core/data_fetcher.py`** : fetch historique TopstepX via l'API ProjectX, écrit des CSV au format projet directement utilisables par `backtest.py --csv-dir ./data`. Couvre tous les timeframes (m1 → mo1) et tous les actifs disponibles (indices, métaux MGC, énergie MCLE, crypto MBT/MET, devises, taux). **À utiliser pour étendre les backtests à des actifs hors MES/NQ/YM**.
- **`core/risk_topstep.py`** : garde-fou par trade. Toujours appelé via `trade_allowed()`.
- **`broker/tg_notify.py`** : notifications Telegram HTML. `python broker/tg_notify.py "MESSAGE"`.

### Sources de données — quand utiliser quoi

| Cas | Source | Pourquoi |
|---|---|---|
| Stratégie sur MES1/NQ1/YM1/MGC1/MCL1/M6E1 | CSV dans `data/{TICKER}_data_m15.csv` (DATA_BACKTEST, sept 2024 → mai 2026) | Historique long (~20 mois) → walk-forward IS/OOS aux dates fixes ; m5 aussi disponible (oct 2025 →) |
| Stratégie sur **autre actif** (MBT, M2K…) | `python -m core.data_fetcher --symbol <ALIAS> --timeframe m15 --days <N> --save --ticker <NEW_TICKER>` | API ProjectX limitée à 20 000 bars/req + un contrat = une échéance (~2-3 mois d'historique max) |
| Stratégie nécessitant h1/h4/d1 sur actifs standards | `build_timeframes(df_15m)` — resample automatique en mémoire | Nativement disponible via `core/data.py`, pas besoin de fetch séparé |
| Découvrir le catalogue ProjectX disponible | `python -m core.data_fetcher --available` | Affiche les 40+ contrats actifs |
| Lister les alias raccourcis du data_fetcher | `python -m core.data_fetcher --list` | MES, MNQ, MYM, M2K, MGC, MCLE, MBT, MET, etc. |

**Important — contrainte d'historique pour les nouveaux actifs** : un contrat front-month ProjectX (ex: `CON.F.US.MGC.M26`) a une vie d'environ 2-3 mois. Au-delà de cette fenêtre, l'API renvoie 0 bougie. Conséquences pour le walk-forward :

- **Actifs standards (MES1/NQ1/YM1)** : utiliser les dates fixes du projet (IS=déc2024→sept2025, OOS=oct2025→mars2026).
- **Nouveaux actifs** : adapter le walk-forward à la fenêtre réellement disponible. Règle pragmatique : **IS = 60 % les plus anciens, OOS = 40 % les plus récents**, avec un minimum de 20 trades OOS. Documenter clairement le choix dans le rapport. Avertir l'utilisateur que ces résultats sont moins robustes statistiquement (< 100 trades typiquement) — verdict 🟢 peu probable, 🟡 plus réaliste.
- **Stitcher plusieurs échéances** (ex: MGC.M26 + MGC.U26 + …) est techniquement possible mais hors scope d'une V1 de stratégie. À envisager seulement si l'actif passe 🟡 sur un seul contrat et qu'on veut consolider.

---

## Ton rôle — ingénieur quant senior

La description ci-dessous est un **point de départ, pas une spécification figée**.
Tu as **carte blanche** pour enrichir, corriger, ou rejeter l'idée.
Chaque décision doit être **argumentée**. Aucun choix arbitraire.

**Limites :** max 5 itérations · arrêt anticipé si concept structurellement fatal.

### Anti-patterns à refuser explicitement

Avant chaque phase, vérifie qu'aucun de ces pièges n'est présent :

- **Modifier `core/` ou `broker/`** sans confirmation explicite : interdit en mode recherche.
- **Hardcoder un paramètre** : tout va dans `config.py`, jamais dans le `.py` de stratégie.
- **Look-ahead** : décision sur la barre `i` doit utiliser uniquement `df.iloc[:i]` ou `prev`. Jamais `bar["close"]` pour décider sur la même barre.
- **Survivor bias** : ne sélectionne pas les actifs *a posteriori* sur leurs résultats.
- **Curve-fitting** : > 4 paramètres optimisés simultanément = drapeau rouge.
- **p-hacking** : tester N configs et prendre la "meilleure" sans correction multiple-testing.
- **Fill optimiste** : si SL et TP sont dans le range d'une même barre, **assume SL prioritaire** (impossible à départager sans M1).
- **Frictions ignorées** : slippage et commissions doivent figurer dans la simulation.
- **Bump de version oublié** : `<STRAT>_STRATEGY_VERSION` doit changer à chaque modification structurelle.
- **OOS portfolio < 50 trades** = bootstrap peu informatif → 🟡 max.
- **Schéma colonnes non standard** : casser le schéma standard casse `core/optimizer.py`. Voir PHASE 2.

---

## Stratégie à développer

$ARGUMENTS

---

## Ressources

- Template Python : [templates/strategy_template.md](templates/strategy_template.md)
- Template rapport : [templates/rapport_template.md](templates/rapport_template.md)

---

## Notifications Telegram

```bash
python broker/tg_notify.py "MESSAGE_HTML"
```
Utilise les balises HTML : `<b>`, `<i>`, `<pre>`. Échappe `&` en `&amp;`.

---

## Critères de verdict (étendus)

Le verdict de `core/metrics.py` reste la référence. Ces critères supplémentaires affinent la décision finale.

| Critère                                  | 🟢 PROD  | 🟡 VEILLE | 🔴 REJET |
|------------------------------------------|----------|-----------|----------|
| OOS Profit Factor                        | ≥ 1.5    | ≥ 1.2     | < 1.2    |
| Bootstrap **portfolio** OOS              | ≥ 80 %   | ≥ 50 %    | < 50 %   |
| Trades OOS portfolio                     | ≥ 50     | ≥ 20      | < 20     |
| P&L OOS net (frais + slippage)           | > 0      | > 0       | ≤ 0      |
| Dégradation IS→OOS (PF)                  | ≤ 30 %   | ≤ 50 %    | > 50 %   |
| Stabilité paramètres (voisinage)         | plateau  | semi-plat | pic isolé|
| Corrélation P&L daily vs OPR/Fib         | < 0.5    | < 0.7     | ≥ 0.7    |
| Performance régimes (trending+ranging)   | OK les 2 | OK ≥ 1    | KO les 2 |
| MC P95 du DD                             | < limite Topstep restante | < limite | ≥ limite |

> **Note bootstrap par ticker** : un actif individuel peut avoir un bootstrap bas (ex: MES1 5.8 %) tout en restant valide si le **portfolio** est ≥ 80 %. Toujours raisonner portfolio en priorité.

---

# PHASES — exécute dans l'ordre

## PHASE 0.5 · Idéation visuelle (optionnelle)

> Phase d'exploration visuelle préalable, à utiliser quand le concept de
> stratégie est **flou ou non formalisé** et qu'il faut s'inspirer de ce
> que fait réellement le marché. Pour un concept déjà clair (ex: "ORB
> pullback NQ 09:30-11:00"), passer directement à PHASE 1.

### Quand l'invoquer
- L'utilisateur demande "trouve-moi un edge sur NQ1" (concept ouvert)
- Athena estime que la formalisation directe par `@researcher` risque de
  produire une "stratégie de manuel" déconnectée du marché réel
- On veut comparer plusieurs marchés (NQ vs MES vs YM) avant de choisir
  le ticker primaire

### Workflow

1. **Générer les charts exploratoires** via `core/explore_chart.py` :
   ```bash
   # Mono-TF (15m uniquement)
   python -m core.explore_chart --ticker NQ1 --n 20 --stratify regime

   # Multi-TF (trio 15m + H1 + D1 pour chaque jour) — recommandé pour
   # les concepts nécessitant un contexte structurel (ICT, Wyckoff,
   # market profile, structure de marché)
   python -m core.explore_chart --ticker NQ1 --n 10 --multi-tf

   # Sortie : output/explore/<TICKER>/<YYYY-MM-DD>_<TF>.png
   ```

   Le script stratifie automatiquement les jours par régime :
   `trending` / `ranging` / `macro` / `vol_h` / `vol_b` / `mixed`.

2. **Invoquer `@chartist`** en mode `idea` avec les charts générés :
   - Lui préciser le `MODE: idea`
   - Lui fournir la liste des PNG à analyser (max 10-15 jours, multi-TF =
     30-45 PNG max pour éviter dilution)
   - Lui demander 3-5 hypothèses d'edge testables

3. **Sauvegarder le rapport chartist** dans
   `output/ideation_<ticker>_<YYYY-MM-DD>.md`.

4. **Transmettre à `@researcher`** (PHASE 1) qui formalise rigoureusement
   les hypothèses retenues en pseudo-algo testable.

### Garde-fous

- Le chartist **propose** des hypothèses, il ne les **valide** pas. Une
  hypothèse "vue sur 10 charts" peut très bien échouer au backtest.
- Toute hypothèse retenue doit avoir un **edge théorique** énonçable par
  `@researcher` (pas juste "j'ai vu ça arriver 4 fois").
- Pas d'implémentation tant que la formalisation `@researcher` n'a pas
  identifié les pièges (look-ahead, timezone, curve-fitting).

### Sortie attendue (rapport chartist mode idea)

3-5 hypothèses d'edge structurées, chacune avec :
- description visuelle de l'observation
- conditions de setup traduisibles en pseudo-algo
- trigger d'entrée précis
- tickers candidats + fenêtre NY
- critère de falsifiabilité

> Cf. `.claude/agents/chartist.md` section "Mode `idea` — format" pour le
> détail du format de sortie.

Telegram (optionnel) :
```
🔍 <b>Idéation visuelle <TICKER></b>

Charts analysés : N (multi-TF : oui/non)
Hypothèses produites : H1, H2, H3

H<N> prioritaire : <résumé 1 ligne>

<i>Rapport : output/ideation_<ticker>_<date>.md</i>
→ Transmis à @researcher pour formalisation
```

---

## PHASE 1 · Analyse & conception

> Aucun fichier à créer dans cette phase.

1. Décrypte le concept : signal, timing, gestion du risque
2. **Énonce l'edge théorique** : pourquoi cet edge existe-t-il ? Qui paie ce P&L ? (sans réponse claire → drapeau rouge)
3. **Énonce une hypothèse falsifiable** : quelle observation invaliderait la stratégie en live ?
4. Identifie les risques : look-ahead, overfitting, volume de trades, régime-dépendance
5. Évalue la **complémentarité** avec OPR (v4/v5.1) / Fib v4 (heures, actifs, type de signal). Quantification en PHASE 6.
6. Décide de chaque indicateur — justifie période et pertinence
7. Propose tes **améliorations** par rapport à l'idée initiale
8. Fixe : `STRATEGY_ID` (format `concept-v1`) · tickers · fenêtre NY · `PARAM_GRID` initial (≤ 4 dimensions, granularité justifiée)
9. **Disponibilité des données** : pour chaque ticker retenu en (8), vérifier la présence du CSV.
   - Si CSV présent dans `data/{TICKER}_data_<tf>.csv` → noter la fenêtre couverte (`head -1` et `tail -1`).
   - Si CSV absent (nouvel actif ex: MGC, MCLE) → cf. PHASE 1.5 ci-dessous.

**→ Rédige un bloc `📋 ANALYSE INITIALE` avant le scaffold.**

---

## PHASE 1.5 · Fetch des données manquantes (si nouveaux actifs)

> Phase optionnelle, à exécuter UNIQUEMENT si un ticker demandé en PHASE 1 n'a pas son CSV dans `data/`.

1. **Découvrir** le mapping symbole ProjectX :
   ```bash
   python -m core.data_fetcher --list                 # alias raccourcis
   python -m core.data_fetcher --available            # catalogue ProjectX complet
   ```
2. **Fetcher** les données nécessaires (un contrat front-month a ~60-90 jours d'historique max — l'API renvoie 0 au-delà). Utilise un timeframe cohérent avec ta stratégie ; par défaut m15.
   ```bash
   python -m core.data_fetcher \
       --symbol  <SYMBOL>       \
       --timeframe <tf>         \
       --days    <N>            \
       --save    --ticker <TICKER_NAME>
   # Exemple : MGC en m15 sur 90 jours → data/MGC1_data_m15.csv
   ```
3. **Vérifier** la fenêtre obtenue et adapter le walk-forward :
   ```bash
   python -c "from core.data import load_csv; df=load_csv('data/<TICKER>_data_<tf>.csv'); print(df.index.min(), '→', df.index.max(), len(df), 'bars')"
   ```
   - Si l'historique est < 60 jours : **arrête le pipeline** et explique à l'utilisateur que l'actif n'a pas assez de données pour un backtest sérieux. Suggère d'attendre que le contrat ait mûri, ou de stitcher plusieurs échéances (hors scope V1).
   - Si l'historique est ≥ 60 jours : continue avec un walk-forward **adapté** :
     ```
     IS_END   = date à 60 % du chemin (ex: si data va du 2026-02-10 au 2026-05-11, IS_END = ~2026-04-08)
     OOS_START = IS_END + 1 jour
     ```
     À passer en CLI : `python optimize.py --strategy <id> --csv-dir ./data --is-end <IS_END> --oos-start <OOS_START>`.
4. **Documenter** dans le rapport final (PHASE 8 § 3) que :
   - Les données ont été fetchées via `core/data_fetcher.py` (préciser la date du fetch).
   - L'historique est plus court que celui de OPR/Fib v4 → robustesse statistique réduite.
   - Le verdict 🟢 nécessite alors `n_OOS ≥ 30` (plus exigeant que le défaut 20) pour compenser le sample size faible.

**→ Si fetch fait, mentionne-le explicitement dans le bloc `📋 ANALYSE INITIALE` :**
```
Données : <TICKER1> (CSV existant, sept 2024 → mai 2026)
         + <TICKER2> (fetché via data_fetcher le YYYY-MM-DD, fenêtre 2026-MM-DD → 2026-MM-DD, N bars)
Walk-forward adapté : IS=YYYY-MM-DD à YYYY-MM-DD | OOS=YYYY-MM-DD à YYYY-MM-DD
```

---

## PHASE 2 · Scaffold

Consulte [templates/strategy_template.md](templates/strategy_template.md) pour la structure complète.

**2a.** `strategies/<strategy_id>.py` — implémente `run_backtest()` et `plot_day()`.

**Schéma de colonnes obligatoire** (compatibilité `core/optimizer.py`) :
```
date, dir, entry, sl, tp, sl_dist, tp_dist, rr, n_ct,
result (TP|SL|TE|NOT_FILLED), pnl, fill_time, exit_time, exit, regime
```
- `pnl` = **P&L net** (slippage entrée+sortie + commissions intégrés)
- Colonnes optionnelles tolérées : `pnl_gross`, `adx`, `atr_pct`, `is_macro_day` (utilisées en PHASE 5)

Autres exigences :
- Fill conservatif (SL prioritaire si ambigu)
- `np.random.seed(42)` si tirage aléatoire
- `plot_day()` : chandeliers OHLC · indicateurs · flèches entrée/sortie · niveaux SL/TP · zone de setup colorée
- Conventions config.py : `RISK_PER_TRADE_USD`, `MAX_TRADES_PER_DAY` (anglais, exception à la convention française)

**2b.** `backtest.py` et `optimize.py` — ajoute `"<strategy_id>": "strategies.<strategy_id>"` dans `REGISTRY` (les **deux** fichiers).

**2c.** `config.py` — ajoute une section `# STRATÉGIE <NOM>` avec tous les paramètres, dont `<STRATEGY_ID>_STRATEGY_VERSION`.

> Si des charts portfolio sont nouveaux (equity curve, DD underwater, monthly heatmap, hourly distribution), proposer leur ajout dans `core/backtester.py` plutôt que dans la stratégie — mais **demander confirmation** avant de modifier `core/`.

---

## PHASE 2.5 · Feature catalog par @quant (optionnel, recommandé)

> Phase d'enrichissement proactif. À invoquer **avant** que le scaffold de
> stratégie soit finalisé, pour que `@quant` propose des features candidates
> alignées sur le concept (regime, timing, microstructure, cross-asset).
> Phase **optionnelle** par défaut, **recommandée** dès que le concept
> repose sur des conditions de marché conditionnelles (filtre de régime,
> heure, volatilité, microstructure).

### Quand l'invoquer
- Concept impliquant du filtrage conditionnel (ex: "uniquement quand ADX > 25")
- Stratégie qui pourrait bénéficier de features pas évidentes (timing, cross-asset)
- Concept neuf sans héritage de stratégie prod (pas un simple variant de OPR/Fib)

### Workflow

1. **Invoquer `@quant` en mode `catalog`** avec :
   - Le concept formalisé par `@researcher` (bloc CONCEPT complet)
   - Le `STRATEGY_ID` cible et les tickers visés

2. **Le @quant propose 5-10 features candidates** classées en :
   - **Must-have** : à intégrer dès le scaffold v1 dans `strategies/<id>.py` (max 3)
   - **Nice-to-have** : à tester en PHASE 3.5 sur trades baseline

3. **Sortie sauvegardée** dans `output/<strategy_id>/quant_catalog.md`.

4. **Le scaffold (PHASE 2)** intègre les features must-have. Les nice-to-have
   restent disponibles pour la PHASE 3.5 (discovery).

### Garde-fous

- Le @quant **propose** ; @new-strategy **applique**. Pas d'écriture autonome.
- Features must-have : maximum 3 pour ne pas alourdir le scaffold v1.
- Si data insuffisante (NYSE TICK, options) : marquer "DATA UNAVAILABLE",
  ne pas tenter.
- Chaque feature doit être calculable sans look-ahead par construction
  (`df.iloc[:i]` ou `.shift(1)`).

> Cf. `.claude/agents/quant.md` section "Mode `catalog`" pour le détail.

---

## PHASE 3 · Backtest initial (sans charts)

```bash
python backtest.py --strategy <strategy_id> --csv-dir ./data
```

Analyse par ticker et portfolio :
- Volume · WR · PF · **Sharpe annualisé** · DD vs limites Topstep
- **Max conséqs perdants** (suite la plus longue de pertes)
- **Distribution horaire** des entrées (concentration excessive = signal régime-dépendant)
- Cohérence avec le concept · signaux d'overfitting

**→ Rédige un bloc `📊 ANALYSE BACKTEST` avec ton diagnostic.**

Telegram :
```
📊 <b>Backtest <STRATEGY_ID> · Itération N</b>

IS Portfolio :
• P&amp;L net : +$X XXX  |  PF : X.XX  |  WR : XX%
• Trades : XXX  |  DD : -$XXX  |  Sharpe : X.X

→ Lancement optimisation...
```

---

## PHASE 3.5 · Feature discovery par @quant (optionnel, recommandé si baseline 🟡)

> Phase d'analyse data-driven post-baseline. À invoquer **après le backtest
> initial v1** pour découvrir des filtres significatifs (sklearn RF importance,
> permutation tests, grid univarié) qui auraient échappé à l'œil humain.
> **Particulièrement utile si baseline = 🟡 borderline** — un filtre data-driven
> peut faire basculer en 🟢.

### Quand l'invoquer

- **Recommandé** : baseline v1 = 🟡 (PF OOS entre 1.2 et 1.5)
- **Optionnel** : baseline v1 = 🟢 mais avec amélioration possible (PF entre 1.5 et 1.8)
- **Sauter** : baseline v1 = 🔴 dur (PF < 1.0) — le problème est structurel, pas un filtre

### Pré-requis

- `n_oos_portfolio ≥ 100` trades (sinon @quant refusera)
- Trades baseline sauvegardés dans `output/<strategy_id>/full/trades_v1.csv`

### Workflow

1. **Invoquer `@quant` en mode `discover`** avec :
   - Le chemin `output/<strategy_id>/full/trades_v1.csv`
   - Les CSV data `data/<TICKER>_data_m15.csv`
   - Le résumé du concept (issu de PHASE 1)
   - Optionnel : `output/<strategy_id>/quant_catalog.md` si PHASE 2.5 exécutée

2. **Le @quant exécute** :
   - Feature engineering (no look-ahead vérifié)
   - Analyse univariée par quantile sur chaque feature continue
   - RandomForest + LogisticRegression avec **TimeSeriesSplit** (jamais KFold)
   - Permutation test 1000 itérations sur top-5 features
   - **Correction Bonferroni** sur N features testées (seuil `p < 0.05/N`)
   - Validation walk-forward du seuil retenu (impact PF OOS séparé IS/OOS)

3. **Output** :
   - `output/<strategy_id>/quant_report.md` (max 80 lignes, 1 page)
   - `output/<strategy_id>/quant_patch.py` (patch Python prêt-à-coller, avec `# ANCHOR`)

4. **Application du patch** :
   - `@new-strategy` lit `quant_patch.py` et l'intègre dans v_next
     (`strategies/<strategy_id>.py` → bumper `STRATEGY_VERSION` à `vN+1`)
   - Le filtre ajouté doit avoir passé Bonferroni ET amélioré PF OOS ≥ +0.2

### Garde-fous

- @quant **propose** un patch ; @new-strategy **applique**. Pas d'écriture
  autonome de `strategies/<id>_vN+1.py` par @quant.
- Multiple-testing : tout seuil retenu doit passer `p < 0.05 / N_features_tested`.
- No look-ahead : `@auditor` en PHASE post-8 vérifiera ligne par ligne le patch
  (`output/<strategy_id>/quant_patch.py`).
- Si verdict @quant = **LOW** (aucun filtre ne passe Bonferroni) : ne pas
  appliquer le patch, documenter dans le rapport final, considérer arrêt.

> Cf. `.claude/agents/quant.md` section "Mode `discover`" pour le détail.

Telegram (optionnel) :
```
🔬 <b>Quant discover <STRATEGY_ID></b>

Features testées : N  |  Bonferroni : p &lt; X.XXX
Filtres retenus  : N (impact PF OOS +X.XX)

Verdict quant : HIGH / MEDIUM / LOW
<i>Patch : output/&lt;strategy_id&gt;/quant_patch.py</i>
```

---

## PHASE 4 · Optimisation rigoureuse

Avant de lancer : analyse l'impact attendu de chaque paramètre. Ajuste le `PARAM_GRID` si nécessaire (bornes, granularité). Justifie chaque modification.

```bash
python optimize.py --strategy <strategy_id> --csv-dir ./data
```

`core/optimizer.py` produit le rapport de base (verdict, bootstrap, dégradation). Compléter avec :

### Analyse OOS approfondie
- **Stabilité des paramètres optimaux** : zone autour du pic doit garder PF ≥ 1.3 sur ≥ 50 % du voisinage immédiat. Pic isolé = fragile.
- **Block bootstrap OOS** : taille bloc ≈ √N, 1000 itérations (essentiel pour séries temporelles)
- **Volume OOS représentatif** : ≥ 20 trades par ticker, ≥ 50 portfolio

### Correction du multiple testing
Si N configurations testées :
- **Méthode simple** : Bonferroni → `p_seuil = 0.05 / N`. Si bootstrap < 1 - p_seuil → rejette.
- **Méthode robuste (préférée si N > 20)** : White's Reality Check ou SPA test sur les rendements.
- **Probabilistic Sharpe Ratio PSR(0)** : probabilité que le vrai Sharpe > 0.

**→ Rédige un bloc `⚙️ ANALYSE OPTIMISATION` avec ton diagnostic.**

Telegram :
```
⚙️ <b>Optimisation <STRATEGY_ID> · Itération N</b>

OOS :
• PF : X.XX  |  P&amp;L net : +$X XXX
• Trades : XX  |  Bootstrap : XX%  |  DD : -$XXX
• Dégradation IS→OOS : XX%  |  PSR(0) : XX%

Verdict préliminaire : 🟢/🟡/🔴
```

---

## PHASE 5 · Stress tests & Monte Carlo

> Phase obligatoire avant verdict. Une stratégie qui passe le bootstrap mais casse en stress = rejet.

### 5a. Stress par régime
Calcule séparément le PF OOS sur :
1. **Trending** : ADX(14) > 25 sur les 4 dernières barres avant entrée
2. **Ranging** : ADX(14) < 20
3. **Volatilité haute** : ATR sur percentile > 75 du mois
4. **Volatilité basse** : ATR sur percentile < 25 du mois
5. **Jours macro** : FOMC, CPI, NFP, JOLTS (liste maintenue dans `config.py` : `MACRO_EVENT_DATES`)

Cible : PF ≥ 1.0 sur **chaque** régime, PF ≥ 1.3 sur ≥ 2 régimes. Sinon → 🟡 max.

### 5b. Monte Carlo permutation
```python
# 1000 permutations de l'ordre des trades OOS
# Pour chaque : calcul de la courbe d'equity et du DD max
# Rapporter : DD median, P95, P99
```
Cible : **DD P95 < 80 % de la limite Topstep restante** (compte tenu du DD déjà consommé en prod).

### 5c. Worst-case clustering
Identifie les 20 pires trades OOS. Concentration sur :
- Une période courte (≤ 2 semaines) ?
- Un actif unique ?
- Un type de jour (lundi, jour macro) ?

Concentration = signal de régime-dépendance non capturé.

**→ Rédige un bloc `🔥 STRESS TESTS` avec ton diagnostic.**

---

## PHASE 5.5 · Live-equivalent backtest (CONDITIONNELLE — BLOCANTE si applicable)

> Phase déclenchée **automatiquement** si la stratégie produit des features
> dérivées de la barre de fill (mèche, MAE intra-bar, MFE intra-bar, range
> de la bougie de fill, etc.). Skip si toutes les features dépendent uniquement
> des barres antérieures au fill.

### Pourquoi cette phase ?

Toute feature lue sur la bougie où le fill se produit est **non-observable en
live à l'instant où l'ordre limite est exécuté broker-side**. Le PF backtest
naïf qui utilise cette feature comme filtre est un **upper bound théorique**,
pas un PF live atteignable.

**Cas d'école projet** :
- OPR v5.1 — feature `F2` (excursion durant pending) → `scripts/live_eq_v5_1.py`
- fib-v4 (2026-05-19) — feature `wick_through_atr` → ce qui a déclenché l'écriture de cette phase

### Inventaire des features à risque

Pour chaque feature du DataFrame de trades, te poser la question :
> "Au moment précis où le broker fille mon ordre limite intra-bar M15, est-ce
> que je connais déjà la valeur de cette feature ?"

Si la réponse est **non** (la feature dépend du low/high/close FINAL de la
bougie de fill ou de toute barre future), la feature est à risque.

### Path A — Wirage `M1Buffer` (préféré)

Si `broker/m1_buffer.py` et `broker/projectx_market_realtime.py` sont disponibles
(Phase C broker, commit ≥ 2026-05-18) :

1. **Vérifier que `M1Buffer` est consommé par le live runner pour ce ticker**
   (cf. `live_runner.py:1050-1102` pour activation, `live_runner.py:470-481`
   pour pattern OPR).
2. **Écrire `get_<strategy_id>_live_signal(df_15m, ticker, m1_buffer, contract_id)`** qui :
   - Détecte les pendings et positions sur les bars M15 closes
   - Pour la barre M15 EN COURS, consulte `m1_buffer.get_current_forming_bar`
     + les M1 closes appartenant à cette M15 pour reconstruire le
     low/high COURANT
   - Évalue les features à risque en utilisant ce low/high courant
   - Retourne CANCEL si la feature franchit le seuil pendant pending
3. **Tester la cohérence backtest ↔ live signal** : sur un ticker, sur 1
   mois de données, faire tourner les deux et vérifier que les décisions
   convergent à ±2 barres M15 près.

Latence attendue : ~1 minute (granularité M1Buffer). Acceptable.

### Path B — Live-equivalent backtest (fallback si pas de M1Buffer)

Si l'infrastructure tick n'est pas disponible pour ce ticker :

1. **Créer `scripts/live_eq_<strategy_id>.py`** sur le modèle
   `scripts/live_eq_v5_1.py`.
2. La logique : décaler la décision de filtre à la barre M15 SUIVANTE
   (n'utilise pas la bougie de fill elle-même).
3. **Re-mesurer les métriques OOS avec ce mode dégradé.** Le PF live-eq est
   typiquement 50-90 % du PF backtest naïf selon la feature.
4. **Le verdict 🟢/🟡/🔴 doit être recalculé sur les métriques live-eq**,
   pas sur les métriques backtest naïves.

### Sortie obligatoire

Inclure dans `output/<id>/summary.json` :
```json
"live_equivalence": {
  "applicable": true,
  "path": "M1Buffer" | "live_eq_script" | "n/a",
  "live_eq_pf_oos": <float ou null>,
  "live_eq_pnl_oos": <float ou null>,
  "live_eq_n_oos": <int ou null>,
  "expected_degradation_pct": <float ou null>,
  "live_signal_function": "core/strategy_<id>.py::get_<id>_live_signal" | null
}
```

Sans ce bloc renseigné quand `applicable=true`, l'auditor **rétrograde
systématiquement** le verdict 🟢→🟡 (cf. `.claude/agents/auditor.md` §2-bis).

---

## PHASE 6 · Génération des charts

```bash
python backtest.py --strategy <strategy_id> --csv-dir ./data --plot --n-charts 10
```

Charts obligatoires (les charts portfolio idéalement dans `core/backtester.py`) :

1. **Equity curve cumulée** (par ticker + portfolio) avec marquage IS/OOS
2. **Drawdown underwater** (par portfolio)
3. **Monthly P&L heatmap** (lignes = mois, colonnes = actifs)
4. **Distribution horaire des entrées** (histogramme par heure NY)
5. **10 day charts** : 5 jours gagnants + 5 jours perdants (pas que les meilleurs)
6. **Corrélation rolling 60j** entre P&L daily de la nouvelle stratégie et OPR/Fib

Si `plot_day()` est incomplet, corrige-le avant de continuer.

---

## PHASE 6.5 · Audit visuel des charts (conditionnel)

> Phase qualitative complémentaire au verdict statistique. Le chartist
> alimente `@auditor` en warnings visuels — il ne décide PAS du verdict.

### Règle de skip (économie ~80-100k tokens sur cycles 🟢 clairs)

| Configuration verdict statistique | Action PHASE 6.5 |
|---|---|
| **🟢 clair** : bootstrap OOS ≥ 80 % **ET** PF OOS ≥ 1.5 | **SAUTER** — verdict statistique suffisant |
| **🟢 borderline** : bootstrap 80-85 % **ou** PF OOS 1.5-1.6 | Exécuter (audit confirmatoire utile) |
| **🟡** : tout verdict 🟡 | Exécuter (audit visuel peut basculer en 🟢 ou rejeter 🟡 → 🔴) |
| **🔴** | **SAUTER** — inutile d'auditer un rejet |

En cas de skip, écrire dans `output/<strategy_id>/summary.json` :
```json
"skip_chartist_audit": true,
"skip_reason": "verdict 🟢 clair : bootstrap=XX% PF_oos=X.XX"
```

### Quand exécuter

L'audit visuel reste utile pour révéler des biais invisibles aux métriques :
TP sur wick, entrées chasing, J+1 macro non filtré. Mais ces biais sont rares
en pratique quand le bootstrap est solide et le PF élevé — c'est pourquoi
on skip sur les 🟢 clairs.

1. **Vérifier les artefacts** générés en PHASE 6 :
   - `output/<strategy_id>/full/charts/<TICKER>/YYYY-MM-DD.png` (5 winners + 5 losers idéalement)
   - `output/<strategy_id>/full/charts/equity_curve.png` (si fourni)
   - `output/<strategy_id>/full/charts/monthly_heatmap.png` (si fourni)

2. **Invoquer `@chartist`** en mode audit visuel, lui fournir :
   - La liste des PNG charts à analyser (10 max recommandé pour éviter dilution)
   - Le chemin du rapport compact `output/<strategy_id>/rapport.md` (contexte stratégie)
   - L'`STRATEGY_ID` et la période couverte

3. **Sauvegarder le rapport chartist** dans `output/<strategy_id>/full/audit_visuel.md`
   pour qu'`@auditor` puisse le lire en PHASE post-8.

4. **Le chartist ne décide PAS du verdict.** Ses warnings sont transmis à
   `@auditor`, qui peut s'en servir pour rétrograder un verdict (🟢→🟡)
   si plusieurs warnings convergents — mais l'autorité finale reste à
   `@auditor`.

> **Rappel garde-fou** : ne JAMAIS rétrograder un verdict 🟢→🟡 sur la
> seule base du chartist. Le verdict statistique reste maître ; le chartist
> documente les risques qualitatifs et l'auditor arbitre.

Telegram (optionnel) :
```
🎨 <b>Audit visuel <STRATEGY_ID></b>

Charts analysés : N
Warnings :
• <warning 1 court>
• <warning 2 court>

Réalisme fills : X/5  |  Edge visible : X/5
<i>Rapport : output/&lt;strategy_id&gt;/full/audit_visuel.md</i>
```

---

## PHASE 7 · Décision & itération

**Verdict 🟢 ou 🟡** → PHASE 8.

**Verdict 🔴** :
1. Cause : paramètres (corrigeable) ou concept non viable (fatal)
2. Si **fatal** → arrêt, explique pourquoi dans le rapport, PHASE 8
3. Si **corrigeable** → modifie (`<strat>-v2`, `<strat>-v3`…), retour PHASE 2, max 5 itérations total

Critère d'arrêt anticipé : si après v3, PF OOS reste < 1.2 ou bootstrap < 50 %, arrête. L'itération supplémentaire risque le p-hacking.

---

## PHASE 8 · Rapport final

Nouveau format à 3 niveaux sous `output/<strategy_id>/` :

```
output/<strategy_id>/
  summary.json          ← lu par Athena/Auditor — verdict + métriques structurées
  rapport.md            ← résumé 1 page (max 80 lignes) — archivable humain
  quant_report.md       ← (si PHASE 3.5 exécutée) rapport @quant
  quant_patch.py        ← (si PHASE 3.5 exécutée) patch appliqué à vN+1
  full/
    robustness.json     ← détail bootstrap, Bonferroni, PSR, MC, stress
    audit_visuel.md     ← (si PHASE 6.5 exécutée) warnings chartist
    charts/             ← 122 PNG (lus uniquement par @chartist si invoqué)
    trades_v1.csv       ← trades baseline (input @quant)
    trades_final.csv    ← trades version retenue
    features_v1.csv     ← (si PHASE 3.5) features calculées par @quant
```

### Schéma `summary.json` (obligatoire)

```json
{
  "strategy_id": "<id>",
  "version": "<vN>",
  "iterations": <int>,
  "verdict": "🟢" | "🟡" | "🔴",
  "skip_chartist_audit": <bool>,
  "skip_reason": "<text si skip>",
  "oos": {"pf": <float>, "pl_net": <int>, "n": <int>, "bootstrap": <pct>, "dd": <float>, "wr_pct": <float>},
  "is": {"pf": <float>, "pl_net": <int>, "n": <int>},
  "degradation_is_oos_pct": <float>,
  "stress": {"trending": <pf>, "ranging": <pf>, "macro": <pf>, "vol_h": <pf>, "vol_b": <pf>},
  "robustness": {"bonferroni_ok": <bool>, "psr_0": <float>, "mc_dd_p95": <float>},
  "quant_used": <bool>,
  "quant_verdict": "HIGH" | "MEDIUM" | "LOW" | null,
  "quant_filters_applied": [<list de strings>],
  "audit_warnings_count": <int>,
  "next_step": "<promotion | itération | rejet>"
}
```

### Rapport `rapport.md` (~80 lignes max)

Consulte [templates/rapport_template.md](templates/rapport_template.md) pour le format compact.
Doit tenir en 1 page de lecture humaine. Verdict, top métriques OOS, top 3 filtres
quant (si utilisé), 3 lignes par bloc majeur. Détails complets dans `full/`.

### Compatibilité ascendante

Les rapports existants (`output/rapport_opr-v5.md`, etc.) **ne sont pas migrés**.
Seules les futures stratégies utilisent ce nouveau format `output/<id>/`. Auditor
et Athena gèrent les deux formats pendant la transition (lecture prioritaire
`summary.json` si présent, sinon fallback sur ancien `rapport_<id>.md`).

### Workflow de promotion (si 🟢 — DEMANDER CONFIRMATION avant exécution)

La promotion modifie `core/` et `broker/` — **toujours demander à l'utilisateur** avant :

1. Créer `core/<strategy_id>.py` (logique d'exécution live, fonction `get_<strategy>_live_signal()`)
2. Mettre à jour `broker/live_runner.py` (imports + boucle de session)
3. Mettre à jour `core/signal_selector.py` si actifs spécifiques
4. Tester en simulation (`PROJECTX_LIVE_MODE = False`)
5. Activation progressive : 1 contrat 1 semaine → sizing nominal

Telegram final :
```
[🟢/🟡/🔴] <b>Analyse <STRATEGY_ID> terminée — N itération(s)</b>

OOS · PF X.XX · P&amp;L net +$X XXX · Bootstrap XX%
Stress : trending X.XX | ranging X.XX | macro X.XX
MC P95 DD : -$XXX  |  Corr OPR/Fib : 0.XX
<i>Rapport : output/&lt;strategy_id&gt;/rapport.md</i>
```
