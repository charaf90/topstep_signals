"""
Comparaison v4 vs v5 vs v5.1 — apples-to-apples sur la même période (full + OOS).

Sorties :
  • Tableau comparatif PF / P&L / DD / n_trades par ticker et portfolio
  • Corrélations P&L daily v4↔v5, v4↔v5.1, v5↔v5.1
  • Distribution des v5_reject_reason pour v5 et v5.1
  • Sauvegarde dans output/compare_v4_v5_v5_1.md

Usage :
    python scripts/compare_v4_v5_v5_1.py
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

from core.data import build_timeframes, load_csv
from strategies import opr as v4
from strategies import opr_v5 as v5
from strategies import opr_v5_1 as v51

TICKERS = ["MES1", "NQ1", "YM1"]
OOS_START = "2025-10-01"


def _stats(df_trades: pd.DataFrame, label: str) -> dict:
    if df_trades is None or df_trades.empty:
        return {
            "label": label,
            "n_filled": 0,
            "n_signals": 0,
            "pnl": 0.0,
            "PF": np.nan,
            "WR_pct": np.nan,
            "DD": 0.0,
        }
    filled = df_trades[df_trades["result"] != "NOT_FILLED"]
    n_filled = len(filled)
    n_signals = len(df_trades)
    pnl = float(df_trades["pnl"].sum())
    wins = filled[filled["pnl"] > 0]
    losses = filled[filled["pnl"] < 0]
    wr = 100.0 * len(wins) / n_filled if n_filled > 0 else np.nan
    gw = float(wins["pnl"].sum()) if not wins.empty else 0.0
    gl = float(losses["pnl"].sum())
    pf = (gw / abs(gl)) if gl < 0 else (np.inf if gw > 0 else np.nan)
    df_sorted = filled.sort_values("date").reset_index(drop=True)
    if not df_sorted.empty:
        cum = df_sorted["pnl"].cumsum()
        peak = cum.cummax()
        dd = float((cum - peak).min())
    else:
        dd = 0.0
    return {
        "label": label,
        "n_filled": int(n_filled),
        "n_signals": int(n_signals),
        "pnl": round(pnl, 2),
        "PF": round(pf, 2) if np.isfinite(pf) else "inf",
        "WR_pct": round(wr, 1) if np.isfinite(wr) else np.nan,
        "DD": round(dd, 2),
    }


def main() -> int:
    print("=" * 70)
    print("  COMPARAISON v4 vs v5 vs v5.1 — apples-to-apples")
    print("=" * 70)

    full_v4: list[pd.DataFrame] = []
    full_v5: list[pd.DataFrame] = []
    full_v51: list[pd.DataFrame] = []
    per_ticker_data: dict = {}

    for ticker in TICKERS:
        csv = PROJECT_ROOT / "data" / f"{ticker}_data_m15.csv"
        df = load_csv(str(csv))
        tf = build_timeframes(df)
        print(f"\n  → Backtest {ticker} v4 (no filter, baseline)...")
        t4 = v4.run_backtest(df, ticker, tf=tf, params=None, topstep_guard=False)
        print(f"  → Backtest {ticker} v5 (params optimaux config)...")
        t5 = v5.run_backtest(df, ticker, tf=tf, params=None, topstep_guard=False)
        print(f"  → Backtest {ticker} v5.1 (params optimaux config)...")
        t51 = v51.run_backtest(df, ticker, tf=tf, params=None, topstep_guard=False)

        t4["ticker"] = ticker
        t5["ticker"] = ticker
        t51["ticker"] = ticker
        full_v4.append(t4)
        full_v5.append(t5)
        full_v51.append(t51)
        per_ticker_data[ticker] = (t4, t5, t51)

    df_v4 = pd.concat(full_v4, ignore_index=True)
    df_v5 = pd.concat(full_v5, ignore_index=True)
    df_v51 = pd.concat(full_v51, ignore_index=True)

    def _mask_oos(df: pd.DataFrame) -> pd.DataFrame:
        return df[pd.to_datetime(df["date"]) >= OOS_START].copy()

    def _mask_is(df: pd.DataFrame) -> pd.DataFrame:
        return df[pd.to_datetime(df["date"]) < OOS_START].copy()

    rows: list[str] = []
    rows.append("# Comparaison opr-v4 vs opr-v5 vs opr-v5.1 — apples-to-apples")
    rows.append("")
    rows.append(
        "Période : sept 2024 → mai 2026 (full), avec split walk-forward "
        "IS=avant 2025-10-01 / OOS=après."
    )
    rows.append("")
    rows.append("opr-v5 et opr-v5.1 utilisent les params optimaux config (PHASE 4 walk-forward).")
    rows.append("")

    # ─── Tableau par ticker ──────────────────────────────────────────────────
    rows.append("## Détail par ticker")
    rows.append("")
    rows.append("### Période full (sept 2024 → mai 2026)")
    rows.append("")
    rows.append("| Ticker | Stratégie | Filled | PF | P&L | WR % | DD |")
    rows.append("|---|---|---:|---:|---:|---:|---:|")
    for ticker, (t4, t5, t51) in per_ticker_data.items():
        s4 = _stats(t4, f"{ticker} v4")
        s5 = _stats(t5, f"{ticker} v5")
        s51 = _stats(t51, f"{ticker} v5.1")
        rows.append(
            f"| {ticker} | opr-v4   | {s4['n_filled']} | {s4['PF']} | "
            f"{s4['pnl']:+.2f} | {s4['WR_pct']} | {s4['DD']:+.2f} |"
        )
        rows.append(
            f"| {ticker} | opr-v5   | {s5['n_filled']} | {s5['PF']} | "
            f"{s5['pnl']:+.2f} | {s5['WR_pct']} | {s5['DD']:+.2f} |"
        )
        rows.append(
            f"| {ticker} | opr-v5.1 | {s51['n_filled']} | {s51['PF']} | "
            f"{s51['pnl']:+.2f} | {s51['WR_pct']} | {s51['DD']:+.2f} |"
        )

    rows.append("")
    rows.append("### Période OOS (oct 2025 → mai 2026)")
    rows.append("")
    rows.append("| Ticker | Stratégie | Filled | PF | P&L | WR % | DD |")
    rows.append("|---|---|---:|---:|---:|---:|---:|")
    for ticker, (t4, t5, t51) in per_ticker_data.items():
        t4_oos = _mask_oos(t4)
        t5_oos = _mask_oos(t5)
        t51_oos = _mask_oos(t51)
        s4 = _stats(t4_oos, f"{ticker} v4 OOS")
        s5 = _stats(t5_oos, f"{ticker} v5 OOS")
        s51 = _stats(t51_oos, f"{ticker} v5.1 OOS")
        rows.append(
            f"| {ticker} | opr-v4   | {s4['n_filled']} | {s4['PF']} | "
            f"{s4['pnl']:+.2f} | {s4['WR_pct']} | {s4['DD']:+.2f} |"
        )
        rows.append(
            f"| {ticker} | opr-v5   | {s5['n_filled']} | {s5['PF']} | "
            f"{s5['pnl']:+.2f} | {s5['WR_pct']} | {s5['DD']:+.2f} |"
        )
        rows.append(
            f"| {ticker} | opr-v5.1 | {s51['n_filled']} | {s51['PF']} | "
            f"{s51['pnl']:+.2f} | {s51['WR_pct']} | {s51['DD']:+.2f} |"
        )

    # ── Portfolio ──
    rows.append("")
    rows.append("## Portfolio agrégé")
    rows.append("")
    rows.append("| Période | Stratégie | Filled | PF | P&L | DD |")
    rows.append("|---|---|---:|---:|---:|---:|")
    for label, df4, df5, df51 in [
        ("Full", df_v4, df_v5, df_v51),
        ("IS", _mask_is(df_v4), _mask_is(df_v5), _mask_is(df_v51)),
        ("OOS", _mask_oos(df_v4), _mask_oos(df_v5), _mask_oos(df_v51)),
    ]:
        s4 = _stats(df4, f"v4 {label}")
        s5 = _stats(df5, f"v5 {label}")
        s51 = _stats(df51, f"v5.1 {label}")
        rows.append(
            f"| {label} | opr-v4   | {s4['n_filled']} | {s4['PF']} | "
            f"{s4['pnl']:+.2f} | {s4['DD']:+.2f} |"
        )
        rows.append(
            f"| {label} | opr-v5   | {s5['n_filled']} | {s5['PF']} | "
            f"{s5['pnl']:+.2f} | {s5['DD']:+.2f} |"
        )
        rows.append(
            f"| {label} | opr-v5.1 | {s51['n_filled']} | {s51['PF']} | "
            f"{s51['pnl']:+.2f} | {s51['DD']:+.2f} |"
        )

    # ── Corrélation P&L daily ──
    rows.append("")
    rows.append("## Corrélations P&L daily")
    rows.append("")
    rows.append("Cible : ∈ [0.7, 0.95] pour valider un edge complémentaire.")
    rows.append("Une corrélation > 0.95 vs v4 signifie quasi-redondance.")
    rows.append("")

    def _daily_pnl(df: pd.DataFrame) -> pd.Series:
        filled = df[df["result"] != "NOT_FILLED"].copy()
        if filled.empty:
            return pd.Series(dtype=float)
        return filled.groupby("date")["pnl"].sum()

    rows.append("| Période | Paire | Corrélation daily | n jours communs |")
    rows.append("|---|---|---:|---:|")
    for label, df4, df5, df51 in [
        ("Full", df_v4, df_v5, df_v51),
        ("OOS", _mask_oos(df_v4), _mask_oos(df_v5), _mask_oos(df_v51)),
    ]:
        s4 = _daily_pnl(df4)
        s5 = _daily_pnl(df5)
        s51 = _daily_pnl(df51)
        for (lhs_name, lhs), (rhs_name, rhs) in [
            (("v4", s4), ("v5", s5)),
            (("v4", s4), ("v5.1", s51)),
            (("v5", s5), ("v5.1", s51)),
        ]:
            df_join = pd.concat([lhs.rename("a"), rhs.rename("b")], axis=1).fillna(0.0)
            if len(df_join) < 5:
                rows.append(f"| {label} | {lhs_name} ↔ {rhs_name} | n/a | {len(df_join)} |")
                continue
            corr = df_join["a"].corr(df_join["b"])
            rows.append(f"| {label} | {lhs_name} ↔ {rhs_name} | {corr:.3f} | {len(df_join)} |")
            print(
                f"  Corrélation daily P&L {label} {lhs_name}↔{rhs_name} : {corr:.3f} ({len(df_join)} jours)"
            )

    # ── Distribution v5_reject_reason ──
    rows.append("")
    rows.append("## Distribution des rejets v5.1 (full période)")
    rows.append("")
    rows.append("Nombre de trades rejetés par chaque filtre v5.1.")
    rows.append("Ordre F1 → F2_min → F2_max → F3 (un trade rejeté par AU PLUS un filtre).")
    rows.append("Nouveau motif v5.1 : `F2_excursion_too_narrow`.")
    rows.append("")
    rows.append("| Ticker | v5_reject_reason | Count |")
    rows.append("|---|---|---:|")
    for ticker, (_, _, t51) in per_ticker_data.items():
        if "v5_reject_reason" in t51.columns:
            counts = t51["v5_reject_reason"].fillna("NO_REJECT").value_counts()
            for reason, c in counts.items():
                rows.append(f"| {ticker} | {reason} | {int(c)} |")

    all_reject = (
        df_v51["v5_reject_reason"].fillna("NO_REJECT").value_counts()
        if "v5_reject_reason" in df_v51.columns
        else pd.Series()
    )
    rows.append("")
    rows.append("### Total portfolio v5.1")
    rows.append("")
    rows.append("| v5_reject_reason | Count |")
    rows.append("|---|---:|")
    for reason, c in all_reject.items():
        rows.append(f"| {reason} | {int(c)} |")

    out_path = PROJECT_ROOT / "output" / "compare_v4_v5_v5_1.md"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text("\n".join(rows))
    print(f"\n  ✓ {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
