"""Moteur de stats v4 — trades unifiés, agrégats, verdict sizing.

Python pur (zéro dépendance Dash) — vérifiable en CLI :
    python -m tools.dashboard_v4.stats

Principes :
- Base des trades = ``placed_tags`` CLOSED du state (seule source qui porte la
  stratégie ET le risque engagé), enrichie du P&L NET broker (fees incluses)
  par jointure order_id puis contract+pnl proche.
- Métrique primaire du diagnostic sizing : ``r_multiple = pnl_net / risk_usd``
  — comparable entre stratégies de sizing différent.
- Rigueur à n petit : win rate borné par l'intervalle de Wilson, expectancy R
  bornée par un CI bootstrap percentile — jamais de verdict binaire trompeur.
"""

from __future__ import annotations

import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.dashboard_v4.accounts import AccountConfig  # noqa: E402
from tools.dashboard_v4.datasource import closed_tag_trades, read_state  # noqa: E402
from tools.dashboard_v4.strategy_refs import STRATEGY_REFS, StrategyRef  # noqa: E402

Z95 = 1.959964  # quantile normal 97.5 %


# ──────────────────────────────────────────────────────────────────────────────
# Intervalles de confiance
# ──────────────────────────────────────────────────────────────────────────────


def wilson_ci(wins: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Intervalle de Wilson 95 % sur une proportion (formule fermée)."""
    if n == 0:
        return (0.0, 1.0)
    p = wins / n
    denom = 1 + z**2 / n
    center = (p + z**2 / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2))) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def n_for_halfwidth(p: float, half: float = 0.15, z: float = Z95) -> int:
    """n requis pour une demi-largeur Wilson ≈ ``half`` autour de p
    (approximation normale) — sert au message « verdict fiable dans ~Y trades »."""
    p = min(max(p, 0.05), 0.95)
    return math.ceil(z**2 * p * (1 - p) / half**2)


def bootstrap_ci(
    values: list[float], n_boot: int = 2000, alpha: float = 0.05, seed: int = 42
) -> tuple[float, float, float]:
    """CI percentile bootstrap (lo, hi, mean) sur la moyenne des valeurs."""
    if not values:
        return (0.0, 0.0, 0.0)
    arr = np.asarray(values, dtype=float)
    if len(arr) == 1:
        v = float(arr[0])
        return (v, v, v)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(arr), size=(n_boot, len(arr)))
    means = arr[idx].mean(axis=1)
    lo, hi = np.percentile(means, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return (float(lo), float(hi), float(arr.mean()))


# ──────────────────────────────────────────────────────────────────────────────
# Trades unifiés (state ∪ broker)
# ──────────────────────────────────────────────────────────────────────────────


def unified_trades(state: dict | None, broker_pairs: list[dict] | None) -> list[dict]:
    """Trades clos avec stratégie + risque (state) et P&L net broker si matché.

    Jointure : order_id exact → fallback contract_id + |Δ pnl brut| < $5 (chaque
    pair broker n'est consommée qu'une fois). Non matché → pnl brut du state,
    ``source="state_only"`` (affiché atténué dans l'UI).
    """
    if not state:
        return []
    base = closed_tag_trades(state)
    pairs = list(broker_pairs or [])
    by_order: dict = {}
    for p in pairs:
        oid = p.get("order_id_open")
        if oid is not None:
            by_order.setdefault(oid, []).append(p)
    consumed: set[int] = set()

    out = []
    for t in base:
        match = None
        cands = by_order.get(t.get("order_id"), [])
        for p in cands:
            if id(p) not in consumed:
                match = p
                break
        if match is None:
            for p in pairs:
                if id(p) in consumed:
                    continue
                if p.get("contract_id") != t.get("contract_id"):
                    continue
                if abs(p.get("pnl_gross", 0.0) - t["close_pnl"]) < 5.0:
                    match = p
                    break
        if match is not None:
            consumed.add(id(match))
            pnl_net = float(match["pnl_net"])
            fees = float(match.get("fees", 0.0))
            source = "broker"
        else:
            pnl_net = t["close_pnl"]
            fees = 0.0
            source = "state_only"
        risk = t["risk_usd"]
        out.append(
            {
                **t,
                "pnl_net": round(pnl_net, 2),
                "fees": round(fees, 2),
                "source": source,
                "r_multiple": round(pnl_net / risk, 3) if risk > 0 else None,
            }
        )
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Agrégats
# ──────────────────────────────────────────────────────────────────────────────


def _base_agg() -> dict:
    return {
        "n": 0,
        "wins": 0,
        "losses": 0,
        "pnl_net": 0.0,
        "fees": 0.0,
        "gross_win": 0.0,
        "gross_loss": 0.0,
        "risks": [],
        "r_values": [],
        "timestamps": [],
        "equity": [],
        "trades": [],
    }


def strategy_aggregates(trades: list[dict]) -> dict[str, dict]:
    """Stats par stratégie : n, WR ± Wilson, PF, expectancy $ et R ± bootstrap."""
    by: dict[str, dict] = defaultdict(_base_agg)
    for t in trades:
        d = by[t["strategy"]]
        pnl = t["pnl_net"]
        d["n"] += 1
        d["pnl_net"] += pnl
        d["fees"] += t["fees"]
        if pnl > 0:
            d["wins"] += 1
            d["gross_win"] += pnl
        elif pnl < 0:
            d["losses"] += 1
            d["gross_loss"] += abs(pnl)
        if t["risk_usd"] > 0:
            d["risks"].append(t["risk_usd"])
        if t["r_multiple"] is not None:
            d["r_values"].append(t["r_multiple"])
        d["timestamps"].append(t["fill_time"])
        d["equity"].append(round(d["pnl_net"], 2))
        d["trades"].append(t)
    for d in by.values():
        n = d["n"]
        d["wr"] = d["wins"] / n if n else 0.0
        d["wr_ci"] = wilson_ci(d["wins"], n)
        d["pf"] = (d["gross_win"] / d["gross_loss"]) if d["gross_loss"] > 0 else float("inf")
        d["expectancy_usd"] = d["pnl_net"] / n if n else 0.0
        lo, hi, mean = bootstrap_ci(d["r_values"])
        d["expectancy_r"] = mean
        d["expectancy_r_ci"] = (lo, hi)
        d["avg_risk_used"] = sum(d["risks"]) / len(d["risks"]) if d["risks"] else 0.0
    return dict(by)


def portfolio_aggregates(trades: list[dict]) -> dict:
    """Agrégats toutes stratégies confondues + contributions + jours +/−."""
    agg = _base_agg()
    daily: dict[str, float] = defaultdict(float)
    contrib: dict[str, float] = defaultdict(float)
    for t in trades:
        pnl = t["pnl_net"]
        agg["n"] += 1
        agg["pnl_net"] += pnl
        agg["fees"] += t["fees"]
        if pnl > 0:
            agg["wins"] += 1
            agg["gross_win"] += pnl
        elif pnl < 0:
            agg["losses"] += 1
            agg["gross_loss"] += abs(pnl)
        if t["r_multiple"] is not None:
            agg["r_values"].append(t["r_multiple"])
        agg["timestamps"].append(t["fill_time"])
        agg["equity"].append(round(agg["pnl_net"], 2))
        agg["trades"].append(t)
        d = (t["fill_time"] or "")[:10]
        if d:
            daily[d] += pnl
        contrib[t["strategy"]] += pnl
    n = agg["n"]
    agg["wr"] = agg["wins"] / n if n else 0.0
    agg["wr_ci"] = wilson_ci(agg["wins"], n)
    agg["pf"] = (agg["gross_win"] / agg["gross_loss"]) if agg["gross_loss"] > 0 else float("inf")
    agg["expectancy_usd"] = agg["pnl_net"] / n if n else 0.0
    lo, hi, mean = bootstrap_ci(agg["r_values"])
    agg["expectancy_r"] = mean
    agg["expectancy_r_ci"] = (lo, hi)
    agg["daily"] = dict(daily)
    agg["win_days"] = sum(1 for v in daily.values() if v > 0)
    agg["loss_days"] = sum(1 for v in daily.values() if v < 0)
    agg["contribution"] = dict(contrib)
    return agg


def correlation_matrix(trades: list[dict], min_overlap: int = 8):
    """Corrélation des P&L journaliers entre stratégies.

    Convention assumée (affichée dans l'UI) : un jour où une stratégie n'a pas
    tradé alors qu'une autre si compte 0 pour elle. Paires avec moins de
    ``min_overlap`` jours où LES DEUX ont tradé → NaN (« n/a »).

    Retourne (DataFrame corr, DataFrame n_overlap) ou (None, None).
    """
    import pandas as pd  # noqa: PLC0415

    rows = []
    for t in trades:
        d = (t["fill_time"] or "")[:10]
        if d:
            rows.append({"day": d, "strategy": t["strategy"], "pnl": t["pnl_net"]})
    if not rows:
        return None, None
    df = pd.DataFrame(rows)
    pivot = df.pivot_table(index="day", columns="strategy", values="pnl", aggfunc="sum")
    if pivot.shape[1] < 2:
        return None, None
    traded = pivot.notna()
    overlap = traded.T.astype(int) @ traded.astype(int)  # jours communs par paire
    corr = pivot.fillna(0.0).corr()
    corr = corr.mask(overlap < min_overlap)
    return corr, overlap


# ──────────────────────────────────────────────────────────────────────────────
# Verdict sizing — live vs référence backtest, borné par l'incertitude
# ──────────────────────────────────────────────────────────────────────────────

VERDICTS = {
    "CALIBRAGE": {"label": "Calibrage", "color": "grey"},
    "SUR_ESTIMEE": {"label": "Sur-estimée", "color": "red"},
    "SOUS_EXPLOITEE": {"label": "Sous-exploitée", "color": "blue"},
    "CONFORME": {"label": "Conforme", "color": "green"},
    "SOUS_REF": {"label": "Sous la référence", "color": "orange"},
}


def sizing_verdict(agg: dict | None, ref: StrategyRef, n_min: int = 10) -> dict:
    """Compare l'expectancy R live (CI bootstrap 95 %) à la référence OOS.

    Retourne {code, label, color, detail, n, ci, expectancy_r, ref_r,
    n_remaining, wr, wr_ci}.
    """
    n = agg["n"] if agg else 0
    base = {
        "ref_r": ref.expectancy_r,
        "ref_wr": ref.wr_oos,
        "n": n,
        "ci": agg["expectancy_r_ci"] if agg else (0.0, 0.0),
        "expectancy_r": agg["expectancy_r"] if agg else 0.0,
        "wr": agg["wr"] if agg else 0.0,
        "wr_ci": agg["wr_ci"] if agg else (0.0, 1.0),
    }
    n_target = n_for_halfwidth(ref.wr_oos if ref.wr_oos else 0.45)
    base["n_remaining"] = max(0, n_target - n)

    if n < n_min:
        return {
            **base,
            "code": "CALIBRAGE",
            **VERDICTS["CALIBRAGE"],
            "detail": f"n={n} — verdict fiable dans ~{base['n_remaining']} trades",
        }
    lo, hi = base["ci"]
    if hi < 0 and n >= 25:
        return {
            **base,
            "code": "SUR_ESTIMEE",
            **VERDICTS["SUR_ESTIMEE"],
            "detail": "Sous-performe significativement (CI < 0) — réduire ou couper",
        }
    if lo > ref.expectancy_r:
        return {
            **base,
            "code": "SOUS_EXPLOITEE",
            **VERDICTS["SOUS_EXPLOITEE"],
            "detail": "Sur-performe la référence — marge pour monter le risk",
        }
    if lo <= ref.expectancy_r <= hi:
        return {
            **base,
            "code": "CONFORME",
            **VERDICTS["CONFORME"],
            "detail": f"Conforme à la référence (CI encore large : ±{(hi - lo) / 2:.2f} R)",
        }
    # Reste : CI entièrement sous la ref (hi < ref_r)
    if hi < 0:
        detail = f"CI négatif mais n={n} < 25 — surveiller de près"
    else:
        detail = "Sous la référence, pas encore significatif — surveiller"
    return {**base, "code": "SOUS_REF", **VERDICTS["SOUS_REF"], "detail": detail}


def all_verdicts(strat_aggs: dict[str, dict]) -> dict[str, dict]:
    return {key: sizing_verdict(strat_aggs.get(key), ref) for key, ref in STRATEGY_REFS.items()}


# ──────────────────────────────────────────────────────────────────────────────
# Assemblage (convenience pour les tabs + CLI)
# ──────────────────────────────────────────────────────────────────────────────


def compute_all(acc: AccountConfig, with_broker: bool = True) -> dict:
    """Snapshot stats complet pour un compte. Broker en best-effort."""
    state = read_state(acc)
    pairs = None
    broker_ok = False
    if with_broker:
        try:
            from tools.dashboard_v4.broker import get_broker  # noqa: PLC0415

            pairs = get_broker(acc).paired_trades(days_back=60)
            broker_ok = bool(pairs)
        except Exception:
            pairs = None
    trades = unified_trades(state, pairs)
    strat_aggs = strategy_aggregates(trades)
    return {
        "state": state,
        "trades": trades,
        "broker_matched": broker_ok,
        "strat_aggs": strat_aggs,
        "portfolio": portfolio_aggregates(trades),
        "verdicts": all_verdicts(strat_aggs),
    }


def _fmt_pf(pf: float) -> str:
    return "∞" if pf == float("inf") else f"{pf:.2f}"


if __name__ == "__main__":
    from tools.dashboard_v4.accounts import ACCOUNTS, DEFAULT_ACCOUNT

    acc = ACCOUNTS[DEFAULT_ACCOUNT]
    snap = compute_all(acc)
    pf = snap["portfolio"]
    print(f"═══ STATS v4 — compte {acc.label} ═══")
    print(
        f"Trades unifiés : {pf['n']}  (matchés broker : "
        f"{sum(1 for t in snap['trades'] if t['source'] == 'broker')})"
    )
    print(
        f"P&L net : ${pf['pnl_net']:+,.2f}  (fees ${pf['fees']:,.2f})  "
        f"PF {_fmt_pf(pf['pf'])}  WR {pf['wr'] * 100:.0f}%"
    )
    print(
        f"Expectancy : ${pf['expectancy_usd']:+,.2f}/trade · "
        f"{pf['expectancy_r']:+.3f} R  CI95 [{pf['expectancy_r_ci'][0]:+.3f}, "
        f"{pf['expectancy_r_ci'][1]:+.3f}]"
    )
    print(f"Jours : {pf['win_days']} verts / {pf['loss_days']} rouges")
    print("\n── Par stratégie ──")
    for key, agg in sorted(snap["strat_aggs"].items()):
        v = snap["verdicts"].get(key, {})
        wr_lo, wr_hi = agg["wr_ci"]
        print(
            f"{key:9s} n={agg['n']:3d}  P&L ${agg['pnl_net']:+8,.0f}  "
            f"PF {_fmt_pf(agg['pf']):>5s}  WR {agg['wr'] * 100:3.0f}% "
            f"[{wr_lo * 100:.0f}–{wr_hi * 100:.0f}]  "
            f"E[R] {agg['expectancy_r']:+.2f} "
            f"[{agg['expectancy_r_ci'][0]:+.2f},{agg['expectancy_r_ci'][1]:+.2f}]  "
            f"→ {v.get('label', '?')} ({v.get('detail', '')})"
        )
    corr, overlap = correlation_matrix(snap["trades"])
    if corr is not None:
        print("\n── Corrélation P&L journaliers ──")
        print(corr.round(2).to_string())
