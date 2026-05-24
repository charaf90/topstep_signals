"""
Optimiseur walk-forward universel.

Contrat attendu du module stratégie :
    PARAM_GRID  : dict[str, list]   — grille d'optimisation
    TICKERS     : list[str]
    STRATEGY_ID : str
    run_backtest(df_15m, ticker, tf=None, params=None,
                 topstep_guard=False) -> pd.DataFrame

Usage :
    from core.optimizer import optimize
    optimize(strategies.opr, data_dict)
"""

import itertools
import json
import time
from pathlib import Path

import pandas as pd
from joblib import Parallel, delayed

from config import OPTIMIZER_PARALLEL_N_JOBS
from core import metrics as m
from core import robustness as rb

# ── Dates walk-forward (cohérentes sur toutes les stratégies) ───────────────
IS_START = "2024-12-01"
IS_END = "2025-09-30"
OOS_START = "2025-10-01"
IS_LABEL = "déc 2024 – sept 2025"
OOS_LABEL = "oct 2025 – mars 2026"


# ══════════════════════════════════════════════════════════════════════════════
# Worker — évalue une combo (ticker × params) → score IS/OOS
# ══════════════════════════════════════════════════════════════════════════════


def _evaluate_combo(combo, keys, dfs, strategy, score_fn, is_end, oos_start):
    """Évalue une combinaison de paramètres sur tous les tickers d'un sous-set.

    Fonction pure — ne touche à aucun état partagé. Conçue pour être
    appelée en parallèle via joblib (workers indépendants).

    Returns:
        dict {"params", "is", "oos", "score"} ou None si aucun ticker n'a
        produit de trades.
    """
    params = dict(zip(keys, combo))
    rows = []
    for ticker, (df_15m, tf) in dfs.items():
        df_all = strategy.run_backtest(df_15m, ticker, tf=tf, params=params, topstep_guard=False)
        if len(df_all) == 0 or "date" not in df_all.columns:
            continue
        df_is = df_all[df_all["date"] <= is_end]
        df_oos = df_all[df_all["date"] >= oos_start]
        rows.append({"ticker": ticker, "is": df_is, "oos": df_oos})
    if not rows:
        return None
    is_combined = pd.concat([r["is"] for r in rows], ignore_index=True)
    oos_combined = pd.concat([r["oos"] for r in rows], ignore_index=True)
    is_s = m.compute_stats(is_combined)
    oos_s = m.compute_stats(oos_combined)
    score = score_fn(is_s, oos_s)
    return {"params": params, "is": is_s, "oos": oos_s, "score": score}


# ══════════════════════════════════════════════════════════════════════════════
# Fonction principale
# ══════════════════════════════════════════════════════════════════════════════


