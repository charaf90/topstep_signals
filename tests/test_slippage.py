"""Tests pour core/slippage — modèle de slippage stochastique réaliste.

Vérifie en priorité que le flag OFF (défaut) ne modifie rien : garant du
golden master. Ensuite, propriétés du modèle quand flag ON.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from core.slippage import (
    apply_slippage_to_trades,
    base_slippage_usd,
    stochastic_slippage_usd,
)


def _make_trades(n: int = 10, ticker: str = "MES1") -> pd.DataFrame:
    """DataFrame minimal compatible avec le schéma standard."""
    rng = np.random.default_rng(42)
    return pd.DataFrame(
        {
            "date": [f"2026-05-{d:02d}" for d in range(1, n + 1)],
            "n_ct": [2] * n,
            "result": ["TP"] * n,
            "pnl": rng.uniform(-100, 200, size=n).round(2),
            "atr_pts": rng.uniform(5.0, 20.0, size=n).round(2),
        }
    )


# ──────────────────────────────────────────────────────────────────────────────
# base_slippage_usd — déterministe
# ──────────────────────────────────────────────────────────────────────────────


class TestBaseSlippage:

    def test_mes1_base(self):
        """MES1 : 1 tick × 0.25 pts × 5 $/pt × 2 contrats × 2 (aller-retour) = -5.0 $."""
        result = base_slippage_usd("MES1", n_ct=2)
        # tick_size=0.25, dpp=5, tick_value=1.25, base=1 tick
        # base = -2 × 1 × 1.25 × 2 = -5.0
        assert result == -5.0

    def test_nq1_base(self):
        """NQ1 : 2 ticks × 0.25 × 2 $/pt × 1 × 2 = -2.0 $."""
        result = base_slippage_usd("NQ1", n_ct=1)
        assert result == -2.0

    def test_negatif(self):
        """Toujours négatif (perte pour le trader)."""
        for ticker in ("MES1", "NQ1", "YM1", "MGC1"):
            assert base_slippage_usd(ticker, n_ct=1) < 0

    def test_proportionnel_n_ct(self):
        """Slippage proportionnel au nombre de contrats."""
        s1 = base_slippage_usd("MES1", n_ct=1)
        s5 = base_slippage_usd("MES1", n_ct=5)
        assert s5 == 5 * s1

    def test_ticker_inconnu(self):
        """Ticker inconnu → 0 (pas de slippage par défaut)."""
        assert base_slippage_usd("INCONNU", n_ct=1) == 0.0


# ──────────────────────────────────────────────────────────────────────────────
# stochastic_slippage_usd
# ──────────────────────────────────────────────────────────────────────────────


class TestStochasticSlippage:

    def test_atr_nul_egal_base(self):
        """Sans info ATR (atr_pts=0 ou median=0) → slippage déterministe."""
        rng = np.random.default_rng(42)
        s = stochastic_slippage_usd("MES1", n_ct=2, atr_pts=0, atr_median_pts=10, rng=rng)
        assert s == base_slippage_usd("MES1", n_ct=2)

    def test_atr_egal_median_egal_base(self):
        """ATR actuel = ATR médian → atr_ratio = 0 → slippage déterministe."""
        rng = np.random.default_rng(42)
        s = stochastic_slippage_usd("MES1", n_ct=2, atr_pts=10, atr_median_pts=10, rng=rng)
        assert s == base_slippage_usd("MES1", n_ct=2)

    def test_atr_eleve_amplifie(self):
        """ATR 2× médian → slippage amplifié (en valeur absolue)."""
        rng = np.random.default_rng(42)
        base = base_slippage_usd("MES1", n_ct=2)
        s = stochastic_slippage_usd("MES1", n_ct=2, atr_pts=20, atr_median_pts=10, rng=rng)
        # base = -5, s doit être plus négatif (ou égal si tirage exp ≈ 0)
        assert s <= base + 1e-9

    def test_negatif_toujours(self):
        """Le slippage est toujours négatif quel que soit l'ATR."""
        rng = np.random.default_rng(42)
        for atr in (0.0, 5.0, 50.0, 500.0):
            s = stochastic_slippage_usd("MES1", n_ct=1, atr_pts=atr, atr_median_pts=10, rng=rng)
            assert s <= 0


# ──────────────────────────────────────────────────────────────────────────────
# apply_slippage_to_trades — garantie golden master
# ──────────────────────────────────────────────────────────────────────────────


