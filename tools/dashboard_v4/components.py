"""Composants UI réutilisables v4 — Dash html/dcc uniquement (zéro calcul)."""

from __future__ import annotations

from dash import dcc, html

from tools.dashboard_v4.charts import STRAT_COLORS, STRAT_LABELS

COLOR_VARS = {
    "green": "var(--green)",
    "red": "var(--red)",
    "blue": "var(--blue)",
    "orange": "var(--orange)",
    "grey": "var(--grey)",
    "purple": "var(--purple)",
    "teal": "var(--teal)",
}


def pnl_class(value: float) -> str:
    if value > 0:
        return "pos"
    if value < 0:
        return "neg"
    return "flat"


def fmt_usd(value: float, signed: bool = True) -> str:
    return f"${value:+,.0f}" if signed else f"${value:,.0f}"


def fmt_pf(pf: float) -> str:
    return "∞" if pf == float("inf") else f"{pf:.2f}"


def card(
    children, title: str | None = None, icon: str | None = None, className: str = ""
) -> html.Div:
    head = []
    if title:
        head = [
            html.Div(
                [html.I(className=f"bi {icon}") if icon else None, html.Span(title)],
                className="v4-card-title",
            )
        ]
    return html.Div(
        head + (children if isinstance(children, list) else [children]),
        className=f"v4-card {className}".strip(),
    )


def kpi_card(label: str, value: str, sub: str | None = None, color: str | None = None) -> html.Div:
    style = {"color": COLOR_VARS.get(color, "var(--text)")} if color else {}
    rows = [
        html.Div(label, className="v4-kpi-label"),
        html.Div(value, className="v4-kpi-value", style=style),
    ]
    if sub:
        rows.append(html.Div(sub, className="v4-kpi-sub"))
    return html.Div(rows, className="v4-kpi")


def pill(text: str, color: str = "grey") -> html.Span:
    return html.Span(text, className=f"v4-pill {color}")


def strat_dot(key: str) -> html.Span:
    return html.Span(className="v4-dot", style={"background": STRAT_COLORS.get(key, "var(--grey)")})


def strat_name(key: str) -> html.Span:
    return html.Span(
        STRAT_LABELS.get(key, key),
        className="v4-strat-name",
        style={"color": STRAT_COLORS.get(key, "var(--text)")},
    )


def verdict_badge(verdict: dict, mini: bool = False) -> html.Span:
    cls = f"v4-verdict {verdict['color']}" + (" mini" if mini else "")
    return html.Span(verdict["label"], className=cls)


def risk_bar(margin: dict) -> html.Div:
    """Barre de marge Topstep (cf. datasource.risk_margins)."""
    ratio = margin["color_ratio"]
    if margin["over"]:
        color, status = "var(--red)", "DÉPASSÉ"
    elif ratio >= 0.75:
        color, status = "var(--red)", f"reste ${margin['headroom']:,.0f}"
    elif ratio >= 0.45:
        color, status = "var(--orange)", f"reste ${margin['headroom']:,.0f}"
    else:
        color, status = "var(--green)", f"reste ${margin['headroom']:,.0f}"
    children = [
        html.Div(
            [
                html.Span(margin["label"]),
                html.Span(status, style={"color": color, "fontWeight": "600"}),
            ],
            className="v4-risk-head",
        ),
        html.Div(
            [
                html.Div(
                    className="v4-risk-fill",
                    style={"width": f"{margin['fill_ratio'] * 100:.1f}%", "background": color},
                ),
            ]
            + (
                [
                    html.Div(
                        className="v4-risk-mark",
                        style={"left": f"{margin['soft_ratio'] * 100:.1f}%"},
                    )
                ]
                if margin.get("soft_ratio")
                else []
            ),
            className="v4-risk-track",
        ),
        html.Div(
            [
                html.Span(f"${margin['used']:,.0f} utilisé"),
                html.Span(f"mur ${margin['track_max']:,.0f}"),
            ],
            className="v4-risk-cap",
        ),
    ]
    return html.Div(children, className="v4-risk-bar")


def health_dot(ok: bool | None) -> html.Span:
    cls = "ok" if ok else ("warn" if ok is None else "bad")
    return html.Span(className=f"v4-health-dot {cls}")


def trades_table(trades: list[dict], limit: int = 20) -> html.Div:
    """Table compacte : date · strat · ticker · dir · R · P&L net."""
    if not trades:
        return html.Div("Aucun trade", className="v4-empty")
    rows = []
    for t in list(reversed(trades))[:limit]:
        pnl = t.get("pnl_net", t.get("close_pnl", 0))
        r = t.get("r_multiple")
        dim = " dim" if t.get("source") == "state_only" else ""
        rows.append(
            html.Div(
                [
                    html.Span(
                        (t.get("fill_time") or "")[5:10].replace("-", "/"), className="v4-tr-date"
                    ),
                    html.Span(
                        [strat_dot(t["strategy"]), STRAT_LABELS.get(t["strategy"], t["strategy"])],
                        className="v4-tr-strat",
                    ),
                    html.Span(
                        f"{t.get('ticker', '?')} {t.get('dir', '?')[:1].upper()}",
                        className="v4-tr-ticker",
                    ),
                    html.Span(
                        f"{r:+.2f}R" if r is not None else "—",
                        className=f"v4-tr-r {pnl_class(pnl)}",
                    ),
                    html.Span(fmt_usd(pnl), className=f"v4-tr-pnl {pnl_class(pnl)}"),
                ],
                className=f"v4-tr{dim}",
            )
        )
    return html.Div(rows, className="v4-trades")


def graph(fig, **kwargs) -> dcc.Graph:
    return dcc.Graph(figure=fig, config={"displayModeBar": False}, **kwargs)


def contribution_rows(contrib: dict[str, float]) -> html.Div:
    """Contribution au P&L par stratégie — barres HTML (lisible sur 390 px)."""
    if not contrib:
        return html.Div("Pas encore de trades", className="v4-empty")
    total = sum(contrib.values())
    span = max(abs(v) for v in contrib.values()) or 1.0
    rows = []
    for key in sorted(contrib, key=lambda k: contrib[k], reverse=True):
        val = contrib[key]
        pct = f"{val / total * 100:.0f}%" if total != 0 else "—"
        width = abs(val) / span * 100
        color = STRAT_COLORS.get(key, "var(--grey)")
        rows.append(
            html.Div(
                [
                    html.Div(
                        [
                            html.Span(
                                [strat_dot(key), STRAT_LABELS.get(key, key)],
                                className="v4-contrib-name",
                            ),
                            html.Span(
                                f"{fmt_usd(val)} · {pct}",
                                className=f"v4-contrib-val {pnl_class(val)}",
                            ),
                        ],
                        className="v4-contrib-head",
                    ),
                    html.Div(
                        html.Div(
                            className="v4-contrib-fill",
                            style={
                                "width": f"{width:.1f}%",
                                "background": color if val >= 0 else "var(--red)",
                            },
                        ),
                        className="v4-contrib-track",
                    ),
                ],
                className="v4-contrib-row",
            )
        )
    return html.Div(rows)


def log_lines(lines: list[str]) -> html.Div:
    if not lines:
        return html.Div("(vide)", className="v4-empty")
    out = []
    for line in lines:
        cls = "v4-log-line"
        if "[ERROR" in line:
            cls += " err"
        elif "[CLÔTURE" in line or "[FILL" in line:
            cls += " hl"
        out.append(html.Div(line, className=cls))
    return html.Div(out, className="v4-log")
