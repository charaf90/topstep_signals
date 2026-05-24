#!/usr/bin/env python3
"""
Comparaison directe M15 vs M5 sur la MÊME période (depuis le 2025-10-19,
date à laquelle on a des données M5 complètes).

Output : delta de fidélité M5 vs M15 sur exactement les mêmes trades.
But : trancher si Phase A (granularité plus fine) est utile pour v5.1.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

np.random.seed(42)

from config import OPR_TIMEZONE, OPR_V5_1_F2_MIN_ATR
from core.data import build_timeframes, load_csv
from core.opr import OPR_ATR_PERIOD, _compute_atr_daily
from strategies import opr_v5_1 as v51

SELECTED_TICKERS = ["NQ1", "YM1"]
# M5 data commence à cette date (cf. data/NQ1_data_m5.csv)
M5_DATA_START = "2025-10-19"


def compute_f2_pre_fill(
    df_ny, trigger_ts, fill_ts, direction, opr_high, opr_low, atr_daily, exclude_fill_bar=True
):
    """F2 pre-fill avec un floor configurable (15min vs 5min)."""
    if atr_daily is None or atr_daily <= 0:
        return 0.0

    if exclude_fill_bar:
        # Bougie de fill exclue : on borne strictement < fill_ts
        upper = fill_ts
    else:
        upper = fill_ts  # même chose pour simplicité

    bars = df_ny[(df_ny.index >= trigger_ts) & (df_ny.index < upper)]
    if len(bars) == 0:
        return 0.0
    if direction == "long":
        excursion = max(float(bars["high"].max()) - opr_high, 0.0)
    else:
        excursion = max(opr_low - float(bars["low"].min()), 0.0)
    return float(excursion / atr_daily)


def main():
    tz = ZoneInfo(OPR_TIMEZONE)

    print("=" * 70)
    print(f"  COMPARAISON M15 vs M5 — opr-v5.1 sur trades depuis {M5_DATA_START}")
    print("=" * 70)

    for ticker in SELECTED_TICKERS:
        df_15m = load_csv(f"data/{ticker}_data_m15.csv")
        df_m5 = load_csv(f"data/{ticker}_data_m5.csv")
        tf = build_timeframes(df_15m)

        # Localiser en NY
        df_15m_ny = df_15m.copy()
        df_15m_ny.index = df_15m_ny.index.tz_localize("UTC").tz_convert(tz)
        df_m5_ny = df_m5.copy()
        df_m5_ny.index = df_m5_ny.index.tz_localize("UTC").tz_convert(tz)

        # Backtest v5.1 (donne tous les trades, fillés et NOT_FILLED)
        trades = v51.run_backtest(df_15m, ticker, tf=tf, params=None, topstep_guard=False)
        # Restreindre à la fenêtre où M5 existe
        trades = trades[trades["date"] >= M5_DATA_START]
        filled = trades[trades["result"] != "NOT_FILLED"].copy().reset_index(drop=True)

        if len(filled) == 0:
            print(f"\n  {ticker}: aucun trade filled dans la fenêtre — skip")
            continue

        threshold = OPR_V5_1_F2_MIN_ATR.get(ticker)
        n_rejected_m15 = 0
        n_rejected_m5 = 0

        for _, row in filled.iterrows():
            trigger_ts = pd.Timestamp(row["trigger_time"])
            fill_ts = pd.Timestamp(row["fill_time"])
            if trigger_ts.tz is None:
                trigger_ts = trigger_ts.tz_localize("UTC").tz_convert(tz)
            else:
                trigger_ts = trigger_ts.tz_convert(tz)
            if fill_ts.tz is None:
                fill_ts = fill_ts.tz_localize("UTC").tz_convert(tz)
            else:
                fill_ts = fill_ts.tz_convert(tz)

            opr_h = float(row["zone_high"])
            opr_l = float(row["zone_low"])
            day_ny = pd.Timestamp(row["date"]).tz_localize(tz)
            atr_d = _compute_atr_daily(df_15m, day_ny, OPR_ATR_PERIOD)

            # F2 M15 pre-fill (excludes M15 bar du fill)
            f2_m15 = compute_f2_pre_fill(
                df_15m_ny, trigger_ts, fill_ts, row["dir"], opr_h, opr_l, atr_d
            )
            # F2 M5 pre-fill (exclut seulement les 5 dernières minutes)
            f2_m5 = compute_f2_pre_fill(
                df_m5_ny, trigger_ts, fill_ts, row["dir"], opr_h, opr_l, atr_d
            )

            if f2_m15 < threshold:
                n_rejected_m15 += 1
            if f2_m5 < threshold:
                n_rejected_m5 += 1

        n = len(filled)
        fid_m15 = (n - n_rejected_m15) / n * 100
        fid_m5 = (n - n_rejected_m5) / n * 100
        print(f"\n  {ticker} — {n} trades fillés (post-fill)")
        print(
            f"    M15 fidélité : {n - n_rejected_m15}/{n}  =  {fid_m15:.1f} %   (rejected {n_rejected_m15})"
        )
        print(
            f"    M5  fidélité : {n - n_rejected_m5}/{n}  =  {fid_m5:.1f} %   (rejected {n_rejected_m5})"
        )
        print(f"    Δ fidélité   : {fid_m5 - fid_m15:+.1f} pp")
        print(f"    Trades sauvés par M5 vs M15 : {n_rejected_m15 - n_rejected_m5}")


if __name__ == "__main__":
    main()
