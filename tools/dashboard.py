"""Dashboard Streamlit v2 — Topstep Live (mobile-first, 5 tabs).

Refonte complète après audit utilisateur (cumul faux, layout pauvre, manque de graphes).

Sources de données (toutes en lecture seule) :
- state/live_state.json :
    * risk_state.cum_pnl / peak_pnl / realized_day_pnl  ← AUTORITATIF (corrige
      le bug du v1 qui sommait naïvement tous les close_pnl)
    * placed_tags : détails trade par trade (entry, sl, tp, fill_time, close_pnl)
- logs/trading_events.log : événements horodatés (latence WS, derniers signaux)
- state/shadow_state.json (optionnel) : comparaison shadow vs live

Architecture :
- 5 tabs : Pulse / Strats / Equity / Trades / Système
- Auto-refresh 30s via time.sleep + st.rerun
- Plotly pour tous les graphes (gauges, sparklines, equity curve, heatmap)
- Cognitive load minimal : tab Pulse = 1 écran sans scroll (mobile-first)

Inspirations : Topstep dashboard (instrument cluster), Edgewonk (strats breakdown),
JournalPlus (equity + DD + WR), Tradezella (mobile-first KPIs).
"""

from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

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
SHADOW_STATE_PATH = ROOT / "state" / "shadow_state.json"
LOG_PATH = ROOT / "logs" / "trading_events.log"
REFRESH_INTERVAL_S = 30

# Palette
GREEN = "#3ddc84"
RED = "#ff6b6b"
YELLOW = "#ffb347"
BLUE = "#5e9eff"
GREY = "#6c757d"
BG = "#0e1117"
BG2 = "#1a1d24"


# ──────────────────────────────────────────────────────────────────────────────
# Lecture sources
# ──────────────────────────────────────────────────────────────────────────────


def _read_state(path: Path = STATE_PATH) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _tail_log(n: int = 30) -> list[str]:
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
            lines = data.decode("utf-8", errors="replace").splitlines()
            return lines[-n:]
    except OSError:
        return []


# ──────────────────────────────────────────────────────────────────────────────
# Calculs métriques (autoritatifs)
# ──────────────────────────────────────────────────────────────────────────────


def _today_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def authoritative_pnl(state: dict) -> dict:
    """SOURCE DE VÉRITÉ — utilise risk_state du PortfolioRiskManager.

    Corrige le bug v1 qui sommait naïvement tous les close_pnl historiques.
    """
    rs = state.get("risk_state", {}) or {}
    cum = float(rs.get("cum_pnl", 0.0))
    peak = float(rs.get("peak_pnl", 0.0))
    rdp = float(rs.get("realized_day_pnl", 0.0))
    active_dd = max(0.0, peak - cum)
    return {
        "cum_pnl": cum,
        "peak_pnl": peak,
        "realized_day_pnl": rdp,
        "active_drawdown": active_dd,
        "consec_loss_days": int(rs.get("consec_loss_days", 0)),
        "daily_fills_count": int(rs.get("daily_fills_count", 0)),
    }


def chronological_trades(state: dict) -> list[dict]:
    """Trades clos triés par exit_time (ou fill_time) chronologique.

    Chaque entrée a : tag, strategy, ticker, dir, n_ct, fill_time, exit_time,
    close_pnl. Sert à reconstruire l'equity curve.
    """
    rows = []
    for tag, info in state.get("placed_tags", {}).items():
        if info.get("close_pnl") is None:
            continue
        t_exit = info.get("exit_time") or info.get("fill_time") or info.get("placed_at") or ""
        rows.append(
            {
                "tag": tag,
                "strategy": info.get("strategy", "?"),
                "ticker": info.get("ticker", "?"),
                "dir": info.get("direction", "?"),
                "n_ct": int(info.get("n_ct", 0)),
                "fill_time": info.get("fill_time", ""),
                "exit_time": t_exit,
                "entry": info.get("entry"),
                "exit": info.get("exit"),
                "close_pnl": float(info["close_pnl"]),
            }
        )
    return sorted(rows, key=lambda r: r["exit_time"])


