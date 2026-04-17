# Topstep Signals

Signaux intraday automatisés sur futures micro (MES, NQ, YM) pour le challenge Topstep 50K.

---

## Comment ça marche

Chaque jour à midi (Paris), le système :

1. Analyse les données 15min sur 4 timeframes (15m, H1, H4, D1)
2. Détecte les zones support/résistance (pivots + clustering multi-TF)
3. Évalue la tendance (EMA triple → régime BULL / BEAR / RANGE + alignment_score)
4. Calcule les features pré-market + volatilité (ATR journalier, gap, overnight range)
5. Score composite 0-100 (zone 40% / trend 25% / pm 20% / vol 15%) — filtrage
   ultra-sélectif par actif (`COMPOSITE_SCORE_MIN`)
6. Génère des ordres limites avec SL et TP fixés (risque fixe $100/trade)
7. Envoie les signaux et graphiques sur Telegram

**Garde-fous Topstep** : refus de trade si le slack journalier ou trailing
DD ne couvre pas le risque nominal × 1.1.

**Circuit breakers intra-jour** :
- `CONSEC_LOSS_PAUSE_DAYS=5` — pause 1 jour après 5 jours perdants consécutifs.

Les ordres ne sont jamais modifiés après placement. Max 2 trades/jour/actif.

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

Données requises : fichiers CSV 15min nommés `MES1_data_m15.csv`, `NQ1_data_m15.csv`, `YM1_data_m15.csv` dans un dossier `data/`.

Format CSV : `datetime, symbol, open, high, low, close, volume`

---

## Usage

### Signaux live → Telegram

```bash
python signals.py                                          # Live TradingView + Telegram
python signals.py --dry-run                                # Live sans envoyer
python signals.py --csv-dir ./data                         # Depuis fichiers CSV
python signals.py --date 2026-01-29 --csv-dir ./data       # Simuler une date passée
```

### Backtest

```bash
python backtest.py --csv-dir ./data                        # 3 actifs
python backtest.py --csv-dir ./data --ticker NQ1           # 1 actif
```

### Backtest avec graphiques

Chaque trade rempli génère un graphique OHLC avec zones S/R, niveaux SL/TP, marqueurs d'entrée/sortie et résultat.

```bash
python backtest.py --csv-dir ./data --plot                           # Tous les trades
python backtest.py --csv-dir ./data --plot --plot-filter sl          # Trades SL uniquement
python backtest.py --csv-dir ./data --plot --plot-filter win         # Trades gagnants
python backtest.py --csv-dir ./data --ticker MES1 --plot --plot-filter loss  # Perdants MES1
```

Filtres disponibles : `all`, `tp`, `sl`, `te`, `win`, `loss`

Les graphiques sont sauvegardés dans `output/backtest_charts/{TICKER}/`.

### Backtest avec envoi Telegram

```bash
python backtest.py --csv-dir ./data --telegram                      # Rapport + tous les graphiques
python backtest.py --csv-dir ./data --ticker NQ1 --telegram         # 1 actif sur Telegram
python backtest.py --csv-dir ./data --telegram --plot-filter sl     # Uniquement les SL
```

`--telegram` active automatiquement `--plot`. Le bot envoie un résumé HTML (stats par ticker) suivi de chaque graphique avec caption.

---

## Structure du projet

```
topstep_signals/
├── config.py              # Tous les paramètres (SL, RR, composite, breakers, Topstep)
├── signals.py             # Signaux live + envoi Telegram
├── backtest.py            # Backtest + validate_topstep (bootstrap 1000 perms)
├── optimize.py            # Walk-forward IS/OOS (Phase A/B/C)
├── run_phase_c.py         # Calibration composite seule (Phase C uniquement)
├── core/
│   ├── data.py            # Chargement CSV ou TradingView
│   ├── zones.py           # Détection pivots + clustering zones S/R
│   ├── trend.py           # Score EMA triple + alignment_score
│   ├── premarket.py       # Features pré-market + filtre
│   ├── scoring.py         # Score composite 0-100 + features volatilité ATR
│   ├── risk_topstep.py    # Garde-fou slack journalier / trailing DD
│   ├── strategy.py        # Génération signaux (filtrage composite) + simulation
│   ├── chart.py           # Graphiques OHLC style TradingView
│   └── telegram.py        # Fonctions d'envoi Telegram (texte + images)
├── data/                  # Fichiers CSV 15min (gitignored)
├── output/                # Graphiques et rapports générés (gitignored)
├── CHECKPOINTS_SUMMARY.md # Historique v1 → v5.2 + résultats par version
├── CLAUDE.md              # Guide projet pour agents IA
├── requirements.txt
└── .gitignore
```

---

## Configuration Telegram

Au premier lancement, envoyer `/start` au bot `@MyTopStep_bot`. Le chat_id est sauvegardé automatiquement dans `.chat_id`.

Les identifiants du bot sont dans `config.py` (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`).

---

## Lancement automatique (cron)

```bash
# Chaque jour à 11h UTC (midi Paris), du lundi au vendredi
0 11 * * 1-5 cd /path/to/topstep_signals && python signals.py >> logs/signals.log 2>&1
```
