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
from core.signal_selector import TICKER_PRIORITY


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
    Replay chronologique. Pour chaque trade (qui est déjà un fill confirmé
    dans les CSVs de backtest) :
      1. À fill_time : can_open() → si OK, register_open() + register_fill()
         (les deux ensemble car le backtest ne distingue pas arm vs fill)
      2. À exit_time : register_close()

    Tri stable : exit avant fill à timestamp égal pour libérer le slack avant
    d'évaluer un nouveau trade concurrent.
    """
    rm = PortfolioRiskManager()

    # Trades dont fill_time == exit_time : SL touché sur la même bougie que le fill.
    # L'exit doit être traité APRÈS le fill (pas avant), sinon la position n'est
    # jamais enregistrée et reste ouverte indéfiniment.
    same_bar_idx = {
        i for i, row in trades.iterrows()
        if row["fill_time_dt"] == row["exit_time_dt"]
    }

    events = []
    for i, row in trades.iterrows():
        events.append((row["fill_time_dt"], "fill", i))
        events.append((row["exit_time_dt"], "exit", i))

    def _sort_key(e):
        ts, kind, idx = e
        # Priorité à égalité de timestamp :
        #  0 = exit d'un trade à durée normale (libère le slack)
        #  1 = fill (profite du slack libéré)
        #  2 = exit same-bar (traité juste après le fill correspondant)
        if kind == "exit" and idx not in same_bar_idx:
            pri = 0
        elif kind == "fill":
            pri = 1
        else:
            pri = 2
        return (ts, pri)

    events.sort(key=_sort_key)

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
            # Dans le backtest les CSVs contiennent des fills confirmés —
            # on arm + fill immédiatement pour comptabiliser le trade journalier.
            rm.register_open(tid, float(row["risk_usd"]),
                             when=ts.to_pydatetime())
            rm.register_fill(tid, when=ts.to_pydatetime())
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


def apply_correlation_filter(trades: pd.DataFrame) -> pd.DataFrame:
    """
    Quand plusieurs actifs déclenchent la même stratégie sur la même bougie
    M15 (fill_time_dt identique), ne conserve que le ticker le mieux classé OOS.

    Groupement par (strategy, fill_time_dt) — sur futures US corrélés, les
    signaux simultanés ont presque toujours la même direction.
    Retourne un DataFrame allégé (lignes dupliquées supprimées).
    """
    rows_to_keep = set()
    for (strategy, fill_ts), grp in trades.groupby(["strategy", "fill_time_dt"]):
        if len(grp) == 1:
            rows_to_keep.update(grp.index.tolist())
            continue
        priority = TICKER_PRIORITY.get(strategy, {})
        best_idx = min(grp.index, key=lambda i: priority.get(trades.at[i, "ticker"], 999))
        rows_to_keep.add(best_idx)
    filtered = trades.loc[sorted(rows_to_keep)].reset_index(drop=True)
    return filtered


def _print_replay(label: str, res: dict, pnl_ref: float):
    print(f"\n{'='*80}")
    print(f"  {label}")
    print(f"{'='*80}")
    print(f"  Trades soumis       : {res['n_total']}")
    print(f"  Trades acceptés     : {res['n_accepted']}")
    print(f"  Trades bloqués      : {res['n_blocked']}  "
          f"({res['block_rate_pct']:.1f} %)")
    print(f"  P&L réalisé         : ${res['realized_pnl']:+,.0f}")
    print(f"  P&L potentiel bloqué: ${res['blocked_pnl_potential']:+,.0f}")
    print(f"  Breaches limites    : {res['n_breaches']}")
    delta = res["realized_pnl"] - pnl_ref
    print(f"  Δ P&L vs référence  : ${delta:+,.0f}  "
          f"({100*delta/abs(pnl_ref) if pnl_ref else 0:.1f} %)")
    if res["top_block_reasons"]:
        print(f"  Top raisons blocage :")
        for reason, count in res["top_block_reasons"].items():
            print(f"    {count:>4}× {reason}")
    if res["n_breaches"] == 0:
        print(f"  ✓ Aucune limite Topstep franchie")
    else:
        print(f"  ⚠ {res['n_breaches']} breach(s)")


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

    # ── Scénario A : garde-fou global SANS sélection actif ──────────────
    res_a = replay(trades)
    _print_replay("REPLAY A — garde-fou global (3 fills/jour, sans sélection actif)",
                  res_a, pnl_total)

    # ── Scénario B : garde-fou global + sélection actif corrélé ─────────
    n_corr_removed = len(trades) - len(apply_correlation_filter(trades))
    trades_sel = apply_correlation_filter(trades)
    res_b = replay(trades_sel)
    _print_replay(
        f"REPLAY B — garde-fou global + sélection actif (élimine {n_corr_removed} signaux corrélés)",
        res_b, pnl_total,
    )

    # ── Comparaison A vs B ───────────────────────────────────────────────
    print(f"\n{'='*80}")
    print(f"  COMPARAISON — impact de la sélection actif (A vs B)")
    print(f"{'='*80}")
    print(f"  Trades soumis     : {res_a['n_total']:>6}  →  {res_b['n_total']:>6}  "
          f"(−{n_corr_removed} corrélés éliminés amont)")
    print(f"  Trades acceptés   : {res_a['n_accepted']:>6}  →  {res_b['n_accepted']:>6}")
    print(f"  Trades bloqués    : {res_a['n_blocked']:>6}  →  {res_b['n_blocked']:>6}")
    print(f"  P&L réalisé       : ${res_a['realized_pnl']:>+10,.0f}  →  "
          f"${res_b['realized_pnl']:>+10,.0f}")
    delta_ab = res_b["realized_pnl"] - res_a["realized_pnl"]
    print(f"  Δ P&L (B − A)     : ${delta_ab:>+10,.0f}")

    # ── Export ──────────────────────────────────────────────────────────
    out_dir = Path("./output")
    trades["accepted_global"] = trades.index.isin(res_a["accepted_idx"])
    trades_sel["accepted_selected"] = trades_sel.index.isin(res_b["accepted_idx"])
    out_path = out_dir / "portfolio_replay.csv"
    trades.to_csv(out_path, index=False)
    out_path_b = out_dir / "portfolio_replay_selected.csv"
    trades_sel.to_csv(out_path_b, index=False)
    print(f"\n  ✓ {out_path}")
    print(f"  ✓ {out_path_b}")


if __name__ == "__main__":
    main()
