"""
Optimisation data-driven du SL/TP d'OPR par analyse MFE/MAE sur données M1.

Approche (cf. plan « Optimisation data-driven du SL/TP d'OPR ») :
  1. Construit un M15 auto-cohérent par resample du M1 (série continue NQ1!,
     back-adjustment propre — on ne mélange pas avec data/ qui a un offset).
  2. Extrait l'entrée OPR « primaire » de chaque jour (1er trigger fillé),
     indépendante du SL/TP (le 1er fill ne dépend pas des niveaux de sortie).
  3. Reconstruit la trajectoire forward M1 du fill jusqu'à 16h30 NY et calcule
     MFE/MAE (normalisés ATR journalier).
  4. Construit la surface d'espérance NETTE $/trade sur une grille (SL,TP) en
     unités ATR, contrainte RR = TP/SL ≥ 2, via simulation first-touch (règle
     same-bar conservatrice = SL). Optimum = plateau max E[$/trade] avec n ≥ plancher.
  5. Walk-forward : estime sur l'IS, valide sur l'OOS.

Le résultat (SL_atr, TP_atr) est en multiples d'ATR → transférable à la prod
(validation séparée sur le vrai backtest, série data/).

Usage :
    python scripts/opr_sltp_mfe_mae.py --ticker NQ1 [--no-cache] [--plot]
"""

from __future__ import annotations

import argparse
import pickle
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config as cfg  # noqa: E402
from core.opr import _bar_hits, run_opr_day  # noqa: E402
from core.optimizer import OOS_START as OOS_START_STR  # noqa: E402

np.random.seed(42)

NY = ZoneInfo("America/New_York")
OOS_START = pd.Timestamp(OOS_START_STR)  # 2025-10-01
M1_DIR = ROOT / "DATA_BACKTEST"
OUT_DIR = ROOT / "output"
CACHE_DIR = OUT_DIR / "cache"


# ════════════════════════════════════════════════════════════════════════════
# 1. Données
# ════════════════════════════════════════════════════════════════════════════
def load_m1(ticker: str) -> pd.DataFrame:
    """Charge le M1 (UTC naïf, OHLCV) depuis DATA_BACKTEST/<T>_data_m1.csv."""
    path = M1_DIR / f"{ticker}_data_m1.csv"
    df = pd.read_csv(path, parse_dates=["datetime"])
    df = df.drop_duplicates(subset=["datetime"], keep="last")
    df = df.sort_values("datetime").set_index("datetime")
    return df[["open", "high", "low", "close", "volume"]]


def m1_to_m15(m1: pd.DataFrame) -> pd.DataFrame:
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    return m1.resample("15min").agg(agg).dropna()


