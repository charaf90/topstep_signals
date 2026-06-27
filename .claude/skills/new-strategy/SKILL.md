---
name: new-strategy
description: Pipeline gated de test d'une stratégie intraday — sélection, cadrage de l'edge, scaffold, backtest+optimize (robustesse auto), gate 🔴/🟡/🟢, puis validation lourde uniquement pour les survivants. Sobre en tokens, rigoureux dans la validation. À invoquer sur commande explicite de l'utilisateur.
disable-model-invocation: true
argument-hint: "[description de la stratégie]"
allowed-tools: Bash Read Write Edit Grep WebSearch
effort: high
context: fork
agent: general-purpose
---

# Pipeline gated d'une stratégie intraday — topstep_signals

## Contexte projet (chargé à chaud)

Versions en production :
!`grep -E "(OPR|FIB|FIB_V4|FIB_FINE)_(STRATEGY_VERSION|ENABLED)|OPR_V5_1_LIVE_TICKERS" config.py`

Données CSV disponibles :
!`ls data/`

Stratégies déjà présentes (auto-discovery) :
!`python -c "from core.registry import list_strategy_names; print(list_strategy_names())" 2>/dev/null || echo "registry indisponible"`

---

## Objectif du système

> **Maximiser le nombre de stratégies rentables, robustes et non-corrélées trouvées par
> token dépensé.** Throughput ↑ · hit-rate ↑ · rigueur de validation INCHANGÉE.

La plupart des idées meurent au premier backtest. On les tue pour **quelques k tokens**,
pas 100k. La machinerie lourde (live-equivalence, quant, audit) ne sert que les **survivants**.

## Les 3 piliers (à garder en tête en permanence)

1. **SOBRIÉTÉ** — pipeline gated, dev inline, **zéro recalcul redondant**. On RUN `optimize.py`
   et on LIT `robustness_<id>.md` ; on ne refait JAMAIS stress/MC/Bonferroni/PSR à la main
   (`core/optimizer.py` les calcule déjà, cf. plus bas).
2. **PERFORMANCE (hit-rate)** — sélection d'idée avant codage (backlog + red-flags), préférer
   les variantes d'edges prouvés, breadth via grille walk-forward, quant pour repêcher les 🟡,
   fit portefeuille, capitalisation systématique.
3. **RIGUEUR (non négociable)** — ce qu'on allège = redondance + cérémonie, **jamais la
   validation**. Gates durs intacts (live-eq BLOCANT, auditor, seuils 🟢, Bonferroni, WF fixe).

---

## GARDE-FOUS PERMANENTS (tous niveaux, jamais d'exception)

- **Moteur = M1 (`core/bt_engine.py`, backtesting.py)** : les fills + SL/TP sont résolus **à la minute**
  (vérité de fill). Plus de moteur maison M15. Le same-bar (SL dans la bougie de fill) est résolu par
  construction — c'est précisément ce que le M15 ratait.
- **No look-ahead** : `emit_signals` calcule les signaux sur la TF de signal (M15/M5, reconstruite du M1) ;
  toute feature de décision doit être connue **à la clôture de la barre de signal** (l'ordre devient vif à
  `arm_ts + tf` = no leak). Jamais le low/high/close FINAL de la barre de fill pour décider.
- **Frictions dans `pnl` net** : recalculées maison par `bt_engine` (`SLIPPAGE_TICKS_PER_TICKER` entrée+sortie
  + `COMMISSION_RT_PER_CONTRACT`) ; backtesting.py tourne `commission=0`.
- **Fill conservatif** : si SL et TP dans la même **minute** M1 → **SL prioritaire** (géré par backtesting.py).
- **Essai = dans `brouillon/strategies/<id>.py`** (dossier jetable). Les **params d'essai restent
  auto-contenus** dans ce fichier (constantes module + `PARAM_GRID`) — on ne pollue PAS `config.py`.
  Migration vers `config.py` uniquement à la **promotion** (`@forge`).
- **`np.random.seed(42)`** si tirage aléatoire.
- **Schéma colonnes standard** : produit automatiquement par `bt_engine` à partir des arms — voir FAST LANE §2.
- **Walk-forward fixe** : dates `WF_*` de `config.py` (IS fin 2025-12-31 · OOS 2026-01-01 →
  `WF_HOLDOUT_START`, **hold-out terminal exclu**). ⚠️ **En M1, IS borné à la couverture M1**
  (effectif 2025-02-16→2025-12-31 ; pas de deep-fetch, API broker plafonnée 20 k barres).
  `--holdout` consulté 1 fois en pré-promotion ; `--multifold` recommandé en deep lane.
