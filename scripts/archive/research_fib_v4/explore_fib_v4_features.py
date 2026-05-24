"""
Phase 3a — Exploration des features data-driven pour fib-v4.

Collecte les trades de toutes les cellules viables (STRONG + VIABLE issues
de scripts/baseline_fib_v4.py), enrichis des 8 features fib-v4 (cf. scaffold
core/strategy_fib.py Phase 2). Analyse par déciles pour identifier les
candidats filtres.

Sortie :
  • output/fib_v4_trades_all.csv        — tous les trades, concaténés, annotés
  • output/fib_v4_features_analysis.md  — tableau déciles par feature × ticker

Usage :
  python scripts/explore_fib_v4_features.py
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import (
    INSTRUMENTS,
    SLIPPAGE_TICKS_PER_TICKER, COMMISSION_RT_PER_CONTRACT,
    FIB_SL_ATR_MULT_PER_TICKER, FIB_TP_ATR_MULT_PER_TICKER,
    FIB_MIN_IMPULSE_ATR_PER_TICKER,
)
from core.data import load_csv
import core.strategy_fib as sf

DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = PROJECT_ROOT / "output"

FIB_CONSTANTS_M15 = {
    "FIB_ATR_PERIOD":         14,  "FIB_EMA_FAST_PERIOD":    50,
    "FIB_EMA_SLOW_PERIOD":    200, "FIB_ADX_PERIOD":         14,
    "FIB_PIVOT_LEFT":         8,   "FIB_PIVOT_RIGHT":        8,
    "FIB_MAX_IMPULSE_BARS":   25,  "FIB_IMPULSE_LOOKBACK":   60,
    "FIB_ORDER_TIMEOUT_BARS": 12,  "FIB_MAX_HOLD_BARS":      32,
}
FIB_CONSTANTS_M5 = {k: v * 3 for k, v in FIB_CONSTANTS_M15.items()}

FEATURES = [
    "bars_to_fill", "pivot_break_atr", "mae_pending_atr",
    "wick_through_atr", "dist_to_ema_fast_atr", "bar_color_streak_pre",
    "volume_at_arm_norm",
    "bars_since_confirm", "adx_at_arm", "adx_slope_3", "ema_stack_atr",
    "price_extension_atr", "impulse_velocity_atr", "impulse_size_atr",
    "recent_vol_atr", "session_hour_utc",
]
BOOL_FEATURE = "is_macro_day"

N_DECILES = 10
MIN_BUCKET_N = 10  # taille minimale d'un bucket pour qu'il soit informatif


def patch_constants(constants: dict) -> None:
    for name, value in constants.items():
        setattr(sf, name, value)


def cost_rt(ticker: str) -> float:
    slip = SLIPPAGE_TICKS_PER_TICKER.get(ticker, 1)
    tick = INSTRUMENTS[ticker]["tick_size"]
    dpp = INSTRUMENTS[ticker]["dollar_per_point"]
    return 2.0 * slip * tick * dpp + COMMISSION_RT_PER_CONTRACT


def collect_trades(ticker: str, tf: str, fib_level: float) -> pd.DataFrame:
    """Re-exécute le backtest baseline (apply_filter=False) et retourne les
    trades nets-de-frictions, annotés des features fib-v4."""
    csv_path = DATA_DIR / f"{ticker}_data_{tf}.csv"
    df = load_csv(str(csv_path))
    patch_constants(FIB_CONSTANTS_M15 if tf == "m15" else FIB_CONSTANTS_M5)
    trades = sf.run_fib_backtest(
        df, ticker,
        fib_level=fib_level,
        sl_mult=FIB_SL_ATR_MULT_PER_TICKER[ticker],
        tp_mult=FIB_TP_ATR_MULT_PER_TICKER[ticker],
        min_imp=FIB_MIN_IMPULSE_ATR_PER_TICKER[ticker],
        apply_filter=False,
    )
    if len(trades) == 0:
        return trades

    # frictions
    rt = cost_rt(ticker)
    trades = trades.copy()
    trades["pnl_gross"] = trades["pnl"]
    trades["pnl"] = trades["pnl_gross"] - rt * trades["n_ct"]

    trades["ticker_id"] = ticker
    trades["tf_id"] = tf
    trades["fib_level_id"] = fib_level
    trades["cell_id"] = f"{ticker}_{tf}_{fib_level:.3f}"
    trades["win"] = (trades["pnl"] > 0).astype(int)

    return trades


def decile_analysis(df: pd.DataFrame, feat: str) -> pd.DataFrame:
    """Stats par décile pour une feature numérique."""
    if feat not in df.columns:
        return pd.DataFrame()
    vals = df[feat].dropna()
    if len(vals) < MIN_BUCKET_N * 2:
        return pd.DataFrame()

    # qcut peut échouer si peu de valeurs uniques → fallback rangs
    try:
        bins = pd.qcut(df[feat], N_DECILES, duplicates="drop")
    except (ValueError, TypeError):
        return pd.DataFrame()

    rows = []
    for b, sub in df.groupby(bins, observed=True):
        if len(sub) < MIN_BUCKET_N:
            continue
        gross = sub.loc[sub["pnl"] > 0, "pnl"].sum()
        loss = -sub.loc[sub["pnl"] < 0, "pnl"].sum()
        pf = (gross / loss) if loss > 0 else (float("inf") if gross > 0 else float("nan"))
        rows.append(dict(
            feature=feat,
            bucket=str(b),
            low=float(b.left),
            high=float(b.right),
            n=int(len(sub)),
            wr=float(sub["win"].mean()),
            pf=float(pf),
            pnl_mean=float(sub["pnl"].mean()),
            pnl_sum=float(sub["pnl"].sum()),
        ))
    return pd.DataFrame(rows)


def main():
    baseline_csv = OUTPUT_DIR / "fib_v4_baseline.csv"
    if not baseline_csv.exists():
        print(f"Baseline introuvable. Exécute d'abord scripts/baseline_fib_v4.py")
        sys.exit(1)
    baseline = pd.read_csv(baseline_csv)
    viable = baseline[baseline["verdict"].isin(["STRONG", "VIABLE"])].copy()
    print(f"  {len(viable)} cellules viables à analyser :")
    for _, r in viable.iterrows():
        print(f"    • {r['ticker']} {r['tf']} fib={r['fib_level']}  (PF={r['pf']:.2f}, n={int(r['n_filled'])})")

    print(f"\n  Collecte des trades pour chaque cellule...")
    all_trades = []
    for _, r in viable.iterrows():
        trades = collect_trades(r["ticker"], r["tf"], float(r["fib_level"]))
        if len(trades):
            filled = trades[trades["result"].isin(["TP", "SL", "TE"])]
            all_trades.append(filled)
            print(f"    [{r['ticker']:>5} {r['tf']:>3} {r['fib_level']:.3f}] "
                  f"→ {len(filled)} trades filled")

    df_all = pd.concat(all_trades, ignore_index=True)
    print(f"\n  Total : {len(df_all)} trades concaténés sur {len(viable)} cellules")
    print(f"  PnL total net : ${df_all['pnl'].sum():+,.0f}")
    print(f"  WR global : {df_all['win'].mean():.1%}")

    out_csv = OUTPUT_DIR / "fib_v4_trades_all.csv"
    df_all.to_csv(out_csv, index=False)
    print(f"  ✅ Trades sauvegardés : {out_csv}")

    # ── Analyse par décile, GLOBALE et PAR CELLULE ──
    lines = []
    lines.append("# Phase 3a — Analyse univariée des features fib-v4\n")
    lines.append(f"**Population** : {len(df_all)} trades cumulés sur "
                 f"{len(viable)} cellules viables.\n")
    lines.append(f"**Méthodologie** : déciles `pd.qcut`, buckets ≥ {MIN_BUCKET_N} "
                 f"trades. Identifie les zones où PF s'écarte significativement "
                 f"de la médiane → candidats filtres.\n")

    # 1. Décile global, toutes cellules confondues
    lines.append("\n## 1. Analyse globale (toutes cellules cumulées)\n")
    for feat in FEATURES:
        d = decile_analysis(df_all, feat)
        if d.empty:
            continue
        lines.append(f"\n### Feature : `{feat}`\n")
        lines.append("| Bucket | n | WR | PF | PnL moy | PnL sum |")
        lines.append("|---|---|---|---|---|---|")
        for _, b in d.iterrows():
            highlight = ""
            if b["n"] >= 20:
                if b["pf"] < 0.9:
                    highlight = " 🔴"
                elif b["pf"] >= 1.5:
                    highlight = " 🟢"
            lines.append(
                f"| {b['bucket']} | {int(b['n'])} | {b['wr']:.1%} | "
                f"{b['pf']:.2f}{highlight} | ${b['pnl_mean']:+.0f} | "
                f"${b['pnl_sum']:+,.0f} |"
            )

    # 2. is_macro_day binaire
    lines.append("\n\n## 2. Effet `is_macro_day` (binaire)\n")
    if BOOL_FEATURE in df_all.columns:
        for val in [False, True]:
            sub = df_all[df_all[BOOL_FEATURE] == val]
            if len(sub) == 0:
                continue
            gross = sub.loc[sub["pnl"] > 0, "pnl"].sum()
            loss = -sub.loc[sub["pnl"] < 0, "pnl"].sum()
            pf = (gross / loss) if loss > 0 else float("nan")
            lines.append(
                f"- `is_macro_day = {val}` : n={len(sub)}, WR={sub['win'].mean():.1%}, "
                f"PF={pf:.2f}, P&L=${sub['pnl'].sum():+,.0f}"
            )

    # 3. Décile par cellule (pour les cellules majeures uniquement)
    lines.append("\n\n## 3. Analyse par cellule (top features candidates)\n")
    top_features = ["pivot_break_atr", "bars_to_fill", "mae_pending_atr",
                    "wick_through_atr", "adx_at_arm", "volume_at_arm_norm",
                    "dist_to_ema_fast_atr"]
    for cell_id, sub in df_all.groupby("cell_id"):
        if len(sub) < 50:
            continue
        lines.append(f"\n### Cellule : `{cell_id}` ({len(sub)} trades, "
                     f"PF global = {(sub.loc[sub['pnl']>0,'pnl'].sum() / max(1, -sub.loc[sub['pnl']<0,'pnl'].sum())):.2f})\n")
        for feat in top_features:
            d = decile_analysis(sub, feat)
            if d.empty:
                continue
            # Récap : extrême bas et extrême haut
            d_sorted = d.sort_values("low")
            low_b = d_sorted.iloc[0]
            high_b = d_sorted.iloc[-1]
            # Identifier le décile le plus mauvais et le meilleur
            worst = d.sort_values("pf").iloc[0]
            best  = d.sort_values("pf").iloc[-1]
            lines.append(
                f"- `{feat}` : extrême bas [{low_b['low']:.2f}, {low_b['high']:.2f}] "
                f"→ PF={low_b['pf']:.2f} (n={int(low_b['n'])}) | "
                f"extrême haut [{high_b['low']:.2f}, {high_b['high']:.2f}] "
                f"→ PF={high_b['pf']:.2f} (n={int(high_b['n'])}) | "
                f"meilleur bucket [{best['low']:.2f}, {best['high']:.2f}] "
                f"PF={best['pf']:.2f} | pire bucket "
                f"[{worst['low']:.2f}, {worst['high']:.2f}] PF={worst['pf']:.2f}"
            )

    # 4. Top hypothèses (auto-extraction des plus gros gaps PF)
    lines.append("\n\n## 4. Top hypothèses de filtres (extrêmes globaux)\n")
    lines.append("Filtres candidats où un bucket extrême a `n ≥ 30` et PF "
                 "très éloigné de la médiane.\n")
    lines.append("| Feature | Bucket | n | PF | Hypothèse |")
    lines.append("|---|---|---|---|---|")
    for feat in FEATURES:
        d = decile_analysis(df_all, feat)
        if d.empty:
            continue
        for _, b in d.iterrows():
            if b["n"] >= 30:
                if b["pf"] < 0.85:
                    lines.append(
                        f"| `{feat}` | [{b['low']:.3f}, {b['high']:.3f}] "
                        f"| {int(b['n'])} | {b['pf']:.2f} "
                        f"| 🔴 REJETER cette zone |"
                    )
                elif b["pf"] >= 1.8:
                    lines.append(
                        f"| `{feat}` | [{b['low']:.3f}, {b['high']:.3f}] "
                        f"| {int(b['n'])} | {b['pf']:.2f} "
                        f"| 🟢 CIBLER cette zone |"
                    )

    out_md = OUTPUT_DIR / "fib_v4_features_analysis.md"
    out_md.write_text("\n".join(lines))
    print(f"  ✅ Analyse markdown : {out_md}")


if __name__ == "__main__":
    main()
