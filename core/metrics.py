"""
Métriques standardisées pour tous les backtests et optimisations.

Toute stratégie passe par ces fonctions — les résultats sont comparables.
Verdict automatique : 🟢 PRODUCTION / 🟡 VEILLE / 🔴 REJET
"""

import numpy as np
import pandas as pd

from config import (
    INSTRUMENTS,
    TOPSTEP_DAILY_LOSS_MAX,
    TOPSTEP_PROFIT_TARGET,
    TOPSTEP_TRAILING_DD,
)

# ── Critères de décision ────────────────────────────────────────────────────
VERDICT_PRODUCTION = {"oos_pf": 1.5, "bootstrap": 0.80, "oos_n": 20}
VERDICT_VEILLE = {"oos_pf": 1.2, "bootstrap": 0.50, "oos_n": 8}


# ══════════════════════════════════════════════════════════════════════════════
# Calcul des métriques
# ══════════════════════════════════════════════════════════════════════════════


def compute_stats(df_trades: pd.DataFrame) -> dict:
    """Métriques de base sur un DataFrame de trades."""
    empty = {
        "n": 0,
        "wr": 0.0,
        "pf": 0.0,
        "pnl": 0.0,
        "dd": 0.0,
        "sharpe": 0.0,
        "avg_win": 0.0,
        "avg_loss": 0.0,
        "n_signals": 0,
    }
    if df_trades is None or len(df_trades) == 0:
        return empty
    if "result" not in df_trades.columns:
        return empty

    n_signals = len(df_trades)
    filled = df_trades[df_trades["result"] != "NOT_FILLED"]
    if len(filled) == 0:
        return {**empty, "n_signals": n_signals}

    wins = filled[filled["pnl"] > 0]
    losses = filled[filled["pnl"] <= 0]
    gp = wins["pnl"].sum() if len(wins) else 0.0
    gl = abs(losses["pnl"].sum()) if len(losses) else 1.0

    cum = filled["pnl"].cumsum()
    dd = float((cum - cum.cummax()).min())

    # Sharpe annualisé sur returns journaliers
    if "date" in filled.columns:
        daily = filled.groupby("date")["pnl"].sum()
        if len(daily) > 1 and daily.std() > 0:
            sharpe = float(daily.mean() / daily.std() * np.sqrt(252))
        else:
            sharpe = 0.0
    else:
        sharpe = 0.0

    return {
        "n": len(filled),
        "n_signals": n_signals,
        "wr": float(len(wins) / len(filled)),
        "pf": float(gp / gl) if gl > 0 else 0.0,
        "pnl": float(filled["pnl"].sum()),
        "dd": dd,
        "sharpe": sharpe,
        "avg_win": float(wins["pnl"].mean()) if len(wins) else 0.0,
        "avg_loss": float(losses["pnl"].mean()) if len(losses) else 0.0,
    }


