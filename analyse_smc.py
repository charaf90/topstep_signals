"""
Analyse exploratoire des trades SMC.

Usage :
  python analyse_smc.py --ticker NQ1
  python analyse_smc.py --ticker NQ1 --csv output/backtest_NQ1_smc.csv
"""

import argparse
from pathlib import Path

import pandas as pd
import numpy as np


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def pf(df):
    wins   = df[df["pnl"] > 0]["pnl"].sum()
    losses = df[df["pnl"] < 0]["pnl"].abs().sum()
    return round(wins / losses, 2) if losses > 0 else float("inf")

def wr(df):
    filled = df[df["result"].isin(["TP", "SL", "TE"])]
    if len(filled) == 0:
        return 0.0
    return round((filled["result"] == "TP").mean() * 100, 1)

def summary(df, label=""):
    filled  = df[df["result"].isin(["TP", "SL", "TE"])]
    n_tot   = len(df)
    n_fill  = len(filled)
    fill_r  = round(n_fill / n_tot * 100, 1) if n_tot > 0 else 0
    pnl     = filled["pnl"].sum()
    avg_w   = filled[filled["pnl"] > 0]["pnl"].mean() if (filled["pnl"] > 0).any() else 0
    avg_l   = filled[filled["pnl"] < 0]["pnl"].mean() if (filled["pnl"] < 0).any() else 0
    return {
        "label":    label,
        "signaux":  n_tot,
        "fills":    n_fill,
        "fill_%":   fill_r,
        "WR_%":     wr(filled),
        "PF":       pf(filled),
        "P&L_$":    round(pnl),
        "avg_win":  round(avg_w),
        "avg_loss": round(avg_l),
    }


def print_table(rows, title):
    if not rows:
        return
    print(f"\n{'─'*70}")
    print(f"  {title}")
    print(f"{'─'*70}")
    df = pd.DataFrame(rows)
    print(df.to_string(index=False))


# ─────────────────────────────────────────────────────────────────────────────
# Génération du CSV exploratoire (toutes killzones, BOS+CHoCH, swing+internal)
# ─────────────────────────────────────────────────────────────────────────────

def generate_exploration_csv(ticker: str, csv_dir: str, output_path: str):
    from core.data import load_csv
    from strategies.smc import run_backtest
    from config import SMC_ALL_KILLZONES

    print(f"  Génération CSV exploratoire {ticker} …")
    df_15m = load_csv(f"{csv_dir}/{ticker}_data_m15.csv")

    params = {
        "entry_pct":       0.5,
        "sl_atr_mult":     0.5,
        "tp_atr_mult":     2.0,
        "structure_level": "both",
        "signal_type":     "both",
        "max_trades":      20,
        "killzones":       "all",
    }
    df_trades = run_backtest(df_15m, ticker, topstep_guard=False, params=params)
    df_trades.to_csv(output_path, index=False)
    print(f"  → {len(df_trades)} trades enregistrés dans {output_path}")
    return df_trades


# ─────────────────────────────────────────────────────────────────────────────
# Analyse
# ─────────────────────────────────────────────────────────────────────────────

def analyse(df: pd.DataFrame, ticker: str):
    print(f"\n{'═'*70}")
    print(f"  ANALYSE SMC — {ticker}")
    print(f"  {len(df)} signaux générés")
    print(f"{'═'*70}")

    # ── Vue d'ensemble ────────────────────────────────────────────────────────
    filled = df[df["result"].isin(["TP", "SL", "TE"])]
    print(f"\n  Fills        : {len(filled)} / {len(df)}  ({round(len(filled)/len(df)*100,1)}%)")
    print(f"  P&L total    : ${filled['pnl'].sum():+,.0f}")
    print(f"  WR           : {wr(filled)}%")
    print(f"  PF           : {pf(filled)}")

    # ── Par type de signal (CHoCH / BOS) ─────────────────────────────────────
    print_table(
        [summary(df[df["struct_type"] == t], t) for t in df["struct_type"].dropna().unique()],
        "PAR TYPE DE SIGNAL"
    )

    # ── Par niveau de structure (swing / internal) ───────────────────────────
    print_table(
        [summary(df[df["struct_label"] == l], l) for l in df["struct_label"].dropna().unique()],
        "PAR NIVEAU DE STRUCTURE"
    )

    # ── Par killzone ──────────────────────────────────────────────────────────
    kz_order = ["london_open", "ny_premarket", "ny_open",
                 "london_close", "ny_midday", "ny_afternoon"]
    present  = [k for k in kz_order if k in df["killzone"].values]
    print_table(
        [summary(df[df["killzone"] == k], k) for k in present],
        "PAR KILLZONE"
    )

    # ── Par direction ─────────────────────────────────────────────────────────
    print_table(
        [summary(df[df["dir"] == d], d) for d in ["long", "short"]],
        "PAR DIRECTION"
    )

    # ── Meilleures combinaisons (≥ 5 fills) ──────────────────────────────────
    combos = []
    for st in df["struct_type"].dropna().unique():
        for sl in df["struct_label"].dropna().unique():
            for kz in df["killzone"].dropna().unique():
                subset = df[(df["struct_type"] == st) &
                            (df["struct_label"] == sl) &
                            (df["killzone"] == kz)]
                filled_s = subset[subset["result"].isin(["TP", "SL", "TE"])]
                if len(filled_s) >= 5:
                    combos.append(summary(subset, f"{st}|{sl}|{kz}"))

    combos.sort(key=lambda r: r["PF"], reverse=True)
    if combos:
        print_table(combos[:10], "TOP 10 COMBINAISONS (≥ 5 fills)")

    # ── Résultat par résultat ─────────────────────────────────────────────────
    print(f"\n{'─'*70}")
    print("  DISTRIBUTION DES RÉSULTATS")
    print(f"{'─'*70}")
    print(df["result"].value_counts().to_string())

    # ── P&L cumulé par killzone (progression) ────────────────────────────────
    print(f"\n{'─'*70}")
    print("  P&L CUMULÉ PAR KILLZONE (fills uniquement)")
    print(f"{'─'*70}")
    for kz in present:
        subset = filled[filled["killzone"] == kz]
        if len(subset):
            cum = subset["pnl"].sum()
            print(f"  {kz:<16} : ${cum:+7,.0f}  ({len(subset)} trades)")

    print(f"\n{'═'*70}\n")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker",  default="NQ1")
    parser.add_argument("--csv",     default=None, help="CSV existant (skip génération)")
    parser.add_argument("--csv-dir", default="./data")
    args = parser.parse_args()

    ticker   = args.ticker
    csv_path = args.csv or f"output/backtest_{ticker}_smc_explore.csv"

    if args.csv and Path(args.csv).exists():
        print(f"  Chargement {args.csv} …")
        df = pd.read_csv(args.csv)
    else:
        df = generate_exploration_csv(ticker, args.csv_dir, csv_path)

    analyse(df, ticker)


if __name__ == "__main__":
    main()
