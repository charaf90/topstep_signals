# CLAUDE.md — AI Assistant Guide for topstep_signals

## Project Overview

`topstep_signals` is a **production intraday trading signal system** for futures micro-contracts (MES1, NQ1, YM1), designed for the Topstep 50K funded-account challenge. It performs multi-timeframe technical analysis, generates daily trade signals, and sends them via Telegram.

The codebase is **pure Python (~1,300 lines)**, file-based (no database), and runs as a cron job or CLI tool. Documentation and variable names are **primarily in French**.

---

## Repository Structure

```
topstep_signals/
├── config.py               # Central configuration (all strategy parameters)
├── signals.py              # Main live signal generator & Telegram sender
├── backtest.py             # Historical backtesting engine
├── requirements.txt        # Python dependencies
├── README.md               # French user documentation
├── CHECKPOINTS_SUMMARY.md  # Strategy version comparison & results
├── core/
│   ├── data.py             # Data loading (CSV / TradingView live)
│   ├── zones.py            # Support/Resistance zone detection
│   ├── trend.py            # Trend detection (EMA-based, multi-TF)
│   ├── premarket.py        # Pre-market feature calculation & filtering
│   ├── strategy.py         # Signal generation & trade simulation
│   ├── chart.py            # TradingView-style chart generation (matplotlib)
│   └── test.py             # Minimal test stub (print "Test OK")
└── data/
    ├── MES1_data_m15.csv   # Micro E-mini S&P 500 15m OHLCV
    ├── NQ1_data_m15.csv    # Micro E-mini Nasdaq 100 15m OHLCV
    └── YM1_data_m15.csv    # Micro E-mini Dow Jones 15m OHLCV
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
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — credentials (hardcoded, not env vars)
- `INSTRUMENTS` — dict with `ticker`, `exchange`, `tv_symbol`, `point_value`, `tick_size`
- Per-asset strategy params: `SL_MIN`, `RR_TARGET`, `ZONE_QUALITY_MIN`, `PREMARKET_FILTER`
- Technical analysis: EMA periods, pivot detection windows, zone clustering tolerance (0.2%)
- Session times: signal cutoff 11:00 UTC, US session 13:00–21:00 UTC

Supported tickers: `MES1`, `NQ1`, `YM1`

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

## Strategy Logic (v3 — current production)

### Signal Generation Pipeline
```
1. Load 15m OHLCV data
2. Resample to D1, H4, H1 timeframes
3. Detect S/R zones (swing pivots, multi-TF clustering)
4. Score zone quality (touches, TF count, pivot weight, recency)
5. Detect trend regime (triple EMA cross, D1=40%, H4=35%, H1=25%)
6. Filter zones:
   - Quality ≥ minimum (per-asset config)
   - Distance 0.15%–2.5% from current price
   - No LONG in BEAR regime
   - No SHORT in BULL regime
   - RANGE trades allowed on MES/NQ only
7. Apply pre-market filter (MES and NQ only):
   - MES: prev_return < 0 OR prev_close_pos < 0.5
   - NQ: overnight_path_efficiency > 0.10
