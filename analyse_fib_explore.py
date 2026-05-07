"""
Analyse exploratoire Fib sans filtre de session.

Usage :
  python analyse_fib_explore.py               # tous les tickers
  python analyse_fib_explore.py --ticker MES1
"""

import argparse
from pathlib import Path

import pandas as pd
import numpy as np

# ─────────────────────────────────────────────────────────────────────────────
# Sessions UTC (heures de début incluse, fin exclue)
# ─────────────────────────────────────────────────────────────────────────────
SESSIONS = {
    "asie":    (0,  7),   # 00h–07h UTC
    "europe":  (7,  13),  # 07h–13h UTC  (London)
    "us_open": (13, 17),  # 13h–17h UTC  (cash open NY)
    "us_pm":   (17, 21),  # 17h–21h UTC  (après-midi NY)
    "nuit":    (21, 24),  # 21h–00h UTC
}


def session_of(h: int) -> str:
    for name, (s, e) in SESSIONS.items():
        if s <= h < e:
            return name
    return "nuit"


# ─────────────────────────────────────────────────────────────────────────────
# Métriques
# ─────────────────────────────────────────────────────────────────────────────

def metrics(df: pd.DataFrame) -> dict:
    filled = df[df["result"].isin(["TP", "SL", "TE"])]
    n      = len(filled)
    if n == 0:
        return {"n": 0, "WR_%": 0, "PF": 0, "P&L_$": 0,
                "avg_win": 0, "avg_loss": 0, "avg_rr": 0}
    wins   = filled[filled["pnl"] > 0]["pnl"].sum()
    losses = filled[filled["pnl"] < 0]["pnl"].abs().sum()
    return {
        "n":        n,
        "WR_%":     round((filled["result"] == "TP").mean() * 100, 1),
        "PF":       round(wins / losses, 2) if losses > 0 else float("inf"),
        "P&L_$":    round(filled["pnl"].sum()),
        "avg_win":  round(filled[filled["pnl"] > 0]["pnl"].mean()) if (filled["pnl"] > 0).any() else 0,
        "avg_loss": round(filled[filled["pnl"] < 0]["pnl"].mean()) if (filled["pnl"] < 0).any() else 0,
        "avg_rr":   round(filled["rr"].mean(), 2),
    }


def print_table(rows: list, title: str):
    if not rows:
        return
    df = pd.DataFrame(rows)
    print(f"\n{'─'*68}")
    print(f"  {title}")
    print(f"{'─'*68}")
    print(df.to_string(index=False))


# ─────────────────────────────────────────────────────────────────────────────
# Analyse d'un ticker
# ─────────────────────────────────────────────────────────────────────────────

def analyse_ticker(ticker: str):
    path = Path(f"output/backtest_{ticker}_fib_explore.csv")
    if not path.exists():
        print(f"  [!] {path} introuvable — lance d'abord le backtest fib_explore")
        return

    df = pd.read_csv(path)
    df["session"] = df["session_hour_utc"].apply(session_of)

    # Comparaison production vs explore
    prod_path = Path(f"output/backtest_{ticker}_fib.csv")
    prod_n = len(pd.read_csv(prod_path)) if prod_path.exists() else "?"

    filled = df[df["result"].isin(["TP", "SL", "TE"])]
    m      = metrics(df)

    print(f"\n{'═'*68}")
    print(f"  FIB EXPLORE — {ticker}")
    print(f"  {len(filled)} trades filés / {len(df)} signaux")
    print(f"  P&L : ${m['P&L_$']:+,}  |  WR : {m['WR_%']}%  |  PF : {m['PF']}")
    print(f"  (production : {prod_n} trades avec filtre session)")
    print(f"{'═'*68}")

    # ── Par heure UTC ────────────────────────────────────────────────────────
    rows_h = []
    for h in range(24):
        sub = df[df["session_hour_utc"] == h]
        if len(sub[sub["result"].isin(["TP","SL","TE"])]) == 0:
            continue
        m = metrics(sub)
        m["heure_utc"] = f"{h:02d}h"
        m["session"]   = session_of(h)
        rows_h.append(m)
    print_table(rows_h, "PAR HEURE UTC")

    # ── Par session ──────────────────────────────────────────────────────────
    rows_s = []
    for sess in SESSIONS:
        sub = df[df["session"] == sess]
        if len(sub) == 0:
            continue
        m = metrics(sub)
        m["session"] = sess
        rows_s.append(m)
    print_table(rows_s, "PAR SESSION")

    # ── Par direction ────────────────────────────────────────────────────────
    rows_d = []
    for d in ["long", "short"]:
        sub = df[df["direction"] == d]
        m   = metrics(sub)
        m["direction"] = d
        rows_d.append(m)
    print_table(rows_d, "PAR DIRECTION")

    # ── Par niveau Fib ───────────────────────────────────────────────────────
    if "fib_level" in df.columns:
        rows_f = []
        for lvl in sorted(df["fib_level"].dropna().unique()):
            sub = df[df["fib_level"] == lvl]
            m   = metrics(sub)
            m["fib_level"] = lvl
            rows_f.append(m)
        print_table(rows_f, "PAR NIVEAU FIB")

    # ── Par tendance ─────────────────────────────────────────────────────────
    if "trend" in df.columns:
        rows_t = []
        for t in df["trend"].dropna().unique():
            sub = df[df["trend"] == t]
            m   = metrics(sub)
            m["trend"] = t
            rows_t.append(m)
        print_table(rows_t, "PAR TENDANCE")

    # ── Meilleures combos session × direction (≥ 5 trades) ──────────────────
    rows_c = []
    for sess in SESSIONS:
        for d in ["long", "short"]:
            sub = df[(df["session"] == sess) & (df["direction"] == d)]
            f   = sub[sub["result"].isin(["TP","SL","TE"])]
            if len(f) < 5:
                continue
            m = metrics(sub)
            m["session"]   = sess
            m["direction"] = d
            rows_c.append(m)
    rows_c.sort(key=lambda r: r["PF"], reverse=True)
    print_table(rows_c, "COMBOS SESSION × DIRECTION (≥ 5 fills)")

    # ── P&L cumulé par session ───────────────────────────────────────────────
    print(f"\n{'─'*68}")
    print("  P&L CUMULÉ PAR SESSION")
    print(f"{'─'*68}")
    for sess in SESSIONS:
        sub = filled[filled["session"] == sess]
        if len(sub):
            bar = "█" * min(30, max(1, int(abs(sub['pnl'].sum()) / 100)))
            sign = "+" if sub["pnl"].sum() >= 0 else "-"
            print(f"  {sess:<10} : ${sub['pnl'].sum():+7,.0f}  ({len(sub):3d} trades)  {sign}{bar}")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", default=None)
    args = parser.parse_args()

    tickers = [args.ticker] if args.ticker else ["MES1", "NQ1", "YM1"]
    for t in tickers:
        analyse_ticker(t)
    print()


if __name__ == "__main__":
    main()
