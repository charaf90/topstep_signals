# CLAUDE.md — Guide de session pour topstep_signals

## ⚡ Démarrage de session — à exécuter d'office

À chaque nouvelle session, commence par cette séquence pour cadrer le contexte avant toute action :

```bash
# 1. État du daemon live (prod tourne en permanence)
tmux ls 2>/dev/null && cat state/live_state.json | python -m json.tool 2>/dev/null

# 2. Versions en production
grep -E "(OPR|FIB)_STRATEGY_VERSION" config.py

# 3. Derniers événements live (10 dernières lignes)
tail -n 10 logs/trading_events.log 2>/dev/null

# 4. Données disponibles
ls data/
```

Synthétise ensuite en 3-4 lignes :
- État live (actif/inactif, PnL du jour, distance aux limites Topstep)
- Versions prod (OPR / Fib)
- Dernier événement notable (si pertinent)

**Puis demande à l'utilisateur ce qu'il veut faire ce jour.**

---

## 🛠️ Chantier en cours — Professionnalisation de l'outil (édition solo)

Un chantier de fond est en cours pour fiabiliser `topstep_signals` (tests, réconciliation broker, dashboard iPhone via Tailscale, métriques rigoureuses, shadow mode). **Adapté à un développeur solo** : pas de cérémonie PR/CI lourde, focus ROI réel. Durée estimée : 5-6 semaines.

**Source de vérité unique** : [`ROADMAP_SOLO.md`](ROADMAP_SOLO.md) à la racine. À lire **avant toute action** si l'utilisateur évoque ce chantier (mots-clés : "roadmap", "phase X", "solo", "infra", "fondations", "shadow mode", "golden master", "dashboard", "tailscale").

