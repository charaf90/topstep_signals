"""Dashboard v3 — Dash + Plotly, mobile-first, niveau pro.

Lancement :
    python -m tools.dashboard_v3.app
ou (avec wrapper tmux) :
    ./tools/launch_dashboard_v3.sh start

Accès :
    Local      : http://localhost:8502
    Tailscale  : http://Katana17:8502  (iPhone)

Pour cohabiter avec le v2 actuellement déployé sur 8501 (port différent).
"""

from __future__ import annotations

import sys
from pathlib import Path

import dash
import dash_bootstrap_components as dbc
import dash_daq as daq  # noqa: F401  (importé pour l'utilisation future)
from dash import Input, Output, dcc, html

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from config import (  # noqa: E402
    CHALLENGE_CONSISTENCY_BEST_DAY_MAX_USD,
    TOPSTEP_DAILY_LOSS_MAX,
    TOPSTEP_PROFIT_TARGET,
    TOPSTEP_TRAILING_DD,
    USER_DAILY_LOSS_MAX,
)
from tools.dashboard_v3 import charts as ch  # noqa: E402
from tools.dashboard_v3 import data as dt  # noqa: E402
from tools.dashboard_v3.broker import get_broker  # noqa: E402

REFRESH_INTERVAL_MS = 30_000

# Bootstrap dark theme + Inter font from Google Fonts
EXTERNAL_STYLESHEETS = [
    dbc.themes.DARKLY,
    "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap",
]

app = dash.Dash(
    __name__,
    external_stylesheets=EXTERNAL_STYLESHEETS,
    title="Topstep Live",
    update_title=None,  # supprime "Updating..." dans le title
    meta_tags=[
        {"name": "viewport", "content": "width=device-width, initial-scale=1.0"},
        {"name": "theme-color", "content": "#0e1117"},
    ],
)