def optimize(
    strategy,
    data: dict,
    is_end: str = IS_END,
    oos_start: str = OOS_START,
    per_ticker: bool = True,
    score_fn=None,
    n_bootstrap: int = 1000,
    robustness_report: bool = True,
    output_dir: str | None = "output",
    n_jobs: int | None = None,
) -> dict:
    """
    Walk-forward IS/OOS sur toutes les combinaisons de PARAM_GRID.

    Args:
        strategy    : module strategies/xxx.py avec PARAM_GRID défini
        data        : {ticker: (df_15m, tf_dict)}
        is_end      : dernière date IS incluse (YYYY-MM-DD)
        oos_start   : première date OOS (YYYY-MM-DD)
        per_ticker  : True = optimise chaque ticker séparément (recommandé)
        score_fn    : fonction de score IS pour classer les combos
                      défaut : oos_pf * oos_pnl (si oos valide) else 0
        n_bootstrap : permutations bootstrap Topstep
        n_jobs      : nombre de workers joblib pour la grille de combos.
                      None → utilise config.OPTIMIZER_PARALLEL_N_JOBS (-1 par
                      défaut = tous les CPU). 1 = séquentiel (debug).

    Returns:
        {ticker: {"params": dict, "is": stats, "oos": stats, "oos_topstep": dict}}
    """
    if n_jobs is None:
        n_jobs = OPTIMIZER_PARALLEL_N_JOBS
    strategy_id = getattr(strategy, "STRATEGY_ID", "unknown")
    param_grid = getattr(strategy, "PARAM_GRID", {})

    if not param_grid:
        print(f"  [{strategy_id}] Aucun PARAM_GRID défini — pas d'optimisation.")
        return {}

    if score_fn is None:
        score_fn = _default_score

    keys = list(param_grid.keys())
    combos = list(itertools.product(*param_grid.values()))
    total = len(combos)

    print(f"\n{'=' * 62}")
    print(f"  OPTIMISATION — {strategy_id}")
    print(f"  IS  : {IS_LABEL}")
    print(f"  OOS : {OOS_LABEL}")
    print(f"  Grille : {total} combinaisons × {len(data)} actifs")
    print(f"{'=' * 62}")

    best_per_ticker = {}

    tickers = list(data.keys())
    if not per_ticker:
        tickers = ["ALL"]  # futur : optimisation globale

    for ticker in tickers:
        if ticker == "ALL":
            dfs = data
        else:
            if ticker not in data:
                continue
            dfs = {ticker: data[ticker]}

        # Bench avant/après documenté dans le log (cible PHASE 2.5 : 4× au minimum
        # sur grilles ≥ 16 combos × 12 CPU).
        mode = "séquentiel" if n_jobs == 1 else f"parallèle (n_jobs={n_jobs})"
        print(f"\n  ▸ Optimisation {ticker} ({total} combos, {mode})...")
        t0 = time.time()

        # Parallélisation joblib : chaque combo est évaluée dans un worker
        # indépendant. Reproductibilité garantie : strategy.run_backtest est
        # déterministe (seed fixé dans chaque module strategies/*.py), et
        # joblib préserve l'ordre des résultats.
        raw_results = Parallel(n_jobs=n_jobs, backend="loky", verbose=0)(
            delayed(_evaluate_combo)(combo, keys, dfs, strategy, score_fn, is_end, oos_start)
            for combo in combos
        )
        results = [r for r in raw_results if r is not None]
        elapsed = time.time() - t0

        if results:
            best_so_far = max(results, key=lambda r: r["score"])
            p_str = "  ".join(f"{k}={v}" for k, v in best_so_far["params"].items())
            print(
                f"    [{len(results):>4}/{total}]  élapsé {elapsed:.1f}s  "
                f"({total / max(elapsed, 0.001):.0f} combos/s)  "
                f"meilleur IS P&L=${best_so_far['is']['pnl']:>+8,.0f}  ({p_str})"
            )

        if not results:
            print(f"  [{ticker}] Aucun résultat.")
            continue

        # Meilleure combo IS — valider OOS
        results.sort(key=lambda r: r["score"], reverse=True)
        best = results[0]

        # Recalcul OOS propre avec params retenus + bootstrap
        oos_trades_list = []
        for t, (df_15m, tf) in dfs.items():
            df_all = strategy.run_backtest(
                df_15m, t, tf=tf, params=best["params"], topstep_guard=False
            )
            if len(df_all) > 0 and "date" in df_all.columns:
                oos_trades_list.append(df_all[df_all["date"] >= oos_start])

        oos_combined = (
            pd.concat(oos_trades_list, ignore_index=True) if oos_trades_list else pd.DataFrame()
        )
        oos_s = m.compute_stats(oos_combined)
        oos_topstep = m.compute_topstep(oos_combined, n_bootstrap=n_bootstrap)

        best_per_ticker[ticker] = {
            "params": best["params"],
            "is": best["is"],
            "oos": oos_s,
            "oos_topstep": oos_topstep,
        }

        _print_ticker_result(ticker, best["params"], best["is"], oos_s, oos_topstep)

    # Rapport global
    _print_global_report(strategy_id, best_per_ticker)

    # Robustesse statistique (post-optimisation, all-in-one) — n'invalide pas
    # le verdict de base, mais le rapport est complet : Bonferroni, PSR,
    # Monte-Carlo, stress par régime, worst-case clustering.
    if robustness_report and best_per_ticker:
        try:
            _run_and_dump_robustness(
                strategy_id=strategy_id,
                strategy=strategy,
                data=data,
                best_per_ticker=best_per_ticker,
                oos_start=oos_start,
                n_combos=len(combos),
                output_dir=output_dir,
            )
        except Exception as exc:
            print(f"  [!] Robustesse non générée : {exc}")

    return best_per_ticker


# ══════════════════════════════════════════════════════════════════════════════
# Robustesse statistique (intégration core/robustness.py)
# ══════════════════════════════════════════════════════════════════════════════


def _run_and_dump_robustness(
    strategy_id: str,
    strategy,
    data: dict,
    best_per_ticker: dict,
    oos_start: str,
    n_combos: int,
    output_dir: str | None,
):
    """
    Recalcule les trades OOS portfolio (all tickers, params optimaux) et lance
    le pipeline core/robustness.py. Exporte un JSON + un Markdown.
    """
    # Recalcul trades OOS portfolio avec les params optimaux par ticker
    parts = []
    for ticker, res in best_per_ticker.items():
        if ticker not in data:
            continue
        df_15m, tf = data[ticker]
        df_all = strategy.run_backtest(
            df_15m, ticker, tf=tf, params=res["params"], topstep_guard=False
        )
        if len(df_all) == 0 or "date" not in df_all.columns:
            continue
        df_oos = df_all[df_all["date"] >= oos_start].copy()
        if "ticker" not in df_oos.columns:
            df_oos["ticker"] = ticker
        parts.append(df_oos)

    if not parts:
        print("  [!] Aucun trade OOS — robustesse non calculée")
        return

    oos_combined = pd.concat(parts, ignore_index=True)
    if "result" in oos_combined.columns:
        oos_combined = oos_combined[oos_combined["result"] != "NOT_FILLED"]
    if len(oos_combined) < 10:
        print(f"  [!] OOS trop court ({len(oos_combined)} trades) — robustesse skipée")
        return

    # Limite Topstep restante : par défaut limite full ; si state/live_state.json
    # est présent on tient compte du DD déjà consommé.
    from config import TOPSTEP_TRAILING_DD

    topstep_dd_remaining = float(TOPSTEP_TRAILING_DD)
    try:
        with open("state/live_state.json", encoding="utf-8") as f:
            live_state = json.load(f)
        rs = live_state.get("risk_state", {})
        cum = float(rs.get("cum_pnl", 0.0))
        peak = float(rs.get("peak_pnl", 0.0))
        dd_consumed = max(0.0, peak - cum)
        topstep_dd_remaining = max(0.0, TOPSTEP_TRAILING_DD - dd_consumed)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    results = rb.run_full_robustness(
        trades=oos_combined,
        n_strategies_tested=max(1, n_combos),
        topstep_dd_remaining=topstep_dd_remaining,
        seed=42,
    )

    md = rb.format_summary_markdown(results)
    print()
    print(md)

    if output_dir:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / f"robustness_{strategy_id}.json").write_text(
            json.dumps(results, indent=2, default=str), encoding="utf-8"
        )
        (out / f"robustness_{strategy_id}.md").write_text(md, encoding="utf-8")
        print(f"  ✓ output/robustness_{strategy_id}.json + .md")


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════