def compute_topstep(df_trades: pd.DataFrame, n_bootstrap: int = 1000) -> dict:
    """Métriques Topstep challenge 50K + bootstrap."""
    if df_trades is None or len(df_trades) == 0:
        return {
            "n_days": 0,
            "bootstrap_pass_rate": 0.0,
            "violates_daily": False,
            "violates_trailing": False,
        }

    filled = (
        df_trades[df_trades["result"] != "NOT_FILLED"].copy()
        if "result" in df_trades.columns
        else df_trades.copy()
    )
    if len(filled) == 0:
        return {
            "n_days": 0,
            "bootstrap_pass_rate": 0.0,
            "violates_daily": False,
            "violates_trailing": False,
        }

    daily = filled.groupby("date")["pnl"].sum().sort_index()
    days = daily.values

    cum = np.cumsum(days)
    peak = np.maximum.accumulate(cum)
    trailing_dd = float((cum - peak).min())
    max_daily_loss = float(days.min())
    max_daily_gain = float(days.max())

    n_win = int((days > 0).sum())

    max_consec = cur = 0
    for v in days:
        cur = cur + 1 if v < 0 else 0
        max_consec = max(max_consec, cur)

    rng = np.random.default_rng(42)
    pass_count = 0
    for _ in range(n_bootstrap):
        perm = rng.permutation(days)
        c = p = 0.0
        valid = reached = False
        for d in perm:
            if d <= -TOPSTEP_DAILY_LOSS_MAX:
                break
            c += d
            if c > p:
                p = c
            if (p - c) >= TOPSTEP_TRAILING_DD:
                break
            if c >= TOPSTEP_PROFIT_TARGET:
                valid = reached = True
                break
        if valid and reached:
            pass_count += 1

    return {
        "n_days": len(days),
        "n_winning_days": n_win,
        "winning_ratio": float(n_win / len(days)),
        "trailing_dd": trailing_dd,
        "max_daily_loss": max_daily_loss,
        "max_daily_gain": max_daily_gain,
        "max_consec_loss": max_consec,
        "bootstrap_pass_rate": float(pass_count / n_bootstrap),
        "violates_daily": max_daily_loss <= -TOPSTEP_DAILY_LOSS_MAX,
        "violates_trailing": trailing_dd <= -TOPSTEP_TRAILING_DD,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Verdict automatique
# ══════════════════════════════════════════════════════════════════════════════


def verdict(oos_stats: dict, oos_topstep: dict) -> tuple[str, str]:
    """
    Retourne (emoji, label) selon les critères IS/OOS standardisés.

    Critères PRODUCTION : OOS PF ≥ 1.5, Bootstrap ≥ 80%, n OOS ≥ 20, P&L OOS > 0
    Critères VEILLE     : OOS PF ≥ 1.2, Bootstrap ≥ 50%, n OOS ≥ 8,  P&L OOS > 0
    """
    pf = oos_stats.get("pf", 0.0)
    n = oos_stats.get("n", 0)
    pnl = oos_stats.get("pnl", 0.0)
    bs = oos_topstep.get("bootstrap_pass_rate", 0.0)

    if (
        pf >= VERDICT_PRODUCTION["oos_pf"]
        and bs >= VERDICT_PRODUCTION["bootstrap"]
        and n >= VERDICT_PRODUCTION["oos_n"]
        and pnl > 0
    ):
        return "🟢", "PRODUCTION"
    if (
        pf >= VERDICT_VEILLE["oos_pf"]
        and bs >= VERDICT_VEILLE["bootstrap"]
        and n >= VERDICT_VEILLE["oos_n"]
        and pnl > 0
    ):
        return "🟡", "VEILLE"
    return "🔴", "REJET"


def augment_verdict(base_verdict: str, robustness: dict) -> tuple[str, list[str]]:
    """
    Affine le verdict 🟢🟡🔴 à la lumière des analyses de core/robustness.py.

    Rétrograde d'un cran si au moins un drapeau rouge est levé :
      - bootstrap PF observé < seuil corrigé Bonferroni
      - PSR(0) < 80 %
      - probabilité que le DD MC dépasse la limite Topstep > 5 %
      - ≥ 2 régimes en ❌

    Retourne (verdict_ajusté, drapeaux). Si robustness est vide ou en erreur,
    base_verdict est renvoyé inchangé.
    """
    if not robustness:
        return base_verdict, []

    flags = []

    bp = robustness.get("bootstrap_pf", {})
    bf = robustness.get("bonferroni", {})
    if "error" not in bp and bf:
        if bp.get("p_above_threshold", 100.0) < bf.get("bootstrap_threshold_pct", 0.0):
            flags.append(
                f"bootstrap {bp['p_above_threshold']:.1f}% < "
                f"seuil Bonferroni {bf['bootstrap_threshold_pct']:.1f}%"
            )

    psr = robustness.get("psr", {})
    if "error" not in psr and psr.get("psr_pct", 100.0) < 80.0:
        flags.append(f"PSR(0) {psr['psr_pct']:.1f}% < 80%")

    mc = robustness.get("monte_carlo_dd", {})
    if "error" not in mc and mc.get("dd_topstep_breach_pct", 0.0) > 5.0:
        flags.append(f"P(DD > Topstep) {mc['dd_topstep_breach_pct']:.1f}% > 5%")

    regime_fails = sum(1 for r in robustness.get("regime_stress", []) if r.get("verdict") == "❌")
    if regime_fails >= 2:
        flags.append(f"{regime_fails} régimes en échec")

    if not flags:
        return base_verdict, []

    downgrade = {"🟢": "🟡", "🟡": "🔴", "🔴": "🔴"}
    return downgrade.get(base_verdict, base_verdict), flags


# ══════════════════════════════════════════════════════════════════════════════
# Affichage
# ══════════════════════════════════════════════════════════════════════════════


def print_stats_report(stats: dict, ticker: str, strategy_id: str, instruments: dict = None):
    inst = (instruments or INSTRUMENTS).get(ticker, {})
    print(f"\n  {'═' * 55}")
    print(f"  {ticker} — {inst.get('name', ticker)}  [{strategy_id}]")
    print(f"  {'═' * 55}")
    print(f"  Trades  : {stats['n']} remplis / {stats['n_signals']} signaux")
    print(f"  {'─' * 55}")
    print(f"  WR      : {stats['wr'] * 100:.0f}%")
    if stats["avg_win"]:
        print(f"  Avg win : ${stats['avg_win']:+.0f}")
    if stats["avg_loss"]:
        print(f"  Avg loss: ${stats['avg_loss']:.0f}")
    print(f"  PF      : {stats['pf']:.2f}")
    print(f"  Sharpe  : {stats['sharpe']:.2f}")
    print(f"  {'─' * 55}")
    print(f"  P&L     : ${stats['pnl']:+,.0f}")
    print(f"  Max DD  : ${stats['dd']:,.0f}")


def print_topstep_report(ts: dict, ticker: str):
    print(f"\n  {'─' * 55}")
    print(f"  TOPSTEP — Validation challenge 50K ({ticker})")
    print(f"  {'─' * 55}")
    if ts.get("n_days", 0) == 0:
        print("  Aucun trade")
        return
    fd = "❌" if ts["violates_daily"] else "✅"
    ft = "❌" if ts["violates_trailing"] else "✅"
    print(
        f"  Jours         : {ts['n_days']} ({ts['n_winning_days']} win, "
        f"ratio {ts['winning_ratio'] * 100:.0f}%)"
    )
    print(
        f"  Perte jour max: ${ts['max_daily_loss']:+,.0f}   "
        f"{fd} (limite -${TOPSTEP_DAILY_LOSS_MAX})"
    )
    print(f"  Trailing DD   : ${ts['trailing_dd']:+,.0f}   {ft} (limite -${TOPSTEP_TRAILING_DD})")
    print(f"  Consec. loss  : {ts['max_consec_loss']} jours")
    print(f"  Bootstrap     : {ts['bootstrap_pass_rate'] * 100:.1f}%  (cible ≥ 80%)")


def print_portfolio_report(results_by_strategy: dict, n_bootstrap: int = 1000):
    """Rapport portefeuille agrégé multi-stratégies / multi-tickers."""
    for strat_id, results in results_by_strategy.items():
        all_trades = [
            r["df_trades"]
            for r in results
            if r.get("df_trades") is not None and len(r["df_trades"]) > 0
        ]
        if not all_trades:
            continue
        combined = pd.concat(all_trades, ignore_index=True)
        filled = (
            combined[combined["result"] != "NOT_FILLED"]
            if "result" in combined.columns
            else combined
        )

        if len(filled) == 0:
            continue

        daily_all = {}
        for _, row in filled.iterrows():
            d = row.get("date", "?")
            daily_all[d] = daily_all.get(d, 0.0) + float(row["pnl"])

        days = np.array(list(daily_all.values()))
        cum = np.cumsum(days)
        peak = np.maximum.accumulate(cum)
        tdd = float((cum - peak).min())
        mdl = float(days.min())
        n_win = int((days > 0).sum())
        bs = _bootstrap(days, n_bootstrap)

        print(f"\n  {'#' * 60}")
        print(f"  PORTEFEUILLE — {strat_id.upper()}")
        print(f"  {'#' * 60}")
        fd = "❌" if mdl <= -TOPSTEP_DAILY_LOSS_MAX else "✅"
        ft = "❌" if tdd <= -TOPSTEP_TRAILING_DD else "✅"
        print(f"  Jours         : {len(days)} ({n_win} win, ratio {n_win / len(days) * 100:.0f}%)")
        print(f"  P&L total     : ${cum[-1]:+,.0f}")
        print(f"  Perte jour max: ${mdl:+,.0f}   {fd} (limite -${TOPSTEP_DAILY_LOSS_MAX})")
        print(f"  Trailing DD   : ${tdd:+,.0f}   {ft} (limite -${TOPSTEP_TRAILING_DD})")
        print(f"  Bootstrap     : {bs * 100:.1f}%  (cible ≥ 80%, target $+{TOPSTEP_PROFIT_TARGET})")


def _bootstrap(days: np.ndarray, n: int = 1000) -> float:
    rng = np.random.default_rng(42)
    ok = 0
    for _ in range(n):
        perm = rng.permutation(days)
        c = p = 0.0
        valid = reached = False
        for d in perm:
            if d <= -TOPSTEP_DAILY_LOSS_MAX:
                break
            c += d
            if c > p:
                p = c
            if (p - c) >= TOPSTEP_TRAILING_DD:
                break
            if c >= TOPSTEP_PROFIT_TARGET:
                valid = reached = True
                break
        if valid and reached:
            ok += 1
    return ok / n


def print_verdict_report(
    strategy_id: str,
    is_stats: dict,
    oos_stats: dict,
    oos_topstep: dict,
    is_period: str = "",
    oos_period: str = "",
):
    """Rapport de décision post-optimisation."""
    v_emoji, v_label = verdict(oos_stats, oos_topstep)
    print(f"\n  {'═' * 60}")
    print(f"  RAPPORT — {strategy_id}")
    print(f"  {'═' * 60}")
    if is_period:
        print(
            f"  IS  ({is_period}) : "
            f"PF={is_stats['pf']:.2f}  P&L=${is_stats['pnl']:+,.0f}  "
            f"n={is_stats['n']}"
        )
    if oos_period:
        print(
            f"  OOS ({oos_period}) : "
            f"PF={oos_stats['pf']:.2f}  P&L=${oos_stats['pnl']:+,.0f}  "
            f"n={oos_stats['n']}"
        )
    print(f"  Bootstrap OOS  : {oos_topstep.get('bootstrap_pass_rate', 0) * 100:.1f}%")
    print(f"  DD OOS         : ${oos_topstep.get('trailing_dd', 0):,.0f}")
    print(f"  {'─' * 60}")
    print(f"  VERDICT : {v_emoji} {v_label}")

    if v_label == "PRODUCTION":
        print(
            f"  Tous les critères validés "
            f"(OOS PF ≥ {VERDICT_PRODUCTION['oos_pf']}, "
            f"BS ≥ {VERDICT_PRODUCTION['bootstrap'] * 100:.0f}%, "
            f"n ≥ {VERDICT_PRODUCTION['oos_n']})"
        )
    elif v_label == "VEILLE":
        print("  Critères VEILLE validés — à surveiller sur nouvelles données")
    else:
        reasons = []
        if oos_stats.get("pf", 0) < VERDICT_VEILLE["oos_pf"]:
            reasons.append(f"OOS PF={oos_stats.get('pf', 0):.2f} < {VERDICT_VEILLE['oos_pf']}")
        if oos_topstep.get("bootstrap_pass_rate", 0) < VERDICT_VEILLE["bootstrap"]:
            reasons.append(
                f"BS={oos_topstep.get('bootstrap_pass_rate', 0) * 100:.0f}% < "
                f"{VERDICT_VEILLE['bootstrap'] * 100:.0f}%"
            )
        if oos_stats.get("n", 0) < VERDICT_VEILLE["oos_n"]:
            reasons.append(f"n={oos_stats.get('n', 0)} < {VERDICT_VEILLE['oos_n']}")
        if oos_stats.get("pnl", 0) <= 0:
            reasons.append(f"P&L OOS={oos_stats.get('pnl', 0):+.0f} ≤ 0")
        print(f"  Critères non atteints : {' | '.join(reasons)}")
    print(f"  {'═' * 60}")
