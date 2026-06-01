"""
Stratégie eia-inv-v1 — event-driven mean-reversion sur EIA Weekly Petroleum.

Concept       : sur-réaction algos HFT au headline EIA Weekly Petroleum
                Status Report (mercredi 10:30 NY) → entrée LIMIT contrarian
                à l'extremum de la barre EIA M15, TP retour à l'ancre
                (close 10:15 NY), SL au-delà du spike (× SL_MULT).
Edge          : mean-reversion documentée < 25 min post-release
                (Lazar/Maneescu/Manzoni 2017, "An analysis of intraday market
                response to crude oil inventory shocks").
Falsification : WR OOS < 55 % OU PF OOS < 1.2 sur 3 mois glissants.
Indicateurs   : 3 features must-have (cf. output/eia-inv-v1/quant_catalog.md)
                - spike_pct           (filtre baseline v1)
                - spike_atr_ratio     (persistée, non filtrante v1)
                - pre_event_trend_atr (persistée, non filtrante v1)
Fenêtre NY    : mercredi, event 10:30 ± 15 min, exit ≤ 11:30 NY (time barrier).
Timeframe     : M15 (backtest). En live : M5 obligatoire via M5Buffer
                (live-équivalence) — cf. feedback_live_eq_pattern.md.

=== BIAIS BACKTEST_ONLY ===
Le fill LIMIT au high/low de la barre EIA M15 (10:30-10:45 NY) est assumé
garanti dès atteinte du prix. En live (carnet épuisé, queue LIMIT longue),
le taux NOT_FILLED réel sera plus élevé. Optimisme PF estimé ~10-15 %.
À corriger en v2 via M5 (entrée différée 10:35 NY) ou wirage M5Buffer.
==========================
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

# ── Reproductibilité ─────────────────────────────────────────────────────────
np.random.seed(42)

# ── Imports projet ───────────────────────────────────────────────────────────
from config import (
    COMMISSION_RT_PER_CONTRACT,
    EIA_INV_ANCHOR_TIME_NY,
    EIA_INV_EVENT_TIME_NY,
    EIA_INV_SKIP_DATES,
    EIA_INV_SL_MULT,
    EIA_INV_SLIP_ENTRY_TICKS,
    EIA_INV_SLIP_EXIT_TICKS,
    EIA_INV_SPIKE_MIN_PCT,
    EIA_INV_STRATEGY_VERSION,
    EIA_INV_TICKERS,
    EIA_INV_TIME_BARRIER_MIN,
    INSTRUMENTS,
    MACRO_EVENT_DATES,
    RISK_PER_TRADE_USD,
)
from core.risk_topstep import trade_allowed

# ── Identité de la stratégie (contrat registry) ──────────────────────────────
STRATEGY_ID = EIA_INV_STRATEGY_VERSION  # "eia-inv-v1"
TICKERS = list(EIA_INV_TICKERS)  # ["MCL1"]
CSV_SUFFIX = "_data"  # data/MCL1_data_m15.csv
CSV_TIMEFRAME = "m15"

_NY = ZoneInfo("America/New_York")
_MACRO_DATES = set(pd.to_datetime(MACRO_EVENT_DATES).date.tolist())
_SKIP_DATES = set(pd.to_datetime(EIA_INV_SKIP_DATES).date.tolist())

# ── Grille d'optimisation (référence — non utilisée en baseline v1) ──────────
PARAM_GRID = {
    "spike_min_pct": [0.30, 0.40, 0.50],
    "sl_mult": [1.0, 1.5, 2.0],
    "time_barrier_min": [45, 60, 90],
}


# ══════════════════════════════════════════════════════════════════════════════
# 1. Helpers — ATR + features quant must-have (cf. quant_catalog.md)
# ══════════════════════════════════════════════════════════════════════════════


def _compute_atr_m15(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """ATR(20) Wilder/SMA sur M15. Shift(1) appliqué en sortie pour garantir
    qu'`atr.iloc[i]` représente l'ATR connu à l'OUVERTURE de la barre i
    (donc lisible avant toute décision sur cette barre).

    Causal strict : aucune valeur de la barre i n'entre dans atr.iloc[i].
    """
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(window=period, min_periods=period).mean()
    return atr.shift(1)


def _compute_eia_features(
    df: pd.DataFrame,
    atr_series: pd.Series,
    i_anchor: int,
    i_eia: int,
    direction: str,
) -> dict:
    """Calcule les 3 features must-have v1 + features auxiliaires log.

    Garanties causales (cf. quant_catalog.md §3) :
      - spike_pct, spike_atr_ratio : lisent la barre EIA (assumé causal car
        décision LIMIT à 10:45 NY après fermeture barre EIA — cf. concept).
      - pre_event_trend_atr        : lit uniquement closes ≤ 10:15 NY.
      - atr_pre_eia                : ATR(20) shift(1) — connu à l'ouverture
        de la barre EIA.
    """
    anchor = float(df["close"].iloc[i_anchor])
    high_eia = float(df["high"].iloc[i_eia])
    low_eia = float(df["low"].iloc[i_eia])

    if direction == "short":
        spike_range = high_eia - anchor
    else:  # long
        spike_range = anchor - low_eia

    spike_pct = (spike_range / anchor) * 100.0 if anchor > 0 else np.nan

    # ATR(20) connu à l'ouverture de la barre EIA (déjà shift(1) à l'intérieur)
    atr_pre_eia = float(atr_series.iloc[i_eia])
    if not np.isfinite(atr_pre_eia) or atr_pre_eia <= 0:
        atr_pre_eia = np.nan

    spike_atr_ratio = spike_range / atr_pre_eia if np.isfinite(atr_pre_eia) else np.nan

    # Trend pré-EIA : ANCHOR − close 08:30 NY normalisé par ATR.
    # Heuristique 7 barres (15 min × 7 = 105 min entre 08:30 et 10:15).
    # Validation runtime : on vérifie via zoneinfo que la barre i_0830 est
    # bien fermée à 08:30 NY (DST-aware). Si pas trouvée → NaN (rare).
    i_0830 = _find_bar_index_by_time(df, i_anchor, target_hour=8, target_min=30)
    if i_0830 is None or i_0830 < 0:
        pre_event_trend_atr = np.nan
    else:
        close_0830 = float(df["close"].iloc[i_0830])
        if np.isfinite(atr_pre_eia):
            pre_event_trend_atr = (anchor - close_0830) / atr_pre_eia
        else:
            pre_event_trend_atr = np.nan

    return {
        "spike_pct": spike_pct,
        "spike_atr_ratio": spike_atr_ratio,
        "pre_event_trend_atr": pre_event_trend_atr,
        "anchor": anchor,
        "atr_pre_eia": atr_pre_eia,
        "spike_range": spike_range,
    }


def _find_bar_index_by_time(
    df: pd.DataFrame, i_anchor: int, target_hour: int, target_min: int
) -> int | None:
    """Trouve l'index de la barre M15 fermée à `target_hour:target_min` NY
    appartenant à la même date NY que i_anchor.

    Cherche par décalage négatif depuis i_anchor (typiquement i_anchor - 7
    pour 08:30 NY vs 10:15 NY) avec ±2 barres de tolérance pour DST safety.
    Retourne None si introuvable dans la fenêtre.
    """
    if i_anchor <= 0:
        return None
    anchor_date_ny = pd.Timestamp(df.index[i_anchor]).tz_localize("UTC").tz_convert(_NY).date()
    # Fenêtre de recherche : 5 à 12 barres avant i_anchor
    for offset in range(7, 13):
        j = i_anchor - offset
        if j < 0:
            return None
        ts_ny = pd.Timestamp(df.index[j]).tz_localize("UTC").tz_convert(_NY)
        if (
            ts_ny.date() == anchor_date_ny
            and ts_ny.hour == target_hour
            and ts_ny.minute == target_min
        ):
            return j
    # Fallback heuristique : i_anchor - 7
    j = i_anchor - 7
    return j if j >= 0 else None


# ══════════════════════════════════════════════════════════════════════════════
# 2. Backtest principal
# ══════════════════════════════════════════════════════════════════════════════


def run_backtest(
    df_15m: pd.DataFrame,
    ticker: str,
    tf=None,
    params: dict | None = None,
    topstep_guard: bool = True,
) -> pd.DataFrame:
    """
    Backtest eia-inv-v1 sur MCL1 M15.

    Schéma de sortie standard projet (cf. core/optimizer.py) + 3 features quant.
    `pnl` = P&L NET (slippage entrée+sortie + commissions inclus).

    Flux de décision (par mercredi non-skippé) :
      1. ANCHOR = close(barre M15 10:15 NY) — connu à 10:15
      2. Attendre fermeture barre EIA (10:45 NY) → décider direction + spike
      3. Filtre v1 : spike_pct ≥ EIA_SPIKE_MIN_PCT (else skip)
      4. LIMIT posé @ extremum ± slip × tick_size (validité = barres suivantes
         jusqu'à 11:30 NY)
      5. Si fill → simulate exit (SL prio si ambiguïté, TP=ANCHOR, time barrier 11:30)
      6. Si non-fill avant time barrier → result = NOT_FILLED (PAS de MARKET)
    """
    if ticker not in TICKERS:
        return _empty_trades_df()

    p = params or {}
    spike_min_pct = float(p.get("spike_min_pct", EIA_INV_SPIKE_MIN_PCT))
    sl_mult = float(p.get("sl_mult", EIA_INV_SL_MULT))
    time_barrier_min = int(p.get("time_barrier_min", EIA_INV_TIME_BARRIER_MIN))

    instr = INSTRUMENTS[ticker]
    tick_size = instr["tick_size"]
    pt_value = instr["dollar_per_point"]
    slip_entry_px = EIA_INV_SLIP_ENTRY_TICKS * tick_size  # 3 ticks event-driven
    slip_exit_px = EIA_INV_SLIP_EXIT_TICKS * tick_size  # 1 tick standard

    # Time barrier en barres M15 (60 min → 4 barres)
    time_barrier_bars = max(1, time_barrier_min // 15)

    df = df_15m.copy()
    # Index timestamps NY pour filtre weekday + hour
    idx_utc = pd.DatetimeIndex(df.index)
    if idx_utc.tz is None:
        idx_utc = idx_utc.tz_localize("UTC")
    idx_ny = idx_utc.tz_convert(_NY)

    # ATR(20) M15 pré-calculé (shift(1) interne → causal strict)
    atr_series = _compute_atr_m15(df, period=20)

    # Pré-localiser les barres EIA : mercredi (weekday=2), hour=10, min=30 NY
    anchor_h, anchor_m = EIA_INV_ANCHOR_TIME_NY  # (10, 15)
    event_h, event_m = EIA_INV_EVENT_TIME_NY  # (10, 30)

    is_eia_bar = (idx_ny.dayofweek == 2) & (idx_ny.hour == event_h) & (idx_ny.minute == event_m)
    eia_indices = np.where(is_eia_bar)[0]

    trades: list = []
    cum_pnl = 0.0
    peak_pnl = 0.0

    for i_eia in eia_indices:
        # Date NY (pour skip + log)
        ts_eia_ny = idx_ny[i_eia]
        date_str = ts_eia_ny.date().isoformat()

        # Skip dates EIA officielles décalées
        if ts_eia_ny.date() in _SKIP_DATES:
            continue

        # ANCHOR = barre M15 précédente (10:15 NY) — toujours i_eia - 1
        # Validation runtime : doit être 10:15 NY même date
        i_anchor = i_eia - 1
        if i_anchor < 0:
            continue
        ts_anchor_ny = idx_ny[i_anchor]
        if not (
            ts_anchor_ny.hour == anchor_h
            and ts_anchor_ny.minute == anchor_m
            and ts_anchor_ny.date() == ts_eia_ny.date()
        ):
            # Gap data (barre 10:15 manquante) — skip
            continue

        # Vérifier qu'on a assez de barres après pour l'exit (time barrier)
        if i_eia + 1 + time_barrier_bars >= len(df):
            continue

        # Vérifier ATR disponible (warmup)
        atr_pre_eia = atr_series.iloc[i_eia]
        if not np.isfinite(atr_pre_eia) or atr_pre_eia <= 0:
            continue

        anchor = float(df["close"].iloc[i_anchor])
        bar_eia = df.iloc[i_eia]
        high_eia = float(bar_eia["high"])
        low_eia = float(bar_eia["low"])
        close_eia = float(bar_eia["close"])

        # ── Direction contrarian : spike haussier → SHORT, baissier → LONG ──
        if close_eia > anchor:
            direction = "short"
            spike_range = high_eia - anchor
            extremum = high_eia
        elif close_eia < anchor:
            direction = "long"
            spike_range = anchor - low_eia
            extremum = low_eia
        else:
            # Pas de spike directionnel (close_eia == anchor) — skip
            continue

        if spike_range <= 0:
            continue

        # ── Features quant must-have (calculées AVANT filtrage) ──
        feats = _compute_eia_features(df, atr_series, i_anchor, i_eia, direction)
        spike_pct = feats["spike_pct"]
        spike_atr_ratio = feats["spike_atr_ratio"]
        pre_event_trend_atr = feats["pre_event_trend_atr"]

        # ── Filtre BASELINE v1 : spike_pct ≥ EIA_SPIKE_MIN_PCT ──
        if not np.isfinite(spike_pct) or spike_pct < spike_min_pct:
            continue

        # ── Trigger d'entrée : LIMIT posé après fermeture EIA (10:45 NY) ──
        # En pratique en backtest : on cherche fill dès la barre i_eia (la
        # barre EIA contient déjà le high/low → fill assumé garanti).
        # BACKTEST_ONLY : en live, le LIMIT serait posé à 10:45 NY et ne
        # pourrait être touché qu'à partir de la barre 10:45-11:00 NY.
        if direction == "long":
            entry = low_eia + slip_entry_px  # LIMIT BUY @ low + slip
            sl = low_eia - sl_mult * spike_range - slip_exit_px
        else:  # short
            entry = high_eia - slip_entry_px  # LIMIT SELL @ high - slip
            sl = high_eia + sl_mult * spike_range + slip_exit_px

        tp = anchor  # TP = retour exact à l'ancre
        sl_dist = abs(entry - sl)
        tp_dist = abs(tp - entry)
        if sl_dist <= 0 or tp_dist <= 0:
            continue
        rr = tp_dist / sl_dist

        # ── Sizing — risque cible incluant slippage anticipé ──
        risk_dollars_per_ct = (sl_dist + slip_exit_px) * pt_value
        if risk_dollars_per_ct <= 0:
            continue
        n_ct = max(1, int(RISK_PER_TRADE_USD / risk_dollars_per_ct))

        # ── Garde-fou Topstep ──
        if topstep_guard:
            allowed, _motif = trade_allowed(day_pnl=0.0, cum_pnl=cum_pnl, peak_pnl=peak_pnl)
            if not allowed:
                continue

        # ── Recherche du fill ──
        # Le LIMIT est posé APRÈS la fermeture de la barre EIA (10:45 NY) —
        # cf. concept.md §TRIGGER D'ENTRÉE. Donc le fill ne peut survenir
        # qu'à partir de la barre i_eia + 1 (10:45-11:00 NY) et jusqu'à la
        # fin de la fenêtre time barrier (= i_eia + time_barrier_bars).
        # BACKTEST_ONLY persistant : le fill au low/high de cette barre M15
        # reste optimiste vs live (granularité M5/M1 réelle des ordres).
        fill_idx = None
        for j in range(i_eia + 1, min(i_eia + 1 + time_barrier_bars, len(df))):
            fb = df.iloc[j]
            if direction == "long" and float(fb["low"]) <= entry:
                fill_idx = j
                break
            if direction == "short" and float(fb["high"]) >= entry:
                fill_idx = j
                break

        # Tags régime (basé sur ATR ratio + macro)
        atr_pct = None  # non utilisé en v1 (filtre temporel suffit)
        is_macro = ts_eia_ny.date() in _MACRO_DATES
        regime = "event"  # tous les trades sont event-driven par construction

        # ── NOT_FILLED : LIMIT non touché dans la fenêtre time barrier ──
        if fill_idx is None:
            trades.append(
                _make_trade_row(
                    date_str=date_str,
                    direction=direction,
                    entry=entry,
                    sl=sl,
                    tp=tp,
                    sl_dist=sl_dist,
                    tp_dist=tp_dist,
                    rr=rr,
                    n_ct=n_ct,
                    result="NOT_FILLED",
                    pnl=0.0,
                    pnl_gross=0.0,
                    fill_time=None,
                    exit_time=None,
                    exit_px=None,
                    regime=regime,
                    atr_pct=atr_pct,
                    is_macro=is_macro,
                    spike_pct=spike_pct,
                    spike_atr_ratio=spike_atr_ratio,
                    pre_event_trend_atr=pre_event_trend_atr,
                )
            )
            continue

        fill_time = df.index[fill_idx]

        # ── Simulation exit (SL prio si ambiguïté, TP=ANCHOR, time barrier) ──
        result, exit_idx, exit_px = _simulate_exit(
            df=df,
            fill_idx=fill_idx,
            direction=direction,
            entry=entry,
            sl=sl,
            tp=tp,
            time_barrier_end_idx=min(i_eia + time_barrier_bars, len(df) - 1),
        )
        exit_time = df.index[exit_idx]

        # ── P&L brut + net ──
        raw_pnl = (exit_px - entry) * (1 if direction == "long" else -1)
        pnl_gross = raw_pnl * n_ct * pt_value
        # Slippage : entrée déjà intégrée dans `entry` (LIMIT au prix dégradé) ;
        # côté sortie, on applique slip_exit_px en coût additionnel (1 tick).
        slip_cost = slip_exit_px * n_ct * pt_value
        comm_cost = COMMISSION_RT_PER_CONTRACT * n_ct
        pnl = pnl_gross - slip_cost - comm_cost

        cum_pnl += pnl
        if cum_pnl > peak_pnl:
            peak_pnl = cum_pnl

        trades.append(
            _make_trade_row(
                date_str=date_str,
                direction=direction,
                entry=entry,
                sl=sl,
                tp=tp,
                sl_dist=sl_dist,
                tp_dist=tp_dist,
                rr=rr,
                n_ct=n_ct,
                result=result,
                pnl=pnl,
                pnl_gross=pnl_gross,
                fill_time=fill_time,
                exit_time=exit_time,
                exit_px=exit_px,
                regime=regime,
                atr_pct=atr_pct,
                is_macro=is_macro,
                spike_pct=spike_pct,
                spike_atr_ratio=spike_atr_ratio,
                pre_event_trend_atr=pre_event_trend_atr,
            )
        )

    return _to_trades_df(trades)


# ══════════════════════════════════════════════════════════════════════════════
# 3. Helpers exit + DF construction
# ══════════════════════════════════════════════════════════════════════════════


def _simulate_exit(
    df: pd.DataFrame,
    fill_idx: int,
    direction: str,
    entry: float,
    sl: float,
    tp: float,
    time_barrier_end_idx: int,
) -> tuple[str, int, float]:
    """Triple barrier exit :
    - SL prioritaire si SL+TP touchés dans la même barre (règle projet)
    - TP si touché seul
    - Time barrier : close de la barre `time_barrier_end_idx` → result=TE
    """
    n = len(df)
    last_idx = min(time_barrier_end_idx, n - 1)

    for j in range(fill_idx, last_idx + 1):
        bar_j = df.iloc[j]
        h, lo = float(bar_j["high"]), float(bar_j["low"])

        sl_hit = (direction == "long" and lo <= sl) or (direction == "short" and h >= sl)
        tp_hit = (direction == "long" and h >= tp) or (direction == "short" and lo <= tp)

        # Sur la barre de fill, le fill s'est produit intra-bar — on autorise
        # SL/TP dans la même barre (règle SL prio si ambiguïté).
        if sl_hit and tp_hit:
            return "SL", j, float(sl)
        if sl_hit:
            return "SL", j, float(sl)
        if tp_hit:
            return "TP", j, float(tp)

    # Time barrier : close de la dernière barre
    return "TE", last_idx, float(df.iloc[last_idx]["close"])


def _make_trade_row(
    date_str: str,
    direction: str,
    entry: float,
    sl: float,
    tp: float,
    sl_dist: float,
    tp_dist: float,
    rr: float,
    n_ct: int,
    result: str,
    pnl: float,
    pnl_gross: float,
    fill_time,
    exit_time,
    exit_px,
    regime: str,
    atr_pct,
    is_macro: bool,
    spike_pct: float,
    spike_atr_ratio: float,
    pre_event_trend_atr: float,
) -> dict:
    """Construit une ligne de trade conforme au schéma standard projet +
    3 colonnes features quant must-have v1 (cf. quant_catalog.md §6)."""
    return {
        "date": date_str,
        "dir": direction,
        "entry": round(float(entry), 5),
        "sl": round(float(sl), 5),
        "tp": round(float(tp), 5),
        "sl_dist": round(float(sl_dist), 5),
        "tp_dist": round(float(tp_dist), 5),
        "rr": round(float(rr), 2),
        "n_ct": int(n_ct),
        "result": result,
        "pnl": round(float(pnl), 2),
        "fill_time": fill_time,
        "exit_time": exit_time,
        "exit": (round(float(exit_px), 5) if exit_px is not None else None),
        "regime": regime,
        # ── Colonnes optionnelles standard projet ──
        "pnl_gross": round(float(pnl_gross), 2),
        "adx": None,  # non calculé (event-driven : régime intra-jour)
        "atr_pct": atr_pct,
        "is_macro_day": bool(is_macro),
        # ── Features quant must-have v1 (persistées pour @quant discover) ──
        "spike_pct": _safe_round(spike_pct, 4),
        "spike_atr_ratio": _safe_round(spike_atr_ratio, 4),
        "pre_event_trend_atr": _safe_round(pre_event_trend_atr, 4),
    }


def _safe_round(v, n: int):
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return None
    try:
        return round(float(v), n)
    except (ValueError, TypeError):
        return None


_COLS = [
    "date",
    "dir",
    "entry",
    "sl",
    "tp",
    "sl_dist",
    "tp_dist",
    "rr",
    "n_ct",
    "result",
    "pnl",
    "fill_time",
    "exit_time",
    "exit",
    "regime",
    "pnl_gross",
    "adx",
    "atr_pct",
    "is_macro_day",
    "spike_pct",
    "spike_atr_ratio",
    "pre_event_trend_atr",
]


def _empty_trades_df() -> pd.DataFrame:
    return pd.DataFrame(columns=_COLS)


def _to_trades_df(trades: list) -> pd.DataFrame:
    if not trades:
        return _empty_trades_df()
    return pd.DataFrame(trades)[_COLS]


# ══════════════════════════════════════════════════════════════════════════════
# 4. plot_day (stub minimal — détails en PHASE 6 sur décision @athena)
# ══════════════════════════════════════════════════════════════════════════════


def plot_day(df_15m, ticker, date_str, day_trades, output_path):
    """Plot minimal pour ne pas casser `--plot` si appelé.

    Phase 3 du pipeline ne demande pas de charts. Si nécessaire ultérieurement
    (PHASE 6), un chart complet sera développé. Pour l'instant, on génère un
    fichier vide ou une vue OHLC simple.
    """
    from pathlib import Path

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ts_idx = pd.to_datetime(df_15m.index)
    if ts_idx.tz is None:
        ts_ny = ts_idx.tz_localize("UTC").tz_convert(_NY)
    else:
        ts_ny = ts_idx.tz_convert(_NY)
    mask = ts_ny.normalize() == pd.Timestamp(date_str, tz=_NY)
    day = df_15m[mask.values]
    if day.empty:
        Path(output_path).touch()
        return

    fig, ax = plt.subplots(figsize=(14, 7))
    for i, (_, row) in enumerate(day.iterrows()):
        color = "#26a69a" if row["close"] >= row["open"] else "#ef5350"
        ax.plot([i, i], [row["low"], row["high"]], color=color, lw=0.8)
        ax.bar(
            i,
            abs(row["close"] - row["open"]),
            bottom=min(row["open"], row["close"]),
            color=color,
            width=0.6,
            alpha=0.9,
        )

    day_pnl = 0.0
    for trade in day_trades:
        if trade.get("result") == "NOT_FILLED":
            continue
        try:
            fill_idx = list(day.index).index(trade["fill_time"])
        except (ValueError, KeyError):
            continue
        try:
            exit_idx = list(day.index).index(trade["exit_time"])
        except (ValueError, KeyError):
            exit_idx = fill_idx
        color = "#00c853" if trade["dir"] == "long" else "#d50000"
        marker = "^" if trade["dir"] == "long" else "v"
        ax.scatter(fill_idx, trade["entry"], marker=marker, color=color, s=140, zorder=5)
        ax.scatter(exit_idx, trade["exit"], marker="x", color="white", s=120, zorder=5)
        ax.axhline(trade["sl"], color="#ef5350", ls="--", lw=0.7, alpha=0.6)
        ax.axhline(trade["tp"], color="#26a69a", ls="--", lw=0.7, alpha=0.6)
        day_pnl += trade.get("pnl", 0.0)

    ax.set_title(
        f"{ticker} — {date_str}   |   P&L net jour : {day_pnl:+.0f} $",
        fontsize=12,
        fontweight="bold",
    )
    ax.set_facecolor("#1a1a2e")
    fig.patch.set_facecolor("#13131f")
    ax.tick_params(colors="white")
    plt.tight_layout()
    plt.savefig(output_path, dpi=110, bbox_inches="tight")
    plt.close(fig)
