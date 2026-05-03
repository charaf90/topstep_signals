# Topstep Signals

Laboratoire de stratégies intraday sur futures micro (MES, NQ, YM) pour le
challenge Topstep 50K. Trois stratégies indépendantes coexistent en parallèle
dans le projet — backtests + optimisation walk-forward, en attendant
l'intégration broker (ProjectX) qui fera l'exécution automatisée.

> **Branche V6** — la couche d'envoi de signaux Telegram et le runner live
> (`signals.py`) ont été supprimés pour concentrer le projet sur la
> recherche de stratégies et le backtest. L'exécution automatique sur
> Topstep passera par l'API ProjectX (à venir, hors de ce dépôt pour
> l'instant).

---

## Trois stratégies

| Stratégie | Module | Logique | Status |
|-----------|--------|---------|--------|
| **Composite** | `core/strategy.py` | Trade de zones S/R multi-TF + score composite | v5.2 |
| **OPR** | `core/opr.py` | Opening Range Breakout (pullback à 9h30 NY) | opr-v4 |
| **Fib** | `core/strategy_fib.py` | Retracement Fibonacci 50% post-impulse | fib-v1 |

Chaque stratégie applique son propre garde-fou Topstep (`risk_topstep.py`).
Sizing risque dollar fixe ($100/trade) commun aux trois.

---

## Résultats backtest portefeuille

**Période :** décembre 2024 → mars 2026
**Comparaison de toutes les combinaisons** (`compare_portfolios.py`) :

| Combinaison | Trades | P&L | Max DD | Sharpe | Bootstrap |
|-------------|--------|------|---------|--------|-----------|
| Composite seul | 142 | +$3,728 | -$1,500 | 4.05 | 100% |
| OPR seul | 814 | +$22,573 | -$746 | 6.45 | 99.9% |
| Fib seul | 128 | +$3,600 | -$467 | 5.29 | 100% |
| Composite + OPR | 956 | +$26,302 | -$852 | 6.46 | 98.5% |
| Composite + Fib | 270 | +$7,329 | -$1,701 | 4.79 | 100% |
| OPR + Fib | 942 | +$26,174 | -$924 | 6.90 | 99.6% |
| **Composite + OPR + Fib** | **1 084** | **+$29,902** | **-$845** | **6.90** | **99.5%** |

**Recommandation production : triplet (Composite + OPR + Fib).**
Maximise P&L (+$29,902) avec le DD le plus bas parmi les portefeuilles
multi-stratégies (-$845), Sharpe annualisé 6.90, bootstrap Topstep 99.5%.

---

## Installation

```bash
pip install -r requirements.txt
```

Données requises pour le mode CSV : fichiers 15min nommés
`MES1_data_m15.csv`, `NQ1_data_m15.csv`, `YM1_data_m15.csv` dans `data/`.

Format CSV : `datetime, symbol, open, high, low, close, volume`

Le mode `--live` télécharge directement depuis TradingView via
[`tvDatafeed`](https://github.com/rongardF/tvdatafeed) (déjà épinglé dans
`requirements.txt`) et n'a pas besoin de fichier local.

---

## Usage

### Backtest

```bash
# 3 stratégies sur 3 actifs (par défaut)
python backtest.py --csv-dir ./data

# Stratégie unique
python backtest.py --csv-dir ./data --strategy composite
python backtest.py --csv-dir ./data --strategy opr
python backtest.py --csv-dir ./data --strategy fib

# Legacy : composite + OPR uniquement (pré-Fib)
python backtest.py --csv-dir ./data --strategy both

# Actif unique
python backtest.py --csv-dir ./data --ticker NQ1
```

### Comparaison des portefeuilles (post-backtest)

```bash
python backtest.py --csv-dir ./data --strategy all   # 1. génère les CSVs
python compare_portfolios.py                          # 2. évalue les 7 combos
```

### Backtest sur données TradingView récentes

```bash
python backtest.py --live                       # 10 000 bougies par actif
python backtest.py --live --bars 20000 --ticker NQ1
```
`--csv-dir` et `--live` sont mutuellement exclusifs.

