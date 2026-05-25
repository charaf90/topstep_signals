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
