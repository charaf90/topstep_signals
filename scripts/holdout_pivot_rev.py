"""
Holdout runner pour pivot-rev-v1.

PAS le walk-forward standard projet (multi-splits) — c'est un HOLDOUT PUR :
  IS  = 2025-10-19 → 2026-01-31  (entraînement RF + grid search 3³ = 27 combos)
  OOS = 2026-02-01 → 2026-05-15  (mesure unique, jamais ré-optimisé)
  PURGE = 20 barres M5 en frontière (label argrelextrema(order=20))

Workflow :
  1. Charger data/MGC1_data_m5.csv
  2. Pour chaque combo (sl_mult, rr_target, vol_rel_min) du PARAM_GRID :
       a. run_backtest(df_full, params=combo) — le RF est entraîné sur IS au
          premier appel et caché par _prepare_dataset()
       b. split trades : IS uniquement (fill_time <= IS_END)
       c. métriques IS
  3. Sélectionner la meilleure combo (PF IS), arrêt précoce si PF IS < 1.5
  4. Sur la meilleure combo : extraire trades OOS (fill_time >= OOS_START purgé)
  5. Métriques OOS + sauvegarder trades_v1.csv + summary.json draft

Usage : python scripts/holdout_pivot_rev.py
"""

from __future__ import annotations

import json
import sys
import warnings
from itertools import product
from pathlib import Path

warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from config import (  # noqa: E402
    PIVOT_REV_IS_END,
    PIVOT_REV_IS_START,
    PIVOT_REV_OOS_END,
    PIVOT_REV_OOS_START,
    PIVOT_REV_PURGE_BARS,
    PIVOT_REV_STRATEGY_VERSION,
    SLIPPAGE_TICKS_PER_TICKER,
)
from core.data import load_csv  # noqa: E402
from core.dataset_hash import snapshot_datasets  # noqa: E402
from strategies import pivot_rev  # noqa: E402

OUT_DIR = ROOT / "output" / "pivot-rev-v1"
FULL_DIR = OUT_DIR / "full"
OUT_DIR.mkdir(parents=True, exist_ok=True)
FULL_DIR.mkdir(parents=True, exist_ok=True)

# ── Critère d'arrêt précoce (consigne orchestrateur) ─────────────────────────
EARLY_STOP_IS_PF_MIN = 1.5


def compute_metrics(trades: pd.DataFrame) -> dict:
    """Métriques de base : n, n_filled, PF, WR, P&L, DD, expiration rate."""
    if trades.empty:
        return {
            "n": 0,
            "n_filled": 0,
            "n_not_filled": 0,
            "expiration_rate": 0.0,
            "pf": 0.0,
            "wr_pct": 0.0,
            "pl_net": 0.0,
            "dd_max": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "n_tp": 0,
            "n_sl": 0,
            "n_te": 0,
        }
    filled = trades[trades["result"] != "NOT_FILLED"].copy()
    n = len(trades)
    n_filled = len(filled)
    n_nf = n - n_filled

    if n_filled == 0:
        return {
            "n": n,
            "n_filled": 0,
            "n_not_filled": n_nf,
            "expiration_rate": n_nf / n if n else 0.0,
            "pf": 0.0,
            "wr_pct": 0.0,
            "pl_net": 0.0,
            "dd_max": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "n_tp": 0,
            "n_sl": 0,
            "n_te": 0,
        }

    pnls = filled["pnl"].to_numpy(dtype=float)
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    gross_win = float(wins.sum()) if len(wins) else 0.0
    gross_loss = float(-losses.sum()) if len(losses) else 0.0
    pf = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
    wr = (len(wins) / n_filled * 100) if n_filled else 0.0

    # DD max sur equity curve cumulée
    cum = np.cumsum(pnls)
    peak = np.maximum.accumulate(cum) if len(cum) else np.array([0.0])
    dd = (cum - peak).min() if len(cum) else 0.0

    result_counts = filled["result"].value_counts().to_dict()
    return {
        "n": int(n),
        "n_filled": int(n_filled),
        "n_not_filled": int(n_nf),
        "expiration_rate": round(n_nf / n, 4) if n else 0.0,
        "pf": round(float(pf), 3) if pf != float("inf") else 999.0,
        "wr_pct": round(float(wr), 2),
        "pl_net": round(float(pnls.sum()), 2),
        "dd_max": round(float(dd), 2),
        "avg_win": round(float(wins.mean()), 2) if len(wins) else 0.0,
        "avg_loss": round(float(losses.mean()), 2) if len(losses) else 0.0,
        "n_tp": int(result_counts.get("TP", 0)),
        "n_sl": int(result_counts.get("SL", 0)),
        "n_te": int(result_counts.get("TE", 0)),
    }


