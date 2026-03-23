#!/usr/bin/env python3
"""
Backtest de la stratégie ordres limites.

Usage :
  python backtest.py --csv-dir ./data                    # Backtest 3 actifs
  python backtest.py --csv-dir ./data --ticker NQ1       # 1 actif
  python backtest.py --csv-dir ./data --plot             # Avec graphiques
  python backtest.py --csv-dir ./data --telegram         # Avec envoi Telegram
"""

import argparse
import time
from pathlib import Path

import pandas as pd
import numpy as np

from config import (
    INSTRUMENTS, SL_MINIMUM, RR_TARGET, ZONE_QUALITY_MIN,
    RISK_PER_TRADE_USD, MAX_TRADES_PER_DAY, CUTOFF_HOUR_UTC,
    US_SESSION_START_UTC, US_SESSION_END_UTC,
    MIN_BARS_HISTORY, MIN_BARS_US_SESSION,
)
from core.data import load_csv, build_timeframes
from core.strategy import generate_signals, simulate_trade
from core.trend import precompute_trends
from core.chart import plot_signal, plot_backtest_trade


def run_backtest(df_15m: pd.DataFrame, tf_dict: dict, ticker: str) -> pd.DataFrame:
    """
    Backtest complet jour par jour.
    Les ordres non remplis ne comptent PAS vers le max de 2 trades/jour.
    """
    dpp = INSTRUMENTS[ticker]["dollar_per_point"]
    trend_scores = precompute_trends(tf_dict)
    dates = df_15m.index.normalize().unique()
    trades = []

    for day in dates:
        ds = day.strftime("%Y-%m-%d")
        cutoff = pd.Timestamp(f"{ds} {CUTOFF_HOUR_UTC:02d}:00:00")
        us_start = pd.Timestamp(f"{ds} {US_SESSION_START_UTC:02d}:00:00")
        us_end = pd.Timestamp(f"{ds} {US_SESSION_END_UTC:02d}:00:00")

        us_data = df_15m[(df_15m.index >= us_start) & (df_15m.index <= us_end)]
        if len(us_data) < MIN_BARS_US_SESSION:
            continue

        # Identique au mode live : on ne génère que les max N meilleurs signaux (qualité décroissante)
        signals = generate_signals(df_15m, tf_dict, ticker, cutoff, trend_scores,
                                   max_signals=MAX_TRADES_PER_DAY)

        # 1. Simuler tous les signaux pour trouver leur heure de déclenchement (fill_time)
        day_trades = []
        for sig in signals:
            result = simulate_trade(us_data, sig, dpp)
            trade = {
                "date": ds,
                "dir": sig["direction"],
                "entry": sig["entry"],
                "sl": sig["sl"],
                "tp": sig["tp"],
                "sl_dist": sig["sl_dist"],
                "tp_dist": sig["tp_dist"],
                "rr": sig["rr"],
                "n_ct": sig["n_ct"],
                "risk_$": sig["risk"],
                "quality": sig["quality"],
                "n_tf": sig["n_tf"],
                "touches": sig["touches"],
                "regime": sig["regime"],
                "zone_low": sig["zone_low"],
                "zone_high": sig["zone_high"],
                "tp_type": sig.get("tp_type", "rr"),
                **{k: sig[k] for k in ["entry_1", "entry_2", "n_ct_1", "n_ct_2", "scale_in"]
                   if k in sig},
                **result,
            }
            day_trades.append(trade)

        # 2. Séparer les trades remplis et non remplis
        filled = [t for t in day_trades if t["result"] != "NOT_FILLED"]
        not_filled = [t for t in day_trades if t["result"] == "NOT_FILLED"]

        # 3. Trier chronologiquement par heure de fill
        # fill_time est une chaîne issue d'un pd.Timestamp ("YYYY-MM-DD HH:MM:SS"), donc triable
        filled.sort(key=lambda t: t["fill_time"])

        # 4. Appliquer la limite MAX_TRADES_PER_DAY sur les trades chronologiques
        kept_filled = filled[:MAX_TRADES_PER_DAY]
        cancelled_filled = filled[MAX_TRADES_PER_DAY:]
        
        # Les ordres "trop tard" deviennent simplement non remplis (annulés virtuellement)
        for t in cancelled_filled:
            t["result"] = "NOT_FILLED"
            t["pnl"] = 0
            t["fill_time"] = None
            t["exit_time"] = None
            t["exit"] = None

        trades.extend(kept_filled)
        trades.extend(not_filled)
        trades.extend(cancelled_filled)

    return pd.DataFrame(trades)


