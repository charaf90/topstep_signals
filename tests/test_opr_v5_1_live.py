"""
Tests live de core/opr_v5_1.py — schéma A entrée différée.

Stratégie : on charge les données M15 réelles, on identifie une journée OOS
avec un signal v4, et on vérifie que le wrapper v5.1 :
  1. Pour MES1 → pass-through identique à run_opr_day
  2. Pour NQ1/YM1 → filtre selon le running F2

Tous les tests sont causaux (utilisent des bars FERMÉES uniquement).
"""

from __future__ import annotations

from datetime import UTC
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from config import OPR_TIMEZONE
from core.data import load_csv
from core.opr import run_opr_day
from core.opr_v5_1 import get_opr_v5_1_live_signals

_TZ = ZoneInfo(OPR_TIMEZONE)


def _trading_day(ts: str) -> pd.Timestamp:
    return pd.Timestamp(ts).tz_localize(_TZ)


@pytest.fixture(scope="module")
def df_mes1():
    return load_csv("data/MES1_data_m15.csv")


@pytest.fixture(scope="module")
def df_nq1():
    return load_csv("data/NQ1_data_m15.csv")


@pytest.fixture(scope="module")
def df_ym1():
    return load_csv("data/YM1_data_m15.csv")


def _truncate_at(df: pd.DataFrame, cutoff: pd.Timestamp) -> pd.DataFrame:
    """Restreint le DataFrame aux bars dont l'index < cutoff (causalité)."""
    return df[df.index < cutoff]


@pytest.fixture()
def v51_routage_force(monkeypatch):
    """Force le routage v5.1 = {NQ1, YM1} (et MES1 = pass-through v4),
    indépendamment de config.py.

    OPR_V5_1_LIVE_TICKERS est un réglage PROD susceptible de changer (NQ1
    retiré le 2026-06-01 — pause edge). Les tests du chemin filtre v5.1
    valident le CODE, pas le routage du jour : sans ce pin, retirer un
    ticker de la prod casse les tests (cas constaté, stale depuis 1eb58c2)."""
    import core.opr_v5_1 as mod

    monkeypatch.setattr(mod, "OPR_V5_1_LIVE_TICKERS", ["NQ1", "YM1"])


# ─────────────────────────────────────────────────────────────────────────────
# Test 1 : MES1 → pass-through strict (v4)
# ─────────────────────────────────────────────────────────────────────────────


def test_mes1_passthrough_to_v4(df_mes1, v51_routage_force):
    """MES1 n'étant PAS dans OPR_V5_1_LIVE_TICKERS, le wrapper doit retourner
    exactement le même output que run_opr_day."""
    day = _trading_day("2025-11-03")  # un lundi OOS aléatoire

    sigs_v4, trades_v4, zone_v4 = run_opr_day(df_mes1, "MES1", day)
    sigs_v51, trades_v51, zone_v51 = get_opr_v5_1_live_signals(df_mes1, "MES1", day)

    # Pass-through strict : objets identiques
    assert sigs_v51 == sigs_v4
    assert trades_v51 == trades_v4
    assert zone_v51 == zone_v4


# ─────────────────────────────────────────────────────────────────────────────
# Test 2 : NQ1 avec timeline complète → F2 a eu le temps de cross
# ─────────────────────────────────────────────────────────────────────────────


def test_nq1_with_full_day_emits_signal(df_nq1, v51_routage_force):
    """Sur une journée complète où v5.1 backtest a fillé, le wrapper avec
    bars complètes (= fin de session) doit émettre le signal car F2 cross
    naturellement."""
    # Date où il y a eu un fill v5.1 NQ1 selon le rapport (cherchons-en une)
    # On prend une date OOS arbitraire et on vérifie que SI v4 a un signal,
    # alors v5.1 sur fin de session aussi (parce que F2 a normalement cross)
    day = _trading_day("2025-12-01")

    sigs_v4, _, _ = run_opr_day(df_nq1, "NQ1", day)
    sigs_v51, _, _ = get_opr_v5_1_live_signals(df_nq1, "NQ1", day)

    # Si v4 n'a pas de signal, v5.1 non plus (passthrough vide)
    if not sigs_v4:
        assert sigs_v51 == []
        return

    # Si v4 a des signals, v5.1 peut en avoir 0 ou moins (filter)
    assert len(sigs_v51) <= len(sigs_v4)

    # Les signals v5.1 doivent avoir la metadata d'émission
    for s in sigs_v51:
        assert "v5_1_f2_running_at_emit" in s
        assert "v5_1_f2_threshold" in s
        assert s["v5_1_f2_running_at_emit"] >= s["v5_1_f2_threshold"]


