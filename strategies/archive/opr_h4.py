"""
Stratégie `opr_h4-v1` — variante recherche d'OPR avec filtre cloud Ichimoku 15m.

Hypothèse H4 (issue du chartist mode idea, charts hebdomadaires NQ1) :
  Les setups OPR déclenchés alors que le prix est DANS le cloud Ichimoku 15m
  sont des faux signaux fréquents (zone de fair value contestée). Les filtrer
  doit améliorer la qualité au prix d'un volume de trades réduit.

Edge théorique :
  Le cloud Ichimoku (Senkou A / Senkou B décalés +26) matérialise une zone
  d'équilibre offre/demande projetée 26 barres en avance. Quand le prix
  oscille dans cette zone, les breakouts OPR (technique de continuation) ont
  une probabilité accrue d'être absorbés par les vendeurs de volatilité
  intraday. Le filtre exige une localisation **hors-cloud** + buffer ATR
  pour s'assurer que le trend dominant est suffisamment marqué.

Filtre H4 — appliqué AVANT armement de l'ordre limite OPR :
  À la bougie i de décision (trigger pullback OPR), regarder bougie i-1 :
    S_A_prev   = senkou_a[i-1]      (déjà shifté +26 dans compute_ichimoku)
    S_B_prev   = senkou_b[i-1]      (déjà shifté +26)
    cloud_high = max(S_A_prev, S_B_prev)
    cloud_low  = min(S_A_prev, S_B_prev)
    close_prev = close[i-1]
    atr_prev   = ATR_Wilder(14)[i-1]   (sur barres 15m, série complète préalable)
  Décision :
    LONG  autorisé si close_prev > cloud_high + BUFFER × atr_prev
    SHORT autorisé si close_prev < cloud_low  - BUFFER × atr_prev
    Sinon → REJET

Anti-look-ahead :
  • Senkou A/B portent `shift(+26)`. Donc `senkou_a.iloc[i-1]` n'utilise que
    `tenkan/kijun` calculés sur barres antérieures à i-1-26 = i-27.
  • ATR(14) Wilder à i-1 ne dépend que des barres ≤ i-1.
  • close[i-1] est strictement antérieur à la décision en barre i.

NB : cette stratégie est un CLONE de la logique OPR de production (core/opr.py)
augmentée du filtre H4. On ne dépend pas de core/opr.py pour éviter le couplage
et pouvoir bumper indépendamment OPR_H4_STRATEGY_VERSION sans toucher la prod.

Frictions : opr-v4 baseline n'applique pas de slippage/commissions ; pour
comparaison apples-to-apples, opr_h4-v1 reste sur le même régime brut. Cf.
rapport opr_h4 pour explication détaillée.
"""

from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

# Reproductibilité (le module ne tire pas d'aléatoire mais la convention du
# projet l'impose en tête de tout module potentiellement stochastique).
np.random.seed(42)

from config import (
    CONSEC_LOSS_PAUSE_DAYS,
    CUTOFF_HOUR_UTC,
    DAILY_LOCKIN_THRESHOLD,
    DAILY_STOP_AFTER_SL,
    INSTRUMENTS,
    OPR_ATR_PERIOD,
    OPR_ENABLED,
    OPR_H4_BUFFER_ATR,
    OPR_H4_ICHIMOKU_KIJUN,
    OPR_H4_ICHIMOKU_SENKOU_B,
    OPR_H4_ICHIMOKU_SHIFT,
    OPR_H4_ICHIMOKU_TENKAN,
    OPR_H4_INTRADAY_ATR_PERIOD,
    OPR_H4_STRATEGY_VERSION,
    OPR_H4_TICKERS,
    OPR_MAX_TRADES_PER_DAY,
    OPR_SESSION_END,
    OPR_SL_ATR_MULT,
    OPR_SL_MIN_POINTS,
    OPR_TIMEZONE,
    OPR_TP_ATR_MULT,
    OPR_WINDOW_END,
    OPR_WINDOW_START,
    RISK_PER_TRADE_USD,
)
from core.explore_chart import compute_ichimoku
from core.risk_topstep import trade_allowed

# ── Identité de la stratégie ─────────────────────────────────────────────────
STRATEGY_ID = OPR_H4_STRATEGY_VERSION  # "opr_h4-v1"
TICKERS = list(OPR_H4_TICKERS)  # ["MES1", "NQ1", "YM1"]
CSV_SUFFIX = "_opr_h4"
CSV_TIMEFRAME = "m15"

