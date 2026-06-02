#!/usr/bin/env python3
"""
Replay risque PORTEFEUILLE — impact de l'ajout de fib-fine-v2 au book live.

Question (go/no-go) : ajouter fib-fine-v2 (NQ1+MES1, sizing $130) au portefeuille
live actuel (OPR v5.1 NQ1/YM1 + fib-v4 NQ1/MES1/MGC1) fait-il breacher les limites
Topstep partagées ($1000 daily, $2000 trailing) ou bloque-t-il massivement des trades ?

Méthodo :
  1. Charge les trades remplis de chaque stratégie sur la fenêtre OOS commune.
  2. Rejoue chronologiquement à travers PortfolioRiskManager (l'état GLOBAL live).
  3. Compare 3 scénarios :
       BASELINE   = book live actuel (OPR + fib-v4)
       +FINE_CORE = baseline + fib-fine NQ1+MES1 ($130)
       +FINE_ALL  = baseline + fib-fine NQ1+MES1+MGC1 ($130)
  4. Métriques : P&L réalisé, max trailing DD (trade-level), pire daily loss,
     breaches Topstep, trades bloqués par le garde-fou (et par quelle stratégie).

Lecture seule sur les CSV — n'écrit qu'un rapport dans output/.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from config import INSTRUMENTS
from core.risk_portfolio import PortfolioRiskManager

WINDOW_START = pd.Timestamp("2025-10-01")
WINDOW_END = pd.Timestamp("2026-02-27 23:59:59")

# (label, path, strategy, ticker, groupe)  groupe ∈ {baseline, fine_core, fine_extra}
SOURCES = [
    ("OPR NQ1 v5.1", "output/backtest_NQ1_opr_v5_1.csv", "opr", "NQ1", "baseline"),
    ("OPR YM1 v5.1", "output/backtest_YM1_opr_v5_1.csv", "opr", "YM1", "baseline"),
    ("fib-v4 NQ1", "output/backtest_NQ1_fib_v4.csv", "fib", "NQ1", "baseline"),
    ("fib-v4 MES1", "output/backtest_MES1_fib_v4.csv", "fib", "MES1", "baseline"),
    ("fib-v4 MGC1", "output/backtest_MGC1_fib_v4.csv", "fib", "MGC1", "baseline"),
    (
        "fib-fine NQ1",
        "output/fib-fine-v2/full/trades/trades_NQ1_proposed.csv",
        "fine",
        "NQ1",
        "fine_core",
    ),
    (
        "fib-fine MES1",
        "output/fib-fine-v2/full/trades/trades_MES1_proposed.csv",
        "fine",
        "MES1",
        "fine_core",
    ),
    (
        "fib-fine MGC1",
        "output/fib-fine-v2/full/trades/trades_MGC1_proposed.csv",
        "fine",
        "MGC1",
        "fine_extra",
    ),
]


def _risk_usd(df: pd.DataFrame, ticker: str) -> pd.Series:
    """risk dollar du trade : colonne explicite sinon n_ct × sl_dist × $/pt."""
    if "risk_$" in df.columns:
        return df["risk_$"].astype(float)
    if "risk" in df.columns:
        return df["risk"].astype(float)
    dpp = INSTRUMENTS[ticker]["dollar_per_point"]
    return (df["n_ct"].astype(float) * df["sl_dist"].astype(float) * dpp).abs()


def load_source(label, path, strategy, ticker, groupe) -> pd.DataFrame:
    p = Path(path)
    if not p.exists():
        print(f"  [MANQUE] {label}: {path}")
        return pd.DataFrame()
    df = pd.read_csv(p)
    if "result" in df.columns:
        df = df[df["result"] != "NOT_FILLED"].copy()
    df["fill_dt"] = pd.to_datetime(df["fill_time"].astype(str).str.slice(0, 19), errors="coerce")
    df["exit_dt"] = pd.to_datetime(df["exit_time"].astype(str).str.slice(0, 19), errors="coerce")
    df = df.dropna(subset=["fill_dt", "exit_dt"])
    df = df[(df["fill_dt"] >= WINDOW_START) & (df["fill_dt"] <= WINDOW_END)]
    if len(df) == 0:
        return pd.DataFrame()
    out = pd.DataFrame(
        {
            "label": label,
            "strategy": strategy,
            "ticker": ticker,
            "groupe": groupe,
            "fill_dt": df["fill_dt"].values,
            "exit_dt": df["exit_dt"].values,
            "risk_usd": _risk_usd(df, ticker).values,
            "pnl": df["pnl"].astype(float).values,
        }
    )
    return out


def replay(trades: pd.DataFrame) -> dict:
    """Rejoue à travers PortfolioRiskManager. trades indexé 0..n-1."""
    trades = trades.sort_values("fill_dt").reset_index(drop=True)
    rm = PortfolioRiskManager()
    same_bar = {i for i, r in trades.iterrows() if r["fill_dt"] == r["exit_dt"]}
    events = []
    for i, r in trades.iterrows():
        events.append((r["fill_dt"], "fill", i))
        events.append((r["exit_dt"], "exit", i))

    def _key(e):
        ts, kind, idx = e
        pri = 0 if (kind == "exit" and idx not in same_bar) else (1 if kind == "fill" else 2)
        return (ts, pri)

    events.sort(key=_key)

    accepted, blocked = set(), {}
    realized = 0.0
    breaches = []
    equity = []  # (exit_dt, cum_pnl) après chaque close
    for ts, kind, idx in events:
        r = trades.iloc[idx]
        if kind == "fill":
            ok, reason = rm.can_open(risk_usd=float(r["risk_usd"]), when=ts.to_pydatetime())
            if not ok:
                blocked[idx] = (f"{r['strategy']}_{r['ticker']}", reason)
                continue
            tid = f"{r['strategy']}_{r['ticker']}_{idx}"
            rm.register_open(tid, float(r["risk_usd"]), when=ts.to_pydatetime())
            rm.register_fill(tid, when=ts.to_pydatetime())
            accepted.add(idx)
        else:
            if idx not in accepted:
                continue
            tid = f"{r['strategy']}_{r['ticker']}_{idx}"
            breach, why = rm.register_close(tid, float(r["pnl"]), when=ts.to_pydatetime())
            realized += float(r["pnl"])
            equity.append((ts, rm.cum_pnl))
            if breach:
                breaches.append((ts, why))

    # Métriques equity trade-level (trailing DD honnête sur cum_pnl réalisé)
    eq = pd.DataFrame(equity, columns=["ts", "cum"]).sort_values("ts")
    if len(eq):
        peak = eq["cum"].cummax()
        max_trail_dd = float((eq["cum"] - peak).min())
        # pire daily loss réalisé
        eqd = eq.copy()
        eqd["day"] = eqd["ts"].dt.date
        daily = eqd.groupby("day")["cum"].last().diff()
        # première journée = cum lui-même
        first_day = eqd.groupby("day")["cum"].last().iloc[0]
        worst_daily = float(min(daily.min() if daily.notna().any() else 0.0, first_day))
    else:
        max_trail_dd, worst_daily = 0.0, 0.0

    # bloqués par stratégie
    blk_by_strat = {}
    for _, (sk, _reason) in blocked.items():
        blk_by_strat[sk] = blk_by_strat.get(sk, 0) + 1

    return {
        "n_total": len(trades),
        "n_accepted": len(accepted),
        "n_blocked": len(blocked),
        "realized_pnl": realized,
        "max_trail_dd": max_trail_dd,
        "worst_daily": worst_daily,
        "n_breaches": len(breaches),
        "breaches": breaches[:10],
        "blk_by_strat": blk_by_strat,
        "final_cum": rm.cum_pnl,
        "peak_cum": rm.peak_pnl,
    }


def _print(label, res):
    print(f"\n{'=' * 74}\n  {label}\n{'=' * 74}")
    print(f"  Trades soumis     : {res['n_total']}")
    print(f"  Trades acceptés   : {res['n_accepted']}")
    print(f"  Trades bloqués    : {res['n_blocked']}  {res['blk_by_strat'] or ''}")
    print(f"  P&L réalisé       : ${res['realized_pnl']:+,.0f}")
    print(f"  Peak cum / final  : ${res['peak_cum']:+,.0f} / ${res['final_cum']:+,.0f}")
    print(f"  Max trailing DD   : ${res['max_trail_dd']:+,.0f}   (limite Topstep -$2000)")
    print(f"  Pire daily P&L    : ${res['worst_daily']:+,.0f}   (limite Topstep -$1000)")
    flag = "✓ aucun breach Topstep" if res["n_breaches"] == 0 else f"⚠ {res['n_breaches']} BREACH"
    print(f"  Breaches          : {res['n_breaches']}  {flag}")
    for ts, why in res["breaches"]:
        print(f"      {ts}  {why}")


def main():
    print(f"Fenêtre OOS commune : {WINDOW_START.date()} → {WINDOW_END.date()}")
    parts = [load_source(*s) for s in SOURCES]
    allt = pd.concat([p for p in parts if len(p)], ignore_index=True)

    print("\n=== Trades chargés par source (fenêtre) ===")
    for (lbl, *_), p in zip(SOURCES, parts):
        if len(p):
            print(
                f"  {lbl:16s} n={len(p):4d}  P&L=${p['pnl'].sum():+,.0f}  "
                f"risk_med=${p['risk_usd'].median():.0f}"
            )

    base = allt[allt["groupe"] == "baseline"]
    core = allt[allt["groupe"].isin(["baseline", "fine_core"])]
    allg = allt  # baseline + fine_core + fine_extra

    res_base = replay(base)
    res_core = replay(core)
    res_all = replay(allg)

    _print("BASELINE — book live actuel (OPR NQ1/YM1 + fib-v4 NQ1/MES1/MGC1)", res_base)
    _print("+ FINE_CORE — baseline + fib-fine NQ1+MES1 ($130)", res_core)
    _print("+ FINE_ALL — baseline + fib-fine NQ1+MES1+MGC1 ($130)", res_all)

    print(f"\n{'=' * 74}\n  VERDICT GO/NO-GO\n{'=' * 74}")
    for lbl, r in [("BASELINE", res_base), ("+FINE_CORE", res_core), ("+FINE_ALL", res_all)]:
        dd_ok = r["max_trail_dd"] > -2000
        dy_ok = r["worst_daily"] > -1000
        br_ok = r["n_breaches"] == 0
        verdict = "🟢 OK" if (dd_ok and dy_ok and br_ok) else "🔴 BREACH"
        print(
            f"  {lbl:11s} DD=${r['max_trail_dd']:+,.0f} daily=${r['worst_daily']:+,.0f} "
            f"breaches={r['n_breaches']} P&L=${r['realized_pnl']:+,.0f}  → {verdict}"
        )
    d_core = res_core["max_trail_dd"] - res_base["max_trail_dd"]
    print(f"\n  Δ trailing DD (FINE_CORE − BASELINE) : ${d_core:+,.0f}")
    print(
        f"  Δ P&L réalisé (FINE_CORE − BASELINE) : ${res_core['realized_pnl'] - res_base['realized_pnl']:+,.0f}"
    )


if __name__ == "__main__":
    main()