def equity_series(trades: list[dict], cum_offset: float = 0.0) -> tuple[list[str], list[float]]:
    """Construit la série (dates, equity_cumulée) à partir des trades chrono.

    cum_offset : valeur de départ (utile si on veut aligner avec risk_state.cum_pnl).
    """
    if not trades:
        return [], []
    dates, equity = [], []
    cum = cum_offset
    for t in trades:
        cum += t["close_pnl"]
        dates.append(t["exit_time"][:10] if t["exit_time"] else "?")
        equity.append(round(cum, 2))
    return dates, equity


def strategy_stats(trades: list[dict]) -> dict[str, dict]:
    """Stats agrégées par stratégie (FIB / OPR / ...)."""
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
    # Compute derived
    for s, d in by_strat.items():
        d["wr_pct"] = (d["wins"] / d["n"] * 100) if d["n"] else 0.0
        d["pf"] = (d["gross_win"] / d["gross_loss"]) if d["gross_loss"] > 0 else float("inf")
        d["avg_pnl"] = (d["pnl"] / d["n"]) if d["n"] else 0.0
    return dict(by_strat)


def today_trades(trades: list[dict], date_str: str | None = None) -> list[dict]:
    date_str = date_str or _today_utc()
    return [t for t in trades if (t["exit_time"] or "").startswith(date_str)]


def open_positions(state: dict) -> list[dict]:
    out = []
    for tag, info in state.get("placed_tags", {}).items():
        if info.get("status") == "FILLED" and info.get("close_pnl") is None:
            out.append({"tag": tag, **info})
    return out


def open_orders(state: dict) -> list[dict]:
    out = []
    open_statuses = {"PLACED", "PENDING", "WORKING", "ARMED"}
    for tag, info in state.get("placed_tags", {}).items():
        if info.get("status") in open_statuses:
            out.append({"tag": tag, **info})
    return out


def last_event_age(lines: list[str]) -> tuple[float | None, str | None]:
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            ts_str = line[:19]
            dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
            return (datetime.now(UTC) - dt).total_seconds(), line
        except ValueError:
            continue
    return None, None


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


def distances_topstep(pnl: dict) -> list[dict]:
    """Calcule les distances aux 5 limites Topstep + état couleur."""
    rdp = pnl["realized_day_pnl"]
    cum = pnl["cum_pnl"]
    peak = pnl["peak_pnl"]

    rows = [
        {
            "label": f"DLL user ${USER_DAILY_LOSS_MAX}",
            "distance": USER_DAILY_LOSS_MAX + rdp,
            "ratio_used": max(0.0, -rdp) / USER_DAILY_LOSS_MAX,
            "kind": "loss",
        },
        {
            "label": f"DLL Topstep ${TOPSTEP_DAILY_LOSS_MAX}",
            "distance": TOPSTEP_DAILY_LOSS_MAX + rdp,
            "ratio_used": max(0.0, -rdp) / TOPSTEP_DAILY_LOSS_MAX,
            "kind": "loss",
        },
        {
            "label": f"Trailing DD ${TOPSTEP_TRAILING_DD}",
            "distance": max(0.0, cum - (peak - TOPSTEP_TRAILING_DD)),
            "ratio_used": max(0.0, peak - cum) / TOPSTEP_TRAILING_DD,
            "kind": "loss",
        },
        {
            "label": f"Consistency ${CHALLENGE_CONSISTENCY_BEST_DAY_MAX_USD}",
            "distance": CHALLENGE_CONSISTENCY_BEST_DAY_MAX_USD - rdp,
            "ratio_used": max(0.0, rdp) / CHALLENGE_CONSISTENCY_BEST_DAY_MAX_USD,
            "kind": "gain",
        },
        {
            "label": f"Profit target ${TOPSTEP_PROFIT_TARGET}",
            "distance": TOPSTEP_PROFIT_TARGET - cum,
            "ratio_used": max(0.0, cum) / TOPSTEP_PROFIT_TARGET,
            "kind": "gain",
        },
    ]

    for r in rows:
        ratio = min(1.0, r["ratio_used"])
        if r["kind"] == "loss":
            # Plus on consomme la limite de perte, plus c'est rouge
            r["color"] = RED if ratio > 0.8 else (YELLOW if ratio > 0.5 else GREEN)
        else:
            # Plus on s'approche du target, plus c'est positif (vert)
            r["color"] = GREEN if ratio > 0.5 else (BLUE if ratio > 0.2 else GREY)
    return rows


