"""Tests pour core/dataset_hash."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from core.dataset_hash import _BLOCK_SIZE, sha256_of_file, snapshot_datasets

# ──────────────────────────────────────────────────────────────────────────────
# sha256_of_file
# ──────────────────────────────────────────────────────────────────────────────


class TestSha256OfFile:

    def test_petit_fichier(self, tmp_path: Path):
        f = tmp_path / "x.csv"
        f.write_bytes(b"hello world")
        expected = "sha256:" + hashlib.sha256(b"hello world").hexdigest()
        assert sha256_of_file(f) == expected

    def test_fichier_vide(self, tmp_path: Path):
        f = tmp_path / "empty.csv"
        f.write_bytes(b"")
        expected = "sha256:" + hashlib.sha256(b"").hexdigest()
        assert sha256_of_file(f) == expected

    def test_streaming_multibloc(self, tmp_path: Path):
        """Le streaming bloc par bloc doit donner le même résultat qu'un read() complet."""
        f = tmp_path / "big.bin"
        # 3× la taille du bloc → exerce la boucle while
        payload = b"abcd" * (_BLOCK_SIZE * 3 // 4)
        f.write_bytes(payload)
        assert sha256_of_file(f) == "sha256:" + hashlib.sha256(payload).hexdigest()

    def test_deterministe(self, tmp_path: Path):
        f = tmp_path / "x.csv"
        f.write_bytes(b"deterministe")
        h1 = sha256_of_file(f)
        h2 = sha256_of_file(f)
        assert h1 == h2

    def test_change_si_contenu_change(self, tmp_path: Path):
        f = tmp_path / "x.csv"
        f.write_bytes(b"v1")
        h1 = sha256_of_file(f)
        f.write_bytes(b"v2")
        h2 = sha256_of_file(f)
        assert h1 != h2

    def test_fichier_inexistant_leve(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            sha256_of_file(tmp_path / "absent.csv")


# ──────────────────────────────────────────────────────────────────────────────
# snapshot_datasets
# ──────────────────────────────────────────────────────────────────────────────


class TestSnapshotDatasets:

    def test_tous_presents(self, tmp_path: Path):
        for t in ("MES1", "NQ1", "YM1"):
            (tmp_path / f"{t}_data_m15.csv").write_bytes(f"data {t}".encode())
        out = snapshot_datasets(tmp_path, ["MES1", "NQ1", "YM1"])
        assert set(out) == {"MES1", "NQ1", "YM1"}
        for t in out.values():
            assert t.startswith("sha256:")
            assert len(t) == 7 + 64  # "sha256:" + 64 hex chars

    def test_un_manquant_balise_missing(self, tmp_path: Path):
        (tmp_path / "MES1_data_m15.csv").write_bytes(b"data MES1")
        out = snapshot_datasets(tmp_path, ["MES1", "NQ1"])
        assert out["MES1"].startswith("sha256:")
        assert out["NQ1"].startswith("missing:")

    def test_tf_suffix_custom(self, tmp_path: Path):
        (tmp_path / "MES1_data_h1.csv").write_bytes(b"hourly")
        out = snapshot_datasets(tmp_path, ["MES1"], tf_suffix="h1")
        assert out["MES1"].startswith("sha256:")

    def test_dossier_vide_tout_missing(self, tmp_path: Path):
        out = snapshot_datasets(tmp_path, ["MES1", "NQ1"])
        assert all(v.startswith("missing:") for v in out.values())

    def test_meme_contenu_meme_hash_entre_tickers(self, tmp_path: Path):
        (tmp_path / "MES1_data_m15.csv").write_bytes(b"identique")
        (tmp_path / "NQ1_data_m15.csv").write_bytes(b"identique")
        out = snapshot_datasets(tmp_path, ["MES1", "NQ1"])
        assert out["MES1"] == out["NQ1"]


# ──────────────────────────────────────────────────────────────────────────────
# Smoke test sur les vraies données du repo (si présentes)
# ──────────────────────────────────────────────────────────────────────────────


def test_smoke_real_data_si_disponible():
    """Smoke test sur les CSV réels de data/ s'ils sont présents."""
    data_dir = Path(__file__).parent.parent / "data"
    if not data_dir.exists():
        pytest.skip("data/ absent en environnement de test")
    out = snapshot_datasets(data_dir, ["NQ1", "MES1", "YM1", "MGC1"])
    # Au moins un fichier doit être présent en environnement de dev
    sha_entries = [v for v in out.values() if v.startswith("sha256:")]
    if not sha_entries:
        pytest.skip("aucun CSV ticker trouvé dans data/")
    for v in sha_entries:
        assert len(v) == 7 + 64
