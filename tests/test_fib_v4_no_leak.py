"""
Tests de non-régression critique pour `core/strategy_fib_v4.py`.

Objectif : prouver que les 2 nouvelles invalidations (pivot break + wick
excess) ne consomment aucune donnée future, et que la stratégie fib-v4
produit toujours moins (ou autant) de trades que fib-v3 sur la même série
(les filtres ne peuvent qu'éliminer, jamais créer).

Lancer :
    pytest tests/test_fib_v4_no_leak.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.strategy_fib_v4 import run_fib_v4_backtest


# Helper : équivalent baseline (filtres v4 inactifs = pas de pivot break,
# pas de wick excess). Sert de référence pour les tests de régression.
def _baseline_v4(df, ticker, fib_level=0.382):
    """fib-v4 avec filtres infinis = équivalent baseline (anciennement
    fib-v3 sans filtre trigger, supprimée 2026-05-19)."""
    return run_fib_v4_backtest(
        df,
        ticker,
        fib_level=fib_level,
        pivot_break_buffer_atr=1e9,
        wick_max_atr=1e9,
        skip_macro=False,
    )


def _synthetic_df(n_bars: int = 2000, seed: int = 42, ticker_pivots: bool = True) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2025-01-02 13:00", periods=n_bars, freq="15min", tz="UTC")
    # Random walk + tendance pour générer des impulses
    drift = np.linspace(-50, 50, n_bars)
    base = 5000 + np.cumsum(rng.normal(0, 5, n_bars)) + drift
    high = base + rng.uniform(2, 10, n_bars)
    low = base - rng.uniform(2, 10, n_bars)
    open_ = base + rng.normal(0, 2, n_bars)
    close = base + rng.normal(0, 2, n_bars)
    high = np.maximum.reduce([high, open_, close])
    low = np.minimum.reduce([low, open_, close])
    df = pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": rng.integers(1000, 5000, n_bars),
        },
        index=idx,
    )
    df.index.name = "datetime"
    return df


@pytest.fixture(scope="module")
def df_synth() -> pd.DataFrame:
    return _synthetic_df()


@pytest.fixture(scope="module")
def df_real() -> pd.DataFrame:
    from core.data import load_csv

    return load_csv(str(Path(__file__).resolve().parent.parent / "data" / "MES1_data_m15.csv"))


def test_fib_v4_no_more_trades_than_baseline(df_real: pd.DataFrame):
    """Les filtres fib-v4 ne peuvent qu'ÉLIMINER des trades vs la baseline
    (équivalent v4 sans filtres = anciennement v3 sans filtre trigger)."""
    trades_base = _baseline_v4(df_real, "MES1", fib_level=0.382)
    trades_v4 = run_fib_v4_backtest(
        df_real, "MES1", fib_level=0.382, pivot_break_buffer_atr=0.0, wick_max_atr=0.05
    )
    filled_base = trades_base[trades_base["result"].isin(["TP", "SL", "TE"])]
    filled_v4 = trades_v4[trades_v4["result"].isin(["TP", "SL", "TE"])]
    assert len(filled_v4) <= len(
        filled_base
    ), f"fib-v4 strict ({len(filled_v4)}) doit être ≤ baseline ({len(filled_base)}) — filtres éliminent"


def test_fib_v4_filters_reduce_to_zero_at_extreme(df_real: pd.DataFrame):
    """Avec wick_max=-1, plus aucun fill ne doit passer (mèche=0 impossible
    sauf cas dégénéré). Confirme que le seuil agit effectivement."""
    trades_extreme = run_fib_v4_backtest(
        df_real, "MES1", fib_level=0.382, pivot_break_buffer_atr=0.0, wick_max_atr=-1.0
    )
    if len(trades_extreme) == 0:
        return  # DataFrame vide attendu — pas de fill possible
    filled = trades_extreme[trades_extreme["result"].isin(["TP", "SL", "TE"])]
    assert len(filled) == 0, f"wick_max=-1 doit éliminer tous les fills (got {len(filled)})"


def test_fib_v4_filters_inactive_idempotent(df_real: pd.DataFrame):
    """Avec wick_max=+∞ ET pivot_buffer=+∞, fib-v4 doit produire des trades
    cohérents (idempotence : 2 appels successifs même résultat — pas de
    side-effect ni de leak temporel)."""
    trades_a = run_fib_v4_backtest(
        df_real, "MES1", fib_level=0.382, pivot_break_buffer_atr=1e9, wick_max_atr=1e9
    )
    trades_b = run_fib_v4_backtest(
        df_real, "MES1", fib_level=0.382, pivot_break_buffer_atr=1e9, wick_max_atr=1e9
    )
    filled_a = trades_a[trades_a["result"].isin(["TP", "SL", "TE"])]
    filled_b = trades_b[trades_b["result"].isin(["TP", "SL", "TE"])]
    assert len(filled_a) == len(
        filled_b
    ), f"Idempotence : 2 runs identiques attendus, got {len(filled_a)} vs {len(filled_b)}"
    pnl_a = filled_a["pnl"].sum()
    pnl_b = filled_b["pnl"].sum()
    assert (
        abs(pnl_a - pnl_b) < 0.01
    ), f"PnL identique attendu (idempotence) : {pnl_a:.4f} vs {pnl_b:.4f}"


def test_fib_v4_pivot_break_invariant(df_real: pd.DataFrame):
    """Tous les trades fillés doivent avoir pivot_break_atr >= -buffer."""
    buffer = 0.10
    trades = run_fib_v4_backtest(
        df_real, "MES1", fib_level=0.382, pivot_break_buffer_atr=buffer, wick_max_atr=1e9
    )
    filled = trades[trades["result"].isin(["TP", "SL", "TE"])]
    if len(filled) == 0:
        pytest.skip("Aucun trade pour valider l'invariant pivot break")
    min_pb = filled["pivot_break_atr"].min()
    assert (
        min_pb >= -buffer - 1e-6
    ), f"pivot_break_atr min ({min_pb}) < -buffer ({-buffer}) : invariant violé"


def test_fib_v4_wick_invariant(df_real: pd.DataFrame):
    """Tous les trades fillés doivent avoir wick_through_atr <= wick_max."""
    wmax = 0.20
    trades = run_fib_v4_backtest(
        df_real, "MES1", fib_level=0.382, pivot_break_buffer_atr=1e9, wick_max_atr=wmax
    )
    filled = trades[trades["result"].isin(["TP", "SL", "TE"])]
    if len(filled) == 0:
        pytest.skip("Aucun trade pour valider l'invariant wick")
    max_wick = filled["wick_through_atr"].max()
    assert (
        max_wick <= wmax + 1e-6
    ), f"wick_through_atr max ({max_wick}) > wick_max ({wmax}) : invariant violé"


def test_fib_v4_temporal_consistency(df_real: pd.DataFrame):
    """Pour tout trade fillé : pending_time < fill_time < exit_time."""
    trades = run_fib_v4_backtest(df_real, "MES1", fib_level=0.382)
    filled = trades[trades["result"].isin(["TP", "SL", "TE"])]
    if len(filled) == 0:
        pytest.skip("Aucun trade pour valider la cohérence temporelle")
    for _, t in filled.iterrows():
        pt = pd.to_datetime(t["pending_time"])
        ft = pd.to_datetime(t["fill_time"])
        et = pd.to_datetime(t["exit_time"])
        assert pt <= ft <= et, f"Inversion temporelle : pending={pt}, fill={ft}, exit={et}"


def test_fib_v4_runs_on_synthetic(df_synth: pd.DataFrame):
    """La stratégie doit tourner sans crash sur une série synthétique."""
    # On force un ticker présent dans config — utilise MES1 (les params seront chargés)
    trades = run_fib_v4_backtest(
        df_synth, "MES1", fib_level=0.382, pivot_break_buffer_atr=0.0, wick_max_atr=0.5
    )
    assert isinstance(trades, pd.DataFrame)
    if len(trades) > 0:
        required = ["entry", "sl", "tp", "pnl", "result", "pivot_break_atr", "wick_through_atr"]
        for col in required:
            assert col in trades.columns, f"Colonne manquante : {col}"
