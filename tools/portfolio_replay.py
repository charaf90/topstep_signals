#!/usr/bin/env python3
"""
Replay portefeuille multi-STRATÉGIES — risque combiné vs limites Topstep.

Problème adressé : la robustesse du pipeline (optimize.py) est calculée PAR
stratégie (multi-tickers), jamais sur le portefeuille live combiné. Or 3 des
4 stratégies live tradent NQ1+MES1 (corrélés ~0.9) : le MC P95 DD qui a
calibré chaque sizing individuellement ne dit rien du pire-cas conjoint.

Méthode :
  1. Backtest de chaque stratégie LIVE avec ses params/univers/sizing LIVE
     (params=None = défauts config, fidèles prod ; bos_fvg surchargé à
     BOS_FVG_RISK_USD car son défaut recherche est le nominal global).
  2. Agrégation chronologique du P&L NET par jour et par stratégie.
  3. Fenêtre d'analyse = OOS commun (OOS_START → dernière donnée).
  4. Corrélations daily inter-stratégies (union 0-filled = vue portefeuille,
     + intersection = vue co-activité).
  5. Monte-Carlo DD sur les JOURS du portefeuille (permutation de jours —
     préserve la corrélation intra-jour entre stratégies, contrairement à
     une permutation par trade).
  6. Compare aux limites Topstep : DLL $1000 (jours observés + pire jour),
     trailing $2000 (MC P95/P99 + breach %).

Usage :
  python tools/portfolio_replay.py                 # fenêtre OOS standard
  python tools/portfolio_replay.py --start 2025-10-01
  python tools/portfolio_replay.py --dd-remaining 1190   # DD Topstep restant réel

Sortie : output/portfolio_replay/{replay.json, report.md} + résumé console.
À fournir comme input à @athena pour le fit portefeuille d'une candidate.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from config import (
    BOS_FVG_LIVE_TICKERS,
    BOS_FVG_RISK_USD,
    FIB_FINE_LIVE_TICKERS,
    FIB_V4_TICKERS,
    OPR_V5_1_LIVE_TICKERS,
    TOPSTEP_DAILY_LOSS_MAX,
    TOPSTEP_TRAILING_DD,
)
from core import robustness as rb
from core.data import build_timeframes, load_csv
from core.optimizer import OOS_START
from core.registry import load_strategy

np.random.seed(42)

CSV_DIR = Path("data")
OUT_DIR = Path("output/portfolio_replay")

# Portefeuille LIVE — source de vérité : flags/univers/sizings de config.py.
# `load_df` : True = charger le CSV m15 (stratégies M15) ; False = passer
# df=None (la stratégie charge sa propre source — fib_fine resamplé M1,
# bos_fvg via load_tf). `params` : overrides nécessaires à la fidélité live.
LIVE_PORTFOLIO = {
    "opr_v5_1": {
        "tickers": list(OPR_V5_1_LIVE_TICKERS),
        "load_df": True,
        "params": None,
    },
    "fib_v4": {
        "tickers": list(FIB_V4_TICKERS),
        "load_df": True,
        "params": None,
    },
    "fib_fine": {
        "tickers": list(FIB_FINE_LIVE_TICKERS),
        "load_df": False,
        "params": None,  # défaut = FIB_FINE_RISK_PER_TRADE_USD ($130), fidèle live
    },
    "bos_fvg": {
        "tickers": list(BOS_FVG_LIVE_TICKERS),
        "load_df": False,
        "params": {"risk_usd": float(BOS_FVG_RISK_USD)},  # live $150 ≠ défaut recherche $200
    },
}


def collect_daily_pnl(start: str) -> tuple[pd.DataFrame, dict]:
    """Backtest chaque stratégie live → P&L net journalier par stratégie.

    Returns:
        daily  : DataFrame index=date (str), colonnes=stratégies, union 0-filled.
        detail : {strategy: {"n_trades", "pnl", "tickers"}} sur la fenêtre.
    """
    per_strategy: dict[str, pd.Series] = {}
    detail: dict[str, dict] = {}

    for name, spec in LIVE_PORTFOLIO.items():
        module = load_strategy(name)
        tf_suffix = getattr(module, "CSV_TIMEFRAME", "m15")
        parts = []
        for ticker in spec["tickers"]:
            if spec["load_df"]:
                csv_path = CSV_DIR / f"{ticker}_data_{tf_suffix}.csv"
                if not csv_path.exists():
                    print(f"  [!] {csv_path} introuvable — skip {name}/{ticker}")
                    continue
                df = load_csv(str(csv_path))
                tf = build_timeframes(df)
            else:
                df, tf = None, None
            trades = module.run_backtest(
                df, ticker, tf=tf, params=spec["params"], topstep_guard=False
            )
            if len(trades) == 0 or "date" not in trades.columns:
                continue
            trades = trades[trades["result"] != "NOT_FILLED"]
            trades = trades[trades["date"].astype(str) >= start]
            if len(trades):
                parts.append(trades[["date", "pnl"]].assign(ticker=ticker))

        if not parts:
            print(f"  [!] {name} : aucun trade sur la fenêtre — exclu")
            continue
        all_trades = pd.concat(parts, ignore_index=True)
        daily = all_trades.groupby(all_trades["date"].astype(str))["pnl"].sum()
        per_strategy[name] = daily
        detail[name] = {
            "n_trades": int(len(all_trades)),
            "pnl": float(all_trades["pnl"].sum()),
            "n_days_active": int(len(daily)),
            "tickers": spec["tickers"],
        }
        print(
            f"  ✓ {name:<10} {len(all_trades):>4} trades  "
            f"P&L ${all_trades['pnl'].sum():>+10,.0f}  ({len(daily)} jours actifs)"
        )

    daily_df = pd.DataFrame(per_strategy).sort_index().fillna(0.0)
    return daily_df, detail


def correlations(daily_df: pd.DataFrame) -> dict:
    """Corrélations daily par paire : union 0-filled + intersection co-active."""
    out = {}
    for a, b in combinations(daily_df.columns, 2):
        union = float(daily_df[a].corr(daily_df[b]))
        both = daily_df[(daily_df[a] != 0) & (daily_df[b] != 0)]
        inter = float(both[a].corr(both[b])) if len(both) >= 10 else None
        out[f"{a}↔{b}"] = {
            "corr_union": round(union, 3),
            "corr_co_actifs": round(inter, 3) if inter is not None else None,
            "n_jours_co_actifs": int(len(both)),
        }
    return out


def challenge_outcome_mc(
    daily_pnl: np.ndarray,
    dd_remaining: float,
    target_remaining: float,
    daily_loss_max: float = float(TOPSTEP_DAILY_LOSS_MAX),
    trailing_dd: float = float(TOPSTEP_TRAILING_DD),
    n_sims: int = 5000,
    max_days: int = 250,
    seed: int = 42,
) -> dict:
    """P(atteindre le target AVANT de breacher), depuis l'état ACTUEL du compte.

    Le MC DD classique (permutation de toute la fenêtre) sur-estime le risque
    du challenge : il mesure le DD sur ~8 mois alors que le challenge s'arrête
    au target. Ici on simule des chemins (tirage iid des jours observés,
    hypothèse documentée : pas d'autocorrélation des jours — cf. streaks) et
    on s'arrête au premier des trois événements : target, breach trailing,
    breach daily.

    État initial encodé relatif : cum=0 ; peak relatif = trailing_dd −
    dd_remaining (de sorte que le floor initial soit à −dd_remaining).
    """
    rng = np.random.default_rng(seed)
    outcomes = {"target": 0, "breach_trail": 0, "breach_daily": 0, "timeout": 0}
    days_to_target: list[int] = []

    for _ in range(n_sims):
        cum = 0.0
        peak = trailing_dd - dd_remaining  # floor initial = peak - trailing = -dd_remaining
        result = "timeout"
        days = rng.choice(daily_pnl, size=max_days, replace=True)
        for i, day in enumerate(days):
            if day <= -daily_loss_max:
                result = "breach_daily"
                break
            cum += day
            peak = max(peak, cum)
            if cum <= peak - trailing_dd:
                result = "breach_trail"
                break
            if cum >= target_remaining:
                result = "target"
                days_to_target.append(i + 1)
                break
        outcomes[result] += 1

    return {
        "n_sims": int(n_sims),
        "max_days": int(max_days),
        "target_remaining": float(target_remaining),
        "dd_remaining": float(dd_remaining),
        "p_target": round(outcomes["target"] / n_sims, 4),
        "p_breach_trailing": round(outcomes["breach_trail"] / n_sims, 4),
        "p_breach_daily": round(outcomes["breach_daily"] / n_sims, 4),
        "p_timeout": round(outcomes["timeout"] / n_sims, 4),
        "days_to_target_median": float(np.median(days_to_target)) if days_to_target else None,
        "days_to_target_p90": float(np.percentile(days_to_target, 90)) if days_to_target else None,
    }


def run(start: str, dd_remaining: float, target_remaining: float) -> dict:
    print(f"\n{'=' * 66}\n  REPLAY PORTEFEUILLE LIVE — fenêtre {start} → aujourd'hui\n{'=' * 66}")
    daily_df, detail = collect_daily_pnl(start)
    if daily_df.empty:
        raise SystemExit("Aucune donnée — abandon.")

    port = daily_df.sum(axis=1)
    cum = port.cumsum()
    peak = cum.cummax()
    dd_observed = float((cum - peak).min())

    # MC DD sur les jours du portefeuille (corrélation intra-jour préservée)
    mc = rb.monte_carlo_drawdown(
        pd.DataFrame({"pnl": port.to_numpy()}),
        n_iterations=2000,
        topstep_dd_remaining=dd_remaining,
        seed=42,
    )

    worst_day = float(port.min())
    days_below_dll = port[port <= -TOPSTEP_DAILY_LOSS_MAX]

    challenge = challenge_outcome_mc(
        port.to_numpy(), dd_remaining=dd_remaining, target_remaining=target_remaining
    )

    results = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "window_start": start,
        "window_end": str(daily_df.index.max()),
        "strategies": detail,
        "portfolio": {
            "n_days": int(len(port)),
            "pnl_total": float(port.sum()),
            "pnl_daily_mean": float(port.mean()),
            "pnl_daily_std": float(port.std()),
            "worst_day": worst_day,
            "best_day": float(port.max()),
            "n_days_breach_dll": int(len(days_below_dll)),
            "days_breach_dll": [{"date": d, "pnl": float(v)} for d, v in days_below_dll.items()],
            "dd_observed": dd_observed,
        },
        "correlations_daily": correlations(daily_df),
        "mc_drawdown_days": mc,
        "challenge_outcome": challenge,
        "topstep": {
            "daily_loss_max": TOPSTEP_DAILY_LOSS_MAX,
            "trailing_dd": TOPSTEP_TRAILING_DD,
            "dd_remaining_used": dd_remaining,
        },
    }
    return results


def format_report(r: dict) -> str:
    p = r["portfolio"]
    mc = r["mc_drawdown_days"]
    lines = [
        "# Replay portefeuille live — risque combiné multi-stratégies",
        "",
        f"_Généré {r['generated_at']} · fenêtre {r['window_start']} → {r['window_end']}_",
        "",
        "## Stratégies (params/univers/sizing LIVE)",
        "",
    ]
    for name, d in r["strategies"].items():
        lines.append(
            f"- **{name}** ({'+'.join(d['tickers'])}) : {d['n_trades']} trades, "
            f"P&L ${d['pnl']:+,.0f}, {d['n_days_active']} jours actifs"
        )
    lines += [
        "",
        "## Portefeuille combiné",
        "",
        f"- P&L total : **${p['pnl_total']:+,.0f}** sur {p['n_days']} jours",
        f"- Jour moyen ${p['pnl_daily_mean']:+,.0f} · σ ${p['pnl_daily_std']:,.0f}",
        f"- Pire jour : **${p['worst_day']:+,.0f}** (DLL Topstep -${r['topstep']['daily_loss_max']})"
        f" · meilleur jour ${p['best_day']:+,.0f}",
        f"- Jours ≤ -DLL observés : **{p['n_days_breach_dll']}**",
        f"- DD max observé (séquence réelle) : **${p['dd_observed']:+,.0f}**",
        "",
        "## Monte-Carlo DD (permutation de JOURS, 2000 itér.)",
        "",
        f"- DD médian ${mc['dd_median']:+,.0f} · P95 **${mc['dd_p95_worst']:+,.0f}** · "
        f"P99 ${mc['dd_p99_worst']:+,.0f} · pire ${mc['dd_worst']:+,.0f}",
        f"- Breach trailing (limite restante ${mc['topstep_dd_remaining']:,.0f}) : "
        f"**{mc['dd_topstep_breach_pct']:.1f} %**",
        "",
        "## Issue du challenge depuis l'état actuel (MC chemins, arrêt au 1er événement)",
        "",
        f"_Budget DD ${r['challenge_outcome']['dd_remaining']:,.0f} · "
        f"target restant ${r['challenge_outcome']['target_remaining']:,.0f} · "
        f"{r['challenge_outcome']['n_sims']} sims_",
        "",
        f"- **P(target atteint) = {r['challenge_outcome']['p_target'] * 100:.1f} %**"
        + (
            f" (médiane {r['challenge_outcome']['days_to_target_median']:.0f} j, "
            f"P90 {r['challenge_outcome']['days_to_target_p90']:.0f} j)"
            if r["challenge_outcome"]["days_to_target_median"]
            else ""
        ),
        f"- P(breach trailing) = {r['challenge_outcome']['p_breach_trailing'] * 100:.1f} % · "
        f"P(breach daily) = {r['challenge_outcome']['p_breach_daily'] * 100:.1f} % · "
        f"P(timeout {r['challenge_outcome']['max_days']} j) = "
        f"{r['challenge_outcome']['p_timeout'] * 100:.1f} %",
        "",
        "## Corrélations daily inter-stratégies",
        "",
        "| Paire | union (0-filled) | jours co-actifs | corr co-actifs |",
        "|---|---|---|---|",
    ]
    for pair, c in r["correlations_daily"].items():
        co = f"{c['corr_co_actifs']:.2f}" if c["corr_co_actifs"] is not None else "n<10"
        lines.append(f"| {pair} | {c['corr_union']:.2f} | {c['n_jours_co_actifs']} | {co} |")
    lines += [
        "",
        "> Lecture : `union` = corrélation du P&L portefeuille réel (jours sans trade = 0) ;",
        "> `co-actifs` = corrélation conditionnelle aux jours où les DEUX stratégies tradent",
        "> (mesure la redondance d'edge — c'est elle qui doit rester basse).",
    ]
    return "\n".join(lines)


def _account_state() -> tuple[float, float, str]:
    """(dd_remaining, target_remaining, source) — depuis state/live_state.json si
    présent, sinon budget complet. ⚠️ La compta risk_state peut désynchroniser
    des fills Topstep réels (cf. mémoire) — passer --dd-remaining/--target-remaining
    avec les valeurs du dashboard broker pour la vérité terrain."""
    from config import TOPSTEP_PROFIT_TARGET

    try:
        rs = json.loads(Path("state/live_state.json").read_text(encoding="utf-8")).get(
            "risk_state", {}
        )
        cum = float(rs.get("cum_pnl", 0.0))
        peak = float(rs.get("peak_pnl", 0.0))
        dd_rem = max(0.0, TOPSTEP_TRAILING_DD - max(0.0, peak - cum))
        tgt_rem = max(0.0, TOPSTEP_PROFIT_TARGET - cum)
        return dd_rem, tgt_rem, "state/live_state.json"
    except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
        return float(TOPSTEP_TRAILING_DD), 3000.0, "défaut (budget complet)"


def main():
    dd_default, tgt_default, state_source = _account_state()

    ap = argparse.ArgumentParser(description="Replay portefeuille live combiné")
    ap.add_argument("--start", default=OOS_START, help=f"Début de fenêtre (défaut {OOS_START})")
    ap.add_argument(
        "--dd-remaining",
        type=float,
        default=dd_default,
        help=f"DD Topstep restant ($) — défaut {dd_default:.0f} ({state_source})",
    )
    ap.add_argument(
        "--target-remaining",
        type=float,
        default=tgt_default,
        help=f"Profit target restant ($) — défaut {tgt_default:.0f} ({state_source})",
    )
    args = ap.parse_args()

    print(
        f"  État compte ({state_source}) : DD restant ${args.dd_remaining:,.0f} · "
        f"target restant ${args.target_remaining:,.0f}"
    )
    results = run(args.start, args.dd_remaining, args.target_remaining)
    report = format_report(results)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "replay.json").write_text(
        json.dumps(results, indent=2, default=str), encoding="utf-8"
    )
    (OUT_DIR / "report.md").write_text(report, encoding="utf-8")

    print()
    print(report)
    print(f"\n✓ {OUT_DIR}/replay.json + report.md")


if __name__ == "__main__":
    main()
