"""
Option A — modélisation du coût d'annulation post-fill pour OPR v5.1.

Logique Option A en live (broker ProjectX sans ordres conditionnels) :
  1. Cassure détectée → place LIMIT à opr_high/opr_low
  2. LIMIT touché → fill confirmé
  3. Au moment du fill, calculer F2 = max excursion observée depuis trigger
  4. SI F2 < f2_min_atr → MARKET CLOSE immédiat (coût d'annulation)
  5. SINON → laisser courir avec SL/TP

Dans le backtest v5.1 actuel, les trades rejetés par F2_min sont marqués
NOT_FILLED avec pnl=0. Sous Option A, ils sont "filled briefly puis close"
avec un coût ~ 2×slippage_ticks × tick_size × dpp + commission_rt.

Ce script :
  1. Charge backtest_{T}_opr_v5_1.csv (résultat v5.1 avec rejets F2_min)
  2. Calcule le coût Option A par ticker (trades F2_min rejetés)
  3. Applique aussi les frictions standards (slippage entry+exit + commissions)
     sur les trades pris (TP/SL/TE)
  4. Compare v4_net / v5.1_net_pure / v5.1_net_optionA
  5. Sépare IS / OOS pour aligner avec le verdict de l'auditor
  6. Produit un rapport markdown + recalcule PF/DD/n_trades

Aucune écriture broker/, core/. Pure analyse de données.
"""

from pathlib import Path

import numpy as np
import pandas as pd

np.random.seed(42)

ROOT = Path(__file__).resolve().parent.parent
import sys

sys.path.insert(0, str(ROOT))

from config import (
    COMMISSION_RT_PER_CONTRACT,
    INSTRUMENTS,
    SLIPPAGE_TICKS_PER_TICKER,
)

REPORT_PATH = ROOT / "output" / "option_a_v5_1.md"
IS_END = "2025-09-30"
OOS_START = "2025-10-01"


# ===========================================================================
# Coûts par ticker
# ===========================================================================
def friction_per_trade(ticker: str) -> dict:
    """
    Coûts pour un trade normal (entry+exit) et un trade Option A (close immédiat).

    Standard fill (TP/SL/TE) :
      slippage entry + slippage exit + commission_RT
      = 2 × slippage_ticks × tick_size × dpp + commission_RT

    Option A cancel (F2_MIN rejected) :
      slippage entry LIMIT = 0 (LIMIT donne le prix demandé)
      slippage exit MARKET ~ 1 tick (cross bid/ask) + spread
      commission_RT
      Approche conservatrice : assumer slippage_ticks équivalent à un fill normal
      car le prix au close market peut s'écarter du LIMIT entrée.
      = 2 × slippage_ticks × tick_size × dpp + commission_RT
      (identique au coût standard — pessimiste)
    """
    tick_size = INSTRUMENTS[ticker]["tick_size"]
    dpp = INSTRUMENTS[ticker]["dollar_per_point"]
    slip = SLIPPAGE_TICKS_PER_TICKER[ticker]
    standard = 2 * slip * tick_size * dpp + COMMISSION_RT_PER_CONTRACT
    option_a_cancel = 2 * slip * tick_size * dpp + COMMISSION_RT_PER_CONTRACT
    return {
        "tick_size": tick_size,
        "dpp": dpp,
        "slip_ticks": slip,
        "standard_friction_per_trade": standard,
        "option_a_cancel_cost_per_trade": option_a_cancel,
    }


# ===========================================================================
# Chargement et split IS/OOS
# ===========================================================================
def load_v5_1_trades():
    parts = []
    for ticker in ["MES1", "NQ1", "YM1"]:
        path = ROOT / "output" / f"backtest_{ticker}_opr_v5_1.csv"
        df = pd.read_csv(path)
        df["ticker"] = ticker
        df["date"] = pd.to_datetime(df["date"])
        parts.append(df)
    df = pd.concat(parts, ignore_index=True)
    df["n_ct"] = pd.to_numeric(df.get("n_ct", 1), errors="coerce").fillna(1).astype(int)
    return df


def split_is_oos(df):
    is_end = pd.Timestamp(IS_END)
    oos_start = pd.Timestamp(OOS_START)
    df_is = df[df["date"] <= is_end].copy()
    df_oos = df[df["date"] >= oos_start].copy()
    return df_is, df_oos


