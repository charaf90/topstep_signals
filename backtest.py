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
    COMPOSITE_SCORE_MIN, TREND_STRENGTH_MIN,
    TOPSTEP_DAILY_LOSS_MAX, TOPSTEP_TRAILING_DD,
    TOPSTEP_PROFIT_TARGET,
    DAILY_STOP_AFTER_SL, CONSEC_LOSS_PAUSE_DAYS, DAILY_LOCKIN_THRESHOLD,
    STRATEGY_VERSION, ANALYSIS_CHARTS_ENABLED,
    OPR_ENABLED, OPR_STRATEGY_VERSION, OPR_MAX_TRADES_PER_DAY,
)
from core.data import load_csv, build_timeframes
from core.strategy import generate_signals, simulate_trade
from core.trend import precompute_trends, get_regime_with_score
from core.risk_topstep import trade_allowed
from core.chart import plot_signal, plot_backtest_trade
from core.analysis_chart import plot_day_analysis
from core.zones import detect_zones
from core.premarket import compute_features as compute_pm_features
from core.scoring import compute_volatility_features
from core.opr import run_opr_day
from zoneinfo import ZoneInfo
from config import OPR_TIMEZONE


def run_backtest(df_15m: pd.DataFrame, tf_dict: dict, ticker: str,
                 topstep_guard: bool = True,
                 analysis_chart_dir=None) -> pd.DataFrame:
    """
    Backtest complet jour par jour.
    Les ordres non remplis ne comptent PAS vers le max de 2 trades/jour.

    Circuit breakers appliqués dans l'ordre chronologique intra-jour :
      • DAILY_STOP_AFTER_SL — après 1 SL, annule les ordres restants
      • DAILY_LOCKIN_THRESHOLD — après gain cumulé ≥ seuil, plus de nouveau trade
      • CONSEC_LOSS_PAUSE_DAYS — saute la journée après N jours perdants d'affilée

    Args:
        topstep_guard : si True, vérifie le slack Topstep avant chaque journée
                         et saute le jour si le risque nominal dépasserait la limite.
    """
    dpp = INSTRUMENTS[ticker]["dollar_per_point"]
    trend_scores = precompute_trends(tf_dict)
    dates = df_15m.index.normalize().unique()
    trades = []

    # Trackers Topstep (sur la séquence backtest du ticker)
    cum_pnl = 0.0
    peak_pnl = 0.0
    consec_loss_days = 0

    for day in dates:
        ds = day.strftime("%Y-%m-%d")
        cutoff = pd.Timestamp(f"{ds} {CUTOFF_HOUR_UTC:02d}:00:00")
        us_start = pd.Timestamp(f"{ds} {US_SESSION_START_UTC:02d}:00:00")
        us_end = pd.Timestamp(f"{ds} {US_SESSION_END_UTC:02d}:00:00")

        us_data = df_15m[(df_15m.index >= us_start) & (df_15m.index <= us_end)]
        if len(us_data) < MIN_BARS_US_SESSION:
            continue

        # Circuit breaker : pause après N jours perdants consécutifs (saute 1 jour)
        if CONSEC_LOSS_PAUSE_DAYS > 0 and consec_loss_days >= CONSEC_LOSS_PAUSE_DAYS:
            consec_loss_days = 0  # reset : la pause d'un jour relance le cycle
            continue

        # Garde-fou Topstep : saute la journée si slack insuffisant
        if topstep_guard:
            allowed, _ = trade_allowed(day_pnl=0.0, cum_pnl=cum_pnl, peak_pnl=peak_pnl)
            if not allowed:
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
                "composite": sig.get("composite", 0),
                "alignment": sig.get("alignment", 0),
                "atr_ratio": sig.get("atr_ratio", 0),
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
        filled.sort(key=lambda t: t["fill_time"])

        # 4. Appliquer la limite MAX_TRADES_PER_DAY
        capped = filled[:MAX_TRADES_PER_DAY]
        cancelled_late = filled[MAX_TRADES_PER_DAY:]

        # 5. Circuit breakers intra-jour (daily stop + lock-in) appliqués chronologiquement
        kept_filled = []
        cancelled_cb = []
        running_pnl = 0.0
        breaker_armed = False
        for t in capped:
            if breaker_armed:
                cancelled_cb.append(t)
                continue
            kept_filled.append(t)
            running_pnl += t["pnl"]
            if DAILY_STOP_AFTER_SL and t["result"] == "SL":
                breaker_armed = True
            elif DAILY_LOCKIN_THRESHOLD > 0 and running_pnl >= DAILY_LOCKIN_THRESHOLD:
                breaker_armed = True

        # Les ordres "trop tard" et ceux coupés par un breaker deviennent NOT_FILLED
        for t in cancelled_late + cancelled_cb:
            t["result"] = "NOT_FILLED"
            t["pnl"] = 0
            t["fill_time"] = None
            t["exit_time"] = None
            t["exit"] = None

        trades.extend(kept_filled)
        trades.extend(not_filled)
        trades.extend(cancelled_late)
        trades.extend(cancelled_cb)

        # ── Graphique d'analyse journalier ──────────────────────────────
        # Une "photographie" complète du jour vu par la stratégie : zones
        # par TF, signaux générés, exécutions, contexte (regime, pm, vol).
        # Voir CLAUDE.md → "Graphiques d'analyse journaliers (consigne pérenne)".
        if analysis_chart_dir is not None and len(signals) > 0:
            try:
                day_zones = detect_zones(tf_dict, cutoff)
                day_regime, day_align = get_regime_with_score(trend_scores, cutoff)
                day_pm = compute_pm_features(df_15m, cutoff)
                day_vol = compute_volatility_features(df_15m, cutoff, ticker)
                chart_path = analysis_chart_dir / f"{ds}.png"
                plot_day_analysis(
                    df_15m=df_15m,
                    ticker=ticker,
                    date_str=ds,
                    cutoff=cutoff,
                    us_end=us_end,
                    zones=day_zones,
                    signals=signals,
                    trades=(kept_filled + cancelled_late + cancelled_cb + not_filled),
                    regime=day_regime,
                    alignment_score=day_align,
                    pm_features=day_pm,
                    vol_features=day_vol,
                    output_path=str(chart_path),
                )
            except Exception as e:
                print(f"  [!] analyse {ds}: {e}")

        # Mise à jour des trackers Topstep et du streak perdant
        day_pnl = sum(t["pnl"] for t in kept_filled)
        cum_pnl += day_pnl
        if cum_pnl > peak_pnl:
            peak_pnl = cum_pnl
        if day_pnl < 0:
            consec_loss_days += 1
        elif day_pnl > 0:
            consec_loss_days = 0
        # day_pnl == 0 (aucun trade rempli) : streak inchangé

    return pd.DataFrame(trades)


