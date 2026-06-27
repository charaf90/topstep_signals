"""
core/bt_engine.py — Moteur de backtest M1 (backtesting.py) = VÉRITÉ DE FILL.

Remplace le moteur maison M15 qui ne résolvait pas le fill **same-bar** (un SL touché
dans la bougie même du fill d'un ordre limite était ignoré). Ici :

  1. les SIGNAUX d'une stratégie sont calculés sur sa TF de signal (M15/M5),
     **reconstruite depuis le M1** (features décalées à la CLÔTURE de barre → no leak) ;
  2. les ORDRES émis sont **rejoués sur les barres M1** par backtesting.py : fill + SL/TP
     résolus à la minute, **SL-prioritaire** quand SL et TP tombent dans la même minute ;
  3. la friction (slippage + commission) est **recalculée par la formule maison** (net_usd),
     backtesting.py tournant commission=0 → P&L net identique au moteur historique.

Contrat d'un module stratégie M1 (`strategies/<nom>.py` ou `brouillon/strategies/<nom>.py`) :

    STRATEGY_ID : str
    TICKERS     : list[str]
    SIGNAL_TF   : "15min" | "5min"            (défaut "15min")
    SESSION_END_MIN : int  (minute NY de clôture forcée ; défaut 960 = 16:00)
    RUN_MIN_WINDOW  : (int, int) | None       (fenêtre NY des barres M1 à simuler — perf ;
                                               None = journée entière)
    emit_signals(sig_df, ticker, params) -> list[dict]
        sig_df = OHLCV sur SIGNAL_TF (colonnes open/high/low/close/volume, index UTC naïf).
        Chaque ordre (« arm ») = dict :
            dir            : "long" | "short"
            entry, sl, tp  : prix (déjà arrondis au tick)
            n_ct           : nombre de contrats (>0)
            arm_ts         : timestamp de la barre de signal (UTC naïf)
            # optionnels :
            sl_dist, tp_dist, rr, regime, timeout_ts (Timestamp d'expiration de l'ordre),
            cancel_price + cancel_side ("above"|"below") : invalidation du pending avant fill,
            + extras (passés tels quels dans le schéma de trades).
    PARAM_GRID / PARAM_SPACE (optionnel — pour optimize.py)

Sorties de `run(...)` :
    {"trades": DataFrame(schéma canonique), "n_signals": int, "stats": <backtesting stats>,
     "html_path": str|None}

Le schéma canonique des trades est identique aux wrappers maison (compat core/metrics.py,
core/robustness.py, @quant) : date, dir, entry, sl, tp, sl_dist, tp_dist, rr, n_ct,
result{TP|SL|TE|NOT_FILLED}, pnl (NET), fill_time, exit_time, exit, regime, pnl_gross, timeframe.
"""

from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from backtesting import Backtest, Strategy

from config import COMMISSION_RT_PER_CONTRACT, INSTRUMENTS, SLIPPAGE_TICKS_PER_TICKER
from core.data import load_csv

_NY = ZoneInfo("America/New_York")
_M1_DIR = "DATA_BACKTEST"
_BACKTESTS_DIR = Path("output/backtests")
_TF_MINUTES = {"15min": 15, "5min": 5, "1min": 1}

# Schéma canonique (ordre identique aux wrappers maison + extras stratégie en fin).
_CANON_COLS = [
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
    "timeframe",
]


# ══════════════════════════════════════════════════════════════════════════════
# Données M1 + friction
# ══════════════════════════════════════════════════════════════════════════════

_M1_CACHE: dict[str, pd.DataFrame] = {}


def load_m1(ticker: str, data_dir: str = _M1_DIR) -> pd.DataFrame:
    """Charge les barres M1 (`DATA_BACKTEST/<TICKER>_data_m1.csv`, index UTC naïf).

    Mis en cache (le CSV M1 ~22 Mo est rechargé à chaque combo par l'optimizer) — les
    appelants ne mutent jamais le df retourné (slice/copy systématiques en aval)."""
    key = f"{data_dir}/{ticker}"
    if key not in _M1_CACHE:
        _M1_CACHE[key] = load_csv(f"{data_dir}/{ticker}_data_m1.csv")
    return _M1_CACHE[key]


