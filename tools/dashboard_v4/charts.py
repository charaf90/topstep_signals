"""Charts Plotly v4 — palette iOS dark + couleur d'accent PAR STRATÉGIE.

Reprend les figures éprouvées du v3 (equity, drawdown, distribution, heatmap
calendrier) et ajoute : sparklines, contribution au P&L, heatmap de
corrélation, jauge CI du verdict sizing, equity multi-stratégies, funnel.
"""

from __future__ import annotations

from collections import defaultdict

import pandas as pd
import plotly.graph_objects as go

# Palette — iOS system colors
GREEN = "#34C759"
RED = "#FF3B30"
ORANGE = "#FF9500"
BLUE = "#007AFF"
PURPLE = "#AF52DE"
TEAL = "#5AC8FA"
GREY = "#8E8E93"
GREY_DIM = "#2C2C2E"
BG = "#000000"
BG2 = "#1C1C1E"
BG3 = "#2C2C2E"

# Accent par stratégie — utilisé PARTOUT (courbes, barres, pastilles)
STRAT_COLORS = {
    "OPR": BLUE,
    "FIB": TEAL,
    "FIB_FINE": PURPLE,
    "BOS_FVG": ORANGE,
}
STRAT_LABELS = {"OPR": "OPR", "FIB": "Fib", "FIB_FINE": "Fib Fine", "BOS_FVG": "BOS-FVG"}

FONT = {
    "family": '-apple-system, "SF Pro Display", Inter, system-ui, sans-serif',
    "color": "#FFFFFF",
}


def rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r}, {g}, {b}, {alpha:.2f})"


def _empty_chart(message: str, height: int = 180) -> go.Figure:
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
        height=height,
        plot_bgcolor=BG,
        paper_bgcolor=BG,
        margin={"l": 0, "r": 0, "t": 0, "b": 0},
        xaxis={"visible": False},
        yaxis={"visible": False},
    )
    return fig


def _base_axes(fig: go.Figure, height: int, prefix: str = "$") -> None:
    fig.update_layout(
        height=height,
        margin={"l": 52, "r": 16, "t": 10, "b": 36},
        xaxis={
            "showgrid": False,
            "tickfont": {**FONT, "size": 10, "color": GREY},
            "linecolor": GREY_DIM,
        },
        yaxis={
            "showgrid": True,
            "gridcolor": GREY_DIM,
            "zerolinecolor": GREY,
            "tickfont": {**FONT, "size": 10, "color": GREY},
            "tickprefix": prefix,
        },
        plot_bgcolor=BG,
        paper_bgcolor=BG,
        font=FONT,
        showlegend=False,
    )


# ── Sparkline (cartes Pulse) ─────────────────────────────────────────────────


def sparkline(values: list[float], color: str, height: int = 38) -> go.Figure:
    fig = go.Figure()
    if len(values) >= 2:
        fig.add_trace(
            go.Scatter(
                y=values,
                mode="lines",
                line={"color": color, "width": 1.8},
                fill="tozeroy",
                fillcolor=rgba(color, 0.12),
                hoverinfo="skip",
            )
        )
    fig.update_layout(
        height=height,
        margin={"l": 0, "r": 0, "t": 2, "b": 2},
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        xaxis={"visible": False},
        yaxis={"visible": False},
        showlegend=False,
    )
    return fig


# ── Equity ───────────────────────────────────────────────────────────────────


def equity_curve(
    timestamps: list[str],
    equity: list[float],
    trades: list[dict],
    peak: float | None = None,
    height: int = 300,
    color: str | None = None,
) -> go.Figure:
    if not equity:
        return _empty_chart("Pas encore de trades clos", height)
    color = color or (GREEN if equity[-1] >= 0 else RED)
    hover = []
    for ts, eq, t in zip(timestamps, equity, trades, strict=False):
        pnl = t.get("pnl_net", t.get("close_pnl", 0))
        hover.append(
            f"<b>{(ts or '?')[:10]}</b><br>{t.get('ticker', '?')} {t.get('dir', '?')} "
            f"×{t.get('n_ct', 0)}<br>P&L net: <b>${pnl:+,.0f}</b><br>Cumul: <b>${eq:+,.0f}</b>"
        )
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=timestamps,
            y=equity,
            mode="lines",
            line={"color": color, "width": 2.5},
            fill="tozeroy",
            fillcolor=rgba(color, 0.13),
            hovertext=hover,
            hoverinfo="text",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=[timestamps[-1]],
            y=[equity[-1]],
            mode="markers",
            marker={"size": 8, "color": color, "line": {"color": BG, "width": 2}},
            hoverinfo="skip",
        )
    )
    if peak and peak > 0:
        fig.add_hline(
            y=peak,
            line={"color": BLUE, "width": 1.5, "dash": "dash"},
            annotation={
                "text": f"<b>Peak ${peak:+,.0f}</b>",
                "font": {**FONT, "color": BLUE, "size": 10},
                "bgcolor": rgba(BLUE, 0.10),
                "borderpad": 4,
            },
            annotation_position="top left",
        )
    _base_axes(fig, height)
    fig.update_layout(
        hovermode="x unified",
        hoverlabel={"bgcolor": BG3, "bordercolor": GREY_DIM, "font": {**FONT, "size": 11}},
    )
    return fig


