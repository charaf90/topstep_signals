"""
Génère N jours stratifiés par régime pour l'idéation visuelle (PHASE 0.5).

Charts "purs" (sans signaux) — utilisables par @chartist en mode idea pour
explorer le comportement d'un marché avant de formaliser un concept de
stratégie. Réutilise les helpers de core/analysis_chart.py et la résolution
des timeframes de core/data.py.

Classification de régime (sur D1) :
  - macro    : date ∈ MACRO_EVENT_DATES
  - trending : ADX(14) ≥ 25
  - ranging  : ADX(14) < 20
  - vol_h    : ATR(14) percentile > 75 (sur 60j roulants)
  - vol_b    : ATR(14) percentile < 25
  - mixed    : sinon

Usage :
    python -m core.explore_chart --ticker NQ1 --n 20
    python -m core.explore_chart --ticker NQ1 --n 10 --multi-tf
    python -m core.explore_chart --ticker NQ1 --n 6 --weekly     # vue hebdo enrichie
    python -m core.explore_chart --ticker MES1 --stratify random --seed 7

Mode weekly : 1 chart par semaine de 5 jours ouvrés avec
  - Bougies 15m
  - Ichimoku Cloud (Tenkan, Kijun, Senkou A/B, Chikou)
  - Sub-plot RSI(14) + StochRSI(14, 3, 3)
  - Swing H/L automatiques (fractals 5-bar)
  - Volume avec MA20
"""

import argparse
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from config import CHART_STYLE, MACRO_EVENT_DATES
from core.data import load_csv, build_timeframes
from core.analysis_chart import (
    _draw_candles, _draw_volume_bars,
    TV_GREEN, TV_RED, TV_BLUE, TV_ORANGE, TV_BG, TV_FG, TV_DIM,
)


np.random.seed(42)

NY_TZ = ZoneInfo("America/New_York")

# Fenêtres de visualisation par TF
CONTEXT = {
    # 15m : ~7h pré-marché + ~12.5h session US (= ~78 barres totales)
    "15m": {"before": 30, "after": 48},
    # H1  : 48h avant + 24h après (3 jours visibles)
    "H1":  {"before": 48, "after": 24},
    # D1  : 15 jours avant + 15 jours après (1 mois visible)
    "D1":  {"before": 15, "after": 15},
}


# ─────────────────────────────────────────────────────────────────────────
# Indicateurs (Wilder)
# ─────────────────────────────────────────────────────────────────────────

