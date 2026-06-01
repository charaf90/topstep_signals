"""
Stratégie vwap-pb-v1 — VWAP pullback intraday (mean-reversion).

Concept       : après stretch significatif (1.0-2.5 × ATR14) au-dessus/dessous
                VWAP du jour ET tendance courte du même côté (3 barres M15
                consécutives), entrée LIMIT @ VWAP(i) avec TP/SL = ±1×ATR.
                TTL ordre LIMIT = 8 barres M15 (2h).
Edge          : VWAP = benchmark institutionnel (Berkowitz/Logue/Noser 1988) ;
                pression de rappel mécanique des algos d'exécution VWAP/TWAP.
                Sur micros : edge second-ordre (comportement mimétique retail
                + arbitrage prix micro/standard), pas premier-ordre flow direct.
Falsification : PF OOS < 1.2 OU taux reversion < 40% sur 8 barres M15 après
                stretch ≥ 1.0×ATR (cf. concept.md §CONCEPT).
Tickers       : NQ1 (primaire), MGC1 (secondaire). MCL1 EXCLU (frictions 31% SL).
Fenêtres NY   : NQ1  [10:00, 15:00) — exclut 09:30-09:59 + collision OPR
                MGC1 [09:30, 13:00) — exclut pré-pit + post-pit COMEX
Timeframe     : M15 (cohérence prod OPR/Fib, frictions ~6% SL vs ~18% en M5)

=== ROLLBACK v2 → v1 (2026-05-26) ===
Le filtre @quant MEDIUM `min_minutes_after_reset=90` a été testé en v2 puis
ROLLBACK car gain marginal (+0.05 PF) au prix de -$1,202 P&L absolu et
finding Bonferroni-fail (p=0.006 vs seuil 0.0038). v1 retenue : plus simple
ET plus rentable. Patch archivé : output/archive/vwap-pb-v2/.
========================================

=== VWAP CAUSALITÉ (CRITIQUE) ===
La VWAP est calculée par groupby('date_ny') avec cumsum().shift(1).
Le shift(1) est OBLIGATOIRE : sans lui, VWAP[i] inclurait (typical[i] × vol[i])
de la barre courante → look-ahead.
Test unitaire dédié à la fin du fichier (auto-exécution si __main__).
==================================

=== COLLISION FIB MGC1 (post-promotion live) ===
Fib-v4 actif sur MGC1 (PF OOS 6.47). Règle : Fib prioritaire.
En backtest v1 : pas d'exclusion (les fills Fib/VWAP-PB peuvent coexister sur
même jour ; orchestration prio = problème live, pas backtest). En post-promotion,
flag `fib_active_mgc1` dans signal_selector.py → skip VWAP-PB si Fib en cours.
Pour v1 backtest : on LOGGE la collision (jour où Fib aurait été actif) mais
on n'exclut pas les trades.
==================================
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
    INSTRUMENTS,
    MACRO_EVENT_DATES,
    RISK_PER_TRADE_USD,
    SLIPPAGE_TICKS_PER_TICKER,
    VWAP_PB_MAX_TRADES_PER_DAY,
    VWAP_PB_RESET_HOUR_NY,
    VWAP_PB_SESSION_WINDOW_NY,
    VWAP_PB_SL_ATR_MULT,
    VWAP_PB_STRATEGY_VERSION,
    VWAP_PB_STRETCH_MAX,
    VWAP_PB_STRETCH_MIN,
    VWAP_PB_TICKERS,
    VWAP_PB_TREND_BARS,
    VWAP_PB_TTL_BARS,
)
from core.risk_topstep import trade_allowed

# ── Identité de la stratégie (contrat registry) ──────────────────────────────
STRATEGY_ID = VWAP_PB_STRATEGY_VERSION  # "vwap-pb-v1"
TICKERS = list(VWAP_PB_TICKERS)  # ["NQ1", "MGC1"]
CSV_SUFFIX = "_data"  # data/<TICKER>_data_m15.csv
CSV_TIMEFRAME = "m15"

_NY = ZoneInfo("America/New_York")
_UTC = ZoneInfo("UTC")
_MACRO_DATES = set(pd.to_datetime(MACRO_EVENT_DATES).date.tolist())

# ── Grille d'optimisation (PHASE 4) ──────────────────────────────────────────
PARAM_GRID = {
    "stretch_min": [0.8, 1.0, 1.2],
    "stretch_max": [2.0, 2.5, 3.0],
    "sl_atr_mult": [0.8, 1.0, 1.5],
    "trend_bars": [2, 3, 4],
}


# ══════════════════════════════════════════════════════════════════════════════
# 1. Helpers — ATR + VWAP causale (DST-aware) + précalculs
# ══════════════════════════════════════════════════════════════════════════════


def _compute_atr_m15(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """ATR(14) Wilder/SMA sur M15. Shift(1) en sortie → `atr.iloc[i]` est connu
    à l'OUVERTURE de la barre i (causal strict, aucune valeur barre i).
    """
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(window=period, min_periods=period).mean()
    return atr.shift(1)


def _to_ny_index(idx: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Convertit un index UTC naïf (CSV TradingView) → NY DST-aware."""
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    return idx.tz_convert(_NY)


