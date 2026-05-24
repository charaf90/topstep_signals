#!/usr/bin/env python3
"""
Live-equivalence backtest v5.1 avec granularité M5 (au lieu de M15).

Motivation :
  Le live-eq M15 donne 74 % de fidélité (cf scripts/live_eq_v5_1.py).
  La cause : F2 atteint son seuil DANS la bougie M15 de fill dans 26 % des
  cas — impossible à distinguer "avant fill" vs "après fill" en M15 pur.

  Avec du M5, la bougie M15 de fill est décomposée en 3 sous-bougies M5.
  On peut savoir si F2 cross dans M5 #1, #2 ou #3, et si le fill a eu lieu
  dans M5 #1, #2 ou #3 → décision "F2 cross avant fill" mieux résolue.

Logique :
  Pour chaque trade FILLED dans le backtest v5.1 (post-fill) :
    1. Identifier la bougie M5 où le fill a probablement eu lieu (low/high
       intra-M5 touche le LIMIT)
    2. F2_M5_pre = max excursion sur [trigger_ts, last_M5_before_fill_M5]
       en utilisant les bars M5 (et non les bars M15)
    3. Si F2_M5_pre < seuil → trade REJETÉ en live (LIMIT cancellé via
       schéma A : on ne place le LIMIT que quand F2 ≥ seuil)
    4. Sinon → trade pris

Estimation a priori : fidélité M5 ≈ 82-85 %.

Usage :
    python -m scripts.live_eq_v5_1_m5 --csv-dir ./data
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

np.random.seed(42)

from config import (
    COMMISSION_RT_PER_CONTRACT,
    OPR_TIMEZONE,
    OPR_V5_1_F2_MIN_ATR,
    SLIPPAGE_TICKS_PER_TICKER,
)
from core.data import load_csv
from core.metrics import compute_stats
from core.opr import OPR_ATR_PERIOD, _compute_atr_daily
from core.optimizer import OOS_START
from strategies import opr_v5_1 as v51

SELECTED_TICKERS = ["NQ1", "YM1"]
DOLLARS_PER_TICK = {"MES1": 1.25, "NQ1": 0.50, "YM1": 0.50}


# ─────────────────────────────────────────────────────────────────────────────
# Recalcul F2 pre-fill sur M5 (au lieu de M15)
# ─────────────────────────────────────────────────────────────────────────────


def compute_f2_pre_fill_m5(
    df_m5_ny: pd.DataFrame,
    trigger_ts: pd.Timestamp,
    fill_ts: pd.Timestamp,
    direction: str,
    opr_high: float,
    opr_low: float,
    atr_daily: float,
) -> float:
    """
    F2_live (M5) = max excursion sur les bougies M5 strictement AVANT la
    bougie M5 qui contient fill_ts. Plus fine que la version M15.

    Exclusion : on retire les bougies M5 dont l'intervalle [t, t+5min)
    contient fill_ts (i.e. la bougie M5 du fill et toutes celles après).
    """
    if atr_daily is None or atr_daily <= 0:
        return 0.0

    # Bougie M5 contenant fill_ts : la bougie dont [t, t+5min) inclut fill_ts.
    # On exclut donc toutes les bougies M5 dont t >= fill_M5_start.
    # Approche simple : bougie de fill = floor(fill_ts à 5 min)
    fill_m5_start = fill_ts.floor("5min")

    bars = df_m5_ny[(df_m5_ny.index >= trigger_ts) & (df_m5_ny.index < fill_m5_start)]

    if len(bars) == 0:
        return 0.0

    if direction == "long":
        excursion_pts = max(float(bars["high"].max()) - opr_high, 0.0)
    else:
        excursion_pts = max(opr_low - float(bars["low"].min()), 0.0)

    return float(excursion_pts / atr_daily)


# ─────────────────────────────────────────────────────────────────────────────
# Friction (identique au script M15)
# ─────────────────────────────────────────────────────────────────────────────


def friction_per_trade(ticker: str, n_ct: int) -> float:
    slip_ticks = SLIPPAGE_TICKS_PER_TICKER.get(ticker, 1)
    dollars_per_tick = DOLLARS_PER_TICK.get(ticker, 0.50)
    slip_cost = slip_ticks * dollars_per_tick * 2 * n_ct
    comm_cost = COMMISSION_RT_PER_CONTRACT * n_ct
    return slip_cost + comm_cost


def add_net_pnl(trades: pd.DataFrame) -> pd.DataFrame:
    out = trades.copy()
    if len(out) == 0:
        out["pnl_net"] = pd.Series(dtype=float)
        return out
    out["friction"] = out.apply(
        lambda r: (
            friction_per_trade(r.get("ticker", "?"), int(r.get("n_ct", 1) or 1))
            if r.get("result", "NOT_FILLED") != "NOT_FILLED"
            else 0.0
        ),
        axis=1,
    )
    out["pnl_net"] = out["pnl"] - out["friction"]
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline principal
# ─────────────────────────────────────────────────────────────────────────────


def run_live_eq_m5(csv_dir: str, oos_start: str = OOS_START) -> dict:
    tz = ZoneInfo(OPR_TIMEZONE)
    results = {}

    for ticker in SELECTED_TICKERS:
        # M15 pour le backtest baseline (v5.1 post-fill)
        csv_m15 = Path(csv_dir) / f"{ticker}_data_m15.csv"
        if not csv_m15.exists():
            print(f"  [!] {csv_m15} introuvable — skip {ticker}")
            continue

        df_15m = load_csv(str(csv_m15))
        # tf pour run_backtest — pas vraiment utilisé par v5_1 mais requis
        from core.data import build_timeframes

        tf = build_timeframes(df_15m)

        # M5 pour le recalcul F2 fine
        csv_m5 = Path(csv_dir) / f"{ticker}_data_m5.csv"
        if not csv_m5.exists():
            print(f"  [!] {csv_m5} introuvable — skip {ticker}")
            continue
        df_m5 = load_csv(str(csv_m5))

        # Localiser df_m5 en NY tz
        if df_m5.index.tz is None:
            df_m5_ny = df_m5.copy()
            df_m5_ny.index = df_m5_ny.index.tz_localize("UTC").tz_convert(tz)
        else:
            df_m5_ny = df_m5.copy()
            df_m5_ny.index = df_m5_ny.index.tz_convert(tz)

        # 1. Run v5.1 backtest (post-fill, params optima config)
        trades_post = v51.run_backtest(df_15m, ticker, tf=tf, params=None, topstep_guard=False)
        trades_post["ticker"] = ticker
        trades_post_oos = trades_post[trades_post["date"] >= oos_start].copy()
        trades_post_oos = trades_post_oos.reset_index(drop=True)

        # 2. Pour chaque trade FILLED, recalculer F2 pre-fill avec M5
        f2_min_threshold = OPR_V5_1_F2_MIN_ATR.get(ticker)

        trades_live = trades_post_oos.copy()
        trades_live["f2_pre_fill_m5_atr"] = np.nan
        trades_live["live_rejected_m5"] = False

        if f2_min_threshold is not None:
            for idx, row in trades_post_oos.iterrows():
                if row["result"] == "NOT_FILLED":
                    continue

                try:
                    trigger_ts = pd.Timestamp(row["trigger_time"])
                    fill_ts = pd.Timestamp(row["fill_time"])
                except Exception:
                    continue

                # Convertir en NY tz
                if trigger_ts.tz is None:
                    trigger_ts = trigger_ts.tz_localize("UTC").tz_convert(tz)
                else:
                    trigger_ts = trigger_ts.tz_convert(tz)
                if fill_ts.tz is None:
                    fill_ts = fill_ts.tz_localize("UTC").tz_convert(tz)
                else:
                    fill_ts = fill_ts.tz_convert(tz)

                opr_high = float(row["zone_high"])
                opr_low = float(row["zone_low"])
                direction = row["dir"]

                day_ny = pd.Timestamp(row["date"]).tz_localize(tz)
                atr_daily = _compute_atr_daily(df_15m, day_ny, OPR_ATR_PERIOD)

                f2_pre_m5 = compute_f2_pre_fill_m5(
                    df_m5_ny=df_m5_ny,
                    trigger_ts=trigger_ts,
                    fill_ts=fill_ts,
                    direction=direction,
                    opr_high=opr_high,
                    opr_low=opr_low,
                    atr_daily=atr_daily if atr_daily is not None else 0.0,
                )
                trades_live.at[idx, "f2_pre_fill_m5_atr"] = f2_pre_m5

                if f2_pre_m5 < f2_min_threshold:
                    trades_live.at[idx, "live_rejected_m5"] = True
                    trades_live.at[idx, "result"] = "NOT_FILLED"
                    trades_live.at[idx, "pnl"] = 0.0
                    trades_live.at[idx, "v5_reject_reason"] = "F2_live_m5_too_narrow"
                    trades_live.at[idx, "fill_time"] = None

        results[ticker] = {
            "trades_post": trades_post_oos,
            "trades_live": trades_live,
            "f2_min_threshold": f2_min_threshold,
        }

    return results


def fmt_money(x):
    return f"${x:+,.0f}"


def print_report(results: dict, output_dir: str):
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    md_lines = [
        "# Live-equivalence M5 — opr-v5.1 (NQ1+YM1)",
        "",
        "Schéma A (entrée différée) avec granularité M5 (3× plus fine que M15).",
        "",
        "| Ticker | Post-fill FILLED | Live-eq M5 FILLED | Trades perdus | Fidélité M5 |",
        "|---|---:|---:|---:|---:|",
    ]

    total_post = 0
    total_live = 0
    total_pnl_post = 0.0
    total_pnl_live = 0.0
    total_pnl_post_net = 0.0
    total_pnl_live_net = 0.0
    all_post, all_live = [], []

    for ticker, r in results.items():
        post = r["trades_post"]
        live = r["trades_live"]
        post_f = post[post["result"] != "NOT_FILLED"]
        live_f = live[live["result"] != "NOT_FILLED"]
        n_p, n_l = len(post_f), len(live_f)
        fid = (n_l / n_p * 100) if n_p > 0 else float("nan")
        md_lines.append(f"| {ticker} | {n_p} | {n_l} | {n_p - n_l} | {fid:.1f} % |")
        total_post += n_p
        total_live += n_l
        total_pnl_post += float(post_f["pnl"].sum())
        total_pnl_live += float(live_f["pnl"].sum())
        post_net = add_net_pnl(post)
        live_net = add_net_pnl(live)
        total_pnl_post_net += float(post_net["pnl_net"].sum())
        total_pnl_live_net += float(live_net["pnl_net"].sum())
        all_post.append(post)
        all_live.append(live)

    fid_total = (total_live / total_post * 100) if total_post > 0 else float("nan")
    md_lines.append(
        f"| **Portfolio** | **{total_post}** | **{total_live}** | "
        f"**{total_post - total_live}** | **{fid_total:.1f} %** |"
    )

    df_post_all = pd.concat(all_post, ignore_index=True) if all_post else pd.DataFrame()
    df_live_all = pd.concat(all_live, ignore_index=True) if all_live else pd.DataFrame()
    stats_post = compute_stats(df_post_all)
    stats_live = compute_stats(df_live_all)

    md_lines += [
        "",
        "## Comparaison M15 vs M5",
        "",
        "| Métrique | M15 (existant) | **M5 (nouveau)** | M15 backtest |",
        "|---|---:|---:|---:|",
        f"| Fidélité | 74.3 % | **{fid_total:.1f} %** | — |",
        f"| PF brut | 2.11 | **{stats_live.get('pf', float('nan')):.2f}** | 2.50 |",
        f"| P&L brut | $+6,238 | **{fmt_money(total_pnl_live)}** | $+10,428 |",
        f"| P&L NET | $+5,061 | **{fmt_money(total_pnl_live_net)}** | $+8,825 |",
    ]

    # Save
    md_path = out_path / "live_eq_v5_1_m5.md"
    md_path.write_text("\n".join(md_lines), encoding="utf-8")
    print("\n".join(md_lines))
    print(f"\n  ✓ Rapport : {md_path}")

    summary = {
        "granularity": "M5",
        "fidelity_pct": fid_total,
        "trades_post": total_post,
        "trades_live": total_live,
        "pnl_brut_post": total_pnl_post,
        "pnl_brut_live": total_pnl_live,
        "pnl_net_post": total_pnl_post_net,
        "pnl_net_live": total_pnl_live_net,
        "pf_post": stats_post.get("pf", float("nan")),
        "pf_live": stats_live.get("pf", float("nan")),
    }
    (out_path / "live_eq_v5_1_m5.json").write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv-dir", type=str, required=True)
    parser.add_argument("--oos-start", type=str, default=OOS_START)
    parser.add_argument("--output-dir", type=str, default="output/no_mes1")
    args = parser.parse_args()

    print("=" * 70)
    print("  LIVE-EQUIVALENCE M5 — opr-v5.1 (NQ1+YM1)")
    print("  Schéma A (entrée différée) avec data M5 (granularité 5 min)")
    print("=" * 70)

    results = run_live_eq_m5(args.csv_dir, args.oos_start)
    print_report(results, args.output_dir)


if __name__ == "__main__":
    main()