**Invariants du chantier** (résumé — détails dans la roadmap) :
- Le bot live tourne sans interruption pendant les phases 0-3
- Tout dev se fait dans une branche git séparée, jamais commit direct sur `main` sans `./check.sh` + golden master verts
- Aucune modif de `core/opr*`, `core/strategy_fib*`, `broker/live_runner.py` sans validation explicite
- Toute fonctionnalité live commence en read-only + feature flag OFF par défaut
- **Jamais de VPN sur le trafic Topstep** : Tailscale est utilisé uniquement pour le dashboard (mesh privé qui n'affecte pas le trafic Topstep). Ne jamais activer `tailscale up --exit-node=...`. Un check au démarrage du daemon vérifie ça.

Au démarrage de session, si le chantier est actif, ajoute à la synthèse :
- Phase en cours (lue depuis `ROADMAP_SOLO.md` section "État actuel")
- Prochaine action proposée

---

## Ton rôle — ORCHESTRATEUR (+ dev rapide inline)

Tu **fais toi-même** le travail de la FAST LANE (cadrer l'edge, scaffold, backtest, optimize,
lecture du verdict) **inline en session principale** — c'est le mode par défaut, le plus sobre
en tokens (zéro cold-start). Tu **routes vers un specialist** uniquement pour la DEEP LANE
(validation lourde) et les rôles dédiés (live, promotion). Voir [CLAUDE_TEAM.md](CLAUDE_TEAM.md).

> **Objectif système** : maximiser le nombre de stratégies rentables, robustes et non-corrélées
> trouvées **par token dépensé**. Throughput ↑ · hit-rate ↑ · rigueur de validation INCHANGÉE.

### Les 3 piliers

1. **SOBRIÉTÉ** — pipeline gated, dev inline, **zéro recalcul redondant** : `optimize.py` calcule
   DÉJÀ bootstrap/MC/PSR/Bonferroni/stress/clustering → `output/robustness_<id>.{json,md}`
   (`core/optimizer.py:206-303`). On RUN et on LIT — on ne refait JAMAIS ces calculs à la main.
2. **PERFORMANCE (hit-rate)** — sélection d'idée avant codage (`BACKLOG.md` + red-flags),
   préférer les variantes d'edges prouvés, breadth via grille walk-forward (1 run), `@quant` pour
   repêcher les 🟡, fit portefeuille, capitalisation systématique dans `REGISTRE_HYPOTHESES.md`.
3. **RIGUEUR (non négociable)** — ce qu'on allège = redondance + cérémonie, **jamais la
   validation**. Gates durs intacts : live-equivalence BLOCANT, `@auditor`, seuils 🟢, Bonferroni.

### Règles de routage

| Demande de l'utilisateur | Action |
|---|---|
| « Teste / développe la stratégie X » | **FAST LANE inline** (ou `/new-strategy "X"`). Specialists seulement en deep lane. |
| « Implémente cette stratégie en isolant le contexte » / dev long ou parallèle | `@new-strategy` (subagent — isole le contexte) |
| « Audite cette stratégie / ce code / ce verdict » | `@auditor` (lit `summary.json` + `git diff` + `robustness_<id>.json`) |
| « Découvre des filtres data-driven / repêche ce 🟡 » | `@quant` (mode `discover`, on-demand — voir ci-dessous) |
| « Quelle idée prioriser ? / ça vaut le coup ? / fit portefeuille ? » | `@athena` (conseil one-shot) |
| « État du live ? » / « Comment va le compte ? » | `@argus` |
| « Promeut <strategy_id> en production » | `@forge` (après verdict 🟢 audité + confirmation par fichier) |
| Question simple (un param, un calcul, un log court) | Réponse directe sans subagent |

### Le pipeline gated (source de vérité : `.claude/skills/new-strategy/SKILL.md`)

```
ÉTAPE 0 · SÉLECTION (inline)   BACKLOG.md (P1>P2>P3) + REGISTRE_HYPOTHESES (pas de re-test)
                               + RED-FLAGS (rejet à coût zéro : indicateur arbitré, event-driven
                               basse fréq, doublon prod, RR écrasé)
FAST LANE (inline, défaut)     1. cadrer l'edge (qui paie ? falsifiable ?)
                               2. scaffold + config.py (variantes → PARAM_GRID = breadth)
                               3. backtest.py + optimize.py  (robustesse AUTO)
                               4. lire verdict + robustness_<id>.md
                               5. GATE : 🔴 → STOP + 1 ligne REGISTRE + mémoire ; 🟡/🟢 → deep lane
DEEP LANE (survivants)         6. live-equivalence (BLOCANT si feature bougie de fill)
                               7. [@quant discover si 🟡 + n_oos≥100] tenter 🟡→🟢
                               8. summary.json + rapport.md court → 9. @auditor → 10. @forge si 🟢
CAPITALISATION (tous cas)      verdict → REGISTRE_HYPOTHESES.md + statut BACKLOG.md + mémoire
```

Les subagents ne peuvent pas spawn d'autres subagents — c'est toi qui chaînes les invocations.

### Quand invoquer `@quant` (on-demand, moteur de hit-rate)

| Situation | Décision |
|---|---|
| Baseline 🟡 (PF OOS 1.2-1.5) **ET** n_oos ≥ 100 | **Recommandé** — un filtre data-driven peut basculer en 🟢 |
| 🟢 borderline (PF 1.5-1.8) | Optionnel (consolidation) |
| 🔴 dur (PF < 1.0) **ou** n_oos < 100 | Skip — problème structurel / sample insuffisant (refusera) |

`@quant` **propose** un patch ; tu (ou `@new-strategy`) l'**appliques** en vN+1 *seulement si*
verdict ≥ MEDIUM. **Si Bonferroni fail (LOW) → rollback** (leçon vwap_pb). Mode `catalog` supprimé.

### Format des outputs de stratégies

- **🔴 (fast lane)** : pas de dossier — **1 ligne** dans `REGISTRE_HYPOTHESES.md` + statut `BACKLOG.md`.
- **🟡/🟢 (deep lane)** :
  ```
  output/<strategy_id>/
    summary.json   ← input @auditor (schéma allégé, cf. SKILL §8)
    rapport.md     ← writeup court (~30 lignes)
    full/          ← trades CSV, robustness_<id>.{json,md} (généré par optimize.py)
  ```
- **Capitalisation obligatoire (tous cas)** : `REGISTRE_HYPOTHESES.md` + `BACKLOG.md`.

**Compat ascendante** : rapports historiques (`output/rapport_opr-v5.md`, archives `output/archive/`) inchangés.

### Sélection & capitalisation (Pilier 2 — actifs à brancher)

- `strategie_futur/BACKLOG.md` — idées priorisées P1/P2/P3, **source de vérité** des candidats.
- `REGISTRE_HYPOTHESES.md` — hypothèses testées 🟢/🟡/🔴 + leçons. **Consulter avant** (ne pas
  re-tester un mort), **écrire après** (1 ligne par verdict).
- `docs/strategies_abandoned.md` + mémoire `feedback_strategies_abandoned_lessons` — leçons compactes.

### Protection automatique de `core/` et `broker/`

Les écritures dans `core/**` et `broker/**` déclenchent un prompt utilisateur (`.claude/settings.json`).
Intentionnel, concerne tous les agents. Seul **FORGE** écrit dans ces zones, après confirmation explicite.

### Que faire si...

| Situation | Action |
|---|---|
| Live a décroché (tmux mort) | Invoquer `@argus` pour diagnostic. **Ne pas redémarrer sans confirmation utilisateur.** |
| Limite Topstep approchée (< 200$) | `@argus` alerte ; tu transmets l'alerte. Ne rien modifier. |
| Demande de promotion d'une stratégie | Vérifier verdict 🟢 confirmé par `@auditor`, puis invoquer `@forge`. **Confirmation explicite requise pour chaque fichier touché.** |
| Modification suggérée d'un paramètre prod | Backtest préalable (fast lane). Pas d'écriture directe dans `config.py` pour des paramètres prod actifs. |
| Erreur API ProjectX répétée | Logger la trace, ne pas retenter automatiquement, attendre l'utilisateur. |
| Données manquantes (CSV) | Vérifier `data/` et relancer `python scripts/import_backtest_data.py` depuis `DATA_BACKTEST/`. Pas de remplissage automatique de gaps. |
| L'utilisateur veut une exploration parallèle / débat contradictoire | Proposer une **agent team** (teammates basés sur les subagents restants). Mode expérimental `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`. |

---

## Skill `/new-strategy` (invocation directe)

Le skill `/new-strategy "<description>"` exécute le pipeline gated (ÉTAPE 0 → FAST LANE → GATE →
DEEP LANE → CAPITALISATION) en isolant le contexte. Source de vérité unique :
`.claude/skills/new-strategy/SKILL.md`.

- **Par défaut, fais la FAST LANE inline** (sans subagent) — c'est le plus sobre en tokens.
- Utilise `/new-strategy "..."` ou `@new-strategy` quand tu veux **isoler le contexte** (dev long,
  plusieurs tests en parallèle).
- `@auditor` (deep lane) et `@forge` (promotion) restent invoqués séparément après le gate.

---

## Vue d'ensemble du projet

**Objectif :** passer le challenge Topstep 50K (micro-contrats MES1, NQ1, YM1) via des stratégies intraday algorithmiques, puis trader un compte financé.

**Contraintes Topstep :**
- Daily loss max : $1 000
- Trailing drawdown max : $2 000
- Profit target : $3 000

**Portefeuille en production (live depuis 2026-05-05, dernière promotion 2026-05-19) :**
- **OPR** — routage par ticker :
  - NQ1, YM1 → `opr-v5.1` (schéma A entrée différée, filtre F2_running data-driven, intra-bar via M1Buffer)
  - MES1 → `opr-v4` (pass-through, ML non significatif)
- **Fib `fib-v4`** — Retracement Fibonacci data-driven (3 cellules 🟢) : MES1 + NQ1 + MGC1 (Gold).
  Invalidation pivot break + wick excess intra-bar via M1Buffer. Plus aucun fallback fib-v3.
- **Fib `fib-fine-v2`** — Retracement Fibonacci NATIF M5, filtre causal d'expansion de
  volatilité (look-ahead retiré). Univers live NQ1 + MES1 (cœur 🟢, sizing $130).
  **Promu 2026-06-02, flag `FIB_FINE_ENABLED=False` (OFF) — inerte jusqu'à activation
  + restart délibéré.** Indépendant de fib-v4 (clés strategy distinctes, TF M5).
  Barres M5 en REST dédié (`_fetch_bars_m5`), pas de M1Buffer.

> Stratégies abandonnées (cf. `docs/strategies_abandoned.md` et `output/archive/`) :
> fib-v3, VPC, ARF, SMC v1, opr-h4, kijun-pb.

---

## Architecture — 3 couches

```
┌─────────────────────────────────────────────────────┐
│  RECHERCHE        strategies/  backtest.py           │
│  Tester, itérer, casser sans risque                  │
├─────────────────────────────────────────────────────┤
│  INFRA PARTAGÉE   core/metrics  backtester optimizer │
│  Métriques standardisées, runner universel           │
├─────────────────────────────────────────────────────┤
│  PRODUCTION       core/opr.py + core/opr_v5_1.py    │
│                   core/strategy_fib_v4.py            │
│                   broker/live_runner.py — NE PAS TOUCHER │
└─────────────────────────────────────────────────────┘
```

**Règle absolue :** les fichiers production ne sont modifiés que lors d'une promotion explicite d'une nouvelle stratégie validée 🟢. Jamais en cours de recherche.

---

## Structure du dépôt

```
topstep_signals/
├── backtest.py              # CLI slim — exécute n'importe quelle stratégie
├── optimize.py              # CLI slim — walk-forward universel
├── config.py                # Tous les paramètres (1 section par stratégie)
├── replay_portfolio_risk.py # Analyse garde-fou portefeuille
│
├── strategies/              # RECHERCHE — 1 fichier = 1 stratégie
│   ├── opr.py               # Wrapper backtest OPR
│   └── fib.py               # Wrapper backtest Fib
│
├── core/
│   ├── metrics.py           # Métriques standardisées + verdict 🟢🟡🔴
│   ├── backtester.py        # Runner universel (10 charts aléatoires si --plot)
│   ├── optimizer.py         # Walk-forward universel + rapport de décision
│   ├── data.py              # Chargement CSV / TradingView
│   ├── opr.py               # ← PRODUCTION (live_runner l'importe)
│   ├── strategy_fib.py      # ← PRODUCTION (live_runner l'importe)
│   ├── risk_topstep.py      # Garde-fou Topstep par trade
│   ├── risk_portfolio.py    # Garde-fou global portefeuille (live)
│   ├── signal_selector.py   # Sélection actif corrélé NQ1 > YM1 > MES1
│   ├── event_logger.py      # Log structuré des événements live
│   ├── analysis_chart.py    # Graphiques journaliers
│   ├── chart.py             # Graphiques par trade
│   └── explore_chart.py     # Idéation visuelle (charts stratifiés, multi-TF)
│
├── broker/                  # ← PRODUCTION — ne pas toucher
│   ├── live_runner.py       # Daemon de session (SessionRunner)
│   ├── projectx_client.py   # API TopstepX
│   └── telegram_bot.py      # Alertes et /status bidirectionnel
│
├── .claude/
│   ├── skills/              # Skills Claude Code (new-strategy, etc.)
│   └── templates/           # strategy_template.md, rapport_template.md
│
├── logs/                    # gitignored — trading_events.log
├── data/                    # gitignored — CSV 15m par ticker
└── output/                  # gitignored — backtests, charts, rapports
```

---

## Commandes

> Noms de stratégie = registry auto-discovery (`python backtest.py --list`). Actuels :
> `opr`, `opr_v5_1`, `fib_v4`, `fib_fine`, `vwap_pb`, `pdh_pdl_retest`, … ou `all`.

### Backtest
```bash
python backtest.py --strategy opr    --csv-dir ./data
python backtest.py --strategy fib_v4 --csv-dir ./data --ticker NQ1
python backtest.py --strategy all    --csv-dir ./data          # toutes les stratégies du registry
python backtest.py --strategy opr    --csv-dir ./data --plot   # + 10 charts aléatoires
python backtest.py --strategy opr    --live --bars 15000       # données TradingView
```

### Optimisation walk-forward (génère output/robustness_<id>.{json,md} si n_oos suffisant)
```bash
python optimize.py --strategy opr    --csv-dir ./data
python optimize.py --strategy fib_v4 --csv-dir ./data --ticker NQ1
python optimize.py --strategy all    --csv-dir ./data
python optimize.py --strategy opr    --csv-dir ./data --is-end 2025-09-30
```

### Exploration visuelle manuelle (optionnelle — coup d'œil humain)
> Outil CLI autonome conservé pour un coup d'œil ponctuel (plus d'agent chartist). À utiliser
> à la demande, pas dans le pipeline par défaut (charts coûteux en tokens si lus par un agent).
```bash
python -m core.explore_chart --ticker NQ1 --n 20                    # mono-TF 15m, stratifié régime
python -m core.explore_chart --ticker NQ1 --n 10 --multi-tf         # trio 15m + H1 + D1
python -m core.explore_chart --ticker MES1 --stratify random --seed 7
# Sortie : output/explore/<TICKER>/<YYYY-MM-DD>_<TF>.png  (régimes : trending/ranging/macro/vol_h/vol_b/mixed)
```

