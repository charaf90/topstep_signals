"""Golden master tests — détecte les régressions silencieuses des stratégies.

Pour chaque stratégie figée (opr, opr_v5_1, fib_v4) on recalcule le
baseline complet et on compare au JSON archivé dans `tests/golden/`.

Si un test échoue : c'est soit un changement volontaire (alors
régénérer les baselines via `tests/golden/regenerate.sh` dans un
commit dédié), soit un bug — investiguer avant tout merge.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._golden_helpers import build_baseline

GOLDEN_DIR = Path(__file__).parent / "golden"
STRATEGIES = ["opr", "opr_v5_1", "fib_v4"]


def _load_baseline(strategy_name: str) -> dict:
    path = GOLDEN_DIR / f"{strategy_name}_baseline.json"
    if not path.exists():
        pytest.skip(
            f"Baseline manquant : {path}. Lance "
            f"`python tests/golden/regenerate.py` pour le créer."
        )
    return json.loads(path.read_text())


@pytest.mark.parametrize("strategy_name", STRATEGIES)
def test_golden_master(strategy_name: str) -> None:
    expected = _load_baseline(strategy_name)
    actual = build_baseline(strategy_name)

    # Comparaison fine pour produire un message d'erreur utile
    assert (
        actual["strategy_id"] == expected["strategy_id"]
    ), f"strategy_id changé : {expected['strategy_id']} → {actual['strategy_id']}"

    for ticker in expected["tickers"]:
        assert ticker in actual["tickers"], f"Ticker manquant dans actual : {ticker}"
        exp = expected["tickers"][ticker]
        act = actual["tickers"][ticker]

        if "missing_data" in exp:
            assert "missing_data" in act, (
                f"[{strategy_name}/{ticker}] baseline marqué missing_data, " f"actual a des données"
            )
            continue

        assert act["n_total"] == exp["n_total"], (
            f"[{strategy_name}/{ticker}] n_total : "
            f"baseline={exp['n_total']} vs actual={act['n_total']}"
        )
        assert act["n_filled"] == exp["n_filled"], (
            f"[{strategy_name}/{ticker}] n_filled : "
            f"baseline={exp['n_filled']} vs actual={act['n_filled']}"
        )
        assert act["sum_pnl"] == exp["sum_pnl"], (
            f"[{strategy_name}/{ticker}] sum_pnl : "
            f"baseline=${exp['sum_pnl']:+,.2f} vs actual=${act['sum_pnl']:+,.2f}"
        )
        # Comparaison trade-à-trade : trouve le premier diff pour message ciblé
        for i, (e_trade, a_trade) in enumerate(zip(exp["trades"], act["trades"], strict=True)):
            assert a_trade == e_trade, (
                f"[{strategy_name}/{ticker}] trade #{i} diffère :\n"
                f"  baseline = {e_trade}\n"
                f"  actual   = {a_trade}"
            )