- **Bump `<STRATEGY_ID>_STRATEGY_VERSION`** à chaque changement structurel.
- **Jamais toucher `core/**` ou `broker/**`** (sauf `@forge` sur confirmation explicite ; `core/bt_engine.py`
  est infra recherche, modifiable hors live).

## Critères de verdict (référence `core/metrics.py`)

| Critère | 🟢 PROD | 🟡 VEILLE | 🔴 REJET |
|---|---|---|---|
| PF OOS | ≥ 1.5 | ≥ 1.2 | < 1.2 |
| Bootstrap **portfolio** OOS | ≥ 80 % | ≥ 50 % | < 50 % |
| Trades OOS | ≥ 50 | ≥ 20 | < 20 |
| P&L OOS net | > 0 | > 0 | ≤ 0 |

Heuristiques de scepticisme (leçons capitalisées) :
*PF OOS ≠ edge si bootstrap fail Bonferroni* · *edge OOS sans edge IS = régime favorable, pas edge* ·
*concentration mensuelle = fragilité* · *multi-ticker relancé après échec = data dredging*.

---

# PIPELINE

## ÉTAPE 0 · SÉLECTION (perf — inline, ~0 token)

Avant d'écrire la moindre ligne :

1. **Consulter les actifs de capitalisation** :
   - `strategie_futur/BACKLOG.md` (priorité P1 > P2 > P3 ; préférer une idée déjà priorisée)
   - `REGISTRE_HYPOTHESES.md` — **ne pas re-tester une hypothèse déjà 🔴** sous une autre forme
   - Mémoire persistante (`MEMORY.md` + fichiers) — leçons transversales accumulées
2. **Préférer une variante d'edge prouvé** (OPR/Fib) à un concept novateur — base-rate de succès
   bien plus élevé.
3. **RED-FLAGS — rejet à coût zéro AVANT tout backtest** (si l'un s'applique, dire pourquoi et stop) :
   - Concept novateur sur indicateur ultra-documenté (Ichimoku, pivots S1/R1…) → probablement déjà arbitré.
   - Event-driven basse fréquence (EIA, gap-fill, news) → incompatible avec n_oos ≥ 50.
   - Doublon d'un edge déjà en prod (ORB standalone, retrace 50 % pivot ≈ Fib v4…) → écarté d'office.
   - Mécanique d'entrée qui écrase le RR (TP résiduel < SL) → edge structurellement mort.

## FAST LANE (par défaut — inline session principale)

### 1. Cadrer l'edge (3-4 phrases — remplace l'ancien @researcher)
- **Signal + timing + fenêtre NY** (DST-aware `zoneinfo("America/New_York")`).
- **Qui paie ce P&L ?** (structurellement). Pas de réponse claire → drapeau rouge, le dire.
- **Quelle observation l'invaliderait en live ?** (falsifiable).
- **Pièges du concept** : look-ahead, timezone, régime-dépendance.
- Concept vraiment inconnu → `WebSearch` ponctuel inline (pas de subagent).

### 2. Scaffold (contrat moteur M1 — `core/bt_engine.py`)
- `brouillon/strategies/<strategy_id>.py` exposant :
  - `STRATEGY_ID`, `TICKERS` (indices uniquement : MES1/NQ1/YM1 — pas de M1 gold/oil)
  - `SIGNAL_TF` (`"15min"` | `"5min"`), `SESSION_END_MIN`, optionnel `RUN_MIN_WINDOW`, `MAX_HOLD_MIN`,
    `INTRADAY_DAYCLOSE`
  - **`emit_signals(sig_df, ticker, params) -> list[dict]`** : `sig_df` = OHLCV sur `SIGNAL_TF`
    (reconstruit du M1 par le moteur). Émet des **arms** `{dir, entry, sl, tp, n_ct, arm_ts,
    [timeout_ts, cancel_price/cancel_side, regime, extras]}`. **AUCUNE résolution de fill ici** — le
    fill + SL/TP est résolu au M1 par `bt_engine`. (Pour une strat à mécanique de fill complexe :
    réutiliser un backtest signal-TF validé et émettre ses trades remplis comme arms — cf. `strategies/fib_fine.py`.)
  - `PARAM_GRID` (ou `PARAM_SPACE` continu pour Optuna)
  Référence : [templates/strategy_template.md](templates/strategy_template.md). **Dossier jetable**.