def run_opr_backtest(df_15m: pd.DataFrame, tf_dict: dict, ticker: str,
                     topstep_guard: bool = True,
                     analysis_chart_dir=None) -> pd.DataFrame:
    """
    Backtest de la stratégie OPR `opr-v2` (PineScript pullback).

    La logique métier vit dans core/opr.run_opr_day() : 1ère bougie 15m de
    la session NY (9h30-9h45) = zone OPR ; trigger pullback (open inside,
    close outside) → ordre limite ; SL/TP en distance fixe ; 1 position à
    la fois ; close all à 16h30 NY.

    Garde-fous Topstep et circuit breakers identiques au backtest composite
    pour cohérence si les deux stratégies tournent en parallèle.
    """
    tz = ZoneInfo(OPR_TIMEZONE)
    trend_scores = precompute_trends(tf_dict)

    # Construit la liste des jours de trading en référence NY pour éviter
    # les ambiguïtés DST.
    if df_15m.index.tz is None:
        idx_ny = df_15m.index.tz_localize("UTC").tz_convert(tz)
    else:
        idx_ny = df_15m.index.tz_convert(tz)
    ny_days = pd.DatetimeIndex(idx_ny.normalize().unique()).sort_values()

    trades_out = []
    cum_pnl = 0.0
    peak_pnl = 0.0
    consec_loss_days = 0

    for day_ny in ny_days:
        ds = day_ny.strftime("%Y-%m-%d")
        cutoff = pd.Timestamp(f"{ds} {CUTOFF_HOUR_UTC:02d}:00:00")
        # us_end pour le chart d'analyse : exprimé en UTC naïf comme l'index
        # source. 16h30 NY (DST-aware) → UTC naïf.
        h_close, m_close = 16, 30
        us_end_utc = (day_ny.replace(hour=h_close, minute=m_close)
                          .tz_convert("UTC").tz_localize(None))

        if CONSEC_LOSS_PAUSE_DAYS > 0 and consec_loss_days >= CONSEC_LOSS_PAUSE_DAYS:
            consec_loss_days = 0
            continue

        if topstep_guard:
            allowed, _ = trade_allowed(day_pnl=0.0, cum_pnl=cum_pnl, peak_pnl=peak_pnl)
            if not allowed:
                continue

        # Régime composite (info contextuelle pour les charts/audit)
        regime, _ = get_regime_with_score(trend_scores, cutoff)

        signals, sim_results, opr_zone = run_opr_day(df_15m, ticker, day_ny)
        if not signals:
            continue

        # Convertit en lignes de trade pour le DataFrame de sortie
        day_trades = []
        for sig, res in zip(signals, sim_results):
            row = {
                "date": ds,
                "strategy": "OPR",
                "dir": sig["direction"],
                "entry": sig["entry"],
                "sl": sig["sl"],
                "tp": sig["tp"],
                "sl_dist": sig["sl_dist"],
                "tp_dist": sig["tp_dist"],
                "rr": sig["rr"],
                "n_ct": sig["n_ct"],
                "risk_$": sig["risk"],
                "quality": 0.0,
                "composite": 0.0,
                "alignment": 0.0,
                "atr_ratio": 0.0,
                "n_tf": 1,
                "touches": 0,
                "regime": regime or "?",
                "zone_low": sig["zone_low"],
                "zone_high": sig["zone_high"],
                "tp_type": sig.get("tp_type", "fixed"),
                "trigger_time": sig["trigger_time"],
                **res,
            }
            day_trades.append(row)

        # Circuit breakers intra-jour (chronologiquement, sur les fills)
        filled = [t for t in day_trades if t["result"] != "NOT_FILLED"]
        not_filled = [t for t in day_trades if t["result"] == "NOT_FILLED"]
        filled.sort(key=lambda t: t["fill_time"] or "")

        kept = []
        cancelled_cb = []
        running_pnl = 0.0
        breaker_armed = False
        for t in filled:
            if breaker_armed:
                cancelled_cb.append(t)
                continue
            kept.append(t)
            running_pnl += t["pnl"]
            if DAILY_STOP_AFTER_SL and t["result"] == "SL":
                breaker_armed = True
            elif DAILY_LOCKIN_THRESHOLD > 0 and running_pnl >= DAILY_LOCKIN_THRESHOLD:
                breaker_armed = True

        for t in cancelled_cb:
            t["result"] = "NOT_FILLED"
            t["pnl"] = 0
            t["fill_time"] = None
            t["exit_time"] = None
            t["exit"] = None

        trades_out.extend(kept)
        trades_out.extend(not_filled)
        trades_out.extend(cancelled_cb)

        # Graphique d'analyse OPR (réutilise plot_day_analysis si activé)
        if analysis_chart_dir is not None and (signals or opr_zone is not None):
            try:
                zones_for_chart = []
                if opr_zone is not None:
                    zones_for_chart.append({
                        "low": opr_zone["low"],
                        "high": opr_zone["high"],
                        "mid": opr_zone["mid"],
                        "quality": 100.0,
                        "n_tf": 1,
                        "touches": 1,
                        "tfs": ["OPR"],
                        "dominant_tf": "OPR",
                        # `start_time` borne la zone à partir de l'ouverture
                        # OPR — le chart ne dessinera rien avant. La valeur
                        # est en UTC naïf (homogène avec df_15m.index).
                        "start_time": opr_zone["time_utc"],
                    })
                day_pm = compute_pm_features(df_15m, cutoff)
                day_vol = compute_volatility_features(df_15m, cutoff, ticker)
                chart_path = analysis_chart_dir / f"{ds}.png"
                plot_day_analysis(
                    df_15m=df_15m,
                    ticker=ticker,
                    date_str=ds,
                    cutoff=cutoff,
                    us_end=us_end_utc,
                    zones=zones_for_chart,
                    signals=signals,
                    trades=(kept + cancelled_cb + not_filled),
                    regime=regime,
                    alignment_score=None,
                    pm_features=day_pm,
                    vol_features=day_vol,
                    output_path=str(chart_path),
                )
            except Exception as e:
                print(f"  [!] analyse OPR {ds}: {e}")

        day_pnl = sum(t["pnl"] for t in kept)
        cum_pnl += day_pnl
        if cum_pnl > peak_pnl:
            peak_pnl = cum_pnl
        if day_pnl < 0:
            consec_loss_days += 1
        elif day_pnl > 0:
            consec_loss_days = 0

    return pd.DataFrame(trades_out)