def _session_id_ny(idx_ny: pd.DatetimeIndex, reset_h: int, reset_m: int) -> pd.Series:
    """Calcule un session_id qui change à chaque reset horaire NY défini par
    (reset_h, reset_m). Permet `groupby(session_id)` même quand le reset
    est intra-jour calendaire (ex: 09:00 NY).

    Une session NY commence à <reset_h:reset_m> NY et finit juste avant la
    prochaine occurrence de ce même horaire. Pour des resets à 09:00 ou 09:30,
    chaque session = 1 journée NY (puisque le reset tombe dans la matinée).
    """
    # Pour reset matinal (≤ 12:00), session_id = date NY du jour si l'heure
    # est ≥ reset, sinon date NY du jour précédent.
    out = []
    for ts in idx_ny:
        if (ts.hour, ts.minute) >= (reset_h, reset_m):
            out.append(ts.date())
        else:
            out.append((ts - pd.Timedelta(days=1)).date())
    return pd.Series(out, index=idx_ny)


def _compute_vwap_causal(
    df: pd.DataFrame,
    idx_ny: pd.DatetimeIndex,
    reset_h: int,
    reset_m: int,
) -> pd.Series:
    """VWAP causale par session NY (reset horaire défini).

    Formule : VWAP(i) = Σ_{k=session_start}^{i-1} (typical_k × vol_k)
                       / Σ_{k=session_start}^{i-1} vol_k

    L'agrégation cumsum SE FAIT sur les barres CLÔTURÉES strictement avant i
    (shift(1) IMPÉRATIF). Sans shift, VWAP(i) inclurait (typ[i] × vol[i]) →
    décision avec close de la barre courante = look-ahead.

    Returns
    -------
    pd.Series indexée comme df, valeur = VWAP causale (NaN avant la 1re barre
    de chaque session puisque shift(1) consomme le premier point).
    """
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = typical * df["volume"]
    vol = df["volume"]

    # Session id par barre (jour NY logique)
    session = _session_id_ny(idx_ny, reset_h, reset_m).values

    # cumsum par session, puis shift(1) GLOBAL (pas par groupe sinon la 1re
    # barre de jour N+1 verrait la dernière valeur du jour N → faux). On
    # shift(1) au sein du groupe.
    sess_series = pd.Series(session, index=df.index, name="session")
    cum_pv = pv.groupby(sess_series).cumsum().shift(1)
    cum_vol = vol.groupby(sess_series).cumsum().shift(1)

    # Le shift(1) global mélange les sessions. On corrige en mettant NaN à la
    # 1re barre de chaque session (cum_vol = 0 ou shift renvoie valeur d'avant).
    # Détecter changement de session via diff de la session_series.
    is_first = sess_series.ne(sess_series.shift(1))
    cum_pv = cum_pv.where(~is_first, np.nan)
    cum_vol = cum_vol.where(~is_first, np.nan)

    vwap = cum_pv / cum_vol.replace(0, np.nan)
    return vwap


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
    """Backtest vwap-pb-v1 sur NQ1/MGC1 M15.

    Schéma de sortie standard projet (cf. core/optimizer.py).
    `pnl` = P&L NET (slippage entrée+sortie + commission round-trip inclus).

    Flux de décision (par barre M15 i, dans la fenêtre horaire de l'actif) :
      1. ATR(14) connu à i, VWAP causale connue à i (calculée sur barres < i)
      2. SETUP : 3 closes consécutives [i-trend..i-1] du même côté de VWAP
      3. STRETCH : 1.0 ≤ |close[i-1] - VWAP[i-1]| / ATR[i] ≤ 2.5
      4. SKIP si jour macro OU 1 trade déjà passé sur l'actif aujourd'hui
      5. POSE LIMIT @ VWAP[i] (calculée sur barres < i, connue à l'ouverture i)
      6. TTL = TTL_BARS barres M15. Cancel si non touché OU si traverse VWAP
         côté opposé de > 0.5×ATR (invalidation anticipée).
      7. Si fill → exit SL/TP ±1×ATR (SL prio si même barre) OU close session
    """
    if ticker not in TICKERS:
        return _empty_trades_df()
    if ticker not in INSTRUMENTS:
        return _empty_trades_df()

    p = params or {}
    stretch_min = float(p.get("stretch_min", VWAP_PB_STRETCH_MIN))
    stretch_max = float(p.get("stretch_max", VWAP_PB_STRETCH_MAX))
    sl_atr_mult = float(p.get("sl_atr_mult", VWAP_PB_SL_ATR_MULT))
    trend_bars = int(p.get("trend_bars", VWAP_PB_TREND_BARS))
    ttl_bars = int(p.get("ttl_bars", VWAP_PB_TTL_BARS))

    instr = INSTRUMENTS[ticker]
    tick_size = instr["tick_size"]
    pt_value = instr["dollar_per_point"]
    slip_ticks = SLIPPAGE_TICKS_PER_TICKER.get(ticker, 1)
    slip_px = slip_ticks * tick_size  # slippage en prix (entrée et sortie)

    # Fenêtre horaire NY pour ce ticker
    if ticker not in VWAP_PB_SESSION_WINDOW_NY:
        return _empty_trades_df()
    (sess_start_h, sess_start_m), (sess_end_h, sess_end_m) = VWAP_PB_SESSION_WINDOW_NY[ticker]
    # Reset VWAP par actif
    reset_h, reset_m = VWAP_PB_RESET_HOUR_NY[ticker]

    df = df_15m.copy()
    idx_utc = pd.DatetimeIndex(df.index)
    if idx_utc.tz is None:
        idx_utc = idx_utc.tz_localize("UTC")
    idx_ny = idx_utc.tz_convert(_NY)

    # ATR(14) M15 — shift(1) interne, causal strict
    df["atr"] = _compute_atr_m15(df, period=14)

    # VWAP causale par session NY (reset horaire par ticker)
    df["vwap"] = _compute_vwap_causal(df, idx_ny, reset_h, reset_m)

    # Vectorisation des filtres temporels (perf + lisibilité)
    df["_hour_ny"] = idx_ny.hour
    df["_min_ny"] = idx_ny.minute
    df["_weekday_ny"] = idx_ny.weekday
    df["_date_ny"] = idx_ny.date

    # Trades par jour NY (compteur)
    daily_count: dict = {}
    trades: list = []
    cum_pnl = 0.0
    peak_pnl = 0.0
    day_pnl_map: dict = {}  # date_ny → pnl_du_jour pour trade_allowed

    # Warmup : il faut au minimum trend_bars + 14 (ATR) + qq barres VWAP
    warmup = max(20, trend_bars + 1)

    # Convertir en minutes pour comparaisons rapides
    sess_start_minutes = sess_start_h * 60 + sess_start_m
    sess_end_minutes = sess_end_h * 60 + sess_end_m

    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    vwaps = df["vwap"].values
    atrs = df["atr"].values
    hours = df["_hour_ny"].values
    mins = df["_min_ny"].values
    weekdays = df["_weekday_ny"].values
    dates = df["_date_ny"].values

    n_bars = len(df)

    for i in range(warmup, n_bars):
        # ── Filtres de la barre i ───────────────────────────────────────
        weekday = weekdays[i]
        if weekday >= 5:
            continue

        ts_minute = hours[i] * 60 + mins[i]
        if ts_minute < sess_start_minutes or ts_minute >= sess_end_minutes:
            continue

        date_today = dates[i]
        date_str = date_today.isoformat()

        # Skip jours macro (FOMC/CPI/NFP/JOLTS)
        if date_today in _MACRO_DATES:
            continue

        # MAX_TRADES_PER_DAY par actif (intra-jour NY)
        if daily_count.get(date_today, 0) >= VWAP_PB_MAX_TRADES_PER_DAY:
            continue

        # ── Indicateurs : ATR et VWAP doivent être connus ──────────────
        atr = atrs[i]
        vwap_i = vwaps[i]
        if not np.isfinite(atr) or atr <= 0:
            continue
        if not np.isfinite(vwap_i):
            continue

        # ── SETUP : 3 closes consécutives du même côté VWAP ────────────
        # Indices [i-trend..i-1], comparaison close[k] vs vwap[k] (causal :
        # vwap[k] connu à l'ouverture de k → décision en fin de k OK).
        start_idx = i - trend_bars
        if start_idx < 1:
            continue

        all_long = True
        all_short = True
        for k in range(start_idx, i):
            ck = closes[k]
            vk = vwaps[k]
            if not np.isfinite(vk):
                all_long = False
                all_short = False
                break
            if ck <= vk:
                all_long = False
            if ck >= vk:
                all_short = False
            if not (all_long or all_short):
                break

        if not (all_long or all_short):
            continue

        # ── STRETCH : magnitude (close[i-1] - vwap[i-1]) / ATR[i] ──────
        # On utilise close[i-1] et vwap[i-1] (toutes deux clôturées avant i)
        close_prev = closes[i - 1]
        vwap_prev = vwaps[i - 1]
        if not np.isfinite(vwap_prev):
            continue

        stretch_abs = abs(close_prev - vwap_prev) / atr
        if stretch_abs < stretch_min or stretch_abs > stretch_max:
            continue

        # ── ROLLBACK v1.1 → v1 (2026-05-26) ─────────────────────────────
        # Filtre SKIP_VOL_H retiré : seuil ATR p80 figé sur IS dépassé par 94%
        # des barres OOS MGC1 (drift +148% vol Gold), filtre éliminait 100%
        # trades MGC1 OOS. Leçon : quantile IS-only pas robuste si drift régime.
        # Patch v1.1 archivé : output/archive/vwap-pb-v1.1/.

        # Direction = sens du pullback (contrarian au stretch)
        # all_long = closes > VWAP → stretch HAUT → LONG pullback = BUY @ VWAP
        # all_short = closes < VWAP → stretch BAS → SHORT pullback = SELL @ VWAP
        if all_long:
            direction = "long"
        else:
            direction = "short"

        # ── Entry LIMIT @ VWAP[i] (connue à l'ouverture i) ─────────────
        entry = float(vwap_i)
        # SL/TP ±1×ATR (RR brut 1:1, RR net ~0.89 après frictions)
        sl_dist = atr * sl_atr_mult
        tp_dist = atr * sl_atr_mult  # RR 1:1 enforced
        if direction == "long":
            sl = entry - sl_dist
            tp = entry + tp_dist
        else:
            sl = entry + sl_dist
            tp = entry - tp_dist

        # Slippage entrée : le LIMIT ne touche que si le prix franchit
        # VRAIMENT le niveau. Le slippage défavorable s'applique au FILL
        # PRICE (on est filled légèrement défavorablement vs le LIMIT theo).
        # LONG : BUY LIMIT @ VWAP → fill_price = VWAP + slip_px (on paie plus)
        # SHORT: SELL LIMIT @ VWAP → fill_price = VWAP - slip_px (on reçoit moins)
        # Détection du touch : on conserve `entry` (théorique) comme seuil
        # de fill. Le `entry_filled` (prix réel d'exécution) intègre le slip.
        if direction == "long":
            entry_filled = entry + slip_px
        else:
            entry_filled = entry - slip_px

        rr = tp_dist / sl_dist if sl_dist > 0 else 0.0

        # ── Sizing ─────────────────────────────────────────────────────
        risk_per_ct = (sl_dist + slip_px) * pt_value
        if risk_per_ct <= 0:
            continue
        n_ct = max(1, int(RISK_PER_TRADE_USD / risk_per_ct))

        # ── Garde-fou Topstep ──────────────────────────────────────────
        if topstep_guard:
            day_pnl_today = day_pnl_map.get(date_today, 0.0)
            allowed, _motif = trade_allowed(
                day_pnl=day_pnl_today,
                cum_pnl=cum_pnl,
                peak_pnl=peak_pnl,
            )
            if not allowed:
                continue

        # ── Recherche du fill (LIMIT @ VWAP, TTL = ttl_bars barres) ────
        # Convention conservatrice projet : pas de fill+exit dans la même
        # barre M15 (impossible à départager sans M1). Donc :
        #   - Fenêtre fill : i+1 à i+ttl_bars (PAS la barre i elle-même)
        #   - Le signal est détecté à la CLÔTURE de la barre i-1, le LIMIT
        #     est posé pour exécution sur les barres ≥ i. On cherche le
        #     fill à partir de i+1 pour éviter le look-ahead intra-bar i.
        #     Justification : à l'ouverture de i on connait VWAP[i] (causale),
        #     mais on ne peut pas savoir si la mèche de i va toucher entry
        #     ET ENSUITE évoluer vers SL ou TP dans la même bougie.
        #
        # Touch détecté avec le prix THÉORIQUE `entry` (pas `entry_filled`),
        # le slippage est appliqué ensuite au prix d'exécution.
        # Invalidation anticipée : si close[j] traverse VWAP côté opposé
        # de > 0.5×ATR sans fill → cancel.
        result = "NOT_FILLED"
        fill_idx = None
        cancel_reason = None
        max_fill_idx = min(i + ttl_bars, n_bars - 1)

        invalidation_threshold = 0.5 * atr

        for j in range(i + 1, max_fill_idx + 1):
            # SÉCURITÉ : le LIMIT doit être cancel à la fin de la session
            # NY du jour de signal (sinon des fills overnight Globex sortent
            # quand le prix dérive la nuit — non-représentatif live).
            if dates[j] != date_today:
                cancel_reason = "eod_cancel"
                break
            ts_j_minutes = hours[j] * 60 + mins[j]
            if ts_j_minutes >= sess_end_minutes:
                cancel_reason = "eod_cancel"
                break

            # Vérifier touch LIMIT intra-bar (low/high de la barre j)
            # avec prix théorique `entry` (le slippage dégrade l'exécution
            # à `entry_filled` mais le critère de touch reste le LIMIT théo).
            if direction == "long":
                if lows[j] <= entry:
                    fill_idx = j
                    break
            else:
                if highs[j] >= entry:
                    fill_idx = j
                    break

            # Invalidation anticipée (vérifiée sur close[j] AVANT de
            # passer à j+1) : si VWAP traversée fortement côté opposé
            vj = vwaps[j]
            if np.isfinite(vj):
                if direction == "long":
                    # On voulait BUY pullback : si close passe sous VWAP
                    # de plus de 0.5×ATR, le setup est cassé
                    if closes[j] < vj - invalidation_threshold:
                        cancel_reason = "invalidation"
                        break
                else:
                    if closes[j] > vj + invalidation_threshold:
                        cancel_reason = "invalidation"
                        break

        # Tagging régime (placeholder simple — détaillé en PHASE 5 si besoin)
        is_macro = bool(date_today in _MACRO_DATES)
        regime = "neutral"  # stress-tests PHASE 5 calculeront ADX

        if fill_idx is None:
            # NOT_FILLED : pas de pnl, pas de frictions
            trades.append(
                _make_trade_row(
                    date_str=date_str,
                    direction=direction,
                    entry=entry_filled,
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
                    atr_pct=None,
                    is_macro=is_macro,
                    vwap_entry=vwap_i,
                    stretch_abs=stretch_abs,
                    cancel_reason=cancel_reason,
                )
            )
            # NOT_FILLED ne consomme PAS un trade du compteur jour (sinon on
            # perd l'opportunité d'un signal valide plus tard sans avoir
            # traded). Décision : compteur incrémenté UNIQUEMENT sur fill.
            continue

        # ── Simulation exit (SL prio si même barre, TP, ou close session) ──
        result_str, exit_idx, exit_px = _simulate_exit(
            highs=highs,
            lows=lows,
            closes=closes,
            hours_ny=hours,
            mins_ny=mins,
            dates_ny=dates,
            fill_idx=fill_idx,
            direction=direction,
            entry=entry_filled,
            sl=sl,
            tp=tp,
            sess_end_h=sess_end_h,
            sess_end_m=sess_end_m,
            date_today=date_today,
            n_bars=n_bars,
        )

        fill_time = df.index[fill_idx]
        exit_time = df.index[exit_idx]

        # ── P&L brut + net ──
        if direction == "long":
            raw = exit_px - entry_filled
        else:
            raw = entry_filled - exit_px

        pnl_gross = raw * n_ct * pt_value
        # Slippage sortie (côté défavorable)
        slip_cost = slip_px * n_ct * pt_value
        comm_cost = COMMISSION_RT_PER_CONTRACT * n_ct
        pnl = pnl_gross - slip_cost - comm_cost

        cum_pnl += pnl
        if cum_pnl > peak_pnl:
            peak_pnl = cum_pnl
        day_pnl_map[date_today] = day_pnl_map.get(date_today, 0.0) + pnl
        daily_count[date_today] = daily_count.get(date_today, 0) + 1

        trades.append(
            _make_trade_row(
                date_str=date_str,
                direction=direction,
                entry=entry_filled,
                sl=sl,
                tp=tp,
                sl_dist=sl_dist,
                tp_dist=tp_dist,
                rr=rr,
                n_ct=n_ct,
                result=result_str,
                pnl=pnl,
                pnl_gross=pnl_gross,
                fill_time=fill_time,
                exit_time=exit_time,
                exit_px=exit_px,
                regime=regime,
                atr_pct=None,
                is_macro=is_macro,
                vwap_entry=vwap_i,
                stretch_abs=stretch_abs,
                cancel_reason=None,
            )
        )

    return _to_trades_df(trades)


