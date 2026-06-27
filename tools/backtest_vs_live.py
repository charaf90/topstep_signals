#!/usr/bin/env python3
"""
backtest_vs_live.py — confronte, sur une JOURNÉE ou PÉRIODE, ce que les stratégies
LIVE ont réellement fait vs ce qu'un backtest fidèle aurait fait.

Pourquoi : vérifier la fidélité du live « en conditions réelles » — détecter les
écarts (signal bloqué par le RM, ordre non rempli, annulé WS, ordre erroné comme le
short involontaire du 2026-06-16, Δ de P&L) trade-par-trade.

Pipeline (lecture seule absolue côté broker/live — uniquement des get_*) :
  1. Données  : fetch des barres du jour/période DEPUIS LE BROKER (get_bars) — les CSV
                data/ sont périmés. Repli --data-source csv pour l'historique.
  2. Backtest : chaque stratégie LIVE (tools/_live_portfolio) aux params PROD, df passé
                explicitement (M15 ou M5 selon CSV_TIMEFRAME), filtré sur la période.
  3. Live     : trades depuis state.placed_tags + P&L réalisé broker (ancre) + raisons
                d'écart depuis logs/trading_events.log (BLOQUÉ / ANNULÉ / ERROR).
  4. Réconcil : appariement (stratégie, ticker, jour, sens, entry ±1 tick) →
                MATCH / BACKTEST_ONLY / LIVE_ONLY + raison.

Sortie : output/backtest_vs_live/<date|range>/{comparison.csv, report.md} (+ --json).

Usage :
  python tools/backtest_vs_live.py                       # jour de trading courant
  python tools/backtest_vs_live.py --date 2026-06-16
  python tools/backtest_vs_live.py --start 2026-06-10 --end 2026-06-16
  python tools/backtest_vs_live.py --tickers MES1,NQ1 --strategies fib_v4,ib_retest
  python tools/backtest_vs_live.py --data-source csv     # backtest historique (CSV)
  python tools/backtest_vs_live.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd
from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from config import LIVE_STATE_FILE, PROJECTX_SYMBOLS
from core import bt_engine
from core.data import build_timeframes, load_csv
from core.registry import load_strategy
from tools._live_portfolio import live_portfolio_specs

OUT_ROOT = Path("output/backtest_vs_live")
EVENTS_LOG = Path("logs/trading_events.log")
WARMUP_DAYS = 12  # historique avant la période pour ATR/ADX/EMA/pivots (signaux fidèles)

# Préfixe de customTag → clé stratégie (state.placed_tags[*]["strategy"]).
_TAG_PREFIX_TO_KEY = {
    "FIBV4": "FIB",
    "FIBFINE": "FIB_FINE",
    "IBRETEST": "IB_RETEST",
    "BOSFVG": "BOS_FVG",
    "BOS": "BOS_FVG",
    "OPRNQ1": "OPR_NQ1",  # doit précéder "OPR" (préfixe plus spécifique)
    "OPR": "OPR",
}


# ══════════════════════════════════════════════════════════════════════════════
# Broker / données
# ══════════════════════════════════════════════════════════════════════════════


def build_client():
    from broker.projectx_client import ProjectXClient

    user = os.environ.get("PROJECTX_USERNAME")
    key = os.environ.get("PROJECTX_API_KEY")
    if not user or not key:
        raise SystemExit("PROJECTX_USERNAME / PROJECTX_API_KEY manquants (.env).")
    c = ProjectXClient(username=user, api_key=key)
    if not c.login():
        raise SystemExit("Authentification ProjectX échouée.")
    return c


def resolve_contract(client, ticker: str, state: dict) -> str | None:
    """Contrat front : d'abord state.contracts (ce que le daemon utilise), sinon search."""
    cid = (state.get("contracts") or {}).get(ticker)
    if cid:
        return cid
    symbol = PROJECTX_SYMBOLS.get(ticker)
    if not symbol:
        return None
    contract = client.search_contract(symbol)
    return contract.get("id") if contract else None


