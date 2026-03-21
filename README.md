# Topstep Signals — Ordres Limites Intraday

Stratégie automatisée d'ordres limites sur futures micro (MES, NQ, YM) pour Topstep 50K.

## Principe

Chaque jour à midi (Paris), le système identifie des zones S/R multi-timeframe, vérifie la tendance et le contexte pré-market, puis génère des ordres limites avec SL et TP fixés. Les ordres ne sont jamais modifiés après placement.

## Résultats backtest (déc 2024 → mars 2026)

| Actif | Trades | WR | PF | P&L | Max DD |
|-------|--------|-----|------|---------|--------|
| MES1 (RR=1.5) | 110 | 58% | 1.92 | +$3,158 | -$253 |
| NQ1 (RR=2.0) | 140 | 56% | 2.35 | +$5,761 | -$405 |
| YM1 (RR=1.5) | 236 | 54% | 1.73 | +$6,634 | -$505 |

## Installation

```bash
pip install -r requirements.txt
```

## Usage

### Signaux live → Telegram
```bash
python signals.py                           # Live TradingView + envoi Telegram
python signals.py --dry-run                 # Live sans envoyer
python signals.py --csv-dir ./data          # Depuis fichiers CSV
python signals.py --date 2026-01-29 --csv-dir ./data      # Simuler une date passée
```

### Backtest
```bash
python backtest.py --csv-dir ./data                    # 3 actifs
python backtest.py --csv-dir ./data --ticker NQ1       # 1 actif
python backtest.py --csv-dir ./data --plot             # Avec graphiques
```

## Structure

```
topstep-signals/
├── config.py           # Paramètres (SL, RR, zones, tendance, Telegram)
├── signals.py          # Générateur de signaux + envoi Telegram
├── backtest.py         # Backtest historique avec audit
├── core/
│   ├── data.py         # Chargement CSV ou TradingView
│   ├── zones.py        # Détection pivots + clustering zones S/R
│   ├── trend.py        # Score EMA triple + régime BULL/BEAR/RANGE
│   ├── premarket.py    # Features pré-market + filtre
│   ├── strategy.py     # Génération signaux + simulation trades
│   └── chart.py        # Graphiques style TradingView
├── requirements.txt
└── .gitignore
```

## Données requises

Fichiers CSV 15 minutes avec colonnes : `datetime, symbol, open, high, low, close, volume`

Nommage : `MES1_data_m15.csv`, `NQ1_data_m15.csv`, `YM1_data_m15.csv`

## Telegram

Au premier lancement, envoyer `/start` au bot `@MyTopStep_bot`. Le chat_id est sauvegardé automatiquement dans `.chat_id`.

## Lancement automatique (cron)

```bash
# Chaque jour à 11h UTC (midi Paris)
0 11 * * 1-5 cd /path/to/topstep-signals && python signals.py >> logs/signals.log 2>&1
```