# ──────────────────────────────────────────────────────────────────────────────
# Composants visuels Plotly
# ──────────────────────────────────────────────────────────────────────────────


def horizontal_gauge(value: float, max_value: float, label: str, color: str) -> go.Figure:
    """Barre horizontale type fuel gauge — mobile friendly."""
    ratio = min(1.0, max(0.0, value / max_value)) if max_value > 0 else 0
    fig = go.Figure()
    # Barre fond
    fig.add_trace(
        go.Bar(
            x=[max_value],
            y=[label],
            orientation="h",
            marker_color=BG2,
            showlegend=False,
            hoverinfo="skip",
        )
    )
    # Barre valeur
    fig.add_trace(
        go.Bar(
            x=[value],
            y=[label],
            orientation="h",
            marker_color=color,
            showlegend=False,
            text=f"${value:+,.0f}",
            textposition="outside",
            textfont={"color": "#fafafa", "size": 13},
            hoverinfo="skip",
        )
    )
    fig.update_layout(
        barmode="overlay",
        height=42,
        margin={"l": 110, "r": 80, "t": 4, "b": 4},
        xaxis={"visible": False, "range": [0, max_value * 1.15]},
        yaxis={"tickfont": {"color": "#fafafa", "size": 12}},
        plot_bgcolor=BG,
        paper_bgcolor=BG,
    )
    return fig


def equity_sparkline(dates: list[str], equity: list[float]) -> go.Figure:
    """Mini courbe equity sans axes — compact pour la tab Pulse."""
    if not equity:
        fig = go.Figure()
        fig.add_annotation(
            text="Pas encore de trades clos",
            xref="paper",
            yref="paper",
            x=0.5,
            y=0.5,
            showarrow=False,
            font={"color": GREY},
        )
    else:
        # Fill area : vert si dernier > premier, rouge sinon
        color = GREEN if equity[-1] >= equity[0] else RED
        fig = go.Figure(
            go.Scatter(
                x=list(range(len(equity))),
                y=equity,
                mode="lines",
                line={"color": color, "width": 2},
                fill="tozeroy",
                fillcolor=f"{color}33",
                hovertemplate="$%{y:+,.0f}<extra></extra>",
            )
        )
    fig.update_layout(
        height=110,
        margin={"l": 0, "r": 0, "t": 8, "b": 0},
        xaxis={"visible": False},
        yaxis={"visible": False},
        plot_bgcolor=BG,
        paper_bgcolor=BG,
        showlegend=False,
    )
    return fig