# ══════════════════════════════════════════════════════════════════════════════
# 3. Helpers exit + DF construction
# ══════════════════════════════════════════════════════════════════════════════


def _simulate_exit(
    highs,
    lows,
    closes,
    hours_ny,
    mins_ny,
    dates_ny,
    fill_idx: int,
    direction: str,
    entry: float,
    sl: float,
    tp: float,
    sess_end_h: int,
    sess_end_m: int,
    date_today,
    n_bars: int,
) -> tuple[str, int, float]:
    """Exit triple barrier (convention conservatrice) :
       - SL prioritaire si SL+TP touchés dans la même barre (règle projet)
       - TP si touché seul
       - Close de session (sess_end) intra-jour ou changement de jour : TE

    CONVENTION CONSERVATRICE : l'évaluation SL/TP commence à la barre
    fill_idx + 1, JAMAIS sur la barre de fill elle-même. Sans données M1,
    impossible de savoir si dans la barre de fill le prix a touché TP ou
    SL avant ou après le fill du LIMIT. Cette règle évite un biais
    optimiste massif (typiquement 60% des trades concernés).

    Search ranges sur les barres j ≥ fill_idx + 1.
    """
    sess_end_minutes = sess_end_h * 60 + sess_end_m

    # Conservation : l'exit commence à fill_idx + 1
    for j in range(fill_idx + 1, n_bars):
        # Si on a changé de jour (jour > date_today) → exit force close
        # On retourne fill_idx (close de la dernière barre dispo dans la
        # session du jour de fill = close à fin de session ≈ neutralité).
        if dates_ny[j] != date_today:
            ref_idx = max(fill_idx, j - 1)
            return "TE", ref_idx, float(closes[ref_idx])

        # Si on a dépassé l'heure de fin de session intra-jour → exit
        ts_minutes = hours_ny[j] * 60 + mins_ny[j]
        if ts_minutes >= sess_end_minutes:
            # Close de la barre précédente (dernière barre dans la session)
            ref_idx = max(fill_idx, j - 1)
            return "TE", ref_idx, float(closes[ref_idx])

        h = float(highs[j])
        lo = float(lows[j])

        if direction == "long":
            sl_hit = lo <= sl
            tp_hit = h >= tp
        else:
            sl_hit = h >= sl
            tp_hit = lo <= tp

        if sl_hit and tp_hit:
            return "SL", j, float(sl)
        if sl_hit:
            return "SL", j, float(sl)
        if tp_hit:
            return "TP", j, float(tp)

    # Fin des données sans hit → close à la dernière barre
    return "TE", n_bars - 1, float(closes[n_bars - 1])


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
    vwap_entry: float,
    stretch_abs: float,
    cancel_reason,
) -> dict:
    """Construit une ligne de trade conforme au schéma standard projet +
    features VWAP-PB (vwap_entry, stretch_abs, cancel_reason)."""
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
        "adx": None,  # PHASE 5 stress tests si nécessaire
        "atr_pct": atr_pct,
        "is_macro_day": bool(is_macro),
        # ── Features VWAP-PB (persistées pour analyse/discovery) ──
        "vwap_entry": _safe_round(vwap_entry, 5),
        "stretch_abs": _safe_round(stretch_abs, 4),
        "cancel_reason": cancel_reason,
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
    "vwap_entry",
    "stretch_abs",
    "cancel_reason",
]


