"""
Stratégie ARF — Asia Range Failure (arf-v1).

Concept :
    Pendant la session asiatique (19h NY veille → 02h NY courant), un range
    se forme (Asia_High / Asia_Low). Pendant la session londonienne
    (02h-05h NY), on attend une fausse cassure :
      - Le prix franchit Asia_High + buffer puis revient sous Asia_High
        → ordre LIMIT SHORT @ Asia_High (échec de cassure haussière).
      - Symétrique : franchissement Asia_Low - buffer puis remontée au-dessus
        → ordre LIMIT LONG @ Asia_Low.
    SL au-delà de l'extrémité du range (Asia_Low - buffer pour long, Asia_High
    + buffer pour short), TP au multiple R:R configurable.

Edge :
    Les stops mécaniques retail (breakout traders, momentum bots) sont placés
    juste au-delà des extrêmes asiatiques. La session asiatique étant peu
    liquide, les cassures manquent de suivi institutionnel → liquidation
    forcée des breakout traders → retour dans le range = position contrarian.
    Qui paie : retail FOMO + algos momentum naïfs déclenchés par la cassure.

Falsification :
    - PF OOS < 1.0 sur 60 trades consécutifs en live, OU
    - DD > 30 % supérieur au DD backtest sur fenêtre 30 jours, OU
    - Taux de "fake breakout puis retour" < 35 % sur 100 setups consécutifs.

Indicateurs :
    - Asia range (low/high) calculé sur 19h-02h NY (sans look-ahead).
    - ATR(14) M15 — calibrage buffers et filtres min/max range.
    - ADX(14) — tagging régime (stress tests PHASE 5 uniquement).

Fenêtre NY :
    - Asie  : 19h (veille) → 02h (courant)
    - Londres : 02h → 05h (déclenchement)

Architecture : RECHERCHE pure. Aucun import broker/. Pas de modification de
core/opr.py / core/strategy_fib.py.
"""

from __future__ import annotations

from typing import Optional, Tuple, List, Dict
from zoneinfo import ZoneInfo

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from config import (
    INSTRUMENTS,
    RISK_PER_TRADE_USD,
    SLIPPAGE_TICKS_PER_TICKER,
    COMMISSION_RT_PER_CONTRACT,
    MACRO_EVENT_DATES,
    # ARF params
    ARF_STRATEGY_VERSION,
    ARF_TICKERS,
    ARF_ASIA_HOUR_START_NY, ARF_ASIA_HOUR_END_NY,
    ARF_LONDON_HOUR_START_NY, ARF_LONDON_HOUR_END_NY,
    ARF_SL_BUFFER_ATR_PER_TICKER, ARF_ENTRY_BUFFER_ATR_PER_TICKER,
    ARF_TP_RR_PER_TICKER,
    ARF_MIN_RANGE_ATR_PER_TICKER, ARF_MAX_RANGE_ATR_PER_TICKER,
    ARF_ATR_PERIOD,
    ARF_ORDER_TIMEOUT_BARS, ARF_MAX_HOLD_BARS,
    ARF_MAX_TRADES_PER_DAY,
    ARF_SL_BUFFER_TICKS_FLOOR,
    ARF_EXCLUDE_MACRO_DAYS,
    ARF_ADX_MAX,
    ARF_SKIP_DOUBLE_BREAKOUT,
    ARF_RETURN_CONFIRM_ATR,
    ARF_ENTRY_MODE,
    ARF_TP_RANGE_PCT,
    ARF_ATR_PCT_MIN,
    ARF_ATR_PCT_MAX,
)

# Reproductibilité
np.random.seed(42)

# ── Identité de la stratégie ─────────────────────────────────────────────────
STRATEGY_ID   = ARF_STRATEGY_VERSION              # "arf-v1"
TICKERS       = ARF_TICKERS                       # ["MES1", "NQ1", "YM1"]
CSV_SUFFIX    = "_arf"
CSV_TIMEFRAME = "m15"

_NY = ZoneInfo("America/New_York")