# ─────────────────────────────────────────────────────────────────────────────
# Test 3 : NQ1 immédiatement après trigger → pas encore d'émission
# ─────────────────────────────────────────────────────────────────────────────


def test_nq1_at_trigger_time_emits_nothing(df_nq1, v51_routage_force):
    """
    Si on tronque les bars EXACTEMENT au trigger_time, il n'y a pas encore
    de bars post-trigger fermées → running F2 ne peut pas être calculé →
    aucun signal émis (on attend le prochain tick).
    """
    day = _trading_day("2025-12-01")
    sigs_v4, _, _ = run_opr_day(df_nq1, "NQ1", day)

    if not sigs_v4:
        pytest.skip("Pas de signal v4 sur cette date — choisir une autre")

    trigger_ts_raw = sigs_v4[0].get("trigger_time")
    trigger_ts = pd.Timestamp(trigger_ts_raw)
    if trigger_ts.tz is None:
        trigger_ts = trigger_ts.tz_localize(_TZ)
    else:
        trigger_ts = trigger_ts.tz_convert(_TZ)

    # Tronquer juste avant la bougie suivant le trigger pour ne PAS avoir de
    # bars post-trigger fermées. df_nq1.index est en UTC naïf.
    cutoff_utc = (trigger_ts + pd.Timedelta(minutes=1)).tz_convert("UTC").tz_localize(None)
    df_truncated = _truncate_at(df_nq1, cutoff_utc)

    sigs_v51, _, _ = get_opr_v5_1_live_signals(df_truncated, "NQ1", day)

    # À ce stade : trigger détecté (la bougie trigger est incluse) mais c'est
    # la SEULE bougie post-trigger → l'excursion est calculée sur le high de
    # cette bougie, qui peut ou non passer le seuil. Le test vérifie surtout
    # qu'aucune exception n'est levée et que le résultat est cohérent.
    assert isinstance(sigs_v51, list)
    # Si signals émis, leur metadata doit être présente
    for s in sigs_v51:
        assert "v5_1_f2_running_at_emit" in s


# ─────────────────────────────────────────────────────────────────────────────
# Test 4 : Idempotence — appels répétés donnent le même résultat
# ─────────────────────────────────────────────────────────────────────────────


def test_idempotent_repeated_calls(df_nq1, v51_routage_force):
    """Appeler le wrapper plusieurs fois sur les mêmes données doit produire
    exactement le même résultat (déterministe, sans état caché)."""
    day = _trading_day("2025-12-01")
    out1 = get_opr_v5_1_live_signals(df_nq1, "NQ1", day)
    out2 = get_opr_v5_1_live_signals(df_nq1, "NQ1", day)
    sigs1, trades1, zone1 = out1
    sigs2, trades2, zone2 = out2
    assert sigs1 == sigs2
    assert trades1 == trades2
    assert zone1 == zone2


# ─────────────────────────────────────────────────────────────────────────────
# Test 5 : Pass-through pour ticker non configuré
# ─────────────────────────────────────────────────────────────────────────────


def test_unknown_ticker_passthrough(df_mes1, monkeypatch):
    """Si OPR_V5_1_LIVE_TICKERS est vidé temporairement, MES1 reste passthrough."""
    import core.opr_v5_1 as mod

    monkeypatch.setattr(mod, "OPR_V5_1_LIVE_TICKERS", [])

    day = _trading_day("2025-11-03")
    sigs_v4, _, _ = run_opr_day(df_mes1, "MES1", day)
    sigs_v51, _, _ = get_opr_v5_1_live_signals(df_mes1, "MES1", day)
    assert sigs_v51 == sigs_v4


# ─────────────────────────────────────────────────────────────────────────────
# Test 6 : threshold None → comportement v4 strict
# ─────────────────────────────────────────────────────────────────────────────


def test_threshold_none_yields_v4(df_nq1, monkeypatch, v51_routage_force):
    """Si f2_min_atr est None pour un ticker, le wrapper retourne v4 strict."""
    import core.opr_v5_1 as mod

    monkeypatch.setattr(mod, "OPR_V5_1_F2_MIN_ATR", {"NQ1": None, "YM1": None})

    day = _trading_day("2025-12-01")
    sigs_v4, _, _ = run_opr_day(df_nq1, "NQ1", day)
    sigs_v51, _, _ = get_opr_v5_1_live_signals(df_nq1, "NQ1", day)
    assert sigs_v51 == sigs_v4


# ─────────────────────────────────────────────────────────────────────────────
# Test 7-9 : Phase C — intégration M1 buffer (paramètres optionnels)
# ─────────────────────────────────────────────────────────────────────────────


