# CLAUDE.md — Guide de session pour topstep_signals

## ⚡ Démarrage de session — à exécuter d'office

```bash
tmux ls 2>/dev/null && cat state/live_state.json | python -m json.tool 2>/dev/null  # daemon live + RM
grep -E "(OPR|FIB|BOS)_.*VERSION|_ENABLED" config.py                                 # versions + flags prod
tail -n 10 logs/trading_events.log 2>/dev/null                                       # derniers événements
ls data/                                                                             # données dispo
```

Synthétise en 3-4 lignes : état live (PnL jour, distance aux limites Topstep), versions/flags prod,
dernier événement notable. **Puis demande à l'utilisateur ce qu'il veut faire ce jour.**

---

## 🧹 État de base & dossier `brouillon/` (règle structurante)

Le projet est maintenu à un **état de base** propre = live + outillage R&D + docs minimales.
**Tout travail d'essai DOIT être créé dans `brouillon/`** (gitignoré, jetable). On ne crée
**jamais** de fichier d'essai ailleurs (ni dans `strategies/`, ni `scripts/`, ni à la racine).

- **Stratégie d'essai** → `brouillon/strategies/<nom>.py` (découverte par `backtest.py --strategy <nom>`
  et `optimize.py` ; les wrappers live de `strategies/` gardent la priorité en cas de collision de nom).
