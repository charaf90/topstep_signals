#!/usr/bin/env python3
"""
Replay des trades du triplet (Composite + OPR + Fib) à travers le
`PortfolioRiskManager` pour quantifier l'impact d'un garde-fou global
vs les garde-fous per-stratégie actuels.

Méthodologie :
1. Charge tous les trades remplis (composite + OPR + Fib) sur les 3 actifs
2. Trie chronologiquement par fill_time
3. Pour chaque trade, demande au PortfolioRiskManager s'il aurait été
   accepté étant donné l'état global (cum_pnl + risque cumulé des positions
   ouvertes au moment du fill)
4. Mesure les trades bloqués, le P&L "perdu" et le P&L réellement réalisé
   sous garde-fou portefeuille

Usage :
    python replay_portfolio_risk.py
"""

from pathlib import Path

import numpy as np
import pandas as pd

from config import INSTRUMENTS, TOPSTEP_PROFIT_TARGET
from core.risk_portfolio import PortfolioRiskManager


SUFFIX_MAP = {"composite": "", "opr": "_opr", "fib": "_fib"}
LABEL_MAP = {"composite": "Composite", "opr": "OPR", "fib": "Fib"}


def load_all_trades(output_dir: Path) -> pd.DataFrame:
    """Concatène les trades remplis des 3 stratégies × 3 actifs."""
    rows = []
    for strategy, suffix in SUFFIX_MAP.items():
        for ticker in INSTRUMENTS.keys():
            path = output_dir / f"backtest_{ticker}{suffix}.csv"
            if not path.exists() or path.stat().st_size < 10:
                continue
            try:
                df = pd.read_csv(path)
            except pd.errors.EmptyDataError:
                continue
            if len(df) == 0 or "result" not in df.columns:
                continue
            df = df[df["result"] != "NOT_FILLED"].copy()
            if len(df) == 0:
                continue
            df["strategy"] = strategy
            df["ticker"] = ticker
            # Récupère fill_time + exit_time en datetime
            df["fill_time_dt"] = pd.to_datetime(
                df["fill_time"].astype(str).str.slice(0, 19),
                errors="coerce",
            )
            df["exit_time_dt"] = pd.to_datetime(
                df["exit_time"].astype(str).str.slice(0, 19),
                errors="coerce",
            )
            # risque dollar effectif du trade (n_ct × sl_dist × $/pt)
            if "risk_$" in df.columns:
                df["risk_usd"] = df["risk_$"]
            elif "risk" in df.columns:
                df["risk_usd"] = df["risk"]
            else:
                df["risk_usd"] = 100.0
            rows.append(df[[
                "strategy", "ticker", "fill_time_dt", "exit_time_dt",
                "risk_usd", "pnl", "result",
            ]])
    if not rows:
        return pd.DataFrame()
    out = pd.concat(rows, ignore_index=True)
    return out.dropna(subset=["fill_time_dt", "exit_time_dt"])


def replay(trades: pd.DataFrame) -> dict:
    """
    Replay chronologique. Pour chaque trade :
      1. À l'heure de fill, on demande can_open(risk) au manager
      2. Si OK, register_open() puis on traite les exits (close) déjà arrivés
         AVANT ce fill (pour libérer du slack)
      3. À l'exit_time, register_close()
    """
    rm = PortfolioRiskManager()

    events = []
    for i, row in trades.iterrows():
        events.append((row["fill_time_dt"], "fill", i))
        events.append((row["exit_time_dt"], "exit", i))
    # Tri stable : exit avant fill si égalité de timestamp (libère slack)
    type_priority = {"exit": 0, "fill": 1}
    events.sort(key=lambda e: (e[0], type_priority[e[1]]))

    blocked_count = 0
    blocked_pnl_potential = 0.0
    accepted_count = 0
    realized_pnl = 0.0
    breach_count = 0
    blocked_reasons = []
    accepted_idx = set()
    blocked_idx = set()

    for ts, kind, idx in events:
        row = trades.iloc[idx]
        if kind == "fill":
            ok, reason = rm.can_open(risk_usd=float(row["risk_usd"]),
                                     when=ts.to_pydatetime())
            if not ok:
                blocked_count += 1
                blocked_pnl_potential += float(row["pnl"])
                blocked_reasons.append(reason)
                blocked_idx.add(idx)
                continue
            tid = f"{row['strategy']}_{row['ticker']}_{idx}"
            rm.register_open(tid, float(row["risk_usd"]),
                             when=ts.to_pydatetime())
            accepted_idx.add(idx)
            accepted_count += 1
        else:  # exit
            if idx not in accepted_idx:
                continue
            tid = f"{row['strategy']}_{row['ticker']}_{idx}"
            breach, _ = rm.register_close(tid, float(row["pnl"]),
                                          when=ts.to_pydatetime())
            realized_pnl += float(row["pnl"])
            if breach:
                breach_count += 1

    n_total = len(trades)
    return {
        "n_total": n_total,
        "n_accepted": accepted_count,
        "n_blocked": blocked_count,
        "block_rate_pct": 100.0 * blocked_count / n_total if n_total else 0.0,
        "realized_pnl": realized_pnl,
        "blocked_pnl_potential": blocked_pnl_potential,
        "n_breaches": breach_count,
        "final_state": rm.status(),
        "top_block_reasons": pd.Series(blocked_reasons).value_counts().head(5).to_dict(),
        "blocked_idx": blocked_idx,
        "accepted_idx": accepted_idx,
    }