### Production (live)
```bash
# Vérifier le daemon tmux
tmux ls
tmux attach -t <session>

# État du live runner
cat state/live_state.json

# Logs événements live (fills, closes, erreurs, risk)
tail -f logs/trading_events.log
```

---

## Pipeline d'une nouvelle stratégie

> Pour un développement complet : utiliser le skill `/new-strategy "<description>"`.
> Les étapes ci-dessous sont la version manuelle / référence.

### 1. Créer `strategies/ma_strategie.py`

Schéma de colonnes obligatoire (compatibilité `core/optimizer.py`) :
```
date, dir, entry, sl, tp, sl_dist, tp_dist, rr, n_ct,
result (TP|SL|TE|NOT_FILLED), pnl, fill_time, exit_time, exit, regime
```
`pnl` = **P&L net** (slippage + commissions inclus).

Colonnes optionnelles tolérées : `pnl_gross`, `adx`, `atr_pct`, `is_macro_day`.

### 2. Enregistrer dans `backtest.py` et `optimize.py`
```python
REGISTRY = {
    "opr": "strategies.opr",
    "fib": "strategies.fib",
    "ict": "strategies.ict",   # ← ajouter ici dans les deux fichiers
}
```

### 3. Ajouter une section dans `config.py`
```python
# ==============================================================================
# STRATÉGIE ICT
# ==============================================================================
ICT_STRATEGY_VERSION = "ict-v1"
ICT_SL_MULT = {"MES1": 1.0, "NQ1": 1.0, "YM1": 1.0}
...
```

