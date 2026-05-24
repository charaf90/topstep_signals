"""
core/robustness.py — Outils de robustesse statistique pour évaluation de stratégies.

Ce module est conçu pour être appelé depuis optimize.py (et le skill new-strategy)
afin de fournir un cadre uniforme et reproductible pour les analyses post-optimisation.

Fonctions exposées :
- block_bootstrap          : stationary bootstrap (Politis & Romano) sur P&L / PF / Sharpe
- monte_carlo_drawdown     : distribution du DD via permutation des trades
- probabilistic_sharpe_ratio : PSR (Bailey & López de Prado, 2012) corrigé skew+kurt
- bonferroni_threshold     : seuil de bootstrap corrigé pour N tests
- reality_check            : test de White (2000) simplifié, comparaison N stratégies
- regime_stress_test       : performance par régime (trending/ranging/vol/macro)
- worst_case_clustering    : analyse de concentration des N pires trades
- run_full_robustness      : pipeline complet, retourne un dict structuré
- format_summary_markdown  : sortie formatée pour insertion dans rapport_<id>.md
- format_telegram_html     : sortie HTML pour notifications Telegram

Conventions :
- DataFrame de trades au schéma standard du projet :
    date, dir, entry, sl, tp, sl_dist, tp_dist, rr, n_ct, result, pnl,
    fill_time, exit_time, exit, regime
  + colonnes optionnelles : pnl_gross, adx, atr_pct, is_macro_day, ticker
- `pnl` = P&L net (canonique, slippage + commissions inclus)
- Reproductibilité : seed=42 par défaut, configurable.
- Aucune dépendance externe au-delà de numpy / pandas / scipy.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
from scipy import stats

# ═══════════════════════════════════════════════════════════════════════════════
# 1. STATIONARY BOOTSTRAP
# ═══════════════════════════════════════════════════════════════════════════════


def _stationary_bootstrap_indices(
    n: int,
    mean_block_size: float,
    rng: np.random.Generator,
) -> np.ndarray:
    """
    Génère un échantillon d'indices via stationary bootstrap (Politis & Romano 1994).

    À chaque pas : avec probabilité p = 1/mean_block_size on tire une nouvelle
    position aléatoire, sinon on continue le bloc courant. Préserve la
    stationnarité asymptotique des séries temporelles, contrairement au
    bootstrap iid qui détruit toute structure de dépendance.
    """
    p = 1.0 / max(2.0, mean_block_size)
    indices = np.empty(n, dtype=np.int64)
    indices[0] = rng.integers(0, n)
    for i in range(1, n):
        if rng.random() < p:
            indices[i] = rng.integers(0, n)
        else:
            indices[i] = (indices[i - 1] + 1) % n
    return indices


def block_bootstrap(
    trades: pd.DataFrame,
    n_iterations: int = 1000,
    metric: str = "profit_factor",
    threshold: float = 1.0,
    seed: int = 42,
) -> dict:
    """
    Stationary bootstrap sur la séquence des P&L.

    Args:
        trades       : DataFrame de trades (colonne `pnl` requise).
        n_iterations : nombre de rééchantillonnages.
        metric       : "profit_factor" | "pnl" | "sharpe".
        threshold    : seuil pour le calcul du `p_above_threshold`
                       (1.0 pour PF, 0.0 pour P&L et Sharpe).
        seed         : graine reproductible.

    Returns:
        dict avec mean, median, p5, p95, p_above_threshold, distribution.
    """
    if "pnl" not in trades.columns:
        return {"error": "Colonne 'pnl' manquante"}
    pnls = trades["pnl"].to_numpy()
    n = len(pnls)
    if n < 10:
        return {"error": f"Trop peu de trades ({n} < 10)"}

    block_size = max(2, int(math.sqrt(n)))
    rng = np.random.default_rng(seed)
    distribution = np.empty(n_iterations, dtype=np.float64)

    for it in range(n_iterations):
        idx = _stationary_bootstrap_indices(n, block_size, rng)
        sample = pnls[idx]

        if metric == "profit_factor":
            gains = sample[sample > 0].sum()
            losses = -sample[sample < 0].sum()
            value = gains / losses if losses > 0 else (np.inf if gains > 0 else 0.0)
        elif metric == "pnl":
            value = float(sample.sum())
        elif metric == "sharpe":
            std = sample.std(ddof=1)
            value = float(sample.mean() / std * math.sqrt(252)) if std > 0 else 0.0
        else:
            raise ValueError(f"Métrique inconnue : {metric}")

        distribution[it] = value

    finite = distribution[np.isfinite(distribution)]
    p_above = float((distribution > threshold).mean() * 100)

    return {
        "metric": metric,
        "n_trades": int(n),
        "block_size": int(block_size),
        "n_iterations": int(n_iterations),
        "mean": float(finite.mean()) if finite.size else float("nan"),
        "median": float(np.median(finite)) if finite.size else float("nan"),
        "p5": float(np.percentile(finite, 5)) if finite.size else float("nan"),
        "p95": float(np.percentile(finite, 95)) if finite.size else float("nan"),
        "p_above_threshold": p_above,
        "threshold": float(threshold),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 2. MONTE CARLO PERMUTATION DU DD
# ═══════════════════════════════════════════════════════════════════════════════


def monte_carlo_drawdown(
    trades: pd.DataFrame,
    n_iterations: int = 1000,
    topstep_dd_remaining: float = 2000.0,
    seed: int = 42,
) -> dict:
    """
    Permute aléatoirement l'ordre des trades pour estimer la distribution du DD max.

    Idée : le DD observé n'est qu'une réalisation parmi les ordres possibles des
    mêmes trades. La distribution donne la queue (P95, P99) du DD pire-cas.

    Args:
        topstep_dd_remaining : limite Topstep restante en valeur absolue (positive).
                               Le module compare le DD (négatif) à -limite.

    Returns:
        dict avec dd_median, dd_p95_worst (queue), dd_p99_worst, dd_breach_pct.
        dd_p95_worst correspond au "DD au-delà duquel on tombe dans 5% des cas".
    """
    if "pnl" not in trades.columns:
        return {"error": "Colonne 'pnl' manquante"}
    pnls = trades["pnl"].to_numpy()
    n = len(pnls)
    if n < 10:
        return {"error": f"Trop peu de trades ({n} < 10)"}

    rng = np.random.default_rng(seed)
    dd_distribution = np.empty(n_iterations, dtype=np.float64)

    for it in range(n_iterations):
        permuted = rng.permutation(pnls)
        cum = np.cumsum(permuted)
        peak = np.maximum.accumulate(cum)
        dd_distribution[it] = (cum - peak).min()  # le plus négatif

    breach_pct = float((dd_distribution < -topstep_dd_remaining).mean() * 100)

    return {
        "n_iterations": int(n_iterations),
        "dd_median": float(np.median(dd_distribution)),
        "dd_p95_worst": float(np.percentile(dd_distribution, 5)),  # 95% au-dessus
        "dd_p99_worst": float(np.percentile(dd_distribution, 1)),  # 99% au-dessus
        "dd_worst": float(dd_distribution.min()),
        "dd_topstep_breach_pct": breach_pct,
        "topstep_dd_remaining": float(topstep_dd_remaining),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 3. PROBABILISTIC SHARPE RATIO (Bailey & López de Prado 2012)
# ═══════════════════════════════════════════════════════════════════════════════


def probabilistic_sharpe_ratio(
    pnls: np.ndarray | pd.Series,
    sr_target: float = 0.0,
    annualization_factor: int = 252,
) -> dict:
    """
    PSR — probabilité que le vrai Sharpe Ratio dépasse `sr_target`.

    Formule (Bailey & López de Prado 2012) :
        PSR = Φ((SR̂ - SR*) · √(n-1) / √(1 - γ̂₃·SR̂ + (γ̂₄ - 1)/4·SR̂²))

    Tient compte de la non-normalité des rendements (skew + kurtosis).
    """
    arr = np.asarray(pnls, dtype=np.float64)
    n = len(arr)
    if n < 30:
        return {"error": f"Trop peu d'observations pour PSR fiable ({n} < 30)"}

    mu = arr.mean()
    sigma = arr.std(ddof=1)
    if sigma == 0:
        return {"error": "Écart-type nul"}

    sr = mu / sigma  # Sharpe par trade
    sr_ann = sr * math.sqrt(annualization_factor)

    skew = float(stats.skew(arr, bias=False))
    kurt = float(stats.kurtosis(arr, fisher=False, bias=False))  # 3 = normal

    denom_sq = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr**2
    if denom_sq <= 0 or not np.isfinite(denom_sq):
        return {"error": "Dénominateur invalide pour PSR"}

    psr_score = (sr - sr_target) * math.sqrt(n - 1) / math.sqrt(denom_sq)
    psr = float(stats.norm.cdf(psr_score))

    return {
        "n": int(n),
        "sharpe_per_trade": float(sr),
        "sharpe_annualized": float(sr_ann),
        "skewness": skew,
        "kurtosis": kurt,
        "sr_target": float(sr_target),
        "psr": psr,
        "psr_pct": float(psr * 100),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 4. CORRECTION DE BONFERRONI
# ═══════════════════════════════════════════════════════════════════════════════


def bonferroni_threshold(alpha: float = 0.05, n_tests: int = 1) -> dict:
    """
    Seuil corrigé de Bonferroni pour multiple testing.

    Si N configurations ont été testées au PARAM_GRID, le seuil de bootstrap
    pour un risque α global doit être (1 - α/N) × 100, pas (1 - α) × 100.

    Exemple : grid 4×4 = 16 tests, α=0.05 → bootstrap ≥ 99.69 % requis.
    """
    if n_tests < 1:
        raise ValueError("n_tests doit être ≥ 1")
    if not 0 < alpha < 1:
        raise ValueError("alpha doit être dans (0, 1)")
    p_corrected = alpha / n_tests
    return {
        "alpha": float(alpha),
        "n_tests": int(n_tests),
        "p_corrected": float(p_corrected),
        "bootstrap_threshold_pct": float((1 - p_corrected) * 100),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 5. WHITE'S REALITY CHECK (simplifié)
# ═══════════════════════════════════════════════════════════════════════════════


def reality_check(
    pnls_per_strategy: dict[str, np.ndarray],
    n_iterations: int = 1000,
    seed: int = 42,
) -> dict:
    """
    Reality Check (White 2000) simplifié.

    H0 : aucune des N stratégies n'a une espérance de rendement > 0.
    Rejette H0 si la meilleure stratégie observée bat le bootstrap des moyennes
    centrées (sous H0) au seuil 5 %.

    Note : version simplifiée, hypothèse de stratégies indépendantes.
    Pour SPA test complet (Hansen 2005), nécessiterait l'alignement temporel
    des P&L journaliers.
    """
    rng = np.random.default_rng(seed)

    means = {
        name: float(np.mean(pnls)) for name, pnls in pnls_per_strategy.items() if len(pnls) > 0
    }
    if not means:
        return {"error": "Aucune stratégie valide"}

    best_name = max(means, key=means.get)
    best_mean = means[best_name]

    # Bootstrap de la statistique max sous H0 (moyennes centrées)
    boot_max = np.empty(n_iterations, dtype=np.float64)
    for it in range(n_iterations):
        sample_means = []
        for name, pnls in pnls_per_strategy.items():
            if len(pnls) == 0:
                continue
            sample = rng.choice(pnls, size=len(pnls), replace=True)
            centered = sample.mean() - means[name]  # centrage sous H0
            sample_means.append(centered)
        boot_max[it] = max(sample_means) if sample_means else 0.0

    if best_mean <= 0:
        p_value = 1.0
    else:
        p_value = float((boot_max >= best_mean).mean())

    return {
        "n_strategies": len(means),
        "best_strategy": best_name,
        "best_mean": float(best_mean),
        "p_value": float(p_value),
        "rejected_h0_at_5pct": bool(p_value < 0.05),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 6. STRESS PAR RÉGIME
# ═══════════════════════════════════════════════════════════════════════════════


def _regime_stats(subset: pd.DataFrame, label: str) -> dict:
    """Statistiques d'un sous-ensemble de trades."""
    if len(subset) == 0:
        return {
            "regime": label,
            "n_trades": 0,
            "pf": None,
            "pnl_total": 0.0,
            "wr_pct": None,
            "verdict": "—",
        }
    gains = float(subset.loc[subset["pnl"] > 0, "pnl"].sum())
    losses = float(-subset.loc[subset["pnl"] < 0, "pnl"].sum())
    pf = gains / losses if losses > 0 else (float("inf") if gains > 0 else 0.0)
    wr = float((subset["pnl"] > 0).mean() * 100)

    if not np.isfinite(pf) or pf >= 1.3:
        verdict = "✅"
    elif pf >= 1.0:
        verdict = "⚠️"
    else:
        verdict = "❌"

    return {
        "regime": label,
        "n_trades": int(len(subset)),
        "pf": round(float(pf), 2) if np.isfinite(pf) else None,
        "pnl_total": round(float(subset["pnl"].sum()), 2),
        "wr_pct": round(wr, 1),
        "verdict": verdict,
    }


