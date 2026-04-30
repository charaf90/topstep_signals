"""
Walk-forward test des filtres OPR candidats.

Calibration des seuils sur IS (avant 2025-10-01) uniquement,
évaluation en aveugle sur OOS. Critère d'acceptation identique
à optimize_opr.py : OOS PF ≥ 1.2, n_trades OOS ≥ 8, P&L OOS > 0.

Usage :
    python analyse/03_filter_backtest.py
    python analyse/03_filter_backtest.py --ticker MES1 --top-n 8
"""

import argparse
import sys
import warnings
from itertools import combinations
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

warnings.filterwarnings("ignore", category=RuntimeWarning)

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import INSTRUMENTS, CHART_STYLE

# ─────────────────────────────────────────────────────────────────────────────
# Constantes
# ─────────────────────────────────────────────────────────────────────────────

IS_END = "2025-09-30"
OOS_PF_MIN = 1.2
OOS_N_MIN = 8
OOS_PNL_MIN = 0.0
N_TOP_FEATURES = 5
N_TOP_COMBOS = 3


# ─────────────────────────────────────────────────────────────────────────────
# Calcul des stats d'un sous-ensemble de trades
# ─────────────────────────────────────────────────────────────────────────────

def _stats(df):
    """
    Calcule n_fills, win_rate, profit factor et P&L total
    sur un DataFrame de trades filtrés (sans NOT_FILLED).
    """
    df_f = df[df["result"] != "NOT_FILLED"]
    if len(df_f) == 0:
        return {"n": 0, "wr": 0.0, "pf": 0.0, "pnl": 0.0, "dd": 0.0}

    wins = df_f[df_f["is_tp"] == 1]
    losses = df_f[df_f["is_tp"] == 0]
    gp = float(wins["pnl"].sum()) if len(wins) > 0 else 0.0
    gl = abs(float(losses["pnl"].sum())) if len(losses) > 0 else 0.0
    pf = gp / gl if gl > 0 else (9.99 if gp > 0 else 0.0)

    # Max trailing drawdown (cumulatif chronologique)
    pnl_cum = df_f.sort_values("date")["pnl"].cumsum()
    rolling_max = pnl_cum.cummax()
    dd = float((pnl_cum - rolling_max).min())

    return {
        "n": int(len(df_f)),
        "wr": float(len(wins) / len(df_f) * 100),
        "pf": float(pf),
        "pnl": float(df_f["pnl"].sum()),
        "dd": dd,
    }


def _valid_oos(st):
    return (
        st["pf"] >= OOS_PF_MIN and
        st["n"] >= OOS_N_MIN and
        st["pnl"] > OOS_PNL_MIN
    )


# ─────────────────────────────────────────────────────────────────────────────
# Optimisation du seuil sur IS
# ─────────────────────────────────────────────────────────────────────────────

def _find_threshold(df_is_filled, feature, direction):
    """
    Balaye les percentiles 10→90 (step 5) de la feature sur IS.
    Retourne le seuil maximisant PF IS avec n_fills ≥ OOS_N_MIN.
    Retourne (None, None) si aucun seuil satisfaisant.
    """
    vals = df_is_filled[feature].dropna()
    if len(vals) < 2 * OOS_N_MIN:
        return None, None

    best_thresh, best_pf = None, -np.inf
    for pct in range(10, 95, 5):
        thresh = float(np.percentile(vals, pct))
        mask = (df_is_filled[feature] > thresh if direction == "gt"
                else df_is_filled[feature] < thresh)
        sub = df_is_filled.loc[mask]
        st = _stats(sub)
        if st["n"] < OOS_N_MIN:
            continue
        if st["pf"] > best_pf:
            best_pf = st["pf"]
            best_thresh = thresh

    return best_thresh, best_pf


# ─────────────────────────────────────────────────────────────────────────────
# Test d'un filtre individuel
# ─────────────────────────────────────────────────────────────────────────────

