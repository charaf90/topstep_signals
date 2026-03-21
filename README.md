# Topstep Signals

Signaux intraday automatisés sur futures micro (MES, NQ, YM) pour le challenge Topstep 50K.

---

## Comment ça marche

Chaque jour à midi (Paris), le système :

1. Analyse les données 15min sur 4 timeframes (15m, H1, H4, D1)
2. Détecte les zones support/résistance (pivots + clustering multi-TF)
3. Évalue la tendance (EMA triple → régime BULL / BEAR / RANGE)
4. Applique les filtres pré-market (MES et NQ uniquement)
5. Génère des ordres limites avec SL et TP fixés
6. Envoie les signaux et graphiques sur Telegram

Les ordres ne sont jamais modifiés après placement. Max 2 trades/jour/actif, risque fixe de $100/trade.

---

## Résultats backtest

**Période : décembre 2024 → mars 2026 (v4 — simulation corrigée)**

| Actif | Trades | WR | PF | P&L | Max DD | $/trade |
|-------|--------|-----|------|---------|--------|---------|
| MES1 | 79 | 33% | 1.28 | +$1,318 | -$1,120 | +$16.7 |
| NQ1 | 127 | 37% | 1.44 | +$2,013 | -$778 | +$15.9 |
| YM1 | 147 | 36% | 0.97 | -$218 | -$1,132 | -$1.5 |
| **Total** | **353** | | | **+$3,113** | | |

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
├── config.py              # Tous les paramètres (SL, RR, zones, tendance, Telegram)
├── signals.py             # Signaux live + envoi Telegram
├── backtest.py            # Backtest historique + audit + graphiques + Telegram
├── core/
│   ├── data.py            # Chargement CSV ou TradingView
│   ├── zones.py           # Détection pivots + clustering zones S/R
│   ├── trend.py           # Score EMA triple + régime BULL/BEAR/RANGE
│   ├── premarket.py       # Features pré-market + filtre
│   ├── strategy.py        # Génération signaux + simulation trades
│   ├── chart.py           # Graphiques OHLC style TradingView
│   └── telegram.py        # Fonctions d'envoi Telegram (texte + images)
├── data/                  # Fichiers CSV 15min (gitignored)
├── output/                # Graphiques et rapports générés (gitignored)
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
