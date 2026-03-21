"""
Graphiques de signaux style TradingView.
"""

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch

from config import (
    INSTRUMENTS, CHART_STYLE, CHART_CANDLES,
    BACKTEST_CHART_CONTEXT_BEFORE, BACKTEST_CHART_CONTEXT_AFTER,
)


TV_GREEN = "#26a69a"
TV_RED = "#ef5350"
TV_BLUE = "#2962ff"
TV_ORANGE = "#ff9800"
TV_BG = "#131722"


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _draw_candles(ax, data, x=None):
    """Dessine les bougies OHLCV sur l'axe."""
    n = len(data)
    if x is None:
        x = np.arange(n)
    o = data["open"].values
    h = data["high"].values
    l = data["low"].values
    c = data["close"].values
    bull = c >= o
    bw = max(0.35, min(0.7, 100 / n))

    for i in range(n):
        clr = TV_GREEN if bull[i] else TV_RED
        ax.plot([x[i], x[i]], [l[i], h[i]], color=clr, lw=0.6, alpha=0.9)
        body = max(o[i], c[i]) - min(o[i], c[i])
        if body > 0:
            ax.bar(x[i], body, bottom=min(o[i], c[i]),
                   width=bw, color=clr, edgecolor=clr, lw=0)


def _draw_x_axis(ax, data, n):
    """Configure l'axe X avec les labels date/heure."""
    step = max(1, n // 8)
    ticks = list(range(0, n, step))
    prev_d = ""
    labels = []
    for t in ticks:
        dt = data.index[t]
        d = dt.strftime("%d/%m")
        tm = dt.strftime("%H:%M")
        labels.append(f"{d}\n{tm}" if d != prev_d else tm)
        prev_d = d
    ax.set_xticks(ticks)
    ax.set_xticklabels(labels, fontsize=7)


# ─────────────────────────────────────────────────────────────
# Signal chart (mode live / pré-trade)
# ─────────────────────────────────────────────────────────────

def plot_signal(
    df_15m: pd.DataFrame,
    signal: dict,
    cutoff: pd.Timestamp,
    output_path: str,
    n_candles: int = CHART_CANDLES,
):
    """
    Génère un graphique OHLC 15min avec le signal (entry, SL, TP, zone).
    Sauvegarde en PNG.
    """
    plt.rcParams.update(CHART_STYLE)
    ticker = signal["ticker"]
    dpp = INSTRUMENTS[ticker]["dollar_per_point"]

    data = df_15m[df_15m.index <= cutoff].iloc[-n_candles:]
    if len(data) < 50:
        return

    fig, ax = plt.subplots(1, 1, figsize=(16, 8))

    n = len(data)
    _draw_candles(ax, data)

    # Échelle prix
    entry, sl, tp = signal["entry"], signal["sl"], signal["tp"]
    all_levels = [entry, sl, tp]
    chart_min = min(data["low"].min(), min(all_levels))
    chart_max = max(data["high"].max(), max(all_levels))
    margin = (chart_max - chart_min) * 0.05
    ax.set_ylim(chart_min - margin, chart_max + margin)

    # Zone S/R
    ax.axhspan(signal["zone_low"], signal["zone_high"], color=TV_BLUE, alpha=0.12)

    # Niveaux
    ax.axhline(entry, color=TV_BLUE, ls="-", lw=1.8, alpha=0.9)
    ax.plot([0, n + 40], [sl, sl], color=TV_RED, ls="--", lw=1.2, alpha=0.8)
    ax.plot([0, n + 40], [tp, tp], color=TV_GREEN, ls="--", lw=1.2, alpha=0.8)

    # Zones colorées
    if signal["direction"] == "long":
        ax.axhspan(sl, entry, color=TV_RED, alpha=0.04)
        ax.axhspan(entry, tp, color=TV_GREEN, alpha=0.04)
    else:
        ax.axhspan(entry, sl, color=TV_RED, alpha=0.04)
        ax.axhspan(tp, entry, color=TV_GREEN, alpha=0.04)

    # Labels
    lx = n + 2
    gain = signal["n_ct"] * signal["tp_dist"] * dpp

    ax.text(lx, entry, f"  ► ENTRY {entry:.2f}", fontsize=8, color=TV_BLUE,
            va="center", fontweight="bold",
            bbox=dict(fc=TV_BG, ec=TV_BLUE, alpha=0.9, pad=3, boxstyle="round,pad=0.3"))
    ax.text(lx, sl,
            f"  ✗ SL {sl:.2f}\n    {signal['sl_dist']:.1f}pts | -${signal['risk']:.0f}",
            fontsize=7.5, color=TV_RED, va="center",
            bbox=dict(fc=TV_BG, ec=TV_RED, alpha=0.9, pad=3, boxstyle="round,pad=0.3"))
    ax.text(lx, tp,
            f"  ✓ TP {tp:.2f}\n    {signal['tp_dist']:.1f}pts | +${gain:.0f}",
            fontsize=7.5, color=TV_GREEN, va="center",
            bbox=dict(fc=TV_BG, ec=TV_GREEN, alpha=0.9, pad=3, boxstyle="round,pad=0.3"))

    # Prix actuel
    price_now = data["close"].iloc[-1]
    ax.axhline(price_now, color="#ffffff", ls=":", lw=0.8, alpha=0.4)
    ax.text(n - 1, price_now, f" {price_now:.2f}", fontsize=7, color="#ffffff",
            va="bottom", ha="right", alpha=0.5)

    # Axe X
    _draw_x_axis(ax, data, n)
    ax.set_xlim(-2, n + n * 0.22)
    ax.yaxis.tick_right()
    ax.grid(True, alpha=0.5, color="#1e222d")

    # Titre
    arrow = "▲ LONG" if signal["direction"] == "long" else "▼ SHORT"
    ax.set_title(
        f"{ticker}  •  15min  •  {arrow}  •  "
        f"Q={signal['quality']:.0f} ({signal['n_tf']}TF, {signal['touches']}t)  •  "
        f"{signal['regime']}  •  RR={signal['rr']}",
        fontsize=11, pad=10, loc="left", color="#d1d4dc", fontweight="bold",
    )
    ax.text(0.99, 0.97,
            f"{signal['n_ct']} micro(s)  •  risque ${signal['risk']:.0f}",
            transform=ax.transAxes, fontsize=8, ha="right", va="top", color="#787b86")

    fig.tight_layout()
    fig.savefig(output_path, dpi=180, bbox_inches="tight", facecolor=TV_BG)
    plt.close(fig)


# ─────────────────────────────────────────────────────────────
# Backtest trade chart (post-trade, avec exécution)
# ─────────────────────────────────────────────────────────────

def plot_backtest_trade(
    df_15m: pd.DataFrame,
    trade: dict,
    ticker: str,
    output_path: str,
):
    """
    Graphique OHLC d'un trade backtest avec marqueurs fill/exit,
    zone S/R, niveaux SL/TP, et résultat coloré.
    """
    plt.rcParams.update(CHART_STYLE)
    dpp = INSTRUMENTS[ticker]["dollar_per_point"]

    # ── Fenêtre visible ──────────────────────────────────────
    fill_ts = pd.Timestamp(trade["fill_time"])
    exit_ts = pd.Timestamp(trade["exit_time"])

    fill_loc = df_15m.index.get_indexer([fill_ts], method="pad")[0]
    exit_loc = df_15m.index.get_indexer([exit_ts], method="pad")[0]

    if fill_loc < 0 or exit_loc < 0:
        return  # Données insuffisantes

    ctx_before = BACKTEST_CHART_CONTEXT_BEFORE
    ctx_after = BACKTEST_CHART_CONTEXT_AFTER

    start = max(0, fill_loc - ctx_before)
    end = min(len(df_15m), exit_loc + ctx_after + 1)
    data = data_slice = df_15m.iloc[start:end]

    if len(data) < 10:
        return

    fig, ax = plt.subplots(1, 1, figsize=(16, 8))
    n = len(data)

    # ── Bougies ──────────────────────────────────────────────
    _draw_candles(ax, data)

    # ── Positions relatives fill/exit dans la fenêtre ────────
    fill_x = fill_loc - start
    exit_x = exit_loc - start

    entry = trade["entry"]
    sl = trade["sl"]
    tp = trade["tp"]
    exit_price = trade["exit"]
    result = trade["result"]
    pnl = trade["pnl"]
    direction = trade["dir"]

    # ── Échelle Y ────────────────────────────────────────────
    all_levels = [entry, sl, tp]
    if exit_price is not None:
        all_levels.append(exit_price)
    chart_min = min(data["low"].min(), min(all_levels))
    chart_max = max(data["high"].max(), max(all_levels))
    margin = (chart_max - chart_min) * 0.06
    ax.set_ylim(chart_min - margin, chart_max + margin)

    # ── Zone S/R ─────────────────────────────────────────────
    ax.axhspan(trade["zone_low"], trade["zone_high"],
               color=TV_BLUE, alpha=0.12, zorder=0)

    # ── Région du trade (fill → exit) ────────────────────────
    trade_color = TV_GREEN if pnl > 0 else TV_RED
    if exit_price is not None:
        y_low = min(entry, exit_price)
        y_high = max(entry, exit_price)
        ax.fill_between(
            [fill_x, exit_x], y_low, y_high,
            color=trade_color, alpha=0.08, zorder=1,
        )
        # Ligne de position (entry → exit)
        ax.plot([fill_x, exit_x], [entry, entry],
                color=trade_color, ls="--", lw=1.0, alpha=0.5, zorder=2)

    # ── Niveaux SL / TP (lignes horizontales) ────────────────
    ax.axhline(entry, color=TV_BLUE, ls="-", lw=1.8, alpha=0.9, zorder=3)
    ax.axhline(sl, color=TV_RED, ls="--", lw=1.2, alpha=0.6, zorder=3)
    ax.axhline(tp, color=TV_GREEN, ls="--", lw=1.2, alpha=0.6, zorder=3)

    # ── Zones de risque / gain (fond subtil) ─────────────────
    if direction == "long":
        ax.axhspan(sl, entry, color=TV_RED, alpha=0.03, zorder=0)
        ax.axhspan(entry, tp, color=TV_GREEN, alpha=0.03, zorder=0)
    else:
        ax.axhspan(entry, sl, color=TV_RED, alpha=0.03, zorder=0)
        ax.axhspan(tp, entry, color=TV_GREEN, alpha=0.03, zorder=0)

    # ── Marqueur FILL (triangle) ─────────────────────────────
    fill_marker = "^" if direction == "long" else "v"
    ax.scatter(fill_x, entry, marker=fill_marker, s=120, color=TV_BLUE,
               edgecolors="white", linewidths=0.8, zorder=5)

    # ── Marqueur EXIT (cercle) ───────────────────────────────
    if exit_price is not None:
        exit_colors = {"TP": TV_GREEN, "SL": TV_RED, "TE": TV_ORANGE}
        exit_clr = exit_colors.get(result, "#ffffff")
        ax.scatter(exit_x, exit_price, marker="o", s=100, color=exit_clr,
                   edgecolors="white", linewidths=0.8, zorder=5)

    # ── Labels prix (côté droit) ─────────────────────────────
    lx = n + 2
    gain_pot = trade["n_ct"] * trade["tp_dist"] * dpp

    ax.text(lx, entry, f"  ► ENTRY {entry:.2f}", fontsize=8, color=TV_BLUE,
            va="center", fontweight="bold",
            bbox=dict(fc=TV_BG, ec=TV_BLUE, alpha=0.9, pad=3, boxstyle="round,pad=0.3"))
    ax.text(lx, sl,
            f"  ✗ SL {sl:.2f}  ({trade['sl_dist']:.1f}pts | -${trade['risk_$']:.0f})",
            fontsize=7.5, color=TV_RED, va="center",
            bbox=dict(fc=TV_BG, ec=TV_RED, alpha=0.9, pad=3, boxstyle="round,pad=0.3"))
    ax.text(lx, tp,
            f"  ✓ TP {tp:.2f}  ({trade['tp_dist']:.1f}pts | +${gain_pot:.0f})",
            fontsize=7.5, color=TV_GREEN, va="center",
            bbox=dict(fc=TV_BG, ec=TV_GREEN, alpha=0.9, pad=3, boxstyle="round,pad=0.3"))

    # ── Badge résultat (coin supérieur droit) ────────────────
    result_colors = {"TP": TV_GREEN, "SL": TV_RED, "TE": TV_ORANGE}
    result_clr = result_colors.get(result, "#ffffff")
    badge = f"{result}  ${pnl:+.0f}"
    ax.text(0.99, 0.97, badge,
            transform=ax.transAxes, fontsize=13, ha="right", va="top",
            color=result_clr, fontweight="bold",
            bbox=dict(fc=TV_BG, ec=result_clr, alpha=0.9, pad=5,
                      boxstyle="round,pad=0.4"))

    # ── Axe X ────────────────────────────────────────────────
    _draw_x_axis(ax, data, n)
    ax.set_xlim(-2, n + n * 0.18)
    ax.yaxis.tick_right()
    ax.grid(True, alpha=0.5, color="#1e222d")

    # ── Titre ────────────────────────────────────────────────
    arrow = "▲ LONG" if direction == "long" else "▼ SHORT"
    ax.set_title(
        f"{ticker}  •  15min  •  {arrow}  •  "
        f"Q={trade['quality']:.0f} ({trade['n_tf']}TF, {trade['touches']}t)  •  "
        f"{trade['regime']}  •  RR={trade['rr']}  •  {trade['date']}",
        fontsize=11, pad=10, loc="left", color="#d1d4dc", fontweight="bold",
    )

    # Sous-titre : contrats, risque, horaires fill/exit
    fill_h = fill_ts.strftime("%H:%M")
    exit_h = exit_ts.strftime("%H:%M")
    ax.text(0.99, 0.91,
            f"{trade['n_ct']} micro(s)  •  risque ${trade['risk_$']:.0f}"
            f"  •  fill {fill_h} → exit {exit_h}",
            transform=ax.transAxes, fontsize=8, ha="right", va="top", color="#787b86")

    fig.tight_layout()
    fig.savefig(output_path, dpi=180, bbox_inches="tight", facecolor=TV_BG)
    plt.close(fig)
