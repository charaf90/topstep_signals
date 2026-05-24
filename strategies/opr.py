"""
Stratégie OPR (Opening Range Breakout pullback) — opr-v4.

Interface plug-and-play :
    run_backtest(df_15m, ticker, tf=None, params=None, topstep_guard=True)
    plot_day(df_15m, ticker, date_str, day_trades, output_path)

Production : core/opr.py (live_runner.py l'importe directement — NE PAS MODIFIER)
Ce module est le wrapper backtest/recherche uniquement.
"""

from zoneinfo import ZoneInfo

import pandas as pd

import config as cfg
from config import (
    CONSEC_LOSS_PAUSE_DAYS,
    CUTOFF_HOUR_UTC,
    DAILY_LOCKIN_THRESHOLD,
    DAILY_STOP_AFTER_SL,
    OPR_SL_ATR_MULT,
    OPR_STRATEGY_VERSION,
    OPR_TIMEZONE,
    OPR_TP_ATR_MULT,
)
from core.opr import run_opr_day
from core.risk_topstep import trade_allowed

# ── Identité de la stratégie ─────────────────────────────────────────────────
STRATEGY_ID = OPR_STRATEGY_VERSION  # "opr-v4"
TICKERS = ["MES1", "NQ1", "YM1"]
CSV_SUFFIX = "_opr"

# ── Grille d'optimisation (walk-forward via core/optimizer.py) ───────────────
PARAM_GRID = {
    "sl_mult": [0.10, 0.15, 0.20, 0.25, 0.30, 0.40],
    "tp_mult": [0.20, 0.35, 0.50, 0.70, 1.00, 1.30],
}


# ══════════════════════════════════════════════════════════════════════════════
# Backtest
# ══════════════════════════════════════════════════════════════════════════════


def run_backtest(
    df_15m: pd.DataFrame,
    ticker: str,
    tf=None,
    params: dict = None,
    topstep_guard: bool = True,
) -> pd.DataFrame:
    """
    Backtest OPR complet jour par jour (heure NY, DST-aware).
    params = {"sl_mult": float, "tp_mult": float}  — override config par ticker.
    """
    tz = ZoneInfo(OPR_TIMEZONE)

    # Override params pour cette exécution
    sl_orig = OPR_SL_ATR_MULT[ticker]
    tp_orig = OPR_TP_ATR_MULT[ticker]
    if params:
        sl = params.get("sl_mult", sl_orig)
        tp = params.get("tp_mult", tp_orig)
        cfg.OPR_SL_ATR_MULT[ticker] = sl
        cfg.OPR_TP_ATR_MULT[ticker] = tp
        from core import opr as _opr

        _opr.OPR_SL_ATR_MULT[ticker] = sl
        _opr.OPR_TP_ATR_MULT[ticker] = tp

    try:
        trades_out = []
        cum_pnl = peak_pnl = 0.0
        consec_loss_days = 0

        if df_15m.index.tz is None:
            idx_ny = df_15m.index.tz_localize("UTC").tz_convert(tz)
        else:
            idx_ny = df_15m.index.tz_convert(tz)
        ny_days = pd.DatetimeIndex(idx_ny.normalize().unique()).sort_values()

        for day_ny in ny_days:
            ds = day_ny.strftime("%Y-%m-%d")

            if CONSEC_LOSS_PAUSE_DAYS > 0 and consec_loss_days >= CONSEC_LOSS_PAUSE_DAYS:
                consec_loss_days = 0
                continue

            if topstep_guard:
                allowed, _ = trade_allowed(day_pnl=0.0, cum_pnl=cum_pnl, peak_pnl=peak_pnl)
                if not allowed:
                    continue

            regime = None

            signals, sim_results, _ = run_opr_day(df_15m, ticker, day_ny)
            if not signals:
                continue

            day_trades = []
            for sig, res in zip(signals, sim_results):
                day_trades.append(
                    {
                        "date": ds,
                        "strategy": "OPR",
                        "dir": sig["direction"],
                        "entry": sig["entry"],
                        "sl": sig["sl"],
                        "tp": sig["tp"],
                        "sl_dist": sig["sl_dist"],
                        "tp_dist": sig["tp_dist"],
                        "rr": sig["rr"],
                        "n_ct": sig["n_ct"],
                        "risk_$": sig["risk"],
                        "regime": regime or "?",
                        "zone_low": sig["zone_low"],
                        "zone_high": sig["zone_high"],
                        "trigger_time": sig.get("trigger_time"),
                        **res,
                    }
                )

            filled = [t for t in day_trades if t["result"] != "NOT_FILLED"]
            not_filled = [t for t in day_trades if t["result"] == "NOT_FILLED"]
            filled.sort(key=lambda t: t.get("fill_time") or "")

            kept = []
            breaker_armed = False
            running_pnl = 0.0
            cancelled_cb = []
            for t in filled:
                if breaker_armed:
                    cancelled_cb.append(t)
                    continue
                kept.append(t)
                running_pnl += t["pnl"]
                if (
                    DAILY_STOP_AFTER_SL
                    and t["result"] == "SL"
                    or DAILY_LOCKIN_THRESHOLD > 0
                    and running_pnl >= DAILY_LOCKIN_THRESHOLD
                ):
                    breaker_armed = True

            for t in cancelled_cb:
                t.update(
                    {
                        "result": "NOT_FILLED",
                        "pnl": 0,
                        "fill_time": None,
                        "exit_time": None,
                        "exit": None,
                    }
                )

            trades_out.extend(kept + not_filled + cancelled_cb)

            day_pnl = sum(t["pnl"] for t in kept)
            cum_pnl += day_pnl
            if cum_pnl > peak_pnl:
                peak_pnl = cum_pnl
            if day_pnl < 0:
                consec_loss_days += 1
            elif day_pnl > 0:
                consec_loss_days = 0

    finally:
        # Restaurer config
        cfg.OPR_SL_ATR_MULT[ticker] = sl_orig
        cfg.OPR_TP_ATR_MULT[ticker] = tp_orig
        from core import opr as _opr

        _opr.OPR_SL_ATR_MULT[ticker] = sl_orig
        _opr.OPR_TP_ATR_MULT[ticker] = tp_orig

    return pd.DataFrame(trades_out)