def bars_to_df(bars: list[dict]) -> pd.DataFrame:
    """Barres broker [{t,o,h,l,c,v}] → DataFrame OHLCV index datetime UTC-naïf
    (même schéma que core.data.load_csv)."""
    if not bars:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(bars)
    df["datetime"] = pd.to_datetime(df["t"], utc=True).dt.tz_localize(None)
    df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    df = df.drop_duplicates(subset=["datetime"]).sort_values("datetime").set_index("datetime")
    return df[["open", "high", "low", "close", "volume"]]


def fetch_history(client, contract_id: str, start_dt: datetime, end_dt: datetime, unit_number: int):
    """Fetch broker chunké (~5 jours/appel pour rester sous les limites API), concat+dedup."""
    out: list[dict] = []
    cur = start_dt
    step = timedelta(days=5)
    while cur < end_dt:
        chunk_end = min(cur + step, end_dt)
        try:
            bars = client.get_bars(
                contract_id,
                cur,
                chunk_end,
                unit=2,
                unit_number=unit_number,
                limit=20000,
                live=False,
            )
        except Exception as exc:
            print(f"  [!] get_bars {contract_id} {cur:%Y-%m-%d} échoué : {exc}")
            bars = []
        if bars:
            out.extend(bars)
        cur = chunk_end
    return bars_to_df(out)


# ══════════════════════════════════════════════════════════════════════════════
# Backtest des stratégies live
# ══════════════════════════════════════════════════════════════════════════════


def run_backtests(specs, tickers_filter, day_start, day_end, data_source, client, state, label):
    """Backtest chaque (stratégie, ticker) live sur [période - warmup, période].

    Stratégie portée M1 (expose `emit_signals`) → moteur backtesting.py sur les barres **M1**
    du broker (vérité de fill) + **HTML viz** dans `output/calibration/<label>/`. Sinon repli
    M15/M5 via run_backtest (legacy, le temps que la strat soit portée).
    Retourne un DataFrame unifié filtré à la période."""
    fetch_start = day_start - timedelta(days=WARMUP_DAYS)
    fetch_end = day_end + timedelta(days=1)
    d0, d1 = day_start.strftime("%Y-%m-%d"), day_end.strftime("%Y-%m-%d")
    cal_dir = Path("output/calibration") / label
    rows = []
    for name, spec in specs.items():
        try:
            module = load_strategy(name)
        except Exception as exc:
            print(f"  [!] load_strategy({name}) échoué : {exc}")
            continue
        is_m1 = bt_engine.supports(module)
        tf_suffix = getattr(module, "CSV_TIMEFRAME", "m15")
        unit_number = 5 if tf_suffix == "m5" else 15
        skey = spec["strategy_key"]
        sid = getattr(module, "STRATEGY_ID", name)
        for ticker in spec["tickers"]:
            if tickers_filter and ticker not in tickers_filter:
                continue
            html_path = None
            # ── 1. Backtest M1 (moteur backtesting.py) si la strat est portée ──
            if is_m1 and data_source != "csv":
                cid = resolve_contract(client, ticker, state)
                if not cid:
                    print(f"  [!] contrat introuvable pour {ticker} — skip {name}")
                    continue
                m1 = fetch_history(client, cid, fetch_start, fetch_end, 1)
                if m1.empty:
                    print(f"  [!] {name}/{ticker} : aucune barre M1 — skip")
                    continue
                cal_dir.mkdir(parents=True, exist_ok=True)
                html_out = cal_dir / f"{sid}__{ticker}.html"
                try:
                    res = bt_engine.run(
                        module,
                        ticker,
                        params=spec["params"],
                        m1=m1,
                        start=d0,
                        end=d1,
                        html=str(html_out),
                        plot_resample=False,
                    )
                except Exception as exc:
                    print(f"  [!] bt_engine.run {name}/{ticker} échoué : {exc}")
                    continue
                trades, html_path = res["trades"], res.get("html_path")
            # ── 2. Legacy M15/M5 (strat non encore portée) ──
            else:
                if data_source == "csv":
                    csv_path = Path("data") / f"{ticker}_data_{tf_suffix}.csv"
                    if not csv_path.exists():
                        print(f"  [!] {csv_path} introuvable — skip {name}/{ticker}")
                        continue
                    df = load_csv(str(csv_path))
                else:
                    cid = resolve_contract(client, ticker, state)
                    if not cid:
                        print(f"  [!] contrat introuvable pour {ticker} — skip {name}")
                        continue
                    df = fetch_history(client, cid, fetch_start, fetch_end, unit_number)
                if df.empty:
                    print(f"  [!] {name}/{ticker} : aucune barre — skip")
                    continue
                tf = build_timeframes(df) if tf_suffix == "m15" else None
                try:
                    trades = module.run_backtest(
                        df, ticker, tf=tf, params=spec["params"], topstep_guard=False
                    )
                except Exception as exc:
                    print(f"  [!] run_backtest {name}/{ticker} échoué : {exc}")
                    continue
            # ── 3. Normalisation + filtre période (commun) ──
            if trades is None or len(trades) == 0:
                continue
            if "dir" not in trades.columns and "direction" in trades.columns:
                trades = trades.rename(columns={"direction": "dir"})
            if "result" not in trades.columns or "dir" not in trades.columns:
                print(f"  [!] {name}/{ticker} : schéma inattendu {list(trades.columns)[:6]} — skip")
                continue
            trades = trades[trades["result"] != "NOT_FILLED"].copy()
            trades = trades[(trades["date"].astype(str) >= d0) & (trades["date"].astype(str) <= d1)]
            for _, t in trades.iterrows():
                rows.append(
                    {
                        "strategy": name,
                        "strategy_key": skey,
                        "ticker": ticker,
                        "date": str(t["date"]),
                        "dir": t["dir"],
                        "entry": float(t["entry"]),
                        "sl": float(t["sl"]),
                        "tp": float(t["tp"]),
                        "n_ct": int(t["n_ct"]),
                        "result": t["result"],
                        "pnl": float(t["pnl"]),
                        "fill_time": t.get("fill_time"),
                    }
                )
            src = "M1" if (is_m1 and data_source != "csv") else tf_suffix
            extra = f"  → {html_path}" if html_path else ""
            print(f"  ✓ {name:<10} {ticker:<5} {len(trades):>2} trade(s) [{src}]{extra}")
    return pd.DataFrame(rows)


