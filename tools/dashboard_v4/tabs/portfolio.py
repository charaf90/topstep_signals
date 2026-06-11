"""Onglet PORTEFEUILLE — stats agrégées toutes stratégies (absent du v3).

KPIs globaux nets · equity combinée + par stratégie · contribution au P&L ·
corrélation des P&L journaliers · carte Monte-Carlo (portfolio_replay).
"""

from __future__ import annotations

from dash import html

from tools.dashboard_v4 import components as c
from tools.dashboard_v4.accounts import AccountConfig
from tools.dashboard_v4.charts import (
    correlation_heatmap,
    drawdown_underwater,
    equity_curve,
    multi_equity,
)
from tools.dashboard_v4.datasource import load_replay
from tools.dashboard_v4.stats import compute_all, correlation_matrix


def _kpis(pf: dict) -> html.Div:
    lo, hi = pf["expectancy_r_ci"]
    return html.Div(
        [
            c.kpi_card(
                "PF global net",
                c.fmt_pf(pf["pf"]),
                color="green" if pf["pf"] >= 1.5 else ("orange" if pf["pf"] >= 1.0 else "red"),
            ),
            c.kpi_card(
                "Expectancy",
                f"{pf['expectancy_r']:+.2f} R",
                sub=f"${pf['expectancy_usd']:+,.0f}/trade · CI [{lo:+.2f}, {hi:+.2f}]",
            ),
            c.kpi_card(
                "Jours verts / rouges",
                f"{pf['win_days']} / {pf['loss_days']}",
                color="green" if pf["win_days"] >= pf["loss_days"] else "red",
            ),
            c.kpi_card(
                "Trades", str(pf["n"]), sub=f"WR {pf['wr'] * 100:.0f}% · fees ${pf['fees']:,.0f}"
            ),
        ],
        className="v4-kpi-grid",
    )


def _mc_card(replay: dict | None, pf: dict) -> html.Div | None:
    if not replay:
        return None
    mc = replay.get("mc_drawdown_days", {})
    outcome = replay.get("challenge_outcome", {})
    port = replay.get("portfolio", {})
    dd_p95 = mc.get("dd_p95_worst")
    p_target = outcome.get("p_target")
    days_med = outcome.get("days_to_target_median")
    # DD réalisé sur l'equity live (trades unifiés)
    eq = pf["equity"]
    dd_live = 0.0
    peak = eq[0] if eq else 0.0
    for v in eq:
        peak = max(peak, v)
        dd_live = min(dd_live, v - peak)
    rows = html.Div(
        [
            c.kpi_card(
                "P(target)",
                f"{p_target * 100:.0f}%" if p_target is not None else "—",
                sub=f"médiane {days_med:.0f} j" if days_med else None,
                color="green" if (p_target or 0) > 0.9 else "orange",
            ),
            c.kpi_card(
                "DD P95 attendu",
                f"${dd_p95:,.0f}" if dd_p95 is not None else "—",
                sub=f"réalisé ${dd_live:,.0f}",
                color="green" if dd_p95 is None or dd_live >= dd_p95 else "red",
            ),
            c.kpi_card(
                "Pire jour simulé",
                f"${port.get('worst_day', 0):,.0f}",
                sub=f"{port.get('n_days', '—')} jours rejoués",
            ),
            c.kpi_card(
                "Breach trailing",
                f"{(outcome.get('p_breach_trailing') or 0) * 100:.1f}%",
                color="green" if (outcome.get("p_breach_trailing") or 1) < 0.05 else "red",
            ),
        ],
        className="v4-kpi-grid",
    )
    note = html.Div(
        f"portfolio_replay du {replay.get('_mtime', '?')} · fenêtre "
        f"{replay.get('window_start', '?')} → {replay.get('window_end', '?')}",
        className="v4-note",
    )
    return c.card([rows, note], title="Monte-Carlo portefeuille", icon="bi-dice-5")


def build(acc: AccountConfig) -> list:
    snap = compute_all(acc)
    pf = snap["portfolio"]
    if pf["n"] == 0:
        return [
            c.card(
                html.Div("Pas encore de trades clos", className="v4-empty"), title="Portefeuille"
            )
        ]

    corr, overlap = correlation_matrix(snap["trades"])
    peak = max(pf["equity"]) if pf["equity"] else 0.0

    blocks = [
        _kpis(pf),
        c.card(
            [
                c.graph(
                    equity_curve(
                        pf["timestamps"], pf["equity"], pf["trades"], peak=peak, height=280
                    )
                ),
                html.Div(
                    "P&L net cumulé, trades unifiés state + broker (fees incluses)",
                    className="v4-note",
                ),
            ],
            title="Equity combinée",
            icon="bi-graph-up",
        ),
        c.card(c.graph(multi_equity(snap["strat_aggs"])), title="Par stratégie", icon="bi-layers"),
        c.card(
            c.graph(drawdown_underwater(pf["timestamps"], pf["equity"])),
            title="Drawdown",
            icon="bi-water",
        ),
        c.card(
            c.contribution_rows(pf["contribution"]),
            title="Contribution au P&L",
            icon="bi-bar-chart-steps",
        ),
        c.card(
            [
                c.graph(correlation_heatmap(corr, overlap)),
                html.Div(
                    "Corrélation des P&L journaliers (0 pour une stratégie inactive un "
                    "jour où une autre trade) · n/a si < 8 jours où les deux ont tradé",
                    className="v4-note",
                ),
            ],
            title="Corrélations",
            icon="bi-bezier2",
        ),
    ]
    mc = _mc_card(load_replay(), pf)
    if mc is not None:
        blocks.append(mc)
    return blocks
