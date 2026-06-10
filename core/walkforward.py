"""
Walk-forward multi-folds ancré — stabilité inter-folds d'une stratégie.

Pourquoi : un split IS/OOS UNIQUE (dates fixes) mesure la performance sur un
seul régime OOS. Quand la même fenêtre OOS est ré-utilisée pour des dizaines
d'hypothèses, les survivants sur-fittent ce régime. Le multi-fold découpe
l'historique en K folds chronologiques : pour chaque fold, on sélectionne les
params sur l'IS expanding (score strictement IS) et on les évalue sur l'OOS
du fold. Aucune donnée du fold k n'influence la sélection du fold k.

Sorties clés :
  - OOS RECOUSU : concaténation des trades OOS de chaque fold avec les params
    sélectionnés PAR CE FOLD → estimation honnête de « qu'aurait donné le
    process complet (re-calibration périodique incluse) ».
  - STABILITÉ : combien de folds positifs, dispersion du PF, variation des
    params retenus (params instables = edge fragile / sur-fitting de grille).

Le hold-out terminal (config WF_HOLDOUT_START) reste exclu de TOUS les folds.

Usage :
    from core.walkforward import run_multifold
    run_multifold(strategy_module, data)         # via optimize.py --multifold
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import pandas as pd
from joblib import Parallel, delayed

from config import (
    OPTIMIZER_PARALLEL_N_JOBS,
    WF_FOLD_MONTHS,
    WF_HOLDOUT_START,
    WF_IS_START,
    WF_N_FOLDS,
)
from core import metrics as m
from core.optimizer import _default_score, _evaluate_combo


def build_folds(
    holdout_start: str = WF_HOLDOUT_START,
    n_folds: int = WF_N_FOLDS,
    fold_months: int = WF_FOLD_MONTHS,
    is_start: str = WF_IS_START,
) -> list[dict]:
    """K folds expanding-IS dont les OOS contigus remontent depuis le hold-out.

    Exemple (holdout 2026-04-15, 5 folds × 2 mois) :
      fold 1 : IS [2024-09-01, 2025-06-14] → OOS [2025-06-15, 2025-08-15)
      ...
      fold 5 : IS [2024-09-01, 2026-02-14] → OOS [2026-02-15, 2026-04-15)
    """
    folds = []
    oos_end = pd.Timestamp(holdout_start)
    for _ in range(n_folds):
        oos_start = oos_end - pd.DateOffset(months=fold_months)
        folds.append(
            {
                "is_start": is_start,
                "is_end": str((oos_start - pd.Timedelta(days=1)).date()),
                "oos_start": str(oos_start.date()),
                "oos_end": str(oos_end.date()),
            }
        )
        oos_end = oos_start
    folds.reverse()
    return folds


def run_multifold(
    strategy,
    data: dict,
    n_folds: int = WF_N_FOLDS,
    fold_months: int = WF_FOLD_MONTHS,
    holdout_start: str = WF_HOLDOUT_START,
    score_fn=None,
    n_jobs: int | None = None,
    output_dir: str | None = "output",
) -> dict:
    """Walk-forward multi-folds par ticker (sélection IS-only par fold).

    Returns:
        {ticker: {"folds": [...], "stitched": stats, "stability": {...}}}
    """
    if n_jobs is None:
        n_jobs = OPTIMIZER_PARALLEL_N_JOBS
    if score_fn is None:
        score_fn = _default_score

    strategy_id = getattr(strategy, "STRATEGY_ID", "unknown")
    param_grid = getattr(strategy, "PARAM_GRID", {})
    if not param_grid:
        print(f"  [{strategy_id}] Aucun PARAM_GRID — multifold impossible.")
        return {}

    keys = list(param_grid.keys())
    combos = list(itertools.product(*param_grid.values()))
    folds = build_folds(holdout_start, n_folds, fold_months)

    print(f"\n{'=' * 66}")
    print(f"  WALK-FORWARD MULTI-FOLDS — {strategy_id}")
    print(f"  {len(folds)} folds × {fold_months} mois · hold-out ≥ {holdout_start} exclu")
    for i, f in enumerate(folds, 1):
        print(f"    fold {i} : IS …{f['is_end']}  →  OOS [{f['oos_start']}, {f['oos_end']})")
    print(f"  Grille : {len(combos)} combos × {len(data)} actifs")
    print(f"{'=' * 66}")

    results: dict[str, dict] = {}

    for ticker, (df_15m, tf) in data.items():
        dfs = {ticker: (df_15m, tf)}
        fold_rows = []
        stitched_parts = []

        for i, f in enumerate(folds, 1):
            raw = Parallel(n_jobs=n_jobs, backend="loky", verbose=0)(
                delayed(_evaluate_combo)(
                    combo,
                    keys,
                    dfs,
                    strategy,
                    score_fn,
                    f["is_end"],
                    f["oos_start"],
                    f["is_start"],
                    f["oos_end"],
                )
                for combo in combos
            )
            evals = [r for r in raw if r is not None]
            if not evals:
                fold_rows.append({"fold": i, **f, "params": None, "oos": None})
                continue
            best = max(evals, key=lambda r: r["score"])
            fold_rows.append({"fold": i, **f, "params": best["params"], "oos": best["oos"]})

            # Trades OOS du fold avec les params de CE fold → OOS recousu
            df_all = strategy.run_backtest(
                df_15m, ticker, tf=tf, params=best["params"], topstep_guard=False
            )
            if len(df_all) and "date" in df_all.columns:
                dates = df_all["date"].astype(str)
                stitched_parts.append(df_all[(dates >= f["oos_start"]) & (dates < f["oos_end"])])

        stitched = (
            m.compute_stats(pd.concat(stitched_parts, ignore_index=True))
            if stitched_parts
            else m.compute_stats(pd.DataFrame())
        )

        valid = [r for r in fold_rows if r["oos"] is not None and r["oos"]["n"] > 0]
        pfs = [r["oos"]["pf"] for r in valid]
        param_sets = [json.dumps(r["params"], sort_keys=True) for r in valid]
        stability = {
            "n_folds_evalues": len(valid),
            "n_folds_positifs": sum(1 for r in valid if r["oos"]["pnl"] > 0),
            "pf_min": min(pfs) if pfs else None,
            "pf_median": float(pd.Series(pfs).median()) if pfs else None,
            "pf_max": max(pfs) if pfs else None,
            "n_jeux_params_distincts": len(set(param_sets)),
        }
        results[ticker] = {"folds": fold_rows, "stitched": stitched, "stability": stability}

        print(f"\n  ▸ {ticker}")
        for r in fold_rows:
            if r["oos"] is None:
                print(f"    fold {r['fold']} : aucun trade")
                continue
            p_str = " ".join(f"{k}={v}" for k, v in r["params"].items())
            o = r["oos"]
            print(
                f"    fold {r['fold']} : OOS n={o['n']:>3} PF={o['pf']:.2f} "
                f"P&L=${o['pnl']:>+8,.0f}   ({p_str})"
            )
        print(
            f"    RECOUSU : n={stitched['n']} PF={stitched['pf']:.2f} "
            f"P&L=${stitched['pnl']:+,.0f}  ·  "
            f"{stability['n_folds_positifs']}/{stability['n_folds_evalues']} folds positifs, "
            f"{stability['n_jeux_params_distincts']} jeux de params distincts"
        )

    if output_dir:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        payload = {
            t: {
                "folds": [
                    {**{k: v for k, v in r.items() if k != "oos"}, "oos": r["oos"]}
                    for r in res["folds"]
                ],
                "stitched": res["stitched"],
                "stability": res["stability"],
            }
            for t, res in results.items()
        }
        (out / f"multifold_{strategy_id}.json").write_text(
            json.dumps(payload, indent=2, default=str), encoding="utf-8"
        )
        print(f"\n  ✓ output/multifold_{strategy_id}.json")

    return results