class TestApplySlippageOff:

    def test_flag_off_retourne_objet_identique(self):
        """Flag OFF → même objet retourné (pas même une copie). GOLDEN MASTER."""
        trades = _make_trades()
        result = apply_slippage_to_trades(trades, ticker="MES1", enabled=False)
        # Même identifiant Python → garantit zéro overhead
        assert result is trades

    def test_flag_off_aucune_colonne_ajoutee(self):
        trades = _make_trades()
        result = apply_slippage_to_trades(trades, ticker="MES1", enabled=False)
        assert "pnl_gross" not in result.columns
        assert "slippage_usd" not in result.columns

    def test_flag_off_pnl_inchange(self):
        trades = _make_trades()
        pnl_avant = trades["pnl"].copy()
        apply_slippage_to_trades(trades, ticker="MES1", enabled=False)
        assert (trades["pnl"] == pnl_avant).all()

    def test_default_enabled_via_config(self):
        """Comportement par défaut (sans paramètre enabled) doit utiliser
        config.BACKTEST_REALISTIC_SLIPPAGE qui est False → identique."""
        trades = _make_trades()
        result = apply_slippage_to_trades(trades, ticker="MES1")
        assert result is trades


class TestApplySlippageOn:

    def test_flag_on_modifie_pnl(self):
        trades = _make_trades(n=20)
        result = apply_slippage_to_trades(trades, ticker="MES1", enabled=True, seed=42)
        # Différent objet (copie)
        assert result is not trades
        # Colonnes ajoutées
        assert "pnl_gross" in result.columns
        assert "slippage_usd" in result.columns
        # pnl ≤ pnl_gross (slippage toujours négatif sur les trades fillés)
        assert (result["pnl"] <= result["pnl_gross"] + 1e-9).all()

    def test_slippage_negatif_sur_filled(self):
        trades = _make_trades(n=20)
        result = apply_slippage_to_trades(trades, ticker="MES1", enabled=True, seed=42)
        filled = result["result"] != "NOT_FILLED"
        assert (result.loc[filled, "slippage_usd"] <= 0).all()

    def test_not_filled_pas_de_slippage(self):
        trades = _make_trades(n=10)
        trades.loc[0:4, "result"] = "NOT_FILLED"
        result = apply_slippage_to_trades(trades, ticker="MES1", enabled=True, seed=42)
        # Les 5 premiers (NOT_FILLED) ne doivent pas avoir de slippage
        assert (result.loc[0:4, "slippage_usd"] == 0).all()

    def test_seed_reproductible(self):
        trades = _make_trades(n=20)
        r1 = apply_slippage_to_trades(trades, ticker="MES1", enabled=True, seed=42)
        r2 = apply_slippage_to_trades(trades, ticker="MES1", enabled=True, seed=42)
        assert (r1["pnl"] == r2["pnl"]).all()
        assert (r1["slippage_usd"] == r2["slippage_usd"]).all()

    def test_seed_different_resultats_differents(self):
        trades = _make_trades(n=20)
        r1 = apply_slippage_to_trades(trades, ticker="MES1", enabled=True, seed=42)
        r2 = apply_slippage_to_trades(trades, ticker="MES1", enabled=True, seed=123)
        # Au moins un trade doit différer (sauf si ATR=median partout)
        assert not (r1["pnl"] == r2["pnl"]).all()

    def test_pnl_gross_preserve_original(self):
        trades = _make_trades(n=10)
        original_pnl = trades["pnl"].copy()
        result = apply_slippage_to_trades(trades, ticker="MES1", enabled=True, seed=42)
        # pnl_gross = pnl original
        assert (result["pnl_gross"] == original_pnl).all()
        # Le DataFrame original n'a pas été modifié (copie)
        assert (trades["pnl"] == original_pnl).all()

    def test_df_vide_retourne_vide(self):
        empty = pd.DataFrame(columns=["pnl", "n_ct", "result", "atr_pts"])
        result = apply_slippage_to_trades(empty, ticker="MES1", enabled=True)
        assert len(result) == 0

    def test_df_sans_atr_marche(self):
        """Si la colonne atr_pts est absente, on tombe sur le slippage déterministe."""
        trades = pd.DataFrame(
            {
                "n_ct": [2, 2, 2],
                "result": ["TP", "SL", "TE"],
                "pnl": [100.0, -50.0, 20.0],
            }
        )
        result = apply_slippage_to_trades(trades, ticker="MES1", enabled=True, seed=42)
        # Tous les slippages doivent être identiques au base_slippage_usd
        base = base_slippage_usd("MES1", n_ct=2)
        assert (result["slippage_usd"] == base).all()