def audit(df_trades: pd.DataFrame, ticker: str) -> bool:
    """
    Vérifie l'intégrité des trades.

    Les contrôles SL_MINIMUM et alignement régime ne s'appliquent qu'à la
    stratégie composite — ils n'ont pas de sens pour la stratégie OPR
    (SL fixe en points, prises de position à contre-tendance autorisées).
    """
    dpp = INSTRUMENTS[ticker]["dollar_per_point"]
    sl_min = SL_MINIMUM[ticker]
    if len(df_trades) == 0 or "result" not in df_trades.columns:
        print(f"  AUDIT: ⚠ aucun trade généré (filtre composite trop strict ?)")
        return True
    filled = df_trades[df_trades["result"] != "NOT_FILLED"]
    if len(filled) == 0:
        print(f"  AUDIT: ⚠ aucun fill")
        return True
    is_opr = "strategy" in filled.columns and (filled["strategy"] == "OPR").all()
    errors = 0

    # P&L (toujours appliqué)
    for _, r in filled.iterrows():
        if r.get("scale_in", False):
            continue
        if r["dir"] == "long":
            exp = r["n_ct"] * (r["exit"] - r["entry"]) * dpp
        else:
            exp = r["n_ct"] * (r["entry"] - r["exit"]) * dpp
        if abs(exp - r["pnl"]) > 1:
            errors += 1

    # Contrôles spécifiques composite (skip pour OPR)
    if not is_opr:
        if (filled["sl_dist"] < sl_min - 0.01).any():
            errors += 1
        for _, r in filled.iterrows():
            if r["dir"] == "long" and r["regime"] == "BEAR":
                errors += 1
            if r["dir"] == "short" and r["regime"] == "BULL":
                errors += 1

    print(f"  AUDIT: {'✅ OK' if errors == 0 else f'❌ {errors} erreurs'}")
    return errors == 0