# ══════════════════════════════════════════════════════════════════════════════
# Vérité live
# ══════════════════════════════════════════════════════════════════════════════


def live_trades_from_state(state: dict, day_start, day_end) -> pd.DataFrame:
    """Trades live depuis state.placed_tags filtrés sur la période (par placed_at/fill_time)."""
    d0, d1 = day_start.strftime("%Y-%m-%d"), day_end.strftime("%Y-%m-%d")
    rows = []
    for tag, info in (state.get("placed_tags") or {}).items():
        day = (info.get("placed_at") or info.get("fill_time") or "")[:10]
        if not (d0 <= day <= d1):
            continue
        rows.append(
            {
                "tag": tag,
                "strategy_key": info.get("strategy"),
                "ticker": info.get("ticker"),
                "date": day,
                "dir": info.get("direction"),
                "entry": _f(info.get("entry")),
                "sl": _f(info.get("sl")),
                "tp": _f(info.get("tp")),
                "n_ct": info.get("n_ct"),
                "status": info.get("status"),
                "pnl": _f(info.get("close_pnl")),
                "order_id": info.get("order_id"),
            }
        )
    return pd.DataFrame(rows)


def parse_events(day_start, day_end) -> dict[str, list[dict]]:
    """Parse trading_events.log → {BLOQUÉ:[...], ANNULÉ:[...], ERROR:[...]} sur la période.
    Chaque entrée : {date, ticker, strategy_key, dir, tag, reason}."""
    out = {"BLOQUÉ": [], "ANNULÉ": [], "ERROR": [], "NOT_FILLED": []}
    if not EVENTS_LOG.exists():
        return out
    d0, d1 = day_start.strftime("%Y-%m-%d"), day_end.strftime("%Y-%m-%d")
    line_re = re.compile(r"^(\d{4}-\d{2}-\d{2}) [\d:]+ UTC\s+\[(\w+)\s*\]\s+(.*)$")
    raison_re = re.compile(r"raison=(.+)$")
    for raw in EVENTS_LOG.read_text(encoding="utf-8", errors="replace").splitlines():
        m = line_re.match(raw)
        if not m:
            continue
        day, etype, rest = m.group(1), m.group(2), m.group(3)
        if not (d0 <= day <= d1) or etype not in out:
            continue
        tag = _first_tag(rest)
        reason_m = raison_re.search(rest)
        out[etype].append(
            {
                "date": day,
                "tag": tag,
                **_tag_parts(tag),
                "reason": reason_m.group(1).strip() if reason_m else rest.strip()[:120],
            }
        )
    return out