# CSS custom — Inter font + variables + animations
app.index_string = """
<!DOCTYPE html>
<html>
<head>
    {%metas%}
    <title>{%title%}</title>
    {%favicon%}
    {%css%}
    <style>
        :root {
            --bg: #0e1117;
            --bg2: #1a1d24;
            --bg3: #252830;
            --green: #3ddc84;
            --red: #ff6b6b;
            --yellow: #ffb347;
            --blue: #5e9eff;
            --purple: #a78bfa;
            --grey: #6c757d;
            --grey-dim: #3a3d44;
            --text: #fafafa;
        }
        * { font-family: 'Inter', system-ui, sans-serif !important; }
        html, body, #react-entry-point {
            background: var(--bg) !important;
            color: var(--text) !important;
            margin: 0;
            padding: 0;
        }
        ._dash-undo-redo, .dash-debug-menu, .dash-debug-menu__outer { display: none !important; }

        /* Container responsive */
        .v3-container {
            max-width: 480px;
            margin: 0 auto;
            padding: 16px 14px;
        }

        /* Header */
        .v3-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 14px;
            animation: fadeIn 0.4s ease-out;
        }
        .v3-title {
            font-size: 22px;
            font-weight: 700;
            letter-spacing: 0.3px;
            background: linear-gradient(135deg, #fafafa, #b8b8b8);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        .v3-subtitle {
            font-size: 11px;
            color: var(--grey);
        }

        /* Metadata row */
        .v3-meta {
            display: flex;
            justify-content: space-between;
            padding: 8px 12px;
            background: var(--bg2);
            border-radius: 10px;
            font-size: 11px;
            color: var(--grey);
            margin-bottom: 14px;
        }
        .v3-meta b { color: var(--text); font-weight: 600; }

        /* KPI cards */
        .v3-kpi-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 10px;
            margin-bottom: 14px;
        }
        .v3-kpi {
            background: linear-gradient(135deg, var(--bg2), var(--bg3));
            border-radius: 14px;
            padding: 14px 16px;
            border-left: 3px solid var(--grey-dim);
            animation: slideUp 0.4s ease-out;
        }
        .v3-kpi.green { border-left-color: var(--green); }
        .v3-kpi.red { border-left-color: var(--red); }
        .v3-kpi.blue { border-left-color: var(--blue); }
        .v3-kpi.grey { border-left-color: var(--grey-dim); }
        .v3-kpi-label {
            font-size: 10px;
            text-transform: uppercase;
            letter-spacing: 1.2px;
            color: var(--grey);
            margin-bottom: 6px;
            font-weight: 500;
        }
        .v3-kpi-value {
            font-size: 30px;
            font-weight: 700;
            line-height: 1;
            margin-bottom: 4px;
        }
        .v3-kpi-value.green { color: var(--green); }
        .v3-kpi-value.red { color: var(--red); }
        .v3-kpi-value.blue { color: var(--blue); }
        .v3-kpi-value.grey { color: var(--grey); }
        .v3-kpi-sub {
            font-size: 11px;
            color: var(--grey);
            font-weight: 400;
        }

        /* Compare badges */
        .v3-badges {
            display: flex;
            gap: 8px;
            margin: 4px 0 14px 0;
            flex-wrap: wrap;
        }
        .v3-badge {
            display: inline-flex;
            align-items: center;
            gap: 4px;
            padding: 5px 10px;
            background: var(--bg2);
            border-radius: 999px;
            font-size: 11px;
            color: var(--grey);
            border: 1px solid var(--grey-dim);
        }
        .v3-badge b { color: var(--text); font-weight: 600; margin-left: 2px; }
        .v3-badge.green { color: var(--green); border-color: var(--green); }
        .v3-badge.red { color: var(--red); border-color: var(--red); }

        /* Section titles */
        .v3-section-title {
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 1.4px;
            color: var(--grey);
            margin: 18px 0 8px 0;
            font-weight: 500;
        }

        /* Gauges grid */
        .v3-gauges-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 8px;
        }

        /* Strats table */
        .v3-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
        }
        .v3-table th {
            text-align: left;
            font-size: 10px;
            text-transform: uppercase;
            letter-spacing: 1px;
            color: var(--grey);
            padding: 8px 6px;
            border-bottom: 1px solid var(--grey-dim);
            font-weight: 500;
        }
        .v3-table td {
            padding: 10px 6px;
            border-bottom: 1px solid var(--grey-dim);
            color: var(--text);
        }
        .v3-table td.pnl-pos { color: var(--green); font-weight: 600; }
        .v3-table td.pnl-neg { color: var(--red); font-weight: 600; }

        /* Tabs styling — FORCE horizontal layout */
        .dash-tabs,
        .dash-tabs > div,
        .dash-tabs-container {
            background: transparent !important;
            border: none !important;
            display: flex !important;
            flex-direction: row !important;
            justify-content: space-around !important;
            border-bottom: 1px solid var(--grey-dim) !important;
        }
        .dash-tab {
            flex: 1 1 auto !important;
            background: transparent !important;
            color: var(--grey) !important;
            border: none !important;
            border-bottom: 2px solid transparent !important;
            padding: 10px 6px !important;
            font-size: 13px !important;
            font-weight: 500 !important;
            transition: all 0.2s;
            text-align: center !important;
        }
        .dash-tab:hover { color: var(--text) !important; }
        .dash-tab--selected {
            color: var(--green) !important;
            border-bottom: 2px solid var(--green) !important;
            background: transparent !important;
        }

        /* Log code block */
        .v3-log {
            font-family: 'JetBrains Mono', 'Courier New', monospace !important;
            font-size: 10px;
            color: var(--grey);
            padding: 6px 8px;
            background: var(--bg2);
            border-radius: 6px;
            margin: 4px 0;
            border-left: 2px solid var(--grey-dim);
            overflow-x: auto;
            white-space: nowrap;
        }

        /* Animations */
        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(-4px); }
            to { opacity: 1; transform: translateY(0); }
        }
        @keyframes slideUp {
            from { opacity: 0; transform: translateY(8px); }
            to { opacity: 1; transform: translateY(0); }
        }

        /* Scrollbar custom */
        ::-webkit-scrollbar { width: 6px; }
        ::-webkit-scrollbar-track { background: var(--bg); }
        ::-webkit-scrollbar-thumb { background: var(--grey-dim); border-radius: 3px; }
    </style>
</head>
<body>
    {%app_entry%}
    <footer>
        {%config%}
        {%scripts%}
        {%renderer%}
    </footer>
</body>
</html>
"""


# ──────────────────────────────────────────────────────────────────────────────
# Helper components
# ──────────────────────────────────────────────────────────────────────────────


def kpi_card(label: str, value: str, subtitle: str, color: str = "grey") -> html.Div:
    return html.Div(
        [
            html.Div(label, className="v3-kpi-label"),
            html.Div(value, className=f"v3-kpi-value {color}"),
            html.Div(subtitle, className="v3-kpi-sub"),
        ],
        className=f"v3-kpi {color}",
    )


