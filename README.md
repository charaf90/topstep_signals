# Topstep Signals

Laboratoire de stratégies intraday sur futures micro (MES, NQ, YM) pour le
challenge Topstep 50K. Deux stratégies de production coexistent en parallèle :
**OPR** (Opening Range Breakout) et **Fib** (retracement Fibonacci 38.2 %).
Le Composite (zones S/R) reste disponible pour la recherche.

> **Statut V6** — le projet est en phase backtest/optimisation. L'exécution
> automatique sur Topstep passera par l'API ProjectX (à venir).

---

## Portefeuille de production

**OPR + Fib** — recommandation retenue après évaluation walk-forward complète.

| Métrique | OPR seul | Fib seul | **OPR + Fib** |
|---|---|---|---|
| Trades (Dec 2024 → Mar 2026) | 814 | 182 | 996 |
| P&L | +$22,573 | +$6,891 | **+$29,464** |
| Max trailing DD | -$746 | -$494 | **-$756** |
| Sharpe annualisé | 6.45 | 5.13 | **7.01** |
| Bootstrap Topstep | 99.9% | 100% | **99.1%** |

Sharpe 7.01 le plus élevé de toutes les combinaisons testées, DD bien contenu
(-$756 sur une limite Topstep de $2,000), bootstrap excellent.

---

## Deux stratégies de production

| Stratégie | Module | Logique | Version |
|---|---|---|---|
| **OPR** | `core/opr.py` | Opening Range Breakout pullback à 9h30 NY | opr-v4 |
| **Fib** | `core/strategy_fib.py` | Retracement Fibonacci 38.2 % post-impulse | fib-v2 |

Chaque stratégie applique son propre garde-fou Topstep (`risk_topstep.py`).
En production multi-stratégies, le `PortfolioRiskManager` (`core/risk_portfolio.py`)
gère la vue globale : cap 3 fills/jour, $200 perte journalière réalisée max.

Sizing risque dollar fixe ($100/trade) commun aux deux.

---

## Installation

```bash
pip install -r requirements.txt
```

Données requises : fichiers 15min dans `data/` nommés
`MES1_data_m15.csv`, `NQ1_data_m15.csv`, `YM1_data_m15.csv`.

Format CSV : `datetime, symbol, open, high, low, close, volume`

---

## Usage

### Backtest production (OPR + Fib — défaut)

```bash
python backtest.py --csv-dir ./data                  # 3 actifs, OPR+Fib
python backtest.py --csv-dir ./data --ticker NQ1     # Actif unique
python backtest.py --csv-dir ./data --no-analysis-charts  # Sans charts journaliers
```

### Backtests par stratégie

```bash
python backtest.py --csv-dir ./data --strategy opr        # OPR uniquement
python backtest.py --csv-dir ./data --strategy fib        # Fib uniquement
python backtest.py --csv-dir ./data --strategy composite  # Composite (recherche)
python backtest.py --csv-dir ./data --strategy all        # Les 3 stratégies
```

### Analyse garde-fou portefeuille global

```bash
python replay_portfolio_risk.py   # Compare scénario sans/avec sélection actif corrélé
```

### Optimisations walk-forward

```bash
# OPR (SL/TP × ATR — production)
python optimize_opr.py --csv-dir ./data

# Fib (SL × TP × IMP — sandbox isolée)
cd draft_fibo_50/
python optimize.py --csv-dir ../data          # SL × TP × IMP par ticker
python optimize_pivot.py --csv-dir ../data    # PIVOT_LEFT/RIGHT
python analyze_filters_v2.py                  # Filtres trigger walk-forward

# OPR filtres trigger (recalibration)
cd analyse/
python 01_extract_features.py --csv-dir ../data
python 03_filter_backtest.py
```

### Backtest avec graphiques par trade

```bash
python backtest.py --csv-dir ./data --plot
python backtest.py --csv-dir ./data --plot --plot-filter sl   # SL uniquement
```

---

## Structure du projet

```
topstep_signals/
├── config.py              # Tous paramètres (OPR opr-v4, Fib fib-v2, Topstep)
├── backtest.py            # Backtest — défaut : OPR+Fib (opr_fib)
├── optimize.py            # Walk-forward Composite (recherche)
├── optimize_opr.py        # Walk-forward OPR (SL/TP × ATR)
├── replay_portfolio_risk.py  # Analyse garde-fou portefeuille global
├── core/
│   ├── data.py            # Chargement CSV
│   ├── zones.py           # Détection zones S/R (Composite)
│   ├── trend.py           # Score EMA triple (Composite)
│   ├── premarket.py       # Features pré-market (Composite)
│   ├── scoring.py         # Score composite 0-100 (Composite)
│   ├── risk_topstep.py    # Garde-fou per-stratégie (daily / trailing DD)
│   ├── risk_portfolio.py  # Garde-fou global portefeuille (live mode)
│   ├── signal_selector.py # Sélection actif prioritaire (corrélation)
│   ├── strategy.py        # Composite (recherche)
│   ├── opr.py             # OPR — production
│   ├── strategy_fib.py    # Fib 38.2 % — production
│   ├── chart.py           # Graphiques par trade
│   └── analysis_chart.py  # Graphique d'analyse journalier
├── analyse/               # Recalibration filtres trigger OPR
├── draft_fibo_50/         # Recalibration et optimisation Fib
├── data/                  # CSV 15min (gitignored)
└── output/                # Graphiques + rapports (gitignored)
```

---

## Roadmap V6

1. ✅ Cleanup Telegram + `signals.py`
2. ✅ OPR opr-v3 : SL/TP basés ATR journalier
3. ✅ OPR opr-v4 : filtres trigger walk-forward
4. ✅ Fib fib-v1 : retracement Fibonacci 50 %
5. ✅ Fib fib-v2 : niveau 38.2 % + filtres trigger re-calibrés
6. ✅ Évaluation portefeuilles → **OPR + Fib retenu en production**
7. ✅ Garde-fou portefeuille global (`PortfolioRiskManager`)
8. ✅ Sélecteur actif corrélé (`signal_selector.py`)
9. ⏳ **Intégration API ProjectX** — exécution automatisée sur Topstep
