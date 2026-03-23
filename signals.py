#!/usr/bin/env python3
"""
Générateur de signaux + envoi Telegram.

Usage :
  python signals.py                      # Live (TradingView) + envoi Telegram
  python signals.py --dry-run            # Live, sans envoyer
  python signals.py --csv-dir ./data     # Depuis CSV locaux
  python signals.py --date 2026-01-29    # Simuler une date passée (CSV requis)
"""

import argparse
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

from config import (
    INSTRUMENTS, RR_TARGET, MAX_TRADES_PER_DAY, CUTOFF_HOUR_UTC,
)
from core.data import load_all
from core.strategy import generate_signals
from core.trend import precompute_trends
from core.chart import plot_signal
from core.telegram import get_chat_id, send_message, send_photo


# ==============================================================================
# FORMATAGE DU MESSAGE
# ==============================================================================

def format_message(all_signals: dict, date_str: str) -> str:
    """Formate le message Telegram."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    jours = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]

    n_total = sum(len(s) for s in all_signals.values())
    msg = f"🔔 <b>SIGNAUX — {jours[dt.weekday()]} {date_str}</b>\n\n"

    if n_total == 0:
        msg += "⚪ Aucun signal aujourd'hui\n"
        return msg

    msg += f"📋 {n_total} signal(s)\n\n"

    for ticker, signals in all_signals.items():
        for s in signals:
            arrow = "▲" if s["direction"] == "long" else "▼"
            d = "LONG" if s["direction"] == "long" else "SHORT"
            re = "🟢" if s["regime"] == "BULL" else "🔴" if s["regime"] == "BEAR" else "🟡"

            msg += f"━━━━━━━━━━━━━━━━━━━━\n"
            msg += f"📊 <b>{ticker} — {arrow} {d}</b>  (RR={s['rr']})\n"
            msg += f"━━━━━━━━━━━━━━━━━━━━\n"
            msg += f"🎯 Entry   : <code>{s['entry']:.2f}</code>\n"
            if s.get("scale_in"):
                msg += f"🎯 Entry 2 : <code>{s['entry_2']:.2f}</code>\n"
            msg += f"🛑 SL      : <code>{s['sl']:.2f}</code> ({s['sl_dist']:.1f} pts)\n"
            msg += f"✅ TP      : <code>{s['tp']:.2f}</code> ({s['tp_dist']:.1f} pts)\n"
            if s.get("scale_in"):
                msg += f"📦 Contrats: {s['n_ct_1']}+{s['n_ct_2']} micro(s)\n"
            else:
                msg += f"📦 Contrats: {s['n_ct']} micro(s)\n"
            msg += f"💰 Risque  : ${s['risk']:.0f}\n"
            msg += f"💵 Gain    : ${s['gain']:.0f}\n"
            msg += f"{re} Régime  : {s['regime']}\n"
            msg += f"⭐ Zone    : Q={s['quality']:.0f} | {s['n_tf']}TF | {s['touches']}t\n\n"

    msg += f"━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"⚠️ Ordres entre 12h-13h Paris\n"
    msg += f"📌 <b>NE PAS MODIFIER</b> les ordres\n"

    return msg


# ==============================================================================
# MAIN
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(description="Générateur de signaux Topstep")
    parser.add_argument("--csv-dir", type=str, default=None,
                        help="Répertoire des CSV (mode offline)")
    parser.add_argument("--date", type=str, default=None,
                        help="Simuler une date passée (format YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Ne pas envoyer sur Telegram")
    parser.add_argument("--output-dir", type=str, default="./output",
                        help="Répertoire de sortie des graphiques")
    args = parser.parse_args()

    tickers = list(INSTRUMENTS.keys())
    output_dir = Path(args.output_dir)
    output_dir.mkdir(exist_ok=True)

    # Date
    if args.date:
        date_str = args.date
    else:
        date_str = datetime.utcnow().strftime("%Y-%m-%d")

    cutoff = pd.Timestamp(f"{date_str} {CUTOFF_HOUR_UTC:02d}:00:00")

    print(f"\n{'='*50}")
    print(f"  SIGNAUX — {date_str} (cutoff {cutoff})")
    print(f"{'='*50}")

    # Chargement données
    is_live = args.csv_dir is None and args.date is None
    all_tf = load_all(tickers, data_dir=args.csv_dir, live=is_live)

    # Génération des signaux
    all_signals = {}
    chart_files = []

    for ticker in tickers:
        if ticker not in all_tf:
            all_signals[ticker] = []
            continue

        tf = all_tf[ticker]
        trend_scores = precompute_trends(tf)
        df_15m = tf["15m"]

        signals = generate_signals(df_15m, tf, ticker, cutoff, trend_scores,
                                   max_signals=MAX_TRADES_PER_DAY)
        all_signals[ticker] = signals
        print(f"  {ticker}: {len(signals)} signal(s)")

        # Graphiques
        for i, sig in enumerate(signals):
            chart_path = str(output_dir / f"{date_str}_{ticker}_signal{i+1}.png")
            plot_signal(df_15m, sig, cutoff, chart_path)
            chart_files.append(chart_path)
            print(f"    ✓ {chart_path}")

    # Message
    msg = format_message(all_signals, date_str)
    print(f"\n{msg}")

    # Envoi Telegram
    if not args.dry_run:
        try:
            chat_id = get_chat_id()
            send_message(chat_id, msg)
            print(f"  ✓ Message envoyé")

            for chart in chart_files:
                send_photo(chat_id, chart)
                print(f"  ✓ Graphique envoyé: {Path(chart).name}")

        except Exception as e:
            print(f"  [!] Erreur Telegram: {e}")
    else:
        print("  (dry-run — pas d'envoi Telegram)")

    # Sauvegarder le message
    msg_path = output_dir / f"{date_str}_signals.txt"
    msg_path.write_text(msg, encoding="utf-8")

    print(f"\n✅ Terminé")


if __name__ == "__main__":
    main()