# ════════════════════════════════════════════════════════════════════════════
# 2-3. Extraction des entrées + trajectoires M1 + MFE/MAE
# ════════════════════════════════════════════════════════════════════════════
def extract_entries(ticker: str, m1: pd.DataFrame, m15: pd.DataFrame) -> list[dict]:
    """1 entrée/jour = 1er trigger OPR fillé (SL/TP-neutre). Trajectoire M1 + MFE/MAE."""
    idx_ny = m15.index.tz_localize("UTC").tz_convert(NY)
    days = pd.DatetimeIndex(idx_ny.normalize().unique()).sort_values()

    entries: list[dict] = []
    for day_ny in days:
        signals, trades, _ = run_opr_day(m15, ticker, day_ny)
        if not signals:
            continue
        # 1er trigger fillé
        first = None
        for sig, tr in zip(signals, trades):
            if tr.get("result") != "NOT_FILLED":
                first = (sig, tr)
                break
        if first is None:
            continue
        sig, tr = first
        atr = float(sig["atr_daily"])
        if not np.isfinite(atr) or atr <= 0:
            continue
        entry = float(sig["entry"])
        direction = sig["direction"]

        # fill_time → UTC naïf ; fenêtre M15 du fill ; session_end 16h30 NY → UTC
        ft = pd.Timestamp(tr["fill_time"])
        ft_utc = ft.tz_convert("UTC").tz_localize(None) if ft.tzinfo else ft
        ft_ny = ft.tz_convert(NY) if ft.tzinfo else ft.tz_localize("UTC").tz_convert(NY)
        sess_end_ny = ft_ny.normalize().replace(hour=16, minute=30)
        sess_end_utc = sess_end_ny.tz_convert("UTC").tz_localize(None)

        # Fill précis : 1ère barre M1 de la fenêtre [ft_utc, ft_utc+15min) qui touche le niveau
        win = m1[(m1.index >= ft_utc) & (m1.index < ft_utc + pd.Timedelta(minutes=15))]
        touch_idx = None
        for ts, bar in win.iterrows():
            if _bar_hits(direction, entry, bar):
                touch_idx = ts
                break
        if touch_idx is None:
            continue  # pas de M1 confirmant le fill (gap data) → skip

        fwd = m1[(m1.index >= touch_idx) & (m1.index <= sess_end_utc)]
        if len(fwd) < 2:
            continue

        H = fwd["high"].to_numpy(float)
        L = fwd["low"].to_numpy(float)
        last_close = float(fwd["close"].iloc[-1])

        if direction == "long":
            mfe = max(H.max() - entry, 0.0)
            mae = max(entry - L.min(), 0.0)
        else:
            mfe = max(entry - L.min(), 0.0)
            mae = max(H.max() - entry, 0.0)

        entries.append(
            {
                "date": ft_ny.normalize().strftime("%Y-%m-%d"),
                "ts_utc": touch_idx,
                "direction": direction,
                "entry": entry,
                "atr": atr,
                "H": H,
                "L": L,
                "last_close": last_close,
                "n_bars": len(fwd),
                "mfe_pts": mfe,
                "mae_pts": mae,
                "mfe_atr": mfe / atr,
                "mae_atr": mae / atr,
            }
        )
    return entries


# ════════════════════════════════════════════════════════════════════════════
# 4. Simulation first-touch + espérance nette
# ════════════════════════════════════════════════════════════════════════════
def _friction_usd(ticker: str, n_ct: int) -> float:
    instr = cfg.INSTRUMENTS[ticker]
    tick_value = instr["dollar_per_point"] * instr["tick_size"]
    slip = 2.0 * cfg.SLIPPAGE_TICKS_PER_TICKER.get(ticker, 1) * tick_value * n_ct
    comm = cfg.COMMISSION_RT_PER_CONTRACT * n_ct
    return slip + comm


def simulate(entry: dict, ticker: str, sl_atr: float, tp_atr: float) -> dict:
    """First-touch sur la trajectoire M1. Same-bar → SL (conservateur)."""
    e, atr, d = entry["entry"], entry["atr"], entry["direction"]
    H, L = entry["H"], entry["L"]
    dpp = cfg.INSTRUMENTS[ticker]["dollar_per_point"]
    sl_min = cfg.OPR_SL_MIN_POINTS.get(ticker, 0.0)

    sl_pts = max(sl_atr * atr, sl_min)
    tp_pts = tp_atr * atr
    if sl_pts <= 0:
        return {"result": "SKIP", "pnl_net": 0.0, "n_ct": 0}

    n_ct = max(int(cfg.RISK_PER_TRADE_USD / (sl_pts * dpp)), 1)

    if d == "long":
        sl_price, tp_price = e - sl_pts, e + tp_pts
        sl_mask, tp_mask = sl_price >= L, tp_price <= H
    else:
        sl_price, tp_price = e + sl_pts, e - tp_pts
        sl_mask, tp_mask = sl_price <= H, tp_price >= L

    fsl = int(np.argmax(sl_mask)) if sl_mask.any() else 10**9
    ftp = int(np.argmax(tp_mask)) if tp_mask.any() else 10**9

    if fsl == 10**9 and ftp == 10**9:
        result, exit_p = "TE", entry["last_close"]
    elif fsl <= ftp:  # same-bar inclus → SL
        result, exit_p = "SL", sl_price
    else:
        result, exit_p = "TP", tp_price

    pnl_pts = (exit_p - e) if d == "long" else (e - exit_p)
    gross = n_ct * pnl_pts * dpp
    net = gross - _friction_usd(ticker, n_ct)
    return {"result": result, "pnl_net": net, "n_ct": n_ct}


