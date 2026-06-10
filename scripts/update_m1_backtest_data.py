#!/usr/bin/env python3
"""
Mise à jour des CSV M1 d'historique (DATA_BACKTEST/) depuis l'API ProjectX.

Miroir de scripts/update_data_api.py mais ciblant le M1 dans DATA_BACKTEST/, qui
alimente les stratégies M5 du portefeuille (fib-fine-v2, bos-fvg-v1) via
strategies/_opr_core.load_tf → _load_m1 → resample. Le M1 historique s'arrête
fin février/mars ; ce script comble le gap jusqu'à aujourd'hui.

Fetch les N derniers jours par actif, fusionne avec le M1 existant (dédup),
vérifie que les prix se chevauchent à 0.01 près sur la période commune.

Usage :
    python scripts/update_m1_backtest_data.py --dry-run            # rapport, pas d'écriture
    python scripts/update_m1_backtest_data.py --days 100           # écrit (défaut 100 j)
    python scripts/update_m1_backtest_data.py --tickers NQ1 MES1   # sélectif
    python scripts/update_m1_backtest_data.py --live               # contrats live (défaut : sim)

Schéma d'écriture : datetime,open,high,low,close,volume (UTC naïf), relisable
directement par strategies/_opr_core._load_m1 (la colonne `symbol` éventuelle de
l'ancien historique est volontairement abandonnée — aucun code ne la lit).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.data_fetcher import fetch_history
from scripts.update_data_api import build_client, verify_overlap

_log = logging.getLogger(__name__)

# Mapping ticker projet → alias CATALOG data_fetcher (seuls les actifs avec M1
# historique et consommés par fib-fine/bos-fvg). MGC1 n'a pas de M1 et est hors
# univers M5 → exclu.
M1_TICKERS: dict[str, str] = {
    "NQ1": "MNQ",
    "MES1": "MES",
    "YM1": "MYM",
}

TIMEFRAME = "m1"
DATA_DIR = ROOT / "DATA_BACKTEST"


def _load_existing_m1(path: Path) -> pd.DataFrame:
    """Charge un M1 existant (datetime + OHLCV), index UTC naïf trié dédupliqué.

    Mirroir de strategies/_opr_core._load_m1 : on ne garde que les colonnes OHLCV
    (toute colonne `symbol` héritée est ignorée)."""
    df = pd.read_csv(path, parse_dates=["datetime"])
    df = df.set_index("datetime").sort_index()
    df = df[~df.index.duplicated(keep="first")]
    return df[["open", "high", "low", "close", "volume"]]


def process_ticker(
    client,
    ticker: str,
    symbol: str,
    days: int,
    data_dir: Path,
    dry_run: bool,
    live: bool,
) -> dict:
    csv_path = data_dir / f"{ticker}_data_m1.csv"

    df_api = fetch_history(client, symbol=symbol, days_back=days, timeframe=TIMEFRAME, live=live)
    if df_api.empty:
        return {"ticker": ticker, "ok": False, "msg": "API retourne 0 bougies"}

    overlap_warnings: list[str] = []
    n_existing = 0
    existing_last = "—"

    if csv_path.exists():
        try:
            df_existing = _load_existing_m1(csv_path)
            n_existing = len(df_existing)
            existing_last = str(df_existing.index[-1])[:16]
            overlap_warnings = verify_overlap(df_existing, df_api, ticker)
            merged = pd.concat([df_existing, df_api])
            merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        except Exception as exc:  # noqa: BLE001
            return {"ticker": ticker, "ok": False, "msg": str(exc)}
    else:
        merged = df_api

    n_new = len(merged) - n_existing

    if not dry_run:
        merged.index.name = "datetime"
        merged = merged.astype(
            {"open": float, "high": float, "low": float, "close": float, "volume": "int64"}
        )
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
        description="Met à jour les CSV M1 DATA_BACKTEST depuis l'API ProjectX."
    )
    parser.add_argument(
        "--days", type=int, default=100, help="Nombre de jours à récupérer (défaut 100)"
    )
    parser.add_argument(
        "--tickers",
        nargs="+",
        default=list(M1_TICKERS.keys()),
        choices=list(M1_TICKERS.keys()),
        metavar="TICKER",
        help="Tickers à mettre à jour (défaut : tous)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Rapport sans écriture des CSV")
    parser.add_argument(
        "--live", action="store_true", help="Utiliser les contrats live (défaut : sim/paper)"
    )
    parser.add_argument(
        "--data-dir", default=str(DATA_DIR), help="Répertoire DATA_BACKTEST/ (défaut)"
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
    print(f"  Mise à jour M1 DATA_BACKTEST — API ProjectX — {mode}")
    print(f"  Tickers  : {', '.join(args.tickers)}")
    print(f"  Fenêtre  : {args.days} jours  |  contrats : {'live' if args.live else 'sim'}")
    print(f"  Sortie   : {data_dir}/")
    print(f"{'═'*62}\n")

    client = build_client(args.live)

    results = []
    for ticker in args.tickers:
        symbol = M1_TICKERS[ticker]
        print(f"  → {ticker} ({symbol}) ...", end=" ", flush=True)
        r = process_ticker(client, ticker, symbol, args.days, data_dir, args.dry_run, args.live)
        results.append(r)
        if r["ok"]:
            print(f"+{r['n_new']} barres  (API {r['api_start']} → {r['api_end']})")
        else:
            print(f"ERREUR: {r['msg']}")

    print(f"\n{'─'*62}")
    print(f"  {'Ticker':<8} {'M1 avant':<18} {'API fin':<18} {'Nouvelles':<11} {'Total'}")
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
        print("\n✅ Overlap OK — prix identiques sur la période commune M1 ↔ API")

    if args.dry_run:
        print("\n[DRY-RUN] Aucun fichier modifié. Relancer sans --dry-run pour écrire.\n")
    else:
        print("\n✅ M1 mis à jour. fib-fine-v2 / bos-fvg-v1 verront les barres fraîches.\n")


if __name__ == "__main__":
    main()
