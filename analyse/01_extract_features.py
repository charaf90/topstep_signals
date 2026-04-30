"""
Extraction des features à chaque trigger OPR (filled + NOT_FILLED).

Approche post-hoc : run_opr_day() fournit les triggers et résultats ground
truth ; les features sont calculées autour de chaque trigger_time sans
rejouer la state machine — aucun risque de divergence avec le backtest.

Usage :
    python analyse/01_extract_features.py --csv-dir ./data
    python analyse/01_extract_features.py --csv-dir ./data --ticker NQ1
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent))


def _f(v):
    """Conversion float sûre — None ou NaN → np.nan."""
    if v is None:
        return np.nan
    try:
        fv = float(v)
        return np.nan if np.isnan(fv) else fv
    except (ValueError, TypeError):
        return np.nan


from config import (
    INSTRUMENTS, OPR_TIMEZONE, OPR_WINDOW_START, OPR_SESSION_END,
    OPR_ATR_PERIOD, CUTOFF_HOUR_UTC,
)
from core.data import load_csv, build_timeframes
from core.opr import (
    _ny_session_view, _opr_bar, _compute_atr_daily, run_opr_day,
)
from core.trend import precompute_trends, get_regime_with_score
from core.premarket import compute_features as compute_pm_features
from core.scoring import compute_volatility_features

# ─────────────────────────────────────────────────────────────────────────────
# Constantes
# ─────────────────────────────────────────────────────────────────────────────

VOL_ZSCORE_WINDOW = 20      # bougies de session pour le z-score volume
OPR_TEST_ZONE_PCT = 0.10    # ±10% du range OPR pour n_tests_before


# ─────────────────────────────────────────────────────────────────────────────
# Helpers features
# ─────────────────────────────────────────────────────────────────────────────

def _count_tests_before(df_session_ny, opr_ts_ny, opr_level, opr_range,
                        ts_trigger):
    """
    Nombre de bougies entre la bougie OPR (exclu) et le trigger (exclu)
    dont le range [low, high] touche la zone [opr_level ± tol].
    """
    tol = OPR_TEST_ZONE_PCT * opr_range
    mask = (df_session_ny.index > opr_ts_ny) & (df_session_ny.index < ts_trigger)
    cands = df_session_ny.loc[mask]
    if cands.empty:
        return 0
    lo, hi = opr_level - tol, opr_level + tol
    return int(((cands["high"] >= lo) & (cands["low"] <= hi)).sum())


def _max_excursion_atr(df_session_ny, opr_ts_ny, ts_trigger,
                       direction, opr_level, atr_daily):
    """
    Excursion maximale dans le sens du trigger depuis la bougie OPR
    (exclus) jusqu'au trigger (inclus), normalisée par atr_daily.
    """
    mask = (df_session_ny.index > opr_ts_ny) & (df_session_ny.index <= ts_trigger)
    window = df_session_ny.loc[mask]
    if window.empty or atr_daily <= 0:
        return np.nan
    if direction == "long":
        excursion = float(window["high"].max()) - opr_level
    else:
        excursion = opr_level - float(window["low"].min())
    return max(excursion, 0.0) / atr_daily


def _compute_trigger_features(bar, df_session_ny, opr_ts_ny,
                               opr_high, opr_low, opr_vol,
                               atr_daily, direction, ts_trigger,
                               session_end_t, trigger_idx):
    """
    Calcule l'ensemble des features pour un trigger donné.
    Retourne un dict (valeurs NaN si données insuffisantes).
    """
    opr_range = opr_high - opr_low
    opr_level = opr_high if direction == "long" else opr_low
    bar_high = float(bar["high"])
    bar_low = float(bar["low"])
    bar_open = float(bar["open"])
    bar_close = float(bar["close"])
    bar_range = bar_high - bar_low
    bar_vol = float(bar.get("volume", np.nan))

    # --- Timing ---
    time_since_opr_mins = (ts_trigger - opr_ts_ny).total_seconds() / 60.0
    bars_since_opr = int(
        ((df_session_ny.index > opr_ts_ny) & (df_session_ny.index <= ts_trigger)).sum()
    )
    session_hour_ny = ts_trigger.hour + ts_trigger.minute / 60.0
    time_to_close_mins = (session_end_t - ts_trigger).total_seconds() / 60.0

    # --- Zone OPR ---
    opr_range_atr_ratio = opr_range / atr_daily if atr_daily > 0 else np.nan
    opr_range_pts = opr_range

    # --- Bougie trigger ---
    trigger_body_ratio = (
        abs(bar_close - bar_open) / bar_range if bar_range > 0 else np.nan
    )
    if direction == "long":
        trigger_close_strength = (
            (bar_close - bar_low) / bar_range if bar_range > 0 else np.nan
        )
        close_beyond_opr_atr = (
            (bar_close - opr_level) / atr_daily if atr_daily > 0 else np.nan
        )
    else:
        trigger_close_strength = (
            (bar_high - bar_close) / bar_range if bar_range > 0 else np.nan
        )
        close_beyond_opr_atr = (
            (opr_level - bar_close) / atr_daily if atr_daily > 0 else np.nan
        )

    trigger_candle_size_atr = (
        bar_range / atr_daily if atr_daily > 0 else np.nan
    )
    trigger_vol_vs_opr = (
        bar_vol / opr_vol if (opr_vol > 0 and not np.isnan(opr_vol)) else np.nan
    )

    # Z-score volume sur les VOL_ZSCORE_WINDOW bougies précédentes de session
    preceding = df_session_ny.loc[df_session_ny.index < ts_trigger]
    last_n = preceding.iloc[-VOL_ZSCORE_WINDOW:]
    if len(last_n) >= 2 and "volume" in last_n.columns:
        mu_v = last_n["volume"].mean()
        std_v = last_n["volume"].std(ddof=1)
        trigger_vol_zscore = (
            (bar_vol - mu_v) / std_v if std_v > 0 else np.nan
        )
    else:
        trigger_vol_zscore = np.nan

    # --- Price action depuis OPR ---
    n_tests_before = _count_tests_before(
        df_session_ny, opr_ts_ny, opr_level, opr_range, ts_trigger
    )
    max_excursion = _max_excursion_atr(
        df_session_ny, opr_ts_ny, ts_trigger, direction, opr_level, atr_daily
    )

    return {
        # timing
        "time_since_opr_mins": round(time_since_opr_mins, 2),
        "bars_since_opr": bars_since_opr,
        "session_hour_ny": round(session_hour_ny, 3),
        "time_to_close_mins": round(time_to_close_mins, 2),
        # zone OPR
        "opr_range_atr_ratio": round(opr_range_atr_ratio, 4) if not np.isnan(opr_range_atr_ratio) else np.nan,
        "opr_range_pts": round(opr_range_pts, 4),
        # bougie trigger
        "trigger_body_ratio": round(trigger_body_ratio, 4) if not np.isnan(trigger_body_ratio) else np.nan,
        "trigger_close_strength": round(trigger_close_strength, 4) if not np.isnan(trigger_close_strength) else np.nan,
        "close_beyond_opr_atr": round(close_beyond_opr_atr, 4) if not np.isnan(close_beyond_opr_atr) else np.nan,
        "trigger_candle_size_atr": round(trigger_candle_size_atr, 4) if not np.isnan(trigger_candle_size_atr) else np.nan,
        "trigger_vol_vs_opr": round(trigger_vol_vs_opr, 4) if not np.isnan(trigger_vol_vs_opr) else np.nan,
        "trigger_vol_zscore": round(trigger_vol_zscore, 4) if not np.isnan(trigger_vol_zscore) else np.nan,
        # price action
        "n_tests_before": n_tests_before,
        "max_excursion_atr": round(max_excursion, 4) if not np.isnan(max_excursion) else np.nan,
        "trigger_idx": trigger_idx,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Extraction principale — 1 jour
# ─────────────────────────────────────────────────────────────────────────────

def extract_day(df_15m, ticker, day_ny, tf, trend_scores, tz):
    """
    Extrait les features de tous les triggers OPR du jour donné.
    Retourne une liste de dicts (1 par trigger, filled ou NOT_FILLED).
    """
    signals, trades, opr_zone = run_opr_day(df_15m, ticker, day_ny)
    if not signals or opr_zone is None:
        return []

    ds = day_ny.strftime("%Y-%m-%d")
    cutoff = pd.Timestamp(f"{ds} {CUTOFF_HOUR_UTC:02d}:00:00")

    # Features contextuelles au cutoff (1 calcul par jour)
    regime, alignment_score = get_regime_with_score(trend_scores, cutoff)
    pm = compute_pm_features(df_15m, cutoff)
    vol = compute_volatility_features(df_15m, cutoff, ticker)

    ovn_path_eff = pm.get("ovn_path_eff") if pm else np.nan
    prev_return = pm.get("prev_return") if pm else np.nan
    prev_close_pos = pm.get("prev_close_pos") if pm else np.nan
    atr_ratio = vol.get("atr_ratio") if vol else np.nan

    is_trend_aligned_base = None  # calculé par trigger

    # Vue session NY (pour les features bougie)
    df_session_ny = _ny_session_view(df_15m, day_ny, tz)
    if df_session_ny is None or df_session_ny.empty:
        return []

    # Données OPR zone
    opr_ts_ny = opr_zone["time_ny"]
    opr_high = float(opr_zone["high"])
    opr_low = float(opr_zone["low"])
    opr_range = opr_high - opr_low

    # Volume de la bougie OPR (pour trigger_vol_vs_opr)
    opr_vol = np.nan
    if opr_ts_ny in df_session_ny.index and "volume" in df_session_ny.columns:
        opr_vol = float(df_session_ny.loc[opr_ts_ny, "volume"])

    # ATR journalier (depuis _compute_atr_daily, cohérent avec run_opr_day)
    atr_daily = _compute_atr_daily(df_15m, day_ny, OPR_ATR_PERIOD)
    if atr_daily is None:
        return []

    # Heure de fermeture de session (NY-aware)
    h_close, m_close = OPR_SESSION_END
    session_end_t = day_ny.replace(
        hour=h_close, minute=m_close, second=0, microsecond=0
    )

    rows = []
    for trigger_idx, (sig, trade) in enumerate(zip(signals, trades), start=1):
        trigger_ts_str = sig["trigger_time"]
        try:
            trigger_ts = pd.Timestamp(trigger_ts_str)
        except Exception:
            continue

        # Lookup de la bougie trigger dans df_session_ny
        if trigger_ts not in df_session_ny.index:
            # Fallback : recherche par proximité ≤ 1 seconde (dérive de format)
            diff = abs(df_session_ny.index - trigger_ts)
            closest_idx = diff.argmin()
            if diff[closest_idx].total_seconds() > 60:
                continue
            trigger_ts = df_session_ny.index[closest_idx]

        bar = df_session_ny.loc[trigger_ts]
        direction = sig["direction"]

        # Calcul features bougie + price action
        feats = _compute_trigger_features(
            bar=bar,
            df_session_ny=df_session_ny,
            opr_ts_ny=opr_ts_ny,
            opr_high=opr_high,
            opr_low=opr_low,
            opr_vol=opr_vol,
            atr_daily=atr_daily,
            direction=direction,
            ts_trigger=trigger_ts,
            session_end_t=session_end_t,
            trigger_idx=trigger_idx,
        )

        is_trend_aligned = int(
            (direction == "long" and regime == "BULL") or
            (direction == "short" and regime == "BEAR")
        ) if regime else np.nan

        row = {
            # identifiant
            "date": ds,
            "ticker": ticker,
            "trigger_ts_ny": str(trigger_ts),
            "direction": direction,
            # features calculées
            **feats,
            # features contextuelles (cutoff)
            "regime": regime,
            "alignment_score": _f(alignment_score),
            "is_trend_aligned": is_trend_aligned,
            "atr_ratio": _f(atr_ratio),
            "prev_return": _f(prev_return),
            "ovn_path_eff": _f(ovn_path_eff),
            "prev_close_pos": _f(prev_close_pos),
            # résultat
            "result": trade.get("result", "NOT_FILLED"),
            "pnl": float(trade.get("pnl", 0.0)),
            "is_tp": 1 if trade.get("result") == "TP" else 0,
        }
        rows.append(row)

    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Extraction features OPR triggers"
    )
    parser.add_argument("--csv-dir", type=str, default="./data")
    parser.add_argument("--ticker", type=str, default=None)
    args = parser.parse_args()

    csv_dir = Path(args.csv_dir)
    out_dir = Path(__file__).parent / "data"
    out_dir.mkdir(parents=True, exist_ok=True)

    tickers = [args.ticker] if args.ticker else list(INSTRUMENTS.keys())
    tz = ZoneInfo(OPR_TIMEZONE)

    for ticker in tickers:
        csv_path = csv_dir / f"{ticker}_data_m15.csv"
        if not csv_path.exists():
            print(f"[!] {csv_path} introuvable — ticker ignoré")
            continue

        print(f"\n{'='*60}")
        print(f"  {ticker}")
        print(f"{'='*60}")

        df_15m = load_csv(str(csv_path))
        tf = build_timeframes(df_15m)
        trend_scores = precompute_trends(tf)

        # Liste des jours NY
        if df_15m.index.tz is None:
            idx_ny = df_15m.index.tz_localize("UTC").tz_convert(tz)
        else:
            idx_ny = df_15m.index.tz_convert(tz)
        ny_days = pd.DatetimeIndex(idx_ny.normalize().unique()).sort_values()

        all_rows = []
        for day_ny in ny_days:
            rows = extract_day(df_15m, ticker, day_ny, tf, trend_scores, tz)
            all_rows.extend(rows)

        if not all_rows:
            print(f"  Aucun trigger trouvé.")
            continue

        df_out = pd.DataFrame(all_rows)
        out_path = out_dir / f"opr_triggers_{ticker}.csv"
        df_out.to_csv(out_path, index=False)

        df_filled = df_out[df_out["result"] != "NOT_FILLED"]
        n_tp = (df_out["result"] == "TP").sum()
        n_sl = (df_out["result"] == "SL").sum()
        n_te = (df_out["result"] == "TE").sum()
        n_nf = (df_out["result"] == "NOT_FILLED").sum()
        wr = n_tp / len(df_filled) * 100 if len(df_filled) > 0 else 0

        print(f"  Triggers : {len(df_out)} total "
              f"| TP={n_tp}  SL={n_sl}  TE={n_te}  NF={n_nf}")
        print(f"  Win rate (fills) : {wr:.1f}%")
        print(f"  Exporté → {out_path}")


if __name__ == "__main__":
    main()