# ── Grille d'optimisation (4 dimensions, 4*3*4*3 = 144 combos) ───────────────
# Bornes ancrées dans la logique de marché :
#  - sl_buffer_atr : 0.10 (très serré) à 0.50 (large) — au-delà = SL absurde
#  - entry_buffer_atr : 0.05 (réactif) à 0.20 (conservateur)
#  - tp_rr : 1.5 (target rapide) à 3.0 (target lointain) — au-delà = TE majoritaire
#  - min_range_atr : 0.8 (range étroit OK) à 1.6 (range large requis)
PARAM_GRID = {
    "sl_buffer_atr":    [0.10, 0.20, 0.30, 0.50],
    "entry_buffer_atr": [0.05, 0.10, 0.20],
    "tp_rr":            [1.5, 2.0, 2.5, 3.0],
    "min_range_atr":    [0.8, 1.2, 1.6],
}


# ══════════════════════════════════════════════════════════════════════════════
# Helpers indicateurs
# ══════════════════════════════════════════════════════════════════════════════

def _compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """ATR sans look-ahead (EMA du True Range)."""
    high, low, close = df["high"], df["low"], df["close"]
    hl = high - low
    hc = (high - close.shift(1)).abs()
    lc = (low - close.shift(1)).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def _compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """ADX pour classification trending/ranging (stress tests PHASE 5)."""
    high, low, close = df["high"], df["low"], df["close"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(span=period, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(span=period, adjust=False).mean() / atr.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(span=period, adjust=False).mean() / atr.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(span=period, adjust=False).mean()


# ══════════════════════════════════════════════════════════════════════════════
# Construction du range asiatique (strictement sans look-ahead)
# ══════════════════════════════════════════════════════════════════════════════

def _is_in_asia_window(ts_ny: pd.Timestamp) -> bool:
    """
    Vrai si l'horaire NY est dans la fenêtre asiatique.
    La fenêtre couvre 19h NY (veille) → 02h NY (courant), à cheval sur minuit.
    """
    hr = ts_ny.hour
    # 19h-23h59 OU 00h-02h
    return (hr >= ARF_ASIA_HOUR_START_NY) or (hr < ARF_ASIA_HOUR_END_NY)


def _is_in_london_window(ts_ny: pd.Timestamp) -> bool:
    """Vrai si l'horaire NY est dans la fenêtre Londres (02h-05h NY)."""
    return ARF_LONDON_HOUR_START_NY <= ts_ny.hour < ARF_LONDON_HOUR_END_NY


def _asia_session_date(ts_ny: pd.Timestamp) -> str:
    """
    Date de la session asiatique à laquelle appartient une barre.
    Convention : une session asiatique X est celle qui se termine le matin
    du jour X (à 02h NY). Donc une barre à 22h NY le 2025-10-01 appartient
    à la session qui se termine 2025-10-02 02h NY → date = '2025-10-02'.
    """
    hr = ts_ny.hour
    if hr >= ARF_ASIA_HOUR_START_NY:
        # Soirée veille → la session est étiquetée au jour suivant
        return (ts_ny.normalize() + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    # Sinon (00h-18h59), on est déjà dans le jour courant
    return ts_ny.strftime("%Y-%m-%d")


# ══════════════════════════════════════════════════════════════════════════════
# Backtest principal
# ══════════════════════════════════════════════════════════════════════════════

def run_backtest(
    df_15m: pd.DataFrame,
    ticker: str,
    tf=None,
    params: Optional[dict] = None,
    topstep_guard: bool = True,
) -> pd.DataFrame:
    """
    Exécute le backtest ARF sur df_15m pour un ticker.

    Schéma de colonnes (compatibilité core/optimizer.py) :
        date, dir, entry, sl, tp, sl_dist, tp_dist, rr, n_ct,
        result (TP|SL|TE|NOT_FILLED), pnl, fill_time, exit_time, exit, regime
    + colonnes optionnelles : pnl_gross, adx, atr_pct, is_macro_day,
      asia_high, asia_low, asia_range, ticker, strategy

    `pnl` = P&L NET (slippage entrée+sortie + commissions inclus).
    """
    if ticker not in TICKERS:
        return pd.DataFrame()

    p = params or {}
    sl_buffer_atr    = p.get("sl_buffer_atr",    ARF_SL_BUFFER_ATR_PER_TICKER.get(ticker, 0.20))
    entry_buffer_atr = p.get("entry_buffer_atr", ARF_ENTRY_BUFFER_ATR_PER_TICKER.get(ticker, 0.10))
    tp_rr            = p.get("tp_rr",            ARF_TP_RR_PER_TICKER.get(ticker, 2.0))
    min_range_atr    = p.get("min_range_atr",    ARF_MIN_RANGE_ATR_PER_TICKER.get(ticker, 1.0))
    max_range_atr    = p.get("max_range_atr",    ARF_MAX_RANGE_ATR_PER_TICKER.get(ticker, 4.0))

    instr     = INSTRUMENTS[ticker]
    tick_size = instr["tick_size"]
    pt_value  = instr["dollar_per_point"]
    slip_ticks = SLIPPAGE_TICKS_PER_TICKER.get(ticker, 1)
    slip_px   = slip_ticks * tick_size

    # ── Indicateurs (strictement sans look-ahead) ──
    df = df_15m.copy()
    df["atr"]     = _compute_atr(df, ARF_ATR_PERIOD)
    df["adx"]     = _compute_adx(df, 14)
    df["atr_pct"] = df["atr"].rolling(window=20 * 26, min_periods=50).rank(pct=True)

    # ── Conversion index → NY (DST-aware) ──
    if df.index.tz is None:
        idx_ny_all = df.index.tz_localize("UTC").tz_convert(_NY)
    else:
        idx_ny_all = df.index.tz_convert(_NY)

    # ── Pré-calcul du range asiatique par session ──
    # Pour chaque barre dans la fenêtre asiatique, on accumule low/high de
    # cette session. À la fin de la fenêtre (02h NY), on a Asia_Low/Asia_High
    # de la session qui vient de se terminer.
    asia_ranges: Dict[str, Dict[str, float]] = {}
    # Première passe : agréger low/high par session_date sur les barres asia
    n = len(df)
    for i in range(n):
        ts_ny = idx_ny_all[i]
        if not _is_in_asia_window(ts_ny):
            continue
        sess = _asia_session_date(ts_ny)
        bar = df.iloc[i]
        low_b, high_b = float(bar["low"]), float(bar["high"])
        if sess not in asia_ranges:
            asia_ranges[sess] = {
                "low": low_b,
                "high": high_b,
                "last_ts": ts_ny,
                "n_bars": 1,
            }
        else:
            r = asia_ranges[sess]
            r["low"]     = min(r["low"], low_b)
            r["high"]    = max(r["high"], high_b)
            r["last_ts"] = ts_ny
            r["n_bars"] += 1

    # ── Boucle principale : déclenchement sur fenêtre Londres ──
    trades: List[Dict] = []
    daily_trades: Dict[str, int] = {}
    # État par session : a-t-on déjà détecté un fake breakout ?
    session_state: Dict[str, Dict] = {}

    warmup = max(ARF_ATR_PERIOD * 4, 50)  # warmup ATR

    for i in range(warmup, n):
        ts_ny = idx_ny_all[i]

        # Filtre weekend
        if ts_ny.weekday() >= 5:
            continue

        # On déclenche uniquement dans la fenêtre Londres
        if not _is_in_london_window(ts_ny):
            continue

        date_str = ts_ny.strftime("%Y-%m-%d")
        sess = date_str  # par convention, une session Londres lundi appartient
                         # à la session asiatique étiquetée lundi (terminée à 02h lundi NY)

        # Exclusion jours macro US
        if ARF_EXCLUDE_MACRO_DAYS and date_str in MACRO_EVENT_DATES:
            continue

        # Limite trades/jour
        if daily_trades.get(date_str, 0) >= ARF_MAX_TRADES_PER_DAY:
            continue

        # Récupère le range asiatique de la session
        ar = asia_ranges.get(sess)
        if ar is None:
            continue
        # Filtre minimum de bougies asia (sinon range pas représentatif)
        if ar["n_bars"] < 8:  # ~2h de session asia mini
            continue

        asia_low, asia_high = ar["low"], ar["high"]
        asia_range = asia_high - asia_low
        if asia_range <= 0:
            continue

        bar = df.iloc[i]
        prev = df.iloc[i - 1]

        # Valeur ATR à la barre courante (sans look-ahead)
        atr_val = float(bar["atr"]) if pd.notna(bar["atr"]) else 0.0
        if atr_val <= 0:
            continue

        # ── Filtres de range ──
        # Range mini : sinon les niveaux sont trop proches du prix actuel
        if asia_range < min_range_atr * atr_val:
            continue
        # Range maxi : range énorme = news overnight, R:R défavorable
        if asia_range > max_range_atr * atr_val:
            continue

        # ── État de session : a-t-on vu un fake breakout ? ──
        # On parcourt les barres Londres déjà passées de cette session pour
        # détecter le franchissement puis le retour.
        # Approche : on garde un état par session indiquant
        #   - "broke_high" si une barre a touché >= asia_high + entry_buffer
        #   - "broke_low"  si une barre a touché <= asia_low  - entry_buffer
        # Puis on cherche le RETOUR sous/au-dessus du niveau.
        st = session_state.setdefault(sess, {
            "broke_high": False,
            "broke_low":  False,
            "high_break_ts": None,
            "low_break_ts":  None,
        })

        entry_buf = entry_buffer_atr * atr_val

        # Met à jour l'état avec la barre courante (la barre courante peut
        # marquer le breakout ; la décision d'entrée se fait UNIQUEMENT sur
        # le retour observé via prev/passé, pas sur bar elle-même)
        bar_high, bar_low = float(bar["high"]), float(bar["low"])

        # Met à jour les breakouts détectés sur les barres prev (déjà fermées)
        # Note : on a déjà parcouru les barres précédentes en raison de la boucle
        # → on reconstruit l'état depuis le début de la session si pas déjà fait.
        # Pour simplifier et garantir la cohérence : on recalcule l'état en
        # parcourant les barres Londres déjà fermées (i.e. de start_london à i-1).
        if not (st["broke_high"] or st["broke_low"]):
            # On reconstruit l'état historique depuis le début de la fenêtre
            # Londres du jour, en partant du premier index Londres jusqu'à i-1.
            # Pour éviter la complexité, on parcourt en arrière jusqu'à trouver
            # le début de Londres.
            j = i - 1
            while j >= 0:
                ts_j = idx_ny_all[j]
                # On s'arrête dès qu'on sort de la fenêtre Londres
                if not _is_in_london_window(ts_j) or ts_j.date() != ts_ny.date():
                    break
                bar_j = df.iloc[j]
                h_j, l_j = float(bar_j["high"]), float(bar_j["low"])
                if h_j >= asia_high + entry_buf and not st["broke_high"]:
                    st["broke_high"] = True
                    st["high_break_ts"] = idx_ny_all[j]
                if l_j <= asia_low - entry_buf and not st["broke_low"]:
                    st["broke_low"] = True
                    st["low_break_ts"] = idx_ny_all[j]
                j -= 1

        # ── v2 : Filtres avant détection du signal ──
        # Filtre ADX : skip si trending fort (les fake breakouts deviennent
        # rares en régime trending — les cassures vont vraiment plus loin)
        adx_val = float(bar["adx"]) if pd.notna(bar["adx"]) else 0.0
        if adx_val > ARF_ADX_MAX:
            continue

        # Filtre whipsaw : si DOUBLE cassure dans la session, le range est mort
        if ARF_SKIP_DOUBLE_BREAKOUT and st["broke_high"] and st["broke_low"]:
            continue

        # ── v4 : Filtre ATR percentile (volatilité contextuelle) ──
        atr_pct_val = float(bar["atr_pct"]) if pd.notna(bar["atr_pct"]) else None
        if atr_pct_val is not None:
            if atr_pct_val < ARF_ATR_PCT_MIN or atr_pct_val > ARF_ATR_PCT_MAX:
                continue

        # ── Détection du signal d'entrée ──
        # SHORT : a cassé asia_high + buf ET la barre prev clôture
        #         FRANCHEMENT (≥ return_confirm * ATR) sous asia_high.
        # LONG  : symétrique.
        prev_close = float(prev["close"])
        return_buf = ARF_RETURN_CONFIRM_ATR * atr_val

        signal = None
        use_market = (ARF_ENTRY_MODE == "market_return")

        if st["broke_high"] and prev_close < asia_high - return_buf:
            # Fake breakout haussier confirmé → short
            # Mode market_return : entry = close de la barre courante (retour
            # déjà observé sur prev). Cohérent : on décide à l'ouverture de
            # bar (bar.close du SIGNAL est utilisé comme exécution → léger biais
            # mais conforme aux templates existants OPR/Fib/VPC).
            if use_market:
                entry = float(bar["close"])
            else:
                entry = asia_high

            # Stop loss au-dessus du HIGH d'excursion réel observé pendant
            # la cassure. On retrouve ce high via les barres déjà parcourues.
            # Approche pragmatique : SL = asia_high + sl_buffer_atr * atr.
            # Avec sl_buffer_atr ≈ 1.2 → tolère excursion ≈ médiane.
            sl = asia_high + max(
                sl_buffer_atr * atr_val,
                ARF_SL_BUFFER_TICKS_FLOOR * tick_size,
            )

            # TP : option A) RR fixe ; option B) % du range vers l'opposé.
            # En mode market_return, on prend B (target_pct du range).
            if use_market:
                # cible vers asia_low : entry - target_pct * range
                tp_dist = ARF_TP_RANGE_PCT * (asia_high - asia_low)
                tp = entry - tp_dist
            else:
                sl_dist = sl - entry
                tp_dist = tp_rr * sl_dist
                tp = entry - tp_dist

            signal = {
                "direction": "short",
                "entry":     entry,
                "level":     asia_high,
                "sl":        sl,
                "tp":        tp,
                "use_market": use_market,
            }
        elif st["broke_low"] and prev_close > asia_low + return_buf:
            # Fake breakout baissier confirmé → long
            if use_market:
                entry = float(bar["close"])
            else:
                entry = asia_low

            sl = asia_low - max(
                sl_buffer_atr * atr_val,
                ARF_SL_BUFFER_TICKS_FLOOR * tick_size,
            )

            if use_market:
                tp_dist = ARF_TP_RANGE_PCT * (asia_high - asia_low)
                tp = entry + tp_dist
            else:
                sl_dist = entry - sl
                tp_dist = tp_rr * sl_dist
                tp = entry + tp_dist

            signal = {
                "direction": "long",
                "entry":     entry,
                "level":     asia_low,
                "sl":        sl,
                "tp":        tp,
                "use_market": use_market,
            }

        if signal is None:
            continue

        direction = signal["direction"]
        entry = signal["entry"]
        sl    = signal["sl"]
        tp    = signal["tp"]
        sl_dist = abs(entry - sl)
        tp_dist = abs(tp - entry)
        if sl_dist <= 0 or tp_dist <= 0:
            continue
        rr = tp_dist / sl_dist

        # Sizing avec slippage anticipé
        risk_per_contract = (sl_dist + slip_px) * pt_value
        if risk_per_contract <= 0:
            continue
        n_ct = max(1, int(RISK_PER_TRADE_USD / risk_per_contract))

        # ── Simulation fill + exit (CONSERVATIVE) ──
        # Ordre LIMIT @ entry. Cherche un fill dans les ORDER_TIMEOUT_BARS suivantes.
        # Filtre sortie : si la fenêtre Londres est totalement passée et pas
        # de fill → annulation.
        result    = "NOT_FILLED"
        fill_time = None
        exit_time = None
        exit_px   = None
        pnl_gross = 0.0
        pnl       = 0.0

        fill_idx = None
        if signal.get("use_market", False):
            # Mode market_return : fill immédiat sur la barre courante
            # (on entre au close de la barre i, conformément aux conventions
            # OPR/Fib/VPC pour les entrées market).
            fill_idx = i
            fill_time = df.index[i]
        else:
            max_fill_search = min(i + ARF_ORDER_TIMEOUT_BARS + 1, n)
            for j in range(i + 1, max_fill_search):
                fut = df.iloc[j]
                ts_j = idx_ny_all[j]
                if ts_j.hour >= 6 and ts_j.hour < 18:
                    break
                if direction == "long" and float(fut["low"]) <= entry:
                    fill_idx = j
                    fill_time = df.index[j]
                    break
                if direction == "short" and float(fut["high"]) >= entry:
                    fill_idx = j
                    fill_time = df.index[j]
                    break

        if fill_idx is not None:
            # En mode market_return, l'entrée est au close de fill_idx — pas
            # d'ambiguïté sur cette barre, on commence à suivre à fill_idx+1.
            # En mode limit, on a un fill intra-barre fill_idx → ambiguïté
            # possible avec SL/TP sur la même barre (SL prioritaire).
            if signal.get("use_market", False):
                sl_hit_at_fill = False
                tp_hit_at_fill = False
            else:
                fb = df.iloc[fill_idx]
                if direction == "long":
                    sl_hit_at_fill = float(fb["low"])  <= sl
                    tp_hit_at_fill = float(fb["high"]) >= tp
                else:
                    sl_hit_at_fill = float(fb["high"]) >= sl
                    tp_hit_at_fill = float(fb["low"])  <= tp

            if sl_hit_at_fill:
                result, exit_px, exit_time = "SL", sl, df.index[fill_idx]
            elif tp_hit_at_fill:
                result, exit_px, exit_time = "TP", tp, df.index[fill_idx]
            else:
                # Suit les barres suivantes (max hold + time-out fin Londres)
                max_end = min(fill_idx + ARF_MAX_HOLD_BARS, n)
                exit_found = False
                for k in range(fill_idx + 1, max_end):
                    fk = df.iloc[k]
                    k_ny = idx_ny_all[k]
                    # Forçage fermeture si on entre dans la session US
                    # (on ne porte pas le trade dans NY)
                    if k_ny.hour >= 9 and k_ny.hour < 18:
                        result, exit_px, exit_time = "TE", float(fk["open"]), df.index[k]
                        exit_found = True
                        break
                    if direction == "long":
                        sl_k = float(fk["low"])  <= sl
                        tp_k = float(fk["high"]) >= tp
                    else:
                        sl_k = float(fk["high"]) >= sl
                        tp_k = float(fk["low"])  <= tp

                    if sl_k and tp_k:
                        result, exit_px, exit_time = "SL", sl, df.index[k]
                        exit_found = True
                        break
                    if sl_k:
                        result, exit_px, exit_time = "SL", sl, df.index[k]
                        exit_found = True
                        break
                    if tp_k:
                        result, exit_px, exit_time = "TP", tp, df.index[k]
                        exit_found = True
                        break
                if not exit_found:
                    last_k = min(fill_idx + ARF_MAX_HOLD_BARS, n - 1)
                    result    = "TE"
                    exit_px   = float(df.iloc[last_k]["close"])
                    exit_time = df.index[last_k]

            # P&L brut + net
            raw_move = (exit_px - entry) * (1 if direction == "long" else -1)
            pnl_gross = raw_move * n_ct * pt_value
            slip_cost = 2 * slip_px * n_ct * pt_value           # entrée + sortie
            comm_cost = COMMISSION_RT_PER_CONTRACT * n_ct       # round-trip
            pnl       = pnl_gross - slip_cost - comm_cost
            daily_trades[date_str] = daily_trades.get(date_str, 0) + 1
        else:
            # Pas de fill → on incrémente quand même un compteur logique pour
            # ne pas générer 10 signaux sur la même session
            daily_trades[date_str] = daily_trades.get(date_str, 0) + 1

        # Tagging régime
        if pd.notna(bar["adx"]) and float(bar["adx"]) > 25:
            regime = "trending"
        elif pd.notna(bar["adx"]) and float(bar["adx"]) < 20:
            regime = "ranging"
        else:
            regime = "neutral"

        is_macro = date_str in MACRO_EVENT_DATES

        trades.append({
            "date":         date_str,
            "ticker":       ticker,
            "strategy":     "ARF",
            "dir":          direction,
            "entry":        round(float(entry), 4),
            "sl":           round(float(sl), 4),
            "tp":           round(float(tp), 4),
            "sl_dist":      round(float(sl_dist), 4),
            "tp_dist":      round(float(tp_dist), 4),
            "rr":           round(float(rr), 2),
            "n_ct":         int(n_ct),
            "result":       result,
            "pnl":          round(float(pnl), 2),
            "fill_time":    fill_time,
            "exit_time":    exit_time,
            "exit":         round(float(exit_px), 4) if exit_px is not None else None,
            "regime":       regime,
            # Optionnelles
            "pnl_gross":    round(float(pnl_gross), 2),
            "adx":          round(float(bar["adx"]), 1) if pd.notna(bar["adx"]) else None,
            "atr_pct":      round(float(bar["atr_pct"]), 2) if pd.notna(bar["atr_pct"]) else None,
            "is_macro_day": bool(is_macro),
            "asia_high":    round(float(asia_high), 4),
            "asia_low":     round(float(asia_low), 4),
            "asia_range":   round(float(asia_range), 4),
            "setup_low":    round(float(asia_low), 4),    # alias pour plot_day commun
            "setup_high":   round(float(asia_high), 4),
        })

    cols = [
        "date", "ticker", "strategy", "dir", "entry", "sl", "tp",
        "sl_dist", "tp_dist", "rr", "n_ct", "result", "pnl",
        "fill_time", "exit_time", "exit", "regime",
        "pnl_gross", "adx", "atr_pct", "is_macro_day",
        "asia_high", "asia_low", "asia_range", "setup_low", "setup_high",
    ]
    return pd.DataFrame(trades, columns=cols) if trades else pd.DataFrame(columns=cols)


# ══════════════════════════════════════════════════════════════════════════════
# Visualisation d'une journée
# ══════════════════════════════════════════════════════════════════════════════

def plot_day(
    df_15m: pd.DataFrame,
    ticker: str,
    date_str: str,
    day_trades: list,
    output_path: str,
) -> None:
    """
    Chart d'une journée ARF : OHLC + niveaux asia + trades + ATR.
    Affiche la session asiatique en grisé et la fenêtre Londres en bleu clair.
    """
    if df_15m.index.tz is None:
        idx_ny = df_15m.index.tz_localize("UTC").tz_convert(_NY)
    else:
        idx_ny = df_15m.index.tz_convert(_NY)

    # On affiche du début Asia (veille 19h NY) jusqu'à 09h NY courant
    target_date = pd.Timestamp(date_str).date()
    start_dt = pd.Timestamp(target_date) - pd.Timedelta(hours=5)   # 19h veille NY
    end_dt   = pd.Timestamp(target_date) + pd.Timedelta(hours=9)   # 09h courant NY

    mask = pd.Series([
        (start_dt <= ts.tz_localize(None) <= end_dt) if ts.tz is not None else False
        for ts in idx_ny
    ])
    # Fallback en pandas vectorisé (idx_ny est DatetimeIndex tz-aware)
    idx_ny_naive = idx_ny.tz_localize(None)
    mask = (idx_ny_naive >= start_dt) & (idx_ny_naive <= end_dt)
    if not mask.any():
        return
    day = df_15m.iloc[np.asarray(mask)].copy()
    if day.empty:
        return

    # Recalcul ATR
    day["atr"] = _compute_atr(df_15m, ARF_ATR_PERIOD).loc[day.index]

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(16, 10),
        gridspec_kw={"height_ratios": [3, 1]}, sharex=True,
    )
    x = range(len(day))

    # Background : Asie (gris) + Londres (bleu clair)
    day_ny_local = day.index.tz_localize("UTC").tz_convert(_NY) \
                   if day.index.tz is None else day.index.tz_convert(_NY)
    for i_b, ts_b in enumerate(day_ny_local):
        if _is_in_asia_window(ts_b):
            ax1.axvspan(i_b - 0.5, i_b + 0.5, color="#3949ab", alpha=0.08, zorder=0)
        elif _is_in_london_window(ts_b):
            ax1.axvspan(i_b - 0.5, i_b + 0.5, color="#00bcd4", alpha=0.10, zorder=0)

    # Chandeliers
    for i_b, (_, row) in enumerate(day.iterrows()):
        color = "#26a69a" if row["close"] >= row["open"] else "#ef5350"
        ax1.plot([i_b, i_b], [row["low"], row["high"]], color=color, lw=0.8)
        ax1.bar(i_b, abs(row["close"] - row["open"]),
                bottom=min(row["open"], row["close"]), color=color, width=0.6, alpha=0.9)

    # Niveaux Asia (extraits du premier trade du jour si dispo)
    asia_high = asia_low = None
    if day_trades:
        t0 = day_trades[0]
        asia_high = t0.get("asia_high")
        asia_low  = t0.get("asia_low")
    if asia_high is not None:
        ax1.axhline(asia_high, color="#ffd54f", ls="-",  lw=1.2, alpha=0.85,
                    label=f"Asia High = {asia_high:.2f}")
    if asia_low is not None:
        ax1.axhline(asia_low, color="#ffd54f", ls="-",  lw=1.2, alpha=0.85,
                    label=f"Asia Low = {asia_low:.2f}")

    # Trades
    day_pnl = 0.0
    for trade in day_trades:
        if trade.get("result") == "NOT_FILLED" or trade.get("fill_time") is None:
            continue
        # localise indices fill/exit dans la fenêtre affichée
        try:
            fill_ts = pd.Timestamp(trade["fill_time"])
            if fill_ts.tz is not None:
                fill_ts = fill_ts.tz_convert(None) if hasattr(fill_ts, "tz_convert") else fill_ts.tz_localize(None)
            fill_idx_x = list(day.index).index(fill_ts)
        except (ValueError, KeyError, TypeError):
            continue
        try:
            exit_ts = pd.Timestamp(trade["exit_time"])
            if exit_ts.tz is not None:
                exit_ts = exit_ts.tz_convert(None) if hasattr(exit_ts, "tz_convert") else exit_ts.tz_localize(None)
            exit_idx_x = list(day.index).index(exit_ts)
        except (ValueError, KeyError, TypeError):
            exit_idx_x = fill_idx_x

        col = "#00c853" if trade["dir"] == "long" else "#d50000"
        mk = "^" if trade["dir"] == "long" else "v"
        ax1.scatter(fill_idx_x, trade["entry"], marker=mk, color=col, s=180,
                    zorder=5, edgecolors="white", linewidths=0.8)
        ax1.scatter(exit_idx_x, trade["exit"], marker="x", color="white", s=140, zorder=5)
        ax1.axhline(trade["sl"], color="#ef5350", ls=":", lw=0.7, alpha=0.5)
        ax1.axhline(trade["tp"], color="#26a69a", ls=":", lw=0.7, alpha=0.5)
        ax1.annotate(
            f"ARF {trade['dir'][0].upper()} {trade['pnl']:+.0f}$",
            xy=(fill_idx_x, trade["entry"]),
            xytext=(fill_idx_x + 1, trade["entry"]),
            fontsize=8, color="white", alpha=0.85,
        )
        day_pnl += float(trade.get("pnl", 0.0))

    # ATR en sous-graphe
    ax2.fill_between(x, 0, day["atr"].values, alpha=0.5, color="#7986cb")
    ax2.set_ylabel("ATR", fontsize=9)

    ax1.set_title(
        f"{ticker} — {date_str}  [ARF]   |   P&L net jour : {day_pnl:+.0f} $",
        fontsize=12, fontweight="bold",
    )
    ax1.legend(loc="upper left", fontsize=8)
    ax1.set_facecolor("#1a1a2e")
    ax2.set_facecolor("#1a1a2e")
    fig.patch.set_facecolor("#13131f")
    ax1.tick_params(colors="white")
    ax2.tick_params(colors="white")
    ax1.title.set_color("white")
    for s in list(ax1.spines.values()) + list(ax2.spines.values()):
        s.set_color("#787b86")

    plt.tight_layout()
    plt.savefig(output_path, dpi=120, bbox_inches="tight")
    plt.close(fig)
