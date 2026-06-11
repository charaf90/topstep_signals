"""Santé système v4 — daemon, flux d'événements, divergence local/broker.

Lecture seule absolue : PID via /proc (jamais de subprocess vers
restart_daemon.sh — zéro effet de bord possible), fraîcheur par mtime.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.dashboard_v4.accounts import AccountConfig  # noqa: E402


def daemon_status(acc: AccountConfig) -> dict:
    """Le PROCESS fait foi (verrou PID), pas la session tmux.

    Retourne {alive, pid, detail}.
    """
    pid_path = acc.daemon_pid
    try:
        pid = int(pid_path.read_text().strip())
    except (FileNotFoundError, ValueError, OSError):
        return {"alive": False, "pid": None, "detail": "pas de fichier PID"}
    cmdline_path = Path(f"/proc/{pid}/cmdline")
    try:
        cmdline = cmdline_path.read_bytes().replace(b"\x00", b" ").decode(errors="replace")
    except (FileNotFoundError, OSError):
        return {"alive": False, "pid": pid, "detail": f"PID {pid} absent de /proc"}
    if "live.py" not in cmdline:
        return {"alive": False, "pid": pid, "detail": f"PID {pid} ≠ live.py ({cmdline[:40]}…)"}
    return {"alive": True, "pid": pid, "detail": f"live.py (pid {pid})"}


def daemon_log_freshness(acc: AccountConfig) -> dict:
    """Âge du dernier log daemon (mtime du plus récent logs/daemon_*.log)."""
    logs = sorted(ROOT.glob(acc.daemon_log_glob))
    if not logs:
        return {"path": None, "age_s": None}
    latest = logs[-1]
    try:
        age = (
            datetime.now(UTC) - datetime.fromtimestamp(latest.stat().st_mtime, UTC)
        ).total_seconds()
    except OSError:
        return {"path": latest.name, "age_s": None}
    return {"path": latest.name, "age_s": age}


def tail_daemon_log(acc: AccountConfig, n: int = 10) -> list[str]:
    from tools.dashboard_v4.datasource import tail_log  # noqa: PLC0415

    logs = sorted(ROOT.glob(acc.daemon_log_glob))
    if not logs:
        return []
    return tail_log(logs[-1], n)


def divergence(
    state: dict | None, broker_summary: dict | None, day_local: float, day_broker: float | None
) -> dict:
    """Écart compta locale (risk_state, P&L brut sans fees) vs vérité broker.

    L'écart cumulé ≈ fees cumulés + dérive (fills non attribués, trades
    manuels) — cf. mémoire projet « désync compta P&L live ».
    """
    out: dict = {"available": False}
    if not state or not broker_summary:
        return out
    rs = state.get("risk_state", {}) or {}
    cum_local = float(rs.get("cum_pnl", 0.0))
    cum_broker = float(broker_summary["cum_pnl_net"])
    out.update(
        {
            "available": True,
            "cum_local": cum_local,
            "cum_broker": cum_broker,
            "delta_cum": cum_local - cum_broker,
            "day_local": day_local,
            "day_broker": day_broker,
            "delta_day": (day_local - day_broker) if day_broker is not None else None,
            "ok": abs(cum_local - cum_broker) < 50,
        }
    )
    return out