def badge(label: str, value: str, color: str = "") -> html.Span:
    cls = f"v3-badge {color}".strip()
    return html.Span([label, " ", html.B(value)], className=cls)


def section_title(text: str) -> html.Div:
    return html.Div(text, className="v3-section-title")


# ──────────────────────────────────────────────────────────────────────────────
# TAB CONTENT BUILDERS
# ──────────────────────────────────────────────────────────────────────────────


def build_pulse(state: dict, pnl: dict, trades: list[dict]) -> html.Div:
    age = dt.last_event_age_seconds(dt.tail_log(n=10))
    comp = dt.temporal_comparisons(trades, pnl)

    # SOURCE DE VÉRITÉ = API broker (balance + trades broker avec frais)
    broker = get_broker()
    acc = broker.account_summary()
    day = broker.realized_day_pnl() if acc else None

    # KPI 1 — P&L Jour
    if acc and day and day["n_total"] == 0:
        kpi_jour = kpi_card("P&L Jour", "$0", "Aucun trade aujourd'hui", "grey")
    elif acc and day:
        v = day["pnl_net"]
        color = "green" if v > 0 else ("red" if v < 0 else "grey")
        kpi_jour = kpi_card(
            "P&L Jour (broker)",
            f"${v:+,.0f}",
            f"{day['n_closing']} trades · frais ${day['fees']:.0f}",
            color,
        )
    elif pnl["stale_day"]:
        kpi_jour = kpi_card(
            "P&L Jour", "$0", f"pas de session · dernière {pnl['stale_day']}", "grey"
        )
    else:
        v = pnl["realized_day_pnl"]
        color = "green" if v > 0 else ("red" if v < 0 else "grey")
        kpi_jour = kpi_card(
            "P&L Jour", f"${v:+,.0f}", f"{pnl['daily_fills_count']} fills (local)", color
        )

    # KPI 2 — P&L Cumul (broker absolu)
    if acc:
        cum = acc["cum_pnl_net"]
        cum_color = "green" if cum > 0 else ("red" if cum < 0 else "grey")
        cum_sub = f"Balance ${acc['balance']:,.0f} · start ${acc['starting_balance']:,.0f}"
        kpi_cum = kpi_card("P&L Net (broker)", f"${cum:+,.0f}", cum_sub, cum_color)
    else:
        cum = pnl["cum_pnl"]
        cum_color = "green" if cum > 0 else ("red" if cum < 0 else "grey")
        cum_sub = f"Peak ${pnl['peak_pnl']:+,.0f} · state local (broker KO)"
        if pnl["active_drawdown"] > 0:
            cum_sub += f" · DD ${pnl['active_drawdown']:.0f}"
        kpi_cum = kpi_card("P&L Cumul Challenge", f"${cum:+,.0f}", cum_sub, cum_color)

    # Badges comparaisons temporelles
    badges = []
    if comp["yesterday_pnl"] != 0:
        delta = comp["delta_yesterday"]
        color = "green" if delta > 0 else "red" if delta < 0 else ""
        badges.append(badge("vs hier", f"${delta:+,.0f}", color))
    if comp["avg_7d"] != 0:
        badges.append(badge("avg 7j", f"${comp['avg_7d']:+,.0f}"))
    if comp["streak_win_days"] > 1:
        badges.append(badge("streak win", f"{comp['streak_win_days']}j", "green"))
    elif comp["streak_loss_days"] > 1:
        badges.append(badge("streak loss", f"{comp['streak_loss_days']}j", "red"))
    if comp["best_day"][0]:
        badges.append(badge("best day", f"${comp['best_day'][1]:+,.0f}"))

    # Gauges Topstep (radial speedometer) — utilise les valeurs broker si dispo
    if acc and day:
        rdp = day["pnl_net"]  # vrai P&L jour broker
        cum_for_gauges = acc["cum_pnl_net"]  # vrai cumul net
        peak_for_gauges = max(pnl["peak_pnl"], cum_for_gauges)  # peak >= cum
    else:
        rdp = pnl["realized_day_pnl"]
        cum_for_gauges = cum
        peak_for_gauges = pnl["peak_pnl"]
    gauges_data = [
        (f"DLL user ${USER_DAILY_LOSS_MAX}", max(0.0, -rdp), USER_DAILY_LOSS_MAX, "loss"),
        (f"DLL Topstep ${TOPSTEP_DAILY_LOSS_MAX}", max(0.0, -rdp), TOPSTEP_DAILY_LOSS_MAX, "loss"),
        (
            f"Trailing DD ${TOPSTEP_TRAILING_DD}",
            max(0.0, peak_for_gauges - cum_for_gauges),
            TOPSTEP_TRAILING_DD,
            "loss",
        ),
        (
            f"Consistency ${CHALLENGE_CONSISTENCY_BEST_DAY_MAX_USD}",
            max(0.0, rdp),
            CHALLENGE_CONSISTENCY_BEST_DAY_MAX_USD,
            "gain",
        ),
        (
            f"Profit target ${TOPSTEP_PROFIT_TARGET}",
            max(0.0, cum_for_gauges),
            TOPSTEP_PROFIT_TARGET,
            "gain",
        ),
    ]
    gauges = []
    for label, used, limit, kind in gauges_data:
        ratio = used / limit if limit > 0 else 0
        if kind == "loss":
            color = ch.RED if ratio > 0.8 else (ch.YELLOW if ratio > 0.5 else ch.GREEN)
        else:
            color = ch.GREEN if ratio > 0.5 else (ch.BLUE if ratio > 0.2 else ch.GREY)
        gauges.append(
            dcc.Graph(
                figure=ch.radial_gauge(used, limit, label, color, height=170),
                config={"displayModeBar": False, "staticPlot": False},
                style={"marginBottom": "4px"},
            )
        )

    return html.Div(
        [
            html.Div(
                [
                    html.Span([html.Span("Compte "), html.B(state.get("account_id", "?"))]),
                    html.Span([html.Span("State "), html.B(state.get("date", "?"))]),
                    html.Span([html.Span("WS "), html.B(dt.format_age(age))]),
                ],
                className="v3-meta",
            ),
            html.Div([kpi_jour, kpi_cum], className="v3-kpi-grid"),
            html.Div(badges, className="v3-badges") if badges else html.Div(),
            section_title("Limites Topstep"),
            html.Div(gauges, className="v3-gauges-grid"),
            section_title("Equity (broker net)" if acc else "Equity (state local)"),
            dcc.Graph(
                figure=_pulse_equity_chart(acc, broker, pnl, trades),
                config={"displayModeBar": False},
            ),
        ]
    )


