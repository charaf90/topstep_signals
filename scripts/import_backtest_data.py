#!/usr/bin/env python3
"""Import et conversion des données DATA_BACKTEST vers le format standard data/.

Transformations appliquées :
- Renommage colonnes : time → datetime, Volume → volume
- Conversion timezone : ISO 8601 NY-aware → UTC-naïf (pd.to_datetime utc=True puis tz_localize(None))
- Mapping tickers : MNQ1 → NQ1, MYM1 → YM1
- Nommage fichiers : {TICKER}_data_{tf}.csv
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

TICKER_MAPPING = {
    "MES1": "MES1",
    "MNQ1": "NQ1",
    "MYM1": "YM1",
    "MGC1": "MGC1",
    "MCL1": "MCL1",
    "M6E1": "M6E1",
}

TIMEFRAME_SUFFIXES = {
    ", 5.csv": "m5",
    ", 15.csv": "m15",
}


def detect_timeframe(filename: str) -> str | None:
    for suffix, tf_label in TIMEFRAME_SUFFIXES.items():
        if filename.endswith(suffix):
            return tf_label
    return None


def convert_file(src: Path, ticker_dst: str, tf: str, output_dir: Path, dry_run: bool) -> dict:
    df = pd.read_csv(str(src))

    df = df.rename(columns={"time": "datetime", "Volume": "volume"})

    # NY-aware ISO 8601 → UTC-naïf
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True).dt.tz_localize(None)

    df = df.drop_duplicates(subset=["datetime"], keep="last")
    df = df.sort_values("datetime").reset_index(drop=True)
    df = df[["datetime", "open", "high", "low", "close", "volume"]]

    out_name = f"{ticker_dst}_data_{tf}.csv"
    if not dry_run:
        df.to_csv(str(output_dir / out_name), index=False)

    return {
        "dst": out_name,
        "ticker": ticker_dst,
        "tf": tf,
        "lignes": len(df),
        "debut": df["datetime"].iloc[0],
        "fin": df["datetime"].iloc[-1],
    }


def main():
    parser = argparse.ArgumentParser(description="Import DATA_BACKTEST → data/")
    parser.add_argument("--data-dir", default="./DATA_BACKTEST", help="Répertoire source")
    parser.add_argument("--output-dir", default="./data", help="Répertoire de sortie")
    parser.add_argument("--dry-run", action="store_true", help="Simulation sans écriture")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    output_dir = Path(args.output_dir)

    if not data_dir.exists():
        print(f"[ERREUR] Répertoire source introuvable : {data_dir}")
        sys.exit(1)

    output_dir.mkdir(parents=True, exist_ok=True)

    if args.dry_run:
        print("=== MODE DRY-RUN (aucune écriture) ===\n")

    resultats = []
    erreurs = []

    for ticker_dir in sorted(data_dir.iterdir()):
        if not ticker_dir.is_dir():
            continue

        ticker_src = ticker_dir.name
        ticker_dst = TICKER_MAPPING.get(ticker_src)
        if ticker_dst is None:
            print(f"[WARN] Ticker inconnu, ignoré : {ticker_src}")
            continue

        # .suffix == ".csv" exclut naturellement les :Zone.Identifier
        csv_files = [f for f in ticker_dir.iterdir() if f.suffix == ".csv"]

        for csv_file in sorted(csv_files):
            tf = detect_timeframe(csv_file.name)
            if tf is None:
                print(f"[WARN] Timeframe non détecté, ignoré : {csv_file.name}")
                continue

            try:
                info = convert_file(csv_file, ticker_dst, tf, output_dir, args.dry_run)
                resultats.append(info)
            except Exception as exc:
                erreurs.append({"fichier": csv_file.name, "erreur": str(exc)})

    # ── Rapport ──────────────────────────────────────────────────────────────
    label = "DRY-RUN " if args.dry_run else ""
    print(f"\n{label}IMPORT DATA_BACKTEST → {output_dir}/")
    print("=" * 72)
    tickers_ecrits = set()
    for r in sorted(resultats, key=lambda x: (x["ticker"], x["tf"])):
        debut = pd.Timestamp(r["debut"]).strftime("%Y-%m-%d")
        fin = pd.Timestamp(r["fin"]).strftime("%Y-%m-%d")
        print(
            f"  {r['ticker']:<6} {r['tf']:<4}  {r['lignes']:>8} lignes  {debut} → {fin}  →  {r['dst']}"
        )
        tickers_ecrits.add(r["ticker"])

    if erreurs:
        print(f"\n[ERREURS] ({len(erreurs)})")
        for e in erreurs:
            print(f"  {e['fichier']} : {e['erreur']}")

    print(f"\nTotal : {len(resultats)} fichiers importés, {len(erreurs)} erreur(s)")

    if not args.dry_run and resultats:
        print("\nFichiers écrits :")
        for ticker in sorted(tickers_ecrits):
            for tf in ["m15", "m5"]:
                p = output_dir / f"{ticker}_data_{tf}.csv"
                if p.exists():
                    size_kb = p.stat().st_size // 1024
                    print(f"  {p.name:<35} {size_kb:>6} KB")


if __name__ == "__main__":
    main()