def test_single_filter(df, feature, ticker, chart_dir):
    """
    Optimise un filtre sur IS (2 directions) et l'évalue sur OOS.
    Retourne le meilleur résultat (IS PF maximal) ou None.
    """
    df_is = df[df["date"] <= IS_END].copy()
    df_oos = df[df["date"] > IS_END].copy()

    df_is_f = df_is[df_is["result"] != "NOT_FILLED"].dropna(subset=[feature])
    df_oos_f = df_oos[df_oos["result"] != "NOT_FILLED"].dropna(subset=[feature])

    base_is = _stats(df_is[df_is["result"] != "NOT_FILLED"])
    base_oos = _stats(df_oos[df_oos["result"] != "NOT_FILLED"])

    best = None
    for direction in ["gt", "lt"]:
        thresh, _ = _find_threshold(df_is_f, feature, direction)
        if thresh is None:
            continue

        mask_is = (df_is_f[feature] > thresh if direction == "gt"
                   else df_is_f[feature] < thresh)
        mask_oos = (df_oos_f[feature] > thresh if direction == "gt"
                    else df_oos_f[feature] < thresh)

        st_is = _stats(df_is_f.loc[mask_is])
        st_oos = _stats(df_oos_f.loc[mask_oos])

        row = {
            "feature": feature,
            "filter_type": "single",
            "direction": direction,
            "threshold": thresh,
            "is_n": st_is["n"],
            "is_wr": round(st_is["wr"], 1),
            "is_pf": round(st_is["pf"], 3),
            "is_pnl": round(st_is["pnl"], 1),
            "is_dd": round(st_is["dd"], 1),
            "oos_n": st_oos["n"],
            "oos_wr": round(st_oos["wr"], 1),
            "oos_pf": round(st_oos["pf"], 3),
            "oos_pnl": round(st_oos["pnl"], 1),
            "oos_dd": round(st_oos["dd"], 1),
            "valid_oos": _valid_oos(st_oos),
            "combo": feature,
            # baseline pour référence
            "base_is_pf": round(base_is["pf"], 3),
            "base_oos_pf": round(base_oos["pf"], 3),
            "base_is_n": base_is["n"],
            "base_oos_n": base_oos["n"],
        }
        if best is None or st_is["pf"] > best["is_pf"]:
            best = row

    if best is None:
        return None

    _plot_filter(best, ticker, chart_dir)
    return best


# ─────────────────────────────────────────────────────────────────────────────
# Test d'un filtre combiné (AND de 2)
# ─────────────────────────────────────────────────────────────────────────────

def test_combo_filter(df, r1, r2, ticker, chart_dir):
    """AND de 2 filtres individuels, seuils calibrés sur IS."""
    fa, da, ta = r1["feature"], r1["direction"], r1["threshold"]
    fb, db, tb = r2["feature"], r2["direction"], r2["threshold"]

    df_is = df[df["date"] <= IS_END].copy()
    df_oos = df[df["date"] > IS_END].copy()

    base_is = _stats(df_is[df_is["result"] != "NOT_FILLED"])
    base_oos = _stats(df_oos[df_oos["result"] != "NOT_FILLED"])

    def _apply(df_in):
        df_f = df_in[df_in["result"] != "NOT_FILLED"].dropna(subset=[fa, fb])
        ma = df_f[fa] > ta if da == "gt" else df_f[fa] < ta
        mb = df_f[fb] > tb if db == "gt" else df_f[fb] < tb
        return df_f.loc[ma & mb]

    st_is = _stats(_apply(df_is))
    st_oos = _stats(_apply(df_oos))
    combo_str = f"{fa}_{da} & {fb}_{db}"
    dir_sym_a = ">" if da == "gt" else "<"
    dir_sym_b = ">" if db == "gt" else "<"
    combo_label = f"{fa}{dir_sym_a}{ta:.3f} & {fb}{dir_sym_b}{tb:.3f}"

    row = {
        "feature": combo_label,
        "filter_type": "combo",
        "direction": f"{da}&{db}",
        "threshold": f"{ta:.4f}&{tb:.4f}",
        "is_n": st_is["n"],
        "is_wr": round(st_is["wr"], 1),
        "is_pf": round(st_is["pf"], 3),
        "is_pnl": round(st_is["pnl"], 1),
        "is_dd": round(st_is["dd"], 1),
        "oos_n": st_oos["n"],
        "oos_wr": round(st_oos["wr"], 1),
        "oos_pf": round(st_oos["pf"], 3),
        "oos_pnl": round(st_oos["pnl"], 1),
        "oos_dd": round(st_oos["dd"], 1),
        "valid_oos": _valid_oos(st_oos),
        "combo": combo_str,
        "base_is_pf": round(base_is["pf"], 3),
        "base_oos_pf": round(base_oos["pf"], 3),
        "base_is_n": base_is["n"],
        "base_oos_n": base_oos["n"],
    }
    _plot_filter(row, ticker, chart_dir)
    return row


# ─────────────────────────────────────────────────────────────────────────────
# Visualisation
# ─────────────────────────────────────────────────────────────────────────────

