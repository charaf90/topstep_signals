# CLAUDE.md — AI Assistant Guide for topstep_signals

## Project Overview

`topstep_signals` is a **research-grade intraday trading strategy lab** for
futures micro-contracts (MES1, NQ1, YM1), designed for the Topstep 50K
funded-account challenge. It performs multi-timeframe technical analysis,
backtests strategies, and produces daily analysis charts. The end goal is a
**fully automated execution loop on Topstep via the ProjectX API** — that
piece is **not implemented yet** : the focus right now is to lock the
strategies down via backtests, then bolt the broker layer on top.

The codebase is **pure Python**, file-based (no database), CLI-driven.
Documentation and variable names are **primarily in French**.

> **V6 cleanup** : le pipeline live (`signals.py`) et la stack Telegram
> ont été supprimés. `backtest.py` et `optimize*.py` sont les seuls points
> d'entrée. La stratégie Composite reste dans le code pour la recherche
> mais **n'est plus dans le portefeuille de production**.

## Portefeuille de production : OPR + Fib

Décision walk-forward (Dec 2024 → Mar 2026) : **OPR opr-v4 + Fib fib-v2**.

| Métrique | OPR seul | Fib seul | **OPR + Fib** |
|---|---|---|---|
| P&L | +$22,573 | +$6,891 | **+$29,464** |
| Max trailing DD | -$746 | -$494 | **-$756** |
| Sharpe | 6.45 | 5.13 | **7.01** |
| Bootstrap | 99.9% | 100% | **99.1%** |

Sharpe 7.01 le plus élevé de toutes les combinaisons. Le Composite reste
disponible via `--strategy composite` pour la recherche mais n'est pas activé
en production : DD standalone -$1,500 trop proche de la limite Topstep $2K.

**Garde-fou global (live mode) :** `core/risk_portfolio.PortfolioRiskManager`
— cap 3 fills/jour, $200 perte journalière réalisée, Topstep slack daily+trailing.
**Sélection actif corrélé :** `core/signal_selector.py` — quand OPR ou Fib
déclenche simultanément sur plusieurs tickers, seul le mieux classé OOS est retenu
(NQ1 > YM1 > MES1 pour OPR et Fib). Les ordres limites armés ne comptent pas
dans le cap journalier — seuls les fills confirmés sont comptabilisés.

**Deux stratégies actives :**
- **OPR opr-v4** (`core/opr.py`) — Opening Range Breakout pullback à 9h30 NY
- **Fib fib-v2** (`core/strategy_fib.py`) — Retracement Fibonacci 38.2 % post-impulse

**Composite v5.2** (`core/strategy.py`) — conservé pour la recherche uniquement.
YM1 désactivé pour Composite (`YM1_ENABLED=False`).

**Roadmap V6 — état actuel :**
1. ✅ Cleanup Telegram + `signals.py`.
2. ✅ OPR opr-v3 : SL/TP → multiplicateurs ATR journalier.
3. ✅ OPR opr-v4 : filtres trigger walk-forward.
4. ✅ Fib fib-v2 : niveau 38.2 % + filtres trigger re-calibrés.
5. ✅ Évaluation portefeuilles → **OPR + Fib retenu**.
6. ✅ Garde-fou portefeuille global (`PortfolioRiskManager` + `signal_selector`).
7. ⏳ **Intégration API ProjectX** — exécution automatisée sur Topstep.

---

## Repository Structure

```
topstep_signals/
├── config.py                  # Tous les paramètres (OPR opr-v4, Fib fib-v2, Topstep)
├── backtest.py                # Backtest — défaut : opr_fib (OPR+Fib production)
├── optimize_opr.py            # Walk-forward OPR (SL/TP × ATR, par actif)
├── optimize.py                # Walk-forward Composite (recherche uniquement)
├── replay_portfolio_risk.py   # Analyse garde-fou global portefeuille
├── requirements.txt
├── core/
│   ├── data.py                # Chargement CSV
│   ├── opr.py                 # OPR — PRODUCTION
│   ├── strategy_fib.py        # Fib 38.2 % — PRODUCTION
│   ├── risk_topstep.py        # Garde-fou per-stratégie (per-ticker)
│   ├── risk_portfolio.py      # Garde-fou global portefeuille (live mode)
│   ├── signal_selector.py     # Sélection actif corrélé (NQ1 > YM1 > MES1)
│   ├── strategy.py            # Composite (recherche uniquement)
│   ├── zones.py               # Zones S/R (Composite)
│   ├── trend.py               # Score EMA triple (Composite)
│   ├── premarket.py           # Features pré-market (Composite)
│   ├── scoring.py             # Score composite 0-100 (Composite)
│   ├── chart.py               # Graphiques par trade
│   └── analysis_chart.py      # Graphique d'analyse journalier (1 PNG/jour/ticker)
├── analyse/                   # Recalibration filtres trigger OPR
├── draft_fibo_50/             # Recalibration et optimisation Fib (sandbox)
│   ├── optimize.py            # Walk-forward SL × TP × IMP
│   ├── optimize_pivot.py      # Walk-forward PIVOT_LEFT/RIGHT
│   └── analyze_filters_v2.py  # Filtres trigger walk-forward fib-v2
└── data/                      # gitignored
```

**data/ et output/ sont gitignorés.** Pas de `signals.py`, pas de Telegram.

---

## Technology Stack

- **Language:** Python 3.7+
- **Key libraries:** pandas, numpy, matplotlib, requests, tvdatafeed (custom fork)
- **Data sources:** Local CSV files (default) or TradingView (`backtest.py --live`)
- **No framework, no database, no build system, no notification layer**
- **Future:** ProjectX API for automated order execution on Topstep (not yet wired)

Install dependencies:
```bash
pip install -r requirements.txt
```