def _wilder_smooth(values: pd.Series, period: int) -> pd.Series:
    """Lissage de Wilder (EMA avec alpha = 1/period)."""
    return values.ewm(alpha=1.0 / period, adjust=False).mean()


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """ATR de Wilder."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return _wilder_smooth(tr, period)


def compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """ADX de Wilder (directional movement index lissé)."""
    high, low = df["high"], df["low"]
    up_move = high - high.shift(1)
    down_move = low.shift(1) - low

    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    atr = compute_atr(df, period).replace(0, np.nan)
    plus_di = 100.0 * _wilder_smooth(plus_dm, period) / atr
    minus_di = 100.0 * _wilder_smooth(minus_dm, period) / atr

    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return _wilder_smooth(dx, period)


# ─────────────────────────────────────────────────────────────────────────
# Classification & sélection
# ─────────────────────────────────────────────────────────────────────────

def classify_regime(df_d1: pd.DataFrame, atr_pct_window: int = 60) -> pd.Series:
    """Étiquette chaque jour D1 avec un régime parmi trending/ranging/macro/vol_h/vol_b/mixed."""
    adx = compute_adx(df_d1, 14)
    atr = compute_atr(df_d1, 14)
    atr_rank = atr.rolling(atr_pct_window, min_periods=20).rank(pct=True)

    macro_set = set(pd.to_datetime(MACRO_EVENT_DATES).normalize())

    out = pd.Series(index=df_d1.index, dtype=object)
    for idx in df_d1.index:
        day_norm = pd.Timestamp(idx).normalize()
        if day_norm in macro_set:
            out.loc[idx] = "macro"
        elif pd.notna(adx.loc[idx]) and adx.loc[idx] >= 25:
            out.loc[idx] = "trending"
        elif pd.notna(adx.loc[idx]) and adx.loc[idx] < 20:
            out.loc[idx] = "ranging"
        elif pd.notna(atr_rank.loc[idx]) and atr_rank.loc[idx] > 0.75:
            out.loc[idx] = "vol_h"
        elif pd.notna(atr_rank.loc[idx]) and atr_rank.loc[idx] < 0.25:
            out.loc[idx] = "vol_b"
        else:
            out.loc[idx] = "mixed"
    return out


def select_days_stratified(regime_series: pd.Series, n: int) -> list:
    """Tire N jours répartis sur les régimes (échantillonnage stratifié)."""
    targets = ["trending", "ranging", "macro", "vol_h", "vol_b", "mixed"]
    per_target = max(1, n // len(targets))
    selected = []
    for r in targets:
        candidates = regime_series.index[regime_series == r].tolist()
        if not candidates:
            continue
        k = min(per_target, len(candidates))
        chosen = np.random.choice(len(candidates), size=k, replace=False)
        selected.extend([candidates[i] for i in chosen])
    # Compléter / tronquer pour atteindre exactement n
    if len(selected) < n:
        remaining = [d for d in regime_series.index if d not in selected]
        k = min(n - len(selected), len(remaining))
        if k > 0:
            chosen = np.random.choice(len(remaining), size=k, replace=False)
            selected.extend([remaining[i] for i in chosen])
    elif len(selected) > n:
        chosen = np.random.choice(len(selected), size=n, replace=False)
        selected = [selected[i] for i in chosen]
    return sorted(pd.Timestamp(d) for d in selected)


# ─────────────────────────────────────────────────────────────────────────
# Plotting
# ─────────────────────────────────────────────────────────────────────────

def _format_x_axis(ax_vol, data: pd.DataFrame, tf: str, n: int) -> None:
    """Configure l'axe X selon le TF (heure NY pour 15m/H1, date pour D1)."""
    step = max(1, n // 8)
    ticks = list(range(0, n, step))
    if ticks[-1] != n - 1:
        ticks.append(n - 1)
    labels = []
    prev_d = ""
    for t in ticks:
        dt = data.index[t]
        if tf in ("15m", "H1"):
            ts = pd.Timestamp(dt).tz_localize("UTC").tz_convert(NY_TZ)
            d = ts.strftime("%d/%m")
            tm = ts.strftime("%H:%M")
            labels.append(f"{d}\n{tm}" if d != prev_d else tm)
            prev_d = d
        else:
            labels.append(pd.Timestamp(dt).strftime("%Y-%m-%d"))
    ax_vol.set_xticks(ticks)
    ax_vol.set_xticklabels(labels, fontsize=7)


def plot_pure_chart(df_tf: pd.DataFrame, ticker: str, tf: str,
                    center_day: pd.Timestamp, output_path: str,
                    regime_label: str = "") -> bool:
    """Chart pur (sans signaux) — bougies + EMA20/EMA50 + volume."""
    plt.rcParams.update(CHART_STYLE)

    if tf not in CONTEXT:
        raise ValueError(f"TF non supporté : {tf}")
    before = CONTEXT[tf]["before"]
    after = CONTEXT[tf]["after"]

    # Centrage : 9h30 NY ≈ 14h30 UTC (hors DST) — on tolère le décalage DST,
    # le but est juste d'avoir une fenêtre raisonnable autour du jour.
    if tf == "D1":
        center = pd.Timestamp(center_day).normalize()
    else:
        center = pd.Timestamp(center_day).normalize() + pd.Timedelta(hours=14, minutes=30)

    pre = df_tf[df_tf.index < center].iloc[-before:]
    post = df_tf[df_tf.index >= center].iloc[:after]
    data = pd.concat([pre, post])
    if len(data) < 10:
        return False

    ema20 = data["close"].ewm(span=20, adjust=False).mean()
    ema50 = data["close"].ewm(span=50, adjust=False).mean()

    fig, (ax, ax_vol) = plt.subplots(
        2, 1, figsize=(18, 10), sharex=True,
        gridspec_kw={"height_ratios": [4, 1], "hspace": 0.05},
    )

    n = len(data)
    x = np.arange(n)
    _draw_candles(ax, data, x)
    _draw_volume_bars(ax_vol, data, x)

    ax.plot(x, ema20.values, color=TV_BLUE, lw=1.0, label="EMA20", alpha=0.9)
    ax.plot(x, ema50.values, color=TV_ORANGE, lw=1.0, label="EMA50", alpha=0.9)
    ax.legend(loc="upper left", fontsize=8)

    # Marqueur visuel du jour étudié
    if tf in ("15m", "H1"):
        target = pd.Timestamp(center_day).normalize() + pd.Timedelta(hours=14, minutes=30)
    else:
        target = pd.Timestamp(center_day).normalize()
    if data.index[0] <= target <= data.index[-1]:
        idx = int(data.index.get_indexer([target], method="nearest")[0])
        ax.axvline(idx, color=TV_ORANGE, lw=0.8, ls="--", alpha=0.5)

    price_min = float(data["low"].min())
    price_max = float(data["high"].max())
    span = price_max - price_min
    margin = max(span * 0.06, 1.0)
    ax.set_ylim(price_min - margin, price_max + margin)

    date_str = pd.Timestamp(center_day).strftime("%Y-%m-%d")
    regime_tag = f"  [{regime_label}]" if regime_label else ""
    ax.set_title(
        f"{ticker} — {date_str}  [{tf}]{regime_tag}  |  EXPLORE (sans signaux)",
        color=TV_FG, fontsize=11,
    )

    _format_x_axis(ax_vol, data, tf, n)
    ax.grid(True, alpha=0.4, color="#1e222d")

    plt.savefig(output_path, dpi=100, bbox_inches="tight", facecolor=TV_BG)
    plt.close(fig)
    return True


# ─────────────────────────────────────────────────────────────────────────
# Indicateurs supplémentaires (vue hebdomadaire enrichie)
# ─────────────────────────────────────────────────────────────────────────

def compute_ichimoku(df: pd.DataFrame,
                     tenkan: int = 9, kijun: int = 26, senkou_b: int = 52,
                     shift: int = 26) -> dict:
    """Ichimoku Kinko Hyo. Retourne 5 séries (Tenkan, Kijun, SenkouA, SenkouB, Chikou)."""
    high, low, close = df["high"], df["low"], df["close"]

    tenkan_sen = (high.rolling(tenkan).max() + low.rolling(tenkan).min()) / 2.0
    kijun_sen = (high.rolling(kijun).max() + low.rolling(kijun).min()) / 2.0
    senkou_a = ((tenkan_sen + kijun_sen) / 2.0).shift(shift)
    senkou_b_line = ((high.rolling(senkou_b).max() + low.rolling(senkou_b).min()) / 2.0).shift(shift)
    chikou = close.shift(-shift)

    return {
        "tenkan": tenkan_sen,
        "kijun": kijun_sen,
        "senkou_a": senkou_a,
        "senkou_b": senkou_b_line,
        "chikou": chikou,
    }


def compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """RSI de Wilder."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = _wilder_smooth(gain, period)
    avg_loss = _wilder_smooth(loss, period)
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def compute_stochrsi(close: pd.Series,
                     rsi_period: int = 14, stoch_period: int = 14,
                     k: int = 3, d: int = 3) -> tuple:
    """StochRSI %K et %D (Stoch sur RSI, lissé)."""
    rsi = compute_rsi(close, rsi_period)
    rsi_min = rsi.rolling(stoch_period).min()
    rsi_max = rsi.rolling(stoch_period).max()
    stoch = 100.0 * (rsi - rsi_min) / (rsi_max - rsi_min).replace(0, np.nan)
    pct_k = stoch.rolling(k).mean()
    pct_d = pct_k.rolling(d).mean()
    return pct_k, pct_d


def detect_fractals(df: pd.DataFrame, n: int = 2) -> tuple:
    """Fractals de Williams (n=2 → fenêtre 5 barres : i-2, i-1, i, i+1, i+2).

    Retourne (is_swing_high, is_swing_low) — séries booléennes.
    """
    high, low = df["high"], df["low"]
    is_high = pd.Series(False, index=df.index)
    is_low = pd.Series(False, index=df.index)
    for i in range(n, len(df) - n):
        window_h = high.iloc[i - n:i + n + 1]
        window_l = low.iloc[i - n:i + n + 1]
        if high.iloc[i] == window_h.max() and (window_h.values == high.iloc[i]).sum() == 1:
            is_high.iloc[i] = True
        if low.iloc[i] == window_l.min() and (window_l.values == low.iloc[i]).sum() == 1:
            is_low.iloc[i] = True
    return is_high, is_low


# ─────────────────────────────────────────────────────────────────────────
# Chart hebdomadaire enrichi
# ─────────────────────────────────────────────────────────────────────────

def _get_week_window(center_day: pd.Timestamp) -> tuple:
    """Retourne (week_start_utc, week_end_utc) — lundi 00:00 NY → vendredi 23:59 NY.

    Si center_day tombe un week-end, on cale sur la semaine la plus proche (lundi suivant).
    """
    cd = pd.Timestamp(center_day).normalize()
    weekday = cd.weekday()  # 0 = lundi
    if weekday >= 5:  # samedi/dimanche
        cd = cd + pd.Timedelta(days=(7 - weekday))
        weekday = 0
    monday = cd - pd.Timedelta(days=weekday)
    friday = monday + pd.Timedelta(days=4)
    # Heure NY : on prend de lundi 00:00 NY à vendredi 23:59 NY
    # Conversion en UTC naïf (les CSV sont UTC naïfs)
    week_start = monday + pd.Timedelta(hours=5)   # ≈ 00:00 NY = 05:00 UTC (hors DST)
    week_end = friday + pd.Timedelta(hours=22)    # ≈ 17:00 NY = 22:00 UTC
    return week_start, week_end


def plot_weekly_chart(df_15m: pd.DataFrame, ticker: str,
                      center_day: pd.Timestamp, output_path: str,
                      regime_label: str = "") -> bool:
    """Chart hebdomadaire 15m enrichi.

    Layout :
      - Panneau prix (large) : bougies + Ichimoku cloud + Tenkan/Kijun + swing H/L
      - Panneau volume       : barres + MA20
      - Panneau oscillateurs : RSI(14) + StochRSI %K/%D
    """
    plt.rcParams.update(CHART_STYLE)

    week_start, week_end = _get_week_window(center_day)

    # Warmup ~60 barres avant pour init Ichimoku/RSI (Senkou B = 52 barres)
    warmup_start = week_start - pd.Timedelta(days=4)
    df_full = df_15m[(df_15m.index >= warmup_start) & (df_15m.index <= week_end + pd.Timedelta(hours=8))].copy()
    if len(df_full) < 80:
        return False

    # Calcul indicateurs sur df_full (avec warmup)
    ichi = compute_ichimoku(df_full)
    rsi = compute_rsi(df_full["close"], 14)
    stoch_k, stoch_d = compute_stochrsi(df_full["close"], 14, 14, 3, 3)
    is_high, is_low = detect_fractals(df_full, n=2)

    # Volume MA20
    vol_ma = df_full["volume"].rolling(20).mean()

    # Filtre sur la fenêtre visible (semaine)
    visible_mask = (df_full.index >= week_start) & (df_full.index <= week_end)
    data = df_full[visible_mask]
    if len(data) < 30:
        return False

    n = len(data)
    x = np.arange(n)
    full_to_visible_idx = data.index

    def _vis(series: pd.Series) -> np.ndarray:
        return series.reindex(full_to_visible_idx).values

    # Layout 3 panneaux
    fig, axes = plt.subplots(
        3, 1, figsize=(22, 12), sharex=True,
        gridspec_kw={"height_ratios": [6, 1.2, 2.5], "hspace": 0.05},
    )
    ax, ax_vol, ax_osc = axes

    # ── Panneau prix ─────────────────────────────────────────────────────
    _draw_candles(ax, data, x)

    # Ichimoku cloud (Senkou A & B)
    sa = _vis(ichi["senkou_a"])
    sb = _vis(ichi["senkou_b"])
    bullish_cloud = sa > sb
    # Vert quand SA > SB, rouge sinon
    ax.fill_between(x, sa, sb, where=bullish_cloud, color="#26a69a", alpha=0.15, zorder=1)
    ax.fill_between(x, sa, sb, where=~bullish_cloud, color="#ef5350", alpha=0.15, zorder=1)
    ax.plot(x, sa, color="#26a69a", lw=0.6, alpha=0.7, zorder=1)
    ax.plot(x, sb, color="#ef5350", lw=0.6, alpha=0.7, zorder=1)

    # Tenkan (rouge clair) et Kijun (bleu)
    ax.plot(x, _vis(ichi["tenkan"]), color="#ff7043", lw=1.0, label="Tenkan(9)", alpha=0.9)
    ax.plot(x, _vis(ichi["kijun"]), color="#2962ff", lw=1.0, label="Kijun(26)", alpha=0.9)

    # Swing H/L (fractals)
    high_idx = np.where(_vis(is_high))[0]
    low_idx = np.where(_vis(is_low))[0]
    if len(high_idx) > 0:
        ax.scatter(high_idx, data["high"].iloc[high_idx].values + (data["high"].max() - data["low"].min()) * 0.005,
                   marker="v", color="#f7b801", s=30, zorder=5, label="Swing H")
    if len(low_idx) > 0:
        ax.scatter(low_idx, data["low"].iloc[low_idx].values - (data["high"].max() - data["low"].min()) * 0.005,
                   marker="^", color="#a566ff", s=30, zorder=5, label="Swing L")

    ax.legend(loc="upper left", fontsize=8, ncol=2)

    # Marqueurs verticaux : début de chaque jour (lundi 09:30 NY, mardi 09:30 NY, etc.)
    day_starts = []
    for day_offset in range(5):
        d = pd.Timestamp(week_start.normalize()) + pd.Timedelta(days=day_offset, hours=14, minutes=30)
        if data.index[0] <= d <= data.index[-1]:
            idx = int(data.index.get_indexer([d], method="nearest")[0])
            day_starts.append((idx, d))
    for idx, _ in day_starts:
        ax.axvline(idx, color=TV_DIM, lw=0.5, ls=":", alpha=0.5)

    # Échelle Y
    price_min = float(data["low"].min())
    price_max = float(data["high"].max())
    span = price_max - price_min
    margin = max(span * 0.06, 1.0)
    ax.set_ylim(price_min - margin, price_max + margin)

    # Titre
    week_label = f"{week_start.strftime('%Y-%m-%d')} → {week_end.strftime('%Y-%m-%d')}"
    regime_tag = f"  [{regime_label}]" if regime_label else ""
    ax.set_title(
        f"{ticker} — Semaine {week_label}  [15m]{regime_tag}  |  EXPLORE (Ichimoku + RSI + StochRSI)",
        color=TV_FG, fontsize=12,
    )
    ax.grid(True, alpha=0.4, color="#1e222d")

    # ── Panneau volume ───────────────────────────────────────────────────
    _draw_volume_bars(ax_vol, data, x)
    vm = _vis(vol_ma)
    ax_vol.plot(x, vm, color="#f7b801", lw=0.8, alpha=0.9)
    ax_vol.grid(True, alpha=0.4, color="#1e222d")

    # ── Panneau oscillateurs ─────────────────────────────────────────────
    ax_osc.plot(x, _vis(rsi), color="#42a5f5", lw=1.0, label="RSI(14)", alpha=0.9)
    ax_osc.plot(x, _vis(stoch_k), color="#f7b801", lw=0.9, label="StochRSI %K", alpha=0.9)
    ax_osc.plot(x, _vis(stoch_d), color="#a566ff", lw=0.9, label="StochRSI %D", alpha=0.9)
    ax_osc.axhline(80, color=TV_DIM, lw=0.5, ls="--", alpha=0.5)
    ax_osc.axhline(50, color=TV_DIM, lw=0.4, ls=":", alpha=0.4)
    ax_osc.axhline(20, color=TV_DIM, lw=0.5, ls="--", alpha=0.5)
    ax_osc.set_ylim(0, 100)
    ax_osc.set_ylabel("RSI / StochRSI", color=TV_DIM, fontsize=8)
    ax_osc.legend(loc="upper left", fontsize=7, ncol=3)
    ax_osc.grid(True, alpha=0.4, color="#1e222d")

    # ── Axe X (date + heure NY) ──────────────────────────────────────────
    step = max(1, n // 12)
    ticks = list(range(0, n, step))
    if ticks[-1] != n - 1:
        ticks.append(n - 1)
    labels = []
    prev_d = ""
    for t in ticks:
        dt = data.index[t]
        ts = pd.Timestamp(dt).tz_localize("UTC").tz_convert(NY_TZ)
        d = ts.strftime("%a %d/%m")  # Lun 16/12
        tm = ts.strftime("%H:%M")
        labels.append(f"{d}\n{tm}" if d != prev_d else tm)
        prev_d = d
    ax_osc.set_xticks(ticks)
    ax_osc.set_xticklabels(labels, fontsize=7)

    plt.savefig(output_path, dpi=100, bbox_inches="tight", facecolor=TV_BG)
    plt.close(fig)
    return True


# ─────────────────────────────────────────────────────────────────────────
# Chart panoramique D1 (vue d'ensemble période complète + marqueurs semaines)
# ─────────────────────────────────────────────────────────────────────────

REGIME_COLORS = {
    "trending": "#26a69a",
    "ranging":  "#787b86",
    "macro":    "#ef5350",
    "vol_h":    "#ff9800",
    "vol_b":    "#42a5f5",
    "mixed":    "#a566ff",
}


def plot_panoramic_d1(df_d1: pd.DataFrame, ticker: str,
                      selected_days: list, regime_series: pd.Series,
                      output_path: str) -> bool:
    """Vue d'ensemble D1 sur toute la période avec marqueurs des semaines analysées.

    Sert de 'carte' pour le chartist : où se situent les semaines analysées
    dans le contexte macro global ?

    Layout :
      - Panneau prix (large) : bougies D1 + Ichimoku D1 + marqueurs semaines
      - Panneau RSI D1       : RSI(14) sur les daily
    """
    plt.rcParams.update(CHART_STYLE)

    data = df_d1.copy()
    if len(data) < 60:
        return False

    # Indicateurs D1
    ichi = compute_ichimoku(data)
    rsi = compute_rsi(data["close"], 14)

    n = len(data)
    x = np.arange(n)

    fig, axes = plt.subplots(
        2, 1, figsize=(22, 10), sharex=True,
        gridspec_kw={"height_ratios": [4, 1.2], "hspace": 0.05},
    )
    ax, ax_rsi = axes

    # ── Panneau prix ─────────────────────────────────────────────────────
    _draw_candles(ax, data, x)

    sa = ichi["senkou_a"].values
    sb = ichi["senkou_b"].values
    bullish_cloud = sa > sb
    ax.fill_between(x, sa, sb, where=bullish_cloud, color="#26a69a", alpha=0.15, zorder=1)
    ax.fill_between(x, sa, sb, where=~bullish_cloud, color="#ef5350", alpha=0.15, zorder=1)
    ax.plot(x, sa, color="#26a69a", lw=0.6, alpha=0.7, zorder=1)
    ax.plot(x, sb, color="#ef5350", lw=0.6, alpha=0.7, zorder=1)

    ax.plot(x, ichi["tenkan"].values, color="#ff7043", lw=1.0, label="Tenkan(9)", alpha=0.9)
    ax.plot(x, ichi["kijun"].values, color="#2962ff", lw=1.0, label="Kijun(26)", alpha=0.9)

    # Marqueurs des semaines sélectionnées
    used_regimes = set()
    for day in selected_days:
        week_start, _ = _get_week_window(day)
        regime_label = str(regime_series.get(day, "mixed"))
        color = REGIME_COLORS.get(regime_label, "#ffffff")
        # Trouver l'index D1 le plus proche de week_start
        target = pd.Timestamp(week_start).normalize()
        if data.index[0] <= target <= data.index[-1]:
            idx = int(data.index.get_indexer([target], method="nearest")[0])
            label = regime_label if regime_label not in used_regimes else None
            ax.axvspan(idx - 0.5, idx + 4.5, color=color, alpha=0.20, zorder=0,
                       label=label)
            used_regimes.add(regime_label)

    ax.legend(loc="upper left", fontsize=8, ncol=3)

    # Échelle Y
    price_min = float(data["low"].min())
    price_max = float(data["high"].max())
    span = price_max - price_min
    margin = max(span * 0.04, 1.0)
    ax.set_ylim(price_min - margin, price_max + margin)

    # Titre
    period_label = f"{data.index[0].strftime('%Y-%m-%d')} → {data.index[-1].strftime('%Y-%m-%d')}"
    ax.set_title(
        f"{ticker} — Vue PANORAMIQUE D1  {period_label}  |  "
        f"{len(selected_days)} semaines analysées (couleur = régime)",
        color=TV_FG, fontsize=12,
    )
    ax.grid(True, alpha=0.4, color="#1e222d")

    # ── Panneau RSI ──────────────────────────────────────────────────────
    ax_rsi.plot(x, rsi.values, color="#42a5f5", lw=1.0, label="RSI(14) D1", alpha=0.9)
    ax_rsi.axhline(70, color=TV_DIM, lw=0.5, ls="--", alpha=0.5)
    ax_rsi.axhline(50, color=TV_DIM, lw=0.4, ls=":", alpha=0.4)
    ax_rsi.axhline(30, color=TV_DIM, lw=0.5, ls="--", alpha=0.5)
    ax_rsi.set_ylim(0, 100)
    ax_rsi.set_ylabel("RSI D1", color=TV_DIM, fontsize=8)
    ax_rsi.legend(loc="upper left", fontsize=7)
    ax_rsi.grid(True, alpha=0.4, color="#1e222d")

    # Axe X — dates espacées
    step = max(1, n // 12)
    ticks = list(range(0, n, step))
    if ticks[-1] != n - 1:
        ticks.append(n - 1)
    labels = [pd.Timestamp(data.index[t]).strftime("%Y-%m-%d") for t in ticks]
    ax_rsi.set_xticks(ticks)
    ax_rsi.set_xticklabels(labels, fontsize=7, rotation=20)

    plt.savefig(output_path, dpi=100, bbox_inches="tight", facecolor=TV_BG)
    plt.close(fig)
    return True


# ─────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Génère N jours stratifiés par régime pour l'idéation chartist.",
    )
    parser.add_argument("--ticker", required=True,
                        help="MES1 | NQ1 | YM1 | MGC1 | MCLE1 | ...")
    parser.add_argument("--n", type=int, default=20,
                        help="Nombre de jours à générer (défaut 20)")
    parser.add_argument("--stratify", default="regime",
                        choices=["regime", "random"],
                        help="Échantillonnage stratifié par régime ou aléatoire")
    parser.add_argument("--multi-tf", action="store_true",
                        help="Génère le trio 15m + H1 + D1 par jour")
    parser.add_argument("--weekly", action="store_true",
                        help="Mode vue hebdomadaire enrichie (Ichimoku + RSI + "
                             "StochRSI + swing H/L) — 1 chart par semaine")
    parser.add_argument("--seed", type=int, default=42,
                        help="Seed pour reproductibilité (défaut 42)")
    parser.add_argument("--csv-dir", default="./data")
    parser.add_argument("--output-dir", default="./output/explore")
    args = parser.parse_args()

    np.random.seed(args.seed)

    csv_path = Path(args.csv_dir) / f"{args.ticker}_data_m15.csv"
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV introuvable : {csv_path}")

    print(f"[explore_chart] Chargement {csv_path}")
    df_15m = load_csv(str(csv_path))
    tfs = build_timeframes(df_15m)

    print("[explore_chart] Classification régime D1 (ADX + ATR)")
    regime_series = classify_regime(tfs["D1"])
    counts = regime_series.value_counts().to_dict()
    print(f"[explore_chart] Distribution : {counts}")

    if args.stratify == "regime":
        days = select_days_stratified(regime_series, args.n)
    else:
        all_days = regime_series.index.tolist()
        k = min(args.n, len(all_days))
        idx = np.random.choice(len(all_days), size=k, replace=False)
        days = sorted(pd.Timestamp(all_days[i]) for i in idx)

    print(f"[explore_chart] {len(days)} jours sélectionnés")

    out_dir = Path(args.output_dir) / args.ticker
    out_dir.mkdir(parents=True, exist_ok=True)

    n_ok = 0

    if args.weekly:
        # Mode hebdomadaire : 1 chart par semaine (5 jours ouvrés)
        # Déduplique les semaines (plusieurs jours stratifiés peuvent tomber dans la même semaine)
        seen_weeks = set()
        kept_days = []
        for day in days:
            week_start, week_end = _get_week_window(day)
            week_key = week_start.strftime("%Y-%m-%d")
            if week_key in seen_weeks:
                continue
            seen_weeks.add(week_key)
            kept_days.append(day)
            regime_label = str(regime_series.get(day, ""))
            output_path = out_dir / f"week_{week_key}.png"
            ok = plot_weekly_chart(
                df_15m, args.ticker, day, str(output_path),
                regime_label=regime_label,
            )
            if ok:
                n_ok += 1
                print(f"  ✓ {output_path.name}  [{regime_label}]")
            else:
                print(f"  ✗ {output_path.name}  (données insuffisantes)")

        # Vue panoramique D1 (carte globale + marqueurs des semaines)
        if kept_days:
            pano_path = out_dir / "panoramic_D1.png"
            ok = plot_panoramic_d1(
                tfs["D1"], args.ticker, kept_days, regime_series, str(pano_path),
            )
            if ok:
                n_ok += 1
                print(f"  ✓ {pano_path.name}  (vue d'ensemble)")
    else:
        # Mode mono-TF ou multi-TF (existant)
        tf_list = ["15m", "H1", "D1"] if args.multi_tf else ["15m"]
        for day in days:
            regime_label = str(regime_series.get(day, ""))
            date_str = pd.Timestamp(day).strftime("%Y-%m-%d")
            for tf in tf_list:
                output_path = out_dir / f"{date_str}_{tf}.png"
                ok = plot_pure_chart(
                    tfs[tf], args.ticker, tf, day, str(output_path),
                    regime_label=regime_label,
                )
                if ok:
                    n_ok += 1
                    print(f"  ✓ {output_path.name}  [{regime_label}]")
                else:
                    print(f"  ✗ {output_path.name}  (données insuffisantes)")

    print(f"\n[explore_chart] Terminé : {n_ok} charts générés dans {out_dir}")


if __name__ == "__main__":
    main()
