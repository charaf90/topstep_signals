# Template — strategies/<strategy_id>.py

> **Architecture** : ce fichier vit dans `strategies/` (couche recherche).
> Aucun import de `broker/`. Pas de modification de `core/opr.py` ni `core/strategy_fib.py`.
> Les helpers réutilisables (charts portfolio, métriques) doivent vivre dans `core/`,
> pas être dupliqués ici.

```python
"""
Stratégie <NOM> — <description courte>.

Concept       : <explication de la logique de trading>
Edge          : <pourquoi cet edge existe — qui paie ce P&L>
Falsification : <quelle observation invaliderait la stratégie>
Indicateurs   : <liste>
Fenêtre NY    : <heures de trading>
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from typing import Optional
from zoneinfo import ZoneInfo

from config import (
    INSTRUMENTS, RISK_PER_TRADE_USD, MAX_TRADES_PER_DAY,
    SLIPPAGE_TICKS_PER_TICKER,        # ex: {"MES1": 1, "NQ1": 2, "YM1": 1}
    COMMISSION_RT_PER_CONTRACT,       # ex: 1.40 (round-trip)
    MACRO_EVENT_DATES,                # liste des dates FOMC/CPI/NFP/JOLTS
    # Tous les paramètres de la stratégie depuis config.py
    <STRATEGY_ID>_STRATEGY_VERSION,
    <STRATEGY_ID>_SL_ATR_MULT_PER_TICKER,
    <STRATEGY_ID>_TP_ATR_MULT_PER_TICKER,
    <STRATEGY_ID>_HOUR_START_NY,
    <STRATEGY_ID>_HOUR_END_NY,
    <STRATEGY_ID>_ORDER_TIMEOUT_BARS,
    # ...
)
from core.risk_topstep import trade_allowed

# Reproductibilité
np.random.seed(42)

# ── Identité ─────────────────────────────────────────────────────────────────
STRATEGY_ID   = <STRATEGY_ID>_STRATEGY_VERSION    # ex: "ict-v1"
# Tickers — actifs standards : ["MES1", "NQ1", "YM1"]
# Nouveaux actifs (via core/data_fetcher.py) : ex ["MGC1"], ["MCLE1", "MNG1"]…
TICKERS       = ["MES1", "NQ1", "YM1"]
CSV_SUFFIX    = "_<suffix>"                       # ex: "_ict"
# Timeframe — m15 par défaut (CSV TradingView). Pour h1/h4/m5 etc., générer
# d'abord les CSV correspondants via `python -m core.data_fetcher --timeframe h1 ...`
CSV_TIMEFRAME = "m15"

_NY = ZoneInfo("America/New_York")                # DST-aware nativement

# ── Grille d'optimisation ─────────────────────────────────────────────────────
# Règle : ≤ 4 dimensions simultanées · bornes ancrées dans la logique marché
PARAM_GRID = {
    "sl_mult":  [0.5, 1.0, 1.5, 2.0],
    "tp_mult":  [1.5, 2.0, 3.0, 4.0],
    # "period":   [10, 20, 50],
}


# ── Helpers indicateurs ───────────────────────────────────────────────────────

def _compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """ATR sans look-ahead."""
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift(1)).abs()
    lc = (df["low"]  - df["close"].shift(1)).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def _compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """ADX pour classification trending/ranging — utilisé en stress test PHASE 5."""
    high, low, close = df["high"], df["low"], df["close"]
    plus_dm  = high.diff()
    minus_dm = -low.diff()
    plus_dm  = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm.shift(0)) & (minus_dm > 0), 0.0)
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(span=period, adjust=False).mean()
    plus_di  = 100 * plus_dm.ewm(span=period, adjust=False).mean()  / atr
    minus_di = 100 * minus_dm.ewm(span=period, adjust=False).mean() / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(span=period, adjust=False).mean()


# Ajouter ici les autres indicateurs (EMA, VWAP, pivots, etc.)


# ── Logique principale ────────────────────────────────────────────────────────

def run_backtest(
    df_15m: pd.DataFrame,
    ticker: str,
    tf=None,
    params: Optional[dict] = None,
    topstep_guard: bool = True,
) -> pd.DataFrame:
    """
    Exécute le backtest de la stratégie sur df_15m.

    Schéma de colonnes obligatoire (compatibilité core/optimizer.py) :
      date, dir, entry, sl, tp, sl_dist, tp_dist, rr, n_ct,
      result (TP|SL|TE|NOT_FILLED), pnl, fill_time, exit_time, exit, regime

    Colonnes optionnelles (utilisées par PHASE 5 stress tests) :
      pnl_gross, adx, atr_pct, is_macro_day

    `pnl` = P&L net (slippage + commissions inclus).
    """
    p = params or {}
    sl_mult = p.get("sl_mult", <STRATEGY_ID>_SL_ATR_MULT_PER_TICKER.get(ticker, 1.0))
    tp_mult = p.get("tp_mult", <STRATEGY_ID>_TP_ATR_MULT_PER_TICKER.get(ticker, 2.0))

    instr      = INSTRUMENTS[ticker]
    tick_size  = instr["tick_size"]
    pt_value   = instr["point_value"]
    slip_ticks = SLIPPAGE_TICKS_PER_TICKER.get(ticker, 1)
    slip_px    = slip_ticks * tick_size            # slippage en prix

    # Indicateurs (strictement sur passé, pas de look-ahead)
    df = df_15m.copy()
    df["atr"]      = _compute_atr(df)
    df["adx"]      = _compute_adx(df)
    df["atr_pct"]  = df["atr"].rolling(window=20*24, min_periods=50).rank(pct=True)
    # df["ema_fast"] = ...

    # Warmup = max période indicateur
    warmup = max(50, 14)

    trades = []
    daily_trades: dict = {}

    for i in range(warmup, len(df)):
        bar  = df.iloc[i]
        prev = df.iloc[i - 1]

        # Filtre fenêtre temporelle NY (DST-aware via zoneinfo)
        ts_ny = pd.Timestamp(df.index[i], tz="UTC").tz_convert(_NY)
        if not (<STRATEGY_ID>_HOUR_START_NY <= ts_ny.hour < <STRATEGY_ID>_HOUR_END_NY):
            continue
        if ts_ny.weekday() >= 5:
            continue

        date_str = ts_ny.date().isoformat()
        if daily_trades.get(date_str, 0) >= MAX_TRADES_PER_DAY:
            continue

        # ── Détection du signal (utilise prev, JAMAIS bar pour décider) ──
        # TODO: implémenter la logique de signal sur prev/df.iloc[:i]
        signal_long  = False
        signal_short = False

        if not (signal_long or signal_short):
            continue

        direction = "long" if signal_long else "short"
        entry     = bar["close"]                   # exécution sur clôture barre courante
        atr       = bar["atr"]

        sl_dist = atr * sl_mult
        tp_dist = atr * tp_mult
        sl      = entry - sl_dist if direction == "long" else entry + sl_dist
        tp      = entry + tp_dist if direction == "long" else entry - tp_dist
        rr      = tp_dist / sl_dist if sl_dist > 0 else 0

        # Sizing — risque cible incluant slippage anticipé
        n_ct = max(1, int(RISK_PER_TRADE_USD / ((sl_dist + slip_px) * pt_value)))

        # Garde-fou Topstep
        if topstep_guard:
            allowed, _ = trade_allowed(ticker, n_ct, sl_dist + slip_px)
            if not allowed:
                continue

        # ── Simulation fill + exit (CONSERVATIVE) ──────────────────────
        result    = "NOT_FILLED"
        fill_time = None
        exit_time = None
        exit_px   = None
        pnl_gross = 0.0
        pnl       = 0.0    # = pnl_net (canonique)

        for j in range(i + 1, min(i + <STRATEGY_ID>_ORDER_TIMEOUT_BARS + 1, len(df))):
            future = df.iloc[j]

            # Fill : prix atteint l'entrée (ordre limit)
            filled = False
            if direction == "long"  and future["low"]  <= entry:
                filled = True
            if direction == "short" and future["high"] >= entry:
                filled = True

            if not filled:
                continue

            fill_time = df.index[j]

            # ── RÈGLE CONSERVATIVE pour SL/TP dans la même barre ──
            # Sans M1, on ne peut départager. Hypothèse : SL prioritaire.
            sl_hit = (direction == "long"  and future["low"]  <= sl) or \
                     (direction == "short" and future["high"] >= sl)
            tp_hit = (direction == "long"  and future["high"] >= tp) or \
                     (direction == "short" and future["low"]  <= tp)

            if sl_hit and tp_hit:
                result, exit_px, exit_time = "SL", sl, df.index[j]   # conservatif
            elif sl_hit:
                result, exit_px, exit_time = "SL", sl, df.index[j]
            elif tp_hit:
                result, exit_px, exit_time = "TP", tp, df.index[j]
            else:
                # Suivre les barres suivantes pour TP/SL
                for k in range(j + 1, len(df)):
                    fut2 = df.iloc[k]
                    sl_k = (direction == "long"  and fut2["low"]  <= sl) or \
                           (direction == "short" and fut2["high"] >= sl)
                    tp_k = (direction == "long"  and fut2["high"] >= tp) or \
                           (direction == "short" and fut2["low"]  <= tp)
                    if sl_k and tp_k:
                        result, exit_px, exit_time = "SL", sl, df.index[k]
                        break
                    if sl_k:
                        result, exit_px, exit_time = "SL", sl, df.index[k]
                        break
                    if tp_k:
                        result, exit_px, exit_time = "TP", tp, df.index[k]
                        break
                else:
                    # Time-out → close à la dernière barre disponible
                    result    = "TE"
                    exit_px   = df.iloc[-1]["close"]
                    exit_time = df.index[-1]

            # ── Calcul P&L brut + net (slippage + commissions) ──
            raw_pnl   = (exit_px - entry) * (1 if direction == "long" else -1)
            pnl_gross = raw_pnl * n_ct * pt_value
            slip_cost = 2 * slip_px * n_ct * pt_value           # entrée + sortie
            comm_cost = COMMISSION_RT_PER_CONTRACT * n_ct       # round-trip
            pnl       = pnl_gross - slip_cost - comm_cost
            daily_trades[date_str] = daily_trades.get(date_str, 0) + 1
            break

        # Tagging régime (pour stress tests PHASE 5)
        if pd.notna(bar["adx"]) and bar["adx"] > 25:
            regime = "trending"
        elif pd.notna(bar["adx"]) and bar["adx"] < 20:
            regime = "ranging"
        else:
            regime = "neutral"

        is_macro = date_str in MACRO_EVENT_DATES

        trades.append({
            # ── Schéma standard (obligatoire pour core/optimizer.py) ──
            "date":         date_str,
            "dir":          direction,
            "entry":        entry,
            "sl":           sl,
            "tp":           tp,
            "sl_dist":      sl_dist,
            "tp_dist":      tp_dist,
            "rr":           round(rr, 2),
            "n_ct":         n_ct,
            "result":       result,
            "pnl":          round(pnl, 2),         # P&L NET (canonique)
            "fill_time":    fill_time,
            "exit_time":    exit_time,
            "exit":         exit_px,
            "regime":       regime,
            # ── Colonnes optionnelles (PHASE 5 stress tests) ──
            "pnl_gross":    round(pnl_gross, 2),
            "adx":          round(bar["adx"], 1) if pd.notna(bar["adx"]) else None,
            "atr_pct":      round(bar["atr_pct"], 2) if pd.notna(bar["atr_pct"]) else None,
            "is_macro_day": is_macro,
        })

    cols = ["date","dir","entry","sl","tp","sl_dist","tp_dist","rr","n_ct",
            "result","pnl","fill_time","exit_time","exit","regime",
            "pnl_gross","adx","atr_pct","is_macro_day"]
    return pd.DataFrame(trades) if trades else pd.DataFrame(columns=cols)


# ── Visualisation par jour ────────────────────────────────────────────────────

def plot_day(
    df_15m: pd.DataFrame,
    ticker: str,
    date_str: str,
    day_trades: list,
    output_path: str,
) -> None:
    """
    Chart complet d'une journée de trading.

    Affiche obligatoirement :
      - Chandeliers OHLC
      - Tous les indicateurs utilisés
      - Flèches d'entrée (▲ long vert, ▼ short rouge)
      - Niveaux SL (rouge pointillé) et TP (vert pointillé)
      - Zone de setup (rectangle semi-transparent)
      - Marqueur de sortie (×)
      - Titre avec ticker, date, P&L net du jour
    """
    ts = pd.to_datetime(df_15m.index).tz_localize("UTC").tz_convert(_NY)
    mask = ts.normalize() == pd.Timestamp(date_str, tz=_NY)
    day  = df_15m[mask.values].copy()
    if day.empty:
        return

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(16, 10), gridspec_kw={"height_ratios": [3, 1]}, sharex=True
    )

    x = range(len(day))

    # Chandeliers
    for i, (_, row) in enumerate(day.iterrows()):
        color = "#26a69a" if row["close"] >= row["open"] else "#ef5350"
        ax1.plot([i, i], [row["low"], row["high"]], color=color, lw=0.8)
        ax1.bar(i, abs(row["close"] - row["open"]),
                bottom=min(row["open"], row["close"]), color=color, width=0.6, alpha=0.9)

    # Indicateurs : exemple
    # if "ema_fast" in day.columns:
    #     ax1.plot(x, day["ema_fast"], color="#2196F3", lw=1.2, label="EMA fast")

    day_pnl = 0.0
    for trade in day_trades:
        if trade.get("result") == "NOT_FILLED":
            continue
        try:
            fill_idx = list(day.index).index(trade["fill_time"])
        except (ValueError, KeyError):
            continue
        try:
            exit_idx = list(day.index).index(trade["exit_time"])
        except (ValueError, KeyError):
            exit_idx = fill_idx

        color  = "#00c853" if trade["dir"] == "long" else "#d50000"
        marker = "^"       if trade["dir"] == "long" else "v"
        ax1.scatter(fill_idx, trade["entry"], marker=marker, color=color, s=150, zorder=5)
        ax1.scatter(exit_idx, trade["exit"],  marker="x", color="white", s=120, zorder=5)
        ax1.axhline(trade["sl"], color="#ef5350", ls="--", lw=0.8, alpha=0.7)
        ax1.axhline(trade["tp"], color="#26a69a", ls="--", lw=0.8, alpha=0.7)

        if trade.get("setup_low") and trade.get("setup_high"):
            ax1.axhspan(trade["setup_low"], trade["setup_high"],
                        alpha=0.08, color=color)

        day_pnl += trade.get("pnl", 0.0)   # `pnl` = net (canonique)

    # ATR
    if "atr" in day.columns:
        ax2.fill_between(x, 0, day["atr"].values, alpha=0.4, color="#7986cb")
        ax2.set_ylabel("ATR", fontsize=9)

    ax1.set_title(f"{ticker} — {date_str}   |   P&L net jour : {day_pnl:+.0f} $",
                  fontsize=12, fontweight="bold")
    ax1.legend(loc="upper left", fontsize=8)
    ax1.set_facecolor("#1a1a2e")
    ax2.set_facecolor("#1a1a2e")
    fig.patch.set_facecolor("#13131f")
    ax1.tick_params(colors="white")
    ax2.tick_params(colors="white")

    plt.tight_layout()
    plt.savefig(output_path, dpi=120, bbox_inches="tight")
    plt.close(fig)
```

