"""Charts Plotly — composants visuels stylés pour le dashboard v3.

Toutes les figures partagent une palette dark cohérente et une typo Inter.
"""

from __future__ import annotations

from collections import defaultdict

import pandas as pd
import plotly.graph_objects as go

# Palette
GREEN = "#3ddc84"
RED = "#ff6b6b"
YELLOW = "#ffb347"
BLUE = "#5e9eff"
PURPLE = "#a78bfa"
GREY = "#6c757d"
GREY_DIM = "#3a3d44"
BG = "#0e1117"
BG2 = "#1a1d24"
BG3 = "#252830"

FONT = {"family": "Inter, system-ui, sans-serif", "color": "#fafafa"}


def rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r}, {g}, {b}, {alpha:.2f})"


# ──────────────────────────────────────────────────────────────────────────────
# GAUGE RADIAL — speedometer style Topstep
# ──────────────────────────────────────────────────────────────────────────────


def radial_gauge(
    value: float, max_value: float, label: str, color: str, height: int = 180
) -> go.Figure:
    """Gauge radiale Plotly Indicator — type speedometer.

    Affiche la valeur centrée, avec un arc gradué qui se remplit selon
    le pourcentage utilisé. Sépare en zones colorées (sûr, attention, danger).
    """
    pct = max(0.0, min(100.0, (value / max_value * 100) if max_value > 0 else 0))
    fig = go.Figure(
        go.Indicator(
            mode="gauge+number",
            value=value,
            number={
                "prefix": "$",
                "valueformat": ",.0f",
                "font": {**FONT, "size": 24, "color": color},
            },
            domain={"x": [0, 1], "y": [0, 1]},
            gauge={
                "axis": {
                    "range": [0, max_value],
                    "tickwidth": 0,
                    "tickfont": {**FONT, "size": 10, "color": GREY},
                    "ticksuffix": "",
                    "showticklabels": False,
                },
                "bar": {"color": color, "thickness": 0.32},
                "bgcolor": BG2,
                "borderwidth": 0,
                "steps": [
                    {"range": [0, max_value * 0.5], "color": rgba(GREEN, 0.10)},
                    {"range": [max_value * 0.5, max_value * 0.8], "color": rgba(YELLOW, 0.12)},
                    {"range": [max_value * 0.8, max_value], "color": rgba(RED, 0.15)},
                ],
                "threshold": {
                    "line": {"color": "#fafafa", "width": 2},
                    "thickness": 0.7,
                    "value": value,
                },
            },
        )
    )
    fig.add_annotation(
        text=f"<b>{label}</b><br><span style='color:{GREY};font-size:10px'>{pct:.0f}% utilisé</span>",
        xref="paper",
        yref="paper",
        x=0.5,
        y=-0.05,
        showarrow=False,
        font={**FONT, "size": 11},
    )
    fig.update_layout(
        height=height,
        margin={"l": 8, "r": 8, "t": 24, "b": 32},
        paper_bgcolor=BG,
        plot_bgcolor=BG,
        font=FONT,
    )
    return fig


# ──────────────────────────────────────────────────────────────────────────────
# EQUITY CURVE — interactive avec hover détaillé + peak annotation
# ──────────────────────────────────────────────────────────────────────────────


def equity_curve_pro(
    timestamps: list[str], equity: list[float], trades: list[dict], peak: float
) -> go.Figure:
    """Equity curve avec hover détaillé (date, trade, cumul)."""
    fig = go.Figure()
    if not equity:
        return _empty_chart("Pas encore de trades clos")

    color = GREEN if equity[-1] >= 0 else RED

    # Construire le hover text custom : date, ticker, dir, pnl
    hover_text = []
    for ts, eq, t in zip(timestamps, equity, trades, strict=False):
        date_str = ts[:10] if ts else "?"
        ticker = t.get("ticker", "?")
        direction = t.get("dir", "?")
        pnl = t["close_pnl"]
        hover_text.append(
            f"<b>{date_str}</b><br>"
            f"{ticker} {direction} ×{t.get('n_ct', 0)}<br>"
            f"P&L: <b style='color:{GREEN if pnl > 0 else RED}'>${pnl:+,.0f}</b><br>"
            f"Cumul: <b>${eq:+,.0f}</b>"
        )

    fig.add_trace(
        go.Scatter(
            x=timestamps,
            y=equity,
            mode="lines+markers",
            line={"color": color, "width": 2.5, "shape": "linear"},
            fill="tozeroy",
            fillcolor=rgba(color, 0.13),
            marker={"size": 5, "color": color, "line": {"color": BG, "width": 1}},
            hovertext=hover_text,
            hoverinfo="text",
            name="Equity",
        )
    )

    if peak > 0:
        fig.add_hline(
            y=peak,
            line={"color": BLUE, "width": 1.5, "dash": "dash"},
            annotation={
                "text": f"<b>Peak ${peak:+,.0f}</b>",
                "font": {**FONT, "color": BLUE, "size": 11},
                "bgcolor": rgba(BLUE, 0.10),
                "borderpad": 4,
            },
            annotation_position="top right",
        )

    fig.update_layout(
        height=340,
        margin={"l": 55, "r": 24, "t": 10, "b": 40},
        xaxis={
            "showgrid": False,
            "tickfont": {**FONT, "size": 11, "color": GREY},
            "linecolor": GREY_DIM,
            "rangeslider": {"visible": False},
        },
        yaxis={
            "showgrid": True,
            "gridcolor": GREY_DIM,
            "zerolinecolor": GREY,
            "tickfont": {**FONT, "size": 11, "color": GREY},
            "tickprefix": "$",
        },
        plot_bgcolor=BG,
        paper_bgcolor=BG,
        font=FONT,
        showlegend=False,
        hovermode="x unified",
        hoverlabel={
            "bgcolor": BG3,
            "bordercolor": GREY_DIM,
            "font": {**FONT, "size": 11},
        },
    )
    return fig