def evaluate_grid(entries: list[dict], ticker: str, sl_grid, tp_grid, min_rr=2.0):
    """Surface E[$net/trade] sur (sl_atr, tp_atr) avec RR≥min_rr."""
    rows = []
    for sl in sl_grid:
        for tp in tp_grid:
            if tp / sl < min_rr - 1e-9:
                continue
            pnls = []
            n_tp = n_sl = n_te = 0
            for en in entries:
                r = simulate(en, ticker, sl, tp)
                if r["result"] == "SKIP":
                    continue
                pnls.append(r["pnl_net"])
                n_tp += r["result"] == "TP"
                n_sl += r["result"] == "SL"
                n_te += r["result"] == "TE"
            if not pnls:
                continue
            pnls = np.array(pnls)
            gains = pnls[pnls > 0].sum()
            losses = -pnls[pnls < 0].sum()
            pf = gains / losses if losses > 0 else (np.inf if gains > 0 else 0.0)
            rows.append(
                {
                    "sl_atr": round(sl, 3),
                    "tp_atr": round(tp, 3),
                    "rr": round(tp / sl, 2),
                    "n": len(pnls),
                    "exp_net": pnls.mean(),
                    "pnl_net": pnls.sum(),
                    "pf": pf,
                    "wr": 100 * (pnls > 0).mean(),
                    "n_tp": n_tp,
                    "n_sl": n_sl,
                    "n_te": n_te,
                }
            )
    return pd.DataFrame(rows)


