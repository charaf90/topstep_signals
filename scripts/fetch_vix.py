#!/usr/bin/env python3
"""
Téléchargement de l'historique journalier du VIX (indice de volatilité CBOE).

Source : CSV officiel CBOE (gratuit, sans clé API, historique depuis 1990).
    https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv

Sortie : data/VIX_daily.csv au format projet (datetime,open,high,low,close).
Le VIX n'a pas de volume — la colonne est omise (≠ CSV m15 intraday).

Usage :
    python scripts/fetch_vix.py                 # tout l'historique
    python scripts/fetch_vix.py --start 2024-01-01
    python scripts/fetch_vix.py --dry-run       # rapport sans écriture
"""

from __future__ import annotations

import argparse
import io
import sys
import urllib.request
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUT_PATH = DATA_DIR / "VIX_daily.csv"

CBOE_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"


def fetch_vix() -> pd.DataFrame:
    """Récupère l'historique VIX CBOE et le renvoie au format projet."""
    req = urllib.request.Request(CBOE_URL, headers={"User-Agent": "Mozilla/5.0"})
    raw = urllib.request.urlopen(req, timeout=30).read().decode()
    df = pd.read_csv(io.StringIO(raw))

    df.columns = [c.strip().lower() for c in df.columns]
    df["datetime"] = pd.to_datetime(df["date"], format="%m/%d/%Y")
    df = df[["datetime", "open", "high", "low", "close"]].copy()
    df = df.sort_values("datetime").reset_index(drop=True)
    return df


def main() -> int:
    ap = argparse.ArgumentParser(description="Télécharge l'historique VIX (CBOE).")
    ap.add_argument("--start", type=str, default=None, help="Date de début (YYYY-MM-DD).")
    ap.add_argument("--dry-run", action="store_true", help="Rapport sans écriture.")
    args = ap.parse_args()

    df = fetch_vix()
    if args.start:
        df = df[df["datetime"] >= pd.Timestamp(args.start)].reset_index(drop=True)

    print(
        f"VIX CBOE : {len(df)} jours  {df['datetime'].iloc[0].date()} → {df['datetime'].iloc[-1].date()}"
    )
    print(
        f"  close récent : {df['close'].iloc[-1]:.2f}  (min {df['close'].min():.2f} / max {df['close'].max():.2f})"
    )

    if args.dry_run:
        print("[dry-run] aucune écriture.")
        return 0

    DATA_DIR.mkdir(exist_ok=True)
    df.to_csv(OUT_PATH, index=False)
    print(f"Écrit → {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