8. Calculate entry (1st quartile of zone), SL (zone edge − buffer), TP (entry ± RR × SL_dist)
9. Size position: n_contracts = $100 / (sl_dist × point_value)
```

### Trade Simulation (backtest)
- Check US session bars (13:00–21:00 UTC) for entry fill
- After fill: check SL first (conservative), then TP each bar
- Time exit (TE) if neither triggered by session close
- Max 2 fills per day per asset; extra fills are cancelled (NOT_FILLED)

### Trend Regime
- Score per TF: `(sign(price−EMAfast) + sign(price−EMAslow) + sign(EMAfast−EMAslow)) / 3`
- Weighted average: BULL if > +0.15, BEAR if < −0.15, else RANGE

### Zone Quality Score (0–100)
- 30% touch count (max 8 touches)
- 25% timeframe count (max 4 TFs)
- 15% pivot weight (D1 > H4 > H1 > 15m)
- 15% recency bonus

---

## Trade Rules & Risk Model

| Rule | Value |
|------|-------|
| Risk per trade | $100 fixed |
| Max trades/day/asset | 2 fills |
| Regime constraints | No LONG in BEAR, no SHORT in BULL |
| RANGE trades | MES/NQ only |
| SL minimums | MES=9pts, NQ=29pts, YM=60pts |
| RR targets | MES=1.5×, NQ=2.0×, YM=1.5× |
| Pre-market filter | MES & NQ enabled, YM disabled |

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
- `compute_premarket_features(df_15m, ticker)` → dict of filter values
- `apply_premarket_filter(signals, features, ticker)` → filtered signal list

### `core/strategy.py`
- `generate_signals(df, ticker, config)` → list of signal dicts
- `simulate_trade(signal, df_session)` → trade result with P&L

### `core/chart.py`
- `generate_chart(df, signals, ticker, date)` → saves PNG, returns path

### `signals.py`
- Main orchestration: load data → generate signals → chart → Telegram
- CLI: `--csv-dir`, `--date`, `--dry-run`, `--output-dir`

### `backtest.py`
- Day-by-day loop, calls `generate_signals` + `simulate_trade`
- Audit: P&L validation, SL enforcement, regime rule checking
- CLI: `--csv-dir`, `--ticker`, `--output-dir`, `--plot`

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

## Development Workflow

### Branch convention
- Work on feature branches (e.g., `claude/<feature-name>`)
- Do not push to `master` directly

### Making changes
1. Read relevant core module(s) before editing
2. Keep all new parameters in `config.py`
3. Maintain French naming conventions for consistency
4. Run backtest after strategy changes; document result changes

### Backtest baseline (v4 + simulation corrigée, Dec 2024 → Mar 2026)
| Asset | Trades | Win% | PF | P&L | Max DD |
|-------|--------|------|----|-----|--------|
| MES1 | 79 | 33% | 1.28 | +$1,318 | -$1,120 |
| NQ1 | 127 | 37% | 1.44 | +$2,013 | -$778 |
| YM1 | 147 | 36% | 0.97 | -$218 | -$1,132 |
| **Total** | **353** | | | **+$3,113** | |

**Simulation corrigée (v4-fix) :** Sur la bougie de fill, le TP n'est accordé que si
la bougie va dans le sens du trade (haussière pour LONG, baissière pour SHORT).
Les paramètres v4 restent les meilleurs après ré-optimisation (optimizer OOS négatif).

Optimisé via `optimize.py` (walk-forward IS: déc 2024–sept 2025 / OOS: oct 2025–mars 2026).
Backtest aligné sur le mode live (`max_signals=MAX_TRADES_PER_DAY` — résultats réalistes).

**v3 → v4 changements clés :**
- Seuil tendance ±0.15 → ±0.33 (filtre régime plus strict, moins de faux signaux)
- ZONE_TOLERANCE_PCT 0.002 → 0.001 (clustering plus serré)
- ZONE_DISTANCE_MAX_PCT 2.5% → 2.0% (zones plus proches du prix)
- SL_BUFFER_TICKS 4 → 2 (SL plus serré = plus de contrats = gains plus élevés par trade)
- RR MES1 1.5 → 3.0, NQ1 2.0 → 2.5, YM1 1.5 → 1.75

---

## Common Pitfalls

- **Data path:** CSV files must be `{csv_dir}/{TICKER}_data_m15.csv` (uppercase ticker)
- **Timezone:** All timestamps are UTC internally; signal cutoff is 11:00 UTC
- **No live data without TradingView credentials:** use `--csv-dir` for local dev
- **Telegram is silently skipped with `--dry-run`**, not disabled globally
- **Pre-market filter is per-ticker:** check `PREMARKET_FILTER` in config before assuming it applies
- **Zone quality thresholds differ per asset:** MES=70, NQ=60, YM=40 — don't homogenize
- **Regime constraints are hard rules**, not soft scores — never bypass them

---

## Gitignore Notes

The following are excluded from version control:
- `data/` and `*.csv` — market data files
- `output/` — generated charts and reports
- `__pycache__/`, `*.pyc` — Python bytecode
- `.env`, `.venv/` — environment files
- `.chat_id` — Telegram runtime state