def print_stats(df_trades: pd.DataFrame, ticker: str):
    """Rapport statistique."""
    if len(df_trades) == 0 or "result" not in df_trades.columns:
        print(f"  Aucun signal généré")
        return
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


def validate_topstep(df_trades: pd.DataFrame, n_bootstrap: int = 1000) -> dict:
    """
    Métriques spécifiques au challenge Topstep.

    Calcule le DD trailing (peak_pnl - cum_pnl) simulé, la perte journalière max,
    le nombre max de jours consécutifs perdants, le taux de journées gagnantes,
    et un bootstrap challenge (X% de permutations qui atteignent le target sans
    violation des limites).
    """
    if len(df_trades) == 0 or "result" not in df_trades.columns:
        return {"passed": False, "reason": "no_trades"}
    filled = df_trades[df_trades["result"] != "NOT_FILLED"].copy()
    if len(filled) == 0:
        return {"passed": False, "reason": "no_trades"}

    # Agrégation par jour
    daily = filled.groupby("date")["pnl"].sum().sort_index()
    days = daily.values

    # Séquence Topstep (ordre chronologique)
    cum = np.cumsum(days)
    peak = np.maximum.accumulate(cum)
    trailing_dd = (cum - peak).min()          # valeur ≤ 0
    max_daily_loss = days.min()               # valeur ≤ 0
    max_daily_gain = days.max()

    # Jours consécutifs perdants
    max_consec_loss = 0
    cur = 0
    for v in days:
        if v < 0:
            cur += 1
            max_consec_loss = max(max_consec_loss, cur)
        else:
            cur = 0

    n_days = len(days)
    n_win = int((days > 0).sum())
    n_flat = int((days == 0).sum())
    winning_ratio = n_win / n_days if n_days > 0 else 0.0

    # Bootstrap challenge : reshuffle l'ordre des jours
    rng = np.random.default_rng(42)
    pass_count = 0
    for _ in range(n_bootstrap):
        perm = rng.permutation(days)
        c = 0.0
        p = 0.0
        ok = True
        reached = False
        for d in perm:
            # Violation daily loss
            if d <= -TOPSTEP_DAILY_LOSS_MAX:
                ok = False; break
            c += d
            if c > p:
                p = c
            # Violation trailing DD
            if (p - c) >= TOPSTEP_TRAILING_DD:
                ok = False; break
            if c >= TOPSTEP_PROFIT_TARGET:
                reached = True
                break
        if ok and reached:
            pass_count += 1

    bootstrap_pass_rate = pass_count / n_bootstrap if n_bootstrap > 0 else 0.0

    return {
        "n_days": n_days,
        "n_winning_days": n_win,
        "n_flat_days": n_flat,
        "winning_ratio": winning_ratio,
        "trailing_dd": float(trailing_dd),
        "max_daily_loss": float(max_daily_loss),
        "max_daily_gain": float(max_daily_gain),
        "max_consec_loss": int(max_consec_loss),
        "bootstrap_pass_rate": bootstrap_pass_rate,
        "violates_daily": bool(max_daily_loss <= -TOPSTEP_DAILY_LOSS_MAX),
        "violates_trailing": bool(trailing_dd <= -TOPSTEP_TRAILING_DD),
    }