def split_is_oos(trades: pd.DataFrame, df_full: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Split trades en IS / OOS via la date du signal (colonne `date`).

    Règle d'attribution :
      - trade dans IS si fill_time <= IS_END (ou si NOT_FILLED et date <= IS_END)
      - trade dans OOS si fill_time >= OOS_START (purge 20 barres déjà appliquée
        via le mécanisme de purge ci-dessous)
      - trade en zone purgée → exclu (frontière IS/OOS)

    On utilise la `date` du signal comme proxy quand fill_time est None.
    """
    is_end = pd.Timestamp(PIVOT_REV_IS_END)
    oos_start = pd.Timestamp(PIVOT_REV_OOS_START)
    oos_end = pd.Timestamp(PIVOT_REV_OOS_END) + pd.Timedelta(days=1)  # inclusif

    # Purge : on retire les PIVOT_REV_PURGE_BARS premières barres de l'OOS
    # (frontière IS/OOS) afin d'éviter le leakage du label oracle order=20.
    purge_minutes = PIVOT_REV_PURGE_BARS * 5  # M5
    purge_until = oos_start + pd.Timedelta(minutes=purge_minutes)

    def trade_period(row) -> str:
        # Timestamp de référence : fill_time si dispo, sinon date du signal
        ref = row["fill_time"] if pd.notna(row["fill_time"]) else pd.Timestamp(row["date"])
        ref = (
            pd.Timestamp(ref).tz_localize(None)
            if pd.Timestamp(ref).tz is not None
            else pd.Timestamp(ref)
        )
        if ref <= is_end:
            return "IS"
        if ref < purge_until:
            return "PURGE"
        if ref < oos_end:
            return "OOS"
        return "FUTURE"

    if trades.empty:
        return trades.copy(), trades.copy()

    periods = trades.apply(trade_period, axis=1)
    is_trades = trades[periods == "IS"].copy()
    oos_trades = trades[periods == "OOS"].copy()
    return is_trades, oos_trades


def monthly_breakdown(trades: pd.DataFrame) -> dict:
    """P&L net par mois (utile diagnostic dérive régime)."""
    if trades.empty:
        return {}
    filled = trades[trades["result"] != "NOT_FILLED"].copy()
    if filled.empty:
        return {}
    filled["month"] = pd.to_datetime(filled["date"]).dt.to_period("M").astype(str)
    grp = filled.groupby("month")["pnl"].agg(["sum", "count"]).round(2)
    return {m: {"pl_net": float(r["sum"]), "n": int(r["count"])} for m, r in grp.iterrows()}


def run():
    print("═" * 70)
    print(f"  HOLDOUT — {PIVOT_REV_STRATEGY_VERSION}")
    print("═" * 70)

    # ── 1. Chargement data ──────────────────────────────────────────────────
    csv_path = ROOT / "data" / "MGC1_data_m5.csv"
    print(f"▸ Chargement {csv_path}")
    df = load_csv(str(csv_path))
    print(f"  • {len(df):,} barres ({df.index[0]} → {df.index[-1]})")

    # ── 2. Préparation features + RF (1x, mis en cache) ─────────────────────
    print("\n▸ Entraînement RF sur IS + calcul proba_RF (1x, caché)…")
    df_prep = pivot_rev._prepare_dataset(df)
    threshold = float(df_prep["proba_rf_threshold_is"].iloc[0])
    print(f"  • {len(df_prep):,} barres après warmup")
    print(f"  • Seuil proba_RF (p10% IS) figé : {threshold:.4f}")

    # ── 3. Grid search sur IS uniquement ────────────────────────────────────
    print("\n▸ Grid search 3³ = 27 combos…")
    grid = list(
        product(
            pivot_rev.PARAM_GRID["sl_mult"],
            pivot_rev.PARAM_GRID["rr_target"],
            pivot_rev.PARAM_GRID["vol_rel_min"],
        )
    )

    results = []
    best_is_pf = -float("inf")
    best_combo = None
    best_trades_is = None
    best_trades_all = None

    print(
        f"  {'sl_mult':<8} {'rr_target':<10} {'vol_rel_min':<12} "
        f"{'IS_PF':<7} {'IS_n':<6} {'IS_PnL':<10} {'IS_DD':<10} {'expir%':<7}"
    )
    print("  " + "─" * 70)

    for sl_mult, rr_target, vol_rel_min in grid:
        params = {"sl_mult": sl_mult, "rr_target": rr_target, "vol_rel_min": vol_rel_min}
        trades_all = pivot_rev.run_backtest(df, "MGC1", params=params)
        trades_is, trades_oos = split_is_oos(trades_all, df_prep)
        m_is = compute_metrics(trades_is)

        results.append(
            {
                "sl_mult": sl_mult,
                "rr_target": rr_target,
                "vol_rel_min": vol_rel_min,
                "is_pf": m_is["pf"],
                "is_n": m_is["n_filled"],
                "is_n_total": m_is["n"],
                "is_pnl": m_is["pl_net"],
                "is_dd": m_is["dd_max"],
                "is_wr": m_is["wr_pct"],
                "is_expir": m_is["expiration_rate"],
            }
        )
        print(
            f"  {sl_mult:<8.2f} {rr_target:<10.2f} {vol_rel_min:<12.2f} "
            f"{m_is['pf']:<7.2f} {m_is['n_filled']:<6d} "
            f"{m_is['pl_net']:<+10.0f} {m_is['dd_max']:<+10.0f} "
            f"{m_is['expiration_rate']*100:<7.1f}"
        )

        if m_is["pf"] > best_is_pf and m_is["n_filled"] >= 5:
            best_is_pf = m_is["pf"]
            best_combo = params
            best_trades_is = trades_is
            best_trades_all = trades_all

    # ── 4. Sélection meilleure combo ────────────────────────────────────────
    print("\n" + "═" * 70)
    if best_combo is None:
        print("  ⚠ Aucune combo n'a produit ≥ 5 trades IS — concept inviable.")
        verdict_draft = "🔴"
        diag = "Aucune combo avec ≥5 trades IS — concept inviable structurellement"
        _write_summary_minimal(results, [], None, None, verdict_draft, diag, df, threshold)
        return
    print(f"  BEST COMBO : {best_combo}")
    print(f"  IS PF : {best_is_pf:.2f}")

    # ── 5. Critère d'arrêt précoce ──────────────────────────────────────────
    if best_is_pf < EARLY_STOP_IS_PF_MIN:
        print(f"  ⚠ IS PF {best_is_pf:.2f} < {EARLY_STOP_IS_PF_MIN} — ARRÊT PRÉCOCE.")
        print("  → Pas de mesure OOS (évite p-hacking).")
        verdict_draft = "🔴"
        diag = (
            f"IS PF best={best_is_pf:.2f} < seuil {EARLY_STOP_IS_PF_MIN}. "
            f"Arrêt précoce sans toucher OOS pour éviter p-hacking."
        )
        # Sauvegarder trades IS de la meilleure combo quand même (diagnostic)
        is_metrics = compute_metrics(best_trades_is) if best_trades_is is not None else {}
        _write_summary_minimal(
            results, [], best_combo, is_metrics, verdict_draft, diag, df, threshold
        )
        # Sauve trades_v1.csv (= meilleure combo, tous trades) pour diagnostic
        best_trades_all.to_csv(FULL_DIR / "trades_v1.csv", index=False)
        return

    # ── 6. Mesure OOS sur la meilleure combo (UNE SEULE FOIS) ───────────────
    print("\n▸ Mesure OOS sur la meilleure combo (mesure unique)…")
    _, best_trades_oos = split_is_oos(best_trades_all, df_prep)
    m_oos = compute_metrics(best_trades_oos)
    m_is_best = compute_metrics(best_trades_is)
    monthly_oos = monthly_breakdown(best_trades_oos)

    print(f"  • OOS n_signals (incl. NOT_FILLED) : {m_oos['n']}")
    print(f"  • OOS n_filled                     : {m_oos['n_filled']}")
    print(f"  • OOS expiration_rate              : {m_oos['expiration_rate']*100:.1f}%")
    print(f"  • OOS PF                           : {m_oos['pf']:.2f}")
    print(f"  • OOS P&L net                      : ${m_oos['pl_net']:+,.0f}")
    print(f"  • OOS DD max                       : ${m_oos['dd_max']:+,.0f}")
    print(f"  • OOS WR                           : {m_oos['wr_pct']:.1f}%")
    print(
        f"  • TP / SL / TE                     : {m_oos['n_tp']} / {m_oos['n_sl']} / {m_oos['n_te']}"
    )

    print("\n  Breakdown mensuel OOS :")
    for m, r in monthly_oos.items():
        print(f"    {m} : P&L=${r['pl_net']:+,.0f}  n={r['n']}")

    # ── 6b. Stress friction (4 ticks RT) ────────────────────────────────────
    stress_friction = _stress_friction(best_trades_oos)

    # ── 7. Sensibilité aux 3 params (mesurée sur IS pour ne pas re-tester OOS) ─
    sensitivity = _sensitivity_analysis(results, best_combo)

    # ── 8. Verdict draft ────────────────────────────────────────────────────
    verdict_draft, diag = _draft_verdict(m_is_best, m_oos)

    # ── 9. Sauvegarde trades_v1.csv ─────────────────────────────────────────
    best_trades_all.to_csv(FULL_DIR / "trades_v1.csv", index=False)
    print(f"\n  → trades_v1.csv ({len(best_trades_all)} trades sauvés)")

    # ── 10. summary.json ────────────────────────────────────────────────────
    _write_summary_full(
        results,
        sensitivity,
        best_combo,
        m_is_best,
        m_oos,
        monthly_oos,
        stress_friction,
        verdict_draft,
        diag,
        df,
        threshold,
    )

    print("\n" + "═" * 70)
    print(f"  VERDICT DRAFT : {verdict_draft}")
    print(f"  {diag}")
    print("═" * 70)


def _sensitivity_analysis(results: list, best_combo: dict) -> dict:
    """Sensibilité IS PF en faisant varier un seul param autour de la best."""
    res_df = pd.DataFrame(results)
    out = {}
    for param in ("sl_mult", "rr_target", "vol_rel_min"):
        others = [p for p in ("sl_mult", "rr_target", "vol_rel_min") if p != param]
        mask = (res_df[others[0]] == best_combo[others[0]]) & (
            res_df[others[1]] == best_combo[others[1]]
        )
        sub = res_df[mask].sort_values(param)[[param, "is_pf", "is_n", "is_pnl"]]
        out[param] = sub.to_dict(orient="records")
    return out


def _stress_friction(trades_oos: pd.DataFrame) -> dict:
    """Scénario stress : 4 ticks RT au lieu de 2 → friction $5.40 vs $3.40."""
    if trades_oos.empty:
        return {}
    filled = trades_oos[trades_oos["result"] != "NOT_FILLED"].copy()
    if filled.empty:
        return {}
    slip_extra_per_ct = (
        (4 - 2) * SLIPPAGE_TICKS_PER_TICKER["MGC1"] * 0.10 * 10.0
    )  # 2 ticks extra × tick_size × pt_value
    extra_cost_per_trade = filled["n_ct"].astype(float) * slip_extra_per_ct
    pnl_stress = filled["pnl"] - extra_cost_per_trade
    gw = pnl_stress[pnl_stress > 0].sum()
    gl = -pnl_stress[pnl_stress < 0].sum()
    pf_stress = (gw / gl) if gl > 0 else (999.0 if gw > 0 else 0.0)
    return {
        "friction_rt_dollars": 5.40,
        "pl_net_oos_stress": round(float(pnl_stress.sum()), 2),
        "pf_oos_stress": round(float(pf_stress), 3),
        "delta_pf_vs_baseline": round(float(pf_stress) - float(_pf(filled["pnl"])), 3),
    }


def _pf(pnls: pd.Series) -> float:
    gw = pnls[pnls > 0].sum()
    gl = -pnls[pnls < 0].sum()
    return float((gw / gl) if gl > 0 else (999.0 if gw > 0 else 0.0))


def _draft_verdict(m_is: dict, m_oos: dict) -> tuple[str, str]:
    """Verdict draft selon les critères projet (sans bootstrap final = PHASE 5)."""
    pf_oos = m_oos["pf"]
    n_oos = m_oos["n_filled"]
    pnl_oos = m_oos["pl_net"]
    pf_is = m_is["pf"]

    # Conditions 🔴
    if pf_oos < 1.2 or pnl_oos <= 0:
        return "🔴", (
            f"OOS PF={pf_oos:.2f}, P&L=${pnl_oos:+,.0f}, n_filled={n_oos} — "
            f"sous le seuil 🔴 (PF<1.2 OU P&L≤0)."
        )
    if n_oos < 8:
        return "🔴", f"n_filled OOS={n_oos} < 8 — échantillon insuffisant."

    # Dégradation IS→OOS
    degr = (1 - pf_oos / pf_is) * 100 if pf_is > 0 else 100
    if degr > 50:
        return "🔴", (f"Dégradation IS→OOS = {degr:.0f}% (>50%) — overfit suspect.")

    # 🟡 ou 🟢
    if pf_oos >= 1.5 and n_oos >= 20:
        return "🟢", (
            f"OOS PF={pf_oos:.2f} (≥1.5), n_filled={n_oos} (≥20), "
            f"P&L=${pnl_oos:+,.0f}, dégradation IS→OOS={degr:.0f}%. "
            f"DRAFT — bootstrap PHASE 5 à confirmer."
        )
    return "🟡", (
        f"OOS PF={pf_oos:.2f}, n_filled={n_oos}, P&L=${pnl_oos:+,.0f}, "
        f"dégradation IS→OOS={degr:.0f}%. Verdict 🟡 — entre 🔴 et 🟢. "
        f"PHASE 3.5 (@quant discover) recommandée si n_oos ≥ 100."
    )


def _write_summary_minimal(results, _unused, best_combo, m_is, verdict, diag, df, threshold):
    """summary.json minimal (cas arrêt précoce ou aucun trade)."""
    summary = {
        "strategy_id": "pivot-rev-v1",
        "version": "pivot-rev-v1",
        "iterations": 1,
        "verdict": verdict,
        "verdict_draft": True,
        "skip_chartist_audit": True,
        "skip_reason": "PHASES 5-8 non exécutées (stop après PHASE 4 / arrêt précoce)",
        "ticker": "MGC1",
        "timeframe": "m5",
        "holdout_schema": {
            "is_start": PIVOT_REV_IS_START,
            "is_end": PIVOT_REV_IS_END,
            "oos_start": PIVOT_REV_OOS_START,
            "oos_end": PIVOT_REV_OOS_END,
            "purge_bars_m5": PIVOT_REV_PURGE_BARS,
        },
        "rf_threshold_p10_is": round(threshold, 5),
        "best_combo": best_combo,
        "is": m_is if m_is else None,
        "oos": None,
        "early_stop": True,
        "diagnostic": diag,
        "grid_results": results,
        "datasets": snapshot_datasets("data", tickers=["MGC1"]),
        "next_step": "rejet" if verdict == "🔴" else "itération",
    }
    (OUT_DIR / "summary.json").write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8"
    )
    print(f"\n  → summary.json sauvé (verdict draft : {verdict})")


def _write_summary_full(
    results,
    sensitivity,
    best_combo,
    m_is,
    m_oos,
    monthly_oos,
    stress_friction,
    verdict,
    diag,
    df,
    threshold,
):
    degr = round((1 - m_oos["pf"] / m_is["pf"]) * 100, 1) if m_is["pf"] > 0 else 100.0
    summary = {
        "strategy_id": "pivot-rev-v1",
        "version": "pivot-rev-v1",
        "iterations": 1,
        "verdict": verdict,
        "verdict_draft": True,
        "skip_chartist_audit": True,
        "skip_reason": "PHASE 4 STOP (consigne orchestrateur — audit visuel + bootstrap reportés)",
        "ticker": "MGC1",
        "timeframe": "m5",
        "holdout_schema": {
            "is_start": PIVOT_REV_IS_START,
            "is_end": PIVOT_REV_IS_END,
            "oos_start": PIVOT_REV_OOS_START,
            "oos_end": PIVOT_REV_OOS_END,
            "purge_bars_m5": PIVOT_REV_PURGE_BARS,
        },
        "rf_threshold_p10_is": round(threshold, 5),
        "best_combo": best_combo,
        "is": {
            "pf": m_is["pf"],
            "pl_net": m_is["pl_net"],
            "n": m_is["n_filled"],
            "n_total_signals": m_is["n"],
            "dd": m_is["dd_max"],
            "wr_pct": m_is["wr_pct"],
            "expiration_rate": m_is["expiration_rate"],
            "n_tp": m_is["n_tp"],
            "n_sl": m_is["n_sl"],
            "n_te": m_is["n_te"],
        },
        "oos": {
            "pf": m_oos["pf"],
            "pl_net": m_oos["pl_net"],
            "n": m_oos["n_filled"],
            "n_total_signals": m_oos["n"],
            "dd": m_oos["dd_max"],
            "wr_pct": m_oos["wr_pct"],
            "expiration_rate": m_oos["expiration_rate"],
            "n_tp": m_oos["n_tp"],
            "n_sl": m_oos["n_sl"],
            "n_te": m_oos["n_te"],
            # Bootstrap reporté à PHASE 5 (orchestrateur stoppe en PHASE 4)
            "bootstrap": None,
        },
        "degradation_is_oos_pct": degr,
        "monthly_breakdown_oos": monthly_oos,
        "stress_friction_4ticks_rt": stress_friction,
        "sensitivity_is": sensitivity,
        "grid_results": results,
        "stress": None,
        "robustness": None,
        "quant_used": False,
        "quant_verdict": None,
        "quant_filters_applied": [],
        "audit_warnings_count": 0,
        "diagnostic": diag,
        "datasets": snapshot_datasets("data", tickers=["MGC1"]),
        "next_step": _next_step(verdict, m_oos),
    }
    (OUT_DIR / "summary.json").write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8"
    )
    print("  → summary.json sauvé")


def _next_step(verdict: str, m_oos: dict) -> str:
    if verdict == "🔴":
        return "rejet"
    if verdict == "🟢":
        return "PHASE 5 (bootstrap + stress) puis promotion"
    # 🟡
    n_oos = m_oos.get("n_filled", 0)
    if n_oos >= 100:
        return "PHASE 3.5 @quant discover (n_oos ≥ 100 — features data-driven)"
    return "itération — n_oos < 100 insuffisant pour @quant discover"


if __name__ == "__main__":
    run()