def _plot_filter(row, ticker, chart_dir):
    """
    2 subplots (IS | OOS) : barres PF et P&L avant/après filtre.
    """
    plt.rcParams.update(CHART_STYLE)
    fig, axes = plt.subplots(1, 2, figsize=(11, 5))

    valid_sym = "✓ VALID" if row["valid_oos"] else "✗ FAIL"
    feat_label = row["feature"][:55]  # tronquer si trop long
    fig.suptitle(
        f"{ticker} — {feat_label}\n"
        f"IS: n={row['is_n']}  PF={row['is_pf']:.2f}  P&L={row['is_pnl']:+.0f}$  |  "
        f"OOS: n={row['oos_n']}  PF={row['oos_pf']:.2f}  "
        f"P&L={row['oos_pnl']:+.0f}$  [{valid_sym}]",
        fontsize=9, y=0.99
    )

    datasets = [
        ("IS", row["base_is_pf"], row["is_pf"], row["is_pnl"]),
        ("OOS", row["base_oos_pf"], row["oos_pf"], row["oos_pnl"]),
    ]
    for ax, (label, base_pf, filt_pf, filt_pnl) in zip(axes, datasets):
        # Barres PF
        col_filt = "#26a69a" if filt_pf >= OOS_PF_MIN else "#ef5350"
        ax.bar([0, 1], [base_pf, filt_pf],
               color=["#787b86", col_filt], width=0.4, alpha=0.85,
               label=["baseline", "filtré"])
        ax.axhline(1.0, color="#d1d4dc", ls="--", lw=0.8, alpha=0.6)
        ax.axhline(OOS_PF_MIN, color="#26a69a", ls=":", lw=0.8, alpha=0.6,
                   label=f"seuil PF {OOS_PF_MIN}")
        ax.set_xticks([0, 1])
        ax.set_xticklabels(["Baseline", "Filtré"], fontsize=8)
        ax.set_ylabel("Profit Factor", fontsize=8)
        ax.set_title(f"{label}  (base n={row[f'base_{label.lower()}_n']}, "
                     f"filtré n={row[f'{label.lower()}_n']})", fontsize=9)
        ax.legend(fontsize=7)

        # P&L en annotation
        for x, v in [(0, row[f"base_{label.lower()}_pf"] if f"base_{label.lower()}_pf" in row else base_pf),
                     (1, filt_pf)]:
            ax.text(x, v + 0.01, f"{v:.2f}", ha="center", va="bottom",
                    fontsize=8, color="#d1d4dc")

        # Axe P&L secondaire
        ax2 = ax.twinx()
        ax2.bar([2.5], [filt_pnl],
                color="#26a69a" if filt_pnl >= 0 else "#ef5350",
                width=0.4, alpha=0.6)
        ax2.set_xticks([0, 1, 2.5])
        ax2.set_xticklabels(["Baseline\nPF", "Filtré\nPF", "Filtré\nP&L"],
                            fontsize=7)
        ax2.set_ylabel("P&L ($)", fontsize=8)
        ax2.text(2.5, filt_pnl + (abs(filt_pnl) * 0.02 + 1),
                 f"{filt_pnl:+.0f}$", ha="center", va="bottom",
                 fontsize=8, color="#d1d4dc")

    safe_name = row["combo"][:50].replace("/", "_").replace(" ", "_").replace("&", "ET")
    out_path = chart_dir / f"{safe_name}_{ticker}.png"
    fig.savefig(str(out_path), dpi=120, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Walk-forward test filtres OPR"
    )
    parser.add_argument("--ticker", type=str, default=None)
    parser.add_argument("--top-n", type=int, default=N_TOP_FEATURES)
    args = parser.parse_args()

    data_dir = Path(__file__).parent / "data"
    chart_dir = Path(__file__).parent / "charts" / "filters"
    chart_dir.mkdir(parents=True, exist_ok=True)

    tickers = [args.ticker] if args.ticker else list(INSTRUMENTS.keys())
    top_n = args.top_n

    for ticker in tickers:
        triggers_path = data_dir / f"opr_triggers_{ticker}.csv"
        ranking_path = data_dir / f"feature_ranking_{ticker}.csv"

        if not triggers_path.exists():
            print(f"[!] {ticker} : {triggers_path} manquant")
            continue
        if not ranking_path.exists():
            print(f"[!] {ticker} : {ranking_path} manquant — lancer 02 d'abord")
            continue

        print(f"\n{'='*65}")
        print(f"  {ticker}")
        print(f"{'='*65}")

        df = pd.read_csv(triggers_path, low_memory=False)
        df["date"] = df["date"].astype(str)
        df_rank = pd.read_csv(ranking_path)

        # Baseline sans filtre
        df_is = df[df["date"] <= IS_END]
        df_oos = df[df["date"] > IS_END]
        base_is = _stats(df_is[df_is["result"] != "NOT_FILLED"])
        base_oos = _stats(df_oos[df_oos["result"] != "NOT_FILLED"])
        print(f"  BASELINE ─ "
              f"IS:  n={base_is['n']:3d}  WR={base_is['wr']:.0f}%  "
              f"PF={base_is['pf']:.2f}  P&L={base_is['pnl']:+.0f}$")
        print(f"           ─ "
              f"OOS: n={base_oos['n']:3d}  WR={base_oos['wr']:.0f}%  "
              f"PF={base_oos['pf']:.2f}  P&L={base_oos['pnl']:+.0f}$")

        # Sélection Top-N features (features continues + discrètes uniquement)
        valid_feats = df_rank.dropna(subset=["mwu_pval"])
        top_features = valid_feats.head(top_n)["feature"].tolist()
        # Exclure les catégorielles (regime) non testables avec seuil numérique
        top_features = [f for f in top_features
                        if f in df.columns and df[f].dtype != object]
        print(f"\n  Features testées ({len(top_features)}) : {top_features}")

        results = []

        # ── Tests individuels ──────────────────────────────────────────────
        print(f"\n  {'─'*63}")
        print(f"  {'Feature':<32} {'Dir':>3}  "
              f"{'IS':>3}  {'IS PF':>6}  {'IS P&L':>8}  "
              f"{'OOS':>3}  {'OOS PF':>6}  {'OOS P&L':>8}  {'':>8}")
        print(f"  {'─'*63}")

        for feat in top_features:
            res = test_single_filter(df, feat, ticker, chart_dir)
            if res is None:
                print(f"  {feat:<32}  (données insuffisantes)")
                continue
            results.append(res)
            flag = "VALID ✓" if res["valid_oos"] else "fail"
            dir_sym = ">" if res["direction"] == "gt" else "<"
            print(
                f"  {feat:<32} {dir_sym:>3}  "
                f"{res['is_n']:3d}  {res['is_pf']:6.3f}  {res['is_pnl']:+8.0f}$  "
                f"{res['oos_n']:3d}  {res['oos_pf']:6.3f}  {res['oos_pnl']:+8.0f}$  "
                f"{flag}"
            )

        # ── Tests combinés (AND) ───────────────────────────────────────────
        valid_singles = [r for r in results if r.get("is_pf", 0) > 1.0
                         and r.get("is_n", 0) >= OOS_N_MIN]
        top3_singles = sorted(valid_singles, key=lambda r: -r["is_pf"])[:3]

        if len(top3_singles) >= 2:
            print(f"\n  Tests combinés (AND) :")
            combos_tested = 0
            for r1, r2 in combinations(top3_singles, 2):
                if combos_tested >= N_TOP_COMBOS:
                    break
                res_c = test_combo_filter(df, r1, r2, ticker, chart_dir)
                results.append(res_c)
                combos_tested += 1
                flag = "VALID ✓" if res_c["valid_oos"] else "fail"
                print(
                    f"  {res_c['combo'][:50]:<52}  "
                    f"IS n={res_c['is_n']:3d} PF={res_c['is_pf']:.2f}  |  "
                    f"OOS n={res_c['oos_n']:3d} PF={res_c['oos_pf']:.2f}  {flag}"
                )

        # ── Export CSV ─────────────────────────────────────────────────────
        df_res = pd.DataFrame(results)
        out_path = data_dir / f"filter_results_{ticker}.csv"
        df_res.to_csv(out_path, index=False)

        # ── Résumé filtres validés OOS ──────────────────────────────────────
        print(f"\n  {'─'*63}")
        validated = df_res[df_res["valid_oos"] == True] if len(df_res) > 0 else pd.DataFrame()
        if len(validated) > 0:
            print(f"  FILTRES VALIDÉS OOS "
                  f"(PF≥{OOS_PF_MIN}, n≥{OOS_N_MIN}, P&L>0) :")
            for _, row in validated.sort_values("oos_pf", ascending=False).iterrows():
                delta_pf = row["oos_pf"] - row["base_oos_pf"]
                print(f"    [{row['filter_type']:6s}] {str(row['feature'])[:48]:<48}  "
                      f"OOS PF={row['oos_pf']:.3f} (Δ{delta_pf:+.3f})  "
                      f"n={row['oos_n']}  P&L={row['oos_pnl']:+.0f}$")
        else:
            print(f"  Aucun filtre ne valide les critères OOS "
                  f"(PF≥{OOS_PF_MIN}, n≥{OOS_N_MIN}, P&L>{OOS_PNL_MIN})")

        print(f"\n  Résultats → {out_path}")
        print(f"  Charts    → {chart_dir}/")


if __name__ == "__main__":
    main()