# ===========================================================================
# Calcul des scénarios
# ===========================================================================
def compute_scenario(df, scenario: str, frictions: dict):
    """
    scenario:
      'v4_net'           = pnl brut tous trades filled - frictions standards
                            (ignorer les rejets v5_reject_reason : ils auraient été pris en v4)
      'v5_1_pure_net'    = pnl brut filled - frictions standards
                            (les rejets restent NOT_FILLED)
      'v5_1_optionA_net' = pnl brut filled - frictions standards
                            (les rejets deviennent -coût_annulation)
    Retourne {n_trades, pnl_total, pf, max_dd, wr}.
    """
    sub = df.copy()
    pnl_realized = []

    for _, row in sub.iterrows():
        ticker = row["ticker"]
        cost_std = frictions[ticker]["standard_friction_per_trade"]
        cost_cancel = frictions[ticker]["option_a_cancel_cost_per_trade"]
        n_ct = max(int(row.get("n_ct", 1)), 1)
        reject = row.get("v5_reject_reason", None)
        if pd.isna(reject) or reject == "":
            reject = None
        result = row.get("result", "NOT_FILLED")
        pnl_brut = float(row.get("pnl", 0.0))

        if scenario == "v4_net":
            # v4 ne connaît pas v5_reject. Pour reconstruire v4 :
            # - Si v5.1 a filled (TP/SL/TE) → trade pris en v4 aussi
            # - Si v5.1 rejected par F2_MIN → trade aurait été pris en v4
            #   (pnl pas dispo ici car NOT_FILLED dans v5.1 dataset)
            # → Approximation : on charge plutôt opr_v5_features.csv qui contient
            #   le pnl v4 pour tous les trades. À refaire séparément.
            # Pour simplifier ici : on prend les trades filled v5.1 et on ne tient
            # pas compte des rejets (sous-estime v4 mais reflète l'effet "additif"
            # uniquement). On va plutôt charger opr_v5_features.csv en parallèle.
            if result != "NOT_FILLED":
                pnl_realized.append(pnl_brut - cost_std * n_ct)
            # rejected_by_F2_min : on ne sait pas (NaN)
        elif scenario == "v5_1_pure_net":
            if result != "NOT_FILLED":
                pnl_realized.append(pnl_brut - cost_std * n_ct)
            # rejected → 0, pas ajouté (équivalent à NOT_FILLED, pnl=0)
        elif scenario == "v5_1_optionA_net":
            if result != "NOT_FILLED":
                pnl_realized.append(pnl_brut - cost_std * n_ct)
            elif reject == "F2_excursion_too_narrow":
                # Option A : on prend le trade au LIMIT, on close au market → coût
                pnl_realized.append(-cost_cancel * n_ct)
            # autres rejets (F1, F2_max, F3) : restent NOT_FILLED naturellement en live
            # → pas de coût car aucun ordre n'aurait été placé

    pnl_arr = np.array(pnl_realized) if pnl_realized else np.array([0.0])
    n = len(pnl_realized)
    pnl_total = float(pnl_arr.sum())
    wins = pnl_arr[pnl_arr > 0]
    losses = pnl_arr[pnl_arr < 0]
    pf = float(wins.sum() / abs(losses.sum())) if losses.sum() != 0 else float("inf")
    wr = float((pnl_arr > 0).mean()) if n > 0 else 0.0
    # DD = max underwater
    cum = pnl_arr.cumsum()
    if len(cum) > 0:
        running_max = np.maximum.accumulate(cum)
        max_dd = float((cum - running_max).min())
    else:
        max_dd = 0.0
    return {
        "n_trades": n,
        "pnl_total": round(pnl_total, 2),
        "pf": round(pf, 3) if pf != float("inf") else None,
        "max_dd": round(max_dd, 2),
        "wr_pct": round(100 * wr, 1),
    }


# ===========================================================================
# v4 baseline net (séparément à partir de opr_v5_features.csv)
# ===========================================================================
def v4_net_from_features(df_features, frictions):
    """
    v4 baseline net : tous les trades v4 filled - frictions standards.
    Aucun filtrage v5_reject (v4 ne filtre rien).
    """
    pnl_realized = []
    for _, row in df_features.iterrows():
        ticker = row["ticker"]
        cost_std = frictions[ticker]["standard_friction_per_trade"]
        result = row.get("result", "NOT_FILLED")
        pnl_brut = float(row.get("pnl", 0.0))
        if result != "NOT_FILLED":
            pnl_realized.append(pnl_brut - cost_std)
    pnl_arr = np.array(pnl_realized) if pnl_realized else np.array([0.0])
    n = len(pnl_realized)
    pnl_total = float(pnl_arr.sum())
    wins = pnl_arr[pnl_arr > 0]
    losses = pnl_arr[pnl_arr < 0]
    pf = float(wins.sum() / abs(losses.sum())) if losses.sum() != 0 else float("inf")
    wr = float((pnl_arr > 0).mean()) if n > 0 else 0.0
    cum = pnl_arr.cumsum()
    if len(cum) > 0:
        running_max = np.maximum.accumulate(cum)
        max_dd = float((cum - running_max).min())
    else:
        max_dd = 0.0
    return {
        "n_trades": n,
        "pnl_total": round(pnl_total, 2),
        "pf": round(pf, 3),
        "max_dd": round(max_dd, 2),
        "wr_pct": round(100 * wr, 1),
    }


