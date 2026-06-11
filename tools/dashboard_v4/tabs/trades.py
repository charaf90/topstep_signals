"""Onglet TRADES — jour courant, positions/pending, historique, calendrier."""

from __future__ import annotations

from dash import html

from tools.dashboard_v4 import components as c
from tools.dashboard_v4.accounts import AccountConfig
from tools.dashboard_v4.broker import get_broker, trading_day_start
from tools.dashboard_v4.charts import heatmap_calendar
from tools.dashboard_v4.datasource import active_positions, pending_orders
from tools.dashboard_v4.stats import compute_all


def _armed_row(o: dict, kind: str) -> html.Div:
    return html.Div(
        [
            html.Span(
                [c.strat_dot(o["strategy"]), c.strat_name(o["strategy"])], className="v4-tr-strat"
            ),
            html.Span(o["ticker"], className="v4-tr-ticker"),
            html.Span(kind, className="v4-tr-date"),
            html.Span(f"${o['risk_usd']:,.0f} armé", className="v4-tr-pnl flat"),
        ],
        className="v4-tr",
    )


def build(acc: AccountConfig) -> list:
    snap = compute_all(acc)
    state = snap["state"]
    trades = snap["trades"]

    # Jour de trading futures courant (pivot 23:00 UTC)
    day_start_iso = trading_day_start().strftime("%Y-%m-%dT%H:%M:%S")
    today_trades = [t for t in trades if (t["fill_time"] or "") >= day_start_iso]
    day_pnl = sum(t["pnl_net"] for t in today_trades)

    # Exposition armée (positions RM + pendings avec risk_usd)
    pos = active_positions(state) if state else []
    pend = pending_orders(state) if state else []
    armed_total = sum(o["risk_usd"] for o in pos) + sum(o["risk_usd"] for o in pend)
    armed_rows = [_armed_row(o, "position") for o in pos] + [_armed_row(o, "pending") for o in pend]
    if not armed_rows:
        armed_rows = [html.Div("Aucune position ni ordre en attente", className="v4-empty")]

    broker = get_broker(acc)
    n_broker_pos = len(broker.positions() or []) if broker.account_summary() else None

    blocks = [
        c.card(
            [
                html.Div(
                    [
                        html.Span("P&L du jour (net)"),
                        html.Span(
                            c.fmt_usd(day_pnl), className=f"v4-day-pnl {c.pnl_class(day_pnl)}"
                        ),
                    ],
                    className="v4-day-head",
                ),
                (
                    c.trades_table(today_trades, limit=15)
                    if today_trades
                    else html.Div(
                        "Aucun trade clos sur le jour de trading courant", className="v4-empty"
                    )
                ),
            ],
            title="Aujourd'hui",
            icon="bi-sun",
        ),
        c.card(
            armed_rows
            + [
                html.Div(
                    f"Risque armé total : ${armed_total:,.0f}"
                    + (
                        f" · broker : {n_broker_pos} position(s)"
                        if n_broker_pos is not None
                        else ""
                    ),
                    className="v4-note",
                )
            ],
            title="En cours",
            icon="bi-hourglass-split",
        ),
        c.card(
            c.trades_table(trades, limit=20), title="Historique (20 derniers)", icon="bi-list-ul"
        ),
        c.card(c.graph(heatmap_calendar(trades)), title="Calendrier P&L", icon="bi-calendar3"),
    ]
    return blocks
