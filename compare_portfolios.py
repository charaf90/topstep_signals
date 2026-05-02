#!/usr/bin/env python3
"""
Comparaison des portefeuilles formés par toutes les combinaisons des 3 stratégies.

Lit les CSVs de sortie de `backtest.py --strategy all` :
  - output/backtest_{TICKER}.csv         (composite)
  - output/backtest_{TICKER}_opr.csv     (OPR)
  - output/backtest_{TICKER}_fib.csv     (Fib)

Pour chaque combinaison non vide de {composite, opr, fib}, agrège les trades
chronologiquement sur les 3 actifs et calcule :
  - P&L total, max drawdown trailing
  - Win rate journalier, jours gagnants
  - Sharpe annualisé (sur returns journaliers)
  - Bootstrap rate Topstep (1000 perm., target $3K, max DD -$2K, daily loss -$1K)

Identifie la combinaison offrant le meilleur compromis P&L / DD / Sharpe.

Usage :
    python compare_portfolios.py
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


ACCOUNT_SIZE = TOPSTEP_ACCOUNT_SIZE
SHARPE_ANNUALIZATION_DAYS = 252
N_BOOTSTRAP = 1000

STRATEGIES = ["composite", "opr", "fib"]
SUFFIX_MAP = {"composite": "", "opr": "_opr", "fib": "_fib"}
LABEL_MAP = {"composite": "Composite", "opr": "OPR", "fib": "Fib"}


# ─────────────────────────────────────────────────────────────────────────────
# Chargement des trades
# ─────────────────────────────────────────────────────────────────────────────

def load_strategy_trades(strategy: str, output_dir: Path) -> pd.DataFrame:
    """Charge tous les trades d'une stratégie sur les 3 actifs."""
    suffix = SUFFIX_MAP[strategy]
    rows = []
    for ticker in INSTRUMENTS.keys():
        path = output_dir / f"backtest_{ticker}{suffix}.csv"
        if not path.exists() or path.stat().st_size < 10:
            continue   # CSV vide (ex. composite YM1 désactivé)
        try:
            df = pd.read_csv(path)
        except pd.errors.EmptyDataError:
            continue
        if len(df) == 0 or "result" not in df.columns:
            continue
        # Garde seulement les trades remplis
        df = df[df["result"] != "NOT_FILLED"].copy()
        if len(df) == 0:
            continue
        df["strategy"] = strategy
        df["ticker"] = ticker
        # Date sortie unifiée : exit_time si dispo (slicing direct des 10
        # premiers caractères "YYYY-MM-DD" — évite les problèmes de mixed-tz).
        if "exit_time" in df.columns and df["exit_time"].notna().any():
            df["exit_date"] = df["exit_time"].astype(str).str.slice(0, 10)
            # Fallback : si exit_date n'est pas une date valide, on retombe sur date
            mask_bad = ~df["exit_date"].str.match(r"^\d{4}-\d{2}-\d{2}$")
            df.loc[mask_bad, "exit_date"] = df.loc[mask_bad, "date"].astype(str)
        else:
            df["exit_date"] = df["date"].astype(str)
        rows.append(df[["strategy", "ticker", "date", "exit_date",
                        "result", "pnl"]])
    if not rows:
        return pd.DataFrame(columns=["strategy", "ticker", "date",
                                     "exit_date", "result", "pnl"])
    return pd.concat(rows, ignore_index=True)


# ─────────────────────────────────────────────────────────────────────────────
# Métriques portefeuille
# ─────────────────────────────────────────────────────────────────────────────

