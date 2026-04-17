#!/usr/bin/env python3
"""
Calibration Phase C uniquement (composite score min + trend strength min
par actif) en walk-forward IS/OOS.

Les paramètres globaux et par-actif (Phase A/B) sont déjà calibrés en v4/v5.
"""
import sys
from pathlib import Path

from optimize import (
    INSTRUMENTS, TRAIN_END, TEST_START,
    load_csv, build_timeframes, precompute_trends,
    optimize_composite_per_asset, update_config,
)


def main():
    csv_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("./data")

    data = {}
    trend_cache = {}
    for ticker in INSTRUMENTS:
        df = load_csv(str(csv_dir / f"{ticker}_data_m15.csv"))
        tf = build_timeframes(df)
        data[ticker] = (df, tf)
        trend_cache[ticker] = precompute_trends(tf)
        print(f"  {ticker}: {df.index.min().date()} → {df.index.max().date()}")

    best_composite, ym1_keep = optimize_composite_per_asset(
        data, trend_cache, "2024-12-01", TRAIN_END, TEST_START
    )

    print("\n=== RÉCAPITULATIF PHASE C ===")
    for t, p in best_composite.items():
        print(f"  {t}: score_min={p['score_min']}  trend_strength={p['trend_strength']}"
              f"  — IS PF={p['is_pf']:.2f} (${p['is_pnl']:+,.0f})"
              f"  OOS PF={p['oos_pf']:.2f} (${p['oos_pnl']:+,.0f})")
    print(f"  YM1_ENABLED = {ym1_keep}")

    # Écrire dans config.py — seuls les champs composite
    # On doit passer des asset_p minimaux pour satisfaire la signature
    from optimize import _DEFAULTS_ASSET, _DEFAULTS_GLOBAL
    update_config(_DEFAULTS_GLOBAL, _DEFAULTS_ASSET, best_composite, ym1_keep)
    print("\n  ✅ config.py mis à jour (COMPOSITE_SCORE_MIN, TREND_STRENGTH_MIN, YM1_ENABLED)")


if __name__ == "__main__":
    main()
