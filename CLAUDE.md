# CLAUDE.md — Guide de session pour topstep_signals

## Rôle de Claude dans ce projet

**Deux rôles distincts selon le contexte :**

### 1. Partenaire de recherche
Développer, backtester et optimiser de nouvelles stratégies de trading. Proposer
des idées, analyser les résultats, prendre des décisions rigoureuses.

### 2. Partenaire de production (live)
Surveiller le daemon live en tmux, analyser les logs Telegram, détecter les
anomalies, vérifier l'état du compte. **En prod : ne jamais modifier un fichier
de production sans confirmation explicite.** La production tourne en permanence.

---

## Vue d'ensemble du projet

**Objectif :** passer le challenge Topstep 50K (micro-contrats MES1, NQ1, YM1)
via des stratégies intraday algorithmiques, puis trader un compte financé.

**Contraintes Topstep :**
- Daily loss max : $1 000
- Trailing drawdown max : $2 000
- Profit target : $3 000

**Portefeuille en production (live depuis 2026-05-05) :**
- **OPR opr-v4** — Opening Range Breakout pullback 9h30 NY
- **Fib fib-v2** — Retracement Fibonacci 38.2 % post-impulse

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
│  PRODUCTION       core/opr.py  core/strategy_fib.py │
│  broker/live_runner.py  — NE PAS TOUCHER             │
└─────────────────────────────────────────────────────┘
```

**Règle absolue :** les fichiers production ne sont modifiés que lors d'une
promotion explicite d'une nouvelle stratégie validée 🟢. Jamais en cours de
recherche.

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
│   ├── analysis_chart.py    # Graphiques journaliers (utilisé par strategies/)
│   └── chart.py             # Graphiques par trade
│
├── broker/                  # ← PRODUCTION — ne pas toucher
│   ├── live_runner.py       # Daemon de session (SessionRunner)
│   ├── projectx_client.py   # API TopstepX
│   └── telegram_bot.py      # Alertes et /status bidirectionnel
│
├── data/                    # gitignored — CSV 15m par ticker
└── output/                  # gitignored — backtests, charts
```

---

## Commandes

### Backtest
```bash
python backtest.py --strategy opr --csv-dir ./data
python backtest.py --strategy fib --csv-dir ./data --ticker NQ1
python backtest.py --strategy all --csv-dir ./data          # opr + fib
python backtest.py --strategy opr --csv-dir ./data --plot   # + 10 charts aléatoires
python backtest.py --strategy opr --live --bars 15000       # données TradingView
```

### Optimisation walk-forward
```bash
python optimize.py --strategy opr --csv-dir ./data
python optimize.py --strategy fib --csv-dir ./data --ticker NQ1
python optimize.py --strategy all --csv-dir ./data
python optimize.py --strategy opr --csv-dir ./data --is-end 2025-09-30
```

### Production (live)
```bash
# Vérifier le daemon tmux
tmux ls
tmux attach -t <session>

# État du live runner
cat state/live_state.json

# Logs
tail -f logs/live.log    # si configuré
```

---

## Pipeline d'une nouvelle stratégie

### 1. Créer `strategies/ma_strategie.py`

```python
STRATEGY_ID = "ict-v1"
TICKERS     = ["MES1", "NQ1", "YM1"]
CSV_SUFFIX  = "_ict"

PARAM_GRID = {
    "sl_mult": [0.5, 1.0, 1.5],
    "tp_mult": [1.0, 2.0, 3.0],
}

def run_backtest(df_15m, ticker, tf=None, params=None, topstep_guard=True):
    """Retourne pd.DataFrame de trades (colonnes standard)."""
    ...

def plot_day(df_15m, ticker, date_str, day_trades, output_path):
    """Optionnel — chart d'une journée pour --plot."""
    ...
```

**Colonnes obligatoires du DataFrame retourné :**
```
date, dir, entry, sl, tp, sl_dist, tp_dist, rr, n_ct,
result (TP|SL|TE|NOT_FILLED), pnl, fill_time, exit_time, exit, regime
```

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
| Bootstrap Topstep | ≥ 80% | ≥ 50% | < 50% |
| Trades OOS | ≥ 20 | ≥ 8 | < 8 |
| P&L OOS | > 0 | > 0 | ≤ 0 |

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

---

## Promotion en production

Uniquement si verdict 🟢 et après validation visuelle des charts :