# ── Grille d'optimisation (walk-forward via core/optimizer.py) ───────────────
# Une seule dimension calibrée (anti-curve-fitting). SL/TP repris d'opr-v4 sans
# re-tunning pour isoler l'effet du filtre Ichimoku.
PARAM_GRID = {
    "buffer_atr": [0.0, 0.3, 0.5, 0.8],
}


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers timezone (clonés de core/opr.py — sans coupling)
# ═══════════════════════════════════════════════════════════════════════════════


def _ny_session_view(
    df_15m: pd.DataFrame, day_ny: pd.Timestamp, tz: ZoneInfo
) -> pd.DataFrame | None:
    """Vue de session bornée [WINDOW_START NY, SESSION_END NY] inclusivement."""
    if df_15m.empty:
        return None
    if df_15m.index.tz is None:
        idx_ny = df_15m.index.tz_localize("UTC").tz_convert(tz)
    else:
        idx_ny = df_15m.index.tz_convert(tz)
    df_ny = df_15m.copy()
    df_ny.index = idx_ny

    h_start, m_start = OPR_WINDOW_START
    h_end, m_end = OPR_SESSION_END
    session_start = day_ny.replace(hour=h_start, minute=m_start, second=0, microsecond=0)
    session_end = day_ny.replace(hour=h_end, minute=m_end, second=0, microsecond=0)
    mask = (df_ny.index >= session_start) & (df_ny.index <= session_end)
    return df_ny.loc[mask]


def _opr_bar(df_session_ny: pd.DataFrame) -> pd.Series | None:
    """Bougie OPR = pic de volume dans la fenêtre [OPR_WINDOW_START, OPR_WINDOW_END[."""
    if df_session_ny is None or df_session_ny.empty:
        return None
    if "volume" not in df_session_ny.columns:
        return None
    h_start, m_start = OPR_WINDOW_START
    h_end, m_end = OPR_WINDOW_END
    day_anchor = df_session_ny.index[0].normalize()
    win_start = day_anchor.replace(hour=h_start, minute=m_start)
    win_end = day_anchor.replace(hour=h_end, minute=m_end)
    cand = df_session_ny[(df_session_ny.index >= win_start) & (df_session_ny.index < win_end)]
    if cand.empty or float(cand["volume"].sum()) <= 0:
        return None
    peak_ts = cand["volume"].idxmax()
    return df_session_ny.loc[peak_ts]


def _compute_atr_daily(df_15m: pd.DataFrame, day_ny: pd.Timestamp, period: int) -> float | None:
    """ATR journalier — référence pour SL/TP (clone de core/opr.py, sans leak)."""
    if df_15m.empty or period <= 0:
        return None
    day_start_ny = day_ny.replace(hour=0, minute=0, second=0, microsecond=0)
    if day_start_ny.tzinfo is None:
        return None
    day_start_utc = day_start_ny.tz_convert("UTC").tz_localize(None)
    if df_15m.index.tz is not None:
        before = df_15m[df_15m.index < day_start_ny]
    else:
        before = df_15m[df_15m.index < day_start_utc]
    if len(before) < period * 80:
        return None
    daily = (
        before.resample("D")
        .agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
            }
        )
        .dropna()
    )
    if len(daily) < period + 1:
        return None
    high = daily["high"]
    low = daily["low"]
    prev_close = daily["close"].shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(period).mean().dropna()
    if atr.empty:
        return None
    val = float(atr.iloc[-1])
    return val if val > 0 else None


