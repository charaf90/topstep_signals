"""Lecture state + helpers autoritatifs v4 — paramétré par AccountConfig.

Module pur Python (zéro dépendance Dash), réutilisable en CLI/tests.
Reprend les parsers éprouvés du v3 (read_state, chronological_trades,
risk_margins, portfolio_status, normalisation stratégie) en les rendant
multi-comptes et résilients à un JSON en cours d'écriture.
"""

from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.dashboard_v4.accounts import AccountConfig  # noqa: E402

STRATEGY_KEYS = ("OPR", "FIB", "FIB_FINE", "BOS_FVG")


def today_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def read_state(acc: AccountConfig) -> dict | None:
    """Lecture seule du state. Retry unique après 150 ms si JSON mi-écrit
    (le daemon réécrit le fichier en place) ; None si toujours illisible."""
    for attempt in (0, 1):
        try:
            return json.loads(acc.state_path.read_text())
        except FileNotFoundError:
            return None
        except json.JSONDecodeError:
            if attempt == 0:
                time.sleep(0.15)
    return None


def authoritative_pnl(state: dict) -> dict:
    """risk_state du PortfolioRiskManager — realized_day_pnl neutralisé si le
    daemon n'a pas rollé sur le jour courant."""
    rs = state.get("risk_state", {}) or {}
    cum = float(rs.get("cum_pnl", 0.0))
    peak = float(rs.get("peak_pnl", 0.0))
    rs_day = rs.get("current_day")
    is_today = rs_day == today_utc()
    return {
        "cum_pnl": cum,
        "peak_pnl": peak,
        "realized_day_pnl": float(rs.get("realized_day_pnl", 0.0)) if is_today else 0.0,
        "stale_day": rs_day if not is_today else None,
        "active_drawdown": max(0.0, peak - cum),
        "consec_loss_days": int(rs.get("consec_loss_days", 0)),
        "daily_fills_count": int(rs.get("daily_fills_count", 0)) if is_today else 0,
    }


# ── normalisation stratégie ───────────────────────────────────────────────────


def normalize_strategy(s: str) -> str:
    """⚠️ FIB_FINE AVANT FIB (« FIB_FINE » commence par « FIB » — bug v3
    constaté 2026-06-11)."""
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


def infer_strategy_from_tag(tag_name: str) -> str:
    """OPR_NQ1_… → OPR · FIBFINE_… → FIB_FINE · BOSFVG_… → BOS_FVG · FIBV4_… → FIB."""
    if not tag_name:
        return "—"
    norm = normalize_strategy(tag_name.split("_")[0])
    return norm if norm in STRATEGY_KEYS else "—"


# ── trades du state ───────────────────────────────────────────────────────────


def closed_tag_trades(state: dict) -> list[dict]:
    """Trades clos depuis placed_tags, triés par fill_time. P&L BRUT (sans fees
    broker) — l'enrichissement net se fait dans stats.unified_trades."""
    rows = []
    for tag, info in state.get("placed_tags", {}).items():
        if info.get("close_pnl") is None:
            continue
        risk = float(info.get("risk") or 0.0)
        rows.append(
            {
                "tag": tag,
                "strategy": normalize_strategy(info.get("strategy", ""))
                or infer_strategy_from_tag(tag),
                "ticker": info.get("ticker", "?"),
                "dir": info.get("direction", "?"),
                "n_ct": int(info.get("n_ct", 0)),
                "entry": info.get("entry"),
                "fill_time": info.get("fill_time") or info.get("placed_at") or "",
                "close_pnl": float(info["close_pnl"]),
                "risk_usd": risk,
                "order_id": info.get("order_id"),
                "contract_id": info.get("contract_id"),
            }
        )
    return sorted(rows, key=lambda r: r["fill_time"])


def pending_orders(state: dict) -> list[dict]:
    """Ordres armés (risk_state.pending_orders, vision RM avec risk_usd)."""
    out = []
    rs = state.get("risk_state", {}) or {}
    for tag, info in (rs.get("pending_orders") or {}).items():
        meta = info.get("metadata") or {}
        out.append(
            {
                "tag": tag,
                "strategy": normalize_strategy(meta.get("strategy", ""))
                or infer_strategy_from_tag(tag),
                "ticker": meta.get("ticker", "?"),
                "risk_usd": float(info.get("risk_usd") or 0.0),
                "opened_at": info.get("opened_at", ""),
            }
        )
    return out


def active_positions(state: dict) -> list[dict]:
    """Positions ouvertes selon le RM (risk_state.active_positions)."""
    out = []
    rs = state.get("risk_state", {}) or {}
    for tag, info in (rs.get("active_positions") or {}).items():
        meta = (info.get("metadata") or {}) if isinstance(info, dict) else {}
        out.append(
            {
                "tag": tag,
                "strategy": normalize_strategy(meta.get("strategy", ""))
                or infer_strategy_from_tag(tag),
                "ticker": meta.get("ticker", "?"),
                "risk_usd": float(info.get("risk_usd") or 0.0) if isinstance(info, dict) else 0.0,
            }
        )
    return out


# ── log (tail léger) ─────────────────────────────────────────────────────────


def tail_log(path: Path, n: int = 30) -> list[str]:
    if not path.exists():
        return []
    try:
        with path.open("rb") as f:
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


# ── attribution stratégie aux trades broker ──────────────────────────────────


