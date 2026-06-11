"""Data helpers — lecture state + métriques calculées (autoritatifs).

Module pur Python, sans dépendance Dash, réutilisable en CLI/tests.
Source de vérité : state/live_state.json (risk_state pour cum_pnl).
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
STATE_PATH = ROOT / "state" / "live_state.json"
SHADOW_STATE_PATH = ROOT / "state" / "shadow_state.json"
LOG_PATH = ROOT / "logs" / "trading_events.log"


def today_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def read_state(path: Path = STATE_PATH) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def authoritative_pnl(state: dict) -> dict:
    """SOURCE DE VÉRITÉ — utilise risk_state du PortfolioRiskManager.

    Neutralise realized_day_pnl si current_day != today (daemon non rollé).
    """
    rs = state.get("risk_state", {}) or {}
    cum = float(rs.get("cum_pnl", 0.0))
    peak = float(rs.get("peak_pnl", 0.0))
    rdp_raw = float(rs.get("realized_day_pnl", 0.0))
    fills_raw = int(rs.get("daily_fills_count", 0))
    rs_day = rs.get("current_day")
    is_today = rs_day == today_utc()
    return {
        "cum_pnl": cum,
        "peak_pnl": peak,
        "realized_day_pnl": rdp_raw if is_today else 0.0,
        "stale_day": rs_day if not is_today else None,
        "active_drawdown": max(0.0, peak - cum),
        "consec_loss_days": int(rs.get("consec_loss_days", 0)),
        "daily_fills_count": fills_raw if is_today else 0,
    }


def chronological_trades(state: dict) -> list[dict]:
    """Trades clos triés par fill_time (clé toujours présente)."""
    rows = []
    for tag, info in state.get("placed_tags", {}).items():
        if info.get("close_pnl") is None:
            continue
        sort_key = info.get("fill_time") or info.get("placed_at") or ""
        rows.append(
            {
                "tag": tag,
                "strategy": info.get("strategy", "?"),
                "ticker": info.get("ticker", "?"),
                "dir": info.get("direction", "?"),
                "n_ct": int(info.get("n_ct", 0)),
                "fill_time": info.get("fill_time", ""),
                "entry": info.get("entry"),
                "exit": info.get("exit"),
                "close_pnl": float(info["close_pnl"]),
                "_sort_key": sort_key,
            }
        )
    return sorted(rows, key=lambda r: r["_sort_key"])


def equity_series(trades: list[dict]) -> tuple[list[str], list[float]]:
    """Cumul des close_pnl par fill_time ISO complet."""
    if not trades:
        return [], []
    timestamps, equity = [], []
    cum = 0.0
    for t in trades:
        cum += t["close_pnl"]
        ts = t["fill_time"] or t.get("_sort_key") or ""
        timestamps.append(ts)
        equity.append(round(cum, 2))
    return timestamps, equity


def strategy_stats(trades: list[dict]) -> dict[str, dict]:
    by_strat: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "pnl": 0.0, "wins": 0, "losses": 0, "gross_win": 0.0, "gross_loss": 0.0}
    )
    for t in trades:
        s = t["strategy"]
        by_strat[s]["n"] += 1
        by_strat[s]["pnl"] += t["close_pnl"]
        if t["close_pnl"] > 0:
            by_strat[s]["wins"] += 1
            by_strat[s]["gross_win"] += t["close_pnl"]
        elif t["close_pnl"] < 0:
            by_strat[s]["losses"] += 1
            by_strat[s]["gross_loss"] += abs(t["close_pnl"])
    for d in by_strat.values():
        d["wr_pct"] = (d["wins"] / d["n"] * 100) if d["n"] else 0.0
        d["pf"] = (d["gross_win"] / d["gross_loss"]) if d["gross_loss"] > 0 else float("inf")
        d["avg_pnl"] = (d["pnl"] / d["n"]) if d["n"] else 0.0
    return dict(by_strat)


def daily_pnl(trades: list[dict]) -> dict[str, float]:
    """Somme close_pnl par jour (date string YYYY-MM-DD)."""
    out: dict[str, float] = defaultdict(float)
    for t in trades:
        d = (t["fill_time"] or "")[:10]
        if d:
            out[d] += t["close_pnl"]
    return dict(out)


def temporal_comparisons(trades: list[dict], pnl: dict) -> dict:
    """Calcule les comparaisons temporelles : streak, vs hier, vs moyenne 7j."""
    daily = daily_pnl(trades)
    sorted_days = sorted(daily.keys())

    today = today_utc()
    yesterday = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")

    today_pnl = pnl["realized_day_pnl"]  # = 0 si stale
    yesterday_pnl = daily.get(yesterday, 0.0)

    # Streak : compte de jours consécutifs gagnants à partir du dernier jour avec activité
    streak_win = 0
    streak_loss = 0
    for d in reversed(sorted_days):
        if daily[d] > 0:
            if streak_loss > 0:
                break
            streak_win += 1
        elif daily[d] < 0:
            if streak_win > 0:
                break
            streak_loss += 1

    # Moyenne 7 derniers jours avec activité
    last_7_pnls = [daily[d] for d in sorted_days[-7:]]
    avg_7d = sum(last_7_pnls) / len(last_7_pnls) if last_7_pnls else 0.0

    # Best / worst day
    best_day = max(daily.items(), key=lambda kv: kv[1]) if daily else (None, 0.0)
    worst_day = min(daily.items(), key=lambda kv: kv[1]) if daily else (None, 0.0)

    return {
        "today_pnl": today_pnl,
        "yesterday_pnl": yesterday_pnl,
        "delta_yesterday": today_pnl - yesterday_pnl,
        "avg_7d": avg_7d,
        "delta_avg_7d": today_pnl - avg_7d,
        "streak_win_days": streak_win,
        "streak_loss_days": streak_loss,
        "best_day": best_day,
        "worst_day": worst_day,
        "n_days_active": len(daily),
    }


def tail_log(n: int = 30) -> list[str]:
    if not LOG_PATH.exists():
        return []
    try:
        with LOG_PATH.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            block = 8192
            data = b""
            while size > 0 and data.count(b"\n") <= n:
                read_size = min(block, size)
                size -= read_size
                f.seek(size)
                data = f.read(read_size) + data
            return data.decode("utf-8", errors="replace").splitlines()[-n:]
    except OSError:
        return []


def last_event_age_seconds(lines: list[str]) -> float | None:
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            dt = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
            return (datetime.now(UTC) - dt).total_seconds()
        except ValueError:
            continue
    return None


def format_age(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds / 60)}min"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}j"


def open_positions(state: dict) -> list[dict[str, Any]]:
    out = []
    for tag, info in state.get("placed_tags", {}).items():
        if info.get("status") == "FILLED" and info.get("close_pnl") is None:
            out.append({"tag": tag, **info})
    return out


def open_orders(state: dict) -> list[dict[str, Any]]:
    out = []
    for tag, info in state.get("placed_tags", {}).items():
        if info.get("status") in {"PLACED", "PENDING", "WORKING", "ARMED"}:
            out.append({"tag": tag, **info})
    return out


def lookup_strategy_for_pair(state: dict, pair: dict) -> str:
    """Cherche la stratégie d'un trade clos broker en parcourant state.placed_tags.

    Stratégie : 3 niveaux de matching, du plus précis au moins précis.

    1. Match par order_id_open : tag.order_id == pair.order_id_open
    2. Match par contract_id + close_pnl approchant (à $1 près) + status FILLED
    3. Inférence depuis le nom du tag (préfixe OPR / FIB / FIBV4 → OPR / FIB)

    Retourne "OPR", "FIB" ou "—" si non trouvé.
    """
    tags = state.get("placed_tags", {})
    cid = pair.get("contract_id")
    pnl_net = pair.get("pnl_net", 0)
    order_id_open = pair.get("order_id_open")

    # Niveau 1 : match exact par order_id
    if order_id_open:
        for tag_name, info in tags.items():
            if info.get("order_id") == order_id_open:
                strat = info.get("strategy", "")
                return _normalize_strategy(strat) or _infer_strategy_from_tag(tag_name)

    # Niveau 2 : match par contract + pnl proche
    for tag_name, info in tags.items():
        if info.get("status") != "FILLED":
            continue
        if info.get("contract_id") != cid:
            continue
        cp = info.get("close_pnl")
        if cp is None:
            continue
        # close_pnl du state est BRUT (sans frais broker)
        # alors que pair.pnl_net inclut les frais (négatifs)
        # → on compare cp avec pair.pnl_gross
        if abs(float(cp) - pair.get("pnl_gross", 0)) < 5.0:
            strat = info.get("strategy", "")
            return _normalize_strategy(strat) or _infer_strategy_from_tag(tag_name)

    return "—"


def _normalize_strategy(s: str) -> str:
    """Normalise une stratégie vers les clés du portefeuille (OPR, FIB,
    FIB_FINE, BOS_FVG). ⚠️ Tester FIB_FINE AVANT FIB : « FIB_FINE » commence
    par « FIB » — bug constaté 2026-06-11 (stats fib-fine fusionnées dans Fib,
    BOS_FVG classé « — »)."""
    if not s:
        return ""
    s_up = s.upper().replace("-", "_")
    if s_up.startswith("FIB_FINE") or s_up.startswith("FIBFINE"):
        return "FIB_FINE"
    if s_up.startswith("BOS"):
        return "BOS_FVG"
    if s_up.startswith("FIB"):
        return "FIB"
    if s_up.startswith("OPR"):
        return "OPR"
    return s_up


def _infer_strategy_from_tag(tag_name: str) -> str:
    """Devine la stratégie depuis le préfixe d'un tag : OPR_NQ1_… → OPR,
    FIBFINE_MES1_… → FIB_FINE, BOSFVG_MES1_… → BOS_FVG, FIBV4_… → FIB."""
    if not tag_name:
        return "—"
    first = tag_name.split("_")[0].upper()
    if first.startswith("FIBFINE"):
        return "FIB_FINE"
    if first.startswith("BOS"):
        return "BOS_FVG"
    if first.startswith("FIB"):
        return "FIB"
    if first.startswith("OPR"):
        return "OPR"
    return "—"


# ──────────────────────────────────────────────────────────────────────────────
# RISQUE & PORTEFEUILLE — adaptation projet (cockpit challenge Topstep)
# ──────────────────────────────────────────────────────────────────────────────


def risk_margins(day_pnl: float, drawdown: float, best_day: float) -> list[dict]:
    """Marges restantes avant chaque limite Topstep (barres de marge).

    Reçoit des scalaires DÉJÀ résolus broker-first par l'appelant (cohérence
    avec le hero net) :

    - ``day_pnl``  : P&L net du jour (négatif = perte)
    - ``drawdown`` : ``peak - cum`` net courant (>= 0)
    - ``best_day`` : meilleur jour net (consistency)

    Chaque entrée décrit une barre :

    - ``track_max`` : dénominateur de la barre (mur dur quand il existe)
    - ``soft_limit`` : limite réellement appliquée par le bot (→ marge + couleur)
    - ``headroom`` : ``soft_limit - used`` (le « reste $X », info dominante)
    - ``fill_ratio`` : ``used / track_max`` (largeur visuelle)
    - ``soft_ratio`` : position du marqueur soft sur la piste (None si == 1)
    - ``color_ratio`` : ``used / soft_limit`` (escalade vert→orange→rouge)
    """
    from config import (  # noqa: PLC0415
        CHALLENGE_CONSISTENCY_BEST_DAY_MAX_USD,
        TOPSTEP_DAILY_LOSS_MAX,
        TOPSTEP_PROFIT_TARGET,
        TOPSTEP_TRAILING_DD,
        USER_DAILY_LOSS_MAX,
    )

    def _bar(
        label: str,
        used: float,
        limit: float,
        track_max: float | None = None,
        guardrail: float | None = None,
    ) -> dict:
        """`limit` = ligne de référence Topstep (dénominateur, marge « reste $X »,
        couleur, état DÉPASSÉ). `track_max` = fin de piste (mur dur si > limit).
        `guardrail` = marqueur optionnel (garde-fou bot, plus serré). À défaut, on
        marque `limit` lui-même quand il est sous le mur (cas perte du jour)."""
        used = max(0.0, float(used))
        track_max = float(track_max or limit)
        mark_val = guardrail if guardrail is not None else (limit if limit < track_max else None)
        soft_ratio = (
            (mark_val / track_max)
            if (mark_val and track_max > 0 and mark_val < track_max)
            else None
        )
        return {
            "label": label,
            "used": used,
            "soft_limit": float(limit),
            "track_max": track_max,
            "headroom": max(0.0, limit - used),
            "fill_ratio": min(1.0, used / track_max) if track_max > 0 else 0.0,
            "soft_ratio": soft_ratio,
            "color_ratio": (used / limit) if limit > 0 else 0.0,
            "over": used > limit,
        }

    # Vraie limite consistency Topstep = 50% du profit target ($1500 pour $3000).
    # Le bot vise plus serré (CHALLENGE_CONSISTENCY_BEST_DAY_MAX_USD, $1400) = garde-fou.
    consistency_real = 0.5 * TOPSTEP_PROFIT_TARGET
    return [
        # Piste jusqu'au mur dur Topstep ($1000), seuil bot ($950) marqué dessus.
        _bar("Perte du jour", -day_pnl, USER_DAILY_LOSS_MAX, track_max=TOPSTEP_DAILY_LOSS_MAX),
        _bar("Trailing drawdown", drawdown, TOPSTEP_TRAILING_DD),
        _bar(
            "Consistency (best day)",
            best_day,
            consistency_real,
            guardrail=CHALLENGE_CONSISTENCY_BEST_DAY_MAX_USD,
        ),
    ]


# (state_key, nom affiché, vars config des LISTES de tickers live, var flag,
#  var version, var sizing). L'univers est résolu EN DIRECT depuis config.py à
# chaque appel — un hardcode ici avait affiché « OPR : NQ1 · YM1 · MES1 »
# pendant des jours alors que la prod ne routait plus que YM1 (constat 2026-06-11).
_PORTFOLIO_SPEC: list[tuple[str, str, tuple[str, ...], str, str, str]] = [
    (
        "OPR",
        "OPR",
        ("OPR_V5_1_LIVE_TICKERS", "OPR_V4_LIVE_TICKERS"),
        "OPR_ENABLED",
        "OPR_V5_1_STRATEGY_VERSION",
        "RISK_PER_TRADE_USD",
    ),
    (
        "FIB",
        "Fib",
        ("FIB_V4_TICKERS",),
        "FIB_V4_ENABLED",
        "FIB_V4_STRATEGY_VERSION",
        "RISK_PER_TRADE_USD",
    ),
    (
        "FIB_FINE",
        "Fib Fine",
        ("FIB_FINE_LIVE_TICKERS",),
        "FIB_FINE_ENABLED",
        "FIB_FINE_STRATEGY_VERSION",
        "FIB_FINE_RISK_USD",
    ),
    (
        "BOS_FVG",
        "BOS-FVG",
        ("BOS_FVG_LIVE_TICKERS",),
        "BOS_FVG_ENABLED",
        "BOS_FVG_STRATEGY_VERSION",
        "BOS_FVG_RISK_USD",
    ),
]


def _resolve_universe(config, ticker_vars: tuple[str, ...]) -> str:
    """Univers affiché = union ordonnée (dédupliquée) des listes config live."""
    seen: list[str] = []
    for var in ticker_vars:
        for t in getattr(config, var, []) or []:
            if t not in seen:
                seen.append(t)
    return " · ".join(seen) if seen else "—"


def last_fill_by_strategy(state: dict | None) -> dict[str, str]:
    """Dernier fill_time (ISO) par stratégie, lu depuis state.placed_tags."""
    last: dict[str, str] = {}
    if not state:
        return last
    for info in state.get("placed_tags", {}).values():
        ft = info.get("fill_time")
        if not ft:
            continue
        st = info.get("strategy", "?")
        if st not in last or ft > last[st]:
            last[st] = ft
    return last


def portfolio_status(state: dict | None = None, days_active: int = 10) -> list[dict]:
    """Statut du portefeuille — lit les flags config.py EN DIRECT (jamais hardcodé).

    Statut par stratégie :
    - ``OFF``  (gris) : flag désactivé dans config.py
    - ``LIVE`` (vert) : flag activé ET fill vu dans les ``days_active`` derniers jours
    - ``ARMÉ`` (bleu) : flag activé mais aucune activité récente (ex : promu mais
      inerte jusqu'au restart du daemon, ou simplement pas de signal récemment)

    Caveat : le dashboard ne connaît pas la config réellement chargée par le
    daemon — ``LIVE`` est inféré via l'activité observée dans le state.
    """
    import config  # noqa: PLC0415

    last_fill = last_fill_by_strategy(state)
    cutoff = (datetime.now(UTC) - timedelta(days=days_active)).strftime("%Y-%m-%dT%H:%M:%SZ")
    global_risk = getattr(config, "RISK_PER_TRADE_USD", None)

    out: list[dict] = []
    for key, name, ticker_vars, en_var, ver_var, size_var in _PORTFOLIO_SPEC:
        universe = _resolve_universe(config, ticker_vars)
        enabled = bool(getattr(config, en_var, False))
        version = getattr(config, ver_var, "?")
        sizing = getattr(config, size_var, global_risk)
        lf = last_fill.get(key)
        active = bool(lf and lf >= cutoff)
        if not enabled:
            status, color = "OFF", "grey"
        elif active:
            status, color = "LIVE", "green"
        else:
            status, color = "ARMÉ", "blue"
        out.append(
            {
                "key": key,
                "name": name,
                "universe": universe,
                "version": version,
                "sizing": sizing,
                "enabled": enabled,
                "last_fill": lf,
                "active": active,
                "status": status,
                "color": color,
            }
        )
    return out