def _pulse_equity_chart(acc, broker, pnl, trades):
    """Mini-chart equity pour le tab Pulse — broker si dispo, sinon state."""
    if acc:
        eq_data = broker.equity_curve_from_trades(days_back=60)
        if eq_data and eq_data["n_trades"] > 0:
            closing = [
                t for t in broker.trades_history(days_back=60) if t.get("profitAndLoss") is not None
            ]
            closing.sort(key=lambda t: t.get("creationTimestamp", ""))
            timestamps = [t.get("creationTimestamp", "") for t in closing]
            equity_net = []
            cum_eq = 0.0
            hover_trades = []
            for t in closing:
                pnl_net = (
                    float(t["profitAndLoss"])
                    - float(t.get("fees", 0) or 0)
                    - float(t.get("commissions", 0) or 0)
                )
                cum_eq += pnl_net
                equity_net.append(round(cum_eq, 2))
                hover_trades.append(
                    {
                        "ticker": (t.get("contractId", "?").split(".")[-2] or "?"),
                        "dir": "BUY" if t.get("side") == 0 else "SELL",
                        "n_ct": t.get("size", 0),
                        "close_pnl": pnl_net,
                    }
                )
            peak_net = max(equity_net) if equity_net else 0.0
            return ch.equity_curve_pro(timestamps, equity_net, hover_trades, peak_net)
    # Fallback state local
    timestamps, equity = dt.equity_series(trades)
    return ch.equity_curve_pro(timestamps, equity, trades, pnl["peak_pnl"])


