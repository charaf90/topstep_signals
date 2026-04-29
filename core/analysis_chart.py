"""
Graphique d'analyse journalier (1 image par jour tradé / ticker).

Conçu pour archiver une "photographie" complète de la journée vue par la
stratégie : contexte de marché (200 bougies avant cutoff + session US),
zones S/R par timeframe, signaux générés, exécutions et features
décisionnelles. Sert de support visuel pour comparer plusieurs stratégies
en parallèle (un sous-dossier par STRATEGY_VERSION).

L'échelle Y est strictement basée sur le range de prix de la fenêtre
visible (low.min / high.max), pas sur les zones — cela garantit qu'on voit
toujours le mouvement du prix en clair.
"""

from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
from matplotlib.lines import Line2D

from zoneinfo import ZoneInfo

from config import (
    INSTRUMENTS, CHART_STYLE,
    ANALYSIS_CHART_CONTEXT_BEFORE,
    OPR_TIMEZONE,
)


# ─────────────────────────────────────────────────────────────────────────
# Palette
# ─────────────────────────────────────────────────────────────────────────
TV_GREEN = "#26a69a"
TV_RED = "#ef5350"
TV_BLUE = "#2962ff"
TV_ORANGE = "#ff9800"
TV_BG = "#131722"
TV_FG = "#d1d4dc"
TV_DIM = "#787b86"

# Couleur par TF — D1 le plus saillant, 15m le plus discret
TF_COLORS = {
    "D1":  "#f7b801",   # ambre
    "H4":  "#a566ff",   # violet
    "H1":  "#42a5f5",   # bleu clair
    "15m": "#9e9e9e",   # gris
    "OPR": "#ffd54f",   # jaune (zone OPR)
}

EXIT_COLORS = {"TP": TV_GREEN, "SL": TV_RED, "TE": TV_ORANGE}


# ─────────────────────────────────────────────────────────────────────────
# Helpers de tracé
# ─────────────────────────────────────────────────────────────────────────

def _draw_candles(ax, data, x):
    """Bougies OHLC sur axe x continu (mèches + corps)."""
    o = data["open"].values
    h = data["high"].values
    l = data["low"].values
    c = data["close"].values
    bull = c >= o
    n = len(data)
    bw = max(0.35, min(0.7, 120 / max(n, 1)))

    for i in range(n):
        clr = TV_GREEN if bull[i] else TV_RED
        ax.plot([x[i], x[i]], [l[i], h[i]], color=clr, lw=0.6, alpha=0.9, zorder=2)
        body = max(o[i], c[i]) - min(o[i], c[i])
        if body > 0:
            ax.bar(x[i], body, bottom=min(o[i], c[i]),
                   width=bw, color=clr, edgecolor=clr, lw=0, zorder=2)


