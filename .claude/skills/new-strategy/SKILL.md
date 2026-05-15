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
!`grep -E "(OPR|FIB|VPC)_STRATEGY_VERSION" config.py`

Données CSV (TradingView, historique long) :
!`ls data/`

Stratégies découvertes (plug-and-play) :
!`python -c "from core.registry import list_strategy_names; print(list_strategy_names())"`

État live :
!`cat state/live_state.json 2>/dev/null | head -30 || echo "Pas de session live active"`

## Benchmark production

| Stratégie  | P&L OOS    | DD    | Bootstrap | Actifs        |
|------------|------------|-------|-----------|---------------|
| OPR opr-v4 | +$22 574   | -$822 | 100 %     | MES1·NQ1·YM1  |
| Fib fib-v3 | +$10 805   | -$672 | 100 %     | MES1·NQ1·YM1  |
| VPC vpc-v4 | +$2 183    | -$1 376 (MC P95) | 99.5 % (stationnaire) | MES1·NQ1 |

**Contraintes Topstep :** daily loss −$1 000 · trailing DD −$2 000 · target +$3 000
**Walk-forward sur actifs standards (dates fixes) :** IS = déc 2024 → 2025-09-30 | OOS = 2025-10-01 → mars 2026
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
| Stratégie sur MES1/NQ1/YM1 uniquement | CSV existants `data/{TICKER}_data_m15.csv` (TradingView, déc 2024 → mars 2026) | Historique long (~16 mois) → walk-forward IS/OOS aux dates fixes |
| Stratégie sur **autre actif** (MGC, MCLE, MBT, M2K…) | `python -m core.data_fetcher --symbol <ALIAS> --timeframe m15 --days <N> --save --ticker <NEW_TICKER>` | API ProjectX limitée à 20 000 bars/req + un contrat = une échéance (~2-3 mois d'historique max) |
| Stratégie nécessitant un autre timeframe (m5, h1, h4) sur actifs standards | `python -m core.data_fetcher --symbol MES --timeframe h1 --days 365 --save --ticker MES1` (écrit `data/MES1_data_h1.csv`) | CSV TradingView sont m15 seulement |
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
5. Évalue la **complémentarité** avec OPR / Fib / VPC (heures, actifs, type de signal). Quantification en PHASE 6.
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
   - L'historique est plus court que celui de OPR/Fib/VPC → robustesse statistique réduite.
   - Le verdict 🟢 nécessite alors `n_OOS ≥ 30` (plus exigeant que le défaut 20) pour compenser le sample size faible.

**→ Si fetch fait, mentionne-le explicitement dans le bloc `📋 ANALYSE INITIALE` :**
```
Données : <TICKER1> (CSV existant, déc 2024 → mars 2026)
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

## PHASE 6.5 · Audit visuel des charts (optionnel, recommandé)

> Phase qualitative complémentaire au verdict statistique. Le chartist
> alimente `@auditor` en warnings visuels — il ne décide PAS du verdict.
> Phase **optionnelle** par défaut, **recommandée** dès qu'un verdict
> 🟢 est revendiqué (l'audit visuel peut révéler des biais invisibles
> aux métriques : TP sur wick, entrées chasing, J+1 macro non filtré).

1. **Vérifier les artefacts** générés en PHASE 6 :
   - `output/charts/<TICKER>/YYYY-MM-DD.png` (5 winners + 5 losers idéalement)
   - `output/equity_curve_<strategy_id>.png` (si fourni)
   - `output/monthly_heatmap_<strategy_id>.png` (si fourni)

2. **Invoquer `@chartist`** en mode audit visuel, lui fournir :
   - La liste des PNG charts à analyser (10 max recommandé pour éviter dilution)
   - Le chemin du rapport long `output/rapport_<strategy_id>.md` (contexte stratégie)
   - L'`STRATEGY_ID` et la période couverte

3. **Sauvegarder le rapport chartist** dans `output/audit_visuel_<strategy_id>.md`
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
<i>Rapport : output/audit_visuel_<strategy_id>.md</i>
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

Consulte [templates/rapport_template.md](templates/rapport_template.md) pour le format.
Crée `output/rapport_<strategy_id>.md`.

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
<i>Rapport : output/rapport_<strategy_id>.md</i>
```