# ===========================================================================
# Main
# ===========================================================================
print("=== Option A simulator — OPR v5.1 ===\n")

frictions = {t: friction_per_trade(t) for t in ["MES1", "NQ1", "YM1"]}
print("Coûts par trade :")
print(pd.DataFrame(frictions).T.round(2).to_string())
print()

df_v5_1 = load_v5_1_trades()
df_v4_features = pd.read_csv(ROOT / "output" / "opr_v5_features.csv")
df_v4_features["date"] = pd.to_datetime(df_v4_features["date"])

# Split IS/OOS
is_v5_1, oos_v5_1 = split_is_oos(df_v5_1)
is_v4_feat, oos_v4_feat = split_is_oos(df_v4_features)

# ============================================================================
# Distribution des rejets F2_MIN par ticker
# ============================================================================
print("\n=== Distribution des rejets dans v5.1 (full période) ===")
reject_dist = (
    df_v5_1.groupby(["ticker", "v5_reject_reason"], dropna=False).size().reset_index(name="count")
)
print(reject_dist.to_string(index=False))

# ============================================================================
# Scénarios OOS portfolio
# ============================================================================
oos_v4 = v4_net_from_features(oos_v4_feat, frictions)
oos_v5_1_pure = compute_scenario(oos_v5_1, "v5_1_pure_net", frictions)
oos_v5_1_optA = compute_scenario(oos_v5_1, "v5_1_optionA_net", frictions)

# Scénarios IS pour vérification stabilité
is_v4 = v4_net_from_features(is_v4_feat, frictions)
is_v5_1_pure = compute_scenario(is_v5_1, "v5_1_pure_net", frictions)
is_v5_1_optA = compute_scenario(is_v5_1, "v5_1_optionA_net", frictions)

# ============================================================================
# Par ticker (OOS)
# ============================================================================
oos_per_ticker = {}
for ticker in ["MES1", "NQ1", "YM1"]:
    sub_v4 = oos_v4_feat[oos_v4_feat["ticker"] == ticker]
    sub_v5 = oos_v5_1[oos_v5_1["ticker"] == ticker]
    oos_per_ticker[ticker] = {
        "v4_net": v4_net_from_features(sub_v4, frictions),
        "v5_1_pure_net": compute_scenario(sub_v5, "v5_1_pure_net", frictions),
        "v5_1_optionA_net": compute_scenario(sub_v5, "v5_1_optionA_net", frictions),
    }

# ============================================================================
# Compter les rejets F2_MIN par période et impact $
# ============================================================================
rejects_oos_by_ticker = {}
for ticker in ["MES1", "NQ1", "YM1"]:
    sub = oos_v5_1[
        (oos_v5_1["ticker"] == ticker) & (oos_v5_1["v5_reject_reason"] == "F2_excursion_too_narrow")
    ]
    n = len(sub)
    cost = frictions[ticker]["option_a_cancel_cost_per_trade"]
    rejects_oos_by_ticker[ticker] = {
        "n_rejected": n,
        "total_cancel_cost": round(n * cost, 2),
    }


# ============================================================================
# Rapport markdown
# ============================================================================
lines = []
lines.append("# Option A — simulation du coût d'annulation post-fill (OPR v5.1)\n")
lines.append(
    "Hypothèse : ProjectX ne supporte pas les ordres conditionnels. "
    "On place le LIMIT à opr_high → fill → close immédiat au market si F2 < f2_min_atr.\n"
)
lines.append(
    "Coût Option A par trade rejeté = 2×slippage_ticks×tick_size×dollar_per_point + commission_RT.\n"
)
lines.append(f"Walk-forward : IS ≤ {IS_END}, OOS ≥ {OOS_START}\n")
lines.append("---\n")

lines.append("## 1. Paramètres de coût par ticker\n")
fric_df = pd.DataFrame(
    {
        t: {
            "tick_size": v["tick_size"],
            "dollar_per_point": v["dpp"],
            "slippage_ticks": v["slip_ticks"],
            "standard_friction_$/trade": round(v["standard_friction_per_trade"], 2),
            "option_a_cancel_$/trade": round(v["option_a_cancel_cost_per_trade"], 2),
        }
        for t, v in frictions.items()
    }
).T
lines.append(fric_df.to_markdown())
lines.append("")

