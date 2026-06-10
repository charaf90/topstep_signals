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

from config import TOPSTEP_PROFIT_TARGET  # noqa: E402
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
        {
            "name": "viewport",
            "content": "width=device-width, initial-scale=1.0, viewport-fit=cover",
        },
        {"name": "theme-color", "content": "#000000"},
        # PWA iPhone — plein écran « Ajouter à l'écran d'accueil »
        {"name": "apple-mobile-web-app-capable", "content": "yes"},
        {"name": "mobile-web-app-capable", "content": "yes"},
        {"name": "apple-mobile-web-app-status-bar-style", "content": "black-translucent"},
        {"name": "apple-mobile-web-app-title", "content": "Topstep"},
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
    <link rel="manifest" href="/assets/manifest.json">
    <link rel="apple-touch-icon" href="/assets/icon-180.png">
    <link rel="icon" type="image/png" href="/assets/icon-192.png">
    {%css%}
    <style>
        :root {
            /* iOS Dark Mode system colors */
            --bg: #000000;
            --bg2: #1C1C1E;       /* iOS secondary system bg */
            --bg3: #2C2C2E;       /* iOS tertiary system bg */
            --green: #34C759;     /* iOS green */
            --red: #FF3B30;       /* iOS red */
            --orange: #FF9500;    /* iOS orange */
            --yellow: #FFCC00;    /* iOS yellow */
            --blue: #007AFF;      /* iOS blue */
            --indigo: #5856D6;    /* iOS indigo */
            --purple: #AF52DE;    /* iOS purple */
            --pink: #FF2D55;      /* iOS pink */
            --teal: #5AC8FA;      /* iOS teal */
            --grey: #8E8E93;      /* iOS gray */
            --grey-dim: #3A3A3C;
            --text: #FFFFFF;
            --text-secondary: rgba(235, 235, 245, 0.6);
            --ease-ios: cubic-bezier(0.25, 0.1, 0.25, 1);
        }
        * {
            font-family: -apple-system, "SF Pro Display", "SF Pro Text", Inter, system-ui, sans-serif !important;
            -webkit-font-smoothing: antialiased;
        }
        html, body, #react-entry-point {
            background: var(--bg) !important;
            color: var(--text) !important;
            margin: 0;
            padding: 0;
        }
        ._dash-undo-redo, .dash-debug-menu, .dash-debug-menu__outer { display: none !important; }

        /* Container responsive (iOS-style centered) + safe-area notch */
        .v3-container {
            max-width: 480px;
            margin: 0 auto;
            padding: max(20px, env(safe-area-inset-top)) max(16px, env(safe-area-inset-right))
                     max(32px, env(safe-area-inset-bottom)) max(16px, env(safe-area-inset-left));
        }

        /* Icônes monochromes (Bootstrap Icons vendorisé dans assets/) */
        .bi { display: inline-block; line-height: 1; vertical-align: -1.5px; }

        /* Header iOS Large Title */
        .v3-header {
            display: flex;
            justify-content: space-between;
            align-items: flex-end;
            margin-bottom: 22px;
            animation: fadeIn 0.5s var(--ease-ios);
        }
        .v3-title {
            font-size: 32px;       /* Apple Large Title */
            font-weight: 700;
            letter-spacing: -0.5px; /* Apple negative tracking */
            line-height: 1;
            color: var(--text);
            display: flex;
            align-items: center;
            gap: 9px;
        }
        .v3-title .bi { font-size: 26px; color: var(--green); }
        .v3-subtitle {
            font-size: 12px;
            color: var(--text-secondary);
            font-weight: 500;
        }

        /* Metadata row — iOS rounded inset */
        .v3-meta {
            display: flex;
            justify-content: space-between;
            padding: 12px 14px;
            background: var(--bg2);
            border-radius: 14px;        /* iOS 14px continuous radius */
            font-size: 12px;
            color: var(--text-secondary);
            margin-bottom: 18px;
            animation: slideUp 0.5s var(--ease-ios);
        }
        .v3-meta b { color: var(--text); font-weight: 600; }

        /* ── HERO challenge — gros chiffre net + progression objectif ─────── */
        .v3-hero {
            background: var(--bg2);
            border-radius: 22px;
            padding: 22px 20px 20px 20px;
            margin-bottom: 14px;
            animation: slideUp 0.5s var(--ease-ios) backwards;
        }
        .v3-hero-label {
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 0.8px;
            color: var(--text-secondary);
            font-weight: 600;
            margin-bottom: 8px;
            display: flex;
            align-items: center;
            gap: 7px;
        }
        .v3-hero-value {
            font-size: 46px;
            font-weight: 700;
            letter-spacing: -2px;
            line-height: 1;
            margin-bottom: 16px;
        }
        .v3-hero-value.green { color: var(--green); }
        .v3-hero-value.red   { color: var(--red); }
        .v3-hero-value.grey  { color: var(--text-secondary); }
        .v3-hero-meta {
            font-size: 12px;
            color: var(--text-secondary);
            font-weight: 500;
            margin-top: 12px;
        }
        .v3-hero-meta b { color: var(--text); font-weight: 600; }

        /* Progress bar (objectif profit) */
        .v3-progress-track {
            height: 8px;
            border-radius: 999px;
            background: var(--bg3);
            overflow: hidden;
        }
        .v3-progress-fill {
            height: 100%;
            border-radius: 999px;
            background: var(--green);
            transition: width 0.6s var(--ease-ios);
        }
        .v3-progress-cap {
            display: flex;
            justify-content: space-between;
            font-size: 11px;
            color: var(--text-secondary);
            margin-top: 7px;
            font-weight: 500;
        }
        .v3-progress-cap b { color: var(--text); font-weight: 600; }

        /* ── Panneau Sécurité — barres de marge ───────────────────────────── */
        .v3-risk {
            background: var(--bg2);
            border-radius: 16px;
            padding: 16px 16px 6px 16px;
        }
        .v3-risk-row { margin-bottom: 16px; }
        .v3-risk-head {
            display: flex;
            justify-content: space-between;
            align-items: baseline;
            margin-bottom: 7px;
        }
        .v3-risk-label { font-size: 13px; font-weight: 600; color: var(--text); }
        .v3-risk-label .sub { font-size: 11px; color: var(--text-secondary); font-weight: 500; margin-left: 6px; }
        .v3-risk-headroom { font-size: 14px; font-weight: 700; letter-spacing: -0.3px; }
        .v3-risk-track {
            position: relative;
            height: 8px;
            border-radius: 999px;
            background: var(--bg3);
            overflow: hidden;
        }
        .v3-risk-fill {
            position: absolute;
            left: 0; top: 0; bottom: 0;
            border-radius: 999px;
            transition: width 0.6s var(--ease-ios);
        }
        .v3-risk-mark {
            position: absolute;
            top: -3px; bottom: -3px;
            width: 2px;
            background: var(--text);
            opacity: 0.55;
        }

        /* ── Portefeuille — stratégies + flags statut ─────────────────────── */
        .v3-portfolio {
            background: var(--bg2);
            border-radius: 14px;
            overflow: hidden;
        }
        .v3-strat-row {
            display: flex;
            align-items: center;
            gap: 11px;
            padding: 13px 14px;
            border-top: 0.5px solid var(--grey-dim);
        }
        .v3-strat-row:first-child { border-top: none; }
        .v3-strat-dot { width: 8px; height: 8px; border-radius: 50%; flex: 0 0 auto; }
        .v3-strat-dot.green { background: var(--green); }
        .v3-strat-dot.blue  { background: var(--blue); }
        .v3-strat-dot.grey  { background: var(--grey-dim); }
        .v3-strat-body { min-width: 0; }
        .v3-strat-name { font-weight: 600; font-size: 14px; color: var(--text); }
        .v3-strat-sub { font-size: 11px; color: var(--text-secondary); font-weight: 500; }
        .v3-status-pill {
            margin-left: auto;
            font-size: 10px;
            font-weight: 700;
            letter-spacing: 0.5px;
            padding: 4px 9px;
            border-radius: 999px;
            flex: 0 0 auto;
        }
        .v3-status-pill.green { color: var(--green); background: rgba(52, 199, 89, 0.15); }
        .v3-status-pill.blue  { color: var(--blue);  background: rgba(0, 122, 255, 0.15); }
        .v3-status-pill.grey  { color: var(--text-secondary); background: var(--bg3); }

        /* KPI cards — iOS style avec glow subtil */
        .v3-kpi-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 12px;
            margin-bottom: 18px;
        }
        .v3-kpi {
            background: var(--bg2);
            border-radius: 18px;
            padding: 18px 18px 16px 18px;
            position: relative;
            overflow: hidden;
            animation: slideUp 0.5s var(--ease-ios) backwards;
        }
        /* Glow d'accent en haut au lieu de border-left rigide */
        .v3-kpi::before {
            content: "";
            position: absolute;
            top: 0; left: 0; right: 0;
            height: 2px;
            background: var(--grey-dim);
            opacity: 0.5;
        }
        .v3-kpi.green::before { background: var(--green); }
        .v3-kpi.red::before   { background: var(--red); }
        .v3-kpi.blue::before  { background: var(--blue); }
        .v3-kpi.orange::before{ background: var(--orange); }
        .v3-kpi.grey::before  { background: var(--grey-dim); }

        .v3-kpi-label {
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 0.8px;
            color: var(--text-secondary);
            margin-bottom: 8px;
            font-weight: 600;
        }
        .v3-kpi-value {
            font-size: 34px;       /* gros chiffre Apple */
            font-weight: 700;
            letter-spacing: -1px;  /* tracking serré */
            line-height: 1;
            margin-bottom: 6px;
        }
        .v3-kpi-value.green  { color: var(--green); }
        .v3-kpi-value.red    { color: var(--red); }
        .v3-kpi-value.blue   { color: var(--blue); }
        .v3-kpi-value.orange { color: var(--orange); }
        .v3-kpi-value.grey   { color: var(--text-secondary); }
        .v3-kpi-sub {
            font-size: 12px;
            color: var(--text-secondary);
            font-weight: 500;
        }

        /* Compare badges — pill iOS */
        .v3-badges {
            display: flex;
            gap: 6px;
            margin: 6px 0 18px 0;
            flex-wrap: wrap;
        }
        .v3-badge {
            display: inline-flex;
            align-items: center;
            gap: 4px;
            padding: 6px 12px;
            background: var(--bg2);
            border-radius: 999px;
            font-size: 12px;
            color: var(--text-secondary);
            font-weight: 500;
        }
        .v3-badge b { color: var(--text); font-weight: 600; margin-left: 2px; }
        .v3-badge.green { color: var(--green); background: rgba(52, 199, 89, 0.15); }
        .v3-badge.red   { color: var(--red); background: rgba(255, 59, 48, 0.15); }
        .v3-badge.blue  { color: var(--blue); background: rgba(0, 122, 255, 0.15); }

        /* Section titles — Apple style header */
        .v3-section-title {
            font-size: 13px;
            text-transform: none;        /* Apple : pas tout en majuscules */
            letter-spacing: -0.2px;
            color: var(--text-secondary);
            margin: 24px 0 10px 4px;
            font-weight: 600;
        }
        .v3-section-title .emoji {
            margin-right: 6px;
            font-size: 15px;
        }
        .v3-section-title .bi {
            margin-right: 7px;
            font-size: 14px;
            opacity: 0.85;
        }

        /* Gauges grid — espacement plus généreux */
        .v3-gauges-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 10px;
        }
        .v3-gauges-grid > div {
            background: var(--bg2);
            border-radius: 16px;
            overflow: hidden;
            animation: slideUp 0.5s var(--ease-ios) backwards;
        }

        /* Strats table — iOS Inset Grouped style */
        .v3-table {
            width: 100%;
            border-collapse: separate;
            border-spacing: 0;
            font-size: 14px;
            background: var(--bg2);
            border-radius: 14px;
            overflow: hidden;
        }
        .v3-table th {
            text-align: left;
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 0.8px;
            color: var(--text-secondary);
            padding: 12px 12px 8px 12px;
            background: var(--bg2);
            font-weight: 600;
        }
        .v3-table td {
            padding: 12px;
            border-top: 0.5px solid var(--grey-dim);   /* separator iOS */
            color: var(--text);
            font-weight: 500;
        }
        .v3-table tr:first-child td { border-top: none; }
        .v3-table td.pnl-pos { color: var(--green); font-weight: 600; }
        .v3-table td.pnl-neg { color: var(--red); font-weight: 600; }

        /* Tabs styling — pill style Apple Segmented Control */
        .dash-tabs,
        .dash-tabs > div,
        .dash-tabs-container {
            background: var(--bg2) !important;
            border: none !important;
            border-radius: 10px !important;
            padding: 3px !important;
            display: flex !important;
            flex-direction: row !important;
            justify-content: space-between !important;
            margin-bottom: 8px !important;
        }
        .dash-tab {
            flex: 1 1 auto !important;
            background: transparent !important;
            color: var(--text-secondary) !important;
            border: none !important;
            border-radius: 7px !important;
            padding: 8px 4px !important;
            font-size: 12px !important;
            font-weight: 600 !important;
            transition: all 0.25s var(--ease-ios);
            text-align: center !important;
        }
        .dash-tab:hover { color: var(--text) !important; }
        .dash-tab--selected {
            color: var(--text) !important;
            background: var(--bg3) !important;        /* fond actif */
            border: none !important;
            box-shadow: 0 1px 2px rgba(0,0,0,0.4);
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


def icon(name: str, color: str | None = None, size: int | None = None) -> html.I:
    """Glyphe monochrome (Bootstrap Icons vendorisé). Ex: icon('graph-up-arrow')."""
    style: dict = {}
    if color:
        style["color"] = color
    if size:
        style["fontSize"] = f"{size}px"
    return html.I(className=f"bi bi-{name}", style=style or None)


def section_title(text: str, icon_name: str = "") -> html.Div:
    """Section title style Apple — icône monochrome optionnelle devant."""
    if icon_name:
        return html.Div([icon(icon_name), text], className="v3-section-title")
    return html.Div(text, className="v3-section-title")


def risk_bar(margin: dict) -> html.Div:
    """Barre de marge : label + reste $ dominant + piste avec seuil soft.

    `margin` provient de dt.risk_margins() : used/soft_limit/track_max/headroom/
    fill_ratio/soft_ratio/color_ratio/over.
    """
    cr = margin["color_ratio"]
    if margin["over"] or cr >= 0.8:
        color = ch.RED
    elif cr >= 0.5:
        color = ch.YELLOW
    else:
        color = ch.GREEN

    if margin["over"]:
        headroom_txt = "DÉPASSÉ"
    else:
        headroom_txt = f"reste ${margin['headroom']:,.0f}"

    children = [
        html.Div(
            className="v3-risk-fill",
            style={"width": f"{margin['fill_ratio'] * 100:.1f}%", "background": color},
        )
    ]
    if margin["soft_ratio"]:
        children.append(
            html.Div(className="v3-risk-mark", style={"left": f"{margin['soft_ratio'] * 100:.1f}%"})
        )

    return html.Div(
        [
            html.Div(
                [
                    html.Span(
                        [
                            margin["label"],
                            html.Span(
                                f"${margin['used']:,.0f} / ${margin['soft_limit']:,.0f}",
                                className="sub",
                            ),
                        ],
                        className="v3-risk-label",
                    ),
                    html.Span(headroom_txt, className="v3-risk-headroom", style={"color": color}),
                ],
                className="v3-risk-head",
            ),
            html.Div(children, className="v3-risk-track"),
        ],
        className="v3-risk-row",
    )


def strat_row(p: dict) -> html.Div:
    """Ligne portefeuille : pastille + nom/univers + version/sizing + pill statut."""
    sizing = f"${p['sizing']:,.0f}" if p["sizing"] is not None else "—"
    return html.Div(
        [
            html.Div(className=f"v3-strat-dot {p['color']}"),
            html.Div(
                [
                    html.Div(p["name"], className="v3-strat-name"),
                    html.Div(
                        f"{p['version']} · {sizing} · {p['universe']}", className="v3-strat-sub"
                    ),
                ],
                className="v3-strat-body",
            ),
            html.Span(p["status"], className=f"v3-status-pill {p['color']}"),
        ],
        className="v3-strat-row",
    )


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

    # ── P&L net (héros) + P&L jour — broker prioritaire, fallback state ──────
    if acc:
        net = acc["cum_pnl_net"]
        balance = acc["balance"]
        net_source = "broker"
    else:
        net = pnl["cum_pnl"]
        balance = None
        net_source = "state local"

    if acc and day and day["n_total"] > 0:
        day_pnl, day_known = day["pnl_net"], True
    elif acc and day:
        day_pnl, day_known = 0.0, True
    elif pnl["stale_day"]:
        day_pnl, day_known = 0.0, False
    else:
        day_pnl, day_known = pnl["realized_day_pnl"], True

    net_color = "green" if net > 0 else ("red" if net < 0 else "grey")
    target = TOPSTEP_PROFIT_TARGET
    prog = max(0.0, min(1.0, net / target)) if target > 0 else 0.0

    # Peak & best day — broker-first (cohérent avec le hero net ; le state local
    # est brut/sans frais et sujet aux doublons → on ne l'utilise qu'en fallback).
    if acc:
        dnp = broker.daily_net_pnl(days_back=60)
        best_day = max(dnp.values()) if dnp else 0.0
        eq = broker.equity_curve_from_trades(days_back=60)
        broker_peak = max(eq["equity_net"]) if (eq and eq["equity_net"]) else net
        peak = max(broker_peak, net, pnl["peak_pnl"])
    else:
        bd = comp["best_day"]
        best_day = bd[1] if bd[0] else 0.0
        peak = pnl["peak_pnl"]
    drawdown = max(0.0, peak - net)

    # ── HERO ─────────────────────────────────────────────────────────────────
    hero_meta = []
    if balance is not None:
        hero_meta.append(html.Span(["Balance ", html.B(f"${balance:,.0f}")]))
    if not day_known:
        hero_meta.append(html.Span(["  ·  hors session"]))
    hero = html.Div(
        [
            html.Div(
                [icon("graph-up-arrow", color=ch.GREY), f"P&L net challenge · {net_source}"],
                className="v3-hero-label",
            ),
            html.Div(f"${net:+,.0f}", className=f"v3-hero-value {net_color}"),
            html.Div(
                html.Div(
                    className="v3-progress-fill",
                    style={
                        "width": f"{prog * 100:.1f}%",
                        "background": ch.GREEN if net >= 0 else ch.RED,
                    },
                ),
                className="v3-progress-track",
            ),
            html.Div(
                [
                    html.Span([html.B(f"${net:+,.0f}"), f" / ${target:,.0f}"]),
                    html.Span(html.B(f"{prog * 100:.0f}%")),
                ],
                className="v3-progress-cap",
            ),
            html.Div(hero_meta, className="v3-hero-meta") if hero_meta else html.Div(),
        ],
        className="v3-hero",
    )

    # ── Badges (pill jour + comparaisons temporelles) ────────────────────────
    if day_known:
        d_color = "green" if day_pnl > 0 else ("red" if day_pnl < 0 else "")
        day_pill = html.Span(
            ["Aujourd'hui ", html.B(f"${day_pnl:+,.0f}")],
            className=f"v3-badge {d_color}".strip(),
        )
    else:
        day_pill = html.Span(["Aujourd'hui ", html.B("—")], className="v3-badge")
    badges = [day_pill]
    if comp["yesterday_pnl"] != 0:
        delta = comp["delta_yesterday"]
        c = "green" if delta > 0 else "red" if delta < 0 else ""
        badges.append(badge("vs hier", f"${delta:+,.0f}", c))
    if comp["avg_7d"] != 0:
        badges.append(badge("avg 7j", f"${comp['avg_7d']:+,.0f}"))
    if comp["streak_win_days"] > 1:
        badges.append(badge("streak win", f"{comp['streak_win_days']}j", "green"))
    elif comp["streak_loss_days"] > 1:
        badges.append(badge("streak loss", f"{comp['streak_loss_days']}j", "red"))
    if best_day:
        badges.append(badge("best day", f"${best_day:+,.0f}"))

    # ── Barres de marge (sécurité Topstep) + statut portefeuille ─────────────
    margins = dt.risk_margins(day_pnl, drawdown, best_day)
    risk_panel = html.Div([risk_bar(m) for m in margins], className="v3-risk")

    portfolio = dt.portfolio_status(state)
    portfolio_panel = html.Div([strat_row(p) for p in portfolio], className="v3-portfolio")

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
            hero,
            html.Div(badges, className="v3-badges"),
            section_title("Sécurité — marges Topstep", icon_name="shield-check"),
            risk_panel,
            section_title("Portefeuille", icon_name="grid-3x3-gap"),
            portfolio_panel,
            section_title(
                "Equity (broker net)" if acc else "Equity (state local)",
                icon_name="graph-up-arrow",
            ),
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
                section_title("Par contrat (broker, net)", icon_name="currency-dollar"),
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
            section_title("Par stratégie (state local, brut)", icon_name="bullseye"),
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
                section_title("Equity nette (broker)", icon_name="graph-up-arrow"),
                html.Div(
                    dcc.Markdown(note, dangerously_allow_html=True),
                    style={"color": ch.GREY, "fontSize": "10px", "marginBottom": "8px"},
                ),
                dcc.Graph(
                    figure=ch.equity_curve_pro(timestamps, equity_net, hover_trades, peak_net),
                    config={"displayModeBar": False},
                ),
                section_title("Drawdown underwater", icon_name="water"),
                dcc.Graph(
                    figure=ch.drawdown_underwater_pro(timestamps, equity_net),
                    config={"displayModeBar": False},
                ),
                section_title("Distribution P&L par trade (net)", icon_name="bar-chart"),
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

    today_section: list = []
    today_count = 0

    # Source de vérité = broker. On apparie OPENING+CLOSING par contractId LIFO
    # pour avoir UNE ligne par trade complet (au lieu de 2 entries broker).
    if acc:
        pairs = broker.paired_trades(days_back=2)
        today_pairs = [p for p in pairs if (p.get("close_time") or "").startswith(today_str)]
        today_count = len(today_pairs)
        positions = broker.positions()
        orders = broker.open_orders()

        if today_pairs:
            rows = []
            for p in today_pairs:
                pnl_net = p["pnl_net"]
                pnl_cls = "pnl-pos" if pnl_net >= 0 else "pnl-neg"
                ticker = (
                    p["contract_id"].split(".")[-2] if "." in p["contract_id"] else p["contract_id"]
                )
                # Lookup stratégie côté state local par order_id ou par pnl match
                strat = dt.lookup_strategy_for_pair(state, p)
                rows.append(
                    html.Tr(
                        [
                            html.Td(
                                html.B(strat)
                                if strat != "—"
                                else html.Span(strat, style={"color": ch.GREY})
                            ),
                            html.Td(ticker),
                            html.Td(p["direction"]),
                            html.Td(str(p["n_ct"])),
                            html.Td(f"{p['entry_price']:.2f}" if p["entry_price"] else "—"),
                            html.Td(f"{p['exit_price']:.2f}" if p["exit_price"] else "—"),
                            html.Td(f"${pnl_net:+,.0f}", className=pnl_cls),
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
                                    html.Th("P&L net"),
                                ]
                            )
                        ),
                        html.Tbody(rows),
                    ],
                    className="v3-table",
                )
            )
        else:
            today_section.append(
                html.Div(
                    "Aucun trade clos aujourd'hui.", style={"color": ch.GREY, "padding": "8px 0"}
                )
            )
    else:
        # Fallback state local
        today_local = [t for t in trades if (t["fill_time"] or "").startswith(today_str)]
        today_count = len(today_local)
        positions = dt.open_positions(state)
        orders = dt.open_orders(state)
        if today_local:
            rows = []
            for t in today_local:
                pnl_cls = "pnl-pos" if t["close_pnl"] >= 0 else "pnl-neg"
                rows.append(
                    html.Tr(
                        [
                            html.Td(t.get("strategy", "—")),
                            html.Td(t["ticker"]),
                            html.Td(t["dir"]),
                            html.Td(str(t["n_ct"])),
                            html.Td(str(t.get("entry", "—"))),
                            html.Td(str(t.get("exit", "—"))),
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
        else:
            today_section.append(
                html.Div(
                    "Aucun trade clos aujourd'hui.", style={"color": ch.GREY, "padding": "8px 0"}
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
            section_title(f"Trades aujourd'hui ({today_count})", icon_name="list-ul"),
            *today_section,
            *pos_orders_section,
            section_title("Heatmap P&L journalier", icon_name="calendar3"),
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
            section_title("Shadow vs Live", icon_name="people"),
            *shadow_section,
            section_title("Logs récents", icon_name="terminal"),
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
                        html.Div([icon("graph-up-arrow"), "Topstep Live"], className="v3-title"),
                        html.Div("30s · net", className="v3-subtitle"),
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