## Charts portfolio — à placer dans `core/backtester.py`

Les charts qui ne dépendent pas de la stratégie (equity curve, DD underwater, monthly heatmap, distribution horaire) doivent vivre dans `core/backtester.py` pour être réutilisés par toutes les stratégies. **Ne pas dupliquer ces fonctions dans `strategies/<strategy_id>.py`.**

Si elles n'existent pas encore dans `core/backtester.py`, proposer leur ajout et **demander confirmation** avant de modifier `core/`.

Signatures suggérées :
```python
def plot_equity_curve(trades_df, output_path, is_end_date="2025-09-30"): ...
def plot_drawdown_underwater(trades_df, output_path): ...
def plot_monthly_heatmap(trades_df, output_path): ...
def plot_hourly_distribution(trades_df, output_path): ...
def plot_correlation_rolling(trades_df, ref_strategies_dfs, output_path): ...
```

## Notes d'implémentation

- **Fill conservatif** : la règle SL prioritaire en cas d'ambiguïté est essentielle. Sans elle, le WR peut être biaisé de 5–10 points.
- **`pnl` = net** : c'est la colonne canonique. `pnl_gross` est gardé pour debug.
- **Régime tagging** : ADX et percentile ATR sont calculés pour permettre les stress tests en PHASE 5 sans réexécuter le backtest.
- **Reproductibilité** : `np.random.seed(42)` dès l'import.
- **MACRO_EVENT_DATES** : à maintenir manuellement dans `config.py` ou via un script d'ingestion.
- **DST-aware** : `zoneinfo("America/New_York")` gère DST nativement, contrairement à `pytz`.
