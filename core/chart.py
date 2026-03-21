"""
Graphiques de signaux style TradingView.
"""

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from config import INSTRUMENTS, CHART_STYLE, CHART_CANDLES


TV_GREEN = "#26a69a"
TV_RED = "#ef5350"
TV_BLUE = "#2962ff"
TV_ORANGE = "#ff9800"


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
    x = np.arange(n)
    o, h, l, c = data["open"].values, data["high"].values, data["low"].values, data["close"].values
    bull = c >= o
    bw = max(0.35, min(0.7, 100 / n))

    # Bougies
    for i in range(n):
        clr = TV_GREEN if bull[i] else TV_RED
        ax.plot([x[i], x[i]], [l[i], h[i]], color=clr, lw=0.6, alpha=0.9)
        if max(o[i], c[i]) - min(o[i], c[i]) > 0:
            ax.bar(x[i], max(o[i], c[i]) - min(o[i], c[i]),
                   bottom=min(o[i], c[i]), width=bw, color=clr, edgecolor=clr, lw=0)

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
            bbox=dict(fc="#131722", ec=TV_BLUE, alpha=0.9, pad=3, boxstyle="round,pad=0.3"))
    ax.text(lx, sl,
            f"  ✗ SL {sl:.2f}\n    {signal['sl_dist']:.1f}pts | -${signal['risk']:.0f}",
            fontsize=7.5, color=TV_RED, va="center",
            bbox=dict(fc="#131722", ec=TV_RED, alpha=0.9, pad=3, boxstyle="round,pad=0.3"))
    ax.text(lx, tp,
            f"  ✓ TP {tp:.2f}\n    {signal['tp_dist']:.1f}pts | +${gain:.0f}",
            fontsize=7.5, color=TV_GREEN, va="center",
            bbox=dict(fc="#131722", ec=TV_GREEN, alpha=0.9, pad=3, boxstyle="round,pad=0.3"))

    # Prix actuel
    price_now = data["close"].iloc[-1]
    ax.axhline(price_now, color="#ffffff", ls=":", lw=0.8, alpha=0.4)
    ax.text(n - 1, price_now, f" {price_now:.2f}", fontsize=7, color="#ffffff",
            va="bottom", ha="right", alpha=0.5)

    # Axe X
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
    fig.savefig(output_path, dpi=180, bbox_inches="tight", facecolor="#131722")
    plt.close(fig)
