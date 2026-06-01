#!/usr/bin/env python3
"""
Mise à jour des CSV m15 production depuis l'API ProjectX (TopstepX).

Fetch les N derniers jours pour chaque actif en production, fusionne avec
les CSV existants (dédup), vérifie que les prix se chevauchent à 0 tick près.

Usage :
    python scripts/update_data_api.py                   # 30 jours, tous tickers
    python scripts/update_data_api.py --dry-run         # rapport sans écriture
    python scripts/update_data_api.py --days 60         # fenêtre plus large
    python scripts/update_data_api.py --tickers NQ1 YM1 # sélectif
    python scripts/update_data_api.py --live            # contrats live (défaut : sim)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from broker.projectx_client import ProjectXClient
from core.data import load_csv
from core.data_fetcher import fetch_history

_log = logging.getLogger(__name__)

# Mapping ticker projet → alias CATALOG data_fetcher
PRODUCTION_TICKERS: dict[str, str] = {
    "NQ1": "MNQ",
    "YM1": "MYM",
    "MES1": "MES",
    "MGC1": "MGC",
}

TIMEFRAME = "m15"


def build_client(live: bool) -> ProjectXClient:
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
    user = os.environ.get("PROJECTX_USERNAME", "").strip()
    api = os.environ.get("PROJECTX_API_KEY", "").strip()
    if not user or not api:
        raise SystemExit("PROJECTX_USERNAME et PROJECTX_API_KEY doivent être définis dans .env")
    client = ProjectXClient(user, api)
    if not client.login():
        raise SystemExit("Authentification ProjectX échouée")
    return client


def verify_overlap(
    df_existing: pd.DataFrame,
    df_api: pd.DataFrame,
    ticker: str,
) -> list[str]:
    """
    Compare open/close sur les barres communes. Retourne une liste de warnings.
    Un écart > 0.01 indique un problème de timezone ou de rollover de contrat.
    """
    common = df_existing.index.intersection(df_api.index)
    if common.empty:
        return [f"  {ticker}: aucun chevauchement — données disjointes (rollover ?)"]

    warnings = []
    for col in ("open", "close"):
        diff = (df_existing.loc[common, col] - df_api.loc[common, col]).abs()
        bad = diff[diff > 0.01]
        if not bad.empty:
            sample = bad.head(3).index.tolist()
            warnings.append(
                f"  {ticker}.{col}: {len(bad)}/{len(common)} bougies divergentes "
                f"(max={bad.max():.4f}) — ex: {[str(s)[:16] for s in sample]}"
            )
    return warnings


def process_ticker(
    client: ProjectXClient,
    ticker: str,
    symbol: str,
    days: int,
    data_dir: Path,
    dry_run: bool,
    live: bool,
) -> dict:
    csv_path = data_dir / f"{ticker}_data_{TIMEFRAME}.csv"

    df_api = fetch_history(client, symbol=symbol, days_back=days, timeframe=TIMEFRAME, live=live)
    if df_api.empty:
        return {"ticker": ticker, "ok": False, "msg": "API retourne 0 bougies"}

    # Charger existant si présent
    overlap_warnings: list[str] = []
    n_existing = 0
    existing_last = "—"

    if csv_path.exists():
        try:
            df_existing = load_csv(str(csv_path))
            n_existing = len(df_existing)
            existing_last = str(df_existing.index[-1])[:16]
            overlap_warnings = verify_overlap(df_existing, df_api, ticker)
            # Merge : existant + API, dédup index, trié
            merged = pd.concat([df_existing, df_api])
            merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        except Exception as exc:
            return {"ticker": ticker, "ok": False, "msg": str(exc)}
    else:
        merged = df_api

    n_new = len(merged) - n_existing

    if not dry_run:
        merged.index.name = "datetime"
        merged.to_csv(csv_path, index=True, date_format="%Y-%m-%d %H:%M:%S")

    return {
        "ticker": ticker,
        "ok": True,
        "api_start": str(df_api.index[0])[:16],
        "api_end": str(df_api.index[-1])[:16],
        "existing_end": existing_last,
        "n_api": len(df_api),
        "n_new": n_new,
        "n_total": len(merged),
        "overlap_w": overlap_warnings,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Met à jour les CSV m15 production depuis l'API ProjectX."
    )
    parser.add_argument(
        "--days", type=int, default=30, help="Nombre de jours à récupérer (défaut 30)"
    )
    parser.add_argument(
        "--tickers",
        nargs="+",
        default=list(PRODUCTION_TICKERS.keys()),
        choices=list(PRODUCTION_TICKERS.keys()),
        metavar="TICKER",
        help="Tickers à mettre à jour (défaut : tous)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Rapport sans écriture des CSV")
    parser.add_argument(
        "--live", action="store_true", help="Utiliser les contrats live (défaut : sim/paper)"
    )
    parser.add_argument(
        "--data-dir", default=str(ROOT / "data"), help="Répertoire data/ (défaut : ./data)"
    )
    parser.add_argument(
        "--log-level", default="WARNING", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s  %(levelname)-7s  %(name)s : %(message)s",
        datefmt="%H:%M:%S",
    )

    data_dir = Path(args.data_dir)
    mode = "DRY-RUN (aucune écriture)" if args.dry_run else "ÉCRITURE"

    print(f"\n{'═'*62}")
    print(f"  Mise à jour données API ProjectX — {mode}")
    print(f"  Tickers  : {', '.join(args.tickers)}")
    print(f"  Fenêtre  : {args.days} jours  |  contrats : {'live' if args.live else 'sim'}")
    print(f"  Sortie   : {data_dir}/")
    print(f"{'═'*62}\n")

    client = build_client(args.live)

    results = []
    for ticker in args.tickers:
        symbol = PRODUCTION_TICKERS[ticker]
        print(f"  → {ticker} ({symbol}) ...", end=" ", flush=True)
        r = process_ticker(client, ticker, symbol, args.days, data_dir, args.dry_run, args.live)
        results.append(r)
        if r["ok"]:
            print(f"+{r['n_new']} barres  (API {r['api_start']} → {r['api_end']})")
        else:
            print(f"ERREUR: {r['msg']}")

    # ── Rapport ──────────────────────────────────────────────────────────────
    print(f"\n{'─'*62}")
    print(f"  {'Ticker':<8} {'CSV avant':<18} {'API fin':<18} {'Nouvelles':<11} {'Total'}")
    print(f"{'─'*62}")
    for r in results:
        if r["ok"]:
            print(
                f"  {r['ticker']:<8} {r['existing_end']:<18} {r['api_end']:<18} "
                f"+{r['n_new']:<10} {r['n_total']}"
            )
        else:
            print(f"  {r['ticker']:<8} ERREUR: {r['msg']}")

    all_warnings = [w for r in results if r.get("ok") for w in r.get("overlap_w", [])]
    if all_warnings:
        print("\n⚠️  Alertes overlap (vérifier timezone ou rollover contrat) :")
        for w in all_warnings:
            print(w)
    else:
        print("\n✅ Overlap OK — prix identiques sur la période commune CSV ↔ API")

    if args.dry_run:
        print("\n[DRY-RUN] Aucun fichier modifié. Relancer sans --dry-run pour écrire.\n")
    else:
        print(
            "\n✅ CSVs mis à jour. Prêt pour : python backtest.py --strategy ... --csv-dir ./data\n"
        )


if __name__ == "__main__":
    main()