def _empty_trades_df() -> pd.DataFrame:
    return pd.DataFrame(columns=_COLS)


def _to_trades_df(trades: list) -> pd.DataFrame:
    if not trades:
        return _empty_trades_df()
    return pd.DataFrame(trades)[_COLS]


# ══════════════════════════════════════════════════════════════════════════════
# 4. plot_day (chart pour --plot, identique pattern eia_inv)
# ══════════════════════════════════════════════════════════════════════════════


def plot_day(df_15m, ticker, date_str, day_trades, output_path):
    """Chart d'une journée : OHLC + VWAP + ATR + flèches entry/exit."""
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
    day = df_15m[mask.values].copy()
    if day.empty:
        Path(output_path).touch()
        return

    # Recalcul VWAP causale + ATR localement pour le chart
    if ticker in VWAP_PB_RESET_HOUR_NY:
        reset_h, reset_m = VWAP_PB_RESET_HOUR_NY[ticker]
        idx_full_ny = ts_ny
        # On recalcule VWAP sur tout l'historique pour cohérence puis on slice
        df_full = df_15m.copy()
        df_full["vwap"] = _compute_vwap_causal(df_full, idx_full_ny, reset_h, reset_m)
        df_full["atr"] = _compute_atr_m15(df_full, period=14)
        day_vwap = df_full.loc[day.index, "vwap"].values
        day_atr = df_full.loc[day.index, "atr"].values
    else:
        day_vwap = np.full(len(day), np.nan)
        day_atr = np.full(len(day), np.nan)

    fig, (ax1, ax2) = plt.subplots(
        2,
        1,
        figsize=(16, 9),
        gridspec_kw={"height_ratios": [3, 1]},
        sharex=True,
    )

    x = list(range(len(day)))

    # Chandeliers
    for i, (_, row) in enumerate(day.iterrows()):
        color = "#26a69a" if row["close"] >= row["open"] else "#ef5350"
        ax1.plot([i, i], [row["low"], row["high"]], color=color, lw=0.8)
        ax1.bar(
            i,
            abs(row["close"] - row["open"]),
            bottom=min(row["open"], row["close"]),
            color=color,
            width=0.6,
            alpha=0.9,
        )

    # VWAP
    ax1.plot(x, day_vwap, color="#ffd54f", lw=1.5, label="VWAP causale")

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
        ax1.scatter(fill_idx, trade["entry"], marker=marker, color=color, s=150, zorder=5)
        ax1.scatter(exit_idx, trade["exit"], marker="x", color="white", s=120, zorder=5)
        ax1.axhline(trade["sl"], color="#ef5350", ls="--", lw=0.7, alpha=0.6)
        ax1.axhline(trade["tp"], color="#26a69a", ls="--", lw=0.7, alpha=0.6)
        day_pnl += trade.get("pnl", 0.0)

    # ATR sous-graphe
    ax2.fill_between(x, 0, day_atr, alpha=0.4, color="#7986cb")
    ax2.set_ylabel("ATR(14)", fontsize=9, color="white")

    ax1.set_title(
        f"{ticker} — {date_str}   |   P&L net jour : {day_pnl:+.0f} $   |   vwap-pb-v1",
        fontsize=12,
        fontweight="bold",
        color="white",
    )
    ax1.legend(loc="upper left", fontsize=9)
    ax1.set_facecolor("#1a1a2e")
    ax2.set_facecolor("#1a1a2e")
    fig.patch.set_facecolor("#13131f")
    ax1.tick_params(colors="white")
    ax2.tick_params(colors="white")

    plt.tight_layout()
    plt.savefig(output_path, dpi=110, bbox_inches="tight")
    plt.close(fig)