def build_strats(trades: list[dict]) -> html.Div:
    """Stats par contrat broker (vraies données) + par stratégie (state local).

    Le broker n'a pas la notion de "stratégie" (OPR/FIB) mais il a le
    contrat exact (MNQ/MES/MYM/MGC). Le state local lui a la stratégie.
    On affiche les deux tables côte à côte.
    """
    broker = get_broker()
    acc = broker.account_summary()

    # Table 1 — Par CONTRAT BROKER (vrai P&L net avec frais)
    contract_section = []
    if acc:
        by_contract = broker.trades_aggregated_by_contract(days_back=60)
        if by_contract:
            # Tri par P&L décroissant pour mettre les gagnants en haut
            sorted_cids = sorted(by_contract.items(), key=lambda kv: -kv[1]["pnl_net"])

            # Mapping contractId → ticker court (CON.F.US.MNQ.M26 → NQ)
            def short_name(cid: str) -> str:
                parts = cid.split(".")
                if len(parts) >= 4:
                    s = parts[-2]  # MNQ / MES / MYM / MGC
                    return s[1:] if s.startswith("M") else s
                return cid[:5]

            rows = []
            for cid, d in sorted_cids:
                pnl_cls = "pnl-pos" if d["pnl_net"] >= 0 else "pnl-neg"
                avg_cls = "pnl-pos" if d["avg_pnl_net"] >= 0 else "pnl-neg"
                rows.append(
                    html.Tr(
                        [
                            html.Td(html.B(short_name(cid))),
                            html.Td(str(d["n"])),
                            html.Td(f"${d['pnl_net']:+,.0f}", className=pnl_cls),
                            html.Td(f"{d['wr_pct']:.0f}%"),
                            html.Td(f"${d['avg_pnl_net']:+,.0f}", className=avg_cls),
                            html.Td(f"${d['fees']:,.0f}", style={"color": ch.GREY}),
                        ]
                    )
                )
            contract_section = [
                section_title("Par contrat (broker, net)"),
                html.Table(
                    [
                        html.Thead(
                            html.Tr(
                                [
                                    html.Th("Ticker"),
                                    html.Th("n"),
                                    html.Th("P&L net"),
                                    html.Th("WR"),
                                    html.Th("Avg"),
                                    html.Th("Frais"),
                                ]
                            )
                        ),
                        html.Tbody(rows),
                    ],
                    className="v3-table",
                ),
                # Bar chart par contrat
                dcc.Graph(
                    figure=ch.strategy_bars(
                        {short_name(c): {"pnl": d["pnl_net"]} for c, d in by_contract.items()}
                    ),
                    config={"displayModeBar": False},
                ),
            ]

    # Table 2 — Par STRATÉGIE (state local — pour info stratégie OPR/FIB)
    strat_section = []
    stats = dt.strategy_stats(trades)
    if stats:
        rows = []
        for s in sorted(stats.keys()):
            d = stats[s]
            pnl_cls = "pnl-pos" if d["pnl"] >= 0 else "pnl-neg"
            avg_cls = "pnl-pos" if d["avg_pnl"] >= 0 else "pnl-neg"
            pf_str = "∞" if d["pf"] == float("inf") else f"{d['pf']:.2f}"
            rows.append(
                html.Tr(
                    [
                        html.Td(html.B(s)),
                        html.Td(str(d["n"])),
                        html.Td(f"${d['pnl']:+,.0f}", className=pnl_cls),
                        html.Td(f"{d['wr_pct']:.0f}%"),
                        html.Td(pf_str),
                        html.Td(f"${d['avg_pnl']:+,.0f}", className=avg_cls),
                    ]
                )
            )
        strat_section = [
            section_title("Par stratégie (state local, brut)"),
            html.Div(
                "Note : ne tient pas compte des frais broker — utilisez 'Par contrat' pour le net réel.",
                style={"color": ch.GREY, "fontSize": "10px", "marginBottom": "6px"},
            ),
            html.Table(
                [
                    html.Thead(
                        html.Tr(
                            [
                                html.Th("Strat"),
                                html.Th("n"),
                                html.Th("P&L brut"),
                                html.Th("WR"),
                                html.Th("PF"),
                                html.Th("Avg"),
                            ]
                        )
                    ),
                    html.Tbody(rows),
                ],
                className="v3-table",
            ),
        ]

    if not contract_section and not strat_section:
        return html.Div("Pas encore de trades.", style={"color": ch.GREY, "padding": "20px"})

    return html.Div(contract_section + strat_section)


