# Topstep Signals

Laboratoire de stratégies intraday sur futures micro (MES, NQ, YM) pour le
challenge Topstep 50K. Backtests + optimisation walk-forward, en attendant
l'intégration broker (ProjectX) qui fera l'exécution automatisée.

> **Branche V6** — la couche d'envoi de signaux Telegram et le runner live
> (`signals.py`) ont été supprimés pour concentrer le projet sur la
> recherche de stratégies et le backtest. L'exécution automatique sur
> Topstep passera par l'API ProjectX (à venir, hors de ce dépôt pour
> l'instant).

---

## Comment ça marche

Pipeline d'analyse appliqué jour par jour dans `backtest.py` :

1. Charge les données 15min (CSV ou TradingView via `--live`) et resample
   sur 4 timeframes (15m, H1, H4, D1).
2. Détecte les zones support/résistance (pivots + clustering multi-TF).
3. Évalue la tendance (EMA triple → régime BULL / BEAR / RANGE +
   `alignment_score`).
4. Calcule les features pré-market + volatilité (ATR journalier, gap,
   overnight range).
5. Score composite 0-100 (zone 40% / trend 25% / pm 20% / vol 15%) —
   filtrage ultra-sélectif par actif (`COMPOSITE_SCORE_MIN`).
6. Génère les signaux composite + OPR (PineScript pullback à 9h30 NY) en
   parallèle, avec SL/TP fixés et risque fixe $100/trade.

**Garde-fous Topstep** : refus de trade si le slack journalier ou
trailing DD ne couvre pas le risque nominal × 1.1.

**Circuit breakers intra-jour** :
- `CONSEC_LOSS_PAUSE_DAYS=5` — pause 1 jour après 5 jours perdants consécutifs.

Les ordres ne sont jamais modifiés après placement. Max 2 trades/jour/actif
(composite). Une seule position à la fois côté OPR.

---

## Résultats backtest

**Période : décembre 2024 → mars 2026 (v5.2 — score composite + circuit breakers)**

Portefeuille 3 actifs (MES1 + NQ1 + YM1 désactivé) :

| Métrique | Valeur | Limite Topstep |
|---|---|---|
| P&L total | **+$3,728** | target +$3,000 ✅ |
| Perte jour max | -$296 | -$1,000 (marge 70%) ✅ |
| Trailing DD | -$1,500 | -$2,000 (marge 25%) ✅ |
| Bootstrap pass rate | **100%** | ≥ 80% ✅ |
| Jours tradés | 91 (55% gagnants) | — |

Détail par actif :

| Actif | Trades | WR | PF | P&L | Max DD | Statut |
|-------|--------|-----|------|---------|--------|--------|
| MES1 | 47 | 34% | 1.39 | +$1,078 | -$1,030 | actif |
| NQ1  | 95 | 42% | 1.87 | +$2,651 | -$632  | actif (calibré Phase C) |
| YM1  | 0  | —   | —    | 0       | —      | **désactivé** (OOS PF 0.73 < 1.2) |

Voir `CHECKPOINTS_SUMMARY.md` pour l'historique complet v1 → v5.2.

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

### Backtest sur CSV locaux (par défaut)

```bash
python backtest.py --csv-dir ./data                        # 3 actifs, composite + OPR
python backtest.py --csv-dir ./data --ticker NQ1           # 1 actif
python backtest.py --csv-dir ./data --strategy opr         # OPR seul
python backtest.py --csv-dir ./data --strategy composite   # Composite seul
```

### Backtest sur données TradingView récentes

`--live` récupère les bougies 15m fraîches via TradingView au lieu de lire
un CSV local. Pratique pour tester un nouvel actif ou rejouer la dernière
semaine sans extraire de CSV.

```bash
python backtest.py --live                                  # 10 000 bougies par actif
python backtest.py --live --bars 20000 --ticker NQ1        # Profondeur custom
```

`--csv-dir` et `--live` sont mutuellement exclusifs (un des deux est requis).

### Backtest avec graphiques par trade

```bash
python backtest.py --csv-dir ./data --plot                           # Tous les trades
python backtest.py --csv-dir ./data --plot --plot-filter sl          # Trades SL uniquement
python backtest.py --csv-dir ./data --plot --plot-filter win         # Trades gagnants
python backtest.py --csv-dir ./data --ticker MES1 --plot --plot-filter loss  # Perdants MES1
```

Filtres disponibles : `all`, `tp`, `sl`, `te`, `win`, `loss`. Sortie :
`output/backtest_charts/{TICKER}/`.

Les graphiques d'analyse journaliers (1 PNG / jour / actif) sont générés
par défaut sous `output/analysis_charts/{STRATEGY_VERSION}/{TICKER}/`. Désactivable
avec `--no-analysis-charts`.

### Optimisation walk-forward

```bash
python optimize.py --csv-dir ./data         # Phase A/B/C composite (multi-heures)
python run_phase_c.py --csv-dir ./data      # Phase C seule (seuils composite)
python optimize_opr.py --csv-dir ./data     # Stratégie OPR (SL/TP en points)
```

---

## Structure du projet

```
topstep_signals/
├── config.py              # Tous les paramètres (SL, RR, composite, breakers, Topstep, OPR)
├── backtest.py            # Backtest + validate_topstep (bootstrap 1000 perms)
├── optimize.py            # Walk-forward IS/OOS (Phase A/B/C composite)
├── optimize_opr.py        # Walk-forward OPR (SL/TP en points)
├── run_phase_c.py         # Calibration composite seule (Phase C uniquement)
├── core/
│   ├── data.py            # Chargement CSV ou TradingView (fetch_live)
│   ├── zones.py           # Détection pivots + clustering zones S/R
│   ├── trend.py           # Score EMA triple + alignment_score
│   ├── premarket.py       # Features pré-market + filtre
│   ├── scoring.py         # Score composite 0-100 + features volatilité ATR
│   ├── risk_topstep.py    # Garde-fou slack journalier / trailing DD
│   ├── strategy.py        # Génération signaux composite (filtrage) + simulation
│   ├── opr.py             # Stratégie OPR (PineScript pullback) — opr-v2
│   ├── chart.py           # Graphiques OHLC style TradingView (par trade)
│   └── analysis_chart.py  # Graphique d'analyse journalier (1 PNG / jour / actif)
├── data/                  # Fichiers CSV 15min (gitignored)
├── output/                # Graphiques et rapports générés (gitignored)
├── CHECKPOINTS_SUMMARY.md # Historique v1 → v5.2 + résultats par version
├── CLAUDE.md              # Guide projet pour agents IA
├── requirements.txt
└── .gitignore
```

---

## Roadmap V6

1. Passer le SL/TP de la stratégie OPR de **distances fixes en points** à
   des **distances basées sur l'ATR** (multiplicateurs ATR par actif).
2. Ré-optimiser les multiplicateurs en walk-forward via `optimize_opr.py`
   adapté à la nouvelle paramétrisation.
3. Quand les stratégies sont figées : intégrer l'**API ProjectX** pour
   l'exécution automatisée des ordres sur Topstep.