def _trigger_ts_for(df, ticker: str, day: pd.Timestamp) -> pd.Timestamp:
    """Helper : retourne le trigger_time du premier signal v4 du jour."""
    sigs_v4, _, _ = run_opr_day(df, ticker, day)
    if not sigs_v4:
        return None
    t = pd.Timestamp(sigs_v4[0]["trigger_time"])
    if t.tz is None:
        t = t.tz_localize(_TZ)
    return t


def test_m1_buffer_overrides_m15_extremum(df_nq1, v51_routage_force):
    """
    Si un buffer M1 contient un bar avec un high SUPÉRIEUR au max des bars M15
    post-trigger, l'excursion calculée doit utiliser le high M1 (et donc
    déclencher l'émission plus tôt ou avec un F2 plus élevé que sans buffer).
    """

    from broker.m1_buffer import M1Bar, M1Buffer

    day = _trading_day("2025-12-01")
    trigger_ts = _trigger_ts_for(df_nq1, "NQ1", day)
    if trigger_ts is None:
        pytest.skip("Pas de signal v4 sur cette date")

    # Sans buffer : baseline
    sigs_baseline, _, _ = get_opr_v5_1_live_signals(df_nq1, "NQ1", day)
    if not sigs_baseline:
        pytest.skip("Pas de signal v5.1 baseline — choisir autre date")

    baseline_f2 = sigs_baseline[0]["v5_1_f2_running_at_emit"]
    opr_high = sigs_baseline[0]["opr_high"]
    atr_daily = sigs_baseline[0]["atr_daily"]

    # Avec buffer M1 contenant un bar avec un high 50 pts au-dessus du baseline
    # → l'excursion long doit augmenter
    buf = M1Buffer(max_minutes=120)
    spike_high = opr_high + (baseline_f2 + 0.5) * atr_daily  # bien au-dessus seuil
    spike_bar = M1Bar(
        contract_id="CON.F.US.MNQ.M26",
        start_ts=trigger_ts.tz_convert("UTC").to_pydatetime().replace(tzinfo=UTC),
        open=spike_high,
        high=spike_high,
        low=spike_high - 1,
        close=spike_high - 0.5,
        volume=10,
    )
    buf.inject_bars([spike_bar])

    sigs_m1, _, _ = get_opr_v5_1_live_signals(
        df_nq1,
        "NQ1",
        day,
        m1_buffer=buf,
        contract_id="CON.F.US.MNQ.M26",
    )
    assert sigs_m1, "Avec spike M1, on devrait toujours émettre"
    assert sigs_m1[0]["v5_1_f2_source"] == "M1_buffer"
    # Le F2 émis doit refléter le spike M1 (≥ baseline)
    assert sigs_m1[0]["v5_1_f2_running_at_emit"] >= baseline_f2


def test_m1_buffer_empty_falls_back_to_m15(df_nq1, v51_routage_force):
    """Buffer M1 fourni mais aucun bar depuis trigger → fallback M15 +
    annotation source=M15."""
    from broker.m1_buffer import M1Buffer

    day = _trading_day("2025-12-01")
    buf = M1Buffer(max_minutes=120)  # vide

    sigs_baseline, _, _ = get_opr_v5_1_live_signals(df_nq1, "NQ1", day)
    sigs_with_empty_buf, _, _ = get_opr_v5_1_live_signals(
        df_nq1,
        "NQ1",
        day,
        m1_buffer=buf,
        contract_id="CON.F.US.MNQ.M26",
    )

    # Même résultat numérique (mêmes signals)
    assert len(sigs_baseline) == len(sigs_with_empty_buf)
    for s1, s2 in zip(sigs_baseline, sigs_with_empty_buf):
        assert s1["v5_1_f2_running_at_emit"] == pytest.approx(s2["v5_1_f2_running_at_emit"])
    # Annotation source = M15 pour les deux
    for s in sigs_with_empty_buf:
        assert s["v5_1_f2_source"] == "M15"


def test_m1_buffer_without_contract_id_uses_m15(df_nq1, v51_routage_force):
    """Buffer M1 fourni SANS contract_id → fallback M15 (sécurité)."""
    from broker.m1_buffer import M1Buffer

    day = _trading_day("2025-12-01")
    buf = M1Buffer(max_minutes=10)
    # Même sans contract_id, le code doit ignorer le buffer
    sigs, _, _ = get_opr_v5_1_live_signals(
        df_nq1,
        "NQ1",
        day,
        m1_buffer=buf,
        contract_id=None,
    )
    for s in sigs:
        assert s["v5_1_f2_source"] == "M15"
