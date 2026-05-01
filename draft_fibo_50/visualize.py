#!/usr/bin/env python3
"""
Génération de graphiques pour les N derniers trades de la stratégie.

Permet une revue visuelle des détections de tendance, des impulsions et
des entrées/sorties — pour vérifier que la logique de détection est cohérente
et corriger en cas de comportement aberrant.

Usage :
    python visualize.py --csv-dir ../data
    python visualize.py --csv-dir ../data --ticker NQ1 --n-trades 30
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec

sys.path.insert(0, str(Path(__file__).parent))

from config import (
    INSTRUMENTS, N_TRADES_VIEW, CHART_CONTEXT_BEFORE, CHART_CONTEXT_AFTER,
    ATR_PERIOD, EMA_FAST_PERIOD, EMA_SLOW_PERIOD, ADX_PERIOD,
    ADX_TREND_THRESHOLD,
)
from strategy import compute_atr, compute_ema, compute_adx
from backtest import load_csv


# ─────────────────────────────────────────────────────────────────────────────
# Style TradingView dark
# ─────────────────────────────────────────────────────────────────────────────

CHART_STYLE = {
    "figure.facecolor": "#131722",
    "axes.facecolor": "#131722",
    "axes.edgecolor": "#2a2e39",
    "axes.labelcolor": "#d1d4dc",
    "text.color": "#d1d4dc",
    "xtick.color": "#787b86",
    "ytick.color": "#787b86",
    "grid.color": "#1e222d",
    "grid.alpha": 0.6,
    "font.family": "sans-serif",
    "font.size": 9,
}

C_BULL = "#26a69a"
C_BEAR = "#ef5350"
C_EMA_FAST = "#42a5f5"
C_EMA_SLOW = "#ab47bc"
C_FIB = "#ffeb3b"
C_SWING = "#ffd54f"
C_ENTRY = "#26c6da"
C_TIMEOUT = "#ffa726"


# ─────────────────────────────────────────────────────────────────────────────
# Plot d'un trade individuel
# ─────────────────────────────────────────────────────────────────────────────

def plot_trade(df: pd.DataFrame, trade: dict, output_path: str):
    """
    Trace un trade avec son contexte :
      - Bougies OHLC simplifiées
      - EMA50 et EMA200
      - Niveaux swing_low / swing_high / fib_50
      - Entry / SL / TP entre fill et exit
      - Marqueurs fill (triangle) et exit (cercle/X coloré)
      - Sous-plot ADX avec seuil 20
    """
    plt.rcParams.update(CHART_STYLE)

    fill_idx = int(trade["fill_idx"])
    exit_idx = int(trade["exit_idx"])
    start = max(0, fill_idx - CHART_CONTEXT_BEFORE)
    end = min(len(df), exit_idx + CHART_CONTEXT_AFTER + 1)
    sub = df.iloc[start:end].copy()

    fig = plt.figure(figsize=(14, 8))
    gs = gridspec.GridSpec(2, 1, height_ratios=[3, 1], hspace=0.08)
    ax_price = fig.add_subplot(gs[0])
    ax_adx = fig.add_subplot(gs[1], sharex=ax_price)

    x = np.arange(len(sub))

    # ── Bougies ──────────────────────────────────────────────────────────
    for i_bar, (_, row) in enumerate(sub.iterrows()):
        col = C_BULL if row["close"] >= row["open"] else C_BEAR
        # mèche
        ax_price.plot([i_bar, i_bar], [row["low"], row["high"]], color=col, lw=0.8)
        # corps
        ax_price.plot([i_bar, i_bar], [row["open"], row["close"]], color=col, lw=2.6)

    # ── EMAs ─────────────────────────────────────────────────────────────
    ax_price.plot(x, sub["ema_fast"].values, color=C_EMA_FAST, lw=1.0,
                  label=f"EMA{EMA_FAST_PERIOD}")
    ax_price.plot(x, sub["ema_slow"].values, color=C_EMA_SLOW, lw=1.0,
                  label=f"EMA{EMA_SLOW_PERIOD}")

    # ── Niveaux swing & Fibonacci 50% ────────────────────────────────────
    swing_low = float(trade["swing_low"])
    swing_high = float(trade["swing_high"])
    fib_50 = (swing_low + swing_high) / 2.0

    ax_price.axhline(swing_high, color=C_SWING, ls="--", lw=0.8, alpha=0.55,
                     label="Swing high")
    ax_price.axhline(swing_low, color=C_SWING, ls="--", lw=0.8, alpha=0.55,
                     label="Swing low")
    ax_price.axhline(fib_50, color=C_FIB, ls="-", lw=1.1, alpha=0.85,
                     label=f"Fib 50% = {fib_50:.2f}")

    # ── Entry / SL / TP entre fill et exit ───────────────────────────────
    fill_x = fill_idx - start
    exit_x = exit_idx - start

    ax_price.plot([fill_x, exit_x], [trade["entry"], trade["entry"]],
                  color=C_ENTRY, lw=1.3,
                  label=f"Entry {trade['entry']:.2f}")
    ax_price.plot([fill_x, exit_x], [trade["sl"], trade["sl"]],
                  color=C_BEAR, lw=1.0, ls=":", alpha=0.75,
                  label=f"SL {trade['sl']:.2f}")
    ax_price.plot([fill_x, exit_x], [trade["tp"], trade["tp"]],
                  color=C_BULL, lw=1.0, ls=":", alpha=0.75,
                  label=f"TP {trade['tp']:.2f}")

    # ── Marqueurs ────────────────────────────────────────────────────────
    arrow_marker = "^" if trade["direction"] == "long" else "v"
    ax_price.scatter([fill_x], [float(trade["entry"])],
                     marker=arrow_marker, s=140, color=C_ENTRY,
                     zorder=5, label=f"Fill ({trade['direction']})")

    result = trade["result"]
    if result == "TP":
        exit_color, exit_marker = C_BULL, "o"
    elif result == "SL":
        exit_color, exit_marker = C_BEAR, "X"
    else:  # TE
        exit_color, exit_marker = C_TIMEOUT, "s"

    ax_price.scatter([exit_x], [float(trade["exit"])],
                     marker=exit_marker, s=140, color=exit_color,
                     zorder=5,
                     label=f"Exit {result} ({trade['pnl']:+.0f}$)")

    # ── ADX subplot ──────────────────────────────────────────────────────
    ax_adx.plot(x, sub["adx"].values, color=C_FIB, lw=1.0, label="ADX")
    ax_adx.axhline(ADX_TREND_THRESHOLD, color="#787b86", ls="--", lw=0.8,
                   alpha=0.7, label=f"Seuil {ADX_TREND_THRESHOLD:.0f}")
    ax_adx.fill_between(x, ADX_TREND_THRESHOLD, sub["adx"].values,
                        where=sub["adx"].values >= ADX_TREND_THRESHOLD,
                        color=C_BULL, alpha=0.10)
    ax_adx.set_ylabel("ADX", fontsize=8)
    ax_adx.legend(fontsize=7, loc="upper left")
    ax_adx.grid(True, alpha=0.3)

    # ── Titre / légende ──────────────────────────────────────────────────
    direction_label = "LONG" if trade["direction"] == "long" else "SHORT"
    title = (
        f"{trade['ticker']} — {direction_label}  "
        f"[{result}, P&L = {trade['pnl']:+.0f}$, RR = {trade['rr']:.1f}, "
        f"hold = {trade['bars_held']} bars]\n"
        f"Fill : {trade['fill_time']}    Exit : {trade['exit_time']}"
    )
    fig.suptitle(title, fontsize=10, color="#d1d4dc", y=0.995)

    ax_price.legend(loc="upper left", fontsize=7, ncol=2, framealpha=0.85)
    ax_price.set_ylabel("Price", fontsize=8)
    ax_price.grid(True, alpha=0.3)

    fig.savefig(
        output_path, dpi=110, bbox_inches="tight",
        facecolor=fig.get_facecolor()
    )
    plt.close(fig)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Génération graphiques des N derniers trades"
    )
    parser.add_argument("--csv-dir", type=str, default="../data")
    parser.add_argument("--ticker", type=str, default=None)
    parser.add_argument("--n-trades", type=int, default=N_TRADES_VIEW)
    args = parser.parse_args()

    out_dir = Path(__file__).parent / "output" / "charts"
    out_dir.mkdir(parents=True, exist_ok=True)

    tickers = [args.ticker] if args.ticker else list(INSTRUMENTS.keys())

    for ticker in tickers:
        trades_path = Path(__file__).parent / "output" / f"trades_{ticker}.csv"
        if not trades_path.exists():
            print(f"[!] {trades_path} manquant — lance backtest.py d'abord")
            continue

        csv_path = Path(args.csv_dir) / f"{ticker}_data_m15.csv"
        if not csv_path.exists():
            print(f"[!] {csv_path} introuvable")
            continue

        df = load_csv(str(csv_path))
        df["atr"] = compute_atr(df, ATR_PERIOD)
        df["ema_fast"] = compute_ema(df["close"], EMA_FAST_PERIOD)
        df["ema_slow"] = compute_ema(df["close"], EMA_SLOW_PERIOD)
        df["adx"] = compute_adx(df, ADX_PERIOD)

        trades = pd.read_csv(trades_path)
        if len(trades) == 0:
            print(f"  [{ticker}] aucun trade.")
            continue

        # Tri par fill_time pour garder l'ordre chronologique
        trades = trades.sort_values("fill_idx").reset_index(drop=True)
        last_trades = trades.tail(args.n_trades).reset_index(drop=True)

        ticker_dir = out_dir / ticker
        ticker_dir.mkdir(exist_ok=True)
        # Nettoyage des anciens charts pour éviter les confusions
        for old in ticker_dir.glob("*.png"):
            old.unlink()

        print(f"\n{ticker}: génération de {len(last_trades)} charts → {ticker_dir}")
        ok = 0
        for i_t, trade_row in last_trades.iterrows():
            trade = trade_row.to_dict()
            try:
                fname = (
                    f"{i_t+1:02d}_{trade['result']}_"
                    f"{int(trade['pnl']):+d}.png"
                )
                output_path = ticker_dir / fname
                plot_trade(df, trade, str(output_path))
                ok += 1
            except Exception as e:
                print(f"  [!] trade #{i_t+1} ({trade.get('fill_time')}): {e}")
        print(f"  → {ok}/{len(last_trades)} charts générés.")


if __name__ == "__main__":
    main()