def drawdown_underwater_pro(timestamps: list[str], equity: list[float]) -> go.Figure:
    fig = go.Figure()
    if not equity:
        return _empty_chart("Pas encore de trades clos")

    peak_so_far = equity[0]
    dd = []
    for v in equity:
        peak_so_far = max(peak_so_far, v)
        dd.append(round(v - peak_so_far, 2))

    fig.add_trace(
        go.Scatter(
            x=timestamps,
            y=dd,
            mode="lines",
            line={"color": RED, "width": 2},
            fill="tozeroy",
            fillcolor=rgba(RED, 0.20),
            hovertemplate="<b>%{x|%Y-%m-%d}</b><br>DD: <b>$%{y:+,.0f}</b><extra></extra>",
        )
    )
    fig.update_layout(
        height=180,
        margin={"l": 55, "r": 24, "t": 10, "b": 40},
        xaxis={
            "showgrid": False,
            "tickfont": {**FONT, "size": 11, "color": GREY},
            "linecolor": GREY_DIM,
        },
        yaxis={
            "showgrid": True,
            "gridcolor": GREY_DIM,
            "tickfont": {**FONT, "size": 11, "color": GREY},
            "tickprefix": "$",
        },
        plot_bgcolor=BG,
        paper_bgcolor=BG,
        font=FONT,
        showlegend=False,
    )
    return fig


# ──────────────────────────────────────────────────────────────────────────────
# DISTRIBUTION P&L
# ──────────────────────────────────────────────────────────────────────────────


def pnl_distribution_pro(trades: list[dict]) -> go.Figure:
    fig = go.Figure()
    if not trades:
        return _empty_chart("Pas encore de trades")

    pnls = [t["close_pnl"] for t in trades]
    fig.add_trace(
        go.Histogram(
            x=pnls,
            nbinsx=20,
            marker={"color": GREEN, "line": {"color": BG, "width": 1}},
            opacity=0.7,
            hovertemplate="$%{x:+,.0f}<br>%{y} trades<extra></extra>",
        )
    )
    median = float(pd.Series(pnls).median())
    fig.add_vline(
        x=median,
        line={"color": BLUE, "width": 2, "dash": "dash"},
        annotation_text=f"Médian ${median:+,.0f}",
        annotation_font={**FONT, "color": BLUE, "size": 11},
    )
    fig.update_layout(
        height=220,
        margin={"l": 55, "r": 24, "t": 10, "b": 50},
        xaxis={
            "title": {"text": "P&L par trade ($)", "font": {**FONT, "color": GREY, "size": 11}},
            "tickfont": {**FONT, "size": 11, "color": GREY},
            "showgrid": False,
            "linecolor": GREY_DIM,
        },
        yaxis={
            "title": {"text": "n trades", "font": {**FONT, "color": GREY, "size": 11}},
            "tickfont": {**FONT, "size": 11, "color": GREY},
            "showgrid": True,
            "gridcolor": GREY_DIM,
        },
        plot_bgcolor=BG,
        paper_bgcolor=BG,
        font=FONT,
        showlegend=False,
    )
    return fig


# ──────────────────────────────────────────────────────────────────────────────
# HEATMAP — style Github contributions
# ──────────────────────────────────────────────────────────────────────────────


