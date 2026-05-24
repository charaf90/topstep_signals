"""
Simulation Monte Carlo du mode challenge adaptatif.

Compare le sizing adaptatif vs sizing statique ($100/trade) sur des challenges
synthétiques de 30 jours, en bootstrappant les outcomes des trades OOS réels
(OPR + Fib) depuis output/backtest_*.csv.

Métriques :
  - P(target hit) : % de challenges où cum_pnl atteint +$3000
  - P(bust)       : % où une limite Topstep est franchie
  - EV net mensuel après abonnement ($165)
  - Distribution durée avant target

Usage :
  python scripts/simulate_challenge_mode.py --n 10000 --seed 42
  python scripts/simulate_challenge_mode.py --n 5000 --gamma 0.5   # sensibilité
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# Permet d'importer config / core depuis scripts/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import (  # noqa: E402
    TOPSTEP_DAILY_LOSS_MAX,
    TOPSTEP_PROFIT_TARGET,
    TOPSTEP_TRAILING_DD,
)
from core.adaptive_sizing import adaptive_risk_usd  # noqa: E402

# Coût d'abonnement Topstep — modélise le "vrai" gain mensuel
TOPSTEP_SUBSCRIPTION_USD = 165.0


@dataclass
class TradeOutcome:
    strategy: str
    outcome: str  # TP / SL / TE / BE
    rr: float  # ratio tp_dist / sl_dist
    pnl_per_dollar: float  # gain par dollar de risque (TP=+rr, SL=-1, BE=0, TE≈-0.3)


def load_bootstrap_pool(output_dir: Path) -> dict[str, list[TradeOutcome]]:
    """
    Charge les trades OOS réels depuis output/backtest_*.csv et calcule
    pnl_per_dollar pour chaque trade. Retourne {strategy: [TradeOutcome]}.
    """
    pool: dict[str, list[TradeOutcome]] = {"OPR": [], "FIB": []}
    files = list(output_dir.glob("backtest_*_opr.csv")) + list(
        output_dir.glob("backtest_*_fib.csv")
    )
    for f in files:
        strat = "OPR" if "_opr" in f.name else "FIB"
        df = pd.read_csv(f)
        # OOS uniquement (post 2025-10-01)
        df["date"] = pd.to_datetime(df["date"])
        df = df[df["date"] >= "2025-10-01"]
        for _, row in df.iterrows():
            outcome = str(row.get("result", ""))
            rr = float(row.get("rr", 0))
            if outcome == "TP":
                ppd = rr
            elif outcome == "SL":
                ppd = -1.0
            elif outcome == "BE":
                ppd = 0.0
            elif outcome == "TE":
                # Timeout : approximation = -30% du risque (réel ~ -10 à -50%)
                ppd = -0.3
            else:
                continue
            pool[strat].append(TradeOutcome(strat, outcome, rr, ppd))
    return pool


@dataclass
class SimState:
    cum_pnl: float = 0.0
    peak_pnl: float = 0.0
    realized_day_pnl: float = 0.0
    busted: bool = False
    target_hit: bool = False
    target_day: int = -1
    bust_reason: str = ""

    def to_status(self) -> dict:
        return {
            "cum_pnl": self.cum_pnl,
            "peak_pnl": self.peak_pnl,
            "realized_day_pnl": self.realized_day_pnl,
        }


def check_breach(state: SimState) -> tuple[bool, str]:
    """Vérifie les limites Topstep dures."""
    if state.realized_day_pnl <= -TOPSTEP_DAILY_LOSS_MAX:
        return True, "daily_loss"
    trail_floor = state.peak_pnl - TOPSTEP_TRAILING_DD
    if state.cum_pnl <= trail_floor:
        return True, "trailing_dd"
    return False, ""


def simulate_one_challenge(
    pool: dict[str, list[TradeOutcome]],
    rng: np.random.Generator,
    n_days: int = 22,  # jours ouvrés dans un mois
    trades_per_day: float = 3.0,  # lambda Poisson
    sizing_mode: str = "adaptive",  # "adaptive" | "static"
    static_risk_usd: float = 100.0,
    start_date: date = date(2026, 6, 1),
) -> SimState:
    """Simule un challenge synthétique de n_days jours ouvrés."""
    state = SimState()
    current = start_date
    for day_idx in range(n_days):
        # Skip weekend
        while current.weekday() >= 5:
            current += timedelta(days=1)

        # Reset daily P&L
        state.realized_day_pnl = 0.0

        # Tirer le nb de trades du jour pour chaque stratégie (Poisson séparé)
        n_opr = rng.poisson(trades_per_day * 0.7)  # ~70% OPR
        n_fib = rng.poisson(trades_per_day * 0.3)  # ~30% Fib

        trades_today = [("OPR", t) for t in rng.choice(pool["OPR"], size=n_opr, replace=True)] + [
            ("FIB", t) for t in rng.choice(pool["FIB"], size=n_fib, replace=True)
        ]
        rng.shuffle(trades_today)

        today_dt = datetime.combine(current, datetime.min.time().replace(hour=14))
        for strat, trade in trades_today:
            if state.busted or state.target_hit:
                break
            # Sizing
            signal = {
                "strategy": strat,
                "sl_dist": 20.0,
            }  # sl_dist factice (utilisé seulement par n_ct dans live_runner)
            if sizing_mode == "adaptive":
                risk_usd, _ = adaptive_risk_usd(state.to_status(), signal, today_dt)
            else:
                risk_usd = static_risk_usd

            # Outcome du trade : pnl = risk_usd × pnl_per_dollar
            pnl = risk_usd * trade.pnl_per_dollar
            state.cum_pnl += pnl
            state.realized_day_pnl += pnl
            if state.cum_pnl > state.peak_pnl:
                state.peak_pnl = state.cum_pnl

            # Check target
            if state.cum_pnl >= TOPSTEP_PROFIT_TARGET and not state.target_hit:
                state.target_hit = True
                state.target_day = day_idx
                break

            # Check breach Topstep
            breach, reason = check_breach(state)
            if breach:
                state.busted = True
                state.bust_reason = reason
                break

        current += timedelta(days=1)
        if state.target_hit or state.busted:
            break

    return state


def run_simulation(
    n: int,
    seed: int,
    output_dir: Path,
    sizing_mode: str = "adaptive",
) -> dict:
    rng = np.random.default_rng(seed)
    pool = load_bootstrap_pool(output_dir)
    print(
        f"Pool OPR : {len(pool['OPR'])} trades | Pool FIB : {len(pool['FIB'])} trades",
        file=sys.stderr,
    )
    if len(pool["OPR"]) < 30 or len(pool["FIB"]) < 5:
        print("⚠️ Pool trop petit — résultats peu fiables", file=sys.stderr)

    states = [simulate_one_challenge(pool, rng, sizing_mode=sizing_mode) for _ in range(n)]

    target = sum(s.target_hit for s in states) / n
    bust = sum(s.busted for s in states) / n
    bust_daily = sum(s.busted and s.bust_reason == "daily_loss" for s in states) / n
    bust_trail = sum(s.busted and s.bust_reason == "trailing_dd" for s in states) / n
    final_pnls = np.array([s.cum_pnl for s in states])
    target_days = np.array([s.target_day for s in states if s.target_hit])

    return {
        "n": n,
        "sizing": sizing_mode,
        "P(target)": target,
        "P(bust)": bust,
        "P(bust_daily)": bust_daily,
        "P(bust_trail)": bust_trail,
        "EV_pnl_mean": float(final_pnls.mean()),
        "EV_pnl_p25": float(np.percentile(final_pnls, 25)),
        "EV_pnl_p50": float(np.percentile(final_pnls, 50)),
        "EV_pnl_p75": float(np.percentile(final_pnls, 75)),
        "EV_net_after_sub": float(final_pnls.mean() - TOPSTEP_SUBSCRIPTION_USD),
        "median_days_to_target": (float(np.median(target_days)) if len(target_days) > 0 else -1),
    }


def print_result(r: dict):
    print(f"\n=== Résultat {r['sizing']} (n={r['n']}) ===")
    print(f"  P(target)        : {r['P(target)'] * 100:5.1f}%")
    print(f"  P(bust)          : {r['P(bust)'] * 100:5.1f}%")
    print(f"    daily          : {r['P(bust_daily)'] * 100:5.1f}%")
    print(f"    trailing DD    : {r['P(bust_trail)'] * 100:5.1f}%")
    print(f"  EV PnL mean      : ${r['EV_pnl_mean']:+8.0f}")
    print(
        f"  EV PnL p25/p50/p75: ${r['EV_pnl_p25']:+.0f} / ${r['EV_pnl_p50']:+.0f} / ${r['EV_pnl_p75']:+.0f}"
    )
    print(f"  EV net after sub : ${r['EV_net_after_sub']:+8.0f}")
    if r["median_days_to_target"] >= 0:
        print(f"  Médiane jours    : {r['median_days_to_target']:.0f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=10000, help="Nombre de challenges synthétiques")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--output-dir", type=Path, default=Path("output"))
    args = ap.parse_args()

    r_adapt = run_simulation(args.n, args.seed, args.output_dir, "adaptive")
    r_static = run_simulation(args.n, args.seed, args.output_dir, "static")

    print_result(r_static)
    print_result(r_adapt)

    # Comparaison
    print("\n=== Comparaison adaptive vs static ===")
    print(f"  Δ P(target)      : {(r_adapt['P(target)'] - r_static['P(target)']) * 100:+5.1f} pp")
    print(f"  Δ P(bust)        : {(r_adapt['P(bust)'] - r_static['P(bust)']) * 100:+5.1f} pp")
    print(
        f"  Δ EV net         : ${(r_adapt['EV_net_after_sub'] - r_static['EV_net_after_sub']):+8.0f}"
    )

    # Critères de promotion
    print("\n=== Critères de promotion ===")
    ok_target = r_adapt["P(target)"] >= 0.35
    ok_bust = r_adapt["P(bust)"] <= 0.15
    ok_ev = r_adapt["EV_net_after_sub"] > 0
    ok_no_regression = (r_adapt["P(bust)"] - r_static["P(bust)"]) <= 0.05
    for name, ok in [
        ("P(target) ≥ 35%", ok_target),
        ("P(bust) ≤ 15%", ok_bust),
        ("EV net > 0", ok_ev),
        ("Pas de régression P(bust) > 5pp", ok_no_regression),
    ]:
        print(f"  {'✅' if ok else '❌'} {name}")
    all_ok = ok_target and ok_bust and ok_ev and ok_no_regression
    print(f"\n{'✅ GO en prod' if all_ok else '⚠️  À ajuster avant prod'}")


if __name__ == "__main__":
    main()