def lookup_strategy_for_pair(state: dict, pair: dict) -> str:
    """Stratégie d'un trade broker apparié : order_id exact, puis contract +
    pnl brut proche, puis préfixe de tag. « — » si introuvable."""
    tags = state.get("placed_tags", {})
    order_id_open = pair.get("order_id_open")
    if order_id_open:
        for tag_name, info in tags.items():
            if info.get("order_id") == order_id_open:
                return normalize_strategy(info.get("strategy", "")) or infer_strategy_from_tag(
                    tag_name
                )
    cid = pair.get("contract_id")
    for tag_name, info in tags.items():
        if info.get("contract_id") != cid:
            continue
        cp = info.get("close_pnl")
        if cp is None:
            continue
        # close_pnl state = BRUT → comparé au pnl_gross broker
        if abs(float(cp) - pair.get("pnl_gross", 0)) < 5.0:
            return normalize_strategy(info.get("strategy", "")) or infer_strategy_from_tag(tag_name)
    return "—"


# ── risque & portefeuille (config EN DIRECT, jamais hardcodé) ────────────────


def risk_margins(
    acc: AccountConfig, day_pnl: float, drawdown: float, best_day: float
) -> list[dict]:
    """Marges restantes avant chaque limite Topstep (cf. v3, limites par compte)."""

    def _bar(
        label: str,
        used: float,
        limit: float,
        track_max: float | None = None,
        guardrail: float | None = None,
    ) -> dict:
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

    consistency_real = 0.5 * acc.profit_target
    return [
        _bar("Perte du jour", -day_pnl, acc.user_daily_loss_max, track_max=acc.daily_loss_max),
        _bar("Trailing drawdown", drawdown, acc.trailing_dd),
        _bar(
            "Consistency (best day)",
            best_day,
            consistency_real,
            guardrail=acc.consistency_guardrail,
        ),
    ]


# (state_key, nom affiché, vars config des listes de tickers live, flag,
#  version, sizing) — résolus EN DIRECT depuis config.py à chaque appel.
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
    (
        "IB_RETEST",
        "IB-Retest",
        ("IB_RETEST_TICKERS",),
        "IB_RETEST_ENABLED",
        "IB_RETEST_STRATEGY_VERSION",
        "RISK_PER_TRADE_USD",  # IB_RETEST_RISK_PER_TRADE_USD=None → global $200
    ),
]


def _resolve_universe(config, ticker_vars: tuple[str, ...]) -> str:
    seen: list[str] = []
    for var in ticker_vars:
        for t in getattr(config, var, []) or []:
            if t not in seen:
                seen.append(t)
    return " · ".join(seen) if seen else "—"


def last_fill_by_strategy(state: dict | None) -> dict[str, str]:
    last: dict[str, str] = {}
    if not state:
        return last
    for tag, info in state.get("placed_tags", {}).items():
        ft = info.get("fill_time")
        if not ft:
            continue
        st = normalize_strategy(info.get("strategy", "")) or infer_strategy_from_tag(tag)
        if st not in last or ft > last[st]:
            last[st] = ft
    return last


def portfolio_status(state: dict | None = None, days_active: int = 10) -> list[dict]:
    """Statut par stratégie : OFF (flag off) / LIVE (fill récent) / ARMÉ.

    Caveat : le dashboard ne connaît pas la config réellement chargée par le
    daemon — LIVE est inféré via l'activité observée dans le state.
    """
    import config  # noqa: PLC0415

    last_fill = last_fill_by_strategy(state)
    cutoff = (datetime.now(UTC) - timedelta(days=days_active)).strftime("%Y-%m-%dT%H:%M:%SZ")
    global_risk = getattr(config, "RISK_PER_TRADE_USD", None)

    out: list[dict] = []
    for key, name, ticker_vars, en_var, ver_var, size_var in _PORTFOLIO_SPEC:
        enabled = bool(getattr(config, en_var, False))
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
                "universe": _resolve_universe(config, ticker_vars),
                "version": getattr(config, ver_var, "?"),
                "sizing": getattr(config, size_var, global_risk),
                "enabled": enabled,
                "last_fill": lf,
                "active": active,
                "status": status,
                "color": color,
            }
        )
    return out


def daily_pnl_by_strategy(trades: list[dict]) -> dict[str, dict[str, float]]:
    """{jour: {stratégie: pnl}} depuis les trades clos (jour du fill_time)."""
    out: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for t in trades:
        d = (t["fill_time"] or "")[:10]
        if d:
            out[d][t["strategy"]] += t.get("pnl_net", t["close_pnl"])
    return {d: dict(v) for d, v in out.items()}


def load_replay() -> dict | None:
    """output/portfolio_replay/replay.json si présent (carte Monte-Carlo)."""
    p = ROOT / "output" / "portfolio_replay" / "replay.json"
    try:
        data = json.loads(p.read_text())
        data["_mtime"] = datetime.fromtimestamp(p.stat().st_mtime, UTC).strftime("%Y-%m-%d")
        return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def state_meta(state: dict | None, acc: AccountConfig) -> dict[str, Any]:
    if not state:
        return {"account_id": "—", "date": "—", "opening_balance": None}
    return {
        "account_id": state.get("account_id", "—"),
        "date": state.get("date", "—"),
        "opening_balance": state.get("opening_balance"),
    }