def to_signal_tf(m1: pd.DataFrame, tf: str = "15min") -> pd.DataFrame:
    """Reconstruit les barres de signal (M15/M5) depuis le M1 (resample left-closed)."""
    if tf in ("1min", None):
        return m1
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    return m1.resample(tf, label="left", closed="left").agg(agg).dropna()


def net_pnl_usd(entry: float, exit_px: float, size_signed: int, ticker: str) -> float:
    """P&L NET maison : brut $ − slippage (entrée+sortie) − commission round-trip."""
    instr = INSTRUMENTS[ticker]
    dpp = instr["dollar_per_point"]
    slip_px = SLIPPAGE_TICKS_PER_TICKER.get(ticker, 1) * instr["tick_size"]
    n = abs(int(size_signed))
    raw = (exit_px - entry) * size_signed  # size<0 pour un short
    gross = raw * dpp
    return gross - 2 * slip_px * n * dpp - COMMISSION_RT_PER_CONTRACT * n


def gross_pnl_usd(entry: float, exit_px: float, size_signed: int, ticker: str) -> float:
    raw = (exit_px - entry) * size_signed
    return raw * INSTRUMENTS[ticker]["dollar_per_point"]


# ══════════════════════════════════════════════════════════════════════════════
# Stratégie générique de REPLAY (place les ordres pré-calculés, fills résolus M1)
# ══════════════════════════════════════════════════════════════════════════════


def _make_replay_strategy(
    place: dict, eod_close_min: int, max_hold_min: int | None = None, dayclose: bool = True
):
    """Fabrique une classe backtesting.py qui rejoue `place` (ts_M1 → arm) sur M1.

    Un seul ordre/position à la fois (sémantique des wrappers : `continue` si occupé).
    Sorties gérées : SL/TP (backtesting.py, M1), clôture forcée à `eod_close_min`,
    `max_hold_min` (durée max de portage = TE), et changement de jour si `dayclose`
    (intraday strict ; False = la position peut porter au-delà de minuit, ex. fib-v4).
    """
    hold_delta = pd.Timedelta(minutes=max_hold_min) if max_hold_min else None

    class _Replay(Strategy):
        def init(self):
            self._cur_day = None
            self._order = None  # ordre d'entrée en attente
            self._arm = None  # arm associé à cet ordre

        def _cancel_pending(self):
            if self._order is not None and self._order in self.orders:
                self._order.cancel()
            self._order = None
            self._arm = None

        def _close_position(self):
            if self.position:
                self.position.close()

        def next(self):
            data = self.data
            ts = data.index[-1]
            m = int(data.Mins[-1])
            day = int(data.Dayord[-1])

            # Nouveau jour : pending jamais reporté ; position fermée si intraday strict.
            if day != self._cur_day:
                self._cur_day = day
                self._cancel_pending()
                if dayclose:
                    self._close_position()

            # Clôture forcée de fin de session.
            if m >= eod_close_min:
                self._close_position()
                self._cancel_pending()
                return

            # Time-stop : durée max de portage atteinte → TE. L'instant d'entrée vient
            # directement de backtesting.py (self.trades = trades OUVERTS) → jamais périmé.
            if self.position and hold_delta is not None and self.trades:
                if ts - self.trades[-1].entry_time >= hold_delta:
                    self._close_position()
                    return

            # Suivi du pending : rempli ? expiré ? invalidé ?
            if self._order is not None:
                if self._order not in self.orders:  # rempli (→ position) ou déjà annulé
                    self._order = None
                    self._arm = None
                else:
                    arm = self._arm
                    expired = arm.get("timeout_ts") is not None and ts >= arm["timeout_ts"]
                    invalidated = False
                    cp = arm.get("cancel_price")
                    if cp is not None:
                        side = arm.get("cancel_side")
                        if (side == "above" and data.High[-1] >= cp) or (
                            side == "below" and data.Low[-1] <= cp
                        ):
                            invalidated = True
                    if expired or invalidated:
                        self._cancel_pending()

            # Armement d'un nouvel ordre si libre.
            if self.position or self._order is not None:
                return
            arm = place.get(ts)
            if arm is None:
                return
            size = int(arm["n_ct"])
            if size <= 0:
                return
            place_order = self.buy if arm["dir"] == "long" else self.sell
            try:
                self._order = place_order(
                    limit=arm["entry"], sl=arm["sl"], tp=arm["tp"], size=size, tag=arm["id"]
                )
                self._arm = arm
            except Exception:
                self._order = None
                self._arm = None

    return _Replay


