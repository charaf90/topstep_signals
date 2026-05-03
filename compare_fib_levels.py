#!/usr/bin/env python3
"""
Comparaison des 7 combinaisons non vides de niveaux Fib ∈ {38.2, 50, 61.8}.

Lit `draft_fibo_50/output/fib_levels_best.csv` (généré par
`optimize_fib_levels.py`) qui contient les meilleurs paramètres SL/TP/IMP
par (ticker, niveau). Pour chaque combinaison de niveaux, lance les
backtests correspondants, agrège chronologiquement et compare.

Usage :
    python compare_fib_levels.py
"""

from itertools import chain, combinations
from pathlib import Path

import numpy as np
import pandas as pd

from config import (
    INSTRUMENTS, TOPSTEP_PROFIT_TARGET,
    TOPSTEP_DAILY_LOSS_MAX, TOPSTEP_TRAILING_DD,
    TOPSTEP_ACCOUNT_SIZE,
)
from core.data import load_csv
from core.strategy_fib import run_fib_backtest


ACCOUNT_SIZE = TOPSTEP_ACCOUNT_SIZE
SHARPE_ANNUALIZATION_DAYS = 252
N_BOOTSTRAP = 1000

LEVEL_LABELS = {0.382: "38.2", 0.500: "50", 0.618: "61.8"}


# ─────────────────────────────────────────────────────────────────────────────
# Métriques portefeuille
# ─────────────────────────────────────────────────────────────────────────────

