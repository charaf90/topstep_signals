"""Dashboard v4 — shell Dash. Mobile-first, bottom nav iOS, 5 onglets.

Lancement :
    python -m tools.dashboard_v4.app
ou (tmux always-on) :
    ./tools/launch_dashboard_v4.sh

Accès :
    Local      : http://localhost:8503
    Tailscale  : http://Katana17:8503  (iPhone — PWA « Ajouter à l'écran d'accueil »)

Cohabite avec le v3 (port 8502) pendant la transition. app.py ne calcule
RIEN : il assemble les builders de tabs/ (datasource/stats/health font le
travail, testables en CLI).
"""

from __future__ import annotations

import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path

import dash
from dash import Input, Output, State, ctx, dcc, html

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.dashboard_v4.accounts import ACCOUNTS, DEFAULT_ACCOUNT, get_account  # noqa: E402
from tools.dashboard_v4.broker import get_broker  # noqa: E402
from tools.dashboard_v4.tabs import portfolio, pulse, strats, system, trades  # noqa: E402

PORT = 8503
REFRESH_INTERVAL_MS = 30_000

TABS = [
    ("pulse", "Pulse", "bi-activity", pulse.build),
    ("portfolio", "Portefeuille", "bi-pie-chart-fill", portfolio.build),
    ("strats", "Stratégies", "bi-bullseye", strats.build),
    ("trades", "Trades", "bi-list-ul", trades.build),
    ("system", "Sys", "bi-heart-pulse", system.build),
]
_BUILDERS = {key: fn for key, _, _, fn in TABS}

app = dash.Dash(
    __name__,
    title="Topstep v4",
    update_title=None,
    suppress_callback_exceptions=True,  # ids présents selon l'onglet rendu
    meta_tags=[
        {
            "name": "viewport",
            "content": "width=device-width, initial-scale=1.0, viewport-fit=cover",
        },
        {"name": "theme-color", "content": "#000000"},
        {"name": "apple-mobile-web-app-capable", "content": "yes"},
        {"name": "mobile-web-app-capable", "content": "yes"},
        {"name": "apple-mobile-web-app-status-bar-style", "content": "black-translucent"},
        {"name": "apple-mobile-web-app-title", "content": "Topstep v4"},
    ],
)

app.index_string = """
<!DOCTYPE html>
<html>
<head>
    {%metas%}
    <title>{%title%}</title>
    {%favicon%}
    <link rel="manifest" href="/assets/manifest.json">
    <link rel="apple-touch-icon" href="/assets/icon-180.png">
    <link rel="icon" type="image/png" href="/assets/icon-192.png">
    {%css%}
</head>
<body>
    {%app_entry%}
    <footer>{%config%}{%scripts%}{%renderer%}</footer>
</body>
</html>
"""


def _header() -> html.Div:
    selector = []
    if len(ACCOUNTS) > 1:  # sélecteur multi-comptes — invisible tant qu'un seul compte
        selector = [
            dcc.Dropdown(
                id="account-select",
                options=[{"label": a.label, "value": k} for k, a in ACCOUNTS.items()],
                value=DEFAULT_ACCOUNT,
                clearable=False,
                className="v4-account-select",
            )
        ]
    return html.Div(
        [
            html.Div(
                [html.I(className="bi bi-lightning-charge-fill"), "Topstep", html.Em("v4")],
                className="v4-title",
            ),
            html.Div([*selector, html.Span(id="v4-clock", className="v4-subtitle")]),
        ],
        className="v4-header",
    )


def _nav() -> html.Div:
    return html.Div(
        [
            html.Button(
                [html.I(className=f"bi {icon}"), html.Span(label)],
                id=f"nav-{key}",
                n_clicks=0,
                className="v4-nav-btn active" if key == "pulse" else "v4-nav-btn",
            )
            for key, label, icon, _ in TABS
        ],
        className="v4-nav",
    )


app.layout = html.Div(
    [
        dcc.Store(id="account-key", data=DEFAULT_ACCOUNT),
        dcc.Store(id="active-tab", data="pulse"),
        dcc.Interval(id="tick", interval=REFRESH_INTERVAL_MS),
        html.Div([_header(), html.Div(id="tab-content")], className="v4-container"),
        _nav(),
    ]
)


@app.callback(
    Output("active-tab", "data"),
    *[Output(f"nav-{key}", "className") for key, _, _, _ in TABS],
    *[Input(f"nav-{key}", "n_clicks") for key, _, _, _ in TABS],
    State("active-tab", "data"),
    prevent_initial_call=True,
)
def switch_tab(*args):
    current = args[-1]
    clicked = ctx.triggered_id
    active = clicked.removeprefix("nav-") if clicked else current
    classes = ["v4-nav-btn active" if key == active else "v4-nav-btn" for key, _, _, _ in TABS]
    return (active, *classes)


@app.callback(
    Output("tab-content", "children"),
    Output("v4-clock", "children"),
    Input("active-tab", "data"),
    Input("tick", "n_intervals"),
    State("account-key", "data"),
)
def render_tab(active, _n, account_key):
    acc = get_account(account_key)
    clock = datetime.now(UTC).strftime("%H:%M UTC")
    builder = _BUILDERS.get(active, pulse.build)
    try:
        return builder(acc), clock
    except Exception:
        # UI dégradée plutôt qu'écran blanc — la trace part en console
        traceback.print_exc()
        return (
            html.Div(
                [
                    html.I(className="bi bi-exclamation-triangle"),
                    " Erreur de rendu — voir la console du dashboard",
                ],
                className="v4-alert",
            ),
            clock,
        )


@app.callback(
    Output("strat-detail", "children"),
    Input("strat-select", "value"),
    State("account-key", "data"),
)
def render_strat_detail(strat_key, account_key):
    try:
        return strats.detail(get_account(account_key), strat_key or "OPR")
    except Exception:
        traceback.print_exc()
        return html.Div("Erreur de rendu stratégie", className="v4-alert")


@app.callback(
    Output("refresh-feedback", "children"),
    Input("btn-refresh-broker", "n_clicks"),
    State("account-key", "data"),
    prevent_initial_call=True,
)
def refresh_broker(n_clicks, account_key):
    get_broker(get_account(account_key)).clear_cache()
    return f"cache broker invalidé à {datetime.now(UTC):%H:%M:%S} UTC"


if len(ACCOUNTS) > 1:

    @app.callback(Output("account-key", "data"), Input("account-select", "value"))
    def select_account(key):
        return key or DEFAULT_ACCOUNT


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
