"""
Stratégie VPC — Volume Profile Confluence (vpc-v1).

Concept :
    Approximation du Volume Profile journalier (POC, VAH, VAL, HVN, LVN) à
    partir des barres M15 de la session cash NY veille. Trois setups
    hiérarchiques sur la session NY courante :

      1) OPEN_OUTSIDE   — open du jour hors Value Area veille → trade
                          directionnel (gap continuation).
      2) BREAKOUT_RETEST — cassure VAH/VAL sur volume + retest pour entrée.
      3) HVN_REBOUND    — bougie de rejet sur un HVN → reversion vers POC.

Edge :
    Niveaux POC/VAH/VAL/HVN agissent comme aimants/repulseurs car beaucoup
    de stops mécaniques (retail, momentum trend-followers) sont placés juste
    autour. Les algos institutionnels en absorbent une partie. Sur M15 (vs
    profil intra-tick), l'approximation est grossière → handicap structurel.

Falsification :
    - Bootstrap portfolio OOS < 50%, OU
    - PF OOS < 1.0 sur 60 trades consécutifs en live.

Interface :
    run_backtest(df_15m, ticker, tf=None, params=None, topstep_guard=True)
    plot_day(df_15m, ticker, date_str, day_trades, output_path)

Architecture : RECHERCHE pure. N'importe rien de broker/ ni core/opr.py.
"""

from __future__ import annotations

from typing import Optional, Dict
from zoneinfo import ZoneInfo

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from core.vpc import (
    _compute_atr,
    _compute_adx,
    _compute_emas,
    _build_previous_day_profile,
    _detect_setup_open_outside,
    _detect_setup_breakout_retest,
    _detect_setup_hvn_rebound,
)

from config import (
    INSTRUMENTS,
    RISK_PER_TRADE_USD,
    SLIPPAGE_TICKS_PER_TICKER,
    COMMISSION_RT_PER_CONTRACT,
    MACRO_EVENT_DATES,
    # VPC params
    VPC_STRATEGY_VERSION,
    VPC_TICKERS,
    VPC_HOUR_START_NY, VPC_HOUR_END_NY,
    VPC_OPEN_HOUR_NY,  VPC_OPEN_MIN_NY,
    VPC_PROFILE_BUCKET_TICKS, VPC_VALUE_AREA_PCT,
    VPC_HVN_VOL_MULT, VPC_LVN_VOL_MULT,
    VPC_VOL_AVG_WINDOW, VPC_VOL_MULT_THRESHOLD,
    VPC_EMA_FAST_PERIOD, VPC_EMA_SLOW_PERIOD,
    VPC_ATR_PERIOD,
    VPC_SL_ATR_MULT_PER_TICKER, VPC_TP_ATR_MULT_PER_TICKER,
    VPC_SL_BUFFER_TICKS,
    VPC_ORDER_TIMEOUT_BARS, VPC_MAX_HOLD_BARS,
    VPC_MAX_TRADES_PER_DAY,
    VPC_ENABLE_OPEN_OUTSIDE, VPC_ENABLE_BREAKOUT_RETEST, VPC_ENABLE_HVN_REBOUND,
    VPC_HVN_ADX_MIN, VPC_HVN_HOUR_START_NY, VPC_HVN_HOUR_END_NY, VPC_HVN_MIN_RR,
    VPC_OPEN_OUTSIDE_FADE, VPC_OPEN_OUTSIDE_MIN_GAP_ATR,
    VPC_EXCLUDE_MACRO_DAYS, VPC_OPEN_OUTSIDE_ADX_MAX,
)

np.random.seed(42)

# ── Identité de la stratégie ─────────────────────────────────────────────────
STRATEGY_ID   = VPC_STRATEGY_VERSION         # "vpc-v1"
TICKERS       = VPC_TICKERS                  # ["MES1", "NQ1"] — YM1 désactivé
CSV_SUFFIX    = "_vpc"
CSV_TIMEFRAME = "m15"

_NY = ZoneInfo("America/New_York")