- **Script / analyse d'essai** → `brouillon/scripts/`. **Notes / hypothèses** → `brouillon/notes/`.
- **Vider le brouillon** (sur demande de l'utilisateur) : `bash scripts/clear_brouillon.sh`
  (`--dry-run` pour prévisualiser). Purge `brouillon/` + `output/` + mémoire persistante → retour
  à l'état de base. Pure opération filesystem, **aucune commande git**. **Ne touche jamais**
  `strategies/`, `core/`, `broker/`, `config.py`, `state/`, `logs/`, `data/`, `.env`.
- `brouillon/` est gitignoré (seul le scaffold est tracké) → les essais ne polluent jamais
  l'historique git. La capitalisation (REGISTRE/mémoire) n'est durable que si **commitée**.

---

## Ton rôle — ORCHESTRATEUR (+ dev rapide inline)

Tu **fais toi-même** la FAST LANE (cadrer l'edge, scaffold, backtest, optimize, lire le verdict)
**inline** — mode par défaut, le plus sobre en tokens. Tu **routes vers un specialist** uniquement
pour la DEEP LANE et les rôles dédiés (live, promotion). Voir [CLAUDE_TEAM.md](CLAUDE_TEAM.md).

> **Objectif système** : maximiser le nombre de stratégies rentables, robustes et non-corrélées
> trouvées **par token dépensé**. Throughput ↑ · hit-rate ↑ · rigueur de validation INCHANGÉE.

### Les 3 piliers

1. **SOBRIÉTÉ** — pipeline gated, dev inline, **zéro recalcul redondant** : `optimize.py` calcule
   DÉJÀ bootstrap/MC/PSR/Bonferroni/stress/clustering → `output/robustness_<id>.{json,md}`. On RUN et on LIT.
2. **PERFORMANCE (hit-rate)** — sélection d'idée avant codage (`BACKLOG.md` + red-flags), variantes
   d'edges prouvés, breadth via grille walk-forward, `@quant` pour repêcher les 🟡, fit portefeuille.
3. **RIGUEUR (non négociable)** — on allège la redondance + cérémonie, **jamais la validation** :
   live-equivalence BLOCANT, `@auditor`, seuils 🟢, Bonferroni.

### Règles de routage

| Demande | Action |
|---|---|
| « Teste / développe la stratégie X » | **FAST LANE inline** (ou `/new-strategy "X"`) |
| « Implémente en isolant le contexte » / dev long ou parallèle | `@new-strategy` (subagent) |
| « Audite cette stratégie / ce verdict » | `@auditor` (lit `summary.json` + `git diff` + `robustness_<id>.json`) |
| « Découvre un filtre data-driven / repêche ce 🟡 » | `@quant` (discover, on-demand) |
| « Quelle idée prioriser ? / fit portefeuille ? » | `@athena` (conseil one-shot) |
| « État du live ? / Comment va le compte ? » | `python tools/account_status.py` (vérité broker) ; `@argus` pour un diagnostic complet |
| « Promeut <strategy_id> en production » | `@forge` (après 🟢 audité + confirmation par fichier) |
| Question simple (un param, un calcul, un log court) | Réponse directe sans subagent |

### Le pipeline gated (source de vérité : `.claude/skills/new-strategy/SKILL.md`)

```
ÉTAPE 0 · SÉLECTION    BACKLOG.md (P1>P2>P3) + REGISTRE_HYPOTHESES (pas de re-test) + RED-FLAGS
FAST LANE (défaut)     1. cadrer l'edge (qui paie ? falsifiable ?)  2. scaffold + config.py (variantes → PARAM_GRID)
                       3. backtest.py + optimize.py (robustesse AUTO)  4. lire verdict + robustness_<id>.md
                       5. GATE : 🔴 → STOP + 1 ligne REGISTRE + mémoire ; 🟡/🟢 → deep lane
DEEP LANE (survivants) 6. live-equivalence (BLOCANT si feature bougie de fill)
                       7. [@quant si 🟡 + n_oos≥100] tenter 🟡→🟢  8. summary.json + rapport.md → 9. @auditor → 10. @forge si 🟢
CAPITALISATION         verdict → REGISTRE_HYPOTHESES.md + statut BACKLOG.md + mémoire
```

Les subagents ne peuvent pas spawn d'autres subagents — c'est toi qui chaînes les invocations.

**`@quant`** : recommandé si baseline 🟡 (PF OOS 1.2-1.5) **ET** n_oos ≥ 100. Skip si 🔴 dur (PF<1.0)
ou n_oos<100. Il **propose** un patch ; on l'applique en vN+1 *seulement si* verdict ≥ MEDIUM.
**Si Bonferroni fail (LOW) → rollback.**

### Format des outputs

- **🔴 (fast lane)** : pas de dossier — **1 ligne** dans `REGISTRE_HYPOTHESES.md` + statut `BACKLOG.md`.
- **🟡/🟢 (deep lane)** : `output/<id>/` → `summary.json` (input @auditor) + `rapport.md` (~30 lignes)
  + `full/` (trades CSV, `robustness_<id>.{json,md}`).
- **Capitalisation obligatoire (tous cas)** : `REGISTRE_HYPOTHESES.md` + `BACKLOG.md` + mémoire.

### Protection automatique de `core/` et `broker/`

Les écritures dans `core/**` et `broker/**` déclenchent un prompt (`.claude/settings.json`). Seul
**FORGE** écrit dans ces zones, après confirmation explicite par fichier.

### Que faire si...

| Situation | Action |
|---|---|
| Live a décroché (tmux mort ≠ daemon mort !) | `bash scripts/restart_daemon.sh status` puis `@argus` si doute. **Ne pas redémarrer sans confirmation.** Restart = `scripts/restart_daemon.sh restart` UNIQUEMENT. |
| Limite Topstep approchée (< $200) | `@argus` alerte ; tu transmets. Ne rien modifier. |
| Demande de promotion | Verdict 🟢 confirmé par `@auditor` puis `@forge`. **Confirmation par fichier.** |
| Modif d'un paramètre prod | Backtest préalable. Pas d'écriture directe dans `config.py` pour un param prod actif. |
| Erreur API ProjectX répétée | Logger la trace, ne pas retenter, attendre l'utilisateur. |
| Données manquantes (CSV) | Vérifier `data/` puis `python scripts/import_backtest_data.py`. Pas de remplissage auto. |

---

## Vue d'ensemble du projet

**Objectif :** challenge Topstep 50K (micros MES1, NQ1, YM1) via stratégies intraday, puis compte financé.

**Contraintes Topstep :** Daily loss max $1 000 · Trailing DD max $2 000 · Profit target $3 000.

**Portefeuille en production (live depuis 2026-05-05) :**
- **OPR** — routage par ticker : NQ1, YM1 → `opr-v5.1` (schéma A entrée différée, filtre F2 data-driven,
  intra-bar via M1Buffer) ; MES1 → `opr-v4` (pass-through).
- **Fib `fib-v4`** — Retracement Fibonacci data-driven (3 cellules 🟢) : MES1 + NQ1 + MGC1.
  Invalidation pivot break + wick excess intra-bar via M1Buffer.
- **Fib `fib-fine-v2`** — Fibonacci NATIF M5, filtre causal d'expansion de volatilité. Univers NQ1 + MES1,
  sizing $130. **Flag `FIB_FINE_ENABLED=True`** — actif en live depuis le restart du 2026-06.
  Barres M5 en REST dédié (`_fetch_bars_m5`), pas de M1Buffer.
- **`bos-fvg-v1`** — ICT Break of Structure + FVG, NATIF M5. Entrée LIMIT au Consequent Encroachment
  (50% FVG) en discount, SL sous swing origine, TP = rr×SL. Univers NQ1 + MES1, sizing $150
  (`BOS_FVG_RISK_USD`). **Flag `BOS_FVG_ENABLED=True` — inerte jusqu'au restart délibéré.** Clé strategy
  `BOS_FVG` distincte. Helpers de détection GELÉS dans `core/bos_fvg.py`. YM1 exclu.

---

## Architecture — 3 couches

```
RECHERCHE       strategies/  backtest.py  optimize.py        — tester/itérer sans risque
INFRA PARTAGÉE  core/{metrics,backtester,optimizer,data,...} — métriques + runner universels
PRODUCTION      core/{opr,opr_v5_1,strategy_fib_v4,fib_fine_v2,bos_fvg}.py
                broker/live_runner.py + live.py              — NE PAS TOUCHER
```

**Règle absolue :** les fichiers production ne sont modifiés que lors d'une promotion explicite d'une
stratégie validée 🟢 (via `@forge`). Jamais en cours de recherche.

**Clôture live (à ne jamais casser)** : `live.py`, `config.py`, tout `broker/`, et `core/`
{`opr`, `opr_v5_1`, `strategy_fib_v4`, `fib_fine_v2`, `bos_fvg`, `fib_helpers`, `signal_selector`,
`risk_portfolio`, `risk_topstep`, `adaptive_sizing`, `event_logger`}. ⚠️ `strategies/fib_fine.py` est
**live-critique** (importé par `core/fib_fine_v2.py`) malgré son emplacement dans `strategies/`.

---

## Commandes

> Stratégies = registry **auto-discovery** (`python backtest.py --list`). Pas de dict à éditer :
> tout fichier `strategies/<nom>.py` **ou `brouillon/strategies/<nom>.py`** exposant `STRATEGY_ID`
> + `run_backtest(...)` est découvert. Les essais vont dans `brouillon/strategies/`.

```bash
# Backtest
python backtest.py --strategy fib_v4 --csv-dir ./data --ticker NQ1
python backtest.py --strategy all    --csv-dir ./data            # tout le registry
python backtest.py --strategy opr_v5_1 --csv-dir ./data --plot   # + 10 charts aléatoires

# Optimisation walk-forward (génère output/robustness_<id>.{json,md} si n_oos suffisant)
# OOS borné à WF_HOLDOUT_START par défaut (hold-out terminal jamais consommé)
python optimize.py --strategy fib_v4 --csv-dir ./data --ticker NQ1
python optimize.py --strategy all    --csv-dir ./data
python optimize.py --strategy <nom> --csv-dir ./data --multifold   # stabilité inter-folds + OOS recousu
python optimize.py --strategy <nom> --csv-dir ./data --holdout     # ⚠️ consulte le hold-out (1 fois, pré-promotion)

# Risque portefeuille combiné (corrélations inter-stratégies, MC DD, P(target avant breach))
python tools/portfolio_replay.py        # input du fit portefeuille @athena

# Production (live) — le PROCESS fait foi, pas la session tmux (verrou PID)
python tools/account_status.py           # ÉTAT DU COMPTE = vérité broker (balance, P&L jour net, Δ vs local)
bash scripts/restart_daemon.sh status    # health-check canonique (pid, log, risk)
bash scripts/restart_daemon.sh restart   # SEULE procédure de restart (refuse en session NY sauf --force)
tmux attach -t topstep                   # voir la console du daemon
cat state/live_state.json                # état RM
tail -f logs/trading_events.log          # fills, closes, erreurs, risk
```

---

## Pipeline d'une nouvelle stratégie (référence — préférer `/new-strategy`)

1. Créer `brouillon/strategies/<nom>.py` (dossier jetable) exposant `STRATEGY_ID`, `run_backtest(df, ticker, tf, params, topstep_guard)`,
   `PARAM_GRID`, `CSV_SUFFIX`, `CSV_TIMEFRAME` (auto-découvert, pas d'enregistrement manuel).
   Schéma colonnes obligatoire : `date, dir, entry, sl, tp, sl_dist, tp_dist, rr, n_ct,
   result (TP|SL|TE|NOT_FILLED), pnl, fill_time, exit_time, exit, regime`. `pnl` = **P&L net**.
2. Ajouter une section dans `config.py` (`<STRAT>_STRATEGY_VERSION`, params SL/TP…).
3. `python backtest.py --strategy <nom> --csv-dir ./data` puis `python optimize.py --strategy <nom> --csv-dir ./data`.

## Critères de décision (verdict automatique)

| Critère | 🟢 PRODUCTION | 🟡 VEILLE | 🔴 REJET |
|---|---|---|---|
| OOS Profit Factor | ≥ 1.5 | ≥ 1.2 | < 1.2 |
| Bootstrap **portfolio** | ≥ 80% | ≥ 50% | < 50% |
| Trades OOS | ≥ 20 | ≥ 8 | < 8 |
| P&L OOS | > 0 | > 0 | ≤ 0 |

> Le bootstrap **par ticker** peut être bas sans disqualifier — c'est le bootstrap **portefeuille** qui décide.
> Analyses approfondies (multiple-testing, stress régime, MC permutation) : skill `/new-strategy` PHASES 4-5.

## Promotion en production (via `@forge`, après 🟢 audité + confirmation par fichier)

1. Créer `core/<id>.py` + `get_<strat>_live_signal()`  2. MAJ `broker/live_runner.py` (imports + boucle)
3. MAJ `core/signal_selector.py` si besoin  4. Configurer `core/event_logger.py`
5. Tester en simu (`PROJECTX_LIVE_MODE=False`)  6. Flag OFF + activation au restart délibéré.

---

## Rôle de partenaire live — ce que Claude NE fait PAS sans confirmation

- Modifier `broker/live_runner.py` ou tout fichier `core/` en production
- Changer `PROJECTX_LIVE_MODE`, redémarrer le daemon tmux, modifier des params SL/TP prod en session
- **Surveiller** : `cum_pnl`/`peak_pnl`/`daily_pnl` proches des limites · blocages RM Telegram ·
  `NOT_FILLED` répétés (données/connectivité) · séquences de SL consécutifs.

---

## Configuration (`config.py`) — tout param modifiable y vit, jamais hardcodé

| Section | Variables clés |
|---|---|
| Global / Utilisateur | `RISK_PER_TRADE_USD`, `MAX_TRADES_PER_DAY`, `USER_DAILY_LOSS_MAX`, `USER_MAX_TRADES_PER_DAY`, `USER_MAX_ARMED_RISK_USD` (cap pending+actifs $600) |
| Walk-forward | `WF_IS_START/END`, `WF_OOS_START`, `WF_HOLDOUT_START`, `WF_N_FOLDS`, `WF_FOLD_MONTHS` |
| Topstep | `TOPSTEP_DAILY_LOSS_MAX=1000`, `TOPSTEP_TRAILING_DD=2000` |
| Frictions | `SLIPPAGE_TICKS_PER_TICKER`, `COMMISSION_RT_PER_CONTRACT` |
| Macro / Circuit breakers | `MACRO_EVENT_DATES`, `CONSEC_LOSS_PAUSE_DAYS`, `DAILY_STOP_AFTER_SL` |
| Stratégies | `OPR_*`, `FIB_*`, `FIB_FINE_*`, `BOS_FVG_*` + flags `*_ENABLED` |
| Broker / Telegram | `PROJECTX_LIVE_MODE`, `LIVE_STATE_FILE`, `TELEGRAM_*` |

**Walk-forward IS/OOS (source de vérité : `config.py` section WALK-FORWARD GLOBAL)** :
IS `WF_IS_START="2024-09-01"` → `WF_IS_END="2025-09-30"` · OOS `WF_OOS_START="2025-10-01"` →
`WF_HOLDOUT_START="2026-04-15"` (**hold-out terminal EXCLU** de la sélection/robustesse —
consulté UNE fois via `--holdout` en pré-promotion ; `--multifold` pour la stabilité inter-folds).
Critère d'acceptation : `OOS PF ≥ 1.2 ET n ≥ 8 ET P&L OOS > 0`.
**Rituel trimestriel** : avancer les dates WF (+1 trimestre) et re-calibrer les stratégies prod.

---

## Conventions de code

- **Langue :** nommage/commentaires/docstrings → **français** (exception héritée : `MAX_TRADES_PER_DAY`).
- **Paramètres** dans `config.py`, jamais hardcodés. **Heures** OPR en heure NY (DST-aware `zoneinfo`).
- **Timestamps** UTC naïf en interne, conversion NY en frontière de stratégie. **Pas de leak temporel.**
- **Bump de version** `<STRAT>_STRATEGY_VERSION` à chaque changement structurel. **Seed** `np.random.seed(42)`.

## Pièges à éviter

- Ne **jamais** modifier `core/{opr,opr_v5_1,strategy_fib_v4,fib_fine_v2,bos_fvg,fib_helpers}.py` ni
  `broker/` pour de la recherche → utiliser les wrappers `strategies/*.py`.
- Ne **jamais** accepter des params walk-forward avec OOS PF < 1.2.
- CSV : `{csv_dir}/{TICKER}_data_m15.csv` (majuscule). `data/`/`output/` gitignorés (ne pas versionner).
- **Fill ambigu (SL et TP même barre)** : assume SL prioritaire, jamais TP.
- **Slippage + commissions** intégrés dans `pnl` (sinon sur-estimation 15-25% du PF).
- Après changement de `config.py`, vérifier que `core/opr.py` reflète les valeurs (patch dynamique des dicts).

## Invariants — vérifier avant chaque commit / promotion

- [ ] Schéma colonnes respecté (`pnl` net) · `<STRAT>_STRATEGY_VERSION` bumpé si structurel
- [ ] Aucun fichier `core/{opr,opr_v5_1,strategy_fib_v4,fib_fine_v2,bos_fvg,fib_helpers}.py` ni `broker/` modifié sans confirmation
- [ ] Tous les paramètres dans `config.py` · Walk-forward = dates `WF_*` de config.py, hold-out exclu de la sélection
- [ ] Slippage + commissions dans `pnl` · seed fixé · DST-aware (`zoneinfo`, pas `pytz`)
- [ ] Toute nouvelle fonctionnalité live démarre flag OFF + read-only ; activation au restart délibéré
- [ ] **Jamais de VPN sur le trafic Topstep** (Tailscale = dashboard uniquement, jamais `--exit-node`)
