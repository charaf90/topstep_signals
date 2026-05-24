#!/usr/bin/env python3
"""
Live-equivalence backtest pour opr-v5.1 (NQ1+YM1) — schéma cancel-before-fill.

Problème :
  En backtest, opr-v5.1 applique le filtre `f2_min_atr` POST-FILL — F2 est
  calculé sur [trigger_ts, fill_ts]. Cette information n'existe pas en live
  AVANT l'exécution du LIMIT : on ne peut pas dé-filler un ordre exécuté.

Schéma testé (cancel-before-fill, conservateur) :
  À la close de chaque bougie M15 après le trigger, recalculer running F2 sur
  les bougies M15 FERMÉES (donc en excluant la bougie courante / future de fill).
  Si à la bougie qui s'apprête à filler, running F2 < seuil → cancel LIMIT.

Implémentation backtest-équivalente :
  F2_live = max excursion sur [trigger_ts, fill_ts) — exclut strictement
  la bougie qui contient fill_ts. Comme F2_post ≥ F2_live par construction,
  un trade rejeté post-fill l'est aussi en live, MAIS un trade validé post-fill
  peut être rejeté en live (les trades A \\ B).

But du script :
  Mesurer la "fidélité" du schéma live-eq vs le backtest v5.1 post-fill :
    - % de trades v5.1 (FILLED) qui restent FILLED en live-equivalent
    - PF / P&L / DD live-equivalent vs backtest
    - Critères auditor : fidélité ≥ 95 % ET PF live-net ≥ 1.8

Usage :
  python -m scripts.live_eq_v5_1 --csv-dir ./data
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

np.random.seed(42)

from config import (
    COMMISSION_RT_PER_CONTRACT,
    OPR_TIMEZONE,
    OPR_V5_1_F2_MIN_ATR,
    SLIPPAGE_TICKS_PER_TICKER,
)
from core.data import build_timeframes, load_csv
from core.metrics import compute_stats
from core.opr import OPR_ATR_PERIOD, _compute_atr_daily
from core.optimizer import OOS_START
from strategies import opr_v5_1 as v51

SELECTED_TICKERS = ["NQ1", "YM1"]
TICK_VALUE = {"MES1": 0.25, "NQ1": 0.25, "YM1": 1.0}  # $ per tick (1 contrat)
DOLLARS_PER_TICK = {"MES1": 1.25, "NQ1": 0.50, "YM1": 0.50}  # $ par tick (ratio standard projet)


# ─────────────────────────────────────────────────────────────────────────────
# Recalcul F2 pre-fill (schéma cancel-before-fill)
# ─────────────────────────────────────────────────────────────────────────────


def compute_f2_pre_fill(
    df_15m_ny: pd.DataFrame,
    trigger_ts: pd.Timestamp,
    fill_ts: pd.Timestamp,
    direction: str,
    opr_high: float,
    opr_low: float,
    atr_daily: float,
) -> float:
    """
    F2_live = max excursion sur [trigger_ts, fill_ts) en ATR daily.
    EXCLUT strictement la bougie qui contient fill_ts (= la bougie de fill).
    """
    if atr_daily is None or atr_daily <= 0:
        return 0.0

    # Bougies strictement avant la bougie de fill
    # NB : index est en NY tz-aware
    bars = df_15m_ny[(df_15m_ny.index >= trigger_ts) & (df_15m_ny.index < fill_ts)]

    if len(bars) == 0:
        # Aucune bougie pré-fill (fill = trigger) → F2_live = 0 → rejeté si seuil > 0
        return 0.0

    if direction == "long":
        excursion_pts = max(float(bars["high"].max()) - opr_high, 0.0)
    else:
        excursion_pts = max(opr_low - float(bars["low"].min()), 0.0)

    return float(excursion_pts / atr_daily)


# ─────────────────────────────────────────────────────────────────────────────
# Friction nette par trade (slippage + commission)
# ─────────────────────────────────────────────────────────────────────────────


def friction_per_trade(ticker: str, n_ct: int) -> float:
    slip_ticks = SLIPPAGE_TICKS_PER_TICKER.get(ticker, 1)
    dollars_per_tick = DOLLARS_PER_TICK.get(ticker, 0.50)
    slip_cost = slip_ticks * dollars_per_tick * 2 * n_ct  # 2 sides
    comm_cost = COMMISSION_RT_PER_CONTRACT * n_ct
    return slip_cost + comm_cost


def add_net_pnl(trades: pd.DataFrame) -> pd.DataFrame:
    """Ajoute colonne pnl_net = pnl - friction."""
    out = trades.copy()
    if len(out) == 0:
        out["pnl_net"] = pd.Series(dtype=float)
        return out
    out["friction"] = out.apply(
        lambda r: (
            friction_per_trade(r.get("ticker", "?"), int(r.get("n_ct", 1) or 1))
            if r.get("result", "NOT_FILLED") != "NOT_FILLED"
            else 0.0
        ),
        axis=1,
    )
    out["pnl_net"] = out["pnl"] - out["friction"]
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline principal
# ─────────────────────────────────────────────────────────────────────────────


def run_live_eq(csv_dir: str, oos_start: str = OOS_START) -> dict:
    tz = ZoneInfo(OPR_TIMEZONE)
    results = {}

    for ticker in SELECTED_TICKERS:
        csv_path = Path(csv_dir) / f"{ticker}_data_m15.csv"
        if not csv_path.exists():
            print(f"  [!] {csv_path} introuvable — skip {ticker}")
            continue

        df_15m = load_csv(str(csv_path))
        tf = build_timeframes(df_15m)

        # Localiser en NY pour les requêtes intra-jour
        if df_15m.index.tz is None:
            df_15m_ny = df_15m.copy()
            df_15m_ny.index = df_15m_ny.index.tz_localize("UTC").tz_convert(tz)
        else:
            df_15m_ny = df_15m.copy()
            df_15m_ny.index = df_15m_ny.index.tz_convert(tz)

        # 1. Run v5.1 backtest (post-fill, params optima config par défaut)
        trades_post = v51.run_backtest(df_15m, ticker, tf=tf, params=None, topstep_guard=False)
        trades_post["ticker"] = ticker

        # Filtrer OOS uniquement
        trades_post_oos = trades_post[trades_post["date"] >= oos_start].copy()
        trades_post_oos = trades_post_oos.reset_index(drop=True)

        # 2. Pour chaque trade FILLED, recalculer F2 pre-fill et ré-appliquer filtre
        f2_min_threshold = OPR_V5_1_F2_MIN_ATR.get(ticker)

        trades_live = trades_post_oos.copy()
        trades_live["f2_pre_fill_atr"] = np.nan
        trades_live["live_rejected"] = False

        if f2_min_threshold is not None:
            for idx, row in trades_post_oos.iterrows():
                if row["result"] == "NOT_FILLED":
                    continue  # déjà rejeté post-fill → idem en live

                # Récupérer timestamps & contexte
                try:
                    trigger_ts = pd.Timestamp(row["trigger_time"])
                    fill_ts = pd.Timestamp(row["fill_time"])
                except Exception:
                    continue

                # Convertir en NY tz si nécessaire
                if trigger_ts.tz is None:
                    trigger_ts = trigger_ts.tz_localize("UTC").tz_convert(tz)
                else:
                    trigger_ts = trigger_ts.tz_convert(tz)
                if fill_ts.tz is None:
                    fill_ts = fill_ts.tz_localize("UTC").tz_convert(tz)
                else:
                    fill_ts = fill_ts.tz_convert(tz)

                opr_high = float(row["zone_high"])
                opr_low = float(row["zone_low"])
                direction = row["dir"]

                # ATR daily pour ce jour (recalcul causal)
                day_ny = pd.Timestamp(row["date"]).tz_localize(tz)
                atr_daily = _compute_atr_daily(df_15m, day_ny, OPR_ATR_PERIOD)

                f2_pre = compute_f2_pre_fill(
                    df_15m_ny=df_15m_ny,
                    trigger_ts=trigger_ts,
                    fill_ts=fill_ts,
                    direction=direction,
                    opr_high=opr_high,
                    opr_low=opr_low,
                    atr_daily=atr_daily if atr_daily is not None else 0.0,
                )
                trades_live.at[idx, "f2_pre_fill_atr"] = f2_pre

                if f2_pre < f2_min_threshold:
                    # Rejeté en live (le LIMIT serait cancellé avant exécution)
                    trades_live.at[idx, "live_rejected"] = True
                    trades_live.at[idx, "result"] = "NOT_FILLED"
                    trades_live.at[idx, "pnl"] = 0.0
                    trades_live.at[idx, "v5_reject_reason"] = "F2_live_too_narrow"
                    trades_live.at[idx, "fill_time"] = None
                    trades_live.at[idx, "exit_time"] = None
                    trades_live.at[idx, "exit"] = None

        results[ticker] = {
            "trades_post": trades_post_oos,
            "trades_live": trades_live,
            "f2_min_threshold": f2_min_threshold,
        }

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────────────────────────


def fmt_money(x):
    return f"${x:+,.0f}"


def print_report(results: dict, output_dir: str):
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    md_lines = [
        "# Live-equivalence backtest — opr-v5.1 (NQ1+YM1)",
        "",
        "## Schéma testé : cancel-before-fill (conservateur)",
        "",
        "F2_live = max excursion sur [trigger_ts, fill_ts) — exclut la bougie de fill.",
        "Un trade FILLED en backtest est REJETÉ en live si F2_live < seuil ticker.",
        "",
        "## Synthèse par ticker (OOS uniquement)",
        "",
        "| Ticker | Post-fill FILLED | Live-eq FILLED | Trades perdus | Fidélité |",
        "|---|---:|---:|---:|---:|",
    ]

    total_post_filled = 0
    total_live_filled = 0
    total_pnl_post = 0.0
    total_pnl_live = 0.0
    total_pnl_post_net = 0.0
    total_pnl_live_net = 0.0
    all_post = []
    all_live = []

    for ticker, r in results.items():
        post = r["trades_post"]
        live = r["trades_live"]
        post_filled = post[post["result"] != "NOT_FILLED"]
        live_filled = live[live["result"] != "NOT_FILLED"]

        n_post = len(post_filled)
        n_live = len(live_filled)
        lost = n_post - n_live
        fidelity = (n_live / n_post * 100) if n_post > 0 else float("nan")

        md_lines.append(f"| {ticker} | {n_post} | {n_live} | {lost} | {fidelity:.1f} % |")

        total_post_filled += n_post
        total_live_filled += n_live
        total_pnl_post += float(post_filled["pnl"].sum())
        total_pnl_live += float(live_filled["pnl"].sum())

        # Net P&L
        post_net = add_net_pnl(post)
        live_net = add_net_pnl(live)
        total_pnl_post_net += float(post_net["pnl_net"].sum())
        total_pnl_live_net += float(live_net["pnl_net"].sum())

        all_post.append(post)
        all_live.append(live)

    fidelity_total = (
        (total_live_filled / total_post_filled * 100) if total_post_filled > 0 else float("nan")
    )
    md_lines.append(
        f"| **Portfolio** | **{total_post_filled}** | **{total_live_filled}** | "
        f"**{total_post_filled - total_live_filled}** | **{fidelity_total:.1f} %** |"
    )

    # Stats agrégées portfolio
    df_post_all = pd.concat(all_post, ignore_index=True) if all_post else pd.DataFrame()
    df_live_all = pd.concat(all_live, ignore_index=True) if all_live else pd.DataFrame()
    stats_post = compute_stats(df_post_all)
    stats_live = compute_stats(df_live_all)

    md_lines += [
        "",
        "## Métriques OOS portfolio (brut, NQ1+YM1)",
        "",
        "| Métrique | Backtest post-fill | Live-equivalent | Delta |",
        "|---|---:|---:|---:|",
        f"| Trades FILLED | {total_post_filled} | {total_live_filled} | "
        f"{total_live_filled - total_post_filled:+d} |",
        f"| P&L brut | {fmt_money(total_pnl_post)} | {fmt_money(total_pnl_live)} | "
        f"{fmt_money(total_pnl_live - total_pnl_post)} |",
        f"| P&L NET (slip+comm) | {fmt_money(total_pnl_post_net)} | {fmt_money(total_pnl_live_net)} | "
        f"{fmt_money(total_pnl_live_net - total_pnl_post_net)} |",
        f"| PF brut | {stats_post.get('pf', float('nan')):.2f} | {stats_live.get('pf', float('nan')):.2f} | "
        f"{stats_live.get('pf', 0) - stats_post.get('pf', 0):+.2f} |",
        f"| WR | {stats_post.get('wr', 0) * 100:.1f} % | {stats_live.get('wr', 0) * 100:.1f} % | "
        f"{(stats_live.get('wr', 0) - stats_post.get('wr', 0)) * 100:+.1f} pp |",
        f"| DD max | {fmt_money(stats_post.get('dd', 0))} | {fmt_money(stats_live.get('dd', 0))} | "
        f"{fmt_money(stats_live.get('dd', 0) - stats_post.get('dd', 0))} |",
    ]

    # Critères auditor
    pf_live_net = (
        (total_pnl_live_net / max(abs(stats_live.get("losses_sum", 1)), 1))
        if stats_live.get("losses_sum")
        else float("nan")
    )
    md_lines += [
        "",
        "## Critères auditor",
        "",
        f"- **Fidélité ≥ 95 %** : {fidelity_total:.1f} % — "
        f"{'✅ PASS' if fidelity_total >= 95.0 else '❌ FAIL'}",
        f"- **PF live brut ≥ 1.8** : {stats_live.get('pf', float('nan')):.2f} — "
        f"{'✅ PASS' if stats_live.get('pf', 0) >= 1.8 else '❌ FAIL'}",
    ]

    # Verdict final
    verdict_fidelity = fidelity_total >= 95.0
    verdict_pf = stats_live.get("pf", 0) >= 1.8
    verdict = (
        "🟢 DÉPLOYABLE"
        if (verdict_fidelity and verdict_pf)
        else (
            "🟡 ACCEPTABLE (un critère manqué)"
            if (verdict_fidelity or verdict_pf)
            else "🔴 NON DÉPLOYABLE (re-design du filtre nécessaire)"
        )
    )
    md_lines += [
        "",
        f"## Verdict final : **{verdict}**",
        "",
        "## Détail par direction du filtrage live",
        "",
    ]

    # Histogramme F2_pre vs F2_post sur les trades concernés
    for ticker, r in results.items():
        live = r["trades_live"]
        threshold = r["f2_min_threshold"]
        filled_post = live[(live["result"] != "NOT_FILLED") | (live["live_rejected"] == True)]
        n_rejected_live = int(live["live_rejected"].sum())

        if threshold is None or len(filled_post) == 0:
            continue

        f2_pre_dist = filled_post["f2_pre_fill_atr"].describe()
        f2_post_dist = filled_post["f2_excursion_atr"].describe()

        md_lines += [
            f"### {ticker} (seuil f2_min_atr = {threshold})",
            "",
            f"Trades rejetés UNIQUEMENT en live (passent post-fill, fail pre-fill) : **{n_rejected_live}**",
            "",
            "Distribution F2 (post-fill vs pre-fill) sur les trades validés post-fill :",
            "",
            "| Stat | F2 post-fill | F2 pre-fill |",
            "|---|---:|---:|",
            f"| n | {int(f2_post_dist['count'])} | {int(f2_pre_dist['count'])} |",
            f"| mean | {f2_post_dist['mean']:.3f} | {f2_pre_dist['mean']:.3f} |",
            f"| min | {f2_post_dist['min']:.3f} | {f2_pre_dist['min']:.3f} |",
            f"| max | {f2_post_dist['max']:.3f} | {f2_pre_dist['max']:.3f} |",
            f"| % below seuil | "
            f"{(filled_post['f2_excursion_atr'] < threshold).mean() * 100:.1f} % | "
            f"{(filled_post['f2_pre_fill_atr'] < threshold).mean() * 100:.1f} % |",
            "",
        ]

    # Save outputs
    md_path = out_path / "live_eq_v5_1.md"
    md_path.write_text("\n".join(md_lines), encoding="utf-8")
    print("\n".join(md_lines))
    print(f"\n  ✓ Rapport écrit : {md_path}")

    # JSON résumé
    summary = {
        "verdict": verdict,
        "fidelity_pct": fidelity_total,
        "trades_post_filled": total_post_filled,
        "trades_live_filled": total_live_filled,
        "pnl_brut_post": total_pnl_post,
        "pnl_brut_live": total_pnl_live,
        "pnl_net_post": total_pnl_post_net,
        "pnl_net_live": total_pnl_live_net,
        "pf_post": stats_post.get("pf", float("nan")),
        "pf_live": stats_live.get("pf", float("nan")),
        "fidelity_pass": verdict_fidelity,
        "pf_pass": verdict_pf,
        "per_ticker": {
            t: {
                "n_post_filled": int(
                    len(r["trades_post"][r["trades_post"]["result"] != "NOT_FILLED"])
                ),
                "n_live_filled": int(
                    len(r["trades_live"][r["trades_live"]["result"] != "NOT_FILLED"])
                ),
                "n_live_rejected": int(r["trades_live"]["live_rejected"].sum()),
                "threshold": r["f2_min_threshold"],
            }
            for t, r in results.items()
        },
    }
    json_path = out_path / "live_eq_v5_1.json"
    json_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(f"  ✓ Résumé JSON : {json_path}")

    # Trades CSV pour audit
    for ticker, r in results.items():
        r["trades_live"].to_csv(out_path / f"trades_live_eq_{ticker}.csv", index=False)


def main():
    parser = argparse.ArgumentParser(description="Live-equivalence backtest opr-v5.1 (NQ1+YM1)")
    parser.add_argument("--csv-dir", type=str, required=True)
    parser.add_argument("--oos-start", type=str, default=OOS_START)
    parser.add_argument("--output-dir", type=str, default="output/no_mes1")
    args = parser.parse_args()

    print("=" * 70)
    print("  LIVE-EQUIVALENCE BACKTEST — opr-v5.1 (NQ1+YM1)")
    print("  Schéma : cancel-before-fill (F2 exclut bougie de fill)")
    print(f"  OOS    : {args.oos_start} → end")
    print("=" * 70)

    results = run_live_eq(args.csv_dir, args.oos_start)
    print_report(results, args.output_dir)


if __name__ == "__main__":
    main()
