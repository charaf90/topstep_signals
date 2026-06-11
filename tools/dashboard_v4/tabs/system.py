"""Onglet SYS — santé daemon/flux, divergence local↔broker, logs récents.

Seule « action » du dashboard : « Rafraîchir broker » (invalide le cache TTL —
read-only par nature). Aucune action trading.
"""

from __future__ import annotations

from dash import html

from tools.dashboard_v4 import components as c
from tools.dashboard_v4 import health as he
from tools.dashboard_v4.accounts import AccountConfig
from tools.dashboard_v4.broker import get_broker
from tools.dashboard_v4.datasource import (
    authoritative_pnl,
    format_age,
    last_event_age_seconds,
    read_state,
    tail_log,
)
from tools.dashboard_v4.events import last_error, read_events


def build(acc: AccountConfig) -> list:
    state = read_state(acc)
    daemon = he.daemon_status(acc)
    log_fresh = he.daemon_log_freshness(acc)
    events = read_events(acc.events_log)
    err = last_error(events)
    ws_age = last_event_age_seconds(tail_log(acc.events_log, 5))

    broker = get_broker(acc)
    summary = broker.account_summary()
    day_broker = broker.day_net_futures()["pnl_net"] if summary else None
    pnl_local = authoritative_pnl(state) if state else None
    div = he.divergence(
        state, summary, pnl_local["realized_day_pnl"] if pnl_local else 0.0, day_broker
    )

    # Cartes santé
    health_cards = html.Div(
        [
            c.kpi_card(
                "Daemon",
                "VIVANT" if daemon["alive"] else "MORT",
                sub=daemon["detail"],
                color="green" if daemon["alive"] else "red",
            ),
            c.kpi_card(
                "Log daemon",
                format_age(log_fresh["age_s"]),
                sub=log_fresh["path"] or "aucun log",
                color="green" if (log_fresh["age_s"] or 9e9) < 300 else "orange",
            ),
            c.kpi_card(
                "Dernier événement",
                format_age(ws_age),
                sub="trading_events.log",
                color="green" if (ws_age or 9e9) < 7200 else "orange",
            ),
            c.kpi_card(
                "Dernière erreur",
                err["ts"][5:16] if err else "aucune",
                sub=(
                    (err["msg"][:42] + "…")
                    if err and len(err["msg"]) > 42
                    else (err["msg"] if err else None)
                ),
                color="orange" if err else "green",
            ),
        ],
        className="v4-kpi-grid",
    )

    # Divergence détaillée
    if div.get("available"):
        delta_day_txt = c.fmt_usd(div["delta_day"]) if div["delta_day"] is not None else "—"
        div_block = html.Div(
            [
                c.kpi_card(
                    "Cumul local vs broker",
                    c.fmt_usd(div["delta_cum"]),
                    sub=f"local {c.fmt_usd(div['cum_local'])} · "
                    f"broker {c.fmt_usd(div['cum_broker'])}",
                    color="green" if div["ok"] else "orange",
                ),
                c.kpi_card(
                    "Jour local vs broker",
                    delta_day_txt,
                    sub="écart ≈ fees : le RM compte le brut",
                    color="green" if abs(div["delta_day"] or 0) < 50 else "orange",
                ),
            ],
            className="v4-kpi-grid two",
        )
    else:
        div_block = html.Div(
            f"Broker injoignable — {broker.last_error or 'cause inconnue'}",
            className="v4-alert",
        )

    refresh = html.Div(
        [
            html.Button(
                [html.I(className="bi bi-arrow-clockwise"), " Rafraîchir broker"],
                id="btn-refresh-broker",
                className="v4-btn",
                n_clicks=0,
            ),
            html.Span(id="refresh-feedback", className="v4-note"),
        ],
        className="v4-refresh-row",
    )

    blocks = [
        c.card(health_cards, title="Santé système", icon="bi-heart-pulse"),
        c.card([div_block, refresh], title="Compta locale ↔ broker", icon="bi-arrow-left-right"),
        c.card(
            c.log_lines(tail_log(acc.events_log, 12)),
            title="Événements trading",
            icon="bi-journal-text",
        ),
        c.card(c.log_lines(he.tail_daemon_log(acc, 12)), title="Log daemon", icon="bi-terminal"),
    ]
    return blocks