def print_topstep_report(ts: dict, ticker: str):
    """Affiche les métriques Topstep pour un ticker."""
    print(f"\n  {'─'*55}")
    print(f"  TOPSTEP — Validation challenge 50K ({ticker})")
    print(f"  {'─'*55}")
    if ts.get("reason") == "no_trades":
        print(f"  Aucun trade")
        return
    flag_d = "❌" if ts["violates_daily"] else "✅"
    flag_t = "❌" if ts["violates_trailing"] else "✅"
    print(f"  Jours         : {ts['n_days']} ({ts['n_winning_days']} win, ratio {ts['winning_ratio']*100:.0f}%)")
    print(f"  Perte jour max: ${ts['max_daily_loss']:+,.0f}   {flag_d} (limite -${TOPSTEP_DAILY_LOSS_MAX})")
    print(f"  Trailing DD   : ${ts['trailing_dd']:+,.0f}   {flag_t} (limite -${TOPSTEP_TRAILING_DD})")
    print(f"  Consec. loss  : {ts['max_consec_loss']} jours")
    print(f"  Bootstrap pass: {ts['bootstrap_pass_rate']*100:.1f}%  (cible ≥ 80%)")


def portfolio_topstep_report(results: list, n_bootstrap: int = 1000):
    """
    Validation Topstep sur le portefeuille global (P&L journalier agrégé
    sur les 3 actifs, c'est ainsi que le broker Topstep le voit).
    """
    all_days = {}
    for r in results:
        filled = r["filled"]
        if filled is None or len(filled) == 0:
            continue
        for _, row in filled.iterrows():
            d = row["date"]
            all_days[d] = all_days.get(d, 0.0) + float(row["pnl"])

    if not all_days:
        print("\n  PORTEFEUILLE — Aucun trade")
        return

    daily = pd.Series(all_days).sort_index()
    days = daily.values

    cum = np.cumsum(days)
    peak = np.maximum.accumulate(cum)
    trailing_dd = (cum - peak).min()
    max_daily_loss = days.min()
    max_daily_gain = days.max()
    n_win = int((days > 0).sum())
    winning_ratio = n_win / len(days)

    max_consec = 0
    cur = 0
    for v in days:
        if v < 0:
            cur += 1
            max_consec = max(max_consec, cur)
        else:
            cur = 0

    # Bootstrap portfolio
    rng = np.random.default_rng(42)
    pass_count = 0
    for _ in range(n_bootstrap):
        perm = rng.permutation(days)
        c, p = 0.0, 0.0
        ok, reached = True, False
        for d in perm:
            if d <= -TOPSTEP_DAILY_LOSS_MAX:
                ok = False; break
            c += d
            if c > p: p = c
            if (p - c) >= TOPSTEP_TRAILING_DD:
                ok = False; break
            if c >= TOPSTEP_PROFIT_TARGET:
                reached = True; break
        if ok and reached:
            pass_count += 1
    boot = pass_count / n_bootstrap if n_bootstrap > 0 else 0.0

    total_pnl = float(cum[-1])
    flag_d = "❌" if max_daily_loss <= -TOPSTEP_DAILY_LOSS_MAX else "✅"
    flag_t = "❌" if trailing_dd <= -TOPSTEP_TRAILING_DD else "✅"
    print(f"\n  {'═'*55}")
    print(f"  PORTEFEUILLE — TOPSTEP 50K (3 actifs agrégés)")
    print(f"  {'═'*55}")
    print(f"  Jours         : {len(days)} ({n_win} win, ratio {winning_ratio*100:.0f}%)")
    print(f"  P&L total     : ${total_pnl:+,.0f}")
    print(f"  Perte jour max: ${max_daily_loss:+,.0f}   {flag_d} (limite -${TOPSTEP_DAILY_LOSS_MAX})")
    print(f"  Gain jour max : ${max_daily_gain:+,.0f}")
    print(f"  Trailing DD   : ${trailing_dd:+,.0f}   {flag_t} (limite -${TOPSTEP_TRAILING_DD})")
    print(f"  Consec. loss  : {max_consec} jours")
    print(f"  Bootstrap pass: {boot*100:.1f}%  (cible ≥ 80%, target $+{TOPSTEP_PROFIT_TARGET})")


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