def audit(df_trades: pd.DataFrame, ticker: str) -> bool:
    """Vérifie l'intégrité des trades."""
    dpp = INSTRUMENTS[ticker]["dollar_per_point"]
    sl_min = SL_MINIMUM[ticker]
    filled = df_trades[df_trades["result"] != "NOT_FILLED"]
    errors = 0

    # P&L
    for _, r in filled.iterrows():
        if r.get("scale_in", False):
            # Scale-in : PnL calculé sur 2 entrées, audit simplifié (skip)
            continue
        if r["dir"] == "long":
            exp = r["n_ct"] * (r["exit"] - r["entry"]) * dpp
        else:
            exp = r["n_ct"] * (r["entry"] - r["exit"]) * dpp
        if abs(exp - r["pnl"]) > 1:
            errors += 1

    # SL minimum
    if (filled["sl_dist"] < sl_min - 0.01).any():
        errors += 1

    # Régime
    for _, r in filled.iterrows():
        if r["dir"] == "long" and r["regime"] == "BEAR":
            errors += 1
        if r["dir"] == "short" and r["regime"] == "BULL":
            errors += 1

    print(f"  AUDIT: {'✅ OK' if errors == 0 else f'❌ {errors} erreurs'}")
    return errors == 0


def print_stats(df_trades: pd.DataFrame, ticker: str):
    """Rapport statistique."""
    filled = df_trades[df_trades["result"] != "NOT_FILLED"]
    if len(filled) == 0:
        print(f"  Aucun trade rempli")
        return

    wins = filled[filled["pnl"] > 0]
    losses = filled[filled["pnl"] <= 0]
    gp = wins["pnl"].sum() if len(wins) else 0
    gl = abs(losses["pnl"].sum()) if len(losses) else 1
    cum = filled["pnl"].cumsum()
    dd = (cum - cum.cummax()).min()

    rr = RR_TARGET[ticker]
    print(f"\n  {'═'*55}")
    print(f"  {ticker} — {INSTRUMENTS[ticker]['name']}")
    print(f"  {'═'*55}")
    print(f"  Config  : SL≥{SL_MINIMUM[ticker]}pts  RR={rr}  QualMin={ZONE_QUALITY_MIN[ticker]}")
    print(f"  Trades  : {len(filled)} remplis / {len(df_trades)} signaux")
    print(f"  {'─'*55}")
    print(f"  WR      : {len(wins)/len(filled)*100:.0f}%")
    if len(wins) > 0:
        print(f"  Avg win : ${wins['pnl'].mean():+.0f}")
    if len(losses) > 0:
        print(f"  Avg loss: ${losses['pnl'].mean():.0f}")
    print(f"  PF      : {gp/gl:.2f}")
    print(f"  {'─'*55}")
    print(f"  P&L     : ${filled['pnl'].sum():+,.0f}")
    print(f"  Max DD  : ${dd:,.0f}")
    print(f"  $/trade : ${filled['pnl'].mean():+.1f}")
    print(f"  {'─'*55}")

    for r in ["TP", "SL", "TE"]:
        sub = filled[filled["result"] == r]
        if len(sub) > 0:
            print(f"  {r:3s}: n={len(sub):>4}  avg=${sub['pnl'].mean():+.0f}")

    print(f"  {'─'*55}")
    for regime in ["BULL", "BEAR", "RANGE"]:
        sub = filled[filled["regime"] == regime]
        if len(sub) > 0:
            wr = (sub["pnl"] > 0).mean() * 100
            print(f"  {regime:5s}: n={len(sub):>3}  WR={wr:.0f}%  P&L=${sub['pnl'].sum():+,.0f}")

    # Mensuel
    print(f"  {'─'*55}")
    fc = filled.copy()
    fc["month"] = pd.to_datetime(fc["date"]).dt.to_period("M")
    monthly = fc.groupby("month")["pnl"].sum()
    for m, v in monthly.items():
        bar = "█" * max(1, int(abs(v) / 100))
        bust = " ⚠ BUST" if v < -2000 else ""
        print(f"  {m} : ${v:>+8,.0f} {bar}{bust}")