def _wilder_atr_15m(df_15m: pd.DataFrame, period: int) -> pd.Series:
    """ATR(period) de Wilder sur barres 15m — pour le filtre Ichimoku.

    Calculé une fois sur toute la série, puis lu à `iloc[i-1]` au moment de
    la décision. Pas de leak : l'ATR à la barre `i-1` ne dépend que des
    barres ≤ i-1.
    """
    high, low, close = df_15m["high"], df_15m["low"], df_15m["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    # Wilder smoothing : EMA avec alpha = 1/period, adjust=False
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


# ═══════════════════════════════════════════════════════════════════════════════
# Trade builder (clone de core/opr.py)
# ═══════════════════════════════════════════════════════════════════════════════


def _make_signal(
    ticker: str,
    direction: str,
    entry: float,
    opr_high: float,
    opr_low: float,
    trigger_time: pd.Timestamp,
    sl_pts: float,
    tp_pts: float,
    atr_daily: float,
) -> dict | None:
    inst = INSTRUMENTS[ticker]
    tick = inst["tick_size"]
    dpp = inst["dollar_per_point"]

    def _tick(p: float) -> float:
        return round(round(p / tick) * tick, 10)

    if sl_pts <= 0 or tp_pts <= 0:
        return None
    if direction == "long":
        sl_price = _tick(entry - sl_pts)
        tp_price = _tick(entry + tp_pts)
    else:
        sl_price = _tick(entry + sl_pts)
        tp_price = _tick(entry - tp_pts)
    sl_pts = abs(entry - sl_price)
    tp_pts = abs(entry - tp_price)
    if sl_pts <= 0 or tp_pts <= 0:
        return None
    n_ct = int(RISK_PER_TRADE_USD / (sl_pts * dpp))
    if n_ct <= 0:
        return None
    risk = n_ct * sl_pts * dpp
    return {
        "ticker": ticker,
        "strategy": "OPR_H4",
        "direction": direction,
        "entry": float(entry),
        "sl": float(sl_price),
        "tp": float(tp_price),
        "sl_dist": float(sl_pts),
        "tp_dist": float(tp_pts),
        "rr": round(tp_pts / sl_pts, 2),
        "n_ct": int(n_ct),
        "risk": float(risk),
        "opr_high": float(opr_high),
        "opr_low": float(opr_low),
        "opr_range": float(opr_high - opr_low),
        "atr_daily": float(atr_daily),
        "trigger_time": str(trigger_time),
        "zone_low": float(opr_low),
        "zone_high": float(opr_high),
        "regime": "OPR_H4",
    }


def _check_trigger(bar: pd.Series, opr_high: float, opr_low: float) -> str | None:
    o = float(bar["open"])
    c = float(bar["close"])
    if o < opr_high and c > opr_high:
        return "long"
    if o > opr_low and c < opr_low:
        return "short"
    return None


def _bar_hits(direction: str, level: float, bar: pd.Series) -> bool:
    return float(bar["low"]) <= level <= float(bar["high"])


# ═══════════════════════════════════════════════════════════════════════════════
# Filtre H4 — cloud Ichimoku 15m + buffer ATR
# ═══════════════════════════════════════════════════════════════════════════════


def _passes_h4_cloud_filter(
    direction: str,
    bar_idx: int,
    senkou_a_15m: pd.Series,
    senkou_b_15m: pd.Series,
    close_15m: pd.Series,
    atr_15m: pd.Series,
    buffer_atr: float,
) -> bool:
    """
    Filtre H4 : à la bougie `bar_idx` (décision d'entrée OPR), inspecte la
    bougie i-1 du DataFrame global 15m.

    Retourne True si le close[i-1] est hors-cloud + buffer × ATR[i-1] dans le
    sens du trade, False sinon.

    Tous les inputs (senkou_a, senkou_b, close, atr) sont les séries 15m
    complètes alignées sur l'index UTC du df global. Aucune valeur > i-1
    n'est lue → anti look-ahead garanti.
    """
    i = bar_idx
    if i <= 0:
        return False
    j = i - 1
    if j >= len(senkou_a_15m):
        return False

    s_a = senkou_a_15m.iloc[j]
    s_b = senkou_b_15m.iloc[j]
    c_prev = close_15m.iloc[j]
    a_prev = atr_15m.iloc[j]

    # Toute valeur manquante (warm-up Ichimoku ~52+26=78 barres ; ATR 14 ~14
    # barres) → on rejette par défaut (signal non évaluable).
    if pd.isna(s_a) or pd.isna(s_b) or pd.isna(c_prev) or pd.isna(a_prev):
        return False
    if a_prev <= 0:
        return False

    cloud_high = max(float(s_a), float(s_b))
    cloud_low = min(float(s_a), float(s_b))
    buf = float(buffer_atr) * float(a_prev)

    if direction == "long":
        return float(c_prev) > cloud_high + buf
    else:
        return float(c_prev) < cloud_low - buf


# ═══════════════════════════════════════════════════════════════════════════════
# Moteur de session (1 jour) — clone de core/opr.run_opr_day + filtre H4
# ═══════════════════════════════════════════════════════════════════════════════


def _run_opr_h4_day(
    df_15m_full: pd.DataFrame,
    senkou_a_15m: pd.Series,
    senkou_b_15m: pd.Series,
    atr_15m: pd.Series,
    ticker: str,
    day_ny: pd.Timestamp,
    sl_mult: float,
    tp_mult: float,
    buffer_atr: float,
) -> tuple[list[dict], list[dict]]:
    """
    Joue une session 1 jour. Identique à core.opr.run_opr_day SAUF :
      • Les filtres OPR_MIN_EXCURSION_ATR / OPR_MAX_VOL_ZSCORE ne sont PAS
        appliqués (on isole le contribution propre du filtre H4 cloud).
      • Avant d'armer l'ordre limite, on consulte le filtre H4 cloud Ichimoku.
      • SL/TP multipliers passés en argument (pour les overrides params).

    Inputs :
      df_15m_full   : DataFrame 15m complet (index UTC naïf), pour récupérer
                      la position absolue de la bougie trigger et lire les
                      séries ichimoku/ATR alignées.
      senkou_a/b_15m, atr_15m : séries pré-calculées sur df_15m_full (mêmes
                      index).
    """
    if not OPR_ENABLED:
        return [], []

    tz = ZoneInfo(OPR_TIMEZONE)
    df_session = _ny_session_view(df_15m_full, day_ny, tz)
    if df_session is None or len(df_session) < 2:
        return [], []

    opr_bar = _opr_bar(df_session)
    if opr_bar is None:
        return [], []
    opr_high = float(opr_bar["high"])
    opr_low = float(opr_bar["low"])
    if opr_high <= opr_low:
        return [], []

    inst = INSTRUMENTS[ticker]
    dpp = inst["dollar_per_point"]

    atr_daily = _compute_atr_daily(df_15m_full, day_ny, OPR_ATR_PERIOD)
    if atr_daily is None:
        return [], []

    sl_floor = float(OPR_SL_MIN_POINTS.get(ticker, 0.0))
    sl_pts = max(sl_mult * atr_daily, sl_floor)
    tp_pts = tp_mult * atr_daily
    if sl_pts <= 0 or tp_pts <= 0:
        return [], []

    h_close, m_close = OPR_SESSION_END
    session_end_t = day_ny.replace(hour=h_close, minute=m_close, second=0, microsecond=0)
    opr_ts_ny = opr_bar.name

    pending: dict | None = None
    position: dict | None = None
    signals: list[dict] = []
    trades: list[dict] = []
    n_fills = 0

    bars = df_session
    timestamps = bars.index

    # Pré-calcul : map du timestamp tz-aware NY → position absolue dans
    # df_15m_full (index UTC naïf), pour aller chercher senkou/atr en i-1.
    # On convertit chaque ts NY en UTC naïf et on cherche son index dans
    # df_15m_full.
    idx_full = df_15m_full.index
    # Construit un dict {ts_utc_naive: int_pos}
    pos_map: dict[pd.Timestamp, int] = {}
    for k, ts_utc in enumerate(idx_full):
        pos_map[ts_utc] = k

    for i_loc, ts in enumerate(timestamps):
        bar = bars.iloc[i_loc]

        if ts <= opr_ts_ny:
            continue

        # Gestion position ouverte (SL/TP/TE) — identique core/opr.py
        if position is not None:
            direction = position["direction"]
            entry = position["entry"]
            sl = position["sl"]
            tp = position["tp"]
            n_ct = position["n_ct"]

            if direction == "long":
                hit_sl = float(bar["low"]) <= sl
                hit_tp = float(bar["high"]) >= tp
            else:
                hit_sl = float(bar["high"]) >= sl
                hit_tp = float(bar["low"]) <= tp

            result = None
            exit_price = None
            if hit_sl and hit_tp:
                # Conservatif : SL prioritaire si ambigu (cf. SKILL anti-pattern)
                bull = float(bar["close"]) >= float(bar["open"])
                if direction == "long":
                    result, exit_price = ("TP", tp) if bull else ("SL", sl)
                else:
                    result, exit_price = ("TP", tp) if not bull else ("SL", sl)
            elif hit_sl:
                result, exit_price = "SL", sl
            elif hit_tp:
                result, exit_price = "TP", tp

            if result is None and ts >= session_end_t:
                result = "TE"
                exit_price = float(bar["close"])

            if result is not None:
                pnl_pts = (exit_price - entry) if direction == "long" else (entry - exit_price)
                pnl = n_ct * pnl_pts * dpp
                trades.append(
                    {
                        "_signal_idx": position["signal_idx"],
                        "result": result,
                        "pnl": float(pnl),
                        "exit": float(exit_price),
                        "fill_time": str(position["fill_time"]),
                        "exit_time": str(ts),
                    }
                )
                position = None
                continue
            continue

        # Tentative de fill ordre limite
        if pending is not None:
            level = pending["entry"]
            direction = pending["direction"]
            if ts >= session_end_t:
                pending = None
            elif _bar_hits(direction, level, bar):
                if n_fills >= OPR_MAX_TRADES_PER_DAY:
                    pending = None
                else:
                    sig = pending["signal"]
                    sig_idx = pending["signal_idx"]
                    position = {
                        "direction": direction,
                        "entry": sig["entry"],
                        "sl": sig["sl"],
                        "tp": sig["tp"],
                        "n_ct": sig["n_ct"],
                        "fill_time": ts,
                        "signal_idx": sig_idx,
                    }
                    n_fills += 1
                    pending = None
                    # check immédiat SL/TP sur la bougie de fill
                    sl = position["sl"]
                    tp = position["tp"]
                    if direction == "long":
                        hit_sl = float(bar["low"]) <= sl
                        hit_tp = float(bar["high"]) >= tp
                    else:
                        hit_sl = float(bar["high"]) >= sl
                        hit_tp = float(bar["low"]) <= tp
                    result = None
                    exit_price = None
                    if hit_sl and hit_tp:
                        bull = float(bar["close"]) >= float(bar["open"])
                        if direction == "long":
                            result, exit_price = ("TP", tp) if bull else ("SL", sl)
                        else:
                            result, exit_price = ("TP", tp) if not bull else ("SL", sl)
                    elif hit_sl:
                        result, exit_price = "SL", sl
                    elif hit_tp:
                        result, exit_price = "TP", tp
                    if result is not None:
                        pnl_pts = (
                            (exit_price - position["entry"])
                            if direction == "long"
                            else (position["entry"] - exit_price)
                        )
                        pnl = position["n_ct"] * pnl_pts * dpp
                        trades.append(
                            {
                                "_signal_idx": position["signal_idx"],
                                "result": result,
                                "pnl": float(pnl),
                                "exit": float(exit_price),
                                "fill_time": str(position["fill_time"]),
                                "exit_time": str(ts),
                            }
                        )
                        position = None
                    continue

        # Pas de position / pas de fill : chercher nouveau trigger
        if position is not None or pending is not None:
            continue
        if ts >= session_end_t:
            continue
        if n_fills >= OPR_MAX_TRADES_PER_DAY:
            continue

        trig = _check_trigger(bar, opr_high, opr_low)
        if trig is None:
            continue

        # ── Filtre H4 cloud Ichimoku ─────────────────────────────────────
        # On a besoin de la position globale `bar_idx` dans df_15m_full.
        # `bar.name` est le timestamp tz-aware NY ; on le convertit en UTC
        # naïf pour matcher pos_map.
        ts_utc_naive = ts.tz_convert("UTC").tz_localize(None)
        bar_idx = pos_map.get(ts_utc_naive)
        if bar_idx is None:
            # Fallback robuste : recherche par .get_loc (ne devrait pas arriver)
            try:
                bar_idx = int(idx_full.get_loc(ts_utc_naive))
            except KeyError:
                continue

        if not _passes_h4_cloud_filter(
            direction=trig,
            bar_idx=bar_idx,
            senkou_a_15m=senkou_a_15m,
            senkou_b_15m=senkou_b_15m,
            close_15m=df_15m_full["close"],
            atr_15m=atr_15m,
            buffer_atr=buffer_atr,
        ):
            continue

        entry_level = opr_high if trig == "long" else opr_low
        sig = _make_signal(
            ticker=ticker,
            direction=trig,
            entry=entry_level,
            opr_high=opr_high,
            opr_low=opr_low,
            trigger_time=ts,
            sl_pts=sl_pts,
            tp_pts=tp_pts,
            atr_daily=atr_daily,
        )
        if sig is None:
            continue
        sig_idx = len(signals)
        signals.append(sig)
        pending = {
            "direction": trig,
            "entry": entry_level,
            "signal": sig,
            "signal_idx": sig_idx,
            "armed_at": ts,
        }

    # Fin de boucle
    if position is not None:
        last_bar = bars.iloc[-1]
        exit_price = float(last_bar["close"])
        direction = position["direction"]
        pnl_pts = (
            (exit_price - position["entry"])
            if direction == "long"
            else (position["entry"] - exit_price)
        )
        pnl = position["n_ct"] * pnl_pts * dpp
        trades.append(
            {
                "_signal_idx": position["signal_idx"],
                "result": "TE",
                "pnl": float(pnl),
                "exit": float(exit_price),
                "fill_time": str(position["fill_time"]),
                "exit_time": str(timestamps[-1]),
            }
        )
        position = None
    if pending is not None:
        trades.append(
            {
                "_signal_idx": pending["signal_idx"],
                "result": "NOT_FILLED",
                "pnl": 0.0,
                "exit": None,
                "fill_time": None,
                "exit_time": None,
            }
        )
        pending = None

    # 1:1 signaux ↔ trades par index
    indexed = {t["_signal_idx"]: t for t in trades}
    out_trades: list[dict] = []
    for i in range(len(signals)):
        t = indexed.get(
            i,
            {
                "result": "NOT_FILLED",
                "pnl": 0.0,
                "exit": None,
                "fill_time": None,
                "exit_time": None,
            },
        )
        t = {k: v for k, v in t.items() if k != "_signal_idx"}
        out_trades.append(t)
    return signals, out_trades


# ═══════════════════════════════════════════════════════════════════════════════
# Interface plug-and-play : run_backtest
# ═══════════════════════════════════════════════════════════════════════════════


def run_backtest(
    df_15m: pd.DataFrame,
    ticker: str,
    tf=None,
    params: dict = None,
    topstep_guard: bool = True,
) -> pd.DataFrame:
    """
    Backtest OPR_H4 complet jour par jour.

    params accepté :
        buffer_atr : float    — surcharge OPR_H4_BUFFER_ATR
        sl_mult    : float    — surcharge OPR_SL_ATR_MULT[ticker]
        tp_mult    : float    — surcharge OPR_TP_ATR_MULT[ticker]
    """
    if ticker not in TICKERS:
        return pd.DataFrame()

    tz = ZoneInfo(OPR_TIMEZONE)

    # Params (priorité aux overrides)
    sl_mult = float(OPR_SL_ATR_MULT.get(ticker, 0.20))
    tp_mult = float(OPR_TP_ATR_MULT.get(ticker, 0.40))
    buffer_atr = float(OPR_H4_BUFFER_ATR)
    if params:
        sl_mult = float(params.get("sl_mult", sl_mult))
        tp_mult = float(params.get("tp_mult", tp_mult))
        buffer_atr = float(params.get("buffer_atr", buffer_atr))

    # ── Pré-calcul des indicateurs 15m (une fois sur toute la série) ────────
    ichi = compute_ichimoku(
        df_15m,
        tenkan=OPR_H4_ICHIMOKU_TENKAN,
        kijun=OPR_H4_ICHIMOKU_KIJUN,
        senkou_b=OPR_H4_ICHIMOKU_SENKOU_B,
        shift=OPR_H4_ICHIMOKU_SHIFT,
    )
    senkou_a_15m = ichi["senkou_a"]
    senkou_b_15m = ichi["senkou_b"]
    atr_15m = _wilder_atr_15m(df_15m, OPR_H4_INTRADAY_ATR_PERIOD)

    # ── Boucle jour-par-jour ─────────────────────────────────────────────────
    trades_out = []
    cum_pnl = peak_pnl = 0.0
    consec_loss_days = 0

    if df_15m.index.tz is None:
        idx_ny = df_15m.index.tz_localize("UTC").tz_convert(tz)
    else:
        idx_ny = df_15m.index.tz_convert(tz)
    ny_days = pd.DatetimeIndex(idx_ny.normalize().unique()).sort_values()

    for day_ny in ny_days:
        ds = day_ny.strftime("%Y-%m-%d")

        if CONSEC_LOSS_PAUSE_DAYS > 0 and consec_loss_days >= CONSEC_LOSS_PAUSE_DAYS:
            consec_loss_days = 0
            continue

        if topstep_guard:
            allowed, _ = trade_allowed(day_pnl=0.0, cum_pnl=cum_pnl, peak_pnl=peak_pnl)
            if not allowed:
                continue

        signals, sim_results = _run_opr_h4_day(
            df_15m_full=df_15m,
            senkou_a_15m=senkou_a_15m,
            senkou_b_15m=senkou_b_15m,
            atr_15m=atr_15m,
            ticker=ticker,
            day_ny=day_ny,
            sl_mult=sl_mult,
            tp_mult=tp_mult,
            buffer_atr=buffer_atr,
        )
        if not signals:
            continue

        day_trades = []
        for sig, res in zip(signals, sim_results):
            day_trades.append(
                {
                    "date": ds,
                    "strategy": "OPR_H4",
                    "dir": sig["direction"],
                    "entry": sig["entry"],
                    "sl": sig["sl"],
                    "tp": sig["tp"],
                    "sl_dist": sig["sl_dist"],
                    "tp_dist": sig["tp_dist"],
                    "rr": sig["rr"],
                    "n_ct": sig["n_ct"],
                    "risk_$": sig["risk"],
                    "regime": sig["regime"],
                    "zone_low": sig["zone_low"],
                    "zone_high": sig["zone_high"],
                    "trigger_time": sig.get("trigger_time"),
                    **res,
                }
            )

        filled = [t for t in day_trades if t["result"] != "NOT_FILLED"]
        not_filled = [t for t in day_trades if t["result"] == "NOT_FILLED"]
        filled.sort(key=lambda t: t.get("fill_time") or "")

        kept = []
        breaker_armed = False
        running_pnl = 0.0
        cancelled_cb = []
        for t in filled:
            if breaker_armed:
                cancelled_cb.append(t)
                continue
            kept.append(t)
            running_pnl += t["pnl"]
            if (
                DAILY_STOP_AFTER_SL
                and t["result"] == "SL"
                or DAILY_LOCKIN_THRESHOLD > 0
                and running_pnl >= DAILY_LOCKIN_THRESHOLD
            ):
                breaker_armed = True

        for t in cancelled_cb:
            t.update(
                {
                    "result": "NOT_FILLED",
                    "pnl": 0,
                    "fill_time": None,
                    "exit_time": None,
                    "exit": None,
                }
            )

        trades_out.extend(kept + not_filled + cancelled_cb)

        day_pnl = sum(t["pnl"] for t in kept)
        cum_pnl += day_pnl
        if cum_pnl > peak_pnl:
            peak_pnl = cum_pnl
        if day_pnl < 0:
            consec_loss_days += 1
        elif day_pnl > 0:
            consec_loss_days = 0

    return pd.DataFrame(trades_out)


# ═══════════════════════════════════════════════════════════════════════════════
# Chart d'une journée
# ═══════════════════════════════════════════════════════════════════════════════


def plot_day(
    df_15m: pd.DataFrame,
    ticker: str,
    date_str: str,
    day_trades: list,
    output_path: str,
):
    """Génère un chart OPR_H4 d'une journée (réutilise analysis_chart)."""
    from core.analysis_chart import plot_day_analysis

    tz = ZoneInfo(OPR_TIMEZONE)
    day_ny = pd.Timestamp(date_str).tz_localize(tz)

    df_session = _ny_session_view(df_15m, day_ny, tz)
    opr_bar = _opr_bar(df_session) if df_session is not None else None

    cutoff = pd.Timestamp(f"{date_str} {CUTOFF_HOUR_UTC:02d}:00:00")
    us_end = day_ny.replace(hour=16, minute=30).tz_convert("UTC").tz_localize(None)

    zones_for_chart = []
    if opr_bar is not None:
        opr_high = float(opr_bar["high"])
        opr_low = float(opr_bar["low"])
        zones_for_chart.append(
            {
                "low": opr_low,
                "high": opr_high,
                "mid": (opr_high + opr_low) / 2.0,
                "quality": 100.0,
                "n_tf": 1,
                "touches": 1,
                "tfs": ["OPR_H4"],
                "dominant_tf": "OPR_H4",
                "start_time": opr_bar.name.tz_convert("UTC").tz_localize(None),
            }
        )

    plot_day_analysis(
        df_15m=df_15m,
        ticker=ticker,
        date_str=date_str,
        cutoff=cutoff,
        us_end=us_end,
        zones=zones_for_chart,
        signals=[t for t in day_trades if t.get("result") != "NOT_FILLED"],
        trades=day_trades,
        regime=None,
        alignment_score=None,
        pm_features=None,
        vol_features=None,
        output_path=output_path,
    )