def _run_strategy_for_ticker(strategy: str, ticker: str, df_15m, tf,
                             args, output_dir: Path) -> dict:
    """
    Exécute une stratégie ('composite' ou 'opr') pour un ticker, gère le
    dossier de graphiques d'analyse, l'audit, les stats et la sauvegarde.
    Retourne {df_trades, filled, label, version}.
    """
    is_opr = strategy == "opr"
    label = "OPR" if is_opr else "COMPOSITE"
    version = OPR_STRATEGY_VERSION if is_opr else STRATEGY_VERSION

    analysis_dir = None
    if ANALYSIS_CHARTS_ENABLED and not args.no_analysis_charts:
        analysis_dir = output_dir / "analysis_charts" / version / ticker
        analysis_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n  ▸ [{label}] Exécution...")
    if is_opr:
        df_trades = run_opr_backtest(df_15m, tf, ticker,
                                     analysis_chart_dir=analysis_dir)
    else:
        df_trades = run_backtest(df_15m, tf, ticker,
                                 analysis_chart_dir=analysis_dir)

    if analysis_dir is not None:
        n_charts = len(list(analysis_dir.glob("*.png")))
        print(f"  ✓ [{label}] {n_charts} graphique(s) d'analyse → {analysis_dir}")

    audit(df_trades, ticker)
    print_stats(df_trades, ticker)
    ts = validate_topstep(df_trades, n_bootstrap=1000)
    print_topstep_report(ts, ticker)

    if len(df_trades) > 0 and "result" in df_trades.columns:
        filled = df_trades[df_trades["result"] != "NOT_FILLED"]
    else:
        filled = pd.DataFrame()

    suffix = "_opr" if is_opr else ""
    out_csv = output_dir / f"backtest_{ticker}{suffix}.csv"
    df_trades.to_csv(out_csv, index=False)
    print(f"  ✓ {out_csv}")

    return {"df_trades": df_trades, "filled": filled,
            "label": label, "version": version}