- **Schéma de colonnes** (produit AUTO par `bt_engine`, compat `core/metrics`/`robustness`/`@quant`) :
  ```
  date, dir, entry, sl, tp, sl_dist, tp_dist, rr, n_ct,
  result (TP|SL|TE|NOT_FILLED), pnl(net), fill_time, exit_time, exit, regime, pnl_gross, timeframe
  ```
- **Params auto-contenus** dans le fichier d'essai (constantes + `<STRATEGY_ID>_STRATEGY_VERSION`).
  On n'écrit dans `config.py` qu'à la **promotion** (`@forge`).
- **Breadth** : coder les variantes naturelles (triggers, tickers, seuils) comme dimensions du
  `PARAM_GRID` (≤ 4) — elles seront testées **en un seul run** walk-forward, pas en cycles séparés.
- **`PARAM_SPACE` (optionnel)** : pour des ranges continus, déclarer en plus
  `PARAM_SPACE = {"sl_mult": ("float", 0.3, 2.5), "f2": ("cat", [None, 0.10, 0.15])}` →
  l'optimizer bascule sur Optuna TPE (`--search optuna`, ou auto si grille > seuil config).
  Score toujours IS-only ; `n_strategies_tested` (Bonferroni/DSR) = jeux réellement évalués.
- Auto-discovery `core/registry.py` : un fichier dans `brouillon/strategies/` suffit (pas besoin
  d'éditer `backtest.py`/`optimize.py`).

### 3. Backtest + Optimize (M1 automatique)
```bash
python backtest.py  --strategy <strategy_id> --csv-dir ./data   # M1 + viz HTML nommée
python optimize.py  --strategy <strategy_id> --csv-dir ./data   # WF M1 + robustesse
python optimize.py  --strategy <strategy_id> --csv-dir ./data --search optuna --n-trials 80
```
> Une strat exposant `emit_signals` est **routée en M1 automatiquement** (`bt_engine.supports`) et
> produit `output/backtests/<id>__<ticker>__full__m1.html` (à consulter dans un navigateur).
> **`optimize.py` calcule DÉJÀ toute la robustesse** (`core/optimizer.py` → `core/robustness.py`) :
> block-bootstrap portfolio, Monte-Carlo DD, PSR, Sharpe deflaté, **Bonferroni**, stress régime,
> clustering → `output/robustness_<id>.{json,md}`. **NE JAMAIS refaire ces calculs à la main.**
> Noms de stratégie = registry auto-discovery : `python backtest.py --list`.

### 4. Lire le verdict
- Verdict auto (`core/metrics.py`) imprimé par `optimize.py` + lecture de `output/robustness_<id>.md`.
- Sanity rapide : volume, WR, PF, max pertes consécutives, distribution horaire, dégradation IS→OOS.

### 5. GATE
- **🔴** → STOP. Écrire **1 ligne** dans `REGISTRE_HYPOTHESES.md` (verdict + métriques + **leçon**)
  + entrée mémoire si leçon transversale. Mettre à jour le statut dans `BACKLOG.md`. **Fin.**
- **🟡 / 🟢** → passer en DEEP LANE.

---

## DEEP LANE (survivants 🟡/🟢 seulement)

### 6. Live-equivalence (le M1 résout le fill ; vérifier le NO-LEAK + la fidélité)
Le moteur M1 résout déjà honnêtement le fill same-bar (plus de PF gonflé par la résolution M15).
Restent deux contrôles **BLOCANTS** :
- **No-leak signal-TF** : chaque feature d'`emit_signals` doit être connue à la **clôture de la barre de
  signal** (`arm_ts`). Une feature lue sur la barre de fill (low/high/close final) ou une barre future =
  look-ahead → `@auditor` rétrograde. Cas d'école **fib-v4** : `wick_through_atr` lu sur la bougie de fill —
  et plus profond, l'edge M15 entier s'est révélé un **artefact same-bar** (réfuté sur M1 honnête 2026-06-18).
- **Calibration backtest↔live** (preuve de fidélité, recommandée avant promotion) :
  `python tools/backtest_vs_live.py --date <jour tradé>` réconcilie le backtest M1 aux fills live réels
  (MATCH/DIVERGENCE, Δ = friction). **0 divergence** attendue.
- **Wirage live** (à la promotion seulement, par `@forge`) : `get_<id>_live_signal(...)` reconstruit l'état
  courant via `broker/m1_buffer.py` (pattern `core/opr.py`, `core/ib_retest.py`).

### 7. [@quant discover] — RECOMMANDÉ si 🟡 + n_oos ≥ 100 (moteur de hit-rate)
But : tenter **🟡 → 🟢** via un filtre data-driven. Invoqué par l'orchestrateur (le skill ne spawn pas).
- Pré-requis : `output/<id>/full/trades_v1.csv` + `n_oos_portfolio ≥ 100`.
- `@quant` produit `output/<id>/quant_patch.py` (no-leak, Bonferroni, TimeSeriesSplit, validation WF).
- Appliquer le patch en `vN+1` **seulement si** verdict quant ≥ MEDIUM ET gain PF OOS ≥ +0.2.
- **Si Bonferroni fail (LOW)** → **rollback**, ne pas appliquer, documenter (leçon vwap_pb).

### 8. Artefacts pour l'audit
Produire (deep lane uniquement) :
- `output/<id>/summary.json` (schéma allégé ci-dessous) — input principal de `@auditor`.
- `output/<id>/rapport.md` (~30 lignes, writeup court) — [templates/rapport_template.md](templates/rapport_template.md).
- `output/<id>/full/` : `trades_v1.csv`, `trades_final.csv`, `robustness_<id>.{json,md}` (déjà généré).

Schéma `summary.json` (allégé) :
```json
{
  "strategy_id": "<id>", "version": "<vN>", "iterations": <int>,
  "verdict": "🟢|🟡|🔴",
  "datasets": {"<TICKER>": "sha256:<hex>"},
  "oos": {"pf": <float>, "pl_net": <int>, "n": <int>, "bootstrap": <pct>, "dd": <float>, "wr_pct": <float>},
  "is": {"pf": <float>, "pl_net": <int>, "n": <int>},
  "degradation_is_oos_pct": <float>,
  "stress": {"trending": <pf>, "ranging": <pf>, "macro": <pf>, "vol_h": <pf>, "vol_b": <pf>},
  "robustness": {"bonferroni_ok": <bool>, "psr_0": <float>, "mc_dd_p95": <float>},
  "live_equivalence": {"applicable": <bool>, "path": "M1Buffer|live_eq_script|n/a",
                       "live_eq_pf_oos": <float|null>, "live_signal_function": "<...>|null"},
  "quant_used": <bool>, "quant_verdict": "HIGH|MEDIUM|LOW|null", "quant_filters_applied": [<str>],
  "next_step": "promotion|itération|rejet"
}
```
Helper empreintes : `from core.dataset_hash import snapshot_datasets`.

### 9. @auditor
Invoqué par l'orchestrateur. Lit `summary.json` + `git diff` + `robustness_<id>.json`. Vérifie
look-ahead, frictions, schéma, walk-forward, live-equivalence, cohérence verdict, patch quant.
Autorité finale : peut rétrograder.

### 10. @forge (promotion)
Si **🟢 confirmé par auditor** ET confirmation explicite utilisateur **par fichier**. Modifie
`core/`/`broker/`. Activation progressive : simulation → 1 contrat → sizing nominal.

---

## CAPITALISATION (perf — tous les cas, obligatoire)

Quel que soit le verdict (🟢/🟡/🔴) :
- **1 ligne dans `REGISTRE_HYPOTHESES.md`** : ID, date, source, verdict, spec courte, métriques, **leçon**.
- Mettre à jour le statut de l'idée dans `strategie_futur/BACKLOG.md`.
- Si leçon transversale → entrée mémoire (`feedback_*` ou `project_*`).

---

## PHASE FETCH (conditionnelle — nouvel actif sans CSV)
Si un ticker demandé n'a pas son CSV dans `data/` :
```bash
python -m core.data_fetcher --list           # alias
python -m core.data_fetcher --available       # catalogue ProjectX
python -m core.data_fetcher --symbol <SYM> --timeframe m15 --days <N> --save --ticker <TICKER>
```
Un contrat front-month ≈ 60-90 jours d'historique max. Si < 60 j → stop (data insuffisante).
Si ≥ 60 j → walk-forward adapté (IS = 60 % anciens / OOS = 40 % récents), verdict 🟢 exige n_oos ≥ 30,
documenter la robustesse réduite.

## Charts
`--plot` **uniquement à la demande** ou pour un survivant avant promotion (≤ 10 PNG : 5 winners +
5 losers + equity). Plus de génération massive (l'ancien batch de 122 PNG alimentait le chartist, supprimé).

## Itération
Max 3 versions. Si 🔴 par défaut structurel après v1 → arrêt, pas de p-hacking. Une correction
de paramètre justifie une v2 ; un concept non viable ne se sauve pas par itération.

## Notifications (optionnel)
`python broker/tg_notify.py "MESSAGE_HTML"` (balises `<b>`, `<i>`, `<pre>`, échapper `&`→`&amp;`)
pour suivre depuis le téléphone aux étapes clés.