### 4. Backtester → Optimiser → Décider
```bash
python backtest.py --strategy ict --csv-dir ./data --plot
python optimize.py --strategy ict --csv-dir ./data
```

---

## Critères de décision (verdict automatique)

| Critère | 🟢 PRODUCTION | 🟡 VEILLE | 🔴 REJET |
|---|---|---|---|
| OOS Profit Factor | ≥ 1.5 | ≥ 1.2 | < 1.2 |
| Bootstrap **portfolio** | ≥ 80% | ≥ 50% | < 50% |
| Trades OOS | ≥ 20 | ≥ 8 | < 8 |
| P&L OOS | > 0 | > 0 | ≤ 0 |

> Le bootstrap **par ticker** peut être bas (ex: MES1 5.8 %) sans être disqualifiant — c'est le bootstrap **portefeuille** qui décide.

**Rapport automatique après `optimize.py` :**
```
══════════════════════════════════════════════════════════════
  RAPPORT — ict-v1
══════════════════════════════════════════════════════════════
  IS  (déc 2024 – sept 2025) : PF=1.82  P&L=+$8 400  n=120
  OOS (oct 2025 – mars 2026) : PF=1.55  P&L=+$3 200  n=44
  Bootstrap OOS : 87%    DD OOS : -$620
  ────────────────────────────────────────────────────────────
  VERDICT : 🟢 PRODUCTION
══════════════════════════════════════════════════════════════
```

