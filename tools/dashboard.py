"""Dashboard Streamlit mobile-first — lecture seule de l'état live.

Affiche en temps quasi-réel :
- Équité jour + cumulée
- Distance aux limites Topstep (DLL, trailing DD, consistency 50%)
- Positions et ordres ouverts
- Derniers fills / clôtures
- Latence WS (dernier événement)

Architecture :
- Lit `state/live_state.json` et `logs/trading_events.log` en lecture seule.
- Auto-refresh 30s via `st_autorefresh` natif (1.0+).
- Aucune écriture, aucun appel API broker — pas de risque d'interférence avec
  le daemon live.

Lancement (cf. tools/launch_dashboard.sh) :
    streamlit run tools/dashboard.py \\
        --server.port 8501 --server.address 0.0.0.0 --server.headless true

Accès iPhone via Tailscale :
    http://topstep-pc:8501  (hostname Tailscale)
    http://100.X.X.X:8501   (IP Tailscale)
"""

from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import streamlit as st

# Permet l'import depuis la racine si lancé depuis tools/
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import (  # noqa: E402
    CHALLENGE_CONSISTENCY_BEST_DAY_MAX_USD,
    TOPSTEP_DAILY_LOSS_MAX,
    TOPSTEP_PROFIT_TARGET,
    TOPSTEP_TRAILING_DD,
    USER_DAILY_LOSS_MAX,
)

# ──────────────────────────────────────────────────────────────────────────────
# Config page
# ──────────────────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Topstep Live",
    page_icon="📊",
    layout="centered",
    initial_sidebar_state="collapsed",
)

STATE_PATH = ROOT / "state" / "live_state.json"
LOG_PATH = ROOT / "logs" / "trading_events.log"
REFRESH_INTERVAL_S = 30


# ──────────────────────────────────────────────────────────────────────────────
# Lecture seule des fichiers
# ──────────────────────────────────────────────────────────────────────────────


def _read_state() -> dict | None:
    """Lit le live_state.json — None si absent ou illisible."""
    try:
        return json.loads(STATE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _tail_log(n: int = 30) -> list[str]:
    """Lit les N dernières lignes du log (best-effort)."""
    if not LOG_PATH.exists():
        return []
    try:
        # Lecture binaire du tail pour éviter de tout charger en mémoire
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
            lines = data.decode("utf-8", errors="replace").splitlines()
            return lines[-n:]
    except OSError:
        return []


def _open_positions(state: dict) -> list[dict]:
    """Tags FILLED non clos."""
    out = []
    for tag, info in state.get("placed_tags", {}).items():
        if info.get("status") == "FILLED" and info.get("close_pnl") is None:
            out.append({"tag": tag, **info})
    return out


def _open_orders(state: dict) -> list[dict]:
    """Tags avec status PLACED/PENDING/WORKING/ARMED."""
    out = []
    open_statuses = {"PLACED", "PENDING", "WORKING", "ARMED"}
    for tag, info in state.get("placed_tags", {}).items():
        if info.get("status") in open_statuses:
            out.append({"tag": tag, **info})
    return out


def _today_str() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _sum_close_pnl(state: dict, date_str: str | None = None) -> float:
    """Somme close_pnl. Si date_str fourni, filtre sur ce jour."""
    total = 0.0
    for info in state.get("placed_tags", {}).values():
        if info.get("close_pnl") is None:
            continue
        if date_str is not None:
            ft = info.get("fill_time") or info.get("placed_at") or ""
            if not ft.startswith(date_str):
                continue
        total += float(info["close_pnl"])
    return round(total, 2)


def _last_event_age_s() -> tuple[float | None, str | None]:
    """Renvoie (age_secondes, dernière_ligne) du dernier événement loggé."""
    lines = _tail_log(n=5)
    for line in reversed(lines):
        if not line.strip():
            continue
        # Format : "YYYY-MM-DD HH:MM:SS UTC  [TYPE]  ..."
        try:
            ts_str = line[:19]
            dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
            age = (datetime.now(UTC) - dt).total_seconds()
            return age, line
        except ValueError:
            continue
    return None, None


# ──────────────────────────────────────────────────────────────────────────────
# Rendu
# ──────────────────────────────────────────────────────────────────────────────


def _format_age(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds / 60)}min"
    return f"{seconds / 3600:.1f}h"


def _color_for_pnl(pnl: float) -> str:
    if pnl > 0:
        return "#3ddc84"
    if pnl < 0:
        return "#ff6b6b"
    return "#aaaaaa"


def _color_for_distance(distance: float, threshold: float = 200.0) -> str:
    """Vert si > 500, jaune si entre threshold et 500, rouge si < threshold."""
    if distance < threshold:
        return "#ff6b6b"
    if distance < 500:
        return "#ffb347"
    return "#3ddc84"


def render_header(state: dict | None):
    st.markdown("# 📊 Topstep Live")
    if state is None:
        st.warning("⚠️ `state/live_state.json` introuvable ou illisible.")
        return
    cols = st.columns(2)
    with cols[0]:
        st.caption("Compte")
        st.write(f"`{state.get('account_id', '?')}`")
    with cols[1]:
        st.caption("State daté")
        st.write(f"`{state.get('date', '?')}`")