def _portfolio_stats(trades: pd.DataFrame) -> dict:
    if len(trades) == 0:
        return {"n": 0, "n_days": 0, "pnl": 0.0, "max_dd": 0.0,
                "max_daily_loss": 0.0, "sharpe": 0.0,
                "bootstrap_pass": 0.0, "wr_day": 0.0}
    daily = trades.groupby("exit_date")["pnl"].sum().sort_index()
    n_days = len(daily)
    win_days = int((daily > 0).sum())
    cum = daily.cumsum()
    rolling_max = cum.cummax()
    max_dd = float((cum - rolling_max).min())
    max_daily_loss = float(daily.min())
    rets = daily / ACCOUNT_SIZE
    sharpe = (
        float(rets.mean() / rets.std() * np.sqrt(SHARPE_ANNUALIZATION_DAYS))
        if rets.std() > 0 and len(rets) > 1 else 0.0
    )
    n_pass = 0
    for _ in range(N_BOOTSTRAP):
        perm = np.random.permutation(daily.values)
        cum_p = np.cumsum(perm)
        peak = np.maximum.accumulate(cum_p)
        max_dd_p = (cum_p - peak).min()
        max_loss_p = perm.min()
        hit_target = bool((cum_p >= TOPSTEP_PROFIT_TARGET).any())
        ok = (hit_target and max_dd_p > -TOPSTEP_TRAILING_DD
              and max_loss_p > -TOPSTEP_DAILY_LOSS_MAX)
        if ok:
            n_pass += 1
    return {
        "n": int(len(trades)), "n_days": int(n_days),
        "wr_day": float(win_days / n_days * 100) if n_days > 0 else 0.0,
        "pnl": float(daily.sum()), "max_dd": max_dd,
        "max_daily_loss": max_daily_loss, "sharpe": sharpe,
        "bootstrap_pass": float(n_pass / N_BOOTSTRAP * 100),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Lecture des paramètres optimaux + génération des trades par niveau
# ─────────────────────────────────────────────────────────────────────────────

def load_best_params(path: Path) -> dict:
    """Retourne {(ticker, fib_level): {sl, tp, imp}} depuis fib_levels_best.csv."""
    df = pd.read_csv(path)
    out = {}
    for _, row in df.iterrows():
        out[(row["ticker"], float(row["fib_level"]))] = {
            "sl": float(row["sl"]), "tp": float(row["tp"]),
            "imp": float(row["imp"]),
        }
    return out


def generate_trades_for_level(df_15m, ticker: str, fib_level: float,
                               params: dict, apply_filter: bool) -> pd.DataFrame:
    trades = run_fib_backtest(
        df_15m, ticker, fib_level=fib_level,
        sl_mult=params["sl"], tp_mult=params["tp"],
        min_imp=params["imp"], apply_filter=apply_filter,
    )
    if len(trades) == 0:
        return trades
    trades = trades[trades["result"] != "NOT_FILLED"].copy()
    trades["ticker"] = ticker
    trades["fib_level"] = fib_level
    if "exit_time" in trades.columns and trades["exit_time"].notna().any():
        trades["exit_date"] = trades["exit_time"].astype(str).str.slice(0, 10)
    else:
        trades["exit_date"] = trades["date"].astype(str)
    return trades[["ticker", "fib_level", "date", "exit_date", "result", "pnl"]]


def all_combinations(levels):
    return list(chain.from_iterable(
        combinations(levels, r) for r in range(1, len(levels) + 1)
    ))


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    np.random.seed(42)
    csv_dir = Path("./data")
    best_path = Path("./draft_fibo_50/output/fib_levels_best.csv")
    if not best_path.exists():
        print("[!] fib_levels_best.csv introuvable — lance "
              "draft_fibo_50/optimize_fib_levels.py d'abord")
        return

    best_params = load_best_params(best_path)

    # Niveaux disponibles : ceux qui ont au moins 1 ticker validé OOS
    levels_present = sorted(set(lvl for (_, lvl) in best_params.keys()))
    print(f"  Niveaux Fib avec configuration validée : "
          f"{[LEVEL_LABELS.get(l, l) for l in levels_present]}")

    # Pré-charge les CSVs 15m
    dfs = {}
    for ticker in INSTRUMENTS.keys():
        path = csv_dir / f"{ticker}_data_m15.csv"
        if path.exists():
            dfs[ticker] = load_csv(str(path))

    # Génère le DataFrame de trades pour chaque (ticker, niveau)
    trades_by_level = {}   # {fib_level: concat trades sur tous tickers}
    print(f"\n  Génération des trades par (ticker, niveau)…")
    for fib_level in levels_present:
        parts = []
        for ticker in INSTRUMENTS.keys():
            if (ticker, fib_level) not in best_params:
                continue
            if ticker not in dfs:
                continue
            params = best_params[(ticker, fib_level)]
            t = generate_trades_for_level(dfs[ticker], ticker, fib_level,
                                          params, apply_filter=False)
            if len(t) > 0:
                parts.append(t)
        if parts:
            trades_by_level[fib_level] = pd.concat(parts, ignore_index=True)
            n = len(trades_by_level[fib_level])
            print(f"    Niveau {LEVEL_LABELS.get(fib_level)}: {n} trades")

    if not trades_by_level:
        print("[!] Aucun trade généré — vérifier fib_levels_best.csv")
        return

    # ── Comparaison des 7 combinaisons ───────────────────────────────────
    print(f"\n{'='*100}")
    print(f"  COMPARAISON — toutes combinaisons de niveaux Fib")
    print(f"{'='*100}")
    print(f"  {'Combinaison':<22}  {'Trades':>6} {'Days':>5} {'WR_d':>5}  "
          f"{'P&L':>9} {'MaxDD':>9} {'DayLoss':>8}  {'Sharpe':>7}  {'BootPass':>8}")
    print(f"  {'-'*100}")

    rows = []
    for combo in all_combinations(sorted(trades_by_level.keys())):
        parts = [trades_by_level[lvl] for lvl in combo]
        merged = pd.concat(parts, ignore_index=True)
        merged = merged.sort_values("exit_date").reset_index(drop=True)
        st = _portfolio_stats(merged)
        combo_label = " + ".join(LEVEL_LABELS.get(l) for l in combo)
        print(f"  {combo_label:<22}  "
              f"{st['n']:>6} {st['n_days']:>5} {st['wr_day']:>4.1f}%  "
              f"${st['pnl']:>+8.0f} ${st['max_dd']:>+8.0f} "
              f"${st['max_daily_loss']:>+7.0f}  "
              f"{st['sharpe']:>7.2f}  {st['bootstrap_pass']:>7.1f}%")
        rows.append({
            "combo": combo_label,
            "levels": ",".join(str(l) for l in combo),
            **st,
        })

    df_res = pd.DataFrame(rows)
    out_dir = Path("./output")
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "fib_levels_comparison.csv"
    df_res.to_csv(out_path, index=False)
    print(f"\n  ✓ {out_path}")

    # ── Recommandation ──────────────────────────────────────────────────
    print(f"\n{'='*100}")
    print(f"  RECOMMANDATIONS")
    print(f"{'='*100}")
    df_res["score"] = df_res["sharpe"] * df_res["bootstrap_pass"]
    best_pnl = df_res.loc[df_res["pnl"].idxmax()]
    best_sharpe = df_res.loc[df_res["sharpe"].idxmax()]
    best_score = df_res.loc[df_res["score"].idxmax()]
    print(f"  Max P&L     : {best_pnl['combo']:<22}  "
          f"P&L=${best_pnl['pnl']:>+,.0f}  Sharpe={best_pnl['sharpe']:.2f}  "
          f"BS={best_pnl['bootstrap_pass']:.1f}%")
    print(f"  Max Sharpe  : {best_sharpe['combo']:<22}  "
          f"P&L=${best_sharpe['pnl']:>+,.0f}  Sharpe={best_sharpe['sharpe']:.2f}  "
          f"BS={best_sharpe['bootstrap_pass']:.1f}%")
    print(f"\n  ➜ Meilleure config (Sharpe × Bootstrap) : {best_score['combo']}")
    print(f"     P&L=${best_score['pnl']:+,.0f}  DD=${best_score['max_dd']:+,.0f}  "
          f"Sharpe={best_score['sharpe']:.2f}  "
          f"Bootstrap={best_score['bootstrap_pass']:.1f}%")


if __name__ == "__main__":
    main()