def equity_curve_full(dates: list[str], equity: list[float], peak: float) -> go.Figure:
    fig = go.Figure()
    if equity:
        color = GREEN if equity[-1] >= 0 else RED
        fig.add_trace(
            go.Scatter(
                x=dates,
                y=equity,
                mode="lines",
                line={"color": color, "width": 2.5},
                fill="tozeroy",
                fillcolor=f"{color}22",
                name="Equity",
                hovertemplate="%{x}<br>$%{y:+,.0f}<extra></extra>",
            )
        )
        if peak > 0:
            fig.add_hline(
                y=peak,
                line={"color": BLUE, "width": 1, "dash": "dash"},
                annotation_text=f"Peak ${peak:+,.0f}",
                annotation_position="top right",
                annotation_font={"color": BLUE, "size": 11},
            )
    fig.update_layout(
        height=320,
        margin={"l": 50, "r": 20, "t": 30, "b": 40},
        xaxis={"showgrid": False, "tickfont": {"color": "#fafafa", "size": 11}},
        yaxis={
            "showgrid": True,
            "gridcolor": "#2a2d34",
            "tickfont": {"color": "#fafafa", "size": 11},
            "title": {"text": "$ cumulé", "font": {"color": GREY, "size": 11}},
        },
        plot_bgcolor=BG,
        paper_bgcolor=BG,
        showlegend=False,
        hovermode="x",
    )
    return fig


def drawdown_underwater(dates: list[str], equity: list[float]) -> go.Figure:
    """Drawdown underwater chart — DD négatif sous l'axe."""
    fig = go.Figure()
    if equity:
        peak_so_far = equity[0]
        dd = []
        for v in equity:
            peak_so_far = max(peak_so_far, v)
            dd.append(v - peak_so_far)  # ≤ 0
        fig.add_trace(
            go.Scatter(
                x=dates,
                y=dd,
                mode="lines",
                line={"color": RED, "width": 2},
                fill="tozeroy",
                fillcolor=f"{RED}33",
                hovertemplate="%{x}<br>DD: $%{y:+,.0f}<extra></extra>",
            )
        )
    fig.update_layout(
        height=180,
        margin={"l": 50, "r": 20, "t": 20, "b": 40},
        xaxis={"showgrid": False, "tickfont": {"color": "#fafafa", "size": 11}},
        yaxis={
            "showgrid": True,
            "gridcolor": "#2a2d34",
            "tickfont": {"color": "#fafafa", "size": 11},
            "title": {"text": "Drawdown $", "font": {"color": GREY, "size": 11}},
        },
        plot_bgcolor=BG,
        paper_bgcolor=BG,
        showlegend=False,
    )
    return fig


def pnl_distribution(trades: list[dict]) -> go.Figure:
    fig = go.Figure()
    if trades:
        pnls = [t["close_pnl"] for t in trades]
        colors = [GREEN if p > 0 else RED for p in pnls]
        fig.add_trace(
            go.Histogram(
                x=pnls,
                nbinsx=20,
                marker={"color": colors},
                hovertemplate="$%{x:+,.0f}<br>%{y} trades<extra></extra>",
            )
        )
        median = float(pd.Series(pnls).median())
        fig.add_vline(
            x=median,
            line={"color": BLUE, "width": 1.5, "dash": "dash"},
            annotation_text=f"Médian ${median:+,.0f}",
            annotation_font={"color": BLUE, "size": 11},
        )
    fig.update_layout(
        height=220,
        margin={"l": 50, "r": 20, "t": 30, "b": 40},
        xaxis={
            "title": {"text": "P&L par trade ($)", "font": {"color": GREY, "size": 11}},
            "tickfont": {"color": "#fafafa", "size": 11},
            "showgrid": False,
        },
        yaxis={
            "title": {"text": "n trades", "font": {"color": GREY, "size": 11}},
            "tickfont": {"color": "#fafafa", "size": 11},
            "showgrid": True,
            "gridcolor": "#2a2d34",
        },
        plot_bgcolor=BG,
        paper_bgcolor=BG,
        showlegend=False,
    )
    return fig