1. Créer `core/ma_strategie.py` (logique d'exécution live)
2. Ajouter `get_X_live_signal()` utilisable par `live_runner.py`
3. Mettre à jour `broker/live_runner.py` (imports + boucle de session)
4. Mettre à jour `core/signal_selector.py` si nécessaire
5. Tester en simulation (`PROJECTX_LIVE_MODE = False`) avant live réel

---

## Rôle de partenaire live

### Checks à faire en début de session
```bash
# 1. Daemon actif ?
tmux ls

# 2. État du RM (risk manager)
cat state/live_state.json | python -m json.tool

# 3. Trades du jour via Telegram ou logs
# 4. Vérifier que les limites Topstep ne sont pas approchées
```

### Signaux d'alerte à surveiller
- `live_state.json` : `cum_pnl`, `peak_pnl`, `daily_pnl` proches des limites
- Telegram : messages de blocage RM, erreurs API ProjectX, pertes de connexion
- Trades répétés NOT_FILLED → problème de données ou de connectivité
- Sequence de SL consécutifs → vérifier conditions de marché

### Ce que Claude NE fait PAS sans confirmation
- Modifier `broker/live_runner.py` ou tout fichier `core/` en production
- Changer `PROJECTX_LIVE_MODE = True` → `False` (interrompt le live)
- Redémarrer le daemon tmux
- Modifier les params SL/TP en production pendant une session live

---

## Configuration (`config.py`)

**Tout paramètre modifiable doit être dans `config.py`. Jamais hardcodé.**

### Sections clés

| Section | Variables clés |
|---|---|
| Global | `RISK_PER_TRADE_USD=100`, `MAX_TRADES_PER_DAY=2` |
| Utilisateur | `USER_DAILY_LOSS_MAX=200`, `USER_MAX_TRADES_PER_DAY=3` |
| Topstep | `TOPSTEP_DAILY_LOSS_MAX=1000`, `TOPSTEP_TRAILING_DD=2000` |
| Circuit breakers | `CONSEC_LOSS_PAUSE_DAYS=5`, `DAILY_STOP_AFTER_SL=False` |
| OPR | `OPR_SL_ATR_MULT`, `OPR_TP_ATR_MULT`, `OPR_STRATEGY_VERSION` |
| Fib | `FIB_SL_ATR_MULT_PER_TICKER`, `FIB_TP_ATR_MULT_PER_TICKER` |
| Broker | `PROJECTX_LIVE_MODE=False` (simulation), `LIVE_STATE_FILE` |
| Telegram | `TELEGRAM_ENABLED`, niveaux `TELEGRAM_LEVEL_*` |

### Walk-forward IS/OOS (dates fixes pour cohérence)
- **IS :** déc 2024 → sept 2025 (`IS_END = "2025-09-30"`)
- **OOS :** oct 2025 → mars 2026 (`OOS_START = "2025-10-01"`)
- Critère d'acceptation : `OOS PF ≥ 1.2 ET n ≥ 8 ET P&L OOS > 0`

---

## Performances production (référence)

### OPR opr-v4 (déc 2024 → mars 2026)

| Asset | Trades | WR | PF | P&L | DD |
|---|---|---|---|---|---|
| MES1 | 139 | 40% | 1.45 | +$2 339 | -$561 |
| NQ1 | 476 | 46% | 1.65 | +$14 304 | -$866 |
| YM1 | 199 | 60% | 1.92 | +$5 931 | -$502 |
| **Portfolio** | **814** | — | — | **+$22 573** | **-$746** |

Bootstrap portefeuille : **99.8%** ✅

### Fib fib-v2 (déc 2024 → mars 2026)

| Asset | Trades | PF | P&L |
|---|---|---|---|
| MES1 | 114 | 1.78 | +$2 956 |
| NQ1 | 20 | 8.58 | +$2 325 |
| YM1 | 48 | 1.76 | +$1 610 |
| **Portfolio** | **182** | — | **+$6 891** |

Bootstrap portefeuille : **100%** ✅

### OPR + Fib combiné
- P&L : **+$29 464** | DD : **-$756** | Sharpe : **7.01** | Bootstrap : **99.1%**

---

## Conventions de code

- **Langue :** nommage, commentaires, docstrings → **français**
- **Paramètres :** toujours dans `config.py`, jamais hardcodés
- **Timeframes :** toutes les heures OPR en **heure NY** (DST-aware via `OPR_TIMEZONE`)
- **Timestamps :** UTC naïf en interne, conversion NY uniquement dans `core/opr.py`
- **Pas de leak temporel :** ATR et features calculés strictement avant le cutoff
- **Bump de version :** `OPR_STRATEGY_VERSION` ou `FIB_STRATEGY_VERSION` à chaque
  changement structurel de la stratégie (nouveau filtre, nouvelle logique)
- **Charts :** `--plot` génère 10 jours aléatoires parmi les jours avec fills

---

## Pièges à éviter

- Ne **jamais** modifier `core/opr.py` ou `core/strategy_fib.py` pour de la recherche
  → utiliser `strategies/opr.py` et `strategies/fib.py`
- Ne **jamais** accepter des params walk-forward avec OOS PF < 1.2
- Données CSV : `{csv_dir}/{TICKER}_data_m15.csv` (majuscule)
- `YM1_ENABLED = False` dans `core/opr.py` — ne pas activer sans preuve OOS
- Après changement de `config.py`, vérifier que `core/opr.py` reflète bien les
  valeurs (il importe les dicts par référence et les patch dynamiquement)
- Le bootstrap par ticker seul peut être bas (ex: MES1 5.8%) — ce qui compte
  c'est le **bootstrap portefeuille** (tous actifs agrégés)
- `data/` et `output/` sont gitignorés — ne pas versionner de données de marché