def build_equity(trades: list[dict], pnl: dict) -> html.Div:
    """Equity curve broker-first.

    Source de vérité : trades broker (avec fees/commissions). Reconstruit
    la courbe à partir de api.get_trades_since avec les vrais P&L net.
    Fallback sur state local si broker indisponible.
    """
    broker = get_broker()
    acc = broker.account_summary()
    eq_data = broker.equity_curve_from_trades(days_back=60) if acc else None

    if eq_data and eq_data["n_trades"] > 0:
        # Construction des "pseudo-trades" pour le hover (1 par closing trade broker)
        broker_trades_raw = broker.trades_history(days_back=60)
        # Filtrer les closing trades (profitAndLoss not None) et trier
        closing = [t for t in broker_trades_raw if t.get("profitAndLoss") is not None]
        closing.sort(key=lambda t: t.get("creationTimestamp", ""))
        # Map vers le format attendu par equity_curve_pro
        timestamps = [t.get("creationTimestamp", "") for t in closing]
        equity_net = []
        cum = 0.0
        for t in closing:
            cum += (
                float(t["profitAndLoss"])
                - float(t.get("fees", 0) or 0)
                - float(t.get("commissions", 0) or 0)
            )
            equity_net.append(round(cum, 2))
        hover_trades = [
            {
                "ticker": (t.get("contractId", "?").split(".")[-2] or "?").replace("US/", "")[:4],
                "dir": "BUY" if t.get("side") == 0 else "SELL",
                "n_ct": t.get("size", 0),
                "close_pnl": float(t["profitAndLoss"])
                - float(t.get("fees", 0) or 0)
                - float(t.get("commissions", 0) or 0),
            }
            for t in closing
        ]
        peak_net = max(equity_net) if equity_net else 0.0
        peak_str = f"Peak ${peak_net:+,.0f}"
        note = (
            f"Source : <b>broker API</b> · {eq_data['n_trades']} trades · "
            f"P&L net <b>${eq_data['total_pnl_net']:+,.0f}</b> · "
            f"P&L brut ${eq_data['total_pnl_gross']:+,.0f} · "
            f"frais ${eq_data['total_fees']:,.0f}"
        )
        if acc:
            note += f" · Balance ${acc['balance']:,.0f}"

        return html.Div(
            [
                section_title("Equity nette (broker)"),
                html.Div(
                    dcc.Markdown(note, dangerously_allow_html=True),
                    style={"color": ch.GREY, "fontSize": "10px", "marginBottom": "8px"},
                ),
                dcc.Graph(
                    figure=ch.equity_curve_pro(timestamps, equity_net, hover_trades, peak_net),
                    config={"displayModeBar": False},
                ),
                section_title("Drawdown underwater"),
                dcc.Graph(
                    figure=ch.drawdown_underwater_pro(timestamps, equity_net),
                    config={"displayModeBar": False},
                ),
                section_title("Distribution P&L par trade (net)"),
                dcc.Graph(
                    figure=ch.pnl_distribution_pro(hover_trades),
                    config={"displayModeBar": False},
                ),
            ]
        )

    # Fallback state local
    timestamps, equity = dt.equity_series(trades)
    final_eq = equity[-1] if equity else 0.0
    note = (
        f"Source : <b>state local</b> (broker KO) · "
        f"Cum equity ${final_eq:+,.0f} · risk_state ${pnl['cum_pnl']:+,.0f}"
    )
    return html.Div(
        [
            section_title("Equity historique (fallback local)"),
            html.Div(
                dcc.Markdown(note, dangerously_allow_html=True),
                style={"color": ch.GREY, "fontSize": "10px", "marginBottom": "8px"},
            ),
            dcc.Graph(
                figure=ch.equity_curve_pro(timestamps, equity, trades, pnl["peak_pnl"]),
                config={"displayModeBar": False},
            ),
            section_title("Drawdown underwater"),
            dcc.Graph(
                figure=ch.drawdown_underwater_pro(timestamps, equity),
                config={"displayModeBar": False},
            ),
            section_title("Distribution P&L par trade"),
            dcc.Graph(figure=ch.pnl_distribution_pro(trades), config={"displayModeBar": False}),
        ]
    )


