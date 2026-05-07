"""
Stratégie SMC (Smart Money Concepts) — wrapper backtest plug-and-play.

Interface :
    run_backtest(df_15m, ticker, tf=None, params=None, topstep_guard=True)
    plot_day(df_15m, ticker, date_str, day_trades, output_path)

Logique : core/strategy_smc.py
"""

import pandas as pd
from zoneinfo import ZoneInfo

from config import (
    INSTRUMENTS, SMC_STRATEGY_VERSION, SMC_TIMEZONE,
    DAILY_STOP_AFTER_SL, CONSEC_LOSS_PAUSE_DAYS, DAILY_LOCKIN_THRESHOLD,
)
from core.strategy_smc import run_smc_day
from core.risk_topstep import trade_allowed

STRATEGY_ID = SMC_STRATEGY_VERSION       # "smc-v1"
TICKERS     = ["MES1", "NQ1", "YM1"]
CSV_SUFFIX  = "_smc"

# Grille walk-forward
PARAM_GRID = {
    "killzones":   ["ny_afternoon", "london_open",
                    ["ny_afternoon", "london_open"]],
    "signal_type": ["choch", "bos", "both"],
    "entry_pct":   [0.3, 0.5, 0.7],
    "sl_atr_mult": [0.5, 1.0, 1.5],
    "tp_atr_mult": [1.5, 2.0, 2.5, 3.0],
}


def run_backtest(
    df_15m: pd.DataFrame,
    ticker: str,
    tf=None,
    params: dict = None,
    topstep_guard: bool = True,
) -> pd.DataFrame:
    tz = ZoneInfo(SMC_TIMEZONE)

    if df_15m.index.tz is None:
        idx_ny = df_15m.index.tz_localize("UTC").tz_convert(tz)
    else:
        idx_ny = df_15m.index.tz_convert(tz)
    ny_days = pd.DatetimeIndex(idx_ny.normalize().unique()).sort_values()

    trades_out       = []
    cum_pnl  = 0.0
    peak_pnl = 0.0
    consec_loss_days = 0

    for day_ny in ny_days:
        ds = day_ny.strftime("%Y-%m-%d")

        if CONSEC_LOSS_PAUSE_DAYS > 0 and consec_loss_days >= CONSEC_LOSS_PAUSE_DAYS:
            consec_loss_days = 0
            continue

        if topstep_guard:
            allowed, _ = trade_allowed(day_pnl=0.0, cum_pnl=cum_pnl, peak_pnl=peak_pnl)
            if not allowed:
                continue

        signals, sim_results = run_smc_day(df_15m, ticker, day_ny, params=params)
        if not signals:
            continue

        day_trades = []
        for sig, res in zip(signals, sim_results):
            day_trades.append({
                "date":         ds,
                "strategy":     "SMC",
                "dir":          sig["direction"],
                "entry":        sig["entry"],
                "sl":           sig["sl"],
                "tp":           sig["tp"],
                "sl_dist":      sig["sl_dist"],
                "tp_dist":      sig["tp_dist"],
                "rr":           sig["rr"],
                "n_ct":         sig["n_ct"],
                "risk_$":       sig["risk"],
                "regime":       sig.get("struct_type", "SMC"),
                "zone_low":     sig["zone_low"],
                "zone_high":    sig["zone_high"],
                "struct_type":  sig.get("struct_type"),    # CHoCH | BOS
                "struct_label": sig.get("struct_label"),   # swing | internal
                "struct_size":  sig.get("struct_size"),    # 50 | 5
                "killzone":     sig.get("killzone"),
                "trigger_time": sig.get("trigger_time"),
                **res,
            })

        # Circuit breakers intra-jour
        filled    = [t for t in day_trades if t["result"] != "NOT_FILLED"]
        not_filled = [t for t in day_trades if t["result"] == "NOT_FILLED"]
        filled.sort(key=lambda t: t.get("fill_time") or "")

        kept = []
        cancelled_cb = []
        running_pnl  = 0.0
        breaker      = False
        for t in filled:
            if breaker:
                cancelled_cb.append(t)
                continue
            kept.append(t)
            running_pnl += t["pnl"]
            if DAILY_STOP_AFTER_SL and t["result"] == "SL":
                breaker = True
            elif DAILY_LOCKIN_THRESHOLD > 0 and running_pnl >= DAILY_LOCKIN_THRESHOLD:
                breaker = True

        for t in cancelled_cb:
            t.update({"result": "NOT_FILLED", "pnl": 0,
                      "fill_time": None, "exit_time": None, "exit": None})

        trades_out.extend(kept + not_filled + cancelled_cb)

        day_pnl  = sum(t["pnl"] for t in kept)
        cum_pnl += day_pnl
        if cum_pnl > peak_pnl:
            peak_pnl = cum_pnl
        if day_pnl < 0:
            consec_loss_days += 1
        elif day_pnl > 0:
            consec_loss_days = 0

    return pd.DataFrame(trades_out)


def plot_day(
    df_15m: pd.DataFrame,
    ticker: str,
    date_str: str,
    day_trades: list,
    output_path: str,
):
    """Chart SMC d'une journée : OHLC + zones OB + structures + trades."""
    from core.analysis_chart import plot_day_analysis
    from zoneinfo import ZoneInfo

    tz     = ZoneInfo(SMC_TIMEZONE)
    day_ny = pd.Timestamp(date_str).tz_localize(tz)

    cutoff = pd.Timestamp(f"{date_str} 11:00:00")
    us_end = (day_ny.replace(hour=16, minute=30)
                    .tz_convert("UTC").tz_localize(None))

    zones_for_chart = []
    for t in day_trades:
        if t.get("zone_low") is None or t.get("zone_high") is None:
            continue
        zones_for_chart.append({
            "low":         float(t["zone_low"]),
            "high":        float(t["zone_high"]),
            "mid":         (float(t["zone_low"]) + float(t["zone_high"])) / 2,
            "quality":     90.0,
            "n_tf":        1,
            "touches":     1,
            "tfs":         [t.get("struct_type", "SMC")],
            "dominant_tf": t.get("struct_type", "SMC"),
        })

    pseudo_signals = [
        {**t, "direction": t["dir"], "quality": 90.0, "composite": 0.0,
         "n_tf": 1, "touches": 1, "alignment": 0.0, "atr_ratio": 0.0}
        for t in day_trades if t.get("result") != "NOT_FILLED"
    ]

    plot_day_analysis(
        df_15m=df_15m,
        ticker=ticker,
        date_str=date_str,
        cutoff=cutoff,
        us_end=us_end,
        zones=zones_for_chart,
        signals=pseudo_signals,
        trades=day_trades,
        regime=None,
        alignment_score=None,
        pm_features=None,
        vol_features=None,
        output_path=output_path,
    )