# ══════════════════════════════════════════════════════════════════════════════
# Conversion backtesting._trades → schéma canonique
# ══════════════════════════════════════════════════════════════════════════════


def _classify(exit_px: float, arm: dict, tick: float) -> str:
    """TP / SL / TE par comparaison du prix de sortie aux bornes (SL-prioritaire)."""
    tol = tick / 2
    if arm["dir"] == "long":
        if exit_px <= arm["sl"] + tol:
            return "SL"
        if exit_px >= arm["tp"] - tol:
            return "TP"
    else:
        if exit_px >= arm["sl"] - tol:
            return "SL"
        if exit_px <= arm["tp"] + tol:
            return "TP"
    return "TE"


def _trades_to_canonical(
    bt_trades: pd.DataFrame, arms: list[dict], ticker: str, signal_tf: str
) -> pd.DataFrame:
    """Joint les trades backtesting.py aux arms (via tag) → schéma canonique + NOT_FILLED."""
    tick = INSTRUMENTS[ticker]["tick_size"]
    arms_by_id = {a["id"]: a for a in arms}
    filled_ids: set = set()
    rows: list[dict] = []

    for tr in bt_trades.itertuples():
        tag = getattr(tr, "Tag", None)
        arm = arms_by_id.get(tag)
        if arm is None:
            continue
        filled_ids.add(tag)
        entry_px = float(tr.EntryPrice)
        exit_px = float(tr.ExitPrice)
        size = int(tr.Size)
        result = _classify(exit_px, arm, tick)
        rows.append(
            {
                "date": pd.Timestamp(tr.ExitTime).strftime("%Y-%m-%d"),
                "dir": arm["dir"],
                "entry": entry_px,
                "sl": float(arm["sl"]),
                "tp": float(arm["tp"]),
                "sl_dist": float(arm.get("sl_dist", abs(arm["entry"] - arm["sl"]))),
                "tp_dist": float(arm.get("tp_dist", abs(arm["entry"] - arm["tp"]))),
                "rr": float(arm.get("rr", 0.0)),
                "n_ct": abs(size),
                "result": result,
                "pnl": round(net_pnl_usd(entry_px, exit_px, size, ticker), 2),
                "fill_time": str(pd.Timestamp(tr.EntryTime)),
                "exit_time": str(pd.Timestamp(tr.ExitTime)),
                "exit": exit_px,
                "regime": arm.get("regime", "neutral"),
                "pnl_gross": round(gross_pnl_usd(entry_px, exit_px, size, ticker), 2),
                "timeframe": signal_tf,
                **{k: v for k, v in arm.get("extras", {}).items()},
            }
        )

    # Arms jamais remplis → NOT_FILLED (P&L nul, exclu des stats mais visible).
    for arm in arms:
        if arm["id"] in filled_ids:
            continue
        rows.append(
            {
                "date": pd.Timestamp(arm["arm_ts"]).strftime("%Y-%m-%d"),
                "dir": arm["dir"],
                "entry": float(arm["entry"]),
                "sl": float(arm["sl"]),
                "tp": float(arm["tp"]),
                "sl_dist": float(arm.get("sl_dist", abs(arm["entry"] - arm["sl"]))),
                "tp_dist": float(arm.get("tp_dist", abs(arm["entry"] - arm["tp"]))),
                "rr": float(arm.get("rr", 0.0)),
                "n_ct": int(arm["n_ct"]),
                "result": "NOT_FILLED",
                "pnl": 0.0,
                "fill_time": None,
                "exit_time": None,
                "exit": None,
                "regime": arm.get("regime", "neutral"),
                "pnl_gross": 0.0,
                "timeframe": signal_tf,
                **{k: v for k, v in arm.get("extras", {}).items()},
            }
        )

    if not rows:
        return pd.DataFrame(columns=_CANON_COLS)
    df = pd.DataFrame(rows)
    return df.sort_values("fill_time", na_position="last").reset_index(drop=True)