---

## Running the Project

The project runs only in research mode — backtests and walk-forward
optimizations. No live signal generation, no Telegram, no broker layer yet.

### Backtesting
```bash
python backtest.py --csv-dir ./data                       # OPR+Fib, 3 actifs (défaut)
python backtest.py --csv-dir ./data --ticker NQ1          # Actif unique
python backtest.py --csv-dir ./data --plot                # + graphiques par trade
python backtest.py --csv-dir ./data --strategy opr        # OPR uniquement
python backtest.py --csv-dir ./data --strategy fib        # Fib uniquement
python backtest.py --csv-dir ./data --strategy composite  # Composite (recherche)
python backtest.py --csv-dir ./data --strategy all        # 3 stratégies (recherche)
python replay_portfolio_risk.py                           # Analyse garde-fou global
```

### Backtesting (TradingView live data)
Use `--live` to fetch fresh 15m bars from TradingView (`tvDatafeed`) instead
of reading local CSV. Useful to backtest on the most recent data or to try
a new ticker without dumping a CSV first.
```bash
python backtest.py --live                              # Default: 10000 bars per ticker
python backtest.py --live --bars 20000 --ticker NQ1    # Deeper history, single asset
```
`--csv-dir` and `--live` are mutually exclusive ; one of them is required.

### Walk-forward optimization
```bash
# Production — OPR
python optimize_opr.py --csv-dir ./data       # SL/TP × ATR (par actif)

# Production — Fib (sandbox isolée)
cd draft_fibo_50/
python optimize.py --csv-dir ../data          # SL × TP × IMP par ticker
python optimize_pivot.py --csv-dir ../data    # PIVOT_LEFT/RIGHT
python analyze_filters_v2.py                  # Filtres trigger walk-forward fib-v2

# Recalibration filtres trigger OPR
cd analyse/
python 01_extract_features.py --csv-dir ../data
python 03_filter_backtest.py

# Composite (recherche uniquement)
python optimize.py --csv-dir ./data           # Phase A/B/C (multi-heure)
```

### Output
- Daily analysis charts: `./output/analysis_charts/{STRATEGY_VERSION}/{TICKER}/{YYYY-MM-DD}.png`
- Per-trade charts (`--plot`): `./output/backtest_charts/{TICKER}/...png`
- Backtest results : `output/backtest_{TICKER}.csv` (composite),
  `output/backtest_{TICKER}_opr.csv` (OPR), `output/backtest_{TICKER}_fib.csv` (Fib)
- Comparaison portefeuilles : `output/portfolio_comparison.csv`

---

## Configuration (`config.py`)

**All strategy parameters live in `config.py`.** Never hardcode values in logic files.

Key sections:
- `INSTRUMENTS` — dict with `dollar_per_point`, `tick_size`, `tv_symbol`, `tv_exchange`.
- Per-asset v3 params: `SL_MINIMUM`, `RR_TARGET`, `ZONE_QUALITY_MIN`, `USE_PM_FILTER`, `TRADE_RANGE`.
- Per-asset **v5 composite params**: `COMPOSITE_SCORE_MIN`, `TREND_STRENGTH_MIN`,
  `ATR_RATIO_MIN/MAX`, `GAP_ATR_MAX`, `OVN_RANGE_MAX`.
- Pondération composite: `COMPOSITE_WEIGHTS` (somme = 1.0, default 0.40/0.25/0.20/0.15).
- Garde-fou Topstep: `TOPSTEP_DAILY_LOSS_MAX=1000`, `TOPSTEP_TRAILING_DD=2000`, `TOPSTEP_SAFETY_MULT=1.1`.
- Circuit breakers: `DAILY_STOP_AFTER_SL` (False), `CONSEC_LOSS_PAUSE_DAYS` (5), `DAILY_LOCKIN_THRESHOLD` (0).
- `YM1_ENABLED` — global kill-switch, False tant que PF OOS < 1.2.
- Session times: signal cutoff 11:00 UTC, US session 13:00–21:00 UTC.

Supported tickers: `MES1`, `NQ1`, `YM1` (the latter gated by `YM1_ENABLED`).

---

## Signal Data Structure

Signals are Python dicts with this schema:
```python
{
    "ticker":     str,              # "MES1" | "NQ1" | "YM1"
    "direction":  "long" | "short",
    "entry":      float,
    "sl":         float,            # Stop loss price
    "tp":         float,            # Take profit price
    "sl_dist":    float,            # Distance entry → SL
    "tp_dist":    float,            # Distance entry → TP
    "rr":         float,            # Risk/reward ratio
    "n_ct":       int,              # Number of contracts
    "risk":       float,            # Dollar risk ($100 fixed)
    "gain":       float,            # Dollar gain at TP
    "quality":    float,            # Zone quality score 0–100
    "n_tf":       int,              # Number of timeframes confirming zone
    "touches":    int,              # Touch count for zone
    "regime":     "BULL" | "BEAR" | "RANGE",
    "zone_low":   float,
    "zone_high":  float,
    "price_now":  float,
}
```

---

## Strategy Logic (v5.2 — current production)