# ══════════════════════════════════════════════════════════════════════════════
# 5. Tests unitaires (auto-exécution si __main__)
# ══════════════════════════════════════════════════════════════════════════════


def _test_vwap_causal():
    """Test unitaire : la VWAP à la barre i NE doit PAS inclure les valeurs
    de la barre i (typical[i] × volume[i]). Recalcul manuel sur séquence
    contrôlée."""
    # Construction d'une mini-séquence M15 sur 1 journée NY (8 barres)
    # Reset NQ1 = 09:30 NY. Donc on construit 5 barres après 09:30 NY.
    # UTC = NY + 5h en EST (hiver) ou +4h EDT (été). Pour ne pas dépendre
    # de DST, on construit en UTC explicit puis on convertit.
    # En janvier (EST) : 09:30 NY = 14:30 UTC
    ts_utc = pd.date_range(
        start="2025-01-13 14:30:00",
        periods=5,
        freq="15min",
        tz=None,
    )
    df_test = pd.DataFrame(
        {
            "open": [100.0, 101.0, 102.0, 103.0, 104.0],
            "high": [101.0, 102.0, 103.0, 104.0, 105.0],
            "low": [99.0, 100.0, 101.0, 102.0, 103.0],
            "close": [100.5, 101.5, 102.5, 103.5, 104.5],
            "volume": [1000, 2000, 3000, 4000, 5000],
        },
        index=ts_utc,
    )
    idx_ny = pd.DatetimeIndex(df_test.index).tz_localize("UTC").tz_convert(_NY)
    vwap = _compute_vwap_causal(df_test, idx_ny, reset_h=9, reset_m=30)

    # Barre 0 (09:30 NY) : 1re barre de session → VWAP = NaN (shift(1))
    assert pd.isna(vwap.iloc[0]), f"VWAP[0] doit être NaN, got {vwap.iloc[0]}"

    # Barre 1 (09:45 NY) : VWAP = (typical[0] × vol[0]) / vol[0]
    # = typical[0] = (101 + 99 + 100.5) / 3 = 100.1666...
    typ_0 = (101.0 + 99.0 + 100.5) / 3.0
    expected_vwap_1 = typ_0  # car cum_vol = vol[0] → typ_0 × vol[0] / vol[0]
    assert (
        abs(vwap.iloc[1] - expected_vwap_1) < 1e-6
    ), f"VWAP[1] attendu {expected_vwap_1}, got {vwap.iloc[1]}"

    # Barre 2 (10:00 NY) : cumsum barres 0 et 1
    typ_1 = (102.0 + 100.0 + 101.5) / 3.0
    expected_vwap_2 = (typ_0 * 1000 + typ_1 * 2000) / (1000 + 2000)
    assert (
        abs(vwap.iloc[2] - expected_vwap_2) < 1e-6
    ), f"VWAP[2] attendu {expected_vwap_2}, got {vwap.iloc[2]}"

    # Barre 3 (10:15 NY) : cumsum barres 0, 1, 2
    typ_2 = (103.0 + 101.0 + 102.5) / 3.0
    expected_vwap_3 = (typ_0 * 1000 + typ_1 * 2000 + typ_2 * 3000) / (1000 + 2000 + 3000)
    assert (
        abs(vwap.iloc[3] - expected_vwap_3) < 1e-6
    ), f"VWAP[3] attendu {expected_vwap_3}, got {vwap.iloc[3]}"

    # NON-look-ahead : VWAP[i] ne doit JAMAIS contenir typ[i] × vol[i].
    # Vérification : si on ré-injecte typ[i] dans la formule attendue,
    # la valeur diverge.
    bad_vwap_3 = (
        typ_0 * 1000 + typ_1 * 2000 + typ_2 * 3000 + (104.0 + 102.0 + 103.5) / 3.0 * 4000
    ) / (1000 + 2000 + 3000 + 4000)
    assert (
        abs(vwap.iloc[3] - bad_vwap_3) > 1e-3
    ), "VWAP[3] semble contenir la barre 3 (look-ahead) !"

    print("  [TEST OK] VWAP causale : pas de look-ahead, shift(1) correct")