def build_trades(state: dict, trades: list[dict]) -> html.Div:
    today_str = dt.today_utc()
    broker = get_broker()
    acc = broker.account_summary()

    # Source de vérité = broker. Si broker OK, on liste les trades du jour
    # depuis l'API (avec PnL net). Fallback state local sinon.
    if acc:
        broker_trades = broker.trades_history(days_back=2)
        today_t = [
            {
                "strategy": "—",
                "ticker": (t.get("contractId", "?").split(".")[-2] or "?"),
                "dir": "BUY" if t.get("side") == 0 else "SELL",
                "n_ct": t.get("size", 0),
                "entry": t.get("price"),
                "exit": "",  # broker ne distingue pas dans get_trades
                "close_pnl": float(t.get("profitAndLoss") or 0)
                - float(t.get("fees", 0) or 0)
                - float(t.get("commissions", 0) or 0),
                "is_closing": t.get("profitAndLoss") is not None,
            }
            for t in broker_trades
            if (t.get("creationTimestamp") or "").startswith(today_str)
        ]
        positions = broker.positions()
        orders = broker.open_orders()
    else:
        # Fallback state local
        today_t = [
            {**t, "is_closing": True}
            for t in trades
            if (t["fill_time"] or "").startswith(today_str)
        ]
        positions = dt.open_positions(state)
        orders = dt.open_orders(state)

    today_section: list = []
    if not today_t:
        today_section.append(
            html.Div("Aucun trade clos aujourd'hui.", style={"color": ch.GREY, "padding": "8px 0"})
        )
    else:
        rows = []
        for t in today_t:
            pnl_cls = "pnl-pos" if t["close_pnl"] >= 0 else "pnl-neg"
            rows.append(
                html.Tr(
                    [
                        html.Td(t["strategy"]),
                        html.Td(t["ticker"]),
                        html.Td(t["dir"]),
                        html.Td(str(t["n_ct"])),
                        html.Td(str(t["entry"])),
                        html.Td(str(t["exit"])),
                        html.Td(f"${t['close_pnl']:+,.0f}", className=pnl_cls),
                    ]
                )
            )
        today_section.append(
            html.Table(
                [
                    html.Thead(
                        html.Tr(
                            [
                                html.Th("Strat"),
                                html.Th("Ticker"),
                                html.Th("Dir"),
                                html.Th("n"),
                                html.Th("Entry"),
                                html.Th("Exit"),
                                html.Th("P&L"),
                            ]
                        )
                    ),
                    html.Tbody(rows),
                ],
                className="v3-table",
            )
        )

    pos_orders_section: list = []
    if positions or orders:
        if positions:
            pos_orders_section.append(section_title(f"Positions ouvertes ({len(positions)})"))
            for p in positions[:5]:
                # Support broker (contractId/type/size) ET state local (ticker/direction/n_ct)
                ticker = p.get("ticker") or (p.get("contractId", "?").split(".")[-2])
                direction = p.get("direction") or ("LONG" if p.get("type") == 0 else "SHORT")
                n_ct = p.get("n_ct") or p.get("size", 0)
                entry = p.get("entry") or p.get("averagePrice", "?")
                sl_str = f"SL {p['sl']}" if "sl" in p else ""
                tp_str = f"TP {p['tp']}" if "tp" in p else ""
                pos_orders_section.append(
                    html.Div(
                        [
                            html.B(ticker),
                            html.Span(f" {direction} ×{n_ct} @ {entry}"),
                            html.Br(),
                            html.Small(
                                " · ".join(s for s in [sl_str, tp_str] if s) or "broker",
                                style={"color": ch.GREY},
                            ),
                        ],
                        style={"padding": "8px 0", "borderBottom": f"1px solid {ch.GREY_DIM}"},
                    )
                )
        if orders:
            pos_orders_section.append(section_title(f"Ordres ouverts ({len(orders)})"))
            for o in orders[:5]:
                ticker = o.get("ticker") or (o.get("contractId", "?").split(".")[-2])
                pos_orders_section.append(
                    html.Div(
                        [html.B(ticker), html.Span(f" · order {o.get('id') or o.get('order_id')}")],
                        style={"padding": "6px 0", "color": ch.GREY, "fontSize": "12px"},
                    )
                )

    return html.Div(
        [
            section_title(f"Trades aujourd'hui ({len(today_t)})"),
            *today_section,
            *pos_orders_section,
            section_title("Heatmap P&L journalier"),
            dcc.Graph(figure=ch.heatmap_github_style(trades), config={"displayModeBar": False}),
        ]
    )