### Signal Generation Pipeline
```
1. Load 15m OHLCV; resample to D1, H4, H1.
2. Detect S/R zones (swing pivots + multi-TF clustering).
3. Score zone quality (touches, TF count, pivot weight, recency) → 0-100.
4. Compute trend: triple EMA per TF, weighted (D1=40%/H4=35%/H1=25%) → regime +
   continuous `alignment_score` ∈ [-1, +1].
5. Compute pre-market features (prev_return, prev_close_pos, ovn_path_eff)
   and volatility features (ATR journalier, atr_ratio = ovn_range/atr_daily,
   gap_atr, ovn_range_atr, vol_score bell-curve around 0.55).
6. Regime filter (hard): no LONG in BEAR, no SHORT in BULL; RANGE allowed per
   `TRADE_RANGE`.
7. Volatility gate (hard, per asset): reject if atr_ratio outside
   [ATR_RATIO_MIN, ATR_RATIO_MAX], gap_atr > GAP_ATR_MAX, ovn_range_atr >
   OVN_RANGE_MAX, or |alignment_score| < TREND_STRENGTH_MIN.
8. Composite score 0-100:
      100 × (0.40·zone/100 + 0.25·|alignment| + 0.20·pm_score + 0.15·vol_score)
   Reject if score < COMPOSITE_SCORE_MIN[ticker].
9. Entry = 1st quartile of zone, SL = zone edge ± SL_BUFFER_TICKS,
   TP = entry ± RR_TARGET × SL_dist. Sizing: n_ct = $100 / (SL_dist × $/pt).
10. Max 2 fills / day / asset; extra fills marked NOT_FILLED.
```

### Intra-day circuit breakers (backtest only for now)
Applied chronologically to the day's filled trades (in `backtest.py`):
- `DAILY_STOP_AFTER_SL` : once True, after the first SL of the day the
  remaining trades are cancelled. Disabled by default — combined with the
  consec-loss breaker it cut too many winners.
- `DAILY_LOCKIN_THRESHOLD` > 0 : freeze remaining trades once the day's cum
  P&L exceeds this value. Disabled by default for the same reason.
- `CONSEC_LOSS_PAUSE_DAYS` (default **5**) : after N consecutive losing days,
  skip the next day entirely. Sweet spot: reduces DD without capping upside.

### Topstep guardrail
Before generating signals for the day, `backtest.py` computes:
```
slack = min(TOPSTEP_DAILY_LOSS_MAX + day_pnl, cum_pnl - (peak_pnl − TOPSTEP_TRAILING_DD))
```
If `slack < RISK_PER_TRADE_USD × TOPSTEP_SAFETY_MULT`, the day is skipped.
This mirrors how a live trader must respect the funded-account bounds.

### Trend alignment
Per TF score: `(sign(price−EMAfast) + sign(price−EMAslow) + sign(EMAfast−EMAslow)) / 3`.
Portfolio `alignment_score` = Σ weight × TF score. Regime labels:
- BULL if > `TREND_BULL_THRESHOLD` (0.33)
- BEAR if < `TREND_BEAR_THRESHOLD` (-0.33)
- RANGE otherwise

### Zone quality score (unchanged from v3)
- 30% touch count (max 8 touches)
- 25% timeframe count (max 4 TFs)
- 15% pivot weight (D1 > H4 > H1 > 15m)
- 15% recency bonus

---

## Trade Rules & Risk Model (v5.2)

| Rule | MES1 | NQ1 | YM1 |
|------|------|-----|-----|
| Risk per trade | $100 fixed | $100 fixed | $100 fixed |
| Max trades/day/asset | 2 | 2 | 2 |
| SL minimum (pts) | 9 | 29 | 60 |
| RR target | 3.0× | 2.5× | 1.75× |
| Zone quality min | 70 | 40 | 30 |
| Pre-market filter | on | on | off |
| Trade RANGE regime | yes | yes | no |
| `COMPOSITE_SCORE_MIN` | 60 | 55 | 70 |
| `TREND_STRENGTH_MIN` | 0.25 | 0.30 | 0.40 |
| `YM1_ENABLED` | — | — | **False** |

Global: no LONG in BEAR, no SHORT in BULL (hard gate). SL buffer = 2 ticks.
Topstep slack guardrail + `CONSEC_LOSS_PAUSE_DAYS=5` sit on top.

---

## Stratégie OPR (`opr-v4`) — exécutée en parallèle du composite et de Fib

Réécriture fidèle au PineScript fourni par l'utilisateur (avr. 2026).
Implémentation : `core/opr.py` → `run_opr_day(df_15m, ticker, day_ny)`.
Backtest dédié : `backtest.py --strategy opr` (ou `--strategy both`).

### Fuseau horaire — `America/New_York` (DST-aware)
Toutes les heures de la stratégie OPR sont définies en **heure NY** afin
que la logique soit invariante au passage été/hiver côté Paris :

| Heure NY | UTC en hiver (EST) | UTC en été (EDT) |
|----------|-------------------|------------------|
| 9h30     | 14h30             | 13h30            |
| 16h30    | 21h30             | 20h30            |

`zoneinfo.ZoneInfo(OPR_TIMEZONE)` gère automatiquement la transition. Le
DataFrame source reste en UTC naïf (cohérent avec le reste du codebase) ;
la conversion s'effectue à l'intérieur de `core/opr.py`.

### Définition de la zone OPR (anchor par pic de volume)
- La bougie OPR est identifiée comme la **bougie de volume max** dans la
  fenêtre `[OPR_WINDOW_START, OPR_WINDOW_END[ NY` (par défaut
  `[9h15, 9h45[`). En pratique, c'est presque toujours la bougie 9h30 NY
  pile — l'ouverture cash NY est marquée par une explosion de volume
  (typiquement 5-30× les bougies pré-marché).
- `opr_high` = high de cette bougie, `opr_low` = low.
- Pourquoi le volume plutôt que l'horaire strict ? **Robustesse** :
  indépendant des dérives mineures de timestamp (broker, data provider) et
  des éventuelles sessions écourtées. Ça gère aussi automatiquement le
  passage DST EST/EDT : 9h30 NY reste 9h30 NY toute l'année, c'est l'UTC
  qui bouge — et la fenêtre est exprimée en NY.