def _draw_x_axis(ax, data, n, tz=None):
    """
    Axe X avec date + heure (tick toutes ~n/10 bougies).

    Si `tz` est fourni, les heures sont affichées dans ce fuseau (typiquement
    America/New_York) — l'index `data` est attendu en UTC naïf et on
    convertit pour l'affichage seulement, pas pour l'indexation. Ça gère
    DST proprement : 9h30 NY reste 9h30 NY toute l'année sur le graphique.
    """
    step = max(1, n // 10)
    ticks = list(range(0, n, step))
    if ticks[-1] != n - 1:
        ticks.append(n - 1)
    prev_d = ""
    labels = []
    for t in ticks:
        dt = data.index[t]
        if tz is not None:
            ts = pd.Timestamp(dt)
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            dt = ts.tz_convert(tz)
        d = dt.strftime("%d/%m")
        tm = dt.strftime("%H:%M")
        labels.append(f"{d}\n{tm}" if d != prev_d else tm)
        prev_d = d
    ax.set_xticks(ticks)
    ax.set_xticklabels(labels, fontsize=7)


def _draw_volume_bars(ax, data, x):
    """
    Sous-plot volume style TradingView : barres verticales colorées
    (vert quand close >= open, rouge sinon), avec marge supérieure pour
    aérer le graphique.
    """
    if "volume" not in data.columns:
        return
    o = data["open"].values
    c = data["close"].values
    v = data["volume"].values.astype(float)
    bull = c >= o
    n = len(data)
    bw = max(0.35, min(0.7, 120 / max(n, 1)))
    colors = np.where(bull, TV_GREEN, TV_RED)
    ax.bar(x, v, width=bw, color=colors, alpha=0.55,
           edgecolor=colors, linewidth=0, zorder=2)
    vmax = float(v.max()) if v.size else 1.0
    ax.set_ylim(0, vmax * 1.15)
    ax.set_yticks([])
    ax.set_ylabel("vol", color=TV_DIM, fontsize=7, rotation=0,
                  ha="right", va="center")
    ax.grid(True, alpha=0.4, color="#1e222d", zorder=0)


def _format_pm(pm: Optional[Dict]) -> str:
    if not pm:
        return "n/a"
    return (
        f"ovn_eff={pm.get('ovn_path_eff', 0):.2f}  "
        f"prev_ret={pm.get('prev_return', 0):+.2f}%  "
        f"prev_close_pos={pm.get('prev_close_pos', 0.5):.2f}"
    )


def _format_vol(vol: Optional[Dict]) -> str:
    if not vol:
        return "n/a"
    return (
        f"atr_d={vol['atr_daily']:.1f}  "
        f"atr_ratio={vol['atr_ratio']:.2f}  "
        f"gap_atr={vol['gap_atr']:.2f}  "
        f"vol_score={vol['vol_score']:.2f}"
    )


# ─────────────────────────────────────────────────────────────────────────
# Plot principal
# ─────────────────────────────────────────────────────────────────────────

def plot_day_analysis(
    df_15m: pd.DataFrame,
    ticker: str,
    date_str: str,
    cutoff: pd.Timestamp,
    us_end: pd.Timestamp,
    zones: List[Dict],
    signals: List[Dict],
    trades: List[Dict],
    regime: Optional[str],
    alignment_score: Optional[float],
    pm_features: Optional[Dict],
    vol_features: Optional[Dict],
    output_path: str,
    context_before: int = None,
):
    """
    Génère un graphique d'analyse couvrant une journée tradée complète.

    Args:
        df_15m         : DataFrame 15min indexé en datetime.
        ticker         : actif ("MES1" | "NQ1" | "YM1").
        date_str       : "YYYY-MM-DD" — utilisé pour le titre et le nom de fichier.
        cutoff         : timestamp d'analyse (heure du calcul des zones).
        us_end         : fin de la session US visualisée.
        zones          : zones S/R agrégées (telles que vues par la stratégie).
                         Chaque zone doit contenir au minimum
                         {low, high, mid, quality, n_tf, touches, tfs, dominant_tf}.
        signals        : liste de tous les signaux générés ce jour-là (avant
                         simulation), incluant ceux non remplis.
        trades         : liste des résultats simulés (1:1 avec signals si
                         possible, sinon liste partielle des trades remplis).
                         Chaque trade peut contenir result, pnl, fill_time,
                         exit_time, exit.
        regime         : "BULL" | "BEAR" | "RANGE" | None.
        alignment_score: score d'alignement de tendance ∈ [-1, +1].
        pm_features    : dict des features pré-marché (ovn_path_eff, ...).
        vol_features   : dict des features de volatilité (atr_daily, ...).
        output_path    : chemin PNG de sortie.
        context_before : nombre de bougies avant cutoff (défaut config).
    """
    plt.rcParams.update(CHART_STYLE)
    inst = INSTRUMENTS[ticker]
    dpp = inst["dollar_per_point"]
    tick = inst["tick_size"]

    cb = context_before if context_before is not None else ANALYSIS_CHART_CONTEXT_BEFORE

    # ── Fenêtre visible ──────────────────────────────────────────────────
    pre = df_15m[df_15m.index < cutoff].iloc[-cb:]
    post = df_15m[(df_15m.index >= cutoff) & (df_15m.index <= us_end)]
    data = pd.concat([pre, post])
    if len(data) < 30:
        return False

    n = len(data)
    n_pre = len(pre)
    x = np.arange(n)

    # Layout : prix (large) + volume (compact) — style TradingView.
    # `gridspec_kw` donne 4 fois plus de hauteur au prix qu'au volume.
    fig, (ax, ax_vol) = plt.subplots(
        2, 1, figsize=(18, 10), sharex=True,
        gridspec_kw={"height_ratios": [4, 1], "hspace": 0.05},
    )

    # ── Bougies ──────────────────────────────────────────────────────────
    _draw_candles(ax, data, x)

    # ── Volume (sous-plot) ───────────────────────────────────────────────
    _draw_volume_bars(ax_vol, data, x)

    # ── Échelle Y basée sur le PRIX (pas les zones) ──────────────────────
    price_min = float(data["low"].min())
    price_max = float(data["high"].max())
    span = price_max - price_min
    margin = max(span * 0.06, 4 * tick)
    y_lo = price_min - margin
    y_hi = price_max + margin
    ax.set_ylim(y_lo, y_hi)

    # ── Zones S/R agrégées ───────────────────────────────────────────────
    # Chaque zone est dessinée en bande horizontale colorée par TF dominante.
    # On limite à un nombre raisonnable pour ne pas saturer.
    visible_zones = [
        z for z in zones
        if z["high"] >= y_lo and z["low"] <= y_hi
    ]
    visible_zones = sorted(visible_zones, key=lambda z: -z["quality"])[:12]

    used_tfs = set()
    for z in visible_zones:
        dom_tf = z.get("dominant_tf", z.get("tfs", ["15m"])[0] if z.get("tfs") else "15m")
        clr = TF_COLORS.get(dom_tf, TV_DIM)
        used_tfs.add(dom_tf)

        # Borne X de la zone : par défaut toute la largeur visible. Si la
        # zone porte un `start_time` (ex. zone OPR), on commence à l'index
        # correspondant — la zone n'apparaît PAS avant son moment de
        # création. Idem pour `end_time` (par défaut : fin de fenêtre).
        x0, x1 = 0, n - 1
        st = z.get("start_time")
        et = z.get("end_time")
        if st is not None:
            st_ts = pd.Timestamp(st)
            if data.index[0] <= st_ts <= data.index[-1]:
                x0 = int(data.index.get_indexer([st_ts], method="pad")[0])
            elif st_ts > data.index[-1]:
                # zone démarrant hors fenêtre visible : on saute le tracé
                continue
        if et is not None:
            et_ts = pd.Timestamp(et)
            if data.index[0] <= et_ts <= data.index[-1]:
                x1 = int(data.index.get_indexer([et_ts], method="pad")[0])

        # Bande bornée (rectangle prix)
        ax.fill_between([x0 - 0.5, x1 + 0.5],
                        z["low"], z["high"],
                        color=clr, alpha=0.18, zorder=1)
        # Bordures fines
        ax.plot([x0 - 0.5, x1 + 0.5], [z["low"], z["low"]],
                color=clr, lw=0.7, alpha=0.7, zorder=1)
        ax.plot([x0 - 0.5, x1 + 0.5], [z["high"], z["high"]],
                color=clr, lw=0.7, alpha=0.7, zorder=1)

        # Étiquette : à gauche pour zones full-width, sinon contre la
        # bordure gauche de la zone bornée.
        tfs_str = "+".join(z.get("tfs", [dom_tf]))
        if st is not None and x0 > 0:
            ax.text(
                x0, z["mid"],
                f" {tfs_str} Q{z['quality']:.0f} ({z['touches']}t)",
                fontsize=6.5, color=clr, va="center", ha="left", alpha=0.95,
            )
        else:
            ax.text(
                -2, z["mid"],
                f"{tfs_str} Q{z['quality']:.0f} ({z['touches']}t)",
                fontsize=6.5, color=clr, va="center", ha="right", alpha=0.95,
            )

    # ── Marqueur cutoff (verticale) ──────────────────────────────────────
    ax.axvline(n_pre - 0.5, color=TV_FG, ls=":", lw=1.0, alpha=0.55, zorder=3)
    ax_vol.axvline(n_pre - 0.5, color=TV_FG, ls=":", lw=1.0,
                   alpha=0.55, zorder=3)
    ax.text(
        n_pre - 0.5, y_hi, "  cutoff",
        fontsize=7.5, color=TV_FG, va="top", ha="left", alpha=0.65,
    )

    # ── Signaux & trades ─────────────────────────────────────────────────
    # On affiche entry/SL/TP de chaque signal + marqueur fill / exit s'il
    # existe un trade correspondant. On les superpose à droite de la zone.
    label_x = n + 1
    sig_count = len(signals)

    # Mapper trade par signal — désambiguïsation par `trigger_time` quand
    # plusieurs signaux partagent la même direction/entrée (cas OPR : tous
    # les longs entrent au niveau opr_high). On consomme les trades au fur
    # et à mesure pour garantir un mapping 1:1.
    _consumed_ids = set()

    def _match_trade(sig):
        sig_trig = sig.get("trigger_time")
        # 1) Match exact sur trigger_time (le plus fiable pour OPR)
        if sig_trig is not None:
            for i, t in enumerate(trades):
                if i in _consumed_ids:
                    continue
                if t.get("trigger_time") == sig_trig:
                    _consumed_ids.add(i)
                    return t
        # 2) Fallback : direction + entry (signaux composite uniques)
        for i, t in enumerate(trades):
            if i in _consumed_ids:
                continue
            if (
                t.get("dir") == sig["direction"]
                and abs(float(t.get("entry", 0)) - float(sig["entry"])) < tick / 2
            ):
                _consumed_ids.add(i)
                return t
        return None

    for idx, sig in enumerate(signals, 1):
        entry = sig["entry"]
        sl = sig["sl"]
        tp = sig["tp"]
        direction = sig["direction"]
        sig_clr = TV_GREEN if direction == "long" else TV_RED

        # Lignes horizontales légères (scope = post-cutoff jusqu'à fin)
        x_left = n_pre - 0.5
        x_right = n - 1 + 0.5
        ax.plot([x_left, x_right], [entry, entry], color=TV_BLUE,
                ls="-", lw=1.4, alpha=0.85, zorder=4)
        ax.plot([x_left, x_right], [sl, sl], color=TV_RED,
                ls="--", lw=1.0, alpha=0.7, zorder=4)
        ax.plot([x_left, x_right], [tp, tp], color=TV_GREEN,
                ls="--", lw=1.0, alpha=0.7, zorder=4)

        # Étiquettes droite : E#, SL#, TP#
        ax.text(label_x, entry, f"  E{idx} {entry:.2f}",
                fontsize=7, color=TV_BLUE, va="center", fontweight="bold",
                bbox=dict(fc=TV_BG, ec=TV_BLUE, alpha=0.85,
                          pad=2, boxstyle="round,pad=0.25"),
                zorder=5)
        ax.text(label_x, sl, f"  SL{idx} {sl:.2f}",
                fontsize=6.5, color=TV_RED, va="center",
                bbox=dict(fc=TV_BG, ec=TV_RED, alpha=0.85,
                          pad=2, boxstyle="round,pad=0.25"),
                zorder=5)
        ax.text(label_x, tp, f"  TP{idx} {tp:.2f}",
                fontsize=6.5, color=TV_GREEN, va="center",
                bbox=dict(fc=TV_BG, ec=TV_GREEN, alpha=0.85,
                          pad=2, boxstyle="round,pad=0.25"),
                zorder=5)

        # Marqueur fill / exit si trade simulé.
        # Les timestamps OPR sont tz-aware NY ; on les ramène à la même
        # base que `data.index` (UTC naïf) pour pouvoir indexer.
        def _to_naive_utc(ts):
            t = pd.Timestamp(ts)
            if t.tzinfo is not None:
                t = t.tz_convert("UTC").tz_localize(None)
            return t

        tr = _match_trade(sig)
        if tr and tr.get("result") and tr["result"] != "NOT_FILLED":
            ft = tr.get("fill_time")
            et = tr.get("exit_time")
            if ft is not None:
                ft = _to_naive_utc(ft)
                if data.index[0] <= ft <= data.index[-1]:
                    fx = int(data.index.get_indexer([ft], method="pad")[0])
                    marker = "^" if direction == "long" else "v"
                    ax.scatter(fx, entry, marker=marker, s=85,
                               color=TV_BLUE, edgecolors="white",
                               linewidths=0.7, zorder=6)
                    ax.text(fx, entry, f" #{idx}", fontsize=6.5,
                            color=TV_BLUE, va="bottom", ha="left",
                            zorder=6)
            exit_price = tr.get("exit")
            if et is not None and exit_price is not None:
                et = _to_naive_utc(et)
                if data.index[0] <= et <= data.index[-1]:
                    ex = int(data.index.get_indexer([et], method="pad")[0])
                    ec = EXIT_COLORS.get(tr["result"], "#ffffff")
                    ax.scatter(ex, float(exit_price), marker="o", s=80,
                               color=ec, edgecolors="white",
                               linewidths=0.7, zorder=6)
                    ax.text(
                        ex, float(exit_price),
                        f" {tr['result']} ${tr.get('pnl', 0):+.0f}",
                        fontsize=6.5, color=ec, va="bottom", ha="left",
                        fontweight="bold", zorder=6,
                    )

    # ── Bandeau récap des signaux (haut gauche) ──────────────────────────
    lines = []
    arrow_map = {"long": "▲", "short": "▼"}
    for idx, sig in enumerate(signals, 1):
        tr = _match_trade(sig)
        result_tag = ""
        if tr and tr.get("result"):
            r = tr["result"]
            pnl = tr.get("pnl", 0)
            result_tag = f"  → {r} ${pnl:+.0f}" if r != "NOT_FILLED" else "  → not filled"
        lines.append(
            f"#{idx} {arrow_map.get(sig['direction'], '?')} {sig['direction'].upper():<5}  "
            f"E={sig['entry']:.2f}  SL={sig['sl']:.2f}  TP={sig['tp']:.2f}  "
            f"RR={sig.get('rr', 0)}  n={sig.get('n_ct', 0)}  "
            f"comp={sig.get('composite', 0):.0f}  Q={sig.get('quality', 0):.0f}"
            f"{result_tag}"
        )
    if not lines:
        lines = ["aucun signal généré ce jour"]

    ax.text(
        0.005, 0.985, "\n".join(lines),
        transform=ax.transAxes,
        fontsize=7.2, ha="left", va="top", color=TV_FG,
        family="monospace",
        bbox=dict(fc=TV_BG, ec=TV_DIM, alpha=0.88,
                  pad=5, boxstyle="round,pad=0.4"),
        zorder=7,
    )

    # ── Bandeau contexte (bas gauche) ────────────────────────────────────
    align_str = f"{alignment_score:+.2f}" if alignment_score is not None else "n/a"
    regime_str = regime or "n/a"
    context_lines = [
        f"regime: {regime_str}   alignment: {align_str}",
        f"pm:  {_format_pm(pm_features)}",
        f"vol: {_format_vol(vol_features)}",
    ]
    ax.text(
        0.005, 0.015, "\n".join(context_lines),
        transform=ax.transAxes,
        fontsize=7.2, ha="left", va="bottom", color=TV_FG,
        family="monospace",
        bbox=dict(fc=TV_BG, ec=TV_DIM, alpha=0.88,
                  pad=5, boxstyle="round,pad=0.4"),
        zorder=7,
    )

    # ── Légende TF (haut droit) ──────────────────────────────────────────
    if used_tfs:
        legend_handles = [
            Patch(facecolor=TF_COLORS[tf], alpha=0.5, label=f"zones {tf}")
            for tf in ["D1", "H4", "H1", "15m"] if tf in used_tfs
        ]
        legend_handles += [
            Line2D([0], [0], color=TV_BLUE, lw=1.5, label="entry"),
            Line2D([0], [0], color=TV_RED, lw=1.0, ls="--", label="SL"),
            Line2D([0], [0], color=TV_GREEN, lw=1.0, ls="--", label="TP"),
        ]
        ax.legend(
            handles=legend_handles, loc="upper right",
            fontsize=7, framealpha=0.85, facecolor=TV_BG, edgecolor=TV_DIM,
            labelcolor=TV_FG,
        )

    # ── Axe X ────────────────────────────────────────────────────────────
    # Heures affichées en heure NY (DST-aware) — l'index source est en UTC
    # naïf, on convertit uniquement pour le rendu. 9h30 NY reste 9h30 toute
    # l'année, indépendant du saisonnier UTC.
    ny_tz = ZoneInfo(OPR_TIMEZONE)
    _draw_x_axis(ax_vol, data, n, tz=ny_tz)
    ax.tick_params(labelbottom=False)
    ax.set_xlim(-2, n + n * 0.18)
    ax_vol.set_xlim(-2, n + n * 0.18)
    ax.yaxis.tick_right()
    ax_vol.yaxis.tick_right()
    ax.grid(True, alpha=0.5, color="#1e222d", zorder=0)

    # ── Titre ────────────────────────────────────────────────────────────
    n_filled = sum(
        1 for t in trades
        if t.get("result") and t["result"] != "NOT_FILLED"
    )
    day_pnl = sum(
        float(t.get("pnl", 0) or 0) for t in trades
        if t.get("result") and t["result"] != "NOT_FILLED"
    )
    title = (
        f"{ticker}  •  {date_str}  •  15min  •  "
        f"{sig_count} signal(s)  /  {n_filled} fill(s)  •  P&L jour ${day_pnl:+,.0f}"
    )
    ax.set_title(title, fontsize=11, pad=10, loc="left",
                 color=TV_FG, fontweight="bold")

    fig.tight_layout()
    fig.savefig(output_path, dpi=160, bbox_inches="tight", facecolor=TV_BG)
    plt.close(fig)
    return True
