#!/usr/bin/env python3
"""
Backtest engine pour la stratégie Fibonacci 50% retracement (M15).

Boucle barre par barre — état interne minimal :
  - `position`  : trade en cours (None si flat)
  - `pending`   : ordre limite en attente de fill
  - `last_impulse_key` : (direction, pivot_low_idx, pivot_high_idx) du dernier
                         impulse tradé, pour éviter de re-trader la même configuration

Usage :
    python backtest.py --csv-dir ../data
    python backtest.py --csv-dir ../data --ticker NQ1
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from config import (
    INSTRUMENTS, RISK_PER_TRADE_USD, ACCOUNT_SIZE,
    ATR_PERIOD, EMA_FAST_PERIOD, EMA_SLOW_PERIOD, ADX_PERIOD,
    ADX_TREND_THRESHOLD, PIVOT_LEFT, PIVOT_RIGHT,
    MIN_IMPULSE_ATR, MAX_IMPULSE_BARS, IMPULSE_LOOKBACK,
    ORDER_TIMEOUT_BARS, MAX_HOLD_BARS, SL_ATR_MULT, TP_ATR_MULT,
    SL_ATR_MULT_PER_TICKER, TP_ATR_MULT_PER_TICKER, MIN_IMPULSE_ATR_PER_TICKER,
    TRIGGER_FILTERS_PER_TICKER,
    US_SESSION_START_UTC, US_SESSION_END_UTC,
    IS_END, SHARPE_ANNUALIZATION,
)
from strategy import (
    compute_atr, compute_ema, compute_adx, detect_pivots,
    detect_trend, find_last_impulse, build_signal,
)


# ─────────────────────────────────────────────────────────────────────────────
# I/O données
# ─────────────────────────────────────────────────────────────────────────────

def load_csv(path: str) -> pd.DataFrame:
    """Charge un CSV M15 au format projet (datetime + OHLCV)."""
    df = pd.read_csv(path, parse_dates=["datetime"])
    df = df.drop_duplicates(subset=["datetime"], keep="last")
    df = df.sort_values("datetime").set_index("datetime")
    return df[["open", "high", "low", "close", "volume"]]


# ─────────────────────────────────────────────────────────────────────────────
# Backtest core
# ─────────────────────────────────────────────────────────────────────────────

def _default_config(ticker: str = None) -> dict:
    """
    Retourne la config par défaut. Si `ticker` est fourni, essaie de pulluler
    les paramètres optimisés par-ticker (SL/TP/MIN_IMPULSE) ; sinon utilise
    les valeurs scalaires de fallback.
    """
    if ticker is not None:
        sl = SL_ATR_MULT_PER_TICKER.get(ticker, SL_ATR_MULT)
        tp = TP_ATR_MULT_PER_TICKER.get(ticker, TP_ATR_MULT)
        imp = MIN_IMPULSE_ATR_PER_TICKER.get(ticker, MIN_IMPULSE_ATR)
    else:
        sl, tp, imp = SL_ATR_MULT, TP_ATR_MULT, MIN_IMPULSE_ATR

    return {
        "ATR_PERIOD": ATR_PERIOD,
        "EMA_FAST_PERIOD": EMA_FAST_PERIOD,
        "EMA_SLOW_PERIOD": EMA_SLOW_PERIOD,
        "ADX_PERIOD": ADX_PERIOD,
        "ADX_TREND_THRESHOLD": ADX_TREND_THRESHOLD,
        "PIVOT_LEFT": PIVOT_LEFT,
        "PIVOT_RIGHT": PIVOT_RIGHT,
        "MIN_IMPULSE_ATR": imp,
        "MAX_IMPULSE_BARS": MAX_IMPULSE_BARS,
        "IMPULSE_LOOKBACK": IMPULSE_LOOKBACK,
        "ORDER_TIMEOUT_BARS": ORDER_TIMEOUT_BARS,
        "MAX_HOLD_BARS": MAX_HOLD_BARS,
        "SL_ATR_MULT": sl,
        "TP_ATR_MULT": tp,
        "RISK_PER_TRADE_USD": RISK_PER_TRADE_USD,
    }


def run_backtest(df: pd.DataFrame, ticker: str,
                 config_overrides: dict = None) -> pd.DataFrame:
    """
    Exécute le backtest complet sur la série M15 fournie.
    Retourne un DataFrame de trades (1 ligne / trade clos).
    Si `config_overrides` est None, utilise les paramètres optimisés du
    ticker (depuis config.py). Sinon, surcharge avec les valeurs fournies
    (utile pour l'optimiseur).
    """
    if config_overrides is None:
        config = _default_config(ticker)
    else:
        config = _default_config()
        config.update(config_overrides)

    df = df.copy()

    # ── Indicateurs ──────────────────────────────────────────────────────
    df["atr"] = compute_atr(df, config["ATR_PERIOD"])
    df["ema_fast"] = compute_ema(df["close"], config["EMA_FAST_PERIOD"])
    df["ema_slow"] = compute_ema(df["close"], config["EMA_SLOW_PERIOD"])
    df["adx"] = compute_adx(df, config["ADX_PERIOD"])

    # ── Pivots (calculés en avance pour gain de perf) ────────────────────
    pivot_highs, pivot_lows = detect_pivots(
        df, config["PIVOT_LEFT"], config["PIVOT_RIGHT"]
    )

    # ── Filtre session US (cohérent projet principal) ────────────────────
    hours = df.index.hour
    in_session_arr = (
        (hours >= US_SESSION_START_UTC)
        & (hours < US_SESSION_END_UTC)
    )
    # `df.index.hour` peut retourner numpy.ndarray (pandas 3.x) — on
    # accède donc par index entier dans la boucle.

    n = len(df)
    trades = []
    pending = None
    position = None
    last_impulse_key = None

    warmup = max(config["EMA_SLOW_PERIOD"], 250)

    for i in range(warmup, n):
        bar = df.iloc[i]

        # ── 1. Gestion position ouverte : check SL / TP / timeout ───────
        if position is not None:
            exit_price = None
            exit_reason = None

            if position["direction"] == "long":
                hit_sl = bar["low"] <= position["sl"]
                hit_tp = bar["high"] >= position["tp"]
            else:
                hit_sl = bar["high"] >= position["sl"]
                hit_tp = bar["low"] <= position["tp"]

            if hit_sl and hit_tp:
                # Tranche par direction de la bougie (TP autorisé seulement
                # si la bougie va dans le sens du trade — règle conservatrice).
                bull_bar = bar["close"] >= bar["open"]
                if position["direction"] == "long":
                    exit_price, exit_reason = (
                        (position["tp"], "TP") if bull_bar
                        else (position["sl"], "SL")
                    )
                else:
                    exit_price, exit_reason = (
                        (position["tp"], "TP") if not bull_bar
                        else (position["sl"], "SL")
                    )
            elif hit_sl:
                exit_price, exit_reason = position["sl"], "SL"
            elif hit_tp:
                exit_price, exit_reason = position["tp"], "TP"

            # Timeout (max hold)
            bars_held = i - position["fill_idx"]
            if exit_reason is None and bars_held >= config["MAX_HOLD_BARS"]:
                exit_price, exit_reason = float(bar["close"]), "TE"

            if exit_reason is not None:
                pnl_pts = (
                    (exit_price - position["entry"])
                    if position["direction"] == "long"
                    else (position["entry"] - exit_price)
                )
                dpp = INSTRUMENTS[position["ticker"]]["dollar_per_point"]
                pnl = position["n_ct"] * pnl_pts * dpp
                trades.append({
                    **{k: v for k, v in position.items() if k != "pending_idx"},
                    "exit": float(exit_price),
                    "exit_idx": int(i),
                    "exit_time": str(df.index[i]),
                    "result": exit_reason,
                    "pnl": float(pnl),
                    "bars_held": int(bars_held),
                })
                position = None
                continue
            else:
                continue  # position ouverte, rien d'autre à faire

        # ── 2. Check fill ordre limite pendant ──────────────────────────
        if pending is not None:
            level = pending["entry"]
            if bar["low"] <= level <= bar["high"]:
                # Fill → ouverture position
                position = {
                    **{k: v for k, v in pending.items() if k != "pending_idx"},
                    "fill_idx": int(i),
                    "fill_time": str(df.index[i]),
                }
                pending_idx_save = pending.get("pending_idx", i)
                pending = None

                # Vérification immédiate SL/TP sur la même bougie de fill
                if position["direction"] == "long":
                    hit_sl = bar["low"] <= position["sl"]
                    hit_tp = bar["high"] >= position["tp"]
                else:
                    hit_sl = bar["high"] >= position["sl"]
                    hit_tp = bar["low"] <= position["tp"]

                exit_price, exit_reason = None, None
                if hit_sl and hit_tp:
                    # Sur la bougie de fill, on privilégie SL (conservateur)
                    exit_price, exit_reason = position["sl"], "SL"
                elif hit_sl:
                    exit_price, exit_reason = position["sl"], "SL"
                elif hit_tp:
                    exit_price, exit_reason = position["tp"], "TP"

                if exit_reason is not None:
                    pnl_pts = (
                        (exit_price - position["entry"])
                        if position["direction"] == "long"
                        else (position["entry"] - exit_price)
                    )
                    dpp = INSTRUMENTS[position["ticker"]]["dollar_per_point"]
                    pnl = position["n_ct"] * pnl_pts * dpp
                    trades.append({
                        **position,
                        "exit": float(exit_price),
                        "exit_idx": int(i),
                        "exit_time": str(df.index[i]),
                        "result": exit_reason,
                        "pnl": float(pnl),
                        "bars_held": 0,
                    })
                    position = None
                continue

            # Pas de fill : check timeout
            bars_pending = i - pending["pending_idx"]
            if bars_pending >= config["ORDER_TIMEOUT_BARS"]:
                pending = None  # ordre annulé silencieusement (pas de log)

        # ── 3. Génération nouveau signal ────────────────────────────────
        if pending is not None or position is not None:
            continue

        if not bool(in_session_arr[i]):
            continue

        atr_now = bar["atr"]
        if pd.isna(atr_now) or atr_now <= 0:
            continue

        trend = detect_trend(
            float(bar["close"]), float(bar["ema_fast"]), float(bar["ema_slow"]),
            float(bar["adx"]) if not pd.isna(bar["adx"]) else np.nan,
            config["ADX_TREND_THRESHOLD"]
        )
        if trend == "RANGE":
            continue

        impulse = find_last_impulse(
            df, i, pivot_highs, pivot_lows, df["atr"], trend, config
        )
        if impulse is None:
            continue

        # Évite de re-trader la même impulse
        impulse_key = (
            impulse["direction"],
            impulse["pivot_low_idx"],
            impulse["pivot_high_idx"],
        )
        if impulse_key == last_impulse_key:
            continue

        # Si le prix a déjà dépassé fib_50 dans le sens du trade, on rate la zone d'entrée
        # (le pullback est terminé — pas la peine d'armer un ordre limite qui ne fillerait
        # qu'à la prochaine remontée vers fib_50, contraire à la logique pullback)
        if impulse["direction"] == "long" and bar["close"] <= impulse["fib_50"]:
            continue
        if impulse["direction"] == "short" and bar["close"] >= impulse["fib_50"]:
            continue

        sig = build_signal(impulse, float(atr_now), ticker, config, INSTRUMENTS)
        if sig is None:
            continue

        sig["pending_idx"] = int(i)
        sig["pending_time"] = str(df.index[i])
        sig["impulse_confirm_idx"] = impulse["confirm_idx"]
        sig["trend"] = trend

        # ── Features additionnelles au moment de l'armement (analyse filtres) ──
        # Toutes calculables AVANT le fill — pas de leak temporel.
        sig["bars_since_confirm"] = int(i - impulse["confirm_idx"])
        sig["adx_at_arm"] = float(bar["adx"]) if not pd.isna(bar["adx"]) else float("nan")
        # Pente ADX 3 bougies : positive = tendance qui se renforce
        if i >= 3 and not pd.isna(df["adx"].iloc[i - 3]):
            sig["adx_slope_3"] = float(bar["adx"] - df["adx"].iloc[i - 3])
        else:
            sig["adx_slope_3"] = float("nan")
        # Distance EMA50-EMA200 normalisée par ATR (force du EMA stack)
        sig["ema_stack_atr"] = float(
            (bar["ema_fast"] - bar["ema_slow"]) / atr_now
        )
        # Extension du prix au-dessus/dessous de fib_50 (signed, dans le sens du trade)
        if impulse["direction"] == "long":
            sig["price_extension_atr"] = float(
                (bar["close"] - impulse["fib_50"]) / atr_now
            )
        else:
            sig["price_extension_atr"] = float(
                (impulse["fib_50"] - bar["close"]) / atr_now
            )
        # Vitesse de l'impulse (ATR/bar)
        sig["impulse_velocity_atr"] = float(
            impulse["impulse_size"] / max(impulse["impulse_bars"], 1) / atr_now
        )
        # Heure de session (UTC), heure NY = UTC - 4 ou -5 selon DST
        sig["session_hour_utc"] = int(df.index[i].hour)
        # Volatilité récente (10 bougies) normalisée
        if i >= 10:
            recent_std = float(df["close"].iloc[i - 10:i].std())
            sig["recent_vol_atr"] = recent_std / atr_now if atr_now > 0 else float("nan")
        else:
            sig["recent_vol_atr"] = float("nan")
        # Taille de l'impulse en multiples d'ATR (au moment du pivot final)
        sig["impulse_size_atr"] = float(
            impulse["impulse_size"] / impulse["atr_at_pivot"]
        )

        # ── Filtre trigger (calibré walk-forward via analyze_filters.py) ──
        # Marque l'impulse comme "vue" même si rejetée — évite de re-vérifier
        # cette même impulse à la bougie suivante (pas de spam de logs).
        last_impulse_key = impulse_key
        filter_cfg = TRIGGER_FILTERS_PER_TICKER.get(ticker)
        if filter_cfg is not None:
            feat_val = sig.get(filter_cfg["feature"])
            if feat_val is None or pd.isna(feat_val):
                continue
            thresh = float(filter_cfg["threshold"])
            if filter_cfg["direction"] == "gt":
                if feat_val <= thresh:
                    continue
            else:  # lt
                if feat_val >= thresh:
                    continue

        pending = sig

    return pd.DataFrame(trades)


# ─────────────────────────────────────────────────────────────────────────────
# Statistiques de performance
# ─────────────────────────────────────────────────────────────────────────────

def stats(trades: pd.DataFrame, account_size: float = ACCOUNT_SIZE) -> dict:
    """
    Calcule les métriques de performance clés sur un DataFrame de trades.
    Sharpe = annualisé via sqrt(SHARPE_ANNUALIZATION) sur trades returns.
    """
    if len(trades) == 0:
        return {"n": 0, "win_rate": 0.0, "pf": 0.0, "pnl_total": 0.0,
                "max_dd": 0.0, "sharpe": 0.0, "avg_win": 0.0, "avg_loss": 0.0}

    n = len(trades)
    wins = trades[trades["pnl"] > 0]
    losses = trades[trades["pnl"] <= 0]

    gp = float(wins["pnl"].sum()) if len(wins) > 0 else 0.0
    gl = abs(float(losses["pnl"].sum())) if len(losses) > 0 else 0.0
    pf = gp / gl if gl > 0 else (9.99 if gp > 0 else 0.0)

    pnl_total = float(trades["pnl"].sum())

    cum = trades["pnl"].cumsum()
    rolling_max = cum.cummax()
    max_dd = float((cum - rolling_max).min())

    returns = trades["pnl"] / account_size
    if returns.std() > 0 and len(returns) > 1:
        sharpe = float(returns.mean() / returns.std() * np.sqrt(SHARPE_ANNUALIZATION))
    else:
        sharpe = 0.0

    return {
        "n": int(n),
        "win_rate": float(len(wins) / n * 100),
        "pf": float(pf),
        "pnl_total": pnl_total,
        "max_dd": max_dd,
        "sharpe": sharpe,
        "avg_win": float(wins["pnl"].mean()) if len(wins) > 0 else 0.0,
        "avg_loss": float(losses["pnl"].mean()) if len(losses) > 0 else 0.0,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main CLI
# ─────────────────────────────────────────────────────────────────────────────

def _print_stats(label: str, s: dict):
    print(f"  {label:<10}: n={s['n']:3d}  WR={s['win_rate']:.0f}%  "
          f"PF={s['pf']:.2f}  P&L=${s['pnl_total']:+.0f}  "
          f"DD=${s['max_dd']:+.0f}  Sharpe={s['sharpe']:.2f}")


def main():
    parser = argparse.ArgumentParser(
        description="Backtest stratégie Fibonacci 50%"
    )
    parser.add_argument("--csv-dir", type=str, default="../data")
    parser.add_argument("--ticker", type=str, default=None)
    parser.add_argument("--output-dir", type=str, default=None)
    args = parser.parse_args()

    csv_dir = Path(args.csv_dir)
    out_dir = Path(args.output_dir) if args.output_dir \
              else Path(__file__).parent / "output"
    out_dir.mkdir(parents=True, exist_ok=True)

    tickers = [args.ticker] if args.ticker else list(INSTRUMENTS.keys())

    all_results = {}
    for ticker in tickers:
        csv_path = csv_dir / f"{ticker}_data_m15.csv"
        if not csv_path.exists():
            print(f"[!] {csv_path} introuvable")
            continue

        print(f"\n{'='*70}")
        print(f"  BACKTEST FIB-50 — {ticker}")
        print(f"{'='*70}")

        df = load_csv(str(csv_path))
        print(f"  {len(df):,} bougies "
              f"[{df.index[0]} → {df.index[-1]}]")

        trades = run_backtest(df, ticker)
        if len(trades) == 0:
            print(f"  Aucun trade généré.")
            continue

        # Sauve trades
        trades_path = out_dir / f"trades_{ticker}.csv"
        trades.to_csv(trades_path, index=False)

        # Split IS/OOS
        trades["date"] = trades["fill_time"].astype(str).str[:10]
        is_t = trades[trades["date"] <= IS_END]
        oos_t = trades[trades["date"] > IS_END]

        s_global = stats(trades)
        s_is = stats(is_t)
        s_oos = stats(oos_t)

        print()
        _print_stats("GLOBAL", s_global)
        _print_stats("IS", s_is)
        _print_stats("OOS", s_oos)

        # Distribution résultats
        n_tp = (trades["result"] == "TP").sum()
        n_sl = (trades["result"] == "SL").sum()
        n_te = (trades["result"] == "TE").sum()
        print(f"  Résultats : TP={n_tp}  SL={n_sl}  TE={n_te}")
        print(f"  ✓ {trades_path}")

        all_results[ticker] = {
            "global": s_global, "is": s_is, "oos": s_oos,
            "n_tp": int(n_tp), "n_sl": int(n_sl), "n_te": int(n_te),
        }

    # Portefeuille
    if len(all_results) > 1:
        all_trades_list = []
        for ticker in all_results:
            t = pd.read_csv(out_dir / f"trades_{ticker}.csv")
            all_trades_list.append(t)
        port = pd.concat(all_trades_list, ignore_index=True)
        port["fill_time"] = pd.to_datetime(port["fill_time"])
        port = port.sort_values("fill_time").reset_index(drop=True)
        port["date"] = port["fill_time"].astype(str).str[:10]

        s_port = stats(port)
        s_port_is = stats(port[port["date"] <= IS_END])
        s_port_oos = stats(port[port["date"] > IS_END])

        print(f"\n{'#'*70}")
        print(f"  PORTEFEUILLE — Stratégie Fibonacci 50%")
        print(f"{'#'*70}")
        _print_stats("GLOBAL", s_port)
        _print_stats("IS", s_port_is)
        _print_stats("OOS", s_port_oos)

    return all_results


if __name__ == "__main__":
    main()