def main():
    output_dir = Path("./output")
    if not output_dir.exists():
        print("[!] output/ introuvable — lance backtest.py --strategy all")
        return

    trades = load_all_trades(output_dir)
    if len(trades) == 0:
        print("[!] aucun trade trouvé")
        return
    trades = trades.sort_values("fill_time_dt").reset_index(drop=True)

    # ── Référence : trades & P&L tels quels (sans garde-fou portefeuille) ──
    n_total = len(trades)
    pnl_total = float(trades["pnl"].sum())
    daily = trades.groupby(trades["exit_time_dt"].dt.date)["pnl"].sum()
    cum = daily.cumsum()
    rolling_max = cum.cummax()
    max_dd = float((cum - rolling_max).min())
    print(f"\n{'='*80}")
    print(f"  RÉFÉRENCE — backtest tel quel (garde-fous per-stratégie actuels)")
    print(f"{'='*80}")
    print(f"  Trades       : {n_total}")
    print(f"  P&L total    : ${pnl_total:+,.0f}")
    print(f"  Max trailing DD : ${max_dd:+,.0f}")
    print(f"  Daily P&L max : ${float(daily.max()):+,.0f}  "
          f"min : ${float(daily.min()):+,.0f}")

    # ── Replay sous PortfolioRiskManager ────────────────────────────────
    print(f"\n{'='*80}")
    print(f"  REPLAY — garde-fou PORTEFEUILLE GLOBAL (live-mode)")
    print(f"{'='*80}")
    res = replay(trades)
    print(f"  Trades soumis       : {res['n_total']}")
    print(f"  Trades acceptés     : {res['n_accepted']}")
    print(f"  Trades bloqués      : {res['n_blocked']}  "
          f"({res['block_rate_pct']:.1f} %)")
    print(f"  P&L réalisé         : ${res['realized_pnl']:+,.0f}")
    print(f"  P&L potentiel bloqué: ${res['blocked_pnl_potential']:+,.0f}")
    print(f"  Breaches limites    : {res['n_breaches']}")
    print(f"\n  État final manager :")
    for k, v in res["final_state"].items():
        if isinstance(v, float):
            print(f"    {k:20s} ${v:+,.0f}" if "pnl" in k or "slack" in k or "remain" in k or "risk" in k
                  else f"    {k:20s} {v:.2f}")
        else:
            print(f"    {k:20s} {v}")
    if res["top_block_reasons"]:
        print(f"\n  Top raisons de blocage :")
        for reason, count in res["top_block_reasons"].items():
            print(f"    {count:>4}× {reason}")

    # ── Synthèse impact ─────────────────────────────────────────────────
    delta_pnl = res["realized_pnl"] - pnl_total
    print(f"\n{'='*80}")
    print(f"  IMPACT — global vs per-stratégie")
    print(f"{'='*80}")
    print(f"  Δ P&L (global − ref) : ${delta_pnl:+,.0f}  "
          f"({100*delta_pnl/abs(pnl_total) if pnl_total else 0:.1f} %)")
    print(f"  Δ trades             : {res['n_accepted'] - n_total:+d} "
          f"(blocked {res['n_blocked']})")
    if res["n_breaches"] == 0:
        print(f"  ✓ Aucune limite Topstep franchie sous garde-fou portefeuille")
    else:
        print(f"  ⚠ {res['n_breaches']} breach(s) résiduel(s) après blocage")

    # ── Export trades acceptés / bloqués ────────────────────────────────
    out_dir = Path("./output")
    trades["accepted_global"] = trades.index.isin(res["accepted_idx"])
    out_path = out_dir / "portfolio_replay.csv"
    trades.to_csv(out_path, index=False)
    print(f"\n  ✓ {out_path}")


if __name__ == "__main__":
    main()