def _default_score(is_s: dict, oos_s: dict) -> float:
    """Score IS utilisé pour le classement. Pénalise si OOS négatif."""
    if oos_s["pnl"] <= 0 or oos_s["pf"] < 1.0:
        return 0.0
    return is_s["pf"] * is_s["pnl"] if is_s["pnl"] > 0 else 0.0


def _print_ticker_result(ticker, params, is_s, oos_s, oos_topstep):
    p_str = "  ".join(f"{k}={v}" for k, v in params.items())
    print(f"\n  Résultat {ticker} : {p_str}")
    print(
        f"    IS  : n={is_s['n']:>4}  WR={is_s['wr'] * 100:.0f}%  "
        f"PF={is_s['pf']:.2f}  P&L=${is_s['pnl']:>+,.0f}"
    )
    print(
        f"    OOS : n={oos_s['n']:>4}  WR={oos_s['wr'] * 100:.0f}%  "
        f"PF={oos_s['pf']:.2f}  P&L=${oos_s['pnl']:>+,.0f}  "
        f"BS={oos_topstep['bootstrap_pass_rate'] * 100:.0f}%"
    )


def _print_global_report(strategy_id: str, best_per_ticker: dict):
    if not best_per_ticker:
        return

    print(f"\n{'=' * 62}")
    print(f"  RAPPORT FINAL — {strategy_id}")
    print(f"{'=' * 62}")

    # Agréger IS et OOS sur tous les tickers
    is_all = {"n": 0, "pnl": 0.0}
    oos_all = {"n": 0, "pnl": 0.0}
    oos_topstep_combined = {"bootstrap_pass_rate": 0.0}

    for ticker, res in best_per_ticker.items():
        is_all["n"] += res["is"]["n"]
        is_all["pnl"] += res["is"]["pnl"]
        oos_all["n"] += res["oos"]["n"]
        oos_all["pnl"] += res["oos"]["pnl"]

    # Verdict global (sur la moyenne des OOS)
    if best_per_ticker:
        avg_oos_pf = sum(r["oos"]["pf"] for r in best_per_ticker.values()) / len(best_per_ticker)
        # Moyenne arithmétique des taux de passage du challenge Topstep par ticker (indicatif).
        # ⚠ Ce n'est PAS un block-bootstrap portefeuille agrégé — le vrai bootstrap
        #   portefeuille est dans output/robustness_<strategy_id>.json (core/robustness.py).
        topstep_bs_per_ticker = sum(
            r["oos_topstep"]["bootstrap_pass_rate"] for r in best_per_ticker.values()
        ) / len(best_per_ticker)
        oos_global = {**oos_all, "pf": avg_oos_pf}
        oos_ts_global = {
            "bootstrap_pass_rate": topstep_bs_per_ticker,
            "trailing_dd": min(
                r["oos_topstep"].get("trailing_dd", 0) for r in best_per_ticker.values()
            ),
        }
        m.print_verdict_report(
            strategy_id,
            is_stats={
                **is_all,
                "pf": sum(r["is"]["pf"] for r in best_per_ticker.values()) / len(best_per_ticker),
            },
            oos_stats=oos_global,
            oos_topstep=oos_ts_global,
            is_period=IS_LABEL,
            oos_period=OOS_LABEL,
        )
        print("  ⚠  Bootstrap ci-dessus = moyenne Topstep challenge per-ticker (indicatif).")
        print(
            f"     Bootstrap portfolio agrégé (block-bootstrap) → output/robustness_{strategy_id}.json"
        )

    print("\n  Paramètres retenus :")
    for ticker, res in best_per_ticker.items():
        p_str = "  ".join(f"{k}={v}" for k, v in res["params"].items())
        print(f"    {ticker} : {p_str}")
    print("\n  → Reporter ces valeurs dans config.py")