### Backtest avec graphiques par trade

```bash
python backtest.py --csv-dir ./data --plot                          # Tous trades
python backtest.py --csv-dir ./data --plot --plot-filter sl         # SL uniquement
python backtest.py --csv-dir ./data --plot --plot-filter win        # Gagnants
```

Les graphiques d'analyse journaliers (1 PNG / jour / actif) sont générés
par défaut sous `output/analysis_charts/{STRATEGY_VERSION}/{TICKER}/`.
Désactivable avec `--no-analysis-charts`.

### Optimisations walk-forward

```bash
python optimize.py --csv-dir ./data         # Composite Phase A/B/C (heures)
python run_phase_c.py --csv-dir ./data      # Phase C composite seule
python optimize_opr.py --csv-dir ./data     # OPR (SL/TP × ATR)
python optimize_zones.py --csv-dir ./data   # Détection des zones (TOL × MW × REC)
```

Pour Fib, l'optimisation se fait dans `draft_fibo_50/` (sandbox de
recherche conservée pour traçabilité) :
```bash
cd draft_fibo_50/
python optimize.py --csv-dir ../data        # SL × TP × IMP
python optimize_pivot.py --csv-dir ../data  # PIVOT_LEFT/RIGHT
python analyze_filters.py                   # Filtres trigger walk-forward
```

---

## Structure du projet

```
topstep_signals/
├── config.py              # Tous paramètres (composite v5.2, opr-v4, fib-v1, Topstep)
├── backtest.py            # Backtest 3 stratégies + validate_topstep (bootstrap)
├── compare_portfolios.py  # Évalue les 7 combinaisons {Composite, OPR, Fib}
├── optimize.py            # Walk-forward composite (Phase A/B/C)
├── optimize_opr.py        # Walk-forward OPR (SL/TP)
├── optimize_zones.py      # Walk-forward zones S/R (TOL × MW × REC)
├── run_phase_c.py         # Phase C composite seule
├── core/
│   ├── data.py            # Chargement CSV ou TradingView
│   ├── zones.py           # Détection pivots + clustering zones S/R
│   ├── trend.py           # Score EMA triple + alignment_score (composite)
│   ├── premarket.py       # Features pré-market + filtre
│   ├── scoring.py         # Score composite 0-100 + features volatilité ATR
│   ├── risk_topstep.py    # Garde-fou slack journalier / trailing DD
│   ├── strategy.py        # Composite — génération signaux + simulation
│   ├── opr.py             # OPR (PineScript pullback) — opr-v4 + filtres trigger
│   ├── strategy_fib.py    # Fib retracement 50% — fib-v1 + filtres trigger
│   ├── chart.py           # Graphiques OHLC style TradingView (par trade)
│   └── analysis_chart.py  # Graphique d'analyse journalier
├── analyse/               # Sandbox d'analyse OPR opr-v4 (filtres trigger)
├── draft_fibo_50/         # Sandbox de développement Fib (conservé en référence)
├── data/                  # CSV 15min (gitignored)
├── output/                # Graphiques + rapports générés (gitignored)
├── CHECKPOINTS_SUMMARY.md # Historique versions composite
├── CLAUDE.md              # Guide projet pour agents IA
└── requirements.txt
```

---

## Roadmap V6

1. ✅ **Cleanup** : suppression de `signals.py` et de la stack Telegram.
2. ✅ **OPR opr-v3** : SL/TP en multiplicateurs ATR journalier 14j.
3. ✅ **OPR opr-v4** : ajout filtres trigger walk-forward (PF +0.46 sur MES1, etc.).
4. ✅ **Fib fib-v1** : retracement Fibonacci 50% intégré (Sharpe OOS 5.10).
5. ✅ **Évaluation portefeuilles 3 stratégies** : triplet recommandé en
   production (P&L +$29,902, DD -$845, Sharpe 6.90, bootstrap 99.5%).
6. ⏳ **Intégration API ProjectX** pour exécution automatisée sur Topstep.

Voir `CLAUDE.md` pour le détail technique de chaque stratégie.
