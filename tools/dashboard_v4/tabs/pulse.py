"""Onglet PULSE — l'essentiel en 5 secondes.

Bandeau santé · hero P&L challenge (broker-first) · marges Topstep ·
portefeuille express (P&L + sparkline + verdict sizing par stratégie).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from dash import html

from tools.dashboard_v4 import components as c
from tools.dashboard_v4 import health as he
from tools.dashboard_v4.accounts import AccountConfig
from tools.dashboard_v4.broker import get_broker
from tools.dashboard_v4.charts import STRAT_COLORS, sparkline
from tools.dashboard_v4.datasource import (
    authoritative_pnl,
    format_age,
    last_event_age_seconds,
    portfolio_status,
    risk_margins,
    tail_log,
)
from tools.dashboard_v4.events import read_events
from tools.dashboard_v4.stats import compute_all


def _sparkline_values(trades: list[dict], strat: str, days: int = 14) -> list[float]:
    cutoff = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
    cum = 0.0
    out = [0.0]
    for t in trades:
        if t["strategy"] != strat or (t["fill_time"] or "")[:10] < cutoff:
            continue
        cum += t["pnl_net"]
        out.append(round(cum, 2))
    return out


def _health_strip(
    acc: AccountConfig, state, broker_summary, day_local: float, day_broker: float | None
) -> html.Div:
    daemon = he.daemon_status(acc)
    ws_age = last_event_age_seconds(tail_log(acc.events_log, 5))
    div = he.divergence(state, broker_summary, day_local, day_broker)
    items = [
        html.Span([c.health_dot(daemon["alive"]), "daemon" if daemon["alive"] else "daemon OFF"]),
        html.Span([html.I(className="bi bi-broadcast"), f" flux {format_age(ws_age)}"]),
    ]
    if div.get("available"):
        delta = div["delta_cum"]
        cls = "ok" if div["ok"] else "warn"
        items.append(
            html.Span(
                [html.I(className="bi bi-arrow-left-right"), f" Δ broker ${delta:+,.0f}"],
                className=cls,
            )
        )
    else:
        items.append(
            html.Span([html.I(className="bi bi-cloud-slash"), " broker —"], className="warn")
        )
    return html.Div(items, className="v4-health-strip")


def _hero(
    acc: AccountConfig, broker_summary, pnl_local, day_net: float, n_closings: int, source: str
) -> html.Div:
    if broker_summary:
        cum = broker_summary["cum_pnl_net"]
        balance = broker_summary["balance"]
        sub = f"Balance ${balance:,.2f} · {source}"
    else:
        cum = pnl_local["cum_pnl"]
        sub = "⚠️ broker injoignable — compta locale (brut, sans fees)"
    target = acc.profit_target
    progress = max(0.0, min(1.0, cum / target)) if target > 0 else 0.0
    remaining = max(0.0, target - cum)
    return html.Div(
        [
            html.Div(
                [html.I(className="bi bi-trophy"), html.Span(f"CHALLENGE {acc.label.upper()}")],
                className="v4-hero-label",
            ),
            html.Div(c.fmt_usd(cum), className=f"v4-hero-value {c.pnl_class(cum)}"),
            html.Div(
                className="v4-progress-track",
                children=html.Div(
                    className="v4-progress-fill", style={"width": f"{progress * 100:.1f}%"}
                ),
            ),
            html.Div(
                [
                    html.Span([html.B(f"${remaining:,.0f}"), f" → target ${target:,.0f}"]),
                    html.Span([html.B(f"{progress * 100:.0f}%")]),
                ],
                className="v4-progress-cap",
            ),
            html.Div(
                [
                    html.Span(
                        [
                            "Aujourd'hui ",
                            html.B(c.fmt_usd(day_net), className=c.pnl_class(day_net)),
                            f" · {n_closings} clôture(s)",
                        ]
                    ),
                ],
                className="v4-hero-meta",
            ),
            html.Div(sub, className="v4-hero-sub"),
        ],
        className="v4-hero",
    )


def _express_row(
    p: dict, agg: dict | None, verdict: dict | None, spark_values: list[float]
) -> html.Div:
    pnl = agg["pnl_net"] if agg else 0.0
    color = STRAT_COLORS.get(p["key"], "#8E8E93")
    return html.Div(
        [
            html.Div(
                [
                    html.Div(
                        [
                            c.strat_dot(p["key"]),
                            c.strat_name(p["key"]),
                            c.pill(p["status"], p["color"]),
                        ],
                        className="v4-express-id",
                    ),
                    html.Div(
                        f"{p['universe']} · {p['version']} · ${p['sizing']}",
                        className="v4-express-meta",
                    ),
                ],
                className="v4-express-left",
            ),
            html.Div(
                c.graph(sparkline(spark_values, color), style={"height": "38px", "width": "86px"}),
                className="v4-express-spark",
            ),
            html.Div(
                [
                    html.Div(c.fmt_usd(pnl), className=f"v4-express-pnl {c.pnl_class(pnl)}"),
                    c.verdict_badge(verdict, mini=True) if verdict else html.Span(),
                ],
                className="v4-express-right",
            ),
        ],
        className="v4-express-row",
    )


def build(acc: AccountConfig) -> list:
    snap = compute_all(acc)
    state = snap["state"]
    pnl_local = (
        authoritative_pnl(state)
        if state
        else {
            "cum_pnl": 0.0,
            "peak_pnl": 0.0,
            "realized_day_pnl": 0.0,
            "active_drawdown": 0.0,
            "consec_loss_days": 0,
            "daily_fills_count": 0,
            "stale_day": None,
        }
    )

    broker = get_broker(acc)
    broker_summary = broker.account_summary()
    day_broker = None
    n_closings = 0
    if broker_summary:
        day = broker.day_net_futures()
        day_broker = day["pnl_net"]
        n_closings = day["n_closing"]
    day_net = day_broker if day_broker is not None else pnl_local["realized_day_pnl"]
    source = "vérité broker" if broker_summary else "state local"

    # Marges Topstep — scalaires broker-first, peak local pour le trailing
    cum_now = broker_summary["cum_pnl_net"] if broker_summary else pnl_local["cum_pnl"]
    peak = max(pnl_local["peak_pnl"], cum_now)
    daily = broker.daily_net_pnl() if broker_summary else snap["portfolio"]["daily"]
    best_day = max(daily.values()) if daily else 0.0
    margins = risk_margins(acc, day_net, peak - cum_now, best_day)

    # Express portefeuille
    statuses = portfolio_status(state)
    express = [
        _express_row(
            p,
            snap["strat_aggs"].get(p["key"]),
            snap["verdicts"].get(p["key"]),
            _sparkline_values(snap["trades"], p["key"]),
        )
        for p in statuses
    ]

    read_events(acc.events_log)  # warm le cache events pour les autres onglets

    return [
        _health_strip(acc, state, broker_summary, pnl_local["cum_pnl"], day_broker),
        _hero(acc, broker_summary, pnl_local, day_net, n_closings, source),
        c.card([c.risk_bar(m) for m in margins], title="Marges Topstep", icon="bi-shield-check"),
        c.card(express, title="Portefeuille", icon="bi-collection"),
    ]