def strategy_bar_chart(stats: dict[str, dict]) -> go.Figure:
    if not stats:
        return go.Figure()
    names = list(stats.keys())
    pnls = [stats[n]["pnl"] for n in names]
    colors = [GREEN if p > 0 else RED for p in pnls]
    fig = go.Figure(
        go.Bar(
            x=names,
            y=pnls,
            marker_color=colors,
            text=[f"${p:+,.0f}" for p in pnls],
            textposition="outside",
            textfont={"color": "#fafafa", "size": 13},
            hovertemplate="%{x}<br>$%{y:+,.0f}<extra></extra>",
        )
    )
    fig.update_layout(
        height=240,
        margin={"l": 40, "r": 20, "t": 30, "b": 40},
        xaxis={"tickfont": {"color": "#fafafa", "size": 13}, "showgrid": False},
        yaxis={
            "tickfont": {"color": "#fafafa", "size": 11},
            "showgrid": True,
            "gridcolor": "#2a2d34",
            "zerolinecolor": GREY,
        },
        plot_bgcolor=BG,
        paper_bgcolor=BG,
        showlegend=False,
    )
    return fig


def monthly_heatmap(trades: list[dict]) -> go.Figure:
    """Heatmap mois × jour avec couleur PnL journalier."""
    if not trades:
        fig = go.Figure()
        fig.add_annotation(
            text="Pas encore de trades clos",
            xref="paper",
            yref="paper",
            x=0.5,
            y=0.5,
            showarrow=False,
            font={"color": GREY},
        )
        fig.update_layout(height=240, plot_bgcolor=BG, paper_bgcolor=BG)
        return fig

    # Agrège par date
    daily = defaultdict(float)
    for t in trades:
        d = (t["exit_time"] or "")[:10]
        if d:
            daily[d] += t["close_pnl"]

    df = pd.DataFrame(
        [{"date": pd.to_datetime(d), "pnl": v} for d, v in daily.items()]
    ).sort_values("date")
    if df.empty:
        return go.Figure()
    df["month"] = df["date"].dt.strftime("%Y-%m")
    df["day"] = df["date"].dt.day

    pivot = df.pivot_table(index="month", columns="day", values="pnl", aggfunc="sum").fillna(0)

    fig = go.Figure(
        go.Heatmap(
            z=pivot.values,
            x=[str(d) for d in pivot.columns],
            y=list(pivot.index),
            colorscale=[
                [0.0, RED],
                [0.5, BG2],
                [1.0, GREEN],
            ],
            zmid=0,
            hovertemplate="%{y} jour %{x}<br>$%{z:+,.0f}<extra></extra>",
            colorbar={"tickfont": {"color": "#fafafa", "size": 10}, "title": "$"},
        )
    )
    fig.update_layout(
        height=240,
        margin={"l": 70, "r": 20, "t": 20, "b": 40},
        xaxis={
            "title": {"text": "Jour du mois", "font": {"color": GREY, "size": 11}},
            "tickfont": {"color": "#fafafa", "size": 10},
            "side": "bottom",
        },
        yaxis={"tickfont": {"color": "#fafafa", "size": 11}, "autorange": "reversed"},
        plot_bgcolor=BG,
        paper_bgcolor=BG,
    )
    return fig


# ──────────────────────────────────────────────────────────────────────────────
# Cards & helpers UI
# ──────────────────────────────────────────────────────────────────────────────