def main():
    parser = argparse.ArgumentParser(description="Backtest stratégie ordres limites")
    parser.add_argument("--csv-dir", type=str, required=True,
                        help="Répertoire des fichiers CSV 15m")
    parser.add_argument("--ticker", type=str, default=None,
                        help="Actif unique (défaut: tous)")
    parser.add_argument("--output-dir", type=str, default="./output",
                        help="Répertoire de sortie")
    parser.add_argument("--strategy", type=str, default="both",
                        choices=["composite", "opr", "both"],
                        help="Stratégie à backtester (défaut: both — lance "
                             "composite + OPR en parallèle).")
    parser.add_argument("--plot", action="store_true",
                        help="Générer graphiques pour chaque trade rempli")
    parser.add_argument("--plot-filter", type=str, default="all",
                        choices=["all", "tp", "sl", "te", "win", "loss"],
                        help="Filtrer les trades à tracer (défaut: all)")
    parser.add_argument("--telegram", action="store_true",
                        help="Envoyer le rapport sur Telegram")
    parser.add_argument("--no-analysis-charts", action="store_true",
                        help="Désactive les graphiques d'analyse journaliers "
                             "(activés par défaut, voir ANALYSIS_CHARTS_ENABLED).")
    args = parser.parse_args()

    # --telegram implique --plot
    if args.telegram:
        args.plot = True

    tickers = [args.ticker] if args.ticker else list(INSTRUMENTS.keys())
    output_dir = Path(args.output_dir)
    output_dir.mkdir(exist_ok=True)

    if args.strategy == "both":
        strategies = ["composite", "opr"]
    else:
        strategies = [args.strategy]

    # Résultats par stratégie : {strategy: [{ticker, filled}, ...]}
    results_by_strategy = {s: [] for s in strategies}
    all_chart_files = []

    for ticker in tickers:
        print(f"\n{'='*60}")
        print(f"  BACKTEST — {ticker} (composite RR={RR_TARGET[ticker]})")
        print(f"{'='*60}")

        csv_path = Path(args.csv_dir) / f"{ticker}_data_m15.csv"
        if not csv_path.exists():
            print(f"  [!] Fichier introuvable: {csv_path}")
            continue

        df_15m = load_csv(str(csv_path))
        tf = build_timeframes(df_15m)
        print(f"  {len(df_15m):,} bougies")

        df_trades = None  # référence vers le composite pour le bloc --plot
        for strat in strategies:
            res = _run_strategy_for_ticker(strat, ticker, df_15m, tf, args, output_dir)
            results_by_strategy[strat].append({"ticker": ticker,
                                               "filled": res["filled"]})
            if strat == "composite":
                df_trades = res["df_trades"]
        if df_trades is None:
            # --strategy opr seul → pas de chart per-trade composite à tracer
            df_trades = pd.DataFrame()

        # Graphiques par trade (composite uniquement, conservés pour compat)
        if args.plot and "composite" in strategies and len(df_trades) > 0:
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

    # Validation Topstep portefeuille (par stratégie + combiné si both)
    for strat in strategies:
        print(f"\n{'#'*60}")
        print(f"  PORTEFEUILLE — Stratégie {strat.upper()}")
        print(f"{'#'*60}")
        portfolio_topstep_report(results_by_strategy[strat], n_bootstrap=1000)

    if len(strategies) > 1:
        print(f"\n{'#'*60}")
        print(f"  PORTEFEUILLE — STRATÉGIES COMBINÉES (composite + opr)")
        print(f"{'#'*60}")
        combined = []
        for strat in strategies:
            for r in results_by_strategy[strat]:
                combined.append(r)
        portfolio_topstep_report(combined, n_bootstrap=1000)

    print(f"\n{'='*60}")
    print(f"  ✅ BACKTEST TERMINÉ")
    print(f"{'='*60}")

    # Envoi Telegram (utilise le composite par défaut, puis OPR si présent)
    backtest_results = results_by_strategy.get("composite",
                                               results_by_strategy.get("opr", []))
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
