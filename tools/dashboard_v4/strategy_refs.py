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
    # FIB v4.1 (2026-06-14) : filtre causal remplace le wick look-ahead. PF causal
    # HONNÊTE (≠ PF naïfs du wick) : OOS sél 2.22 / hold-out 3.19 → ref prudente 2.2.
    "FIB": StrategyRef(
        name="FIB",
        version="fib-v4.1",
        pf_oos=2.2,
        wr_oos=None,
        expectancy_r=0.30,
        n_oos=60,
        rr_typical=2.0,
        risk_usd=200,
        source="output/fib-v4-causal/ (causal, hold-out 3.19 / OOS 2.22)",
        date_ref="2026-06-14",
    ),
    # FIB_FINE v2 : PF OOS 1.63, BS 99.6 %, Bonferroni OK (REGISTRE l.33).
    # PF 1.63 & rr≈2 → wr ≈ 0.45, expectancy_r ≈ 0.13. n_oos # À AFFINER.
    "FIB_FINE": StrategyRef(
        name="FIB_FINE",
        version="fib-fine-v2",
        pf_oos=1.63,
        wr_oos=0.45,
        expectancy_r=0.13,
        n_oos=80,
        rr_typical=2.0,
        risk_usd=130,
        source="REGISTRE_HYPOTHESES.md fib-fine-v2",
        date_ref="2026-06-02",
    ),
    # IB_RETEST v3 (M15, IB break franche 0.5×ATR + retest LIMITE) : OOS sél 2.69,
    # hold-out terminal 1.95, multifold 10/10, DSR 95.1 %. Réf PRUDENTE = hold-out
    # (le plus pur) : PF 1.95, wr 0.60, expectancy_r = +$3164/43/$200 ≈ 0.37. Audité 🟢.
    "IB_RETEST": StrategyRef(
        name="IB_RETEST",
        version="ib-retest-v3",
        pf_oos=1.95,
        wr_oos=0.60,
        expectancy_r=0.37,
        n_oos=43,
        rr_typical=1.5,
        risk_usd=200,
        source="REGISTRE_HYPOTHESES.md ib-retest-v3 (hold-out terminal, prudent)",
        date_ref="2026-06-14",
    ),
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