def multi_equity(strat_aggs: dict[str, dict], height: int = 280) -> go.Figure:
    """Courbes d'equity superposées, une par stratégie (accent dédié)."""
    fig = go.Figure()
    has_data = False
    for key, agg in strat_aggs.items():
        if not agg["equity"]:
            continue
        has_data = True
        color = STRAT_COLORS.get(key, GREY)
        fig.add_trace(
            go.Scatter(
                x=agg["timestamps"],
                y=agg["equity"],
                mode="lines",
                name=STRAT_LABELS.get(key, key),
                line={"color": color, "width": 2},
                hovertemplate="%{x|%d/%m} · $%{y:+,.0f}<extra>"
                + STRAT_LABELS.get(key, key)
                + "</extra>",
            )
        )
    if not has_data:
        return _empty_chart("Pas encore de trades clos", height)
    _base_axes(fig, height)
    fig.update_layout(
        showlegend=True,
        legend={
            "orientation": "h",
            "y": 1.12,
            "font": {**FONT, "size": 11},
            "bgcolor": "rgba(0,0,0,0)",
        },
    )
    return fig


def drawdown_underwater(timestamps: list[str], equity: list[float], height: int = 170) -> go.Figure:
    if not equity:
        return _empty_chart("Pas encore de trades clos", height)
    peak_so_far = equity[0]
    dd = []
    for v in equity:
        peak_so_far = max(peak_so_far, v)
        dd.append(round(v - peak_so_far, 2))
    fig = go.Figure(
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
    _base_axes(fig, height)
    return fig


# ── Portefeuille ─────────────────────────────────────────────────────────────


def correlation_heatmap(corr, overlap, height: int = 280) -> go.Figure:
    """Corrélation des P&L journaliers — NaN (recouvrement < 8 j) affiché n/a."""
    if corr is None or corr.isna().all().all():
        return _empty_chart("Pas encore 8 jours communs entre stratégies", height)
    keys = list(corr.columns)
    labels = [STRAT_LABELS.get(k, k) for k in keys]
    z = corr.values
    text = []
    for i, ki in enumerate(keys):
        row = []
        for j, kj in enumerate(keys):
            v = z[i][j]
            n_ov = int(overlap.loc[ki, kj]) if overlap is not None else 0
            row.append(
                "n/a" if pd.isna(v) else f"{v:+.2f}<br><span style='font-size:9px'>{n_ov}j</span>"
            )
        text.append(row)
    fig = go.Figure(
        go.Heatmap(
            z=z,
            x=labels,
            y=labels,
            zmin=-1,
            zmax=1,
            colorscale=[[0.0, RED], [0.5, BG2], [1.0, GREEN]],
            xgap=4,
            ygap=4,
            text=text,
            texttemplate="%{text}",
            textfont={**FONT, "size": 12},
            hoverinfo="skip",
            showscale=False,
        )
    )
    fig.update_layout(
        height=height,
        margin={"l": 70, "r": 10, "t": 6, "b": 50},
        xaxis={"tickfont": {**FONT, "size": 10, "color": GREY}},
        yaxis={"tickfont": {**FONT, "size": 10, "color": GREY}, "autorange": "reversed"},
        plot_bgcolor=BG,
        paper_bgcolor=BG,
        font=FONT,
    )
    return fig


# ── Verdict sizing — jauge CI horizontale ────────────────────────────────────


def ci_gauge(verdict: dict, height: int = 110) -> go.Figure:
    """[CI bootstrap de l'expectancy R live] + marqueurs 0 et référence OOS."""
    lo, hi = verdict["ci"]
    mean = verdict["expectancy_r"]
    ref = verdict["ref_r"]
    color = {"green": GREEN, "red": RED, "blue": BLUE, "orange": ORANGE, "grey": GREY}.get(
        verdict["color"], GREY
    )

    span_lo = min(lo, 0, ref) - 0.12
    span_hi = max(hi, 0, ref) + 0.12
    fig = go.Figure()
    # Piste
    fig.add_shape(
        type="rect", x0=span_lo, x1=span_hi, y0=0.40, y1=0.60, fillcolor=BG3, line={"width": 0}
    )
    # CI
    fig.add_shape(
        type="rect",
        x0=lo,
        x1=hi,
        y0=0.36,
        y1=0.64,
        fillcolor=rgba(color, 0.45),
        line={"color": color, "width": 1},
    )
    # Moyenne live
    fig.add_shape(
        type="line", x0=mean, x1=mean, y0=0.28, y1=0.72, line={"color": color, "width": 3}
    )
    # Zéro
    fig.add_shape(
        type="line", x0=0, x1=0, y0=0.18, y1=0.82, line={"color": GREY, "width": 1, "dash": "dot"}
    )
    fig.add_annotation(
        x=0, y=0.10, text="0", showarrow=False, font={**FONT, "size": 10, "color": GREY}
    )
    # Référence backtest
    fig.add_shape(
        type="line",
        x0=ref,
        x1=ref,
        y0=0.18,
        y1=0.82,
        line={"color": "#FFFFFF", "width": 2, "dash": "dash"},
    )
    fig.add_annotation(
        x=ref,
        y=0.94,
        text=f"réf {ref:+.2f}R",
        showarrow=False,
        font={**FONT, "size": 10, "color": "#FFFFFF"},
    )
    # Bornes CI
    fig.add_annotation(
        x=lo, y=0.10, text=f"{lo:+.2f}", showarrow=False, font={**FONT, "size": 9, "color": color}
    )
    fig.add_annotation(
        x=hi, y=0.10, text=f"{hi:+.2f}", showarrow=False, font={**FONT, "size": 9, "color": color}
    )
    fig.update_layout(
        height=height,
        margin={"l": 10, "r": 10, "t": 8, "b": 4},
        xaxis={"range": [span_lo, span_hi], "visible": False},
        yaxis={"range": [0, 1], "visible": False},
        plot_bgcolor=BG2,
        paper_bgcolor=BG2,
        font=FONT,
        showlegend=False,
    )
    return fig


# ── Funnel signaux ───────────────────────────────────────────────────────────


def funnel_bars(f: dict, color: str, height: int = 170) -> go.Figure:
    """SIGNAL → ORDRE → FILL / ANNULÉ / BLOQUÉ pour une stratégie."""
    stages = ["Signaux", "Ordres", "Fills", "Annulés", "Bloqués"]
    vals = [f["signals"], f["orders"], f["fills"], f["cancelled"], f["blocked"]]
    colors = [rgba(color, 0.9), rgba(color, 0.7), GREEN, GREY, RED]
    fig = go.Figure(
        go.Bar(
            y=stages[::-1],
            x=vals[::-1],
            orientation="h",
            marker={"color": colors[::-1]},
            text=[str(v) for v in vals[::-1]],
            textposition="outside",
            textfont={**FONT, "size": 12},
            hoverinfo="skip",
            width=0.6,
        )
    )
    fig.update_layout(
        height=height,
        margin={"l": 62, "r": 30, "t": 6, "b": 8},
        xaxis={"visible": False, "range": [0, max(vals) * 1.25 if max(vals) else 1]},
        yaxis={"tickfont": {**FONT, "size": 11, "color": GREY}},
        plot_bgcolor=BG2,
        paper_bgcolor=BG2,
        font=FONT,
        showlegend=False,
    )
    return fig


# ── Distribution + heatmap calendrier (repris v3, P&L net) ──────────────────


def pnl_distribution(trades: list[dict], height: int = 210) -> go.Figure:
    if not trades:
        return _empty_chart("Pas encore de trades", height)
    pnls = [t.get("pnl_net", t.get("close_pnl", 0)) for t in trades]
    fig = go.Figure(
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
    _base_axes(fig, height)
    fig.update_layout(yaxis={"tickprefix": ""})
    return fig


def heatmap_calendar(trades: list[dict], height: int = 230) -> go.Figure:
    """Heatmap mois × jour du mois (style contributions GitHub)."""
    if not trades:
        return _empty_chart("Pas encore de trades clos", height)
    daily: dict[str, float] = defaultdict(float)
    for t in trades:
        d = (t["fill_time"] or "")[:10]
        if d:
            daily[d] += t.get("pnl_net", t.get("close_pnl", 0))
    if not daily:
        return _empty_chart("Pas de dates valides", height)
    df = pd.DataFrame([{"date": pd.to_datetime(d), "pnl": v} for d, v in daily.items()])
    df = df.sort_values("date")
    # month/day en STRINGS — sinon Plotly convertit l'axe en datetime continu
    df["month"] = df["date"].dt.strftime("%Y-%m").astype(str)
    df["day"] = df["date"].dt.day.astype(str)
    pivot = df.pivot_table(index="month", columns="day", values="pnl", aggfunc="sum")
    pivot = pivot.reindex(columns=sorted(pivot.columns, key=int))
    abs_max = max(abs(df["pnl"].min()), abs(df["pnl"].max()), 1)
    fig = go.Figure(
        go.Heatmap(
            z=pivot.values,
            x=list(pivot.columns),
            y=list(pivot.index),
            colorscale=[[0.0, RED], [0.45, BG2], [0.5, GREY_DIM], [0.55, BG2], [1.0, GREEN]],
            zmid=0,
            zmin=-abs_max,
            zmax=abs_max,
            xgap=4,
            ygap=4,
            hovertemplate="<b>%{y} · jour %{x}</b><br>P&L: <b>$%{z:+,.0f}</b><extra></extra>",
            showscale=False,
        )
    )
    fig.update_layout(
        height=height,
        margin={"l": 64, "r": 10, "t": 6, "b": 34},
        xaxis={"tickfont": {**FONT, "size": 9, "color": GREY}, "showgrid": False},
        yaxis={
            "tickfont": {**FONT, "size": 10, "color": "#fafafa"},
            "autorange": "reversed",
            "showgrid": False,
            "type": "category",
        },
        plot_bgcolor=BG,
        paper_bgcolor=BG,
        font=FONT,
    )
    return fig