# ── Grille d'optimisation (4 dimensions × 4×4×3×3 = 144 combos) ──────────────
PARAM_GRID = {
    "sl_atr_mult":         [1.0, 1.5, 2.0, 2.5],
    "tp_atr_mult":         [1.5, 2.0, 3.0, 4.0],
    "vol_mult_threshold":  [1.0, 1.5, 2.0],
    "value_area_pct":      [0.60, 0.70, 0.80],
}


# ══════════════════════════════════════════════════════════════════════════════
# Backtest principal
# ══════════════════════════════════════════════════════════════════════════════

def run_backtest(
    df_15m: pd.DataFrame,
    ticker: str,
    tf=None,
    params: Optional[dict] = None,
    topstep_guard: bool = True,
) -> pd.DataFrame:
    """
    Exécute le backtest VPC sur df_15m pour un ticker.

    Schéma de colonnes (compatibilité core/optimizer.py) :
      date, dir, entry, sl, tp, sl_dist, tp_dist, rr, n_ct,
      result (TP|SL|TE|NOT_FILLED), pnl, fill_time, exit_time, exit, regime
    + colonnes optionnelles : pnl_gross, adx, atr_pct, is_macro_day, setup, ticker

    `pnl` = P&L NET (slippage entrée+sortie + commissions).
    """
    if ticker not in TICKERS:
        return pd.DataFrame()

    p = params or {}
    sl_atr_mult = p.get("sl_atr_mult", VPC_SL_ATR_MULT_PER_TICKER.get(ticker, 1.5))
    tp_atr_mult = p.get("tp_atr_mult", VPC_TP_ATR_MULT_PER_TICKER.get(ticker, 2.0))
    vol_thr     = p.get("vol_mult_threshold", VPC_VOL_MULT_THRESHOLD)
    va_pct      = p.get("value_area_pct", VPC_VALUE_AREA_PCT)

    instr      = INSTRUMENTS[ticker]
    tick_size  = instr["tick_size"]
    pt_value   = instr["dollar_per_point"]
    slip_ticks = SLIPPAGE_TICKS_PER_TICKER.get(ticker, 1)
    slip_px    = slip_ticks * tick_size

    # Indicateurs (sans look-ahead)
    df = df_15m.copy()
    df["atr"]      = _compute_atr(df, VPC_ATR_PERIOD)
    df["adx"]      = _compute_adx(df, 14)
    df["atr_pct"]  = df["atr"].rolling(window=20 * 26, min_periods=50).rank(pct=True)
    df["ema_fast"], df["ema_slow"] = _compute_emas(
        df, VPC_EMA_FAST_PERIOD, VPC_EMA_SLOW_PERIOD,
    )
    df["vol_avg"]  = df["volume"].rolling(VPC_VOL_AVG_WINDOW, min_periods=10).mean()

    # Convert index → NY
    if df.index.tz is None:
        idx_ny_all = df.index.tz_localize("UTC").tz_convert(_NY)
    else:
        idx_ny_all = df.index.tz_convert(_NY)

    # Cache profil par date (évite recalcul à chaque barre)
    profile_cache: Dict[str, Optional[Dict]] = {}

    trades: List[Dict] = []
    daily_trades: Dict[str, int] = {}

    # Warmup minimum (EMA slow + buffer + 1 jour pour profil veille)
    warmup = max(VPC_EMA_SLOW_PERIOD * 2, 100)

    n = len(df)
    for i in range(warmup, n):
        bar  = df.iloc[i]
        prev = df.iloc[i - 1]
        ts_ny = idx_ny_all[i]

        # Filtre weekend
        if ts_ny.weekday() >= 5:
            continue

        # Filtre fenêtre NY cash
        hr, mn = ts_ny.hour, ts_ny.minute
        if hr < VPC_HOUR_START_NY or hr >= VPC_HOUR_END_NY:
            continue
        if hr == VPC_HOUR_START_NY and mn < VPC_OPEN_MIN_NY:
            continue  # avant 9h30 NY

        date_str = ts_ny.strftime("%Y-%m-%d")
        # vpc-v4 : exclusion jours macro (stress test v3 → PF 0.41 sur macro)
        if VPC_EXCLUDE_MACRO_DAYS and date_str in MACRO_EVENT_DATES:
            continue
        if daily_trades.get(date_str, 0) >= VPC_MAX_TRADES_PER_DAY:
            continue

        # Construction profil veille (cache)
        if date_str not in profile_cache:
            profile_cache[date_str] = _build_previous_day_profile(
                df, ts_ny.normalize(), tick_size,
                VPC_PROFILE_BUCKET_TICKS, va_pct,
                VPC_HVN_VOL_MULT, VPC_LVN_VOL_MULT,
            )
        profile = profile_cache[date_str]
        if profile is None:
            continue

        # ATR & EMA values pour cette barre
        atr_val = float(bar["atr"]) if pd.notna(bar["atr"]) else 0.0
        if atr_val <= 0:
            continue
        ema_fast_v = float(bar["ema_fast"]) if pd.notna(bar["ema_fast"]) else None
        ema_slow_v = float(bar["ema_slow"]) if pd.notna(bar["ema_slow"]) else None
        if ema_fast_v is None or ema_slow_v is None:
            continue
        avg_vol = float(prev["vol_avg"]) if pd.notna(prev["vol_avg"]) else 0.0

        # ── Setup 1 : OPEN_OUTSIDE (uniquement à 9h30 NY exact) ─────────
        signal = None
        is_open_bar = (hr == VPC_OPEN_HOUR_NY and mn == VPC_OPEN_MIN_NY)
        if VPC_ENABLE_OPEN_OUTSIDE and is_open_bar:
            # vpc-v4 : filtre ADX max (fade évite trending fort)
            adx_v = float(bar["adx"]) if pd.notna(bar["adx"]) else 0.0
            if adx_v < VPC_OPEN_OUTSIDE_ADX_MAX:
                signal = _detect_setup_open_outside(
                    bar, bar, profile, tick_size,
                    VPC_SL_BUFFER_TICKS, sl_atr_mult, tp_atr_mult, atr_val,
                    fade=VPC_OPEN_OUTSIDE_FADE,
                    min_gap_atr=VPC_OPEN_OUTSIDE_MIN_GAP_ATR,
                )

        # ── Setup 2 : BREAKOUT_RETEST ──────────────────────────────────
        if signal is None and VPC_ENABLE_BREAKOUT_RETEST:
            signal = _detect_setup_breakout_retest(
                bar, prev, profile, ema_fast_v, ema_slow_v,
                avg_vol, vol_thr, tick_size,
                VPC_SL_BUFFER_TICKS, sl_atr_mult, tp_atr_mult, atr_val,
            )

        # ── Setup 3 : HVN_REBOUND (vpc-v2 : fenêtre 10h-15h NY + filtres) ──
        if signal is None and VPC_ENABLE_HVN_REBOUND:
            # Restriction horaire spécifique HVN (cohérence vpc-v2)
            if VPC_HVN_HOUR_START_NY <= hr < VPC_HVN_HOUR_END_NY:
                adx_v = float(bar["adx"]) if pd.notna(bar["adx"]) else 0.0
                signal = _detect_setup_hvn_rebound(
                    bar, profile, ema_fast_v, ema_slow_v,
                    avg_vol, vol_thr, tick_size,
                    VPC_SL_BUFFER_TICKS, sl_atr_mult, tp_atr_mult, atr_val,
                    adx_v, VPC_HVN_MIN_RR,
                )

        if signal is None:
            continue

        direction = signal["direction"]
        entry     = signal["entry"]
        sl        = signal["sl"]
        tp        = signal["tp"]
        sl_dist   = abs(entry - sl)
        tp_dist   = abs(tp - entry)
        if sl_dist <= 0 or tp_dist <= 0:
            continue
        rr = tp_dist / sl_dist

        # Sizing avec slippage anticipé
        risk_per_contract = (sl_dist + slip_px) * pt_value
        if risk_per_contract <= 0:
            continue
        n_ct = max(1, int(RISK_PER_TRADE_USD / risk_per_contract))

        # ── Simulation fill + exit (CONSERVATIVE) ──────────────────────
        result    = "NOT_FILLED"
        fill_time = None
        exit_time = None
        exit_px   = None
        pnl_gross = 0.0
        pnl       = 0.0

        # Setup OPEN_OUTSIDE = entrée market sur l'open → fill immédiat à i
        # (l'entrée = bar["open"] de la bougie 9h30, qui est le prix par lequel
        #  cette barre commence → réaliste)
        if signal["setup"] == "OPEN_OUTSIDE":
            fill_start = i
            fill_time  = df.index[i]
        else:
            # Setups limit : on cherche un fill dans les VPC_ORDER_TIMEOUT_BARS suivantes
            fill_start = None
            for j in range(i + 1, min(i + VPC_ORDER_TIMEOUT_BARS + 1, n)):
                fut = df.iloc[j]
                if direction == "long"  and float(fut["low"])  <= entry:
                    fill_start = j
                    fill_time  = df.index[j]
                    break
                if direction == "short" and float(fut["high"]) >= entry:
                    fill_start = j
                    fill_time  = df.index[j]
                    break

        if fill_start is not None:
            # ── Cherche SL/TP/Timeout dans les bougies suivantes ──
            # Sur la bougie de fill : ambiguïté SL+TP → SL prioritaire
            sl_hit_at_fill = False
            tp_hit_at_fill = False
            if signal["setup"] != "OPEN_OUTSIDE":
                fb = df.iloc[fill_start]
                if direction == "long":
                    sl_hit_at_fill = float(fb["low"])  <= sl
                    tp_hit_at_fill = float(fb["high"]) >= tp
                else:
                    sl_hit_at_fill = float(fb["high"]) >= sl
                    tp_hit_at_fill = float(fb["low"])  <= tp

            if sl_hit_at_fill:
                result, exit_px, exit_time = "SL", sl, df.index[fill_start]
            elif tp_hit_at_fill:
                result, exit_px, exit_time = "TP", tp, df.index[fill_start]
            else:
                # Cherche dans les barres suivantes (max hold + fin session)
                max_end = min(fill_start + VPC_MAX_HOLD_BARS, n)
                exit_found = False
                for k in range(fill_start + 1, max_end):
                    fk = df.iloc[k]
                    # Forçage fin de session NY 16h00
                    k_ny = idx_ny_all[k]
                    if k_ny.hour >= VPC_HOUR_END_NY:
                        result, exit_px, exit_time = "TE", float(fk["open"]), df.index[k]
                        exit_found = True
                        break
                    if direction == "long":
                        sl_k = float(fk["low"])  <= sl
                        tp_k = float(fk["high"]) >= tp
                    else:
                        sl_k = float(fk["high"]) >= sl
                        tp_k = float(fk["low"])  <= tp

                    if sl_k and tp_k:
                        result, exit_px, exit_time = "SL", sl, df.index[k]
                        exit_found = True
                        break
                    if sl_k:
                        result, exit_px, exit_time = "SL", sl, df.index[k]
                        exit_found = True
                        break
                    if tp_k:
                        result, exit_px, exit_time = "TP", tp, df.index[k]
                        exit_found = True
                        break
                if not exit_found:
                    # Time-out plein : exit à la dernière barre disponible
                    last_k = min(fill_start + VPC_MAX_HOLD_BARS, n - 1)
                    result, exit_px, exit_time = "TE", float(df.iloc[last_k]["close"]), df.index[last_k]

            # ── Calcul P&L brut + net ──
            raw_move = (exit_px - entry) * (1 if direction == "long" else -1)
            pnl_gross = raw_move * n_ct * pt_value
            slip_cost = 2 * slip_px * n_ct * pt_value           # entrée + sortie
            comm_cost = COMMISSION_RT_PER_CONTRACT * n_ct       # round-trip
            pnl       = pnl_gross - slip_cost - comm_cost
            daily_trades[date_str] = daily_trades.get(date_str, 0) + 1

        # Tagging régime
        if pd.notna(bar["adx"]) and float(bar["adx"]) > 25:
            regime = "trending"
        elif pd.notna(bar["adx"]) and float(bar["adx"]) < 20:
            regime = "ranging"
        else:
            regime = "neutral"

        is_macro = date_str in MACRO_EVENT_DATES

        trades.append({
            # Schéma standard
            "date":         date_str,
            "ticker":       ticker,
            "strategy":     "VPC",
            "setup":        signal["setup"],
            "dir":          direction,
            "entry":        round(float(entry), 4),
            "sl":           round(float(sl), 4),
            "tp":           round(float(tp), 4),
            "sl_dist":      round(float(sl_dist), 4),
            "tp_dist":      round(float(tp_dist), 4),
            "rr":           round(float(rr), 2),
            "n_ct":         int(n_ct),
            "result":       result,
            "pnl":          round(float(pnl), 2),
            "fill_time":    fill_time,
            "exit_time":    exit_time,
            "exit":         round(float(exit_px), 4) if exit_px is not None else None,
            "regime":       regime,
            # Optionnelles
            "pnl_gross":    round(float(pnl_gross), 2),
            "adx":          round(float(bar["adx"]), 1) if pd.notna(bar["adx"]) else None,
            "atr_pct":      round(float(bar["atr_pct"]), 2) if pd.notna(bar["atr_pct"]) else None,
            "is_macro_day": bool(is_macro),
            "setup_low":    float(signal.get("setup_low", 0.0)),
            "setup_high":   float(signal.get("setup_high", 0.0)),
            "poc":          float(profile["poc"]),
            "vah":          float(profile["vah"]),
            "val":          float(profile["val"]),
        })

    cols = ["date","ticker","strategy","setup","dir","entry","sl","tp","sl_dist","tp_dist",
            "rr","n_ct","result","pnl","fill_time","exit_time","exit","regime",
            "pnl_gross","adx","atr_pct","is_macro_day","setup_low","setup_high",
            "poc","vah","val"]
    return pd.DataFrame(trades, columns=cols) if trades else pd.DataFrame(columns=cols)


