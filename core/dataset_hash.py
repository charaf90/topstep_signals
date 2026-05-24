"""Empreintes SHA256 des datasets CSV pour reproductibilité.

Sert à figer dans `summary.json` (cf. skill `/new-strategy`) la version
exacte des données utilisées par un backtest. Permet de détecter qu'une
métrique d'un rapport a été produite avec des données différentes (gap
comblé, donnée corrigée a posteriori, mauvaise série livrée).

Usage typique :
    from core.dataset_hash import snapshot_datasets

    digests = snapshot_datasets(csv_dir="data", tickers=["NQ1", "MES1", "MGC1"])
    summary["datasets"] = digests

Format renvoyé :
    {
        "NQ1": "sha256:a1b2c3...",
        "MES1": "sha256:d4e5f6...",
        "MGC1": "sha256:9a8b7c...",
    }

Hashage par streaming (block-by-block) → consommation mémoire constante
même pour des CSV de plusieurs centaines de MB.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

# Taille de bloc lue à chaque itération (64 KB) — équilibre entre nombre
# d'appels système et empreinte mémoire.
_BLOCK_SIZE = 64 * 1024


def sha256_of_file(path: str | Path) -> str:
    """Calcule le SHA256 d'un fichier, retourne `sha256:<hex>`.

    Lève FileNotFoundError si le fichier n'existe pas.
    """
    p = Path(path)
    h = hashlib.sha256()
    with p.open("rb") as f:
        while chunk := f.read(_BLOCK_SIZE):
            h.update(chunk)
    return f"sha256:{h.hexdigest()}"


def snapshot_datasets(
    csv_dir: str | Path,
    tickers: list[str] | tuple[str, ...],
    tf_suffix: str = "m15",
) -> dict[str, str]:
    """Calcule l'empreinte de chaque CSV `<TICKER>_data_<tf_suffix>.csv`.

    Args:
        csv_dir   : dossier contenant les CSV (typiquement "data").
        tickers   : tickers à hasher (ex: ["NQ1", "MES1"]).
        tf_suffix : suffixe de timeframe (défaut: "m15").

    Returns:
        Dict mappant chaque ticker à son `sha256:<hex>`. Si un fichier
        est introuvable, la valeur est `"missing:<path>"` plutôt que
        de lever — la reproductibilité reste vérifiable même partiellement.
    """
    csv_dir = Path(csv_dir)
    out: dict[str, str] = {}
    for ticker in tickers:
        csv_path = csv_dir / f"{ticker}_data_{tf_suffix}.csv"
        if not csv_path.exists():
            out[ticker] = f"missing:{csv_path}"
            continue
        out[ticker] = sha256_of_file(csv_path)
    return out