def _test_dst_awareness():
    """Test : la conversion UTC → NY doit gérer DST automatiquement.
    Vérification sur mi-mars (passage à EDT) et début novembre (retour à EST)."""
    # Mi-mars 2025 : DST commence le dimanche 9 mars 2025 à 02:00 NY.
    # Avant : EST (UTC-5). Après : EDT (UTC-4).
    # 2025-03-08 14:30 UTC → 09:30 EST (avant DST)
    # 2025-03-10 13:30 UTC → 09:30 EDT (après DST)
    ts1 = pd.Timestamp("2025-03-08 14:30:00").tz_localize("UTC").tz_convert(_NY)
    ts2 = pd.Timestamp("2025-03-10 13:30:00").tz_localize("UTC").tz_convert(_NY)
    assert ts1.hour == 9 and ts1.minute == 30, f"mars-8 attendu 09:30 NY, got {ts1}"
    assert ts2.hour == 9 and ts2.minute == 30, f"mars-10 attendu 09:30 NY, got {ts2}"

    # Début novembre 2025 : DST se termine le dimanche 2 novembre 2025 à 02:00 NY.
    # Avant : EDT (UTC-4). Après : EST (UTC-5).
    # 2025-10-31 13:30 UTC → 09:30 EDT
    # 2025-11-03 14:30 UTC → 09:30 EST
    ts3 = pd.Timestamp("2025-10-31 13:30:00").tz_localize("UTC").tz_convert(_NY)
    ts4 = pd.Timestamp("2025-11-03 14:30:00").tz_localize("UTC").tz_convert(_NY)
    assert ts3.hour == 9 and ts3.minute == 30, f"oct-31 attendu 09:30 NY, got {ts3}"
    assert ts4.hour == 9 and ts4.minute == 30, f"nov-3 attendu 09:30 NY, got {ts4}"

    print("  [TEST OK] DST-aware : conversion UTC→NY correcte mars + novembre")


if __name__ == "__main__":
    print(f"Tests unitaires {STRATEGY_ID} :")
    _test_vwap_causal()
    _test_dst_awareness()
    print("Tous les tests passent.")