lines.append("## 2. Distribution des rejets v5.1 (full période)\n")
lines.append(reject_dist.to_markdown(index=False))
lines.append("")

lines.append("## 3. Rejets F2_min OOS — coût total Option A\n")
rej_df = pd.DataFrame(rejects_oos_by_ticker).T
rej_df.index.name = "ticker"
rej_df["cost_per_month"] = round(rej_df["total_cancel_cost"] / 7, 2)  # ~7 mois OOS
lines.append(rej_df.to_markdown())
lines.append("")

lines.append("## 4. Portfolio OOS — comparaison nette des 3 scénarios\n")
scen_df = pd.DataFrame(
    {
        "v4 (baseline net)": oos_v4,
        "v5.1 pure (filtré, NET)": oos_v5_1_pure,
        "v5.1 Option A (NET avec coût annulation)": oos_v5_1_optA,
    }
).T
lines.append(scen_df.to_markdown())
lines.append("")

lines.append("## 5. Par ticker OOS — net\n")
for ticker in ["MES1", "NQ1", "YM1"]:
    lines.append(f"### {ticker}\n")
    d = pd.DataFrame(
        {
            "v4 (net)": oos_per_ticker[ticker]["v4_net"],
            "v5.1 pure (net)": oos_per_ticker[ticker]["v5_1_pure_net"],
            "v5.1 Option A (net)": oos_per_ticker[ticker]["v5_1_optionA_net"],
        }
    ).T
    lines.append(d.to_markdown())
    lines.append("")

lines.append("## 6. Stabilité IS portfolio (sanity check)\n")
is_scen = pd.DataFrame(
    {
        "v4 (baseline net)": is_v4,
        "v5.1 pure (filtré, NET)": is_v5_1_pure,
        "v5.1 Option A (NET avec coût annulation)": is_v5_1_optA,
    }
).T
lines.append(is_scen.to_markdown())
lines.append("")

# Verdict
lines.append("## 7. Verdict net Option A\n")
delta_pnl_opta_vs_pure = oos_v5_1_optA["pnl_total"] - oos_v5_1_pure["pnl_total"]
delta_pnl_opta_vs_v4 = oos_v5_1_optA["pnl_total"] - oos_v4["pnl_total"]
delta_pf_vs_v4 = (oos_v5_1_optA["pf"] or 0) - oos_v4["pf"]
lines.append(
    f"- **Δ PnL (Option A − v5.1 pure)** : ${delta_pnl_opta_vs_pure:+,.2f} (coût total des annulations OOS)"
)
lines.append(f"- **Δ PnL (Option A − v4)** : ${delta_pnl_opta_vs_v4:+,.2f}")
lines.append(f"- **Δ PF (Option A − v4)** : {delta_pf_vs_v4:+.3f}")
lines.append(
    f"- **DD réduit ?** v4 DD = ${oos_v4['max_dd']:,.0f}, Option A DD = ${oos_v5_1_optA['max_dd']:,.0f} (réduction {100*(1 - oos_v5_1_optA['max_dd']/oos_v4['max_dd']):+.1f}% si dd<0)"
)

opta_pf = oos_v5_1_optA["pf"] or 0
verdict_emoji = "🟢" if opta_pf >= 1.5 else "🟡" if opta_pf >= 1.2 else "🔴"
lines.append(
    f"\n**Verdict statistique brut Option A net portfolio OOS : {verdict_emoji} PF={opta_pf:.2f}**\n"
)
lines.append("Critères SKILL.md :")
lines.append(f"- OOS PF ≥ 1.5 (🟢) : {'✅' if opta_pf >= 1.5 else '❌'}")
lines.append(f"- OOS PnL > 0 : {'✅' if oos_v5_1_optA['pnl_total'] > 0 else '❌'}")
lines.append(f"- OOS n ≥ 50 : {'✅' if oos_v5_1_optA['n_trades'] >= 50 else '❌'}")
lines.append("")

# Save
REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
print(f"\nRapport sauvegardé : {REPORT_PATH}")
print("\nOOS portfolio :")
print(
    f"  v4 net          : PnL ${oos_v4['pnl_total']:,.0f}  PF {oos_v4['pf']}  DD ${oos_v4['max_dd']:,.0f}"
)
print(
    f"  v5.1 pure net   : PnL ${oos_v5_1_pure['pnl_total']:,.0f}  PF {oos_v5_1_pure['pf']}  DD ${oos_v5_1_pure['max_dd']:,.0f}"
)
print(
    f"  v5.1 OptionA net: PnL ${oos_v5_1_optA['pnl_total']:,.0f}  PF {oos_v5_1_optA['pf']}  DD ${oos_v5_1_optA['max_dd']:,.0f}"
)