# ══════════════════════════════════════════════════════════════════════════════
# Visualisation par jour
# ══════════════════════════════════════════════════════════════════════════════

def plot_day(
    df_15m: pd.DataFrame,
    ticker: str,
    date_str: str,
    day_trades: list,
    output_path: str,
) -> None:
    """Chart d'une journée VPC : OHLC + EMA + niveaux profil + trades."""
    if df_15m.index.tz is None:
        idx_ny = df_15m.index.tz_localize("UTC").tz_convert(_NY)
    else:
        idx_ny = df_15m.index.tz_convert(_NY)

    day_target = pd.Timestamp(date_str).date()
    mask = pd.Series([d.date() == day_target for d in idx_ny])
    if not mask.any():
        return
    day = df_15m.iloc[mask.values].copy()
    if day.empty:
        return

    # Recalcul indicateurs (sans look-ahead → on copie le full DF et on tronque ensuite)
    fast, slow = _compute_emas(df_15m, VPC_EMA_FAST_PERIOD, VPC_EMA_SLOW_PERIOD)
    day["ema_fast"] = fast.loc[day.index]
    day["ema_slow"] = slow.loc[day.index]
    day["atr"]      = _compute_atr(df_15m, VPC_ATR_PERIOD).loc[day.index]

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(16, 10),
        gridspec_kw={"height_ratios": [3, 1]}, sharex=True,
    )
    x = range(len(day))

    # Chandeliers
    for i, (_, row) in enumerate(day.iterrows()):
        color = "#26a69a" if row["close"] >= row["open"] else "#ef5350"
        ax1.plot([i, i], [row["low"], row["high"]], color=color, lw=0.8)
        ax1.bar(i, abs(row["close"] - row["open"]),
                bottom=min(row["open"], row["close"]), color=color, width=0.6, alpha=0.9)

    # EMAs
    ax1.plot(x, day["ema_fast"], color="#42a5f5", lw=1.2, label="EMA20")
    ax1.plot(x, day["ema_slow"], color="#ffb74d", lw=1.2, label="EMA50")

    # Niveaux Volume Profile veille (extraits du premier trade du jour)
    poc = vah = val = None
    if day_trades:
        t0 = day_trades[0]
        poc = t0.get("poc")
        vah = t0.get("vah")
        val = t0.get("val")
    if poc is not None:
        ax1.axhline(poc, color="#ffd54f", ls="-",  lw=1.0, alpha=0.75, label="POC (veille)")
    if vah is not None:
        ax1.axhline(vah, color="#ce93d8", ls="--", lw=0.9, alpha=0.6, label="VAH/VAL (veille)")
    if val is not None:
        ax1.axhline(val, color="#ce93d8", ls="--", lw=0.9, alpha=0.6)

    # Trades
    day_pnl = 0.0
    for trade in day_trades:
        if trade.get("result") == "NOT_FILLED" or trade.get("fill_time") is None:
            continue
        try:
            fill_idx = list(day.index).index(pd.Timestamp(trade["fill_time"]).tz_localize(None)
                                              if pd.Timestamp(trade["fill_time"]).tz is None
                                              else pd.Timestamp(trade["fill_time"]))
        except (ValueError, KeyError):
            continue
        try:
            exit_idx = list(day.index).index(pd.Timestamp(trade["exit_time"]).tz_localize(None)
                                              if pd.Timestamp(trade["exit_time"]).tz is None
                                              else pd.Timestamp(trade["exit_time"]))
        except (ValueError, KeyError):
            exit_idx = fill_idx

        color  = "#00c853" if trade["dir"] == "long" else "#d50000"
        marker = "^"       if trade["dir"] == "long" else "v"
        ax1.scatter(fill_idx, trade["entry"], marker=marker, color=color, s=180, zorder=5,
                    edgecolors="white", linewidths=0.8)
        ax1.scatter(exit_idx, trade["exit"],  marker="x", color="white", s=140, zorder=5)
        ax1.axhline(trade["sl"], color="#ef5350", ls=":", lw=0.7, alpha=0.5)
        ax1.axhline(trade["tp"], color="#26a69a", ls=":", lw=0.7, alpha=0.5)

        # Annotation setup
        ax1.annotate(
            f"{trade.get('setup','?')[:3]} {trade['dir'][0].upper()} {trade['pnl']:+.0f}$",
            xy=(fill_idx, trade["entry"]),
            xytext=(fill_idx + 1, trade["entry"]),
            fontsize=7, color="white", alpha=0.85,
        )
        day_pnl += float(trade.get("pnl", 0.0))

    # Sous-graphe : volume + moyenne
    vol_avg = day["volume"].rolling(VPC_VOL_AVG_WINDOW, min_periods=2).mean()
    ax2.bar(x, day["volume"].values, color="#7986cb", alpha=0.7, width=0.6)
    ax2.plot(x, vol_avg.values, color="#ffeb3b", lw=1.0, label="Vol avg 20")
    ax2.set_ylabel("Volume", fontsize=9)
    ax2.legend(loc="upper right", fontsize=8)

    ax1.set_title(
        f"{ticker} — {date_str}  [VPC]   |   P&L net jour : {day_pnl:+.0f} $",
        fontsize=12, fontweight="bold",
    )
    ax1.legend(loc="upper left", fontsize=8)
    ax1.set_facecolor("#1a1a2e")
    ax2.set_facecolor("#1a1a2e")
    fig.patch.set_facecolor("#13131f")
    ax1.tick_params(colors="white")
    ax2.tick_params(colors="white")
    ax1.title.set_color("white")
    for s in list(ax1.spines.values()) + list(ax2.spines.values()):
        s.set_color("#787b86")

    plt.tight_layout()
    plt.savefig(output_path, dpi=120, bbox_inches="tight")
    plt.close(fig)