# ══════════════════════════════════════════════════════════════════════════════
# Orchestration
# ══════════════════════════════════════════════════════════════════════════════


def _ny_minutes(idx: pd.DatetimeIndex) -> np.ndarray:
    ny = idx.tz_localize("UTC").tz_convert(_NY)
    return (ny.hour * 60 + ny.minute).to_numpy()


def _build_bt_df(m1: pd.DataFrame) -> pd.DataFrame:
    """OHLCV capitalisé + colonnes auxiliaires Mins/Dayord (NY) pour backtesting.py."""
    ny = m1.index.tz_localize("UTC").tz_convert(_NY)
    return pd.DataFrame(
        {
            "Open": m1["open"].to_numpy(),
            "High": m1["high"].to_numpy(),
            "Low": m1["low"].to_numpy(),
            "Close": m1["close"].to_numpy(),
            "Volume": m1["volume"].to_numpy(),
            "Mins": (ny.hour * 60 + ny.minute).to_numpy(),
            "Dayord": pd.factorize(ny.strftime("%Y-%m-%d").to_numpy())[0],
        },
        index=m1.index,
    ).dropna()


def html_path_for(strategy_id: str, ticker: str, tag: str) -> Path:
    """Convention de nom explicite : output/backtests/<id>__<ticker>__<tag>__m1.html."""
    _BACKTESTS_DIR.mkdir(parents=True, exist_ok=True)
    safe = str(tag).replace("/", "-").replace(" ", "_")
    return _BACKTESTS_DIR / f"{strategy_id}__{ticker}__{safe}__m1.html"


