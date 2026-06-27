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

from config import (
    OPTIMIZER_GRID_MAX_COMBOS,
    OPTIMIZER_N_TRIALS,
    OPTIMIZER_OPTUNA_SEED,
    OPTIMIZER_PARALLEL_N_JOBS,
    WF_HOLDOUT_START,
    WF_IS_END,
    WF_IS_START,
    WF_OOS_START,
)
from core import bt_engine
from core import metrics as m
from core import robustness as rb

# ── Dates walk-forward (source de vérité : config.py, section WALK-FORWARD) ──
IS_START = WF_IS_START
IS_END = WF_IS_END
OOS_START = WF_OOS_START
HOLDOUT_START = WF_HOLDOUT_START
IS_LABEL = f"{IS_START} – {IS_END}"
OOS_LABEL = f"{OOS_START} – {HOLDOUT_START} (hold-out exclu)"


# ══════════════════════════════════════════════════════════════════════════════
# Worker — évalue une combo (ticker × params) → score IS/OOS
# ══════════════════════════════════════════════════════════════════════════════


def _evaluate_combo(
    combo, keys, dfs, strategy, score_fn, is_end, oos_start, is_start=IS_START, oos_end=None
):
    """Évalue une combinaison de paramètres sur tous les tickers d'un sous-set.

    Fonction pure — ne touche à aucun état partagé. Conçue pour être
    appelée en parallèle via joblib (workers indépendants).

    Args:
        is_start : borne basse IS (défaut IS_START — no-op historique, le m15
                   démarre sept 2024 ; indispensable pour le multi-fold).
        oos_end  : borne haute EXCLUSIVE de l'OOS (None = jusqu'au bout).
                   Sert au hold-out terminal et aux folds.

    Returns:
        dict {"params", "is", "oos", "score"} ou None si aucun ticker n'a
        produit de trades.
    """
    params = dict(zip(keys, combo))
    rows = []
    for ticker, (df_15m, tf) in dfs.items():
        df_all = bt_engine.run_trades(strategy, ticker, df_15m=df_15m, tf=tf, params=params)
        if len(df_all) == 0 or "date" not in df_all.columns:
            continue
        dates = df_all["date"].astype(str)
        df_is = df_all[(dates >= is_start) & (dates <= is_end)]
        oos_mask = dates >= oos_start
        if oos_end is not None:
            oos_mask &= dates < oos_end
        df_oos = df_all[oos_mask]
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
    is_start: str = IS_START,
    oos_end: str | None = HOLDOUT_START,
    evaluate_holdout: bool = False,
    search: str = "auto",
    n_trials: int | None = None,
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
        is_start    : borne basse IS (défaut WF_IS_START — no-op historique).
        oos_end     : borne haute EXCLUSIVE de l'OOS de sélection/robustesse.
                      Défaut WF_HOLDOUT_START : le hold-out terminal n'est
                      JAMAIS consommé par la sélection. None = ancien
                      comportement (OOS jusqu'au bout des données).
        evaluate_holdout : True = évalue UNE fois les params retenus sur le
                      hold-out [oos_end, ∞) et l'affiche. À ne faire qu'en
                      pré-promotion — chaque consultation consomme le hold-out.
        search      : "grid" (exhaustif, chemin historique), "optuna" (TPE,
                      core/search_optuna.py) ou "auto" (grid si grille ≤
                      OPTIMIZER_GRID_MAX_COMBOS et pas de PARAM_SPACE).
        n_trials    : trials TPE par ticker (None → OPTIMIZER_N_TRIALS).

    Returns:
        {ticker: {"params": dict, "is": stats, "oos": stats, "oos_topstep": dict}}
    """
    if n_jobs is None:
        n_jobs = OPTIMIZER_PARALLEL_N_JOBS
    strategy_id = getattr(strategy, "STRATEGY_ID", "unknown")
    param_grid = getattr(strategy, "PARAM_GRID", {})
    param_space = getattr(strategy, "PARAM_SPACE", None)

    if not param_grid and not param_space:
        print(f"  [{strategy_id}] Aucun PARAM_GRID/PARAM_SPACE défini — pas d'optimisation.")
        return {}

    if score_fn is None:
        score_fn = _default_score

    keys = list(param_grid.keys())
    combos = list(itertools.product(*param_grid.values())) if param_grid else []
    total = len(combos)

    backend = _resolve_search(search, strategy, total)
    if backend == "grid" and not combos:
        print(
            f"  [{strategy_id}] PARAM_SPACE seul défini — backend grid impossible "
            "(utiliser --search optuna)."
        )
        return {}
    if n_trials is None:
        n_trials = OPTIMIZER_N_TRIALS

    oos_label = f"{oos_start} – {oos_end} (hold-out exclu)" if oos_end else f"{oos_start} – fin"
    print(f"\n{'=' * 62}")
    print(f"  OPTIMISATION — {strategy_id}")
    print(f"  IS  : {is_start} – {is_end}")
    print(f"  OOS : {oos_label}")
    if backend == "grid":
        print(f"  Grille : {total} combinaisons × {len(data)} actifs (grid exhaustif)")
    else:
        print(
            f"  Recherche : Optuna TPE — {n_trials} trials × {len(data)} actifs "
            f"(seed={OPTIMIZER_OPTUNA_SEED})"
        )
    print(f"{'=' * 62}")

    best_per_ticker = {}
    n_tested_by_ticker: dict[str, int] = {}

    tickers = list(data.keys())
    if not per_ticker:
        tickers = ["ALL"]  # futur : optimisation globale

    # Backend optuna : studies par ticker indépendantes → lancées en parallèle
    # ICI (les trials restent séquentiels DANS chaque study = déterminisme, cf.
    # core/search_optuna.py). Les studies sont créées dans les workers et seuls
    # des dicts purs en sortent (jamais d'objet Study picklé par loky).
    search_out: dict[str, dict] = {}
    if backend == "optuna":
        from core.search_optuna import run_optuna_search

        payloads = [
            (t, data if t == "ALL" else {t: data[t]}) for t in tickers if t == "ALL" or t in data
        ]
        outs = Parallel(n_jobs=n_jobs, backend="loky", verbose=0)(
            delayed(run_optuna_search)(
                strategy,
                dfs,
                score_fn,
                is_end,
                oos_start,
                is_start,
                oos_end,
                n_trials,
                seed=OPTIMIZER_OPTUNA_SEED,
                label=t,
            )
            for t, dfs in payloads
        )
        search_out = {t: o for (t, _), o in zip(payloads, outs)}

    for ticker in tickers:
        if ticker == "ALL":
            dfs = data
        else:
            if ticker not in data:
                continue
            dfs = {ticker: data[ticker]}

        if backend == "grid":
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
                delayed(_evaluate_combo)(
                    combo, keys, dfs, strategy, score_fn, is_end, oos_start, is_start, oos_end
                )
                for combo in combos
            )
            results = [r for r in raw_results if r is not None]
            elapsed = time.time() - t0
            n_tested = total
            trials_log = None
        else:
            out_t = search_out.get(ticker)
            if out_t is None:
                continue
            results = out_t["results"]
            trials_log = out_t["trials"]
            n_tested = out_t["n_evaluated"]
            elapsed = out_t["elapsed"]
            print(
                f"\n  ▸ Optimisation {ticker} (optuna TPE : {len(trials_log)} trials, "
                f"{n_tested} jeux distincts évalués)..."
            )

        if results:
            best_so_far = max(results, key=lambda r: r["score"])
            p_str = "  ".join(f"{k}={v}" for k, v in best_so_far["params"].items())
            print(
                f"    [{len(results):>4}/{n_tested}]  élapsé {elapsed:.1f}s  "
                f"({n_tested / max(elapsed, 0.001):.0f} combos/s)  "
                f"meilleur IS P&L=${best_so_far['is']['pnl']:>+8,.0f}  ({p_str})"
            )

        if not results:
            print(f"  [{ticker}] Aucun résultat.")
            continue

        n_tested_by_ticker[ticker] = n_tested

        # Historique des trials → artefact d'audit (params + score IS par trial)
        if trials_log is not None and output_dir:
            out = Path(output_dir)
            out.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(trials_log).to_csv(out / f"trials_{strategy_id}_{ticker}.csv", index=False)

        # Meilleure combo IS — valider OOS
        results.sort(key=lambda r: r["score"], reverse=True)
        best = results[0]

        # Recalcul OOS propre avec params retenus + bootstrap
        oos_trades_list = []
        for t, (df_15m, tf) in dfs.items():
            df_all = bt_engine.run_trades(strategy, t, df_15m=df_15m, tf=tf, params=best["params"])
            if len(df_all) > 0 and "date" in df_all.columns:
                dates = df_all["date"].astype(str)
                mask = dates >= oos_start
                if oos_end is not None:
                    mask &= dates < oos_end
                oos_trades_list.append(df_all[mask])

        oos_combined = (
            pd.concat(oos_trades_list, ignore_index=True) if oos_trades_list else pd.DataFrame()
        )
        oos_s = m.compute_stats(oos_combined)
        oos_topstep = m.compute_topstep(oos_combined, n_bootstrap=n_bootstrap)

        plateau = _plateau_analysis(results, best)

        best_per_ticker[ticker] = {
            "params": best["params"],
            "is": best["is"],
            "oos": oos_s,
            "oos_topstep": oos_topstep,
            "plateau": plateau,
        }

        _print_ticker_result(ticker, best["params"], best["is"], oos_s, oos_topstep, plateau)

    # Rapport global
    _print_global_report(strategy_id, best_per_ticker)

    # Hold-out terminal — consultation EXPLICITE et unique (pré-promotion).
    # Jamais utilisé pour la sélection : simple évaluation des params retenus.
    if evaluate_holdout and oos_end is not None and best_per_ticker:
        _evaluate_holdout(strategy, data, best_per_ticker, holdout_start=oos_end)

    # Robustesse statistique (post-optimisation, all-in-one) — n'invalide pas
    # le verdict de base, mais le rapport est complet : Bonferroni, PSR,
    # Monte-Carlo, stress par régime, worst-case clustering.
    if robustness_report and best_per_ticker:
        # Compte honnête d'hypothèses testées pour Bonferroni/DSR : taille de
        # grille en grid (inchangé), jeux de params distincts évalués en optuna.
        n_tested_global = max(n_tested_by_ticker.values(), default=total)
        try:
            _run_and_dump_robustness(
                strategy_id=strategy_id,
                strategy=strategy,
                data=data,
                best_per_ticker=best_per_ticker,
                oos_start=oos_start,
                n_combos=n_tested_global,
                output_dir=output_dir,
                oos_end=oos_end,
                extra={
                    "search": {
                        "backend": backend,
                        "n_tested": n_tested_global,
                        "n_trials": n_trials if backend == "optuna" else None,
                    },
                    "plateau": {t: r.get("plateau") for t, r in best_per_ticker.items()},
                },
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
    oos_end: str | None = None,
    extra: dict | None = None,
):
    """
    Recalcule les trades OOS portfolio (all tickers, params optimaux) et lance
    le pipeline core/robustness.py. Exporte un JSON + un Markdown.
    `oos_end` (exclusif) borne l'OOS au hold-out, comme la sélection.
    `extra` (optionnel) : sections additionnelles mergées dans le JSON et
    appendées au Markdown (clés "search" et "plateau") — core/robustness.py
    reste intact.
    """
    # Recalcul trades OOS portfolio avec les params optimaux par ticker
    parts = []
    for ticker, res in best_per_ticker.items():
        if ticker not in data:
            continue
        df_15m, tf = data[ticker]
        df_all = bt_engine.run_trades(strategy, ticker, df_15m=df_15m, tf=tf, params=res["params"])
        if len(df_all) == 0 or "date" not in df_all.columns:
            continue
        dates = df_all["date"].astype(str)
        mask = dates >= oos_start
        if oos_end is not None:
            mask &= dates < oos_end
        df_oos = df_all[mask].copy()
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

    if extra:
        results.update(extra)

    md = rb.format_summary_markdown(results)
    if extra:
        md += _format_extra_markdown(extra)
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


def _resolve_search(search: str, strategy, n_grid_combos: int) -> str:
    """Choisit le backend de recherche ("grid" | "optuna").

    "auto" → optuna si la stratégie définit un PARAM_SPACE ou si la grille
    dépasse OPTIMIZER_GRID_MAX_COMBOS ; sinon grid (chemin historique intact).
    Optuna manquant : erreur claire si demandé explicitement, fallback grid
    (avec warning) en auto.
    """
    if search == "grid":
        return "grid"
    if search not in ("optuna", "auto"):
        raise ValueError(f"search={search!r} inconnu (grid|optuna|auto)")
    wants_optuna = search == "optuna" or (
        getattr(strategy, "PARAM_SPACE", None) or n_grid_combos > OPTIMIZER_GRID_MAX_COMBOS
    )
    if not wants_optuna:
        return "grid"
    try:
        from core.search_optuna import _require_optuna

        _require_optuna()
        return "optuna"
    except ImportError:
        if search == "optuna":
            raise
        print("  [!] optuna absent — fallback grid exhaustif (pip install 'optuna>=4.0')")
        return "grid"


def _plateau_analysis(results: list[dict], best: dict, k: int = 8) -> dict:
    """Le best est-il sur un plateau de l'espace params, ou un pic isolé ?

    Distance L1 normalisée entre jeux de params : dimensions numériques
    min-max-scalées sur les valeurs observées, autres (cat/None/bool/str) 0/1.
    Compare le score IS médian des k plus proches voisins au score du best :
    un pic isolé (voisins ≪ best) signe une combinaison sur-fittée. Verdict
    informatif — n'invalide pas la sélection. Search-agnostique (grid/optuna).
    """
    if best["score"] <= 0 or len(results) < 4:
        return {"k": 0, "plateau_ratio": None, "verdict": "n/a"}

    keys = list(best["params"].keys())
    num_bounds = {}
    for key in keys:
        vals = [
            r["params"][key]
            for r in results
            if isinstance(r["params"].get(key), (int, float))
            and not isinstance(r["params"].get(key), bool)
        ]
        if len(vals) == len(results):  # dimension numérique sur TOUS les résultats
            lo, hi = min(vals), max(vals)
            if hi > lo:
                num_bounds[key] = (lo, hi)

    def dist(p: dict) -> float:
        d = 0.0
        for key in keys:
            a, b = p.get(key), best["params"].get(key)
            if key in num_bounds:
                lo, hi = num_bounds[key]
                d += abs(a - b) / (hi - lo)
            else:
                d += 0.0 if a == b else 1.0
        return d / max(len(keys), 1)

    neighbors = sorted(
        (r for r in results if r["params"] != best["params"]),
        key=lambda r: dist(r["params"]),
    )[:k]
    if not neighbors:
        return {"k": 0, "plateau_ratio": None, "verdict": "n/a"}

    med = float(pd.Series([r["score"] for r in neighbors]).median())
    ratio = med / best["score"]
    return {
        "k": len(neighbors),
        "neighbor_score_median": round(med, 2),
        "best_score": round(best["score"], 2),
        "plateau_ratio": round(ratio, 3),
        "verdict": "plateau" if ratio >= 0.5 else "pic_isole",
    }


def _format_extra_markdown(extra: dict) -> str:
    """Section Markdown « recherche + plateau » appendée au rapport robustesse."""
    lines = ["", "## Recherche & plateau de paramètres", ""]
    search = extra.get("search") or {}
    if search:
        line = f"- Backend : **{search.get('backend')}** · {search.get('n_tested')} jeux testés"
        if search.get("n_trials"):
            line += f" ({search['n_trials']} trials TPE)"
        lines.append(line)
    for ticker, p in (extra.get("plateau") or {}).items():
        if not p:
            continue
        if p.get("plateau_ratio") is None:
            lines.append(f"- {ticker} : plateau n/a")
        else:
            flag = "✓" if p["verdict"] == "plateau" else "⚠"
            lines.append(
                f"- {ticker} : ratio={p['plateau_ratio']} "
                f"(médiane {p['k']} voisins / best) {flag} **{p['verdict']}**"
            )
    lines.append("")
    lines.append(
        "> Le best doit être sur un plateau (voisins ≥ 50 % de son score IS), "
        "pas un pic isolé — un pic signe une grille sur-fittée."
    )
    return "\n".join(lines) + "\n"


def _evaluate_holdout(strategy, data: dict, best_per_ticker: dict, holdout_start: str):
    """Évalue les params retenus sur le hold-out terminal [holdout_start, ∞).

    AUCUN rôle dans la sélection — pure mesure de confirmation pré-promotion.
    Chaque consultation « consomme » le hold-out : si on itère sur les params
    après l'avoir vu, il devient un OOS ordinaire (à documenter dans le
    rapport de la stratégie).
    """
    print(f"\n{'=' * 62}")
    print(f"  HOLD-OUT TERMINAL — [{holdout_start} → fin]  (consultation unique)")
    print(f"{'=' * 62}")
    parts = []
    for ticker, res in best_per_ticker.items():
        if ticker not in data:
            continue
        df_15m, tf = data[ticker]
        df_all = bt_engine.run_trades(strategy, ticker, df_15m=df_15m, tf=tf, params=res["params"])
        if len(df_all) == 0 or "date" not in df_all.columns:
            continue
        df_ho = df_all[df_all["date"].astype(str) >= holdout_start]
        s = m.compute_stats(df_ho)
        print(
            f"  {ticker:<6} n={s['n']:>4}  WR={s['wr'] * 100:.0f}%  "
            f"PF={s['pf']:.2f}  P&L=${s['pnl']:>+,.0f}"
        )
        parts.append(df_ho)
    if parts:
        s = m.compute_stats(pd.concat(parts, ignore_index=True))
        print(
            f"  {'TOTAL':<6} n={s['n']:>4}  WR={s['wr'] * 100:.0f}%  "
            f"PF={s['pf']:.2f}  P&L=${s['pnl']:>+,.0f}"
        )
        print("  ⚠  Hold-out consulté — ne plus itérer sur les params après cette lecture.")


def _default_score(is_s: dict, oos_s: dict) -> float:
    """Score de classement des combos — STRICTEMENT in-sample.

    INVARIANT MÉTHODOLOGIQUE : ce score ne doit JAMAIS lire `oos_s`.
    Toute condition sur l'OOS ici contamine la sélection des paramètres
    (les combos survivantes auraient un OOS positif par construction) et
    invalide le walk-forward. L'OOS n'est consulté qu'APRÈS sélection,
    pour le verdict. Le paramètre `oos_s` n'est conservé que pour la
    compatibilité de signature avec les `score_fn` custom.
    """
    del oos_s  # jamais utilisé — voir invariant ci-dessus
    return is_s["pf"] * is_s["pnl"] if is_s["pnl"] > 0 else 0.0


def _print_ticker_result(ticker, params, is_s, oos_s, oos_topstep, plateau=None):
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
    if plateau and plateau.get("plateau_ratio") is not None:
        flag = "✓" if plateau["verdict"] == "plateau" else "⚠"
        print(
            f"    Plateau : ratio={plateau['plateau_ratio']} "
            f"(k={plateau['k']} voisins) {flag} {plateau['verdict']}"
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
