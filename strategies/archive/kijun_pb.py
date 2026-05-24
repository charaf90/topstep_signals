"""
Stratégie KIJUN_PB — Kijun pullback bidirectionnel (kijun_pb-v1).

Concept :
    En régime trending (prix nettement hors Cloud Ichimoku 15m), la Kijun(26)
    sert de barycentre/fair value à moyen terme. Les retracements vers la
    Kijun en alignement avec un cross StochRSI depuis une zone extrême
    (survente pour LONG, surachat pour SHORT) offrent une entrée limit à
    fort R:R.

Setup LONG (SHORT symétrique) — décision sur la barre i avec lecture i-1, i-2 :
    (1) close[i-1] > max(Senkou_A[i-1], Senkou_B[i-1]) + BUFFER × ATR[i-1]
    (2) Kijun[i-1] > Kijun[i-1 - SLOPE_LOOKBACK]
    (3) min(low[i-LOOKBACK..i-1]) <= max(Kijun[i-LOOKBACK..i-1])  (pullback récent)
    (4) close[i-1] > Kijun[i-1]                                   (retracement résolu)
    (5) Cross StochRSI K>D à i-1, K[i-2] <= D[i-2], K[i-2] < OVERSOLD
    (6) Aucun trade ouvert sur ce ticker

Trigger :
    Limit BUY @ close[i-1]. Fill SI low[i] <= entry (LONG). Sinon NOT_FILLED.

Gestion :
    SL  = low_pullback - SL_BUFFER × ATR[i-1]
    TP  = entry + TP_MULT × ATR[i-1]
    SL prioritaire si SL+TP dans même barre.
    Max KIJUN_PB_MAX_TRADES_PER_DAY trades/jour/ticker.

Edge théorique :
    Confluence d'ordres limit retail/quant autour de la Kijun en trending,
    plus filtre cross StochRSI extrême (rare) qui filtre la majorité du temps.
    Le filtre Cloud-out écarte les régimes plats où Kijun n'a pas de
    défense significative.

Falsification :
    Bootstrap portfolio OOS < 50 %, OU PF < 1.0 sur 50 trades live consécutifs.

Architecture : RECHERCHE pure (strategies/). N'importe rien de broker/ ou
de core/opr.py / core/strategy_fib.py / core/vpc.py.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from config import (
    COMMISSION_RT_PER_CONTRACT,
    INSTRUMENTS,
    KIJUN_PB_ATR_PERIOD,
    KIJUN_PB_BUFFER_ATR_DEFAULT,
    KIJUN_PB_EXCLUDE_MACRO_DAYS,
    KIJUN_PB_KIJUN_PERIOD,
    KIJUN_PB_KUMO_SHIFT,
    KIJUN_PB_LOOKBACK_DEFAULT,
    KIJUN_PB_MAX_HOLD_BARS,
    KIJUN_PB_MAX_TRADES_PER_DAY,
    KIJUN_PB_ORDER_TIMEOUT_BARS,
    KIJUN_PB_RSI_PERIOD,
    KIJUN_PB_SENKOU_B_PERIOD,
    KIJUN_PB_SESSION_END,
    KIJUN_PB_SESSION_START,
    KIJUN_PB_SL_BUFFER_ATR_DEFAULT,
    KIJUN_PB_SL_BUFFER_TICKS_FLOOR,
    KIJUN_PB_SLOPE_LOOKBACK,
    KIJUN_PB_STOCH_D,
    KIJUN_PB_STOCH_K,
    KIJUN_PB_STOCH_PERIOD,
    KIJUN_PB_STOCHRSI_OVERBOUGHT,
    KIJUN_PB_STOCHRSI_OVERSOLD,
    # Kijun_PB params
    KIJUN_PB_STRATEGY_VERSION,
    KIJUN_PB_TENKAN_PERIOD,
    KIJUN_PB_TICKERS,
    KIJUN_PB_TIMEZONE,
    KIJUN_PB_TP_ATR_MULT_DEFAULT,
    MACRO_EVENT_DATES,
    RISK_PER_TRADE_USD,
    SLIPPAGE_TICKS_PER_TICKER,
)

# Réutilise les helpers déjà testés (causaux par construction)
from core.explore_chart import (
    compute_atr,
    compute_ichimoku,
    compute_stochrsi,
)

np.random.seed(42)

# ── Identité de la stratégie ─────────────────────────────────────────────────
STRATEGY_ID = KIJUN_PB_STRATEGY_VERSION  # "kijun_pb-v1"
TICKERS = KIJUN_PB_TICKERS  # ["NQ1"]
CSV_SUFFIX = "_kijun_pb"
CSV_TIMEFRAME = "m15"

_NY = ZoneInfo(KIJUN_PB_TIMEZONE)

# ── Grille d'optimisation (4 dimensions × 2×2×2×3 = 24 combos) ───────────────
# Bonferroni p_seuil = 0.05/24 ≈ 0.00208 → bootstrap > 99.79 %
PARAM_GRID = {
    "buffer_atr": [0.3, 0.5],
    "lookback": [5, 10],
    "sl_buffer_atr": [0.5, 0.8],
    "tp_atr_mult": [1.5, 2.0, 2.5],
}


# ══════════════════════════════════════════════════════════════════════════════
# Backtest principal
# ══════════════════════════════════════════════════════════════════════════════


def run_backtest(
    df_15m: pd.DataFrame,
    ticker: str,
    tf=None,
    params: dict | None = None,
    topstep_guard: bool = True,
) -> pd.DataFrame:
    """
    Exécute le backtest KIJUN_PB sur df_15m pour un ticker.

    Schéma de colonnes standard (compatibilité core/optimizer.py) :
      date, dir, entry, sl, tp, sl_dist, tp_dist, rr, n_ct,
      result (TP|SL|TE|NOT_FILLED), pnl, fill_time, exit_time, exit, regime
    + optionnelles : pnl_gross, adx, atr_pct, is_macro_day, ticker, strategy

    `pnl` = P&L NET (slippage entrée+sortie + commissions intégrés).
    """
    if ticker not in TICKERS:
        return pd.DataFrame()

    p = params or {}
    buffer_atr = p.get("buffer_atr", KIJUN_PB_BUFFER_ATR_DEFAULT)
    lookback = int(p.get("lookback", KIJUN_PB_LOOKBACK_DEFAULT))
    sl_buffer_atr = p.get("sl_buffer_atr", KIJUN_PB_SL_BUFFER_ATR_DEFAULT)
    tp_atr_mult = p.get("tp_atr_mult", KIJUN_PB_TP_ATR_MULT_DEFAULT)

    instr = INSTRUMENTS[ticker]
    tick_size = instr["tick_size"]
    pt_value = instr["dollar_per_point"]
    slip_ticks = SLIPPAGE_TICKS_PER_TICKER.get(ticker, 1)
    slip_px = slip_ticks * tick_size
    sl_floor_px = KIJUN_PB_SL_BUFFER_TICKS_FLOOR * tick_size

    # ── Indicateurs (causaux : on les calcule sur df complet mais on lit i-1) ──
    df = df_15m.copy()
    ichi = compute_ichimoku(
        df,
        tenkan=KIJUN_PB_TENKAN_PERIOD,
        kijun=KIJUN_PB_KIJUN_PERIOD,
        senkou_b=KIJUN_PB_SENKOU_B_PERIOD,
        shift=KIJUN_PB_KUMO_SHIFT,
    )
    df["kijun"] = ichi["kijun"]
    df["tenkan"] = ichi["tenkan"]
    # Senkou A/B sont déjà projetées 26 barres dans le futur PAR CONSTRUCTION
    # (shift=+26 dans compute_ichimoku). Donc à la barre i, df["senkou_a"].iloc[i]
    # = projection du Kumo "futur". Pour décider sur barre i avec l'état Kumo
    # **présent** à i, il faut lire senkou_a[i] (projeté depuis i-26) → c'est
    # bien causal (la valeur est celle calculée à i-26 = passé).
    df["senkou_a"] = ichi["senkou_a"]
    df["senkou_b"] = ichi["senkou_b"]

    df["atr"] = compute_atr(df, KIJUN_PB_ATR_PERIOD)
    df["atr_pct"] = df["atr"].rolling(window=20 * 26, min_periods=50).rank(pct=True)

    stoch_k, stoch_d = compute_stochrsi(
        df["close"],
        rsi_period=KIJUN_PB_RSI_PERIOD,
        stoch_period=KIJUN_PB_STOCH_PERIOD,
        k=KIJUN_PB_STOCH_K,
        d=KIJUN_PB_STOCH_D,
    )
    df["stoch_k"] = stoch_k
    df["stoch_d"] = stoch_d

    # ADX léger juste pour tagging régime (pas pour filtrage)
    from core.explore_chart import compute_adx

    df["adx"] = compute_adx(df, 14)

    # Convert index → NY
    if df.index.tz is None:
        idx_ny_all = df.index.tz_localize("UTC").tz_convert(_NY)
    else:
        idx_ny_all = df.index.tz_convert(_NY)

    # Warmup : Senkou B = 52 barres + Kumo shift = 26 → 78 barres min + buffer
    warmup = max(
        KIJUN_PB_SENKOU_B_PERIOD + KIJUN_PB_KUMO_SHIFT + 10,
        KIJUN_PB_RSI_PERIOD + KIJUN_PB_STOCH_PERIOD + 10,
        100,
    )

    trades: list[dict] = []
    daily_trades: dict[str, int] = {}

    n = len(df)
    h_start, m_start = KIJUN_PB_SESSION_START
    h_end, m_end = KIJUN_PB_SESSION_END

    for i in range(warmup, n):
        ts_ny = idx_ny_all[i]

        # ── Filtres séance ─────────────────────────────────────────────
        if ts_ny.weekday() >= 5:
            continue

        # Fenêtre NY : [SESSION_START, SESSION_END[
        hm_cur = ts_ny.hour * 60 + ts_ny.minute
        hm_lo = h_start * 60 + m_start
        hm_hi = h_end * 60 + m_end
        if hm_cur < hm_lo or hm_cur >= hm_hi:
            continue

        date_str = ts_ny.strftime("%Y-%m-%d")

        if KIJUN_PB_EXCLUDE_MACRO_DAYS and date_str in MACRO_EVENT_DATES:
            continue
        if daily_trades.get(date_str, 0) >= KIJUN_PB_MAX_TRADES_PER_DAY:
            continue

        # ── Lecture causale : on décide sur i en lisant i-1 et i-2 ─────
        prev = df.iloc[i - 1]
        prev2 = df.iloc[i - 2]
        bar = df.iloc[i]

        # Validité des indicateurs
        for col in ("kijun", "senkou_a", "senkou_b", "atr", "stoch_k", "stoch_d"):
            if pd.isna(prev[col]) or pd.isna(prev2[col]):
                break
        else:
            atr_val = float(prev["atr"])
            if atr_val <= 0:
                continue

            # Senkou A/B "présentes" à i-1 (déjà shiftées dans le helper)
            sa = float(prev["senkou_a"])
            sb = float(prev["senkou_b"])
            cloud_top = max(sa, sb)
            cloud_bot = min(sa, sb)

            kijun_prev = float(prev["kijun"])
            kijun_slope = float(prev["kijun"]) - float(
                df.iloc[i - 1 - KIJUN_PB_SLOPE_LOOKBACK]["kijun"]
            )

            # Pullback récent : window [i-lookback .. i-1]
            window = df.iloc[i - lookback : i]
            low_pullback = float(window["low"].min())
            high_pullback = float(window["high"].max())
            kijun_window_max = float(window["kijun"].max())
            kijun_window_min = float(window["kijun"].min())

            # Cross StochRSI à i-1
            k1 = float(prev["stoch_k"])
            d1 = float(prev["stoch_d"])
            k2 = float(prev2["stoch_k"])
            d2 = float(prev2["stoch_d"])

            signal = None

            # ── LONG ───────────────────────────────────────────────────
            cond_long_cloud = float(prev["close"]) > cloud_top + buffer_atr * atr_val
            cond_long_slope = kijun_slope > 0
            cond_long_pb = low_pullback <= kijun_window_max
            cond_long_above = float(prev["close"]) > kijun_prev
            cond_long_cross = (k1 > d1) and (k2 <= d2) and (k2 < KIJUN_PB_STOCHRSI_OVERSOLD)

            if (
                cond_long_cloud
                and cond_long_slope
                and cond_long_pb
                and cond_long_above
                and cond_long_cross
            ):
                entry = float(prev["close"])
                sl_base = low_pullback - sl_buffer_atr * atr_val
                # Floor : éviter SL trop serré (au moins SL_BUFFER_TICKS_FLOOR)
                if (entry - sl_base) < sl_floor_px:
                    sl_base = entry - sl_floor_px
                sl_px = sl_base
                tp_px = entry + tp_atr_mult * atr_val
                signal = {
                    "direction": "long",
                    "entry": entry,
                    "sl": sl_px,
                    "tp": tp_px,
                    "setup_low": low_pullback,
                    "setup_high": high_pullback,
                }

            # ── SHORT (symétrique strict) ──────────────────────────────
            if signal is None:
                cond_short_cloud = float(prev["close"]) < cloud_bot - buffer_atr * atr_val
                cond_short_slope = kijun_slope < 0
                cond_short_pb = high_pullback >= kijun_window_min
                cond_short_below = float(prev["close"]) < kijun_prev
                cond_short_cross = (k1 < d1) and (k2 >= d2) and (k2 > KIJUN_PB_STOCHRSI_OVERBOUGHT)

                if (
                    cond_short_cloud
                    and cond_short_slope
                    and cond_short_pb
                    and cond_short_below
                    and cond_short_cross
                ):
                    entry = float(prev["close"])
                    sl_base = high_pullback + sl_buffer_atr * atr_val
                    if (sl_base - entry) < sl_floor_px:
                        sl_base = entry + sl_floor_px
                    sl_px = sl_base
                    tp_px = entry - tp_atr_mult * atr_val
                    signal = {
                        "direction": "short",
                        "entry": entry,
                        "sl": sl_px,
                        "tp": tp_px,
                        "setup_low": low_pullback,
                        "setup_high": high_pullback,
                    }

            if signal is None:
                continue

            direction = signal["direction"]
            entry = signal["entry"]
            sl = signal["sl"]
            tp = signal["tp"]
            sl_dist = abs(entry - sl)
            tp_dist = abs(tp - entry)
            if sl_dist <= 0 or tp_dist <= 0:
                continue
            rr = tp_dist / sl_dist

            # Sizing avec slippage anticipé
            risk_per_contract = (sl_dist + slip_px) * pt_value
            if risk_per_contract <= 0:
                continue
            n_ct = max(1, int(RISK_PER_TRADE_USD / risk_per_contract))

            # ── Simulation fill limit + exit (conservatif) ─────────────
            result = "NOT_FILLED"
            fill_time = None
            exit_time = None
            exit_px = None
            pnl_gross = 0.0
            pnl = 0.0

            # Fill limit : on cherche un fill dans les KIJUN_PB_ORDER_TIMEOUT_BARS suivantes
            # à partir de la barre i (la barre i = la barre où le signal est armé).
            fill_start = None
            for j in range(i, min(i + KIJUN_PB_ORDER_TIMEOUT_BARS, n)):
                fut = df.iloc[j]
                if direction == "long" and float(fut["low"]) <= entry:
                    fill_start = j
                    fill_time = df.index[j]
                    break
                if direction == "short" and float(fut["high"]) >= entry:
                    fill_start = j
                    fill_time = df.index[j]
                    break

            if fill_start is not None:
                # Sur la barre de fill : ambiguïté SL+TP → SL prioritaire
                fb = df.iloc[fill_start]
                if direction == "long":
                    sl_hit_at_fill = float(fb["low"]) <= sl
                    tp_hit_at_fill = float(fb["high"]) >= tp
                else:
                    sl_hit_at_fill = float(fb["high"]) >= sl
                    tp_hit_at_fill = float(fb["low"]) <= tp

                if sl_hit_at_fill:
                    result, exit_px, exit_time = "SL", sl, df.index[fill_start]
                elif tp_hit_at_fill:
                    result, exit_px, exit_time = "TP", tp, df.index[fill_start]
                else:
                    # Cherche dans les barres suivantes (max hold + fin session NY)
                    max_end = min(fill_start + KIJUN_PB_MAX_HOLD_BARS, n)
                    exit_found = False
                    for k in range(fill_start + 1, max_end):
                        fk = df.iloc[k]
                        k_ny = idx_ny_all[k]
                        # Forçage fin de session NY (16h NY, marge large)
                        if k_ny.hour >= 16:
                            result, exit_px, exit_time = "TE", float(fk["open"]), df.index[k]
                            exit_found = True
                            break
                        if direction == "long":
                            sl_k = float(fk["low"]) <= sl
                            tp_k = float(fk["high"]) >= tp
                        else:
                            sl_k = float(fk["high"]) >= sl
                            tp_k = float(fk["low"]) <= tp

                        if sl_k and tp_k:
                            # Ambiguïté → SL prioritaire (conservateur)
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
                        last_k = min(fill_start + KIJUN_PB_MAX_HOLD_BARS, n - 1)
                        result, exit_px, exit_time = (
                            "TE",
                            float(df.iloc[last_k]["close"]),
                            df.index[last_k],
                        )

                # ── Calcul P&L brut + net ─────────────────────────────
                raw_move = (exit_px - entry) * (1 if direction == "long" else -1)
                pnl_gross = raw_move * n_ct * pt_value
                slip_cost = 2 * slip_px * n_ct * pt_value  # entrée + sortie
                comm_cost = COMMISSION_RT_PER_CONTRACT * n_ct  # round-trip
                pnl = pnl_gross - slip_cost - comm_cost
                daily_trades[date_str] = daily_trades.get(date_str, 0) + 1

            # Tagging régime (ADX du moment du signal)
            adx_val = float(prev["adx"]) if pd.notna(prev["adx"]) else 0.0
            if adx_val > 25:
                regime = "trending"
            elif adx_val < 20:
                regime = "ranging"
            else:
                regime = "neutral"

            is_macro = date_str in MACRO_EVENT_DATES

            trades.append(
                {
                    # Schéma standard
                    "date": date_str,
                    "ticker": ticker,
                    "strategy": "KIJUN_PB",
                    "dir": direction,
                    "entry": round(float(entry), 4),
                    "sl": round(float(sl), 4),
                    "tp": round(float(tp), 4),
                    "sl_dist": round(float(sl_dist), 4),
                    "tp_dist": round(float(tp_dist), 4),
                    "rr": round(float(rr), 2),
                    "n_ct": int(n_ct),
                    "result": result,
                    "pnl": round(float(pnl), 2),
                    "fill_time": fill_time,
                    "exit_time": exit_time,
                    "exit": round(float(exit_px), 4) if exit_px is not None else None,
                    "regime": regime,
                    # Optionnelles
                    "pnl_gross": round(float(pnl_gross), 2),
                    "adx": round(float(adx_val), 1) if adx_val > 0 else None,
                    "atr_pct": (
                        round(float(prev["atr_pct"]), 2) if pd.notna(prev["atr_pct"]) else None
                    ),
                    "is_macro_day": bool(is_macro),
                    "setup_low": round(float(signal["setup_low"]), 4),
                    "setup_high": round(float(signal["setup_high"]), 4),
                    "kijun_at_signal": round(float(prev["kijun"]), 4),
                    "stoch_k_prev": round(float(k1), 2),
                    "stoch_d_prev": round(float(d1), 2),
                }
            )

    cols = [
        "date",
        "ticker",
        "strategy",
        "dir",
        "entry",
        "sl",
        "tp",
        "sl_dist",
        "tp_dist",
        "rr",
        "n_ct",
        "result",
        "pnl",
        "fill_time",
        "exit_time",
        "exit",
        "regime",
        "pnl_gross",
        "adx",
        "atr_pct",
        "is_macro_day",
        "setup_low",
        "setup_high",
        "kijun_at_signal",
        "stoch_k_prev",
        "stoch_d_prev",
    ]
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
    """Chart d'une journée KIJUN_PB : OHLC + Cloud + Kijun + StochRSI + trades."""
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

    # Recalcul indicateurs sur full df puis tronque (causal pour les indicateurs
    # rolling — la valeur à date X reste la même qu'elle soit calculée sur
    # df[:X+k] ou df entier, par construction des rolling Wilder)
    ichi = compute_ichimoku(
        df_15m,
        tenkan=KIJUN_PB_TENKAN_PERIOD,
        kijun=KIJUN_PB_KIJUN_PERIOD,
        senkou_b=KIJUN_PB_SENKOU_B_PERIOD,
        shift=KIJUN_PB_KUMO_SHIFT,
    )
    day["kijun"] = ichi["kijun"].loc[day.index]
    day["tenkan"] = ichi["tenkan"].loc[day.index]
    day["senkou_a"] = ichi["senkou_a"].loc[day.index]
    day["senkou_b"] = ichi["senkou_b"].loc[day.index]
    stoch_k, stoch_d = compute_stochrsi(
        df_15m["close"],
        rsi_period=KIJUN_PB_RSI_PERIOD,
        stoch_period=KIJUN_PB_STOCH_PERIOD,
        k=KIJUN_PB_STOCH_K,
        d=KIJUN_PB_STOCH_D,
    )
    day["stoch_k"] = stoch_k.loc[day.index]
    day["stoch_d"] = stoch_d.loc[day.index]

    fig, (ax1, ax2) = plt.subplots(
        2,
        1,
        figsize=(16, 10),
        gridspec_kw={"height_ratios": [3, 1]},
        sharex=True,
    )
    x = list(range(len(day)))

    # Chandeliers
    for i, (_, row) in enumerate(day.iterrows()):
        color = "#26a69a" if row["close"] >= row["open"] else "#ef5350"
        ax1.plot([i, i], [row["low"], row["high"]], color=color, lw=0.8)
        ax1.bar(
            i,
            abs(row["close"] - row["open"]),
            bottom=min(row["open"], row["close"]),
            color=color,
            width=0.6,
            alpha=0.9,
        )

    # Cloud Ichimoku
    sa = day["senkou_a"].values
    sb = day["senkou_b"].values
    bullish_cloud = sa > sb
    ax1.fill_between(x, sa, sb, where=bullish_cloud, color="#26a69a", alpha=0.18, zorder=1)
    ax1.fill_between(x, sa, sb, where=~bullish_cloud, color="#ef5350", alpha=0.18, zorder=1)
    ax1.plot(x, sa, color="#26a69a", lw=0.6, alpha=0.7)
    ax1.plot(x, sb, color="#ef5350", lw=0.6, alpha=0.7)

    # Tenkan + Kijun
    ax1.plot(x, day["tenkan"].values, color="#ff7043", lw=1.0, label="Tenkan(9)", alpha=0.9)
    ax1.plot(x, day["kijun"].values, color="#2962ff", lw=1.4, label="Kijun(26)", alpha=0.95)

    # Trades
    day_pnl = 0.0
    for trade in day_trades:
        if trade.get("result") == "NOT_FILLED" or trade.get("fill_time") is None:
            continue
        try:
            fill_ts = pd.Timestamp(trade["fill_time"])
            if fill_ts.tz is not None:
                fill_ts = fill_ts.tz_localize(None)
            fill_idx = list(day.index).index(fill_ts)
        except (ValueError, KeyError):
            continue
        try:
            exit_ts = pd.Timestamp(trade["exit_time"])
            if exit_ts.tz is not None:
                exit_ts = exit_ts.tz_localize(None)
            exit_idx = list(day.index).index(exit_ts)
        except (ValueError, KeyError):
            exit_idx = fill_idx

        color = "#00c853" if trade["dir"] == "long" else "#d50000"
        marker = "^" if trade["dir"] == "long" else "v"
        ax1.scatter(
            fill_idx,
            trade["entry"],
            marker=marker,
            color=color,
            s=180,
            zorder=5,
            edgecolors="white",
            linewidths=0.8,
        )
        ax1.scatter(exit_idx, trade["exit"], marker="x", color="white", s=140, zorder=5)
        ax1.axhline(trade["sl"], color="#ef5350", ls=":", lw=0.7, alpha=0.5)
        ax1.axhline(trade["tp"], color="#26a69a", ls=":", lw=0.7, alpha=0.5)

        ax1.annotate(
            f"{trade['dir'][0].upper()} {trade['pnl']:+.0f}$",
            xy=(fill_idx, trade["entry"]),
            xytext=(fill_idx + 1, trade["entry"]),
            fontsize=8,
            color="white",
            alpha=0.85,
        )
        day_pnl += float(trade.get("pnl", 0.0))

    # Sous-graphe StochRSI
    ax2.plot(x, day["stoch_k"].values, color="#f7b801", lw=1.0, label="StochRSI %K")
    ax2.plot(x, day["stoch_d"].values, color="#a566ff", lw=1.0, label="StochRSI %D")
    ax2.axhline(KIJUN_PB_STOCHRSI_OVERBOUGHT, color="#787b86", ls="--", lw=0.5, alpha=0.5)
    ax2.axhline(KIJUN_PB_STOCHRSI_OVERSOLD, color="#787b86", ls="--", lw=0.5, alpha=0.5)
    ax2.set_ylim(0, 100)
    ax2.set_ylabel("StochRSI", fontsize=9)
    ax2.legend(loc="upper right", fontsize=8)

    ax1.set_title(
        f"{ticker} — {date_str}  [KIJUN_PB]   |   P&L net jour : {day_pnl:+.0f} $",
        fontsize=12,
        fontweight="bold",
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