def broker_realized_pnl(client, account_id: int, day_start, day_end):
    """(total, by_order_id) du P&L réalisé broker sur la période — vérité terrain."""
    start = day_start.replace(tzinfo=UTC)
    end = (day_end + timedelta(days=1)).replace(tzinfo=UTC)
    total, by_oid = 0.0, {}
    try:
        trades = client.get_trades_since(account_id, start)
    except Exception as exc:
        print(f"  [!] get_trades_since échoué : {exc}")
        return 0.0, {}
    for t in trades:
        ts = t.get("creationTimestamp", "")
        try:
            tdt = pd.to_datetime(ts, utc=True)
        except Exception:
            continue
        if not (start <= tdt < end):
            continue
        pnl = t.get("profitAndLoss")
        if pnl is None:
            continue
        total += float(pnl)
        by_oid[int(t.get("orderId", 0))] = by_oid.get(int(t.get("orderId", 0)), 0.0) + float(pnl)
    return round(total, 2), by_oid


# ══════════════════════════════════════════════════════════════════════════════
# Réconciliation
# ══════════════════════════════════════════════════════════════════════════════


def reconcile(bt: pd.DataFrame, live: pd.DataFrame, events: dict, tickers_filter) -> pd.DataFrame:
    """Appariement backtest ↔ live par (strategy_key, ticker, date, dir), puis entry ±1 tick."""
    rows = []
    keys = set()

    def key_of(r):
        return (r["strategy_key"], r["ticker"], r["date"], r["dir"])

    bt_by, live_by = {}, {}
    if not bt.empty:
        for _, r in bt.iterrows():
            bt_by.setdefault(key_of(r), []).append(r)
            keys.add(key_of(r))
    if not live.empty:
        for _, r in live.iterrows():
            if tickers_filter and r["ticker"] not in tickers_filter:
                continue
            live_by.setdefault(key_of(r), []).append(r)
            keys.add(key_of(r))

    # index des signaux bloqués / annulés par (strategy_key, ticker, date, dir)
    block_by = {}
    for etype in ("BLOQUÉ", "ANNULÉ", "ERROR"):
        for e in events.get(etype, []):
            k = (e.get("strategy_key"), e.get("ticker"), e.get("date"), e.get("dir"))
            block_by.setdefault(k, []).append((etype, e.get("reason")))

    for k in sorted(keys, key=lambda x: (x[2], x[0], x[1], x[3])):
        skey, ticker, date, dirn = k
        bts = list(bt_by.get(k, []))
        lvs = list(live_by.get(k, []))
        used: set[int] = set()
        # appariement par identité (même strat/ticker/jour/sens) → entry le plus proche,
        # tolérance relative 0.3 % (data/timing broker vs live ⇒ entry à quelques pts près).
        for b in bts:
            best_j, best_d = None, None
            for j, lv in enumerate(lvs):
                if j in used or lv["entry"] is None:
                    continue
                d = abs(float(lv["entry"]) - float(b["entry"]))
                if best_d is None or d < best_d:
                    best_d, best_j = d, j
            if best_j is not None and best_d <= 0.003 * float(b["entry"]):
                used.add(best_j)
                lv = lvs[best_j]
                if _live_filled(lv):
                    rows.append(_row("MATCH", skey, ticker, date, dirn, b, lv, ""))
                else:
                    br = _block_reason(block_by.get(k)) or f"live {lv.get('status')} (non rempli)"
                    rows.append(
                        _row(
                            "DIVERGENCE",
                            skey,
                            ticker,
                            date,
                            dirn,
                            b,
                            lv,
                            f"bt aurait tradé ({b['result']} ${float(b['pnl']):+,.0f}) — {br}",
                        )
                    )
            else:
                rows.append(
                    _row(
                        "BACKTEST_ONLY",
                        skey,
                        ticker,
                        date,
                        dirn,
                        b,
                        None,
                        _explain_bt_only(block_by.get(k)),
                    )
                )
        for j, lv in enumerate(lvs):  # live sans backtest correspondant
            if j in used:
                continue
            rows.append(
                _row("LIVE_ONLY", skey, ticker, date, dirn, None, lv, _live_only_reason(lv))
            )
    return pd.DataFrame(rows)