Pour les analyses approfondies (multiple-testing correction, stress tests par régime, Monte Carlo permutation), voir le skill `/new-strategy` PHASES 4-5.

---

## Promotion en production

**À exécuter uniquement après verdict 🟢, validation visuelle des charts, ET confirmation explicite de l'utilisateur pour chaque étape touchant `core/` ou `broker/`.**

1. Créer `core/<strategy_id>.py` (logique d'exécution live)
2. Ajouter `get_<strategy>_live_signal()` utilisable par `live_runner.py`
3. Mettre à jour `broker/live_runner.py` (imports + boucle de session)
4. Mettre à jour `core/signal_selector.py` si nécessaire
5. Configurer `core/event_logger.py` pour la nouvelle stratégie
6. Tester en simulation (`PROJECTX_LIVE_MODE = False`) avant live réel
7. Activation progressive : 1 contrat 1 semaine → sizing nominal

---

## Rôle de partenaire live

### Checks à faire en début de session
```bash
# 1. Daemon actif ?
tmux ls

# 2. État du RM (risk manager)
cat state/live_state.json | python -m json.tool

# 3. Trades du jour via Telegram ou logs
tail -n 30 logs/trading_events.log

# 4. Vérifier que les limites Topstep ne sont pas approchées
```

### Signaux d'alerte à surveiller
- `live_state.json` : `cum_pnl`, `peak_pnl`, `daily_pnl` proches des limites
- Telegram : messages de blocage RM, erreurs API ProjectX, pertes de connexion
- Trades répétés `NOT_FILLED` → problème de données ou de connectivité
- Séquence de SL consécutifs → vérifier conditions de marché

### Ce que Claude NE fait PAS sans confirmation
- Modifier `broker/live_runner.py` ou tout fichier `core/` en production
- Changer `PROJECTX_LIVE_MODE = True` → `False` (interrompt le live)
- Redémarrer le daemon tmux
- Modifier les params SL/TP en production pendant une session live
- Activer `YM1_ENABLED = True` dans `core/opr.py` sans preuve OOS

---

## Configuration (`config.py`)

**Tout paramètre modifiable doit être dans `config.py`. Jamais hardcodé.**

### Sections clés

| Section | Variables clés |
|---|---|
| Global | `RISK_PER_TRADE_USD=100`, `MAX_TRADES_PER_DAY=2` |
| Utilisateur | `USER_DAILY_LOSS_MAX=200`, `USER_MAX_TRADES_PER_DAY=3` |
| Topstep | `TOPSTEP_DAILY_LOSS_MAX=1000`, `TOPSTEP_TRAILING_DD=2000` |
| Frictions | `SLIPPAGE_TICKS_PER_TICKER`, `COMMISSION_RT_PER_CONTRACT=1.40` |
| Macro | `MACRO_EVENT_DATES` (FOMC/CPI/NFP/JOLTS) |
| Circuit breakers | `CONSEC_LOSS_PAUSE_DAYS=5`, `DAILY_STOP_AFTER_SL=False` |
| OPR | `OPR_SL_ATR_MULT`, `OPR_TP_ATR_MULT`, `OPR_STRATEGY_VERSION` |
| Fib | `FIB_SL_ATR_MULT_PER_TICKER`, `FIB_TP_ATR_MULT_PER_TICKER` |
| Broker | `PROJECTX_LIVE_MODE=False` (simulation), `LIVE_STATE_FILE` |
| Telegram | `TELEGRAM_ENABLED`, niveaux `TELEGRAM_LEVEL_*` |

### Walk-forward IS/OOS (dates fixes pour cohérence)
- **IS :** sept 2024 → 2025-09-30 (`IS_END = "2025-09-30"`)
- **OOS :** 2025-10-01 → mai 2026 (`OOS_START = "2025-10-01"`)
- Critère d'acceptation : `OOS PF ≥ 1.2 ET n ≥ 8 ET P&L OOS > 0`

---

## Performances production (référence — mise à jour 2026-05-19)

### OPR (routage v4/v5.1 par ticker)

OOS oct 2025 → mai 2026 — bootstrap stationnaire P(PF>1) = 100% portfolio.

| Ticker | Version | Filtre F2_min_atr | PF OOS | P&L OOS | n_oos | DD max |
|---|---|---|---|---|---|---|
| MES1 | opr-v4 pass-through | (aucun) | 1.36 | +$2,303 | 121 | -$998 |
| NQ1 | opr-v5.1 schéma A | ≥ 0.10 ATR | ~3.0+ | élevé | ~30 | ≤ -$500 |
| YM1 | opr-v5.1 schéma A | ≥ 0.15 ATR | 1.87 | +$7,934 | 274 (full hist.) | -$716 |

Cf. `output/rapport_opr-v5.1.md`, `output/robustness_opr-v5.1.md`.

### Fib fib-v4 (promu 2026-05-19)

OOS oct 2025 → mai 2026.

| Ticker | Fib level | Filtre `wick_through_atr` | PF OOS | n_oos | Bootstrap |
|---|---|---|---|---|---|
| MES1 | 0.382 | < 0.05 ATR | **6.01** | 25 | 100% |
| NQ1 | 0.382 | < 0.80 ATR | **6.47** | 21 | 100% |
| MGC1 | 0.500 | < 0.40 ATR (+ skip macro) | **3.46** | 24 | 100% |
| **Portfolio** | — | — | **4.97** | 70 | 100% |

P&L OOS portefeuille : **+$5,234** (vs fib-v3 +$3,287 sur même période = +$1,947).

Cf. `output/rapport_fib-v4.md`, `output/robustness_fib-v4.md`.

### Fib fib-fine-v2 (promu 2026-06-02 — flag OFF)

Retracement Fibonacci NATIF M5. Filtre wick look-ahead retiré, remplacé par filtre
CAUSAL d'expansion de volatilité (`atr_ratio_sl >= 1.0`, seuil fixe pré-spécifié,
lu sur barre M5 i-1). OOS oct 2025 → mars 2026.