def regime_stress_test(trades: pd.DataFrame) -> pd.DataFrame:
    """
    Stress test par régime de marché.

    Utilise les colonnes optionnelles `regime`, `atr_pct`, `is_macro_day`.
    Retourne un DataFrame {regime, n_trades, pf, pnl_total, wr_pct, verdict}.
    """
    rows = []

    if "regime" in trades.columns:
        rows.append(_regime_stats(trades[trades["regime"] == "trending"], "trending"))
        rows.append(_regime_stats(trades[trades["regime"] == "ranging"], "ranging"))
        rows.append(_regime_stats(trades[trades["regime"] == "neutral"], "neutral"))

    if "atr_pct" in trades.columns and trades["atr_pct"].notna().any():
        rows.append(_regime_stats(trades[trades["atr_pct"] >= 0.75], "vol_high"))
        rows.append(_regime_stats(trades[trades["atr_pct"] <= 0.25], "vol_low"))

    if "is_macro_day" in trades.columns:
        macro = trades[trades["is_macro_day"] == True]
        non_macro = trades[trades["is_macro_day"] == False]
        rows.append(_regime_stats(macro, "macro_day"))
        rows.append(_regime_stats(non_macro, "non_macro_day"))

    return (
        pd.DataFrame(rows)
        if rows
        else pd.DataFrame(columns=["regime", "n_trades", "pf", "pnl_total", "wr_pct", "verdict"])
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 7. WORST-CASE CLUSTERING
# ═══════════════════════════════════════════════════════════════════════════════


def worst_case_clustering(trades: pd.DataFrame, n_worst: int = 20) -> dict:
    """
    Analyse de concentration des N pires trades.

    Détecte si les pertes extrêmes sont concentrées sur :
    - une période courte (< 14 jours)
    - un seul actif
    - un type de jour (lundi, jour macro, etc.)
    """
    if len(trades) < n_worst:
        return {"error": f"Moins de {n_worst} trades ({len(trades)})"}

    worst = trades.nsmallest(n_worst, "pnl").copy()
    worst["date_dt"] = pd.to_datetime(worst["date"])
    date_range = (worst["date_dt"].max() - worst["date_dt"].min()).days

    out: dict = {
        "n_worst": int(n_worst),
        "total_loss": round(float(worst["pnl"].sum()), 2),
        "mean_loss": round(float(worst["pnl"].mean()), 2),
        "date_range_days": int(date_range),
        "concentration_period": "courte (<14j)" if date_range < 14 else "étalée",
    }

    if "ticker" in worst.columns:
        counts = worst["ticker"].value_counts()
        out["ticker_concentration"] = {k: int(v) for k, v in counts.items()}
        max_ticker_pct = float(counts.max() / n_worst * 100)
        out["max_ticker_pct"] = round(max_ticker_pct, 1)

    weekday_counts = worst["date_dt"].dt.day_name().value_counts().to_dict()
    out["weekday_counts"] = {k: int(v) for k, v in weekday_counts.items()}

    if "is_macro_day" in worst.columns:
        out["macro_day_pct"] = round(float(worst["is_macro_day"].mean() * 100), 1)

    # Drapeau global
    flags = []
    if date_range < 14:
        flags.append("période courte")
    if "max_ticker_pct" in out and out["max_ticker_pct"] > 70:
        flags.append("actif unique")
    if "macro_day_pct" in out and out["macro_day_pct"] > 40:
        flags.append("biais macro")
    out["concentration_flags"] = flags

    return out


# ═══════════════════════════════════════════════════════════════════════════════
# 8. PIPELINE COMPLET
# ═══════════════════════════════════════════════════════════════════════════════


def run_full_robustness(
    trades: pd.DataFrame,
    n_strategies_tested: int = 1,
    topstep_dd_remaining: float = 2000.0,
    seed: int = 42,
) -> dict:
    """
    Pipeline complet à appeler depuis optimize.py.

    Args:
        trades               : DataFrame OOS au schéma standard.
        n_strategies_tested  : taille du PARAM_GRID (pour Bonferroni).
        topstep_dd_remaining : limite DD Topstep restante (valeur absolue).
        seed                 : graine reproductible.

    Returns:
        dict avec toutes les analyses, prêt à sérialiser ou injecter dans un rapport.
    """
    pnls = trades["pnl"].to_numpy() if "pnl" in trades.columns else np.array([])

    return {
        "n_trades": int(len(trades)),
        "bootstrap_pf": block_bootstrap(trades, metric="profit_factor", threshold=1.0, seed=seed),
        "bootstrap_pnl": block_bootstrap(trades, metric="pnl", threshold=0.0, seed=seed),
        "bootstrap_sharpe": block_bootstrap(trades, metric="sharpe", threshold=0.0, seed=seed),
        "monte_carlo_dd": monte_carlo_drawdown(
            trades, topstep_dd_remaining=topstep_dd_remaining, seed=seed
        ),
        "bonferroni": bonferroni_threshold(alpha=0.05, n_tests=n_strategies_tested),
        "psr": probabilistic_sharpe_ratio(pnls, sr_target=0.0),
        "regime_stress": regime_stress_test(trades).to_dict("records"),
        "worst_clustering": worst_case_clustering(trades, n_worst=20),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 9. FORMATAGE DE SORTIE
# ═══════════════════════════════════════════════════════════════════════════════


def format_summary_markdown(results: dict) -> str:
    """Génère un bloc markdown prêt à insérer dans output/rapport_<id>.md."""
    lines = ["## Robustesse statistique\n"]

    # Bootstrap
    bp = results.get("bootstrap_pf", {})
    if "error" not in bp:
        lines.append(
            f"**Bootstrap stationnaire** (n_trades={bp['n_trades']}, "
            f"block_size={bp['block_size']}, {bp['n_iterations']} itér.)"
        )
        lines.append("")
        lines.append("| Métrique | P5 | Médian | P95 | P(> seuil) |")
        lines.append("|---|---|---|---|---|")
        for key, label, fmt in [
            ("bootstrap_pf", "Profit Factor", "{:.2f}"),
            ("bootstrap_pnl", "P&L net ($)", "{:+.0f}"),
            ("bootstrap_sharpe", "Sharpe annualisé", "{:.2f}"),
        ]:
            r = results.get(key, {})
            if "error" in r:
                continue
            lines.append(
                f"| {label} | {fmt.format(r['p5'])} | {fmt.format(r['median'])} "
                f"| {fmt.format(r['p95'])} | {r['p_above_threshold']:.1f} % |"
            )
        lines.append("")

    # Bonferroni
    bf = results.get("bonferroni", {})
    if bf:
        lines.append(
            f"**Correction Bonferroni** : {bf['n_tests']} tests · "
            f"seuil bootstrap requis ≥ **{bf['bootstrap_threshold_pct']:.2f} %**"
        )
        lines.append("")

    # PSR
    psr = results.get("psr", {})
    if "error" not in psr:
        lines.append(
            f"**Probabilistic Sharpe Ratio (PSR)** : "
            f"Sharpe ann. = {psr['sharpe_annualized']:.2f} · "
            f"PSR(0) = **{psr['psr_pct']:.1f} %** "
            f"(skew={psr['skewness']:.2f}, kurt={psr['kurtosis']:.2f})"
        )
        lines.append("")

    # Monte Carlo DD
    mc = results.get("monte_carlo_dd", {})
    if "error" not in mc:
        lines.append(f"**Monte Carlo permutation du DD** ({mc['n_iterations']} itérations)")
        lines.append("")
        lines.append("| Métrique | Valeur |")
        lines.append("|---|---|")
        lines.append(f"| DD médian | ${mc['dd_median']:+.0f} |")
        lines.append(f"| DD P95 (queue) | ${mc['dd_p95_worst']:+.0f} |")
        lines.append(f"| DD P99 (queue) | ${mc['dd_p99_worst']:+.0f} |")
        lines.append(f"| DD pire-cas | ${mc['dd_worst']:+.0f} |")
        lines.append(
            f"| P(DD > limite Topstep ${mc['topstep_dd_remaining']:.0f}) | "
            f"{mc['dd_topstep_breach_pct']:.1f} % |"
        )
        lines.append("")

    # Régimes
    regime_rows = results.get("regime_stress", [])
    if regime_rows:
        lines.append("**Stress par régime**")
        lines.append("")
        lines.append("| Régime | n | PF | P&L | WR | |")
        lines.append("|---|---|---|---|---|---|")
        for r in regime_rows:
            pf = f"{r['pf']:.2f}" if r.get("pf") is not None else "—"
            wr = f"{r['wr_pct']:.0f}%" if r.get("wr_pct") is not None else "—"
            lines.append(
                f"| {r['regime']} | {r['n_trades']} | {pf} | "
                f"${r['pnl_total']:+.0f} | {wr} | {r['verdict']} |"
            )
        lines.append("")

    # Clustering
    wc = results.get("worst_clustering", {})
    if "error" not in wc:
        lines.append(
            f"**Worst-case clustering** (top {wc['n_worst']} pires trades) : "
            f"perte cumulée ${wc['total_loss']:+.0f} sur "
            f"{wc['date_range_days']} jours ({wc['concentration_period']})"
        )
        if wc.get("concentration_flags"):
            lines.append(f"⚠️ Drapeaux : {', '.join(wc['concentration_flags'])}")
        lines.append("")

    return "\n".join(lines)


def format_telegram_html(results: dict, strategy_id: str) -> str:
    """Génère un message Telegram HTML synthétique."""
    bp = results.get("bootstrap_pf", {})
    psr = results.get("psr", {})
    mc = results.get("monte_carlo_dd", {})
    bf = results.get("bonferroni", {})

    bootstrap_pct = bp.get("p_above_threshold", float("nan"))
    bf_threshold = bf.get("bootstrap_threshold_pct", float("nan"))
    pass_bonf = "✅" if bootstrap_pct >= bf_threshold else "❌"
    psr_pct = psr.get("psr_pct", float("nan"))
    dd_p95 = mc.get("dd_p95_worst", float("nan"))
    breach = mc.get("dd_topstep_breach_pct", float("nan"))

    return (
        f"🔬 <b>Robustesse {strategy_id}</b>\n\n"
        f"Bootstrap PF (block) : <b>{bootstrap_pct:.1f} %</b>\n"
        f"Bonferroni ({bf.get('n_tests', '?')} tests) : seuil "
        f"{bf_threshold:.1f} % {pass_bonf}\n"
        f"PSR(0) : <b>{psr_pct:.1f} %</b>\n"
        f"MC DD P95 : <b>${dd_p95:+.0f}</b>\n"
        f"P(DD &gt; limite Topstep) : <b>{breach:.1f} %</b>"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 10. AUTO-TEST sur données synthétiques
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    """Exécutable directement : python -m core.robustness"""
    print("═" * 70)
    print("  AUTO-TEST core/robustness.py — données synthétiques")
    print("═" * 70)

    # Génère 200 trades synthétiques avec edge léger positif
    rng = np.random.default_rng(123)
    n = 200
    pnls = rng.normal(loc=15, scale=80, size=n)  # espérance +$15, vol $80
    dates = pd.date_range("2025-10-01", periods=n, freq="3h").strftime("%Y-%m-%d")
    regimes = rng.choice(["trending", "ranging", "neutral"], size=n, p=[0.4, 0.4, 0.2])
    tickers = rng.choice(["MES1", "NQ1", "YM1"], size=n)

    df = pd.DataFrame(
        {
            "date": dates,
            "dir": rng.choice(["long", "short"], size=n),
            "entry": 100.0,
            "sl": 99.0,
            "tp": 102.0,
            "sl_dist": 1.0,
            "tp_dist": 2.0,
            "rr": 2.0,
            "n_ct": 1,
            "result": np.where(pnls > 0, "TP", "SL"),
            "pnl": pnls.round(2),
            "fill_time": pd.NaT,
            "exit_time": pd.NaT,
            "exit": 100.0,
            "regime": regimes,
            "ticker": tickers,
            "atr_pct": rng.uniform(0, 1, size=n),
            "is_macro_day": rng.random(size=n) < 0.1,
        }
    )

    print(
        f"\nGénéré : {n} trades, P&L total = ${df['pnl'].sum():+.0f}, "
        f"WR = {(df['pnl'] > 0).mean() * 100:.1f}%\n"
    )

    results = run_full_robustness(
        df,
        n_strategies_tested=16,  # ex : grid 4×4
        topstep_dd_remaining=2000.0,
        seed=42,
    )

    print(format_summary_markdown(results))
    print("─" * 70)
    print("Telegram preview :\n")
    print(format_telegram_html(results, "test-v1"))
    print()