def heatmap_github_style(trades: list[dict]) -> go.Figure:
    """Heatmap style Github contributions : grid de cellules, gradient propre."""
    if not trades:
        return _empty_chart("Pas encore de trades clos")

    daily: dict[str, float] = defaultdict(float)
    for t in trades:
        d = (t["fill_time"] or "")[:10]
        if d:
            daily[d] += t["close_pnl"]
    if not daily:
        return _empty_chart("Pas de dates valides")

    df = pd.DataFrame([{"date": pd.to_datetime(d), "pnl": v} for d, v in daily.items()])
    df = df.sort_values("date")
    # IMPORTANT : conserver month/day comme STRINGS (sinon Plotly traite l'axe Y
    # en datetime continu et affiche des timestamps bizarres comme "23:59:59.9996")
    df["month"] = df["date"].dt.strftime("%Y-%m").astype(str)
    df["day"] = df["date"].dt.day.astype(str)

    # Pivot mois × jour-du-mois — values devient un array 2D
    pivot = df.pivot_table(index="month", columns="day", values="pnl", aggfunc="sum")
    # Tri des colonnes day numérique (mais en string pour l'affichage)
    pivot = pivot.reindex(columns=sorted(pivot.columns, key=int))

    # Colorscale : rouge → grey foncé → vert (centré sur 0)
    abs_max = max(abs(df["pnl"].min()), abs(df["pnl"].max()), 1)

    fig = go.Figure(
        go.Heatmap(
            z=pivot.values,
            x=list(pivot.columns),
            y=list(pivot.index),
            colorscale=[
                [0.0, RED],
                [0.45, BG2],
                [0.5, GREY_DIM],
                [0.55, BG2],
                [1.0, GREEN],
            ],
            zmid=0,
            zmin=-abs_max,
            zmax=abs_max,
            xgap=4,
            ygap=4,
            hovertemplate="<b>%{y} · jour %{x}</b><br>P&L: <b>$%{z:+,.0f}</b><extra></extra>",
            colorbar={
                "tickfont": {**FONT, "size": 10, "color": GREY},
                "title": {"text": "$", "font": {**FONT, "size": 11, "color": GREY}},
                "outlinewidth": 0,
                "thickness": 12,
                "len": 0.7,
            },
        )
    )
    fig.update_layout(
        height=240,
        margin={"l": 80, "r": 24, "t": 10, "b": 40},
        xaxis={
            "title": {"text": "Jour du mois", "font": {**FONT, "color": GREY, "size": 11}},
            "tickfont": {**FONT, "size": 10, "color": GREY},
            "showgrid": False,
            "side": "bottom",
        },
        yaxis={
            "tickfont": {**FONT, "size": 11, "color": "#fafafa"},
            "autorange": "reversed",
            "showgrid": False,
            "type": "category",  # FORCE catégoriel — évite la conversion datetime
        },
        plot_bgcolor=BG,
        paper_bgcolor=BG,
        font=FONT,
    )
    return fig


# ──────────────────────────────────────────────────────────────────────────────
# STRAT BAR CHART
# ──────────────────────────────────────────────────────────────────────────────


def strategy_bars(stats: dict[str, dict]) -> go.Figure:
    if not stats:
        return _empty_chart("Pas encore de stats")

    names = list(stats.keys())
    pnls = [stats[n]["pnl"] for n in names]
    colors = [GREEN if p > 0 else RED for p in pnls]

    fig = go.Figure(
        go.Bar(
            x=names,
            y=pnls,
            marker={"color": colors, "line": {"color": BG, "width": 1}},
            text=[f"${p:+,.0f}" for p in pnls],
            textposition="outside",
            textfont={**FONT, "size": 14, "color": "#fafafa"},
            hovertemplate="<b>%{x}</b><br>$%{y:+,.0f}<extra></extra>",
            width=0.5,
        )
    )
    fig.update_layout(
        height=260,
        margin={"l": 50, "r": 30, "t": 40, "b": 40},
        xaxis={
            "tickfont": {**FONT, "size": 14, "color": "#fafafa"},
            "showgrid": False,
            "linecolor": GREY_DIM,
        },
        yaxis={
            "tickfont": {**FONT, "size": 11, "color": GREY},
            "tickprefix": "$",
            "showgrid": True,
            "gridcolor": GREY_DIM,
            "zerolinecolor": GREY,
        },
        plot_bgcolor=BG,
        paper_bgcolor=BG,
        font=FONT,
        showlegend=False,
    )
    return fig


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _empty_chart(message: str) -> go.Figure:
    fig = go.Figure()
    fig.add_annotation(
        text=message,
        xref="paper",
        yref="paper",
        x=0.5,
        y=0.5,
        showarrow=False,
        font={**FONT, "color": GREY, "size": 13},
    )
    fig.update_layout(
        height=200,
        plot_bgcolor=BG,
        paper_bgcolor=BG,
        margin={"l": 0, "r": 0, "t": 0, "b": 0},
        xaxis={"visible": False},
        yaxis={"visible": False},
    )
    return fig
