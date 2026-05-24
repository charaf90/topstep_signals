"""
Stress tests OPR v5.1 (PHASE 5 du pipeline).

Calcule la performance de v5.1 (params optimaux config) sur les sous-segments :
  • Trending : ADX(14) > 25 sur les 4 dernières barres avant entrée
  • Ranging  : ADX(14) < 20
  • Macro    : jours figurant dans MACRO_EVENT_DATES
  • Vol haute / basse : ATR daily percentile > 75 / < 25 (rolling 30 jours)

Sortie : output/stress_opr_v5_1.md
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

np.random.seed(42)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.data import load_csv, build_timeframes
from strategies import opr_v5_1
from config import MACRO_EVENT_DATES

TICKERS = ["MES1", "NQ1", "YM1"]


def _compute_adx_at_entry(df_15m: pd.DataFrame, ts_str: str, period: int = 14) -> float:
    """ADX (Wilder) à l'instant ts_str. Retourne NaN si insuffisant."""
    if not ts_str:
        return np.nan
    try:
        ts = pd.Timestamp(ts_str)
    except Exception:
        return np.nan
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    df = df_15m[df_15m.index <= ts].copy()
    if len(df) < period * 5:
        return np.nan
    df = df.tail(period * 10).copy()
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(),
                    (low - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1.0 / period, adjust=False).mean()
    up_move = high.diff()
    down_move = low.shift(1) - low
    plus_dm = ((up_move > down_move) & (up_move > 0)) * up_move
    minus_dm = ((down_move > up_move) & (down_move > 0)) * down_move
    plus_di = 100.0 * plus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / atr.replace(0, np.nan)
    minus_di = 100.0 * minus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / atr.replace(0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(alpha=1.0 / period, adjust=False).mean()
    val = adx.iloc[-1] if len(adx) > 0 else np.nan
    return float(val) if not pd.isna(val) else np.nan


def _atr_pct(df_15m: pd.DataFrame, ts_str: str, period: int = 14, window_days: int = 30) -> float:
    """Percentile rang de l'ATR daily à l'instant donné (sur window_days précédents)."""
    if not ts_str:
        return np.nan
    try:
        ts = pd.Timestamp(ts_str)
    except Exception:
        return np.nan
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    before = df_15m[df_15m.index < ts]
    if before.empty:
        return np.nan
    daily = before.resample("D").agg({"open": "first", "high": "max",
                                       "low": "min", "close": "last"}).dropna()
    if len(daily) < period + 5:
        return np.nan
    high, low, close = daily["high"], daily["low"], daily["close"]
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(),
                    (low - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.rolling(period).mean().dropna()
    if len(atr) < window_days:
        return np.nan
    window = atr.tail(window_days)
    current = atr.iloc[-1]
    pct = float((window <= current).sum()) / float(len(window))
    return pct


def _pf(df: pd.DataFrame) -> float:
    f = df[df["result"] != "NOT_FILLED"]
    if f.empty:
        return np.nan
    wins = f[f["pnl"] > 0]["pnl"].sum()
    losses = f[f["pnl"] < 0]["pnl"].sum()
    return float(wins / abs(losses)) if losses < 0 else (np.inf if wins > 0 else np.nan)


def _stats(df: pd.DataFrame) -> dict:
    f = df[df["result"] != "NOT_FILLED"]
    n = len(f)
    pnl = float(f["pnl"].sum())
    pf = _pf(df)
    wr = 100.0 * (f["pnl"] > 0).sum() / n if n > 0 else np.nan
    return {"n": n, "PF": round(pf, 2) if np.isfinite(pf) else "inf",
            "pnl": round(pnl, 2),
            "WR_pct": round(wr, 1) if np.isfinite(wr) else np.nan}


def _monte_carlo_dd(pnl_series: pd.Series, n_perms: int = 1000, seed: int = 42) -> dict:
    """Permutation Monte Carlo : permute l'ordre des trades, calcule la distribution DD max."""
    np.random.seed(seed)
    pnl_arr = pnl_series.to_numpy()
    dds = []
    for _ in range(n_perms):
        perm = np.random.permutation(pnl_arr)
        cum = np.cumsum(perm)
        peak = np.maximum.accumulate(cum)
        dd_max = float((cum - peak).min())
        dds.append(dd_max)
    dds = np.array(dds)
    return {
        "n_perms": n_perms,
        "DD_median":  float(np.percentile(dds, 50)),
        "DD_P95":     float(np.percentile(dds, 5)),   # 5e percentile sur DD négatif = P95 sur queue
        "DD_P99":     float(np.percentile(dds, 1)),
        "DD_worst":   float(dds.min()),
    }


def main() -> int:
    print("=" * 70)
    print("  STRESS TESTS opr-v5.1 (params optimaux)")
    print("=" * 70)

    all_trades = []
    raw_data = {}
    for ticker in TICKERS:
        csv = PROJECT_ROOT / "data" / f"{ticker}_data_m15.csv"
        df = load_csv(str(csv))
        raw_data[ticker] = df
        tf = build_timeframes(df)
        print(f"\n  → {ticker}...")
        t51 = opr_v5_1.run_backtest(df, ticker, tf=tf, params=None, topstep_guard=False)
        t51["ticker"] = ticker
        all_trades.append(t51)

    df_all = pd.concat(all_trades, ignore_index=True)
    df_filled = df_all[df_all["result"] != "NOT_FILLED"].copy()

    df_oos = df_filled[pd.to_datetime(df_filled["date"]) >= "2025-10-01"].copy()
    print(f"\n  OOS portfolio : {len(df_oos)} trades filled")

    print("  → Calcul ADX et ATR pct par trade...")
    adx_vals = []
    atr_pcts = []
    for _, row in df_oos.iterrows():
        ticker = row["ticker"]
        df_t = raw_data[ticker]
        ts = row.get("trigger_time") or row.get("fill_time")
        adx_vals.append(_compute_adx_at_entry(df_t, ts))
        atr_pcts.append(_atr_pct(df_t, ts))
    df_oos = df_oos.copy()
    df_oos["adx_at_entry"] = adx_vals
    df_oos["atr_pct"] = atr_pcts

    macro_set = set(MACRO_EVENT_DATES)
    df_oos["is_macro"] = df_oos["date"].apply(lambda d: d in macro_set)

    rows = ["# Stress tests opr-v5.1 (OOS portfolio — oct 2025 → mai 2026)"]
    rows.append("")
    rows.append("Régimes calculés au moment du trigger : ADX(14) M15, ATR daily percentile rolling 30j.")
    rows.append("Cible (SKILL.md PHASE 5a) : PF ≥ 1.0 sur chaque régime, PF ≥ 1.3 sur ≥ 2 régimes.")
    rows.append("")
    rows.append("| Régime | Définition | n | PF | P&L | WR % | Verdict |")
    rows.append("|---|---|---:|---:|---:|---:|:---:|")

    def _row(label: str, definition: str, sub: pd.DataFrame):
        s = _stats(sub)
        pf = s["PF"]
        pf_num = float(pf) if isinstance(pf, (int, float)) and np.isfinite(pf) else np.nan
        verdict = "🟢" if pf_num >= 1.3 else ("🟡" if pf_num >= 1.0 else "🔴")
        if not np.isfinite(pf_num):
            verdict = "—"
        print(f"  {label:<14}  n={s['n']:>3}  PF={pf}  P&L={s['pnl']:+8.2f}  WR={s['WR_pct']}%  {verdict}")
        rows.append(f"| {label} | {definition} | {s['n']} | {pf} | "
                    f"{s['pnl']:+.2f} | {s['WR_pct']} | {verdict} |")

    print("\n  Décomposition par régime OOS :")
    _row("Trending",   "ADX > 25 au trigger",
         df_oos[df_oos["adx_at_entry"] > 25])
    _row("Ranging",    "ADX < 20 au trigger",
         df_oos[df_oos["adx_at_entry"] < 20])
    _row("Vol haute",  "ATR daily pct > 0.75",
         df_oos[df_oos["atr_pct"] > 0.75])
    _row("Vol basse",  "ATR daily pct < 0.25",
         df_oos[df_oos["atr_pct"] < 0.25])
    _row("Macro day",  "Date ∈ MACRO_EVENT_DATES",
         df_oos[df_oos["is_macro"]])
    _row("Non-macro",  "Date ∉ MACRO_EVENT_DATES",
         df_oos[~df_oos["is_macro"]])

    # ── Monte Carlo permutation DD ──
    rows.append("")
    rows.append("## Monte Carlo permutation du DD (OOS, 1 000 itérations)")
    rows.append("")
    if not df_oos.empty:
        # Ordonner par date pour cohérence (on permute l'ordre pas la composition)
        pnl_series = df_oos.sort_values("date")["pnl"]
        mc = _monte_carlo_dd(pnl_series, n_perms=1000)
        rows.append(f"- DD médian   : ${mc['DD_median']:.2f}")
        rows.append(f"- DD P95      : ${mc['DD_P95']:.2f}")
        rows.append(f"- DD P99      : ${mc['DD_P99']:.2f}")
        rows.append(f"- DD pire-cas : ${mc['DD_worst']:.2f}")
        print("\n  Monte Carlo DD permutation OOS :")
        print(f"    DD médian = ${mc['DD_median']:.0f} | DD P95 = ${mc['DD_P95']:.0f} "
              f"| DD P99 = ${mc['DD_P99']:.0f} | DD worst = ${mc['DD_worst']:.0f}")
    else:
        rows.append("- pas de données OOS")

    # ── Worst-case clustering ──
    rows.append("")
    rows.append("## Worst-case clustering (top 20 pires trades OOS)")
    rows.append("")
    if len(df_oos) >= 20:
        worst = df_oos.nsmallest(20, "pnl")
        cum_loss = float(worst["pnl"].sum())
        rows.append(f"- Perte cumulée top 20 : ${cum_loss:.2f}")
        # Concentration ticker
        rows.append("")
        rows.append("Par ticker :")
        rows.append("")
        rows.append("| Ticker | n_worst | %_du_top20 |")
        rows.append("|---|---:|---:|")
        for tk, c in worst["ticker"].value_counts().items():
            rows.append(f"| {tk} | {int(c)} | {100*c/20:.0f}% |")
        # Concentration temps (jours uniques)
        rows.append("")
        rows.append(f"Jours distincts dans top 20 : {worst['date'].nunique()}")
    else:
        rows.append("(n < 20 — non calculable)")

    # ── Comparaison v4 vs v5 vs v5.1 ──
    rows.append("")
    rows.append("## Comparaison directe par régime")
    rows.append("")
    rows.append("Référence : voir output/compare_v4_v5_v5_1.md pour les métriques portfolio brutes.")

    out = PROJECT_ROOT / "output" / "stress_opr_v5_1.md"
    out.write_text("\n".join(rows))
    print(f"\n  ✓ {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