def kpi_card(label: str, value: str, subtitle: str = "", color: str = "#fafafa") -> None:
    """Carte KPI stylée — utilisée pour les chiffres principaux."""
    st.markdown(
        f"""
        <div style="background:{BG2};border-radius:12px;padding:14px 16px;
                    border-left:4px solid {color};">
            <div style="color:{GREY};font-size:11px;text-transform:uppercase;
                        letter-spacing:1px;margin-bottom:4px;">{label}</div>
            <div style="color:{color};font-size:28px;font-weight:700;
                        line-height:1;">{value}</div>
            <div style="color:{GREY};font-size:12px;margin-top:6px;">{subtitle}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def pnl_color(value: float) -> str:
    if value > 0:
        return GREEN
    if value < 0:
        return RED
    return GREY


# ──────────────────────────────────────────────────────────────────────────────
# TAB 1 — PULSE (mobile-first, sans scroll)
# ──────────────────────────────────────────────────────────────────────────────


def render_pulse(state: dict, pnl: dict, trades: list[dict]) -> None:
    # Header ligne fine
    age, _ = last_event_age(_tail_log(n=10))
    st.markdown(
        f"""
        <div style="display:flex;justify-content:space-between;color:{GREY};
                    font-size:12px;margin-bottom:14px;">
            <span>Compte <b style="color:#fafafa;">{state.get('account_id', '?')}</b></span>
            <span>State <b style="color:#fafafa;">{state.get('date', '?')}</b></span>
            <span>Last event <b style="color:#fafafa;">{format_age(age)}</b></span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # 2 KPI cards : P&L Jour + P&L Cumul Challenge
    c1, c2 = st.columns(2)
    with c1:
        kpi_card(
            "P&L Jour",
            f"${pnl['realized_day_pnl']:+,.0f}",
            subtitle=f"{pnl['daily_fills_count']} fills aujourd'hui",
            color=pnl_color(pnl["realized_day_pnl"]),
        )
    with c2:
        cum = pnl["cum_pnl"]
        peak = pnl["peak_pnl"]
        sub = f"Peak ${peak:+,.0f}"
        if pnl["active_drawdown"] > 0:
            sub += f" · DD ${pnl['active_drawdown']:.0f}"
        kpi_card("P&L Cumul Challenge", f"${cum:+,.0f}", subtitle=sub, color=pnl_color(cum))

    st.markdown("<br>", unsafe_allow_html=True)

    # 5 gauges horizontales
    st.markdown(
        f"<div style='color:{GREY};font-size:11px;text-transform:uppercase;"
        f"letter-spacing:1px;'>Limites Topstep</div>",
        unsafe_allow_html=True,
    )
    distances = distances_topstep(pnl)
    for d in distances:
        # max_value = la valeur absolue de la limite (1000, 2000, etc.)
        # Pour les "loss", on affiche la distance restante avant breach
        # Pour les "gain", on affiche la distance restante vers l'objectif
        if d["kind"] == "loss":
            limit = {
                "DLL user $950": USER_DAILY_LOSS_MAX,
                "DLL Topstep $1000": TOPSTEP_DAILY_LOSS_MAX,
                "Trailing DD $2000": TOPSTEP_TRAILING_DD,
            }.get(d["label"], 1000.0)
            fig = horizontal_gauge(d["distance"], limit, d["label"], d["color"])
        else:
            limit = {
                "Consistency $1400": CHALLENGE_CONSISTENCY_BEST_DAY_MAX_USD,
                "Profit target $3000": TOPSTEP_PROFIT_TARGET,
            }.get(d["label"], 3000.0)
            fig = horizontal_gauge(d["distance"], limit, d["label"], d["color"])
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

    # Sparkline equity
    st.markdown(
        f"<div style='color:{GREY};font-size:11px;text-transform:uppercase;"
        f"letter-spacing:1px;margin-top:8px;'>Equity Curve</div>",
        unsafe_allow_html=True,
    )
    dates, equity = equity_series(trades)
    st.plotly_chart(
        equity_sparkline(dates, equity),
        use_container_width=True,
        config={"displayModeBar": False},
    )


# ──────────────────────────────────────────────────────────────────────────────
# TAB 2 — STRATS
# ──────────────────────────────────────────────────────────────────────────────


def render_strats(trades: list[dict]) -> None:
    stats = strategy_stats(trades)
    if not stats:
        st.info("Pas encore de trades clos pour calculer les stats par stratégie.")
        return

    # Table
    rows = []
    for s, d in sorted(stats.items()):
        rows.append(
            {
                "Stratégie": s,
                "n": d["n"],
                "P&L": f"${d['pnl']:+,.0f}",
                "WR %": f"{d['wr_pct']:.0f}",
                "PF": "∞" if d["pf"] == float("inf") else f"{d['pf']:.2f}",
                "Avg/trade": f"${d['avg_pnl']:+,.0f}",
            }
        )
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    # Bar chart P&L par stratégie
    st.markdown(
        f"<div style='color:{GREY};font-size:11px;text-transform:uppercase;"
        f"letter-spacing:1px;margin-top:12px;'>P&L par stratégie</div>",
        unsafe_allow_html=True,
    )
    st.plotly_chart(
        strategy_bar_chart(stats), use_container_width=True, config={"displayModeBar": False}
    )


# ──────────────────────────────────────────────────────────────────────────────
# TAB 3 — EQUITY
# ──────────────────────────────────────────────────────────────────────────────


def render_equity(pnl: dict, trades: list[dict]) -> None:
    dates, equity = equity_series(trades)

    st.markdown(
        f"<div style='color:{GREY};font-size:11px;text-transform:uppercase;"
        f"letter-spacing:1px;'>Equity cumulée (challenge en cours)</div>",
        unsafe_allow_html=True,
    )
    st.plotly_chart(
        equity_curve_full(dates, equity, pnl["peak_pnl"]),
        use_container_width=True,
        config={"displayModeBar": False},
    )

    st.markdown(
        f"<div style='color:{GREY};font-size:11px;text-transform:uppercase;"
        f"letter-spacing:1px;margin-top:8px;'>Drawdown underwater</div>",
        unsafe_allow_html=True,
    )
    st.plotly_chart(
        drawdown_underwater(dates, equity),
        use_container_width=True,
        config={"displayModeBar": False},
    )

    st.markdown(
        f"<div style='color:{GREY};font-size:11px;text-transform:uppercase;"
        f"letter-spacing:1px;margin-top:8px;'>Distribution P&L par trade</div>",
        unsafe_allow_html=True,
    )
    st.plotly_chart(
        pnl_distribution(trades), use_container_width=True, config={"displayModeBar": False}
    )


# ──────────────────────────────────────────────────────────────────────────────
# TAB 4 — TRADES
# ──────────────────────────────────────────────────────────────────────────────


def render_trades(state: dict, trades: list[dict]) -> None:
    # Trades aujourd'hui
    today = today_trades(trades)
    st.markdown(
        f"<div style='color:{GREY};font-size:11px;text-transform:uppercase;"
        f"letter-spacing:1px;'>Trades aujourd'hui ({len(today)})</div>",
        unsafe_allow_html=True,
    )
    if today:
        rows = [
            {
                "Stratégie": t["strategy"],
                "Ticker": t["ticker"],
                "Dir": t["dir"],
                "n_ct": t["n_ct"],
                "Entry": t["entry"],
                "Exit": t["exit"],
                "P&L": f"${t['close_pnl']:+,.0f}",
            }
            for t in today
        ]
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.caption("Aucun trade clos aujourd'hui.")

    # Positions ouvertes
    positions = open_positions(state)
    orders = open_orders(state)
    if positions or orders:
        c1, c2 = st.columns(2)
        with c1:
            st.markdown(
                f"<div style='color:{GREY};font-size:11px;text-transform:uppercase;"
                f"letter-spacing:1px;'>Positions ouvertes ({len(positions)})</div>",
                unsafe_allow_html=True,
            )
            for p in positions[:5]:
                st.markdown(
                    f"**{p['ticker']}** {p['direction']} ×{p['n_ct']}  \n"
                    f"@ `{p.get('entry','?')}`  SL `{p.get('sl','?')}`  TP `{p.get('tp','?')}`"
                )
        with c2:
            st.markdown(
                f"<div style='color:{GREY};font-size:11px;text-transform:uppercase;"
                f"letter-spacing:1px;'>Ordres en attente ({len(orders)})</div>",
                unsafe_allow_html=True,
            )
            for o in orders[:5]:
                st.markdown(
                    f"**{o['ticker']}** {o['direction']} ×{o['n_ct']}  \n"
                    f"status `{o['status']}`  id `{o.get('order_id','?')}`"
                )

    # Heatmap mensuelle
    st.markdown(
        f"<div style='color:{GREY};font-size:11px;text-transform:uppercase;"
        f"letter-spacing:1px;margin-top:14px;'>Heatmap mensuelle P&L</div>",
        unsafe_allow_html=True,
    )
    st.plotly_chart(
        monthly_heatmap(trades), use_container_width=True, config={"displayModeBar": False}
    )


# ──────────────────────────────────────────────────────────────────────────────
# TAB 5 — SYS
# ──────────────────────────────────────────────────────────────────────────────


def render_system(state: dict) -> None:
    log_lines = _tail_log(n=30)
    age, last_line = last_event_age(log_lines)

    c1, c2 = st.columns(2)
    with c1:
        kpi_card(
            "Dernier événement", format_age(age), subtitle="Heure WS dernière activité", color=BLUE
        )
    with c2:
        now = datetime.now(UTC).strftime("%H:%M:%S")
        kpi_card("Heure actuelle (UTC)", now, subtitle="Maintenant", color=GREY)

    if age is not None and age > 600:
        st.warning(
            f"⚠️ Dernier événement il y a {format_age(age)} — WS potentiellement déconnectée."
        )

    # Comparaison shadow
    st.markdown(
        f"<div style='color:{GREY};font-size:11px;text-transform:uppercase;"
        f"letter-spacing:1px;margin-top:14px;'>Shadow vs Live</div>",
        unsafe_allow_html=True,
    )
    shadow = _read_state(SHADOW_STATE_PATH)
    if shadow is None:
        st.caption("Shadow runner non démarré (state/shadow_state.json absent).")
    else:
        live_tags = set(state.get("placed_tags", {}))
        shadow_tags = set(shadow.get("placed_tags", {}))
        common = live_tags & shadow_tags
        only_live = live_tags - shadow_tags
        only_shadow = shadow_tags - live_tags
        sc1, sc2, sc3 = st.columns(3)
        with sc1:
            kpi_card("Tags communs", str(len(common)), "", color=GREEN)
        with sc2:
            kpi_card("Unique live", str(len(only_live)), "", color=YELLOW if only_live else GREY)
        with sc3:
            kpi_card(
                "Unique shadow", str(len(only_shadow)), "", color=YELLOW if only_shadow else GREY
            )

    # Logs récents
    st.markdown(
        f"<div style='color:{GREY};font-size:11px;text-transform:uppercase;"
        f"letter-spacing:1px;margin-top:14px;'>Logs récents</div>",
        unsafe_allow_html=True,
    )
    if not log_lines:
        st.caption("`logs/trading_events.log` vide.")
    else:
        for line in reversed(log_lines[-8:]):
            if line.strip():
                st.code(line, language=None)


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────


def main():
    state = _read_state()
    if state is None:
        st.error("⚠️ `state/live_state.json` introuvable ou illisible.")
        return

    pnl = authoritative_pnl(state)
    trades = chronological_trades(state)

    st.markdown(
        f"""
        <div style="display:flex;align-items:center;justify-content:space-between;
                    padding:2px 0 14px 0;">
            <div style="font-size:24px;font-weight:700;color:#fafafa;">📊 Topstep Live</div>
            <div style="font-size:11px;color:{GREY};">v2 · auto-refresh {REFRESH_INTERVAL_S}s</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    tab1, tab2, tab3, tab4, tab5 = st.tabs(
        ["💓 Pulse", "🎯 Strats", "📈 Equity", "📋 Trades", "⚙️ Sys"]
    )
    with tab1:
        render_pulse(state, pnl, trades)
    with tab2:
        render_strats(trades)
    with tab3:
        render_equity(pnl, trades)
    with tab4:
        render_trades(state, trades)
    with tab5:
        render_system(state)

    # Auto-refresh
    time.sleep(REFRESH_INTERVAL_S)
    st.rerun()


if __name__ == "__main__":
    main()