def format_backtest_report(results: list) -> str:
    """Formate le rapport backtest en HTML pour Telegram."""
    msg = "📊 <b>RAPPORT BACKTEST</b>\n\n"

    total_pnl = 0
    total_trades = 0

    for res in results:
        ticker = res["ticker"]
        filled = res["filled"]
        n = len(filled)
        if n == 0:
            continue

        total_trades += n
        wins = filled[filled["pnl"] > 0]
        losses = filled[filled["pnl"] <= 0]
        pnl = filled["pnl"].sum()
        total_pnl += pnl
        gp = wins["pnl"].sum() if len(wins) else 0
        gl = abs(losses["pnl"].sum()) if len(losses) else 1
        cum = filled["pnl"].cumsum()
        dd = (cum - cum.cummax()).min()

        msg += f"━━━━━━━━━━━━━━━━━━━━\n"
        msg += f"📈 <b>{ticker}</b> — {INSTRUMENTS[ticker]['name']}\n"
        msg += f"━━━━━━━━━━━━━━━━━━━━\n"
        msg += f"  Trades : {n} (WR={len(wins)/n*100:.0f}%)\n"
        msg += f"  PF     : {gp/gl:.2f}\n"
        msg += f"  P&amp;L   : <b>${pnl:+,.0f}</b>\n"
        msg += f"  Max DD : ${dd:,.0f}\n"
        msg += f"  $/trade: ${filled['pnl'].mean():+.1f}\n"

        for r in ["TP", "SL", "TE"]:
            sub = filled[filled["result"] == r]
            if len(sub) > 0:
                msg += f"  {r}: n={len(sub)}  avg=${sub['pnl'].mean():+.0f}\n"
        msg += "\n"

    msg += f"━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"💰 <b>TOTAL: {total_trades} trades  |  ${total_pnl:+,.0f}</b>\n"

    return msg