# ════════════════════════════════════════════════════════════════════════════
# Main
# ════════════════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", default="NQ1")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--min-n-oos", type=int, default=20)
    args = ap.parse_args()
    ticker = args.ticker

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"opr_entries_m1_{ticker}.pkl"

    if cache.exists() and not args.no_cache:
        entries = pickle.loads(cache.read_bytes())
        print(f"[cache] {len(entries)} entrées chargées depuis {cache.name}")
    else:
        print(f"[1] Chargement M1 {ticker}...")
        m1 = load_m1(ticker)
        m15 = m1_to_m15(m1)
        print(f"    M1 {len(m1)} barres ({m1.index.min()} → {m1.index.max()}) ; M15 {len(m15)}")
        print("[2-3] Extraction entrées + trajectoires M1 + MFE/MAE...")
        entries = extract_entries(ticker, m1, m15)
        cache.write_bytes(pickle.dumps(entries))
        print(f"    {len(entries)} entrées extraites → {cache.name}")

    if not entries:
        print("Aucune entrée — abandon.")
        return

    df_en = pd.DataFrame(
        [{k: e[k] for k in ("date", "direction", "mfe_atr", "mae_atr", "n_bars")} for e in entries]
    )
    df_en["date"] = pd.to_datetime(df_en["date"])
    is_mask = df_en["date"] < OOS_START
    print(
        f"\n=== Échantillon {ticker} : {len(entries)} entrées "
        f"(IS={is_mask.sum()}, OOS={(~is_mask).sum()}) ==="
    )
    print(
        f"  MFE médian {df_en['mfe_atr'].median():.2f} ATR ; MAE médian {df_en['mae_atr'].median():.2f} ATR"
    )
    print(
        f"  longs {(df_en['direction']=='long').sum()} / shorts {(df_en['direction']=='short').sum()}"
    )

    # Grilles
    sl_grid = np.round(np.arange(0.10, 1.51, 0.05), 3)
    tp_grid = np.round(np.arange(0.20, 3.01, 0.10), 3)

    entries_is = [e for e, m in zip(entries, is_mask) if m]
    entries_oos = [e for e, m in zip(entries, is_mask) if not m]

    print("\n[4] Surface d'espérance IS (RR≥2)...")
    grid_is = evaluate_grid(entries_is, ticker, sl_grid, tp_grid)
    # plancher de fréquence proportionnel (IS plus long que OOS)
    min_n_is = max(args.min_n_oos * len(entries_is) // max(len(entries_oos), 1), args.min_n_oos)
    cand = grid_is[grid_is["n"] >= min_n_is].copy()
    if cand.empty:
        cand = grid_is.copy()
    cand = cand.sort_values("exp_net", ascending=False)
    best = cand.iloc[0]
    print(
        f"  meilleur IS : SL={best.sl_atr} TP={best.tp_atr} (RR={best.rr})  "
        f"E[$]={best.exp_net:.2f}  n={int(best.n)}  PF={best.pf:.2f}  WR={best.wr:.0f}%"
    )

    print("\n[5] Validation OOS (SL/TP retenu sur IS)...")
    grid_oos = evaluate_grid(entries_oos, ticker, [best.sl_atr], [best.tp_atr])
    if not grid_oos.empty:
        o = grid_oos.iloc[0]
        print(
            f"  OOS : SL={o.sl_atr} TP={o.tp_atr}  E[$]={o.exp_net:.2f}  n={int(o.n)}  "
            f"PF={o.pf:.2f}  WR={o.wr:.0f}%  PnL_net={o.pnl_net:.0f}$"
        )

    print("\n  Top IS (E[$net/trade]) :")
    print(
        cand.head(8)[["sl_atr", "tp_atr", "rr", "n", "exp_net", "pnl_net", "pf", "wr"]].to_string(
            index=False
        )
    )

    # ── Robustesse IS↔OOS sur TOUTE la surface ──────────────────────────────
    print("\n[6] Robustesse IS↔OOS (toute la surface RR≥2)...")
    grid_oos_full = evaluate_grid(entries_oos, ticker, sl_grid, tp_grid)
    m = grid_is.merge(grid_oos_full, on=["sl_atr", "tp_atr", "rr"], suffixes=("_is", "_oos"))
    if len(m) > 3:
        corr = m["exp_net_is"].corr(m["exp_net_oos"])
        both_pos = m[(m["exp_net_is"] > 0) & (m["exp_net_oos"] > 0)]
        print(f"  corrélation E[$] IS↔OOS sur {len(m)} cellules : {corr:+.2f}")
        print(
            f"  cellules E[$]>0 sur IS ET OOS : {len(both_pos)}/{len(m)} "
            f"({100*len(both_pos)/len(m):.0f}%)"
        )
        # pick robuste : max min(IS,OOS) avec n suffisant des 2 côtés
        m["robust"] = m[["exp_net_is", "exp_net_oos"]].min(axis=1)
        mr = m[(m["n_is"] >= min_n_is) & (m["n_oos"] >= args.min_n_oos)].sort_values(
            "robust", ascending=False
        )
        if not mr.empty:
            r = mr.iloc[0]
            print(
                f"  meilleur robuste (max min(IS,OOS)) : SL={r.sl_atr} TP={r.tp_atr} (RR={r.rr})  "
                f"E[$]_IS={r.exp_net_is:.1f}  E[$]_OOS={r.exp_net_oos:.1f}  "
                f"PF_IS={r.pf_is:.2f}  PF_OOS={r.pf_oos:.2f}"
            )

    # ── Distributions MFE/MAE (insight interprétatif) ───────────────────────
    print("\n[7] Distributions MFE/MAE (ATR) :")
    for lbl, col in [("MFE", "mfe_atr"), ("MAE", "mae_atr")]:
        q = df_en[col].quantile([0.25, 0.5, 0.75, 0.9])
        print(
            f"  {lbl} : p25={q[0.25]:.2f}  p50={q[0.5]:.2f}  p75={q[0.75]:.2f}  p90={q[0.9]:.2f} ATR"
        )

    # sauvegardes
    odir = OUT_DIR / f"opr_sltp_{ticker.lower()}"
    odir.mkdir(parents=True, exist_ok=True)
    grid_is.to_csv(odir / "surface_is.csv", index=False)
    if len(m) > 3:
        m.to_csv(odir / "surface_is_oos.csv", index=False)
    df_en.to_csv(odir / "entries.csv", index=False)
    print(f"\nÉcrit : {odir}/surface_is.csv, surface_is_oos.csv, entries.csv")


if __name__ == "__main__":
    main()