def build_sys(state: dict) -> html.Div:
    log_lines = dt.tail_log(n=30)
    age = dt.last_event_age_seconds(log_lines)

    shadow = dt.read_state(dt.SHADOW_STATE_PATH)
    shadow_section: list = []
    if shadow is None:
        shadow_section.append(
            html.Div("Shadow runner non démarré.", style={"color": ch.GREY, "padding": "8px 0"})
        )
    else:
        live_tags = set(state.get("placed_tags", {}))
        shadow_tags = set(shadow.get("placed_tags", {}))
        common = live_tags & shadow_tags
        only_live = live_tags - shadow_tags
        only_shadow = shadow_tags - live_tags
        shadow_section.append(
            html.Div(
                [
                    kpi_card("Tags communs", str(len(common)), "", "green"),
                    kpi_card(
                        "Unique live", str(len(only_live)), "", "blue" if only_live else "grey"
                    ),
                    kpi_card(
                        "Unique shadow",
                        str(len(only_shadow)),
                        "",
                        "blue" if only_shadow else "grey",
                    ),
                ],
                style={"display": "grid", "gridTemplateColumns": "1fr 1fr 1fr", "gap": "8px"},
            )
        )

    return html.Div(
        [
            html.Div(
                [
                    kpi_card("Dernier événement", dt.format_age(age), "Heure WS", "blue"),
                    kpi_card(
                        "UTC maintenant",
                        __import__("datetime")
                        .datetime.now(__import__("datetime").UTC)
                        .strftime("%H:%M:%S"),
                        "",
                        "grey",
                    ),
                ],
                className="v3-kpi-grid",
            ),
            section_title("Shadow vs Live"),
            *shadow_section,
            section_title("Logs récents"),
            html.Div(
                [
                    html.Div(line, className="v3-log")
                    for line in reversed([line for line in log_lines if line.strip()][-8:])
                ]
            ),
        ]
    )


# ──────────────────────────────────────────────────────────────────────────────
# Layout
# ──────────────────────────────────────────────────────────────────────────────


app.layout = html.Div(
    [
        dcc.Interval(id="refresh", interval=REFRESH_INTERVAL_MS, n_intervals=0),
        html.Div(
            [
                html.Div(
                    [
                        html.Div("Topstep Live", className="v3-title"),
                        html.Div("v3 · refresh 30s", className="v3-subtitle"),
                    ],
                    className="v3-header",
                ),
                dcc.Tabs(
                    id="tabs",
                    value="pulse",
                    children=[
                        dcc.Tab(
                            label="Pulse",
                            value="pulse",
                            className="dash-tab",
                            selected_className="dash-tab--selected",
                        ),
                        dcc.Tab(
                            label="Strats",
                            value="strats",
                            className="dash-tab",
                            selected_className="dash-tab--selected",
                        ),
                        dcc.Tab(
                            label="Equity",
                            value="equity",
                            className="dash-tab",
                            selected_className="dash-tab--selected",
                        ),
                        dcc.Tab(
                            label="Trades",
                            value="trades",
                            className="dash-tab",
                            selected_className="dash-tab--selected",
                        ),
                        dcc.Tab(
                            label="Sys",
                            value="sys",
                            className="dash-tab",
                            selected_className="dash-tab--selected",
                        ),
                    ],
                    className="dash-tabs",
                ),
                html.Div(id="tab-content", style={"marginTop": "16px"}),
            ],
            className="v3-container",
        ),
    ]
)


@app.callback(
    Output("tab-content", "children"),
    Input("tabs", "value"),
    Input("refresh", "n_intervals"),
)
def render_tab(active_tab: str, _n: int) -> html.Div:
    state = dt.read_state()
    if state is None:
        return html.Div(
            "state/live_state.json introuvable", style={"color": ch.RED, "padding": "20px"}
        )
    pnl = dt.authoritative_pnl(state)
    trades = dt.chronological_trades(state)

    if active_tab == "strats":
        return build_strats(trades)
    if active_tab == "equity":
        return build_equity(trades, pnl)
    if active_tab == "trades":
        return build_trades(state, trades)
    if active_tab == "sys":
        return build_sys(state)
    return build_pulse(state, pnl, trades)


def main():
    # Pré-chargement broker (login + 1er fetch) pour éviter le fallback au 1er render
    print("[v3] Pré-chargement broker ProjectX...")
    broker = get_broker()
    acc = broker.account_summary()
    if acc:
        print(
            f"[v3] Broker OK : {acc['name']} · balance ${acc['balance']:,.2f} · P&L ${acc['cum_pnl_net']:+,.2f}"
        )
    else:
        print(f"[v3] Broker KO : {broker.last_error} → fallback state local")
    # Pré-fetch trades pour avoir le cache prêt au 1er render
    if acc:
        n = len(broker.trades_history(days_back=60))
        print(f"[v3] {n} trades chargés depuis broker (60 jours)")

    app.run(host="0.0.0.0", port=8502, debug=False)


if __name__ == "__main__":
    main()