# ══════════════════════════════════════════════════════════════════════════════
# Chart d'une journée (appelé par backtester pour 10 jours aléatoires)
# ══════════════════════════════════════════════════════════════════════════════


def plot_day(
    df_15m: pd.DataFrame,
    ticker: str,
    date_str: str,
    day_trades: list,
    output_path: str,
):
    """Génère le chart OPR d'une journée."""
    from zoneinfo import ZoneInfo

    from core.analysis_chart import plot_day_analysis
    from core.opr import run_opr_day

    tz = ZoneInfo(OPR_TIMEZONE)
    day_ny = pd.Timestamp(date_str).tz_localize(tz)

    _, _, opr_zone = run_opr_day(df_15m, ticker, day_ny)
    cutoff = pd.Timestamp(f"{date_str} {CUTOFF_HOUR_UTC:02d}:00:00")

    # us_end DST-aware
    us_end = day_ny.replace(hour=16, minute=30).tz_convert("UTC").tz_localize(None)

    zones_for_chart = []
    if opr_zone:
        zones_for_chart.append(
            {
                "low": opr_zone["low"],
                "high": opr_zone["high"],
                "mid": opr_zone["mid"],
                "quality": 100.0,
                "n_tf": 1,
                "touches": 1,
                "tfs": ["OPR"],
                "dominant_tf": "OPR",
                "start_time": opr_zone["time_utc"],
            }
        )

    plot_day_analysis(
        df_15m=df_15m,
        ticker=ticker,
        date_str=date_str,
        cutoff=cutoff,
        us_end=us_end,
        zones=zones_for_chart,
        signals=[t for t in day_trades if t.get("result") != "NOT_FILLED"],
        trades=day_trades,
        regime=None,
        alignment_score=None,
        pm_features=None,
        vol_features=None,
        output_path=output_path,
    )
