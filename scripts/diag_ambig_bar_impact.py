"""
Diagnostic d'impact — correction du biais « bougie ambiguë TP+SL → SL ».

Compare, par stratégie/ticker, les métriques full-history et OOS avant/après
le fix de résolution intra-bar. À lancer deux fois (une fois sur le code avec
le fix, une fois sur le code sans, via git stash) puis comparer les JSON.

Usage :
    python scripts/diag_ambig_bar_impact.py <tag>   # tag = "new" ou "old"

Réutilise tests._golden_helpers.build_baseline (mêmes données, mêmes params
que le golden master) — donc strictement comparable au baseline.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._golden_helpers import build_baseline  # noqa: E402

STRATEGIES = ["opr", "opr_v5_1", "fib_v4"]
OOS_START = "2025-10-01"


def _pf(pnls: list[float]) -> float:
    gains = sum(p for p in pnls if p > 0)
    losses = -sum(p for p in pnls if p < 0)
    if losses == 0:
        return float("inf") if gains > 0 else 0.0
    return gains / losses


def _metrics(trades: list[dict]) -> dict:
    filled = [t for t in trades if t.get("result") not in (None, "NOT_FILLED")]
    same_bar = [
        t for t in filled if t.get("fill_time") and t.get("fill_time") == t.get("exit_time")
    ]
    results = {}
    for t in filled:
        results[t["result"]] = results.get(t["result"], 0) + 1

    def _slice(ts):
        return [t for t in ts if (t.get("date") or "") >= OOS_START]

    oos = _slice(filled)
    pnls = [t.get("pnl") or 0.0 for t in filled]
    pnls_oos = [t.get("pnl") or 0.0 for t in oos]
    return {
        "n_total": len(trades),
        "n_filled": len(filled),
        "n_same_bar": len(same_bar),
        "same_bar_pct": round(100 * len(same_bar) / len(filled), 1) if filled else 0.0,
        "results": results,
        "pf_full": round(_pf(pnls), 2),
        "pnl_full": round(sum(pnls), 0),
        "n_oos": len(oos),
        "pf_oos": round(_pf(pnls_oos), 2),
        "pnl_oos": round(sum(pnls_oos), 0),
    }


def main(tag: str) -> None:
    out: dict = {}
    rows = []
    for strat in STRATEGIES:
        b = build_baseline(strat)
        out[strat] = {"strategy_id": b["strategy_id"], "tickers": {}}
        for ticker, data in b["tickers"].items():
            if "missing_data" in data:
                continue
            m = _metrics(data["trades"])
            out[strat]["tickers"][ticker] = m
            rows.append(
                {
                    "strat": strat,
                    "ticker": ticker,
                    "n_fill": m["n_filled"],
                    "same_bar%": m["same_bar_pct"],
                    "pf_full": m["pf_full"],
                    "pnl_full": m["pnl_full"],
                    "n_oos": m["n_oos"],
                    "pf_oos": m["pf_oos"],
                    "pnl_oos": m["pnl_oos"],
                    "results": m["results"],
                }
            )

    Path("output").mkdir(exist_ok=True)
    dest = Path("output") / f"diag_ambig_{tag}.json"
    dest.write_text(json.dumps(out, indent=2))
    print(f"\n=== Diagnostic [{tag}] ===")
    print(pd.DataFrame(rows).to_string(index=False))
    print(f"\nÉcrit : {dest}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "new")
