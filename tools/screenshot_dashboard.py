"""Screenshot du dashboard Streamlit via Playwright (viewport iPhone 13).

Permet à Claude de visualiser le rendu exact que voit l'utilisateur sur
son iPhone, sans avoir besoin du téléphone.

Usage :
    python tools/screenshot_dashboard.py
    python tools/screenshot_dashboard.py --tab Strats
    python tools/screenshot_dashboard.py --tab Equity --out /tmp/eq.png
    python tools/screenshot_dashboard.py --url http://localhost:8501 --viewport desktop

Le script :
1. Lance Chromium headless (déjà téléchargé via `playwright install chromium`)
2. Configure le viewport (defaut iPhone 13 — 390×844)
3. Charge le dashboard
4. Attend que Streamlit ait fini de rendre (heuristique : timeout 6s + wait
   for selectors Plotly)
5. Optionnel : clique sur un onglet spécifique
6. Sauvegarde un PNG fullpage
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

VIEWPORTS = {
    "iphone": {"width": 390, "height": 844, "device_scale_factor": 3, "is_mobile": True},
    "iphone-se": {"width": 375, "height": 667, "device_scale_factor": 2, "is_mobile": True},
    "ipad": {"width": 820, "height": 1180, "device_scale_factor": 2, "is_mobile": True},
    "desktop": {"width": 1280, "height": 900, "device_scale_factor": 1, "is_mobile": False},
}


def screenshot(
    url: str = "http://localhost:8501",
    output: str = "/tmp/dashboard.png",
    viewport: str = "iphone",
    tab: str | None = None,
    timeout_ms: int = 8000,
    full_page: bool = True,
) -> str:
    vp = VIEWPORTS.get(viewport, VIEWPORTS["iphone"])
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": vp["width"], "height": vp["height"]},
            device_scale_factor=vp["device_scale_factor"],
            is_mobile=vp["is_mobile"],
            user_agent=(
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
            ),
        )
        page = context.new_page()
        page.goto(url, wait_until="networkidle", timeout=15000)

        # Attendre que le dashboard ait commencé à rendre. Dash expose .dash-tabs,
        # Streamlit expose [role="tablist"] — on tente les deux.
        rendered = False
        for sel in (".dash-tabs", ".dash-tab", '[role="tablist"]'):
            try:
                page.wait_for_selector(sel, timeout=timeout_ms // 3)
                rendered = True
                break
            except Exception:  # noqa: PERF203
                continue
        if not rendered:
            print(
                f"⚠️  Aucun conteneur d'onglets trouvé après {timeout_ms}ms — screenshot tel quel.",
                file=sys.stderr,
            )

        # Clic sur un onglet si demandé. Différents selectors selon le framework
        # (Streamlit utilise [role="tab"], Dash utilise .dash-tab).
        if tab:
            clicked = False
            selectors = [
                f'.dash-tab:has-text("{tab}")',
                f'[role="tab"]:has-text("{tab}")',
                f'div:has-text("{tab}")[data-rb-event-key]',
            ]
            for sel in selectors:
                try:
                    page.click(sel, timeout=2000)
                    clicked = True
                    break
                except Exception:  # noqa: PERF203
                    continue
            if not clicked:
                print(f"⚠️  Onglet '{tab}' introuvable", file=sys.stderr)
            else:
                page.wait_for_timeout(3000)  # Dash callbacks + Plotly render

        # Attendre 2s de plus pour Plotly charts (rendus async côté client)
        page.wait_for_timeout(2500)

        Path(output).parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=output, full_page=full_page)

        browser.close()
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Screenshot du dashboard Streamlit")
    parser.add_argument("--url", default="http://localhost:8501")
    parser.add_argument("--out", default="/tmp/dashboard.png")
    parser.add_argument("--viewport", default="iphone", choices=list(VIEWPORTS.keys()))
    parser.add_argument(
        "--tab",
        default=None,
        help="Onglet à activer (texte visible : Pulse, Strats, Equity, Trades, Sys)",
    )
    parser.add_argument(
        "--no-full-page",
        action="store_true",
        help="Screenshot du viewport seulement (pas la page entière)",
    )
    args = parser.parse_args()

    out = screenshot(
        url=args.url,
        output=args.out,
        viewport=args.viewport,
        tab=args.tab,
        full_page=not args.no_full_page,
    )
    print(f"✓ Screenshot enregistré : {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
