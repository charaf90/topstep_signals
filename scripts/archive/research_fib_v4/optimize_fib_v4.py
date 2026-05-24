"""
Phase 4 — Walk-forward IS/OOS resserré pour fib-v4.

Pour chaque cellule viable :
  1. Re-exécute le baseline (apply_filter=False) avec features fib-v4
  2. Split IS / OOS sur date
  3. Évalue 2 stratégies de filtre :
       A. Seuil universel : wick_through_atr < 0.5 AND pivot_break_atr >= 0
       B. Grid par cellule : 5 seuils wick_through_atr × pivot_break_atr >= 0
  4. Sélection sur IS, validation OOS
  5. Bootstrap stationnaire sur les trades OOS

Sortie :
  • output/fib_v4_optimize_results.csv
  • output/rapport_fib-v4_optimize.md

IS/OOS :
  • M15 : IS sept 2024 → 2025-09-30 ; OOS 2025-10-01 → mai 2026
  • M5  : IS 50% (oct 2025 → mi-janv 2026) ; OOS 50% (mi-janv → mai 2026)

Usage :
  python scripts/optimize_fib_v4.py
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
from core.robustness import block_bootstrap
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

# Splits IS/OOS — heure UTC, date des trades (pending_time)
IS_END_M15 = pd.Timestamp("2025-09-30 23:59:59")
OOS_END_M15 = pd.Timestamp("2026-05-31 23:59:59")
IS_END_M5  = pd.Timestamp("2026-01-15 23:59:59")  # ~50% de l'historique M5

# Grille de seuils wick_through_atr (5 valeurs)
WICK_GRID = [0.05, 0.10, 0.20, 0.40, 0.80]

# Seuil universel pour stratégie A
WICK_UNIVERSAL = 0.50
PIVOT_BREAK_BUFFER = 0.0  # buffer ATR sur pivot_break_atr (>= -buffer)


def patch_constants(constants: dict) -> None:
    for name, value in constants.items():
        setattr(sf, name, value)


def cost_rt(ticker: str) -> float:
    slip = SLIPPAGE_TICKS_PER_TICKER.get(ticker, 1)
    tick = INSTRUMENTS[ticker]["tick_size"]
    dpp = INSTRUMENTS[ticker]["dollar_per_point"]
    return 2.0 * slip * tick * dpp + COMMISSION_RT_PER_CONTRACT


def pf(arr_pnl: np.ndarray) -> float:
    gp = arr_pnl[arr_pnl > 0].sum()
    gl = -arr_pnl[arr_pnl < 0].sum()
    if gl <= 0:
        return float("inf") if gp > 0 else float("nan")
    return float(gp / gl)


def run_cell_full(ticker: str, tf: str, fib_level: float) -> pd.DataFrame:
    """Backtest baseline complet + frictions + parsing date."""
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

    # Garder uniquement les trades fillés
    trades = trades[trades["result"].isin(["TP", "SL", "TE"])].copy()

    rt = cost_rt(ticker)
    trades["pnl_gross"] = trades["pnl"]
    trades["pnl"] = trades["pnl_gross"] - rt * trades["n_ct"]

    trades["pending_dt"] = pd.to_datetime(trades["pending_time"])
    return trades


def split_is_oos(trades: pd.DataFrame, tf: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    is_end = IS_END_M15 if tf == "m15" else IS_END_M5
    oos_end = OOS_END_M15
    is_trades = trades[trades["pending_dt"] <= is_end].copy()
    oos_trades = trades[(trades["pending_dt"] > is_end) &
                        (trades["pending_dt"] <= oos_end)].copy()
    return is_trades, oos_trades


def apply_filter(trades: pd.DataFrame, wick_max: float,
                 pivot_buffer: float = 0.0) -> pd.DataFrame:
    """Filtre fib-v4 : wick_through_atr < seuil ET pivot_break_atr >= -buffer."""
    if "wick_through_atr" not in trades.columns:
        return trades
    mask = (trades["wick_through_atr"] < wick_max) & \
           (trades["pivot_break_atr"] >= -pivot_buffer)
    return trades[mask].copy()


def metrics(trades: pd.DataFrame) -> dict:
    if len(trades) == 0:
        return dict(n=0, wr=float("nan"), pf=float("nan"),
                    pnl=0.0, dd=0.0, sharpe=float("nan"))
    wins = trades[trades["pnl"] > 0]
    losses = trades[trades["pnl"] < 0]
    gp = wins["pnl"].sum()
    gl = -losses["pnl"].sum()
    pf_val = (gp / gl) if gl > 0 else (float("inf") if gp > 0 else float("nan"))
    eq = trades.sort_values("pending_dt")["pnl"].cumsum()
    dd = float((eq - eq.cummax()).min()) if len(eq) else 0.0
    pnl_std = trades["pnl"].std(ddof=1) if len(trades) > 1 else float("nan")
    sharpe = (trades["pnl"].mean() / pnl_std) if pnl_std and pnl_std > 0 else float("nan")
    return dict(
        n=len(trades),
        wr=float((trades["pnl"] > 0).mean()),
        pf=float(pf_val),
        pnl=float(trades["pnl"].sum()),
        dd=dd,
        sharpe=float(sharpe) if not np.isnan(sharpe) else float("nan"),
    )


def evaluate_cell(ticker: str, tf: str, fib_level: float) -> dict:
    """Workflow complet sur une cellule : baseline → IS/OOS → grid → bootstrap."""
    trades = run_cell_full(ticker, tf, fib_level)
    if len(trades) < 60:
        return dict(ticker=ticker, tf=tf, fib_level=fib_level, n_total=len(trades),
                    skipped="trop peu de trades")

    is_trades, oos_trades = split_is_oos(trades, tf)
    if len(is_trades) < 30 or len(oos_trades) < 10:
        return dict(ticker=ticker, tf=tf, fib_level=fib_level,
                    n_total=len(trades), n_is=len(is_trades), n_oos=len(oos_trades),
                    skipped="split IS/OOS insuffisant")

    # Baseline IS/OOS (sans filtre)
    base_is = metrics(is_trades)
    base_oos = metrics(oos_trades)

    # ── Stratégie A : seuil universel ──
    a_is = apply_filter(is_trades, WICK_UNIVERSAL, PIVOT_BREAK_BUFFER)
    a_oos = apply_filter(oos_trades, WICK_UNIVERSAL, PIVOT_BREAK_BUFFER)
    a_is_m = metrics(a_is)
    a_oos_m = metrics(a_oos)

    # ── Stratégie B : grid par cellule ──
    grid_rows = []
    for wmax in WICK_GRID:
        gi = apply_filter(is_trades, wmax, PIVOT_BREAK_BUFFER)
        go = apply_filter(oos_trades, wmax, PIVOT_BREAK_BUFFER)
        mi, mo = metrics(gi), metrics(go)
        grid_rows.append(dict(
            wick_max=wmax,
            n_is=mi["n"], pf_is=mi["pf"], pnl_is=mi["pnl"],
            n_oos=mo["n"], pf_oos=mo["pf"], pnl_oos=mo["pnl"], dd_oos=mo["dd"],
        ))
    grid_df = pd.DataFrame(grid_rows)

    # Sélection : meilleur seuil sur IS, contrainte n_is ≥ 30
    candidates = grid_df[grid_df["n_is"] >= 30].copy()
    if len(candidates) == 0:
        chosen = grid_df.loc[grid_df["pf_is"].fillna(0).idxmax()]
    else:
        chosen = candidates.loc[candidates["pf_is"].fillna(0).idxmax()]

    # Bootstrap stationnaire sur OOS du seuil retenu
    chosen_wmax = float(chosen["wick_max"])
    chosen_oos_trades = apply_filter(oos_trades, chosen_wmax, PIVOT_BREAK_BUFFER)
    if len(chosen_oos_trades) >= 10:
        boot = block_bootstrap(chosen_oos_trades, n_iterations=1000,
                                metric="profit_factor", threshold=1.0, seed=42)
        boot_p = float(boot.get("p_above_threshold", float("nan")))
    else:
        boot_p = float("nan")

    # Verdict
    # NB : core.robustness.block_bootstrap retourne p_above_threshold en
    # POURCENTAGE (0-100), pas en fraction. Les seuils ci-dessous sont donc
    # à exprimer sur l'échelle 0-100 (correction bug audit 2026-05-19).
    pf_oos = float(chosen["pf_oos"])
    n_oos = int(chosen["n_oos"])
    if pf_oos >= 1.5 and n_oos >= 20 and boot_p >= 80.0:
        verdict = "🟢 PRODUCTION"
    elif pf_oos >= 1.2 and n_oos >= 8 and boot_p >= 50.0:
        verdict = "🟡 VEILLE"
    else:
        verdict = "🔴 REJET"

    return dict(
        ticker=ticker, tf=tf, fib_level=fib_level,
        n_total=len(trades), n_is=len(is_trades), n_oos=len(oos_trades),
        # Baseline (sans filtre)
        base_pf_is=base_is["pf"], base_pf_oos=base_oos["pf"],
        base_pnl_oos=base_oos["pnl"], base_dd_oos=base_oos["dd"],
        # Stratégie A
        a_pf_is=a_is_m["pf"], a_pf_oos=a_oos_m["pf"], a_n_oos=a_oos_m["n"],
        a_pnl_oos=a_oos_m["pnl"], a_dd_oos=a_oos_m["dd"],
        # Stratégie B (chosen)
        b_wick_max=chosen_wmax, b_pf_is=float(chosen["pf_is"]),
        b_pf_oos=pf_oos, b_n_oos=n_oos, b_pnl_oos=float(chosen["pnl_oos"]),
        b_dd_oos=float(chosen["dd_oos"]),
        boot_p_above_1=boot_p,
        verdict=verdict,
        # Grid détaillé en JSON pour le rapport
        grid=grid_df.to_dict(orient="records"),
    )


def main():
    baseline_csv = OUTPUT_DIR / "fib_v4_baseline.csv"
    baseline = pd.read_csv(baseline_csv)
    viable = baseline[baseline["verdict"].isin(["STRONG", "VIABLE"])].copy()
    print(f"\n  Walk-forward sur {len(viable)} cellules viables.\n")

    results = []
    for _, r in viable.iterrows():
        print(f"  • {r['ticker']} {r['tf']} fib={r['fib_level']} ... ", end="", flush=True)
        res = evaluate_cell(r["ticker"], r["tf"], float(r["fib_level"]))
        results.append(res)
        if res.get("skipped"):
            print(f"SKIP ({res['skipped']})")
        else:
            print(f"verdict={res['verdict']} | A: PF_oos={res['a_pf_oos']:.2f} | "
                  f"B: wick<{res['b_wick_max']:.2f} → PF_oos={res['b_pf_oos']:.2f} "
                  f"(n={res['b_n_oos']}, BS={res['boot_p_above_1']:.2f})")

    # Tableau simple
    simple_rows = [{k: v for k, v in r.items() if k != "grid"} for r in results]
    df = pd.DataFrame(simple_rows)
    csv_path = OUTPUT_DIR / "fib_v4_optimize_results.csv"
    df.to_csv(csv_path, index=False)
    print(f"\n  ✅ CSV : {csv_path}")

    write_markdown_report(results, OUTPUT_DIR / "rapport_fib-v4_optimize.md")


def write_markdown_report(results: list, path: Path) -> None:
    lines = []
    lines.append("# Phase 4 — Walk-forward fib-v4\n")
    lines.append(f"**Filtres testés** :")
    lines.append(f"- Stratégie A (seuil universel) : `wick_through_atr < {WICK_UNIVERSAL}` "
                 f"AND `pivot_break_atr >= -{PIVOT_BREAK_BUFFER}`")
    lines.append(f"- Stratégie B (grid par cellule) : `wick_through_atr ∈ {WICK_GRID}` "
                 f"+ `pivot_break_atr >= -{PIVOT_BREAK_BUFFER}` ; sélection sur IS, validation OOS\n")
    lines.append(f"**Splits IS/OOS** :")
    lines.append(f"- M15 : IS ≤ 2025-09-30 ; OOS 2025-10-01 → 2026-05-31")
    lines.append(f"- M5  : IS ≤ 2026-01-15 (~50%) ; OOS 2026-01-16 → 2026-05-31\n")
    lines.append(f"**Bootstrap stationnaire** : `core.robustness.block_bootstrap` "
                 f"(1000 itérations, block_size auto = √n).\n")
    lines.append(f"**Critères verdict** :")
    lines.append(f"- 🟢 PRODUCTION : PF OOS ≥ 1.5 ET n_oos ≥ 20 ET P(PF>1) ≥ 80%")
    lines.append(f"- 🟡 VEILLE     : PF OOS ≥ 1.2 ET n_oos ≥ 8 ET P(PF>1) ≥ 50%")
    lines.append(f"- 🔴 REJET      : sinon\n")

    # Tableau de synthèse
    lines.append("\n## Tableau de synthèse\n")
    lines.append("| Cellule | n IS | n OOS | Baseline PF OOS | A PF OOS | B seuil | B PF OOS | n OOS B | P(PF>1) | Verdict |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in results:
        if r.get("skipped"):
            lines.append(f"| {r['ticker']} {r['tf']} {r['fib_level']:.3f} "
                         f"| — | — | — | — | — | — | — | — | SKIP ({r['skipped']}) |")
            continue
        lines.append(
            f"| {r['ticker']} {r['tf']} {r['fib_level']:.3f} "
            f"| {r['n_is']} | {r['n_oos']} "
            f"| {r['base_pf_oos']:.2f} | {r['a_pf_oos']:.2f} "
            f"| {r['b_wick_max']:.2f} | {r['b_pf_oos']:.2f} "
            f"| {r['b_n_oos']} | {r['boot_p_above_1']:.2f} "
            f"| {r['verdict']} |"
        )

    # Détail cellule par cellule
    lines.append("\n\n## Détail par cellule (grid wick_through_atr)\n")
    for r in results:
        if r.get("skipped"):
            continue
        lines.append(f"\n### {r['ticker']} {r['tf']} fib={r['fib_level']:.3f} → {r['verdict']}\n")
        lines.append(f"Baseline (sans filtre) — IS: PF={r['base_pf_is']:.2f} | OOS: "
                     f"PF={r['base_pf_oos']:.2f}, P&L=${r['base_pnl_oos']:+,.0f}, DD=${r['base_dd_oos']:+,.0f}\n")
        lines.append(f"Stratégie A (wick<{WICK_UNIVERSAL}) — OOS: PF={r['a_pf_oos']:.2f}, "
                     f"n={r['a_n_oos']}, P&L=${r['a_pnl_oos']:+,.0f}\n")
        lines.append(f"Stratégie B (chosen wick<{r['b_wick_max']}) — OOS: PF={r['b_pf_oos']:.2f}, "
                     f"n={r['b_n_oos']}, P&L=${r['b_pnl_oos']:+,.0f}, DD=${r['b_dd_oos']:+,.0f}, "
                     f"P(PF>1)={r['boot_p_above_1']:.2f}\n")
        lines.append("\nGrid détaillé :\n")
        lines.append("| wick_max | n IS | PF IS | n OOS | PF OOS | P&L OOS |")
        lines.append("|---|---|---|---|---|---|")
        for g in r["grid"]:
            lines.append(
                f"| {g['wick_max']} | {g['n_is']} | {g['pf_is']:.2f} "
                f"| {g['n_oos']} | {g['pf_oos']:.2f} | ${g['pnl_oos']:+,.0f} |"
            )

    # Synthèse des verdicts
    lines.append("\n\n## Synthèse des verdicts\n")
    counts = {"🟢 PRODUCTION": 0, "🟡 VEILLE": 0, "🔴 REJET": 0, "SKIP": 0}
    for r in results:
        if r.get("skipped"):
            counts["SKIP"] += 1
        else:
            counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    for k, v in counts.items():
        lines.append(f"- {k} : {v} cellules")

    path.write_text("\n".join(lines))
    print(f"  ✅ Rapport : {path}")


if __name__ == "__main__":
    main()