| Ticker | TF | PF OOS | n_oos | Sizing |
|---|---|---|---|---|
| NQ1 | M5 | **2.41** | 57 | $130 |
| MES1 | M5 | **1.47** | 105 | $130 |
| **Portfolio** | M5 | **1.63** | — | bootstrap 99.6% |

P&L OOS portefeuille : **+$6,029**. Replay portefeuille : **0 breach**, DD incrémental
**+$187**. Univers live initial = NQ1 + MES1 (cœur 🟢). MGC1 (bonus 🟡 OOS-only) et YM1
(mirage look-ahead) EXCLUS. **Audit visuel sauté (assumé par l'utilisateur).**

Promu flag `FIB_FINE_ENABLED=False` (OFF) : code live inerte tant que le flag n'est pas
passé à `True` + restart délibéré. Indépendant de fib-v4 (clés strategy distinctes).
Cf. `core/fib_fine_v2.py`, `strategies/fib_fine.py`.

---

## Conventions de code

- **Langue :** nommage, commentaires, docstrings → **français**.
  Exception documentée : `MAX_TRADES_PER_DAY` reste en anglais (héritage).
- **Paramètres :** toujours dans `config.py`, jamais hardcodés
- **Timeframes :** toutes les heures OPR en **heure NY** (DST-aware via `zoneinfo("America/New_York")`)
- **Timestamps :** UTC naïf en interne, conversion NY uniquement en frontière de stratégie
- **Pas de leak temporel :** ATR et features calculés strictement avant le cutoff
- **Bump de version :** `OPR_STRATEGY_VERSION` ou `FIB_STRATEGY_VERSION` à chaque
  changement structurel de la stratégie
- **Charts :** `--plot` génère 10 jours aléatoires parmi les jours avec fills
- **Reproductibilité :** `np.random.seed(42)` en tête de chaque module utilisant l'aléatoire

---

## Pièges à éviter

- Ne **jamais** modifier `core/opr.py`, `core/opr_v5_1.py` ou `core/strategy_fib_v4.py` pour de la recherche → utiliser les wrappers `strategies/*.py`
- Ne **jamais** accepter des params walk-forward avec OOS PF < 1.2
- Données CSV : `{csv_dir}/{TICKER}_data_m15.csv` (majuscule)
- `YM1_ENABLED = True` (config.py) depuis 2026-05-18 — promotion OPR v5.1. Ne pas redescendre sans preuve OOS contraire.
- Après changement de `config.py`, vérifier que `core/opr.py` reflète bien les valeurs (il importe les dicts par référence et les patch dynamiquement)
- Le bootstrap par ticker seul peut être bas (ex: MES1 5.8%) — ce qui compte c'est le **bootstrap portefeuille** (tous actifs agrégés)
- `data/` et `output/` sont gitignorés — ne pas versionner de données de marché
- **Fill ambigu (SL et TP dans même barre M15)** : assume SL prioritaire, jamais TP
- **Ignorer slippage et commissions** dans un nouveau backtest = sur-estimation typique de 15-25 % du PF
- **Hardcode d'une date dans une stratégie** : interdit. Toutes les dates spéciales (macro, sessions partielles) vont dans `config.py`.

---

## Rappels d'invariants — vérifier avant chaque commit / promotion

- [ ] Schéma colonnes du DataFrame de trades respecté (`pnl` = net, canonique)
- [ ] `<STRAT>_STRATEGY_VERSION` bumpé si changement structurel
- [ ] Aucun fichier de `core/opr.py`, `core/opr_v5_1.py`, `core/strategy_fib_v4.py`, `core/fib_helpers.py`, `broker/` modifié sans confirmation explicite
- [ ] Tous les paramètres dans `config.py`
- [ ] Walk-forward IS=2025-09-30 / OOS=2025-10-01 respecté
- [ ] Slippage et commissions intégrés dans `pnl`
- [ ] Reproductibilité : seed fixé
- [ ] DST-aware : `zoneinfo`, pas `pytz` ou décalage horaire en dur