def _live_filled(lv) -> bool:
    if not _isna(lv.get("pnl")):  # NaN (close_pnl None) ⇒ non rempli, pas "is not None"
        return True
    return lv.get("status") in ("CLOSED", "FILLED", "ACTIVE")


def _block_reason(blocks) -> str | None:
    if blocks:
        etype, reason = blocks[0]
        return f"{etype.lower()} : {reason}"
    return None


def _explain_bt_only(blocks) -> str:
    if blocks:
        etype, reason = blocks[0]
        return f"{etype.lower()} live : {reason}"
    return "aucune trace live (signal non généré : data/timing/look-ahead ou daemon down)"


def _live_only_reason(lv) -> str:
    st = lv.get("status")
    if st in ("CANCELLED", "PENDING"):
        return f"live {st} (pas de fill ; pas de trade backtest correspondant)"
    return "trade live sans signal backtest (écart de fidélité / ordre manuel / état périmé)"


# ══════════════════════════════════════════════════════════════════════════════
# Rendu
# ══════════════════════════════════════════════════════════════════════════════


def build_report(comp, bt, live, events, broker_total, label) -> str:
    L = [f"# Backtest vs Live — {label}", ""]

    def _cnt(c):
        return int((comp["category"] == c).sum()) if not comp.empty else 0

    L += [
        f"- **{_cnt('MATCH')} MATCH** · **{_cnt('DIVERGENCE')} divergence** · "
        f"**{_cnt('BACKTEST_ONLY')} backtest-only** · **{_cnt('LIVE_ONLY')} live-only**",
        f"- P&L backtest (apparié + bt-only) : **${(bt['pnl'].sum() if not bt.empty else 0):+,.0f}**",
        f"- P&L live (state.close_pnl) : **${(live['pnl'].dropna().sum() if not live.empty else 0):+,.0f}**",
        f"- **P&L réalisé broker (ancre) : ${broker_total:+,.0f}**",
        "",
        "## Par stratégie / ticker",
        "",
        "| Stratégie | Ticker | bt trades | bt P&L | live trades | live P&L |",
        "|---|---|---|---|---|---|",
    ]
    skeys = sorted(
        set(list(bt["strategy_key"]) if not bt.empty else [])
        | set(list(live["strategy_key"].dropna()) if not live.empty else [])
    )
    observed_tk = sorted(
        set(list(bt["ticker"]) if not bt.empty else [])
        | set(list(live["ticker"].dropna()) if not live.empty else [])
    )
    for sk in skeys:
        for tk in observed_tk:
            b = bt[(bt["strategy_key"] == sk) & (bt["ticker"] == tk)] if not bt.empty else bt
            lv = (
                live[(live["strategy_key"] == sk) & (live["ticker"] == tk)]
                if not live.empty
                else live
            )
            if (b.empty if hasattr(b, "empty") else True) and (
                lv.empty if hasattr(lv, "empty") else True
            ):
                continue
            L.append(
                f"| {sk} | {tk} | {len(b)} | ${(b['pnl'].sum() if len(b) else 0):+,.0f} "
                f"| {len(lv)} | ${(lv['pnl'].dropna().sum() if len(lv) else 0):+,.0f} |"
            )
    # Divergences
    if not comp.empty:
        div = comp[comp["category"] != "MATCH"]
        if not div.empty:
            L += [
                "",
                "## Divergences (à comprendre)",
                "",
                "| Cat | Strat | Tk | Jour | Sens | entry bt | entry live | pnl bt | pnl live | Raison |",
                "|---|---|---|---|---|---|---|---|---|---|",
            ]
            for _, r in div.iterrows():
                L.append(
                    f"| {r['category']} | {r['strategy_key']} | {r['ticker']} | {r['date']} | {r['dir']} "
                    f"| {_p(r['entry_bt'])} | {_p(r['entry_live'])} | {_s(r['pnl_bt'])} | {_s(r['pnl_live'])} | {r['reason']} |"
                )
    # MATCH avec Δpnl notable
    if not comp.empty:
        mt = comp[comp["category"] == "MATCH"].copy()
        if not mt.empty:
            L += [
                "",
                "## Trades appariés",
                "",
                "| Strat | Tk | Jour | Sens | entry | result bt | pnl bt | pnl live | Δ |",
                "|---|---|---|---|---|---|---|---|---|",
            ]
            for _, r in mt.iterrows():
                delta = (
                    (r["pnl_live"] - r["pnl_bt"])
                    if (r["pnl_live"] is not None and r["pnl_bt"] is not None)
                    else None
                )
                L.append(
                    f"| {r['strategy_key']} | {r['ticker']} | {r['date']} | {r['dir']} | {_p(r['entry_bt'])} "
                    f"| {r['result_bt']} | {_s(r['pnl_bt'])} | {_s(r['pnl_live'])} | {_s(delta)} |"
                )
    L += [
        "",
        "## Notes de fidélité",
        "- Backtest `topstep_guard=False` : le RM (caps, consec-loss, daily-stop) n'est PAS rejoué → un signal backtest peut être `BACKTEST_ONLY` car **bloqué RM** en live (voir Raison).",
        "- `pnl` backtest = NET (slippage + commissions). Ancre vérité = **P&L réalisé broker**.",
        "- Fill intra-bar SL-prioritaire ; fib-v4.1/fib-fine = filtre causal (sans look-ahead).",
        "- Données = fetch broker (barres du jour). Rollover de contrat : v1 = contrat front courant.",
    ]
    return "\n".join(L)


