"""Tests pour broker/shadow_runner + tools/shadow_vs_live.

INVARIANT critique testé : le shadow runner force dry_run=True en dur.
Toute tentative de produire un shadow runner avec dry_run=False doit échouer.
"""

from __future__ import annotations

import json

# Import du module shadow_vs_live (tools n'est pas un package)
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import shadow_vs_live  # noqa: E402

from broker.shadow_runner import build_shadow_runner  # noqa: E402

# ──────────────────────────────────────────────────────────────────────────────
# Mock minimal ProjectXClient
# ──────────────────────────────────────────────────────────────────────────────


class MockClient:
    def __init__(self):
        self.token = "fake-token"

    def search_contract(self, symbol, live=True):
        return {"id": f"CON.{symbol}", "name": symbol}


# ──────────────────────────────────────────────────────────────────────────────
# build_shadow_runner — invariant dry_run
# ──────────────────────────────────────────────────────────────────────────────


class TestBuildShadowRunner:

    def test_dry_run_force_a_true(self, tmp_path):
        runner = build_shadow_runner(
            client=MockClient(),
            account_id=12345,
            state_file=str(tmp_path / "shadow.json"),
            tickers=["MES1"],
        )
        assert runner.dry_run is True

    def test_state_file_separe(self, tmp_path):
        path = str(tmp_path / "shadow.json")
        runner = build_shadow_runner(
            client=MockClient(),
            account_id=12345,
            state_file=path,
            tickers=["MES1"],
        )
        assert str(runner.state_file) == path

    def test_state_file_par_defaut_est_shadow(self):
        from config import SHADOW_STATE_FILE

        runner = build_shadow_runner(
            client=MockClient(),
            account_id=12345,
            tickers=["MES1"],
        )
        assert str(runner.state_file) == SHADOW_STATE_FILE

    def test_tickers_personnalises(self, tmp_path):
        runner = build_shadow_runner(
            client=MockClient(),
            account_id=12345,
            state_file=str(tmp_path / "shadow.json"),
            tickers=["NQ1", "MES1"],
        )
        assert runner.tickers == ["NQ1", "MES1"]


# ──────────────────────────────────────────────────────────────────────────────
# shadow_vs_live.compare — logique pure
# ──────────────────────────────────────────────────────────────────────────────