def render_pnl_block(state: dict):
    today = _today_str()
    pnl_today = _sum_close_pnl(state, date_str=today)
    pnl_total = _sum_close_pnl(state)

    cols = st.columns(2)
    with cols[0]:
        st.metric("P&L Jour", f"${pnl_today:+,.0f}", help="Somme close_pnl du jour")
    with cols[1]:
        st.metric("P&L Cumulé", f"${pnl_total:+,.0f}", help="Somme close_pnl tous tags")


def render_topstep_limits(state: dict):
    """Distance aux limites Topstep — basé sur le state local uniquement."""
    today = _today_str()
    rdp = _sum_close_pnl(state, date_str=today)  # realized day pnl
    cum = _sum_close_pnl(state)  # cum pnl

    # Distances en USD (positif = marge restante avant breach)
    dist_user_daily = USER_DAILY_LOSS_MAX + rdp
    dist_topstep_daily = TOPSTEP_DAILY_LOSS_MAX + rdp
    # Trailing : on n'a pas peak_pnl exact dans le state, donc approx avec cum.
    # peak_pnl >= cum_pnl par construction, donc dist_trail = cum - (peak - 2000)
    # ≤ cum - (cum - 2000) = 2000. On affiche la borne supérieure conservatrice.
    dist_trail_min = max(0.0, TOPSTEP_TRAILING_DD - max(0.0, -cum))
    dist_consistency = CHALLENGE_CONSISTENCY_BEST_DAY_MAX_USD - rdp
    dist_target = TOPSTEP_PROFIT_TARGET - cum

    st.markdown("### Limites Topstep")
    rows = [
        ("DLL user $950", dist_user_daily, "Realized day pnl + 950"),
        ("DLL Topstep $1000", dist_topstep_daily, "Realized day pnl + 1000"),
        ("Trailing DD $2000", dist_trail_min, "Borne basse — vrai trailing nécessite peak_pnl"),
        ("Consistency $1400", dist_consistency, "Best day cap (règle 50% Topstep)"),
        ("Profit target", dist_target, "Marge restante avant target $3000"),
    ]
    for label, distance, help_text in rows:
        color = _color_for_distance(distance)
        st.markdown(
            f"<div style='display:flex;justify-content:space-between;"
            f"padding:4px 8px;border-radius:6px;background:{color}22;'>"
            f"<span>{label}</span>"
            f"<span style='color:{color};font-weight:bold'>${distance:+,.0f}</span>"
            f"</div>",
            unsafe_allow_html=True,
            help=help_text,
        )


def render_positions(state: dict):
    pos = _open_positions(state)
    st.markdown(f"### Positions ouvertes ({len(pos)})")
    if not pos:
        st.caption("Aucune position ouverte.")
        return
    for p in pos:
        st.markdown(
            f"**{p['ticker']}** {p['direction']} ×{p['n_ct']}  \n"
            f"@ {p.get('entry', '?')}  SL {p.get('sl', '?')}  TP {p.get('tp', '?')}  \n"
            f"`{p['tag']}`"
        )


def render_orders(state: dict):
    orders = _open_orders(state)
    st.markdown(f"### Ordres ouverts ({len(orders)})")
    if not orders:
        st.caption("Aucun ordre ouvert.")
        return
    for o in orders:
        st.markdown(
            f"**{o['ticker']}** {o['direction']} ×{o['n_ct']}  \n"
            f"order_id `{o.get('order_id', '?')}`  status `{o['status']}`"
        )


def render_recent_events():
    st.markdown("### Derniers événements")
    lines = _tail_log(n=10)
    if not lines:
        st.caption("`logs/trading_events.log` vide ou absent.")
        return
    for line in reversed([line for line in lines if line.strip()]):
        st.code(line, language=None)


def render_health(state: dict | None):
    age, last_line = _last_event_age_s()
    cols = st.columns(2)
    with cols[0]:
        st.caption("Dernier événement")
        st.write(f"`{_format_age(age)}`")
    with cols[1]:
        st.caption("Heure actuelle (UTC)")
        st.write(f"`{datetime.now(UTC).strftime('%H:%M:%S')}`")
    if age is not None and age > 600:
        st.warning(
            f"⚠️ Dernier événement il y a {_format_age(age)} — WS potentiellement déconnectée."
        )


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────


def main():
    state = _read_state()
    render_header(state)
    if state is None:
        return

    render_pnl_block(state)
    st.divider()
    render_topstep_limits(state)
    st.divider()
    render_positions(state)
    st.divider()
    render_orders(state)
    st.divider()
    render_health(state)
    st.divider()
    render_recent_events()

    # Auto-refresh via st.rerun() + sleep — léger et sans dépendance externe.
    # Si l'utilisateur ferme l'onglet, le sleep ne consomme rien côté serveur
    # (Streamlit kill la session après timeout).
    time.sleep(REFRESH_INTERVAL_S)
    st.rerun()


if __name__ == "__main__":
    main()
