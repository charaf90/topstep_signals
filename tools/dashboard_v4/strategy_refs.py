"""Références backtest par stratégie — attentes OOS pour le verdict sizing.

Chaque ``StrategyRef`` fige ce que la stratégie DEVRAIT produire (validation
walk-forward OOS) au moment de sa promotion. Le dashboard compare le live à
ces attentes (expectancy R primaire, win rate secondaire) et émet un verdict
borné par l'incertitude (CI bootstrap / Wilson — cf. stats.py).

Métrique primaire : ``expectancy_r`` = P&L OOS / n_oos / risk_usd
(gain moyen par trade, en multiples du risque engagé — comparable entre
stratégies de sizing différent).

⚠️ Après une promotion/bump de version, mettre à jour la ref correspondante :
le dashboard alerte si ``version`` ≠ ``*_STRATEGY_VERSION`` de config.py.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StrategyRef:
    name: str
    version: str  # doit matcher <STRAT>_STRATEGY_VERSION de config.py
    pf_oos: float
    wr_oos: float | None  # None si non extrait des rapports → WR live affiché sans ref
    expectancy_r: float  # attente PRIMAIRE (R par trade)
    n_oos: int
    rr_typical: float
    risk_usd: int  # sizing config au moment de la ref
    source: str
    date_ref: str


STRATEGY_REFS: dict[str, StrategyRef] = {
    # OPR v5.1, périmètre live YM1 seul : OOS PF=5.75, P&L=+$3216, n=45
    # (config.py ~l.821). expectancy_r brut = 3216/45/200 ≈ 0.357.
    # Fidélité live mesurée 74 % (26 % des trades backtest rejetés en live,
    # config.py ~l.840) → ref dégradée ×0.74 ≈ 0.26.
    "OPR": StrategyRef(
        name="OPR",
        version="opr-v5.1",
        pf_oos=5.75,
        wr_oos=None,
        expectancy_r=0.26,
        n_oos=45,
        rr_typical=2.0,
        risk_usd=200,
        source="output/robustness_opr-v5.1.json (YM1) · fidélité scripts/live_eq_v5_1.py",
        date_ref="2026-05-18",
    ),
    # FIB v4.1 — RÉF RETIRÉE 2026-06-19 : edge RÉFUTÉ sur M1 honnête (OOS pool 0.85),
    # FIB_V4_ENABLED=False (coupé). Le dash affiche fib-v4 OFF sans carte verdict-sizing.
    # FIB_FINE v2 — NQ1 SEUL (MES1 coupé 2026-06-19), tp_mult 1.2, risk $240.
    # Réf PRUDENTE = hold-out terminal 2026-04-15→06-18 : PF 2.67, WR 0.76,
    # +$766/21/$240 ≈ 0.15 (OOS sél PF 3.98 ; multifold 5/5 folds +). rr_typical 1.2 (TP serré).
    "FIB_FINE": StrategyRef(
        name="FIB_FINE",
        version="fib-fine-v2",
        pf_oos=2.0,
        wr_oos=0.72,
        expectancy_r=0.15,
        n_oos=44,
        rr_typical=1.2,
        risk_usd=240,
        source="REGISTRE fib-fine-v2 NQ1 re-optim (hold-out terminal, prudent)",
        date_ref="2026-06-19",
    ),
    # IB_RETEST v3 — RÉF RETIRÉE 2026-06-19 : M1 MARGINAL (IS breakeven, DSR 19.5 %),
    # IB_RETEST_ENABLED=False (coupé). Le dash affiche ib-retest OFF sans carte verdict-sizing.
    # BOS_FVG — RÉF RETIRÉE 2026-06-13 : le PF de promotion (2.26-2.93) est
    # ARTEFACTUEL (biais d'ordre fill/invalidation ; honnête OOS PF ~1.2, NQ1 sans
    # edge — cf. REGISTRE bos-fvg-v2 révision + BACKLOG P1 bos-fvg-fill-bias).
    # Stratégie PAUSÉE (BOS_FVG_ENABLED=False). NE PAS ré-ajouter de ref tant que
    # la re-validation honnête n'a pas produit de chiffres fiables → le dash affiche
    # bos-fvg "OFF" sans carte verdict-sizing (pas de comparaison à une attente fausse).
}

# Variable de version config.py associée à chaque ref (contrôle de péremption).
_VERSION_VARS = {
    "OPR": "OPR_V5_1_STRATEGY_VERSION",
    "FIB": "FIB_V4_STRATEGY_VERSION",
    "FIB_FINE": "FIB_FINE_STRATEGY_VERSION",
    "BOS_FVG": "BOS_FVG_STRATEGY_VERSION",
    "IB_RETEST": "IB_RETEST_STRATEGY_VERSION",
}


def stale_refs() -> dict[str, tuple[str, str]]:
    """Refs périmées : {stratégie: (version_ref, version_config)} si divergence."""
    import config  # noqa: PLC0415

    out: dict[str, tuple[str, str]] = {}
    for key, ref in STRATEGY_REFS.items():
        current = getattr(config, _VERSION_VARS[key], None)
        if current and current != ref.version:
            out[key] = (ref.version, current)
    return out