class TestCompare:

    @staticmethod
    def _state_with(tags: dict):
        return {"date": "2026-05-25", "account_id": 12345, "placed_tags": tags}

    def test_alignement_total(self):
        tag_info = {
            "ticker": "NQ1",
            "direction": "long",
            "entry": 28000.0,
            "sl": 27950.0,
            "tp": 28100.0,
            "n_ct": 2,
            "status": "FILLED",
            "placed_at": "2026-05-25T14:00:00Z",
        }
        live = self._state_with({"T1": tag_info})
        shadow = self._state_with({"T1": dict(tag_info)})
        report = shadow_vs_live.compare(live, shadow)
        assert len(report.common_tags) == 1
        assert report.n_diverging == 0
        assert not report.only_in_live
        assert not report.only_in_shadow

    def test_divergence_entry(self):
        live_tag = {
            "entry": 28000.0,
            "sl": 27950.0,
            "tp": 28100.0,
            "n_ct": 2,
            "placed_at": "2026-05-25T14:00:00Z",
        }
        shadow_tag = dict(live_tag, entry=28005.0)  # 5 points de diff
        report = shadow_vs_live.compare(
            self._state_with({"T1": live_tag}),
            self._state_with({"T1": shadow_tag}),
        )
        assert len(report.common_tags) == 1
        assert report.n_diverging == 1
        assert report.common_tags[0].has_diff
        assert report.common_tags[0].live_entry == 28000.0
        assert report.common_tags[0].shadow_entry == 28005.0

    def test_orphelin_live(self):
        live = self._state_with(
            {
                "T_live_only": {"entry": 100, "placed_at": "2026-05-25T14:00:00Z"},
            }
        )
        shadow = self._state_with({})
        report = shadow_vs_live.compare(live, shadow)
        assert report.only_in_live == ["T_live_only"]
        assert not report.only_in_shadow

    def test_orphelin_shadow(self):
        live = self._state_with({})
        shadow = self._state_with(
            {"T_shadow_only": {"entry": 200, "placed_at": "2026-05-25T14:00:00Z"}}
        )
        report = shadow_vs_live.compare(live, shadow)
        assert not report.only_in_live
        assert report.only_in_shadow == ["T_shadow_only"]

    def test_filtre_par_date(self):
        live = self._state_with(
            {
                "T_22": {"entry": 1, "placed_at": "2026-05-22T14:00:00Z"},
                "T_25": {"entry": 2, "placed_at": "2026-05-25T14:00:00Z"},
            }
        )
        shadow = self._state_with(
            {
                "T_22": {"entry": 1, "placed_at": "2026-05-22T14:00:00Z"},
                "T_25": {"entry": 2, "placed_at": "2026-05-25T14:00:00Z"},
            }
        )
        # Filtre sur 2026-05-22 → seul T_22 doit apparaître
        report = shadow_vs_live.compare(live, shadow, date_filter="2026-05-22")
        assert len(report.common_tags) == 1
        assert report.common_tags[0].tag == "T_22"

    def test_n_ct_different(self):
        live = self._state_with(
            {"T1": {"entry": 1, "sl": 0.5, "tp": 2, "n_ct": 2, "placed_at": "2026-05-25T14:00:00Z"}}
        )
        shadow = self._state_with(
            {"T1": {"entry": 1, "sl": 0.5, "tp": 2, "n_ct": 4, "placed_at": "2026-05-25T14:00:00Z"}}
        )
        report = shadow_vs_live.compare(live, shadow)
        assert report.n_diverging == 1
        assert report.common_tags[0].live_n_ct == 2
        assert report.common_tags[0].shadow_n_ct == 4


# ──────────────────────────────────────────────────────────────────────────────
# Rendu humain — sanity check
# ──────────────────────────────────────────────────────────────────────────────


def test_format_human_alignement():
    live = {
        "placed_tags": {
            "T1": {
                "entry": 100,
                "sl": 95,
                "tp": 110,
                "n_ct": 2,
                "placed_at": "2026-05-25T14:00:00Z",
            }
        }
    }
    shadow = {
        "placed_tags": {
            "T1": {
                "entry": 100,
                "sl": 95,
                "tp": 110,
                "n_ct": 2,
                "placed_at": "2026-05-25T14:00:00Z",
            }
        }
    }
    report = shadow_vs_live.compare(live, shadow)
    txt = shadow_vs_live.format_report_human(report)
    assert "SHADOW ALIGNÉ" in txt


def test_format_human_divergence():
    live = {"placed_tags": {"T1": {"entry": 100, "placed_at": "2026-05-25T14:00:00Z"}}}
    shadow = {"placed_tags": {"T1": {"entry": 105, "placed_at": "2026-05-25T14:00:00Z"}}}
    report = shadow_vs_live.compare(live, shadow)
    txt = shadow_vs_live.format_report_human(report)
    assert "DIVERGENCE" in txt


# ──────────────────────────────────────────────────────────────────────────────
# to_dict — sortie JSON déterministe
# ──────────────────────────────────────────────────────────────────────────────


def test_to_dict_serialisable():
    live = {"placed_tags": {"T1": {"entry": 100, "placed_at": "2026-05-25T14:00:00Z"}}}
    shadow = {"placed_tags": {"T1": {"entry": 105, "placed_at": "2026-05-25T14:00:00Z"}}}
    report = shadow_vs_live.compare(live, shadow)
    d = report.to_dict()
    # JSON serializable
    serialized = json.dumps(d, sort_keys=True)
    assert "common_tags" in serialized
    assert "n_diverging" in serialized
    assert d["n_diverging"] == 1
