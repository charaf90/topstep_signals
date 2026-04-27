# CLAUDE.md — AI Assistant Guide for topstep_signals

## Project Overview

`topstep_signals` is a **production intraday trading signal system** for futures micro-contracts (MES1, NQ1, YM1), designed for the Topstep 50K funded-account challenge. It performs multi-timeframe technical analysis, generates daily trade signals, and sends them via Telegram.

The codebase is **pure Python (~2,000 lines)**, file-based (no database), and runs as a cron job or CLI tool. Documentation and variable names are **primarily in French**.

**Current version: v5.2** (composite score + Topstep guardrails + intra-day circuit breakers + walk-forward-calibrated thresholds). Key additions vs v4:
- Composite score 0-100 (zone 40% / trend 25% / pm 20% / vol 15%) replaces the simple quality threshold.
- Topstep slack guardrail refuses a trade when daily-loss or trailing-DD cushion < risk × 1.1.
- Consecutive-loss circuit breaker pauses trading 1 day after 5 consecutive losing days.
- YM1 disabled globally (`YM1_ENABLED=False`) — no OOS profitability proof yet.

---

## Repository Structure

```
topstep_signals/
├── config.py               # Central configuration (all strategy parameters)
├── signals.py              # Main live signal generator & Telegram sender
├── backtest.py             # Historical backtesting engine + validate_topstep bootstrap
├── optimize.py             # Walk-forward IS/OOS optimizer (Phase A/B/C)
├── run_phase_c.py          # Phase C alone (composite calibration only)
├── requirements.txt        # Python dependencies
├── README.md               # French user documentation
├── CHECKPOINTS_SUMMARY.md  # Strategy version history v1 → v5.2
├── core/
│   ├── data.py             # Data loading (CSV / TradingView live)
│   ├── zones.py            # Support/Resistance zone detection
│   ├── trend.py            # Trend detection (EMA-based, multi-TF) + alignment_score
│   ├── premarket.py        # Pre-market feature calculation & filtering
│   ├── scoring.py          # Composite score 0-100 + ATR volatility features
│   ├── risk_topstep.py     # Topstep slack guardrail (daily loss / trailing DD)
│   ├── strategy.py         # Signal generation + composite filtering + simulation
│   ├── chart.py            # TradingView-style chart generation (matplotlib)
│   └── telegram.py         # Telegram text + image senders
└── data/                   # gitignored
    ├── MES1_data_m15.csv
    ├── NQ1_data_m15.csv
    └── YM1_data_m15.csv
```

**Data and output directories are gitignored.** CSV files are not committed.

---

## Technology Stack

- **Language:** Python 3.7+
- **Key libraries:** pandas, numpy, matplotlib, requests, tvdatafeed (custom fork)
- **Data sources:** Local CSV files (backtest) or TradingView live API (production)
- **Notifications:** Telegram Bot API (text + chart images)
- **No framework, no database, no build system**

Install dependencies:
```bash
pip install -r requirements.txt
```

---

## Running the Project

### Live Signals (production)
```bash
python signals.py                            # Fetch live data, send Telegram
python signals.py --dry-run                  # Live data, skip Telegram
```

### CSV/Simulation Mode
```bash
python signals.py --csv-dir ./data                        # Today's date
python signals.py --date 2026-01-29 --csv-dir ./data      # Simulate past date
```

### Backtesting
```bash
python backtest.py --csv-dir ./data                       # All 3 assets
python backtest.py --csv-dir ./data --ticker NQ1          # Single asset
python backtest.py --csv-dir ./data --plot                 # With charts
```

### Output
- Charts saved to `./output/{date}_{ticker}_signal{n}.png`
- Signal summaries: `./output/{date}_signals.txt`
- Backtest results: `./output/backtest_{ticker}.csv`

---

## Configuration (`config.py`)

**All strategy parameters live in `config.py`.** Never hardcode values in logic files.

Key sections:
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — credentials (hardcoded, not env vars).
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

## Key Modules: What They Do

### `core/data.py`
- `load_csv(path)` → pandas DataFrame with DatetimeIndex
- `fetch_live(ticker, n_bars)` → DataFrame from TradingView (5 retries, 2s backoff)
- Deduplication and sorting on load

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

### `signals.py`
- Live orchestration: load data → composite filter → chart → Telegram.
- CLI: `--csv-dir`, `--date`, `--dry-run`, `--output-dir`.

### `backtest.py`
- Day-by-day loop with Topstep slack guard + consec-loss streak tracker.
- Per-day: compute trend/pm/vol, call `generate_signals`, simulate, apply
  intra-day circuit breakers, update rolling `cum_pnl / peak_pnl`.
- `validate_topstep(trades_df)`: bootstrap 1000 permutations of day order to
  estimate the probability of completing the $3K target without breaching the
  $1K daily / $2K trailing limits.
- CLI: `--csv-dir`, `--ticker`, `--plot`, `--plot-filter`, `--telegram`.

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
- **No side effects in core modules** — `signals.py` handles I/O and Telegram

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
- **Timezone:** all timestamps UTC internally; signal cutoff 11:00 UTC.
- **No live data without TradingView credentials:** use `--csv-dir` for local dev.
- **Telegram silently skipped with `--dry-run`**, not disabled globally.
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
- **Circuit breakers tracked in `backtest.py` only** today — the live path
  (`signals.py`) does not enforce `CONSEC_LOSS_PAUSE_DAYS` or the Topstep
  slack. The trader must respect those manually in live.
- **Circular import risk:** `run_phase_c.py` imports from `optimize.py`;
  keep optimizer helpers importable without side effects.

---

## Gitignore Notes

The following are excluded from version control:
- `data/` and `*.csv` — market data files
- `output/` — generated charts and reports
- `__pycache__/`, `*.pyc` — Python bytecode
- `.env`, `.venv/` — environment files
- `.chat_id` — Telegram runtime state