def run(
    strategy_module,
    ticker: str,
    params: dict | None = None,
    m1: pd.DataFrame | None = None,
    start: str | None = None,
    end: str | None = None,
    html: bool | str = False,
    html_tag: str = "full",
    plot_resample=True,
) -> dict:
    """Backtest M1 d'une stratégie sur un ticker.

    start/end (YYYY-MM-DD, inclusifs) bornent la fenêtre simulée (features calculées sur
    tout l'historique → pas de problème de warmup). html=True (ou un chemin) génère le HTML.
    """
    strategy_id = getattr(strategy_module, "STRATEGY_ID", strategy_module.__name__)
    signal_tf = getattr(strategy_module, "SIGNAL_TF", "15min")
    eod_min = getattr(strategy_module, "SESSION_END_MIN", 960)
    run_win = getattr(strategy_module, "RUN_MIN_WINDOW", None)
    max_hold_min = getattr(strategy_module, "MAX_HOLD_MIN", None)
    dayclose = getattr(strategy_module, "INTRADAY_DAYCLOSE", True)
    tf_delta = pd.Timedelta(minutes=_TF_MINUTES[signal_tf])

    if m1 is None:
        try:
            m1 = load_m1(ticker)
        except FileNotFoundError:
            # Pas de M1 pour ce ticker (gold/oil = indices uniquement) → skip propre.
            return {
                "trades": pd.DataFrame(columns=_CANON_COLS),
                "n_signals": 0,
                "stats": None,
                "html_path": None,
            }
    if len(m1) == 0:
        return {
            "trades": pd.DataFrame(columns=_CANON_COLS),
            "n_signals": 0,
            "stats": None,
            "html_path": None,
        }

    # 1) Signaux sur tout l'historique (warmup features inclus).
    sig_df = to_signal_tf(m1, signal_tf)
    arms = list(strategy_module.emit_signals(sig_df, ticker, params))

    # 2) Fenêtre de simulation.
    if start is not None:
        arms = [a for a in arms if pd.Timestamp(a["arm_ts"]).strftime("%Y-%m-%d") >= start]
        m1 = m1[m1.index >= pd.Timestamp(start)]
    if end is not None:
        end_excl = pd.Timestamp(end) + pd.Timedelta(days=1)
        arms = [a for a in arms if pd.Timestamp(a["arm_ts"]).strftime("%Y-%m-%d") <= end]
        m1 = m1[m1.index < end_excl]

    # 3) id + ts d'activation pour chaque arm. Défaut = clôture de la barre de signal
    #    (arm_ts + tf = no leak) ; une strat pré-résolue peut fournir place_ts explicite.
    place: dict = {}
    for k, a in enumerate(arms):
        a["id"] = k
        a["place_ts"] = (
            pd.Timestamp(a["place_ts"])
            if a.get("place_ts") is not None
            else pd.Timestamp(a["arm_ts"]) + tf_delta
        )
        place.setdefault(a["place_ts"], a)  # 1 arm/barre (collision improbable)

    # 4) Barres M1 à simuler (option : restreindre à la fenêtre intraday active = perf).
    bt_df = _build_bt_df(m1)
    if run_win is not None:
        lo, hi = run_win
        bt_df = bt_df[(bt_df["Mins"] >= lo) & (bt_df["Mins"] < hi)]
    if len(bt_df) == 0 or not arms:
        empty = _trades_to_canonical(pd.DataFrame(), arms, ticker, signal_tf)
        return {"trades": empty, "n_signals": len(arms), "stats": None, "html_path": None}

    # 5) Run backtesting.py (commission 0 — friction recalculée maison).
    StratCls = _make_replay_strategy(place, eod_min, max_hold_min, dayclose)
    bt = Backtest(
        bt_df,
        StratCls,
        cash=10_000_000,
        commission=0.0,
        margin=1.0,
        trade_on_close=False,
        finalize_trades=True,
    )
    stats = bt.run()

    trades = _trades_to_canonical(stats._trades, arms, ticker, signal_tf)

    # 6) HTML viz nommé.
    html_path = None
    if html:
        out = Path(html) if isinstance(html, str) else html_path_for(strategy_id, ticker, html_tag)
        try:
            bt.plot(
                filename=str(out),
                open_browser=False,
                resample=plot_resample,
                plot_volume=True,
                superimpose=False,
            )
            html_path = str(out)
        except Exception as exc:  # pragma: no cover - viz best-effort
            print(f"  [bt_engine] plot HTML échoué ({type(exc).__name__}: {exc})")

    return {"trades": trades, "n_signals": len(arms), "stats": stats, "html_path": html_path}


# Compat « registry » : un module M1 expose emit_signals + une classe Strategy générique
# n'est pas requise. backtest.py/optimize.py appellent bt_engine.run(module, ...).
def supports(strategy_module) -> bool:
    """True si le module suit le contrat M1 (callable emit_signals)."""
    return callable(getattr(strategy_module, "emit_signals", None))


def run_trades(
    strategy,
    ticker: str,
    df_15m: pd.DataFrame | None = None,
    tf=None,
    params: dict | None = None,
    topstep_guard: bool = False,
    start: str | None = None,
    end: str | None = None,
) -> pd.DataFrame:
    """Routeur unifié backtest → DataFrame de trades canonique.

    Stratégie portée (expose `emit_signals`) → moteur M1 (vérité de fill) sur tout
    l'historique (le caller filtre IS/OOS par date). Sinon → legacy `run_backtest` (M15).
    Utilisé par core/optimizer.py, tools/portfolio_replay.py… pour un comportement M1
    homogène sur toutes les stratégies portées.
    """
    if supports(strategy):
        return run(strategy, ticker, params=params, start=start, end=end, html=False)["trades"]
    return strategy.run_backtest(df_15m, ticker, tf=tf, params=params, topstep_guard=topstep_guard)
