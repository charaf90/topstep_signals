"""Onglet STRATÉGIES — détail par stratégie + VERDICT SIZING (carte vedette).

Le verdict compare l'expectancy R live (CI bootstrap 95 %) à la référence
backtest OOS (strategy_refs.py) — jamais de verdict binaire à n petit.
"""

from __future__ import annotations

from dash import dcc, html

from tools.dashboard_v4 import components as c
from tools.dashboard_v4.accounts import AccountConfig
from tools.dashboard_v4.charts import (
    STRAT_COLORS,
    STRAT_LABELS,
    ci_gauge,
    equity_curve,
    funnel_bars,
)
from tools.dashboard_v4.datasource import STRATEGY_KEYS, portfolio_status
from tools.dashboard_v4.events import funnel_stats, read_events
from tools.dashboard_v4.stats import compute_all
from tools.dashboard_v4.strategy_refs import STRATEGY_REFS, stale_refs


def build(acc: AccountConfig, selected: str = "OPR") -> list:
    """Shell de l'onglet : segmented control + détail (rendu par callback)."""
    return [
        dcc.RadioItems(
            id="strat-select",
            options=[{"label": STRAT_LABELS[k], "value": k} for k in STRATEGY_KEYS],
            value=selected,
            className="v4-segment",
            inputClassName="v4-segment-input",
            labelClassName="v4-segment-label",
        ),
        html.Div(id="strat-detail"),
    ]


def _verdict_card(key: str, verdict: dict, ref) -> html.Div:
    wr_lo, wr_hi = verdict["wr_ci"]
    wr_line = f"Win rate live {verdict['wr'] * 100:.0f}% [CI {wr_lo * 100:.0f}–{wr_hi * 100:.0f}]"
    if ref.wr_oos is not None:
        wr_line += f" · réf {ref.wr_oos * 100:.0f}%"
    extra = []
    if verdict["code"] == "CALIBRAGE" and verdict["n_remaining"]:
        extra.append(
            html.Div(
                f"≈ {verdict['n_remaining']} trades avant un CI exploitable (±15 pts)",
                className="v4-note",
            )
        )
    return c.card(
        [
            html.Div(
                [
                    c.verdict_badge(verdict),
                    html.Span(f"n={verdict['n']} · CI 95 %", className="v4-verdict-n"),
                ],
                className="v4-verdict-head",
            ),
            html.Div(verdict["detail"], className="v4-verdict-detail"),
            c.graph(ci_gauge(verdict), style={"height": "110px"}),
            html.Div(wr_line, className="v4-note"),
            html.Div(
                f"Réf backtest : {ref.version} · PF OOS {ref.pf_oos:.2f} · "
                f"E[R] {ref.expectancy_r:+.2f} · n={ref.n_oos} ({ref.date_ref})",
                className="v4-note dim",
            ),
        ]
        + extra,
        title="Verdict sizing",
        icon="bi-speedometer2",
    )


def detail(acc: AccountConfig, key: str) -> list:
    snap = compute_all(acc)
    agg = snap["strat_aggs"].get(key)
    verdict = snap["verdicts"].get(key)
    ref = STRATEGY_REFS.get(key)
    color = STRAT_COLORS.get(key, "#8E8E93")
    status = next((p for p in portfolio_status(snap["state"]) if p["key"] == key), None)

    blocks: list = []

    # Alerte ref périmée (version config ≠ version ref)
    stale = stale_refs().get(key)
    if stale:
        blocks.append(
            html.Div(
                [
                    html.I(className="bi bi-exclamation-triangle"),
                    f" Référence périmée : ref {stale[0]} vs config {stale[1]} — "
                    "mettre à jour strategy_refs.py",
                ],
                className="v4-alert",
            )
        )

    if verdict and ref:
        blocks.append(_verdict_card(key, verdict, ref))

    if not agg:
        blocks.append(
            c.card(
                html.Div("Aucun trade clos pour cette stratégie", className="v4-empty"),
                title=STRAT_LABELS.get(key, key),
            )
        )
        return blocks

    # KPIs
    wr_lo, wr_hi = agg["wr_ci"]
    blocks.append(
        html.Div(
            [
                c.kpi_card(
                    "P&L net",
                    c.fmt_usd(agg["pnl_net"]),
                    color="green" if agg["pnl_net"] > 0 else "red",
                    sub=f"fees ${agg['fees']:,.0f}",
                ),
                c.kpi_card(
                    "Profit factor",
                    c.fmt_pf(agg["pf"]),
                    sub=f"réf OOS {ref.pf_oos:.2f}" if ref else None,
                    color=(
                        "green" if agg["pf"] >= 1.5 else ("orange" if agg["pf"] >= 1.0 else "red")
                    ),
                ),
                c.kpi_card(
                    "Win rate",
                    f"{agg['wr'] * 100:.0f}%",
                    sub=f"CI [{wr_lo * 100:.0f}–{wr_hi * 100:.0f}] · n={agg['n']}",
                ),
                c.kpi_card(
                    "Expectancy",
                    f"{agg['expectancy_r']:+.2f} R",
                    sub=f"${agg['expectancy_usd']:+,.0f}/trade",
                ),
            ],
            className="v4-kpi-grid",
        )
    )

    # Risk utilisé vs configuré (détecte le sizing dégradé)
    if status:
        configured = float(status["sizing"] or 0)
        used = agg["avg_risk_used"]
        ratio = used / configured if configured > 0 else 0
        col = "green" if ratio >= 0.9 else ("orange" if ratio >= 0.7 else "red")
        blocks.append(
            c.card(
                html.Div(
                    [
                        c.kpi_card(
                            "Risk configuré",
                            f"${configured:,.0f}",
                            sub=f"{status['universe']} · {status['version']}",
                        ),
                        c.kpi_card(
                            "Risk moyen appliqué",
                            f"${used:,.0f}",
                            sub=f"{ratio * 100:.0f}% du configuré",
                            color=col,
                        ),
                    ],
                    className="v4-kpi-grid two",
                ),
                title="Sizing appliqué",
                icon="bi-cash-stack",
            )
        )

    # Equity (couleur d'accent de la stratégie)
    eq_fig = equity_curve(agg["timestamps"], agg["equity"], agg["trades"], height=240, color=color)
    blocks.append(c.card(c.graph(eq_fig), title="Equity", icon="bi-graph-up"))

    # Funnel signaux
    funnel = funnel_stats(read_events(acc.events_log)).get(key)
    if funnel and funnel["signals"]:
        nf = funnel["not_filled_rate"]
        blocks.append(
            c.card(
                [
                    c.graph(funnel_bars(funnel, color)),
                    html.Div(
                        f"Taux not-filled : {nf * 100:.0f}% des ordres" if nf is not None else "",
                        className="v4-note",
                    ),
                ],
                title="Funnel signaux",
                icon="bi-funnel",
            )
        )

    # Derniers trades
    blocks.append(
        c.card(
            c.trades_table(agg["trades"], limit=10),
            title="Derniers trades",
            icon="bi-clock-history",
        )
    )
    return blocks