- Sur 320 jours actifs testés (MES1 Dec 2024 → Mar 2026), **320/320**
  voient leur pic de volume tomber sur la bougie 9h30 NY pile dans la
  fenêtre [9h15, 9h45[. Aucun cas de drift observé en historique, mais la
  marge ±15min protège la stratégie en live.

### Triggers de pullback (PineScript-faithful)
Vérifiés sur chaque bougie qui clôture **strictement après 9h45 NY** et
**avant 16h30 NY**, et uniquement quand aucune position n'est ouverte ni
qu'aucun ordre limite n'est en attente.

- **LONG** : `bar.open < opr_high AND bar.close > opr_high` →
  arme un ordre `limit BUY @ opr_high`.
- **SHORT** : `bar.open > opr_low AND bar.close < opr_low` →
  arme un ordre `limit SELL @ opr_low`.

L'ordre limite est armé pour la bougie suivante. Il fait fill dès qu'une
bougie ultérieure touche le niveau OPR (`bar.low ≤ opr_high ≤ bar.high`
côté long, symétrique côté short). Une seule position à la fois — tant
qu'une position est ouverte ou qu'un ordre limite est pendant, aucun
nouveau trigger n'est armé. Si le prix s'éloigne sans fill, l'ordre reste
actif jusqu'à 16h30 NY (puis annulé, marqué `NOT_FILLED`).

### Filtre trigger (opr-v4 — avant armement de l'ordre limite)
Appliqué dans `_passes_trigger_filter()` juste après `_check_trigger()`, avant `_make_signal()`.

- **MES1 — `trigger_vol_zscore < OPR_MAX_VOL_ZSCORE` (seuil = -0.45)** : rejette les triggers
  sur une bougie dont le volume est anormalement élevé vs les 20 bougies de session
  précédentes (z-score > -0.45). Un spike de volume = move émotionnel / news, pas un
  retour technique propre. OOS : PF 1.33 → 1.79 (+0.46).
- **YM1 — `max_excursion_atr > OPR_MIN_EXCURSION_ATR` (seuil = 0.17)** : exige que le prix
  ait déjà parcouru ≥ 17 % de l'ATR journalier dans le sens du trigger depuis 9h30 NY.
  Élimine les tests immédiats et chaotiques post-OPR. OOS : PF 1.51 → 2.59 (+1.08 après
  re-calibration SL/TP).
- **NQ1 — aucun filtre** : baseline OPR PF OOS=1.65 déjà robuste, aucun filtre
  n'améliore de façon consistante.

Calibration : `analyse/03_filter_backtest.py` — IS avant 2025-10-01, OOS à partir de
2025-10-01. Résultats complets dans `analyse/RESULTATS.md`.

### Construction du trade (opr-v4 — SL/TP basés ATR)
- `entry`     = `opr_high` (long) ou `opr_low` (short)
- `atr_daily` = ATR(`OPR_ATR_PERIOD=14`) sur les bougies D1 achevées
                **strictement avant `day_ny`** (pas de leak temporel —
                la journée courante, partielle, n'entre pas dans le calcul).
- `sl_dist`   = `max(OPR_SL_ATR_MULT[ticker] × atr_daily,
                     OPR_SL_MIN_POINTS[ticker])`
- `tp_dist`   = `OPR_TP_ATR_MULT[ticker] × atr_daily`
- `sl`        = `entry ∓ sl_dist`, `tp` = `entry ± tp_dist`
- `n_ct`      = `RISK_PER_TRADE_USD / (sl_dist × $/pt)` — risque fixe $100
- À 16h30 NY, toute position ouverte est fermée au close (`result=TE`).

> **Pourquoi l'ATR journalier ?** Pour une stratégie intraday, on veut que
> SL/TP représentent une fraction du « voyage typique » d'une journée. C'est
> plus stable qu'un ATR 15m (trop bruité) et plus pertinent qu'un ATR sur la
> seule bougie OPR. Effet recherché : sur jours volatiles → SL plus large +
> moins de contrats ; sur jours calmes → SL plus serré + plus de contrats.
> Le sizing s'adapte automatiquement au régime, à risque dollar constant.

### Paramètres calibrables (`config.py`)
| Param                  | Rôle                                             |
|------------------------|--------------------------------------------------|
| `OPR_TIMEZONE`         | `"America/New_York"` (ne pas modifier)           |
| `OPR_WINDOW_START/END` | `(9,30) / (9,45)` — fenêtre de formation OPR     |
| `OPR_SESSION_END`      | `(16,30)` — close all                            |
| `OPR_ATR_PERIOD`       | période ATR journalier (défaut 14)               |
| `OPR_SL_ATR_MULT`      | multiplicateur ATR pour le SL, par ticker        |
| `OPR_TP_ATR_MULT`      | multiplicateur ATR pour le TP, par ticker        |
| `OPR_SL_MIN_POINTS`    | floor SL minimum (en points) — anti noise stop-out |
| `OPR_MAX_TRADES_PER_DAY` | plafond fills/jour (sécurité, rarement atteint)|
| `OPR_MIN_EXCURSION_ATR` | excursion min vers OPR avant trigger / atr_daily (None = off) |
| `OPR_MAX_VOL_ZSCORE`   | z-score volume max du trigger vs session (None = off) |
| `OPR_VOL_ZSCORE_WINDOW` | fenêtre z-score volume (défaut 20 bougies)       |

### Calibration walk-forward `opr-v4` (IS Dec 2024 → Sep 2025, OOS Oct 2025 → Mar 2026)

`python optimize_opr.py --csv-dir ./data` balaye une grille
(`SL_ATR_MULT × TP_ATR_MULT`) par actif avec les filtres trigger actifs.
Le script applique le critère : **OOS PF ≥ 1.2, n_trades OOS ≥ 8, P&L OOS > 0**.
Score = OOS_PF × OOS_P&L. La sélection finale dans `config.py` exclut les combos
avec IS PF < 1.35 (faux positifs).

Combinaisons retenues `opr-v4` :

| Asset | SL_mult | TP_mult | RR   | IS PF | IS P&L  | OOS PF | OOS P&L | OOS DD  |
|-------|---------|---------|------|-------|---------|--------|---------|---------|
| MES1  | 0.15    | 0.50    | 3.33 | 1.41  | +$1,339 | 1.51   | +$1,000 | -$508   |
| NQ1   | 0.05    | 0.10    | 2.00 | 1.65  | +$10,073 | 1.65 | +$4,230 | -$804   |
| YM1   | 0.12    | 0.15    | 1.25 | 1.69  | +$3,292 | 2.59   | +$2,639 | -$264   |

Backtest portefeuille (Dec 2024 → Mar 2026) avec filtres trigger + multiplicateurs recalibrés,
**OPR seul** :

| Métrique          | MES1     | NQ1       | YM1       | Portefeuille   |
|-------------------|----------|-----------|-----------|----------------|
| Trades            | 139      | 476       | 199       | 814            |
| Win rate          | 40%      | 46%       | 60%       | —              |
| Profit factor     | 1.45     | 1.65      | 1.92      | —              |
| P&L total         | +$2,339  | +$14,304  | +$5,931   | **+$22,573**   |
| Max trailing DD   | -$561    | -$866     | -$502     | -$746          |
| Bootstrap pass    | 5.8%     | 99.8%     | 100%      | **99.8%**      |

> P&L portfolio réduit vs opr-v3 (+$22,573 vs +$29,370) mais DD divisé par 2
> (-$746 vs -$1,515) et bootstrap portefeuille stable (99.8%). MES1 bootstrap
> faible (5.8%) car peu de trades (139) avec RR=3.33 — le portefeuille reste
> viable grâce à NQ1+YM1.
>
> Les valeurs sont à ré-évaluer visuellement via les graphiques d'analyse
> avant d'être adoptées en production. Le backtest brut peut sur-fitter
> sur l'IS — la revue chart-par-chart prime sur le PF.

### Règles à respecter pour évoluer la stratégie OPR
- **Bump `OPR_STRATEGY_VERSION`** dans `config.py` (ex. `opr-v5`) à chaque
  changement structurel (nouvelle règle de trigger, nouveau filtre, autre
  base de référence ATR…). Cela isole les graphiques d'analyse et permet
  une comparaison versionnée.
- **Re-calibrer `OPR_SL_ATR_MULT` / `OPR_TP_ATR_MULT`** via `optimize_opr.py`
  quand la règle de trigger, les filtres trigger ou la définition de l'ATR change.
- **Re-calibrer les filtres trigger** via `analyse/01_extract_features.py` +
  `analyse/03_filter_backtest.py` quand de nouvelles données sont disponibles ou
  quand la logique de base change. Les seuils `OPR_MIN_EXCURSION_ATR` et
  `OPR_MAX_VOL_ZSCORE` dans `config.py` sont calibrés IS/OOS et doivent être
  re-validés avant tout changement manuel.
- **Ne pas hard-coder d'heure UTC** dans la logique OPR — toute heure doit
  passer par `OPR_TIMEZONE` afin de gérer DST automatiquement.
- **Pas de leak temporel pour l'ATR** : `_compute_atr_daily` doit toujours
  exclure la journée courante (et toute donnée post-9h30 NY). Tout
  changement à cette fonction doit être audité contre le risque de leak.
- **Garder le module `core/opr.py` indépendant** des modules composite
  (`zones.py`, `scoring.py`) — les deux stratégies cohabitent en parallèle.
- **Backtests / optimisations sans charts par défaut** côté assistant
  (ajouter `--no-analysis-charts` au CLI). L'utilisateur génère les charts
  de revue de son côté pour ne pas allonger les itérations.

---

## Stratégie Fibonacci 50% (`fib-v1`) — promotion depuis `draft_fibo_50/`

Troisième stratégie du portefeuille, indépendante de Composite et OPR.
Logique métier : voir `core/strategy_fib.py` (et `draft_fibo_50/README_LOGIC.md`
pour la justification technique complète).

### Pipeline (M15)
1. Tendance multi-critères : `EMA50 > EMA200 + ADX(14) > 20` → BULL/BEAR/RANGE
2. Détection impulse : pivots `LEFT=RIGHT=8` + filtres ATR + durée +
   alignement avec la tendance
3. Entrée LIMIT à `fib_50 = swing_low + 0.5 × (swing_high − swing_low)`
   (long, inverse short)
4. SL = entry ∓ `FIB_SL_ATR_MULT × ATR`, TP = entry ± `FIB_TP_ATR_MULT × ATR`
5. Filtre trigger walk-forward (per-ticker)
6. Position fermée au timeout (`FIB_MAX_HOLD_BARS = 32` bougies = ~8h)

### Calibration walk-forward (IS Dec 2024 → Sep 2025, OOS Oct 2025 → Mar 2026)

**Niveau Fibonacci (`FIB_LEVEL_PER_TICKER`) :** test des 3 niveaux 38.2 / 50 / 61.8 %
en walk-forward via `draft_fibo_50/optimize_fib_levels.py` + `compare_fib_levels.py`.
Retenu : **38.2 % uniforme sur les 3 actifs**. Toutes combinaisons multi-niveaux
dégradent le bootstrap Topstep (DDs additifs).

| Asset | Level | SL_mult | TP_mult | IMP_min | RR | Filtre trigger | OOS Sharpe |
|-------|-------|---------|---------|---------|------|----------------|------------|
| MES1 | 0.382 | 0.75 | 2.00 | 2.00 | 2.67 | `bars_since_confirm < 10` | 4.51 |
| NQ1  | 0.382 | 1.50 | 1.50 | 1.00 | 1.00 | `adx_at_arm > 44.035`     | 18.67 |
| YM1  | 0.382 | 1.00 | 2.00 | 2.00 | 2.00 | `bars_since_confirm < 2`  | 7.11 |

> Caveat : NQ1 et YM1 ont des samples OOS faibles (n=10/12) — Sharpe spectaculaires
> en partie un artefact de small sample. À re-valider sur 2026-Q2/Q3.

Backtest portefeuille Fib seul (fib-v2, Dec 2024 → Mar 2026) :
- 182 trades, P&L=+$6,891, DD=-$494, Sharpe=5.13, Bootstrap=100%
- vs fib-v1 (50 %) : 128 trades, P&L=+$3,600 → +91 % de P&L pour DD comparable

### Règles à respecter pour évoluer Fib
- **Bump `FIB_STRATEGY_VERSION`** dans `config.py` à chaque changement structurel.
- Re-calibration via `draft_fibo_50/optimize.py` quand la logique change
  (la sandbox est conservée pour traçabilité historique).
- Aucun chart d'analyse journalier pour Fib (pas de cutoff jour pertinent —
  les triggers sont étalés sur la session).

---

## Portefeuille combiné — analyse 3 stratégies

`compare_portfolios.py` charge les CSVs de sortie de
`backtest.py --strategy all` et calcule les métriques pour les 7
combinaisons non vides de {Composite, OPR, Fib} :
- Concaténation chronologique des trades sur les 3 actifs
- Agrégation journalière du P&L
- Sharpe annualisé (sur returns journaliers, sqrt(252))
- Bootstrap Topstep (1000 permutations) — target $3K, max DD $2K, daily $1K

### Résultats (Dec 2024 → Mar 2026, Fib en version fib-v2 / 38.2 %)
| Combinaison | Trades | P&L | Max DD | Sharpe | Bootstrap |
|-------------|--------|------|---------|--------|-----------|
| Composite seul | 142 | +$3 728 | -$1 500 | 4.05 | 100 % |
| OPR seul | 814 | +$22 573 | -$746 | 6.45 | 99.9 % |
| Fib seul (fib-v2) | 182 | +$6 891 | -$494 | 5.13 | 100 % |
| Composite + OPR | 956 | +$26 302 | -$852 | 6.46 | 98.5 % |
| OPR + Fib | 996 | +$29 464 | -$756 | **7.01** | 99.1 % |
| **Composite + OPR + Fib** | **1 138** | **+$33 193** | -$904 | 6.96 | 99.2 % |

**Recommandation production : triplet Composite + OPR + Fib-v2.**
Maximise P&L (+$33 193) avec DD contenu (-$904), Sharpe annualisé 6.96,
bootstrap 99.2 %. Alternative Sharpe-max : OPR + Fib (P&L -$3 729 mais
DD -$756 et Sharpe 7.01). La diversification entre les 3 logiques de signal
(zones S/R / OPR / pullback Fib 38.2) lisse la courbe d'equity.

> **Caveat pour la prod broker** : actuellement chaque stratégie applique
> son propre garde-fou Topstep PAR TICKER. En production multi-stratégies,
> il faudra un garde-fou GLOBAL qui voit tous les trades en chronologique
> (sinon risque de dépassement du daily $1K si 3 trades simultanés). À
> traiter au moment de l'intégration ProjectX.

---

## Key Modules: What They Do

### `core/data.py`
- `load_csv(path)` → pandas DataFrame with DatetimeIndex
- `fetch_live(ticker, n_bars)` → DataFrame from TradingView (5 retries, 2s backoff)
- Deduplication and sorting on load

### `core/strategy_fib.py` (fib-v1)
- `compute_ema/atr/adx` — indicateurs standalone (pas de TA-Lib)
- `detect_pivots(df, left, right)` — pivots high/low confirmés
- `detect_trend(close, ema_f, ema_s, adx, threshold)` → BULL/BEAR/RANGE
- `find_last_impulse(...)` → dict décrivant l'impulse alignée tendance
- `build_signal(impulse, atr, ticker, sl_mult, tp_mult)` → signal trade
- `run_fib_backtest(df_15m, ticker)` → DataFrame de trades clos
- Filtre trigger walk-forward appliqué inline (per-ticker via config.py)

### `core/zones.py`
- `detect_pivots(df, window)` → swing high/low indices
- `cluster_zones(pivots, tolerance)` → merged zone list with quality scores
- `filter_zones(zones, price, config)` → distance and quality filtering

### `core/trend.py`
- `compute_ema(df, period)` → EMA series
- `detect_regime(df_d1, df_h4, df_h1, config)` → `"BULL"` | `"BEAR"` | `"RANGE"`

### `core/premarket.py`
- `compute_features(df_15m, cutoff, ticker)` → dict of pm features
- `filter_pass(pm, ticker)` → boolean gate (per-asset thresholds)

### `core/scoring.py` (v5)
- `compute_volatility_features(df_15m, cutoff, ticker)` → `atr_daily`, `atr_ratio`,
  `gap_atr`, `ovn_range_atr`, `vol_score`.
- `compute_composite_score(zone, alignment_score, pm, vol, ticker)` → 0-100 or
  `None` if a hard gate fails. Used by `strategy.py` to filter zones.

### `core/risk_topstep.py` (v5)
- `trade_allowed(day_pnl, cum_pnl, peak_pnl, risk_per_trade=100)` →
  `(bool, reason_str)`. Called before `generate_signals` in backtest loop.

### `core/strategy.py`
- `generate_signals(df, ticker, trend_scores=None, pm=None, vol=None, max_signals=...)`
  → list of signal dicts. Applies composite filter at zone-selection time.
- `simulate_trade(signal, df_session)` → trade result with P&L (TP only granted
  if fill bar goes in trade direction).

### `core/chart.py`
- `generate_chart(df, signals, ticker, date)` → saves PNG, returns path.

### `backtest.py`
- Sole runtime entry point. Day-by-day loop with Topstep slack guard +
  consec-loss streak tracker, runs both the composite and OPR strategies
  (`--strategy both` by default).
- Per-day: compute trend/pm/vol, call `generate_signals` (composite) and
  `run_opr_day` (OPR), simulate, apply intra-day circuit breakers, update
  rolling `cum_pnl / peak_pnl`.
- `validate_topstep(trades_df)`: bootstrap 1000 permutations of day order to
  estimate the probability of completing the $3K target without breaching the
  $1K daily / $2K trailing limits.
- Data source: CSV (`--csv-dir`) or TradingView live (`--live [--bars N]`),
  mutually exclusive. The `--live` path goes through `core.data.fetch_live`
  via the `tvDatafeed` fork in `requirements.txt`.
- CLI: `--csv-dir | --live`, `--bars`, `--ticker`, `--strategy`, `--plot`,
  `--plot-filter`, `--no-analysis-charts`, `--output-dir`.

### `optimize.py`
- Walk-forward IS (2024-12 → 2025-09) / OOS (2025-10 → 2026-03).
- Phase A (global): grid over general params, picks best IS with OOS sanity.
- Phase B (per-asset): per-ticker SL/RR/zone_quality tuning.
- Phase C (composite): `optimize_composite_per_asset` scans
  `COMPOSITE_SCORE_MIN × TREND_STRENGTH_MIN`, keeps IS winner only if OOS
  PF ≥ 1.2 and n_trades ≥ 8. YM1 flipped to enabled only if OOS PF ≥ 1.2.
- `update_config(global_p, asset_p, composite_p=None, ym1_enabled=None)` writes
  results back into `config.py` in-place.

### `run_phase_c.py`
- Lightweight entry point that runs **only** Phase C. Useful when A/B are
  already calibrated (v4 / v5) and you only want to refresh composite
  thresholds without a full multi-hour optimizer run.

---

## Code Conventions

- **Language:** Variable names, comments, and docstrings are in **French**
- **Naming:** `snake_case` for functions/variables
- **Config:** All parameters in `config.py` — no magic numbers in logic
- **Signals:** Passed as dicts (not classes)
- **DataFrames:** Use `DatetimeIndex`, always sorted ascending
- **Timeframes:** Built by resampling from 15m base data
- **No side effects in core modules** — `backtest.py` is the only orchestrator.
  Core modules must remain pure (no network, no Telegram, no broker calls)

---

## Testing

There is **no automated test framework**. The `core/test.py` file is a single-line stub.

Validation is done via backtest audit:
```bash
python backtest.py --csv-dir ./data
# Check printed audit for warnings/failures
```

When adding new features:
1. Run a full backtest to confirm no regressions
2. Use `--plot` to visually inspect signal placement
3. Compare results against `CHECKPOINTS_SUMMARY.md` baselines

---

## Graphiques d'analyse journaliers (consigne pérenne)

> **Règle imposée par l'utilisateur — vaut pour TOUTES les stratégies, présentes
> et futures. À ne pas retirer sans demande explicite.**

Chaque exécution de `backtest.py` doit produire **un graphique PNG par jour
tradé / ticker** dans :

```
output/analysis_charts/{STRATEGY_VERSION}/{TICKER}/{YYYY-MM-DD}.png
```

C'est une "photographie" complète de la journée vue par la stratégie : on
doit pouvoir prendre n'importe quelle journée tradée, ouvrir le PNG
correspondant, et comprendre toute la décision sans relancer le code.

### Contenu obligatoire de chaque graphique

1. **Cours OHLC 15min** — `ANALYSIS_CHART_CONTEXT_BEFORE` (200) bougies avant
   le cutoff d'analyse + toutes les bougies jusqu'à la fin de la session US
   du jour. Une seule image regroupe **tous** les signaux du jour, jamais un
   par signal.
2. **Échelle Y basée sur le prix** (`low.min` → `high.max` + marge), **pas** sur
   les zones — c'est explicite dans la spec utilisateur. Les zones hors
   fenêtre sont ignorées plutôt que d'aplatir le mouvement du prix.
3. **Zones S/R identifiées par timeframe** : bandes horizontales colorées par
   TF dominante (D1=ambre, H4=violet, H1=bleu, 15m=gris) avec étiquette
   `TFs Q{quality} ({touches}t)`.
4. **Marqueur cutoff vertical** pour visualiser le moment d'analyse.
5. **Pour chaque signal** : lignes E/SL/TP étendues sur toute la session US,
   étiquettes numérotées (E1, SL1, TP1, …), marqueur fill (triangle bleu) et
   exit (cercle vert/rouge/orange selon TP/SL/TE) avec P&L annoté.
6. **Encadré récap des signaux** (haut gauche) listant pour chaque signal :
   direction, prix d'entrée, SL, TP, RR, contrats, score composite, qualité
   de zone, résultat simulé.
7. **Encadré contexte** (bas gauche) avec : régime, alignment, features
   pré-marché (`ovn_path_eff`, `prev_return`, `prev_close_pos`) et features
   de volatilité (`atr_daily`, `atr_ratio`, `gap_atr`, `vol_score`).
8. **Légende TF** + entry/SL/TP en haut à droite.
9. **Titre** : ticker, date, nombre de signaux, nombre de fills, P&L jour.
10. **Sous-plot volume** (style TradingView) — bandes verticales colorées
    vert/rouge selon la direction de la bougie, alignées avec le prix. Sert
    surtout à confirmer visuellement l'ancrage de l'OPR sur l'explosion de
    volume cash open.
11. **Heures sur l'axe X en heure NY** (DST-aware) — la bougie 9h30 NY
    s'affiche toujours à 09:30, indépendamment du saisonnier UTC.

### Implémentation actuelle

- Module : `core/analysis_chart.py` → `plot_day_analysis(...)`
- Activation : `ANALYSIS_CHARTS_ENABLED = True` dans `config.py`
- Override CLI : `python backtest.py --no-analysis-charts` pour désactiver.
- Tag stratégie : `STRATEGY_VERSION` dans `config.py` — bump à chaque
  nouvelle stratégie pour avoir un dossier dédié et conserver les
  graphiques de la version précédente côte à côte (analyse comparative).

### Règles à respecter dans toute évolution

- **Ne pas désactiver** la génération par défaut — l'utilisateur s'appuie
  dessus pour valider chaque nouvelle stratégie.
- **Bump `STRATEGY_VERSION`** dans `config.py` dès qu'une stratégie change
  significativement (nouveau filtre, nouvelle pondération, nouveau seuil).
  Cela évite d'écraser les graphiques d'une version précédente.
- **Si une nouvelle feature de décision est ajoutée** (un nouveau filtre,
  un nouveau scoring, un nouveau régime…), elle doit apparaître dans le
  bandeau contexte du graphique. Touchez `core/analysis_chart.py` en même
  temps que vous touchez la logique de décision.
- **Pas de fork** : si vous ajoutez un autre type de graphique (ex. revue
  par trade), conservez `plot_day_analysis` comme la vue principale.

---

## Development Workflow

### Branch convention
- Work on feature branches (e.g., `claude/<feature-name>`)
- Do not push to `master` directly

### Making changes
1. Read relevant core module(s) before editing
2. Keep all new parameters in `config.py`
3. Maintain French naming conventions for consistency
4. Run backtest after strategy changes; document result changes

### Backtest baseline (v5.2, Dec 2024 → Mar 2026)

Portefeuille (MES1 + NQ1, YM1 désactivé) :

| Metric | Value | Topstep limit |
|---|---|---|
| P&L total | **+$3,728** | target +$3,000 ✅ |
| Max daily loss | -$296 | -$1,000 ✅ |
| Max trailing DD | -$1,500 | -$2,000 ✅ |
| Bootstrap pass rate | **100%** | ≥ 80% ✅ |
| Winning days | 55% (91 jours tradés) | — |

| Asset | Trades | Win% | PF | P&L | Max DD | Status |
|-------|--------|------|----|-----|--------|--------|
| MES1 | 47 | 34% | 1.39 | +$1,078 | -$1,030 | active |
| NQ1 | 95 | 42% | 1.87 | +$2,651 | -$632 | active (Phase C calibrated) |
| YM1 | 0 | — | — | 0 | — | **disabled** (OOS PF 0.73 < 1.2) |

**Why v5.2 is the canonical version**
- v5: introduced composite score + Topstep guardrail → portfolio PF ≈ 1.5.
- v5.1: added `CONSEC_LOSS_PAUSE_DAYS=5` → lowered DD from -$1,820 to -$1,500.
- v5.2: Phase C walk-forward picked NQ1 `score_min=55, trend=0.30`
  (OOS PF=1.75 validated). MES1 v5 values retained — optimizer proposed more
  permissive thresholds but OOS PF=0.64 flagged overfit.

Full version history: `CHECKPOINTS_SUMMARY.md`.

---

## Common Pitfalls

- **Data path:** CSV files must be `{csv_dir}/{TICKER}_data_m15.csv` (uppercase ticker).
- **Timezone:** all timestamps UTC internally; composite cutoff 11:00 UTC.
  OPR is anchored to NY time via `OPR_TIMEZONE` (DST-aware) — never hard-code UTC for OPR.
- **TradingView is best-effort:** `backtest.py --live` may return empty
  data on rate limit / network error (`fetch_live` retries 5×). Fall back
  to `--csv-dir` if `--live` returns nothing.
- **No live execution layer:** the project no longer emits signals or
  pushes notifications. The future ProjectX/Topstep broker integration is
  not built yet — don't reintroduce a `signals.py`-style live path or any
  Telegram/Slack/email notifier without an explicit user request.
- **Per-ticker configs:** `USE_PM_FILTER`, `TRADE_RANGE`, `COMPOSITE_SCORE_MIN`,
  `TREND_STRENGTH_MIN`, and all ATR thresholds — never homogenize.
- **`YM1_ENABLED=False` must be honored** by any new code path. The composite
  + walk-forward haven't proven YM1 profitable OOS; flipping it without fresh
  OOS evidence will likely trash the bootstrap rate.
- **Regime constraints are hard gates**, not soft penalties — never bypass.
- **Composite overfit risk:** if you re-run `optimize.py`, only accept new
  thresholds when OOS PF ≥ 1.2 **and** n_trades ≥ 8 **and** P&L OOS > 0.
  Phase C already enforces this; if you hand-tune, apply the same rule.
- **Temporal leak:** ATR/ATR30 and pre-market features must be strictly
  computed on `df[df.index < cutoff]`. `core/scoring.py` already does this;
  keep it that way.
- **Circuit breakers tracked in `backtest.py` only.** When the broker
  layer eventually lands (ProjectX), the live runner will need to
  re-implement `CONSEC_LOSS_PAUSE_DAYS` and the Topstep slack guard ; do
  not assume the backtest enforcement carries over for free.
- **Circular import risk:** `run_phase_c.py` imports from `optimize.py`;
  keep optimizer helpers importable without side effects.

---

## Gitignore Notes

The following are excluded from version control:
- `data/` and `*.csv` — market data files
- `output/` — generated charts and reports
- `__pycache__/`, `*.pyc` — Python bytecode
- `.env`, `.venv/` — environment files
