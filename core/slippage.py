"""Modèle de slippage stochastique réaliste pour les backtests.

Add-on optionnel, contrôlé par le feature flag `BACKTEST_REALISTIC_SLIPPAGE`
dans `config.py`. **Flag OFF par défaut** : aucune modification du pipeline
de backtest existant — le golden master reste valide au cent près.

Modèle (simple, calibré sur les tickers Topstep micro) :

    slip_usd(ticker, atr_pts) =
        base_usd × (1 + Exp(λ=ATR_SCALE) × max(0, atr_pts / atr_median - 1))

    base_usd = 2 × SLIPPAGE_TICKS_PER_TICKER[ticker] × tick_value × n_ct
               (entrée + sortie = aller-retour)

    atr_median  = médiane de la colonne atr sur le DataFrame de trades
                  (si absente, on utilise atr_pts comme référence → ratio=1)

Pourquoi pas plus compliqué : la calibration fine demande des fills live
réels par condition de marché. Les logs/trading_events.log fournissent un
échantillon, mais trop limité pour calibrer une distribution conditionnelle.
Le modèle multiplicatif simple suffit pour exhiber la sensibilité PF/PnL
des stratégies au slippage avant de promouvoir en live.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from config import (
    BACKTEST_REALISTIC_SLIPPAGE,
    INSTRUMENTS,
    REALISTIC_SLIPPAGE_ATR_SCALE,
    REALISTIC_SLIPPAGE_SEED,
    SLIPPAGE_TICKS_PER_TICKER,
)


def base_slippage_usd(ticker: str, n_ct: int = 1) -> float:
    """Slippage déterministe aller-retour en USD (signé négatif, perte).

    Calculé à partir de SLIPPAGE_TICKS_PER_TICKER[ticker] × tick_value × 2.
    Sert de référence pour la composante stochastique.
    """
    instr = INSTRUMENTS.get(ticker, {})
    dollar_per_point = float(instr.get("dollar_per_point", 0.0))
    tick_size = float(instr.get("tick_size", 0.0))
    tick_value = dollar_per_point * tick_size
    base_ticks = SLIPPAGE_TICKS_PER_TICKER.get(ticker, 1)
    return -2.0 * base_ticks * tick_value * float(n_ct)


def stochastic_slippage_usd(
    ticker: str,
    n_ct: int,
    atr_pts: float = 0.0,
    atr_median_pts: float = 0.0,
    rng: np.random.Generator | None = None,
) -> float:
    """Slippage stochastique aller-retour (signé négatif).

    Composantes :
      base                 = -2 × ticks × tick_value × n_ct (toujours appliqué)
      extra (stochastique) = base × Exp(λ) × atr_ratio
      atr_ratio            = max(0, atr_pts / atr_median - 1)

    Si atr_median_pts <= 0, atr_ratio = 0 → on retombe sur le slippage déterministe.
    """
    base = base_slippage_usd(ticker, n_ct)  # négatif
    if rng is None:
        rng = np.random.default_rng(REALISTIC_SLIPPAGE_SEED)
    if atr_median_pts <= 0 or atr_pts <= 0:
        return base
    atr_ratio = max(0.0, float(atr_pts) / float(atr_median_pts) - 1.0)
    if atr_ratio == 0:
        return base
    extra_factor = float(rng.exponential(scale=REALISTIC_SLIPPAGE_ATR_SCALE)) * atr_ratio
    # base est négatif ; on amplifie sa valeur absolue de (1 + extra_factor)
    return base * (1.0 + extra_factor)


def apply_slippage_to_trades(
    df_trades: pd.DataFrame,
    ticker: str,
    enabled: bool | None = None,
    seed: int | None = None,
) -> pd.DataFrame:
    """Modifie `pnl` du DataFrame pour inclure le slippage stochastique.

    Args:
        df_trades : DataFrame avec colonnes au minimum {pnl, n_ct, result}.
                    Optionnel : colonne `atr_pts` pour le ratio ATR.
        ticker    : ticker (clé de INSTRUMENTS).
        enabled   : override du flag global (None → utilise
                    config.BACKTEST_REALISTIC_SLIPPAGE).
        seed      : override de REALISTIC_SLIPPAGE_SEED.

    Returns:
        Nouveau DataFrame (copie) avec colonnes :
          - pnl_gross : PnL d'origine (ce que retournait la stratégie)
          - slippage_usd : slippage appliqué (négatif)
          - pnl : pnl_gross + slippage_usd (net)
        Si flag OFF, **retourne df_trades inchangé** (même objet, pas de copie).
    """
    if enabled is None:
        enabled = BACKTEST_REALISTIC_SLIPPAGE
    if not enabled:
        return df_trades

    if df_trades is None or len(df_trades) == 0 or "pnl" not in df_trades.columns:
        return df_trades

    out = df_trades.copy()
    rng = np.random.default_rng(seed if seed is not None else REALISTIC_SLIPPAGE_SEED)
    out["pnl_gross"] = out["pnl"].astype(float)

    has_atr = "atr_pts" in out.columns
    atr_median = float(out["atr_pts"].median()) if has_atr else 0.0

    slip = np.zeros(len(out), dtype=float)
    filled_mask = (
        out["result"] != "NOT_FILLED" if "result" in out.columns else np.ones(len(out), dtype=bool)
    )
    for i in out.index[filled_mask]:
        n_ct = int(out.at[i, "n_ct"]) if "n_ct" in out.columns else 1
        atr = float(out.at[i, "atr_pts"]) if has_atr else 0.0
        slip[out.index.get_loc(i)] = stochastic_slippage_usd(
            ticker=ticker,
            n_ct=n_ct,
            atr_pts=atr,
            atr_median_pts=atr_median,
            rng=rng,
        )

    out["slippage_usd"] = slip
    out["pnl"] = out["pnl_gross"] + slip
    return out