def main():
    parser = argparse.ArgumentParser(description="Backtest stratégie ordres limites")
    parser.add_argument("--csv-dir", type=str, required=True,
                        help="Répertoire des fichiers CSV 15m")
    parser.add_argument("--ticker", type=str, default=None,
                        help="Actif unique (défaut: tous)")
    parser.add_argument("--output-dir", type=str, default="./output",
                        help="Répertoire de sortie")
    parser.add_argument("--plot", action="store_true",
                        help="Générer graphiques pour chaque trade rempli")
    parser.add_argument("--plot-filter", type=str, default="all",
                        choices=["all", "tp", "sl", "te", "win", "loss"],
                        help="Filtrer les trades à tracer (défaut: all)")
    parser.add_argument("--telegram", action="store_true",
                        help="Envoyer le rapport sur Telegram")
    args = parser.parse_args()

    # --telegram implique --plot
    if args.telegram:
        args.plot = True

    tickers = [args.ticker] if args.ticker else list(INSTRUMENTS.keys())
    output_dir = Path(args.output_dir)
    output_dir.mkdir(exist_ok=True)

    backtest_results = []
    all_chart_files = []

    for ticker in tickers:
        print(f"\n{'='*60}")
        print(f"  BACKTEST — {ticker} (RR={RR_TARGET[ticker]})")
        print(f"{'='*60}")

        csv_path = Path(args.csv_dir) / f"{ticker}_data_m15.csv"
        if not csv_path.exists():
            print(f"  [!] Fichier introuvable: {csv_path}")
            continue

        df_15m = load_csv(str(csv_path))
        tf = build_timeframes(df_15m)
        print(f"  {len(df_15m):,} bougies")

        print(f"  ▸ Exécution...")
        df_trades = run_backtest(df_15m, tf, ticker)
        audit(df_trades, ticker)
        print_stats(df_trades, ticker)

        # Collecter les résultats pour le rapport Telegram
        filled_trades = df_trades[df_trades["result"] != "NOT_FILLED"]
        backtest_results.append({"ticker": ticker, "filled": filled_trades})

        # Sauvegarde
        out_csv = output_dir / f"backtest_{ticker}.csv"
        df_trades.to_csv(out_csv, index=False)
        print(f"\n  ✓ {out_csv}")

        # Graphiques des trades
        if args.plot:
            chart_dir = output_dir / "backtest_charts" / ticker
            chart_dir.mkdir(parents=True, exist_ok=True)

            filled = df_trades[df_trades["result"] != "NOT_FILLED"].copy()

            # Appliquer le filtre
            pf = args.plot_filter
            if pf == "tp":
                filled = filled[filled["result"] == "TP"]
            elif pf == "sl":
                filled = filled[filled["result"] == "SL"]
            elif pf == "te":
                filled = filled[filled["result"] == "TE"]
            elif pf == "win":
                filled = filled[filled["pnl"] > 0]
            elif pf == "loss":
                filled = filled[filled["pnl"] <= 0]

            total = len(filled)
            if total == 0:
                print(f"  Aucun trade à tracer (filtre: {pf})")
            else:
                print(f"  ▸ Génération de {total} graphiques (filtre: {pf})...")
                for idx, (_, row) in enumerate(filled.iterrows()):
                    trade_dict = row.to_dict()
                    tag = trade_dict["result"].lower()
                    chart_path = str(chart_dir / f"{trade_dict['date']}_{tag}_{idx+1}.png")
                    plot_backtest_trade(df_15m, trade_dict, ticker, chart_path)
                    all_chart_files.append((chart_path, trade_dict))
                    if (idx + 1) % 25 == 0:
                        print(f"    {idx+1}/{total}...")
                print(f"  ✓ {total} graphiques → {chart_dir}")

    print(f"\n{'='*60}")
    print(f"  ✅ BACKTEST TERMINÉ")
    print(f"{'='*60}")

    # Envoi Telegram
    if args.telegram:
        from core.telegram import get_chat_id, send_message, send_photo

        try:
            chat_id = get_chat_id()

            # Résumé texte
            report = format_backtest_report(backtest_results)
            send_message(chat_id, report)
            print(f"\n  ✓ Rapport envoyé sur Telegram")

            # Graphiques
            total = len(all_chart_files)
            if total > 0:
                print(f"  ▸ Envoi de {total} graphiques...")
                for idx, (chart_path, trade_dict) in enumerate(all_chart_files):
                    ticker_t = trade_dict.get("date", "")
                    caption = (
                        f"{trade_dict['date']} {trade_dict['dir'].upper()} "
                        f"{trade_dict['result']} ${trade_dict['pnl']:+.0f}"
                    )
                    send_photo(chat_id, chart_path, caption=caption)
                    if (idx + 1) % 25 == 0:
                        print(f"    {idx+1}/{total}...")
                    time.sleep(0.05)
                print(f"  ✓ {total} graphiques envoyés")

        except Exception as e:
            print(f"  [!] Erreur Telegram: {e}")


if __name__ == "__main__":
    main()
