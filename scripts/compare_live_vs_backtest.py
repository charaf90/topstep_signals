#!/usr/bin/env python3
"""
Comparaison fills live vs signaux backtest — vérification prix, SL, TP, timing NY.

Charge les trades CLOSED depuis live_state.json et les confronte aux CSV
générés par backtest.py. Pour chaque fill live, cherche le trade backtest
du même jour (timezone NY), même direction, et compare entry/SL/TP tick par tick.

Prérequis : avoir lancé les backtests OPR v5.1 + FIB v4 sur données fraîches :
    python backtest.py --strategy opr_v5_1 --csv-dir ./data --ticker NQ1
    python backtest.py --strategy opr_v5_1 --csv-dir ./data --ticker YM1
    python backtest.py --strategy opr       --csv-dir ./data --ticker MES1
    python backtest.py --strategy fib_v4   --csv-dir ./data

Usage :
    python scripts/compare_live_vs_backtest.py
    python scripts/compare_live_vs_backtest.py --days-back 21
    python scripts/compare_live_vs_backtest.py --output output/rapport_live_bt.md
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

NY_TZ = ZoneInfo("America/New_York")

# Mapping (strategy_live, ticker) → fichier CSV backtest attendu
BACKTEST_MAP: dict[tuple[str, str], str] = {
    ("OPR", "NQ1"): "backtest_NQ1_opr_v5_1.csv",
    ("OPR", "YM1"): "backtest_YM1_opr_v5_1.csv",
    ("OPR", "MES1"): "backtest_MES1_opr.csv",
    ("FIB", "MES1"): "backtest_MES1_fib_v4.csv",
    ("FIB", "NQ1"): "backtest_NQ1_fib_v4.csv",
    ("FIB", "MGC1"): "backtest_MGC1_fib_v4.csv",
}

# Taille d'un tick par ticker (pour calcul de l'écart en ticks)
TICK_SIZE: dict[str, float] = {
    "NQ1": 0.25,
    "YM1": 1.0,
    "MES1": 0.25,
    "MGC1": 0.1,
}

# Fenêtre OPR en heure NY (EDT = UTC-4, EST = UTC-5)
OPR_WINDOW_NY_START = "09:30"
OPR_WINDOW_NY_END = "11:30"


# ─── Parsing live_state.json ──────────────────────────────────────────────────


def parse_live_fills(state_file: Path, days_back: int) -> list[dict]:
    """Extrait les trades CLOSED du live_state.json récents (≤ days_back jours)."""
    with open(state_file, encoding="utf-8") as f:
        state = json.load(f)

    cutoff = datetime.now(UTC) - timedelta(days=days_back)
    fills = []

    for tag, info in state.get("placed_tags", {}).items():
        if info.get("status") != "CLOSED":
            continue
        fill_str = info.get("fill_time")
        if not fill_str:
            continue

        fill_utc = datetime.fromisoformat(fill_str.replace("Z", "+00:00"))
        if fill_utc < cutoff:
            continue

        fill_ny = fill_utc.astimezone(NY_TZ)

        fills.append(
            {
                "tag": tag,
                "strategy": info.get("strategy", "?"),
                "ticker": info.get("ticker", "?"),
                "direction": info.get("direction", "?"),  # "long" / "short"
                "entry_live": info.get("entry"),
                "sl_live": info.get("sl"),
                "tp_live": info.get("tp"),
                "n_ct": info.get("n_ct"),
                "close_pnl": info.get("close_pnl"),
                "fill_utc": fill_utc.replace(tzinfo=None),
                "fill_ny": fill_ny,
                "date_ny": fill_ny.date(),
            }
        )

    fills.sort(key=lambda x: x["fill_utc"])
    return fills


# ─── Chargement backtest ──────────────────────────────────────────────────────


_bt_cache: dict[str, pd.DataFrame | None] = {}


def load_backtest(output_dir: Path, key: tuple[str, str]) -> pd.DataFrame | None:
    csv_name = BACKTEST_MAP.get(key)
    if not csv_name:
        return None
    if csv_name in _bt_cache:
        return _bt_cache[csv_name]
    path = output_dir / csv_name
    if not path.exists():
        _bt_cache[csv_name] = None
        return None
    df = pd.read_csv(str(path))
    # Normalise colonne de direction : "direction" ou "dir" → "dir"
    if "direction" in df.columns and "dir" not in df.columns:
        df = df.rename(columns={"direction": "dir"})
    # Normalise la colonne date (peut être au début ou en fin de CSV)
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    _bt_cache[csv_name] = df
    return df


def find_bt_match(bt_df: pd.DataFrame, fill: dict) -> dict | None:
    """
    Cherche le trade backtest correspondant au fill live.
    Critères : même date NY + même direction.
    Si plusieurs (MAX_TRADES_PER_DAY=2), on prend le plus proche en fill_time.
    """
    # Normalise la direction live (le backtest OPR utilise "long"/"short" minuscule)
    direction = fill["direction"].lower()
    mask = (bt_df["date"] == fill["date_ny"]) & (bt_df["dir"].str.lower() == direction)
    candidates = bt_df[mask]
    if candidates.empty:
        return None

    # Si fill_time disponible dans le backtest, prendre le plus proche en temps
    if "fill_time" in candidates.columns and fill["fill_utc"] is not None:
        try:
            candidates = candidates.copy()
            candidates["_ft"] = pd.to_datetime(
                candidates["fill_time"], utc=True, errors="coerce"
            ).dt.tz_localize(None)
            fill_dt = fill["fill_utc"]
            candidates["_delta"] = (candidates["_ft"] - fill_dt).abs()
            return candidates.sort_values("_delta").iloc[0].to_dict()
        except Exception:
            pass

    return candidates.iloc[0].to_dict()


# ─── Helpers ─────────────────────────────────────────────────────────────────


def fmt_tick(live_val: float | None, bt_val, ticker: str) -> str:
    """Formatte la différence en ticks entre valeur live et backtest."""
    if live_val is None:
        return "—"
    try:
        bt_f = float(bt_val)
    except (TypeError, ValueError):
        return "—"
    tick = TICK_SIZE.get(ticker, 0.25)
    diff = (live_val - bt_f) / tick
    if abs(diff) < 0.05:
        return "✅ 0t"
    return f"⚠️ {diff:+.1f}t"


def check_opr_timing(fill: dict) -> str:
    """Vérifie que le fill OPR tombe dans la fenêtre OPR (09:30-11:30 NY)."""
    if fill["strategy"] != "OPR":
        return fill["fill_ny"].strftime("%H:%M NY")
    t = fill["fill_ny"].strftime("%H:%M")
    ok = OPR_WINDOW_NY_START <= t <= OPR_WINDOW_NY_END
    flag = "✅" if ok else "⚠️ hors fenêtre OPR"
    return f"{t} NY {flag}"


# ─── Main ────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Comparaison fills live vs signaux backtest (entry, SL, TP, timing NY)."
    )
    parser.add_argument(
        "--days-back", type=int, default=14, help="Fenêtre d'analyse en jours (défaut 14)"
    )
    parser.add_argument("--state-file", default=str(ROOT / "state" / "live_state.json"))
    parser.add_argument(
        "--output-dir",
        default=str(ROOT / "output"),
        help="Répertoire des CSV backtest et sortie rapport",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Chemin du rapport markdown (défaut : output/live_vs_backtest_<date>.md)",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    fills = parse_live_fills(Path(args.state_file), args.days_back)

    if not fills:
        print(f"Aucun fill CLOSED dans les {args.days_back} derniers jours.")
        return

    print(f"\n{'═'*70}")
    print(f"  Live vs Backtest — {args.days_back} derniers jours")
    print(f"  {len(fills)} fills CLOSED analysés")
    print(f"{'═'*70}\n")

    rows = []
    for fill in fills:
        key = (fill["strategy"], fill["ticker"])
        bt_df = load_backtest(output_dir, key)

        date_str = fill["fill_ny"].strftime("%Y-%m-%d")
        timing = check_opr_timing(fill)
        csv_name = BACKTEST_MAP.get(key, "—")

        if bt_df is None:
            statut = f"⚠️ CSV manquant ({csv_name})"
            row = _row(fill, date_str, timing, None, statut)
        else:
            bt = find_bt_match(bt_df, fill)
            if bt is None:
                statut = "❌ signal absent en backtest"
                row = _row(fill, date_str, timing, None, statut)
            else:
                d_e = fmt_tick(fill["entry_live"], bt.get("entry"), fill["ticker"])
                d_s = fmt_tick(fill["sl_live"], bt.get("sl"), fill["ticker"])
                d_t = fmt_tick(fill["tp_live"], bt.get("tp"), fill["ticker"])
                checks = [d_e, d_s, d_t]
                if all("✅" in c for c in checks):
                    statut = "✅ match parfait"
                else:
                    statut = "⚠️ écart détecté"
                row = _row(fill, date_str, timing, bt, statut, d_entry=d_e, d_sl=d_s, d_tp=d_t)

        rows.append(row)
        _print_row(row)

    # ── Rapport markdown ───────────────────────────────────────────────────
    today = datetime.now().strftime("%Y-%m-%d")
    out_path = Path(args.output) if args.output else (output_dir / f"live_vs_backtest_{today}.md")
    _write_md(rows, out_path, args.days_back)

    # ── Résumé ─────────────────────────────────────────────────────────────
    n_ok = sum(1 for r in rows if "match parfait" in r["Statut"])
    n_ecart = sum(1 for r in rows if "écart" in r["Statut"])
    n_absent = sum(1 for r in rows if "absent" in r["Statut"])
    n_missing = sum(1 for r in rows if "manquant" in r["Statut"])

    print(f"\n{'─'*70}")
    print(f"  ✅ Match parfait       : {n_ok}/{len(rows)}")
    print(f"  ⚠️  Écart détecté      : {n_ecart}/{len(rows)}")
    print(f"  ❌ Signal absent BT   : {n_absent}/{len(rows)}")
    if n_missing:
        print(f"  ⚠️  CSV backtest manquant : {n_missing}/{len(rows)}  → relancer les backtests")
    print(f"{'─'*70}")
    print(f"\nRapport : {out_path}\n")


def _row(
    fill: dict,
    date_str: str,
    timing: str,
    bt: dict | None,
    statut: str,
    d_entry="—",
    d_sl="—",
    d_tp="—",
) -> dict:
    return {
        "Date": date_str,
        "Ticker": fill["ticker"],
        "Strat": fill["strategy"],
        "Dir": fill["direction"],
        "Entry live": fill["entry_live"],
        "Entry BT": f"{float(bt['entry']):.4g}" if bt and bt.get("entry") else "—",
        "ΔEntry": d_entry,
        "SL live": fill["sl_live"],
        "SL BT": f"{float(bt['sl']):.4g}" if bt and bt.get("sl") else "—",
        "ΔSL": d_sl,
        "TP live": fill["tp_live"],
        "TP BT": f"{float(bt['tp']):.4g}" if bt and bt.get("tp") else "—",
        "ΔTP": d_tp,
        "PnL live": fill["close_pnl"],
        "Timing NY": timing,
        "Statut": statut,
    }


def _print_row(r: dict) -> None:
    print(
        f"  {r['Date']}  {r['Ticker']:<5} {r['Strat']:<4} {r['Dir']:<6} "
        f"entry={r['Entry live']}({r['ΔEntry']})  "
        f"sl=({r['ΔSL']})  tp=({r['ΔTP']})  "
        f"pnl={r['PnL live']:+.0f}$  {r['Timing NY']}  {r['Statut']}"
    )


def _write_md(rows: list[dict], path: Path, days_back: int) -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    df = pd.DataFrame(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# Live vs Backtest — {today}\n\n")
        f.write(f"Analyse des **{days_back} derniers jours** de fills CLOSED.\n\n")
        f.write(df.to_markdown(index=False))
        f.write("\n\n---\n\n## Légende\n\n")
        f.write("| Symbole | Signification |\n|---------|---------------|\n")
        f.write("| ✅ 0t | Prix identique live et backtest (à 1 tick près) |\n")
        f.write("| ⚠️ Xt | Écart de X ticks entre live et backtest |\n")
        f.write("| ❌ signal absent | Le backtest ne génère pas ce trade ce jour-là |\n")
        f.write("| ⚠️ CSV manquant | Relancer `backtest.py --strategy ... --csv-dir ./data` |\n")
        f.write(
            "\n**Timing NY OPR** : le fill doit tomber entre 09:30 et 11:30 NY "
            "(EDTmars-nov = UTC-4, ESTnov-mars = UTC-5).\n"
        )


if __name__ == "__main__":
    main()