def _portfolio_stats(trades: pd.DataFrame) -> dict:
    """
    Stats portefeuille agrégé chronologiquement sur exit_date.
    P&L journalier, DD, Sharpe annualisé, bootstrap Topstep.
    """
    if len(trades) == 0:
        return {"n": 0, "n_days": 0, "win_days": 0, "wr_day": 0.0,
                "pnl": 0.0, "max_dd": 0.0, "max_daily_loss": 0.0,
                "sharpe": 0.0, "bootstrap_pass": 0.0, "n_bs_pass": 0}

    daily = trades.groupby("exit_date")["pnl"].sum().sort_index()
    n_days = len(daily)
    win_days = int((daily > 0).sum())

    # P&L cumulé + drawdown
    cum = daily.cumsum()
    rolling_max = cum.cummax()
    max_dd = float((cum - rolling_max).min())
    max_daily_loss = float(daily.min())

    # Sharpe annualisé sur returns journaliers
    rets = daily / ACCOUNT_SIZE
    if rets.std() > 0 and len(rets) > 1:
        sharpe = float(rets.mean() / rets.std() * np.sqrt(SHARPE_ANNUALIZATION_DAYS))
    else:
        sharpe = 0.0

    # Bootstrap Topstep : 1000 permutations de l'ordre des jours
    n_pass = 0
    for _ in range(N_BOOTSTRAP):
        perm = np.random.permutation(daily.values)
        cum_p = np.cumsum(perm)
        peak = np.maximum.accumulate(cum_p)
        max_dd_p = (cum_p - peak).min()
        max_loss_p = perm.min()
        # Hit target $3K avant breach DD trailing $2K et perte journalière $1K
        hit_target = False
        for v in cum_p:
            if v >= TOPSTEP_PROFIT_TARGET:
                hit_target = True
                break
        ok = (
            hit_target
            and max_dd_p > -TOPSTEP_TRAILING_DD
            and max_loss_p > -TOPSTEP_DAILY_LOSS_MAX
        )
        if ok:
            n_pass += 1

    return {
        "n": int(len(trades)),
        "n_days": int(n_days),
        "win_days": int(win_days),
        "wr_day": float(win_days / n_days * 100) if n_days > 0 else 0.0,
        "pnl": float(daily.sum()),
        "max_dd": max_dd,
        "max_daily_loss": max_daily_loss,
        "sharpe": sharpe,
        "bootstrap_pass": float(n_pass / N_BOOTSTRAP * 100),
        "n_bs_pass": int(n_pass),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Énumération des combinaisons
# ─────────────────────────────────────────────────────────────────────────────

def all_combinations(strats):
    """Toutes les combinaisons non vides de la liste `strats`."""
    return list(chain.from_iterable(
        combinations(strats, r) for r in range(1, len(strats) + 1)
    ))


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    np.random.seed(42)
    output_dir = Path("./output")
    if not output_dir.exists():
        print("[!] output/ introuvable — lance backtest.py --strategy all")
        return

    # Charger trades par stratégie
    trades_by_strat = {}
    for s in STRATEGIES:
        df = load_strategy_trades(s, output_dir)
        trades_by_strat[s] = df
        n = len(df)
        print(f"  Chargé {s}: {n} trades remplis")

    print(f"\n{'='*92}")
    print(f"  COMPARAISON PORTEFEUILLES — toutes combinaisons "
          f"de {{Composite, OPR, Fib}}")
    print(f"{'='*92}")
    print(f"  {'Combinaison':<28}  {'Trades':>6} {'Days':>5} {'WR_day':>6}  "
          f"{'P&L':>9} {'MaxDD':>9} {'DayLoss':>8}  {'Sharpe':>6}  {'BootPass':>8}")
    print(f"  {'-'*92}")

    rows = []
    for combo in all_combinations(STRATEGIES):
        # Concaténation des trades
        parts = [trades_by_strat[s] for s in combo if len(trades_by_strat[s]) > 0]
        if not parts:
            continue
        merged = pd.concat(parts, ignore_index=True)
        merged = merged.sort_values("exit_date").reset_index(drop=True)
        st = _portfolio_stats(merged)
        combo_label = " + ".join(LABEL_MAP[s] for s in combo)
        print(f"  {combo_label:<28}  "
              f"{st['n']:>6} {st['n_days']:>5} {st['wr_day']:>5.1f}%  "
              f"${st['pnl']:>+8.0f} ${st['max_dd']:>+8.0f} ${st['max_daily_loss']:>+7.0f}  "
              f"{st['sharpe']:>6.2f}  {st['bootstrap_pass']:>7.1f}%")
        rows.append({
            "combo": combo_label,
            "strategies": ",".join(combo),
            **st,
        })

    df_res = pd.DataFrame(rows)
    out_path = output_dir / "portfolio_comparison.csv"
    df_res.to_csv(out_path, index=False)
    print(f"\n  ✓ {out_path}")

    # ── Recommandations ─────────────────────────────────────────────────
    if len(df_res) == 0:
        return
    print(f"\n{'='*92}")
    print(f"  RECOMMANDATIONS")
    print(f"{'='*92}")

    best_pnl = df_res.loc[df_res["pnl"].idxmax()]
    best_sharpe = df_res.loc[df_res["sharpe"].idxmax()]
    best_bootstrap = df_res.loc[df_res["bootstrap_pass"].idxmax()]
    # Score composite : Sharpe × bootstrap (en %), pour favoriser robustesse + qualité
    df_res["score"] = df_res["sharpe"] * df_res["bootstrap_pass"]
    best_score = df_res.loc[df_res["score"].idxmax()]

    print(f"  Max P&L           : {best_pnl['combo']:<28}  "
          f"${best_pnl['pnl']:>+,.0f}  Sharpe={best_pnl['sharpe']:.2f}  "
          f"Bootstrap={best_pnl['bootstrap_pass']:.1f}%")
    print(f"  Max Sharpe        : {best_sharpe['combo']:<28}  "
          f"${best_sharpe['pnl']:>+,.0f}  Sharpe={best_sharpe['sharpe']:.2f}  "
          f"Bootstrap={best_sharpe['bootstrap_pass']:.1f}%")
    print(f"  Max Bootstrap     : {best_bootstrap['combo']:<28}  "
          f"${best_bootstrap['pnl']:>+,.0f}  Sharpe={best_bootstrap['sharpe']:.2f}  "
          f"Bootstrap={best_bootstrap['bootstrap_pass']:.1f}%")
    print(f"\n  ➜ Recommandation : {best_score['combo']:<28}  "
          f"(Sharpe × Bootstrap = {best_score['score']:.0f})")
    print(f"     P&L=${best_score['pnl']:+,.0f}  DD=${best_score['max_dd']:+,.0f}  "
          f"Sharpe={best_score['sharpe']:.2f}  "
          f"Bootstrap={best_score['bootstrap_pass']:.1f}%")


if __name__ == "__main__":
    main()