# ── petits helpers ──
def _f(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _isna(v) -> bool:
    if v is None:
        return True
    try:
        return bool(pd.isna(v))
    except (TypeError, ValueError):
        return False


def _s(v):  # P&L signé
    if _isna(v):
        return ""
    return f"{v:+,.0f}" if isinstance(v, (int, float)) else str(v)


def _p(v):  # prix (plain, décimales utiles)
    if _isna(v):
        return ""
    if isinstance(v, (int, float)):
        return f"{v:,.2f}".rstrip("0").rstrip(".")
    return str(v)


def _first_tag(text: str) -> str | None:
    m = re.search(r"\b([A-Z]+_[A-Z0-9]+_\d{8}_[a-z]+_[-\d.]+_[-\d.]+)", text)
    if m:
        return m.group(1)
    m = re.search(r"\b([A-Z][A-Z0-9_]*_[A-Z0-9]+_\d{8}_[a-z]+)", text)
    return m.group(1) if m else None


def _tag_parts(tag: str | None) -> dict:
    if not tag:
        return {"strategy_key": None, "ticker": None, "dir": None}
    parts = tag.split("_")
    prefix = parts[0]
    skey = _TAG_PREFIX_TO_KEY.get(prefix, prefix)
    ticker = parts[1] if len(parts) > 1 else None
    dirn = None
    for p in parts:
        if p in ("long", "short"):
            dirn = p
            break
    return {"strategy_key": skey, "ticker": ticker, "dir": dirn}


def _row(cat, skey, ticker, date, dirn, b, lv, reason):
    return {
        "category": cat,
        "strategy_key": skey,
        "ticker": ticker,
        "date": date,
        "dir": dirn,
        "entry_bt": (float(b["entry"]) if b is not None else None),
        "sl_bt": (float(b["sl"]) if b is not None else None),
        "tp_bt": (float(b["tp"]) if b is not None else None),
        "n_ct_bt": (int(b["n_ct"]) if b is not None else None),
        "result_bt": (b["result"] if b is not None else None),
        "pnl_bt": (round(float(b["pnl"]), 2) if b is not None else None),
        "entry_live": (lv["entry"] if lv is not None else None),
        "status_live": (lv["status"] if lv is not None else None),
        "n_ct_live": (lv["n_ct"] if lv is not None else None),
        "pnl_live": (lv["pnl"] if lv is not None else None),
        "tag_live": (lv["tag"] if lv is not None else None),
        "reason": reason,
    }


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════


def _trading_day_now() -> str:
    """Jour de trading futures courant (ancre 23:00 UTC)."""
    now = datetime.now(UTC)
    anchor = now.replace(hour=23, minute=0, second=0, microsecond=0)
    day = now if now >= anchor else now - timedelta(days=1)
    return day.strftime("%Y-%m-%d")


def main():
    ap = argparse.ArgumentParser(description="Comparaison backtest vs live par jour/période")
    ap.add_argument("--date", help="Jour unique YYYY-MM-DD (défaut : jour de trading courant)")
    ap.add_argument("--start", help="Début de période YYYY-MM-DD")
    ap.add_argument("--end", help="Fin de période YYYY-MM-DD")
    ap.add_argument("--tickers", help="Filtre tickers (CSV, ex. MES1,NQ1)")
    ap.add_argument(
        "--strategies", help="Filtre stratégies (CSV des modules, ex. fib_v4,ib_retest)"
    )
    ap.add_argument("--data-source", choices=["broker", "csv"], default="broker")
    ap.add_argument(
        "--include-disabled", action="store_true", help="Inclure les stratégies inertes (bos-fvg)"
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.start or args.end:
        d0 = args.start or args.end
        d1 = args.end or args.start
    else:
        d0 = d1 = args.date or _trading_day_now()
    day_start = datetime.strptime(d0, "%Y-%m-%d")
    day_end = datetime.strptime(d1, "%Y-%m-%d")
    label = d0 if d0 == d1 else f"{d0}_{d1}"
    tickers_filter = set(args.tickers.split(",")) if args.tickers else None

    specs = live_portfolio_specs(include_disabled=args.include_disabled)
    if args.strategies:
        wanted = set(args.strategies.split(","))
        specs = {k: v for k, v in specs.items() if k in wanted}

    state = json.loads(Path(LIVE_STATE_FILE).read_text()) if Path(LIVE_STATE_FILE).exists() else {}
    account_id = int(state.get("account_id", 0))

    client = None
    if args.data_source == "broker":
        client = build_client()

    print(
        f"\n{'=' * 66}\n  BACKTEST vs LIVE — {label}  (source données : {args.data_source})\n{'=' * 66}"
    )
    print("Backtest des stratégies live…")
    bt = run_backtests(
        specs, tickers_filter, day_start, day_end, args.data_source, client, state, label
    )

    print("Extraction de la vérité live…")
    live = live_trades_from_state(state, day_start, day_end)
    if tickers_filter and not live.empty:
        live = live[live["ticker"].isin(tickers_filter)]
    events = parse_events(day_start, day_end)
    broker_total, _ = (
        broker_realized_pnl(client, account_id, day_start, day_end) if client else (0.0, {})
    )

    comp = reconcile(bt, live, events, tickers_filter)
    report = build_report(comp, bt, live, events, broker_total, label)

    out_dir = OUT_ROOT / label
    out_dir.mkdir(parents=True, exist_ok=True)
    (comp if not comp.empty else pd.DataFrame()).to_csv(out_dir / "comparison.csv", index=False)
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    if args.json:
        payload = {
            "label": label,
            "broker_realized_pnl": broker_total,
            "counts": (
                {
                    c: int((comp["category"] == c).sum())
                    for c in ("MATCH", "DIVERGENCE", "BACKTEST_ONLY", "LIVE_ONLY")
                }
                if not comp.empty
                else {}
            ),
            "comparison": comp.to_dict(orient="records") if not comp.empty else [],
        }
        (out_dir / "comparison.json").write_text(
            json.dumps(payload, indent=2, default=str), encoding="utf-8"
        )

    print()
    print(report)
    print(f"\n✓ {out_dir}/comparison.csv + report.md")


if __name__ == "__main__":
    main()
