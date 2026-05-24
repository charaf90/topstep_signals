"""
core/data_fetcher.py — fetch historique propre depuis l'API ProjectX (TopstepX).

Conçu pour alimenter les backtests et le skill /new-strategy avec :
  • Tous les timeframes supportés par l'API (M1 → D1).
  • Tous les actifs disponibles sur TopstepX (indices, métaux, énergie,
    devises, crypto, taux).
  • Pagination automatique : l'API plafonne à 20 000 bougies/requête,
    on découpe en chunks temporels et on reconstitue l'historique complet
    sans doublon.
  • Format DataFrame identique à `core.data.load_csv` → compatibilité
    immédiate avec `core/backtester.py`, `strategies/*.py`, `core/optimizer.py`.

Usage Python
────────────
    from broker.projectx_client import ProjectXClient
    from core.data_fetcher import fetch_history, save_to_csv

    client = ProjectXClient(username, api_key)
    client.login()

    df = fetch_history(client, symbol="MGC",
                       days_back=180, timeframe="m15", live=False)
    save_to_csv(df, ticker="MGC1", timeframe="m15")
    # → data/MGC1_data_m15.csv prêt pour `python backtest.py --strategy …`

Usage CLI
─────────
    python -m core.data_fetcher --list
    python -m core.data_fetcher --symbol MGC --timeframe m15 --days 180 --save
    python -m core.data_fetcher --symbol MCL --timeframe h1  --days 365 --save
    python -m core.data_fetcher --available --live false        # catalogue ProjectX
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from broker.projectx_client import ProjectXClient

_log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# Constantes
# ═══════════════════════════════════════════════════════════════════════════════

# Plafond API ProjectX par requête (20 000 bougies). On laisse une marge de
# sécurité pour absorber les corrections d'horaire / overlap des chunks.
MAX_BARS_PER_REQUEST = 20_000
_SAFETY_FACTOR = 0.95  # on demande ≤ 19 000 bars par chunk

# Unit codes API : (unit, unit_number, minutes_par_bougie)
TIMEFRAMES: dict[str, tuple[int, int, int]] = {
    # Seconds
    "s1": (1, 1, 0),  # 1/60 minute (0 = sub-minute, traité à part)
    "s5": (1, 5, 0),
    "s15": (1, 15, 0),
    "s30": (1, 30, 0),
    # Minutes
    "m1": (2, 1, 1),
    "m5": (2, 5, 5),
    "m15": (2, 15, 15),
    "m30": (2, 30, 30),
    # Hours
    "h1": (3, 1, 60),
    "h2": (3, 2, 120),
    "h4": (3, 4, 240),
    # Days, weeks, months
    "d1": (4, 1, 1440),
    "w1": (5, 1, 10080),
    "mo1": (6, 1, 43200),
}

# Catalogue raccourci des sous-jacents Topstep usuels.
# Mapping : alias court → symbole de recherche ProjectX (passé à
# /api/Contract/search). Le résultat est filtré sur le contrat front-month
# (premier `activeContract=True`).
#
# Note : cette liste est indicative. Pour découvrir tous les contrats
# disponibles, utiliser `list_known_symbols()` ou la CLI `--available`.
CATALOG: dict[str, dict[str, str]] = {
    # Equity index micros
    "MES": {"search": "MES", "desc": "Micro E-mini S&P 500"},
    "MNQ": {"search": "MNQ", "desc": "Micro E-mini Nasdaq-100"},
    "MYM": {"search": "MYM", "desc": "Micro E-mini Dow Jones"},
    "M2K": {"search": "M2K", "desc": "Micro E-mini Russell 2000"},
    # Equity index full
    "ES": {"search": "ES", "desc": "E-mini S&P 500"},
    "NQ": {"search": "NQ", "desc": "E-mini Nasdaq-100"},
    # Metals
    "MGC": {"search": "MGC", "desc": "Micro Gold"},
    "SIL": {"search": "SIL", "desc": "Micro Silver"},
    "GC": {"search": "GC", "desc": "Gold"},
    # Energy
    "MCL": {"search": "MCL", "desc": "Micro Crude Oil"},
    "MNG": {"search": "MNG", "desc": "Micro Natural Gas"},
    "CL": {"search": "CL", "desc": "Crude Oil"},
    # Currencies
    "M6E": {"search": "M6E", "desc": "Micro Euro FX"},
    "M6B": {"search": "M6B", "desc": "Micro British Pound"},
    "M6A": {"search": "M6A", "desc": "Micro Australian Dollar"},
    # Crypto
    "MBT": {"search": "MBT", "desc": "Micro Bitcoin"},
    "MET": {"search": "MET", "desc": "Micro Ether"},
    # Rates
    "10Y": {"search": "10Y", "desc": "Micro 10-Year Yield"},
}


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _resolve_timeframe(tf: str) -> tuple[int, int, int]:
    """tf string → (unit, unit_number, minutes_par_bougie). Lève ValueError."""
    key = tf.lower().strip()
    if key not in TIMEFRAMES:
        avail = ", ".join(TIMEFRAMES)
        raise ValueError(f"Timeframe inconnu '{tf}'. Disponibles : {avail}")
    return TIMEFRAMES[key]


def _chunk_window_minutes(tf: str) -> int:
    """
    Durée maximale d'un chunk pour rester sous MAX_BARS_PER_REQUEST.

    Si un timeframe = N minutes, alors une fenêtre de N × 19 000 minutes
    contient au plus 19 000 bougies (avec marge de sécurité).
    """
    _, _, mpb = _resolve_timeframe(tf)
    if mpb <= 0:
        # Sous-minute : on suppose 60 secondes / bougie comme borne
        # (s1 = 1s donc 20 000 secondes ≈ 333 minutes par chunk)
        return int(MAX_BARS_PER_REQUEST * _SAFETY_FACTOR / 60)
    return int(MAX_BARS_PER_REQUEST * _SAFETY_FACTOR * mpb)


def _bars_to_df(bars: list[dict]) -> pd.DataFrame:
    """
    Convertit la réponse API en DataFrame OHLCV au format projet :
      index : pd.DatetimeIndex UTC NAÏF (cohérent avec core/data.py)
      columns : open, high, low, close, volume (float, sauf volume int)
    """
    if not bars:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(bars)
    df = df.rename(
        columns={"t": "datetime", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}
    )
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True).dt.tz_localize(None)  # UTC naïf
    df = (
        df.set_index("datetime")
        .sort_index()[["open", "high", "low", "close", "volume"]]
        .astype({"open": float, "high": float, "low": float, "close": float})
    )
    df["volume"] = df["volume"].astype("int64")
    return df


# ═══════════════════════════════════════════════════════════════════════════════
# Résolution de contrat
# ═══════════════════════════════════════════════════════════════════════════════


def resolve_contract(
    client: ProjectXClient,
    symbol: str,
    live: bool = False,
    prefer_micro: bool = True,
) -> dict | None:
    """
    Résout un symbole (alias CATALOG ou raw) → premier contrat actif.

    Logique :
      1. Si symbol est dans CATALOG, on utilise l'entrée 'search'.
      2. Sinon on tape le symbole tel quel dans /api/Contract/search.
      3. Filtre : on garde les contrats `activeContract=True`.
      4. Si plusieurs résultats : on prend le premier (le plus liquide /
         front-month par convention API).

    `prefer_micro` est conservé pour compat future (l'API renvoie déjà le
    micro en premier quand on cherche par nom).
    """
    sym = symbol.strip().upper()
    search_text = CATALOG.get(sym, {}).get("search", sym)
    contracts = client.search_contracts(search_text, live=live)
    if not contracts:
        _log.warning("resolve_contract(%s) : aucun match", symbol)
        return None

    active = [c for c in contracts if c.get("activeContract")]
    candidates = active or contracts
    # Match exact sur le root symbol si possible (évite par ex. NQ → MNQ
    # quand on demande "NQ" mais via CATALOG)
    exact = [
        c
        for c in candidates
        if c.get("name", "").upper().startswith(search_text.upper())
        or c.get("symbolId", "").upper().endswith(search_text.upper())
    ]
    chosen = (exact or candidates)[0]
    _log.info(
        "resolve_contract(%s) → %s (%s)", symbol, chosen.get("id"), chosen.get("description", "")
    )
    return chosen


# ═══════════════════════════════════════════════════════════════════════════════
# Pagination temporelle (cœur du module)
# ═══════════════════════════════════════════════════════════════════════════════


def fetch_bars_paged(
    client: ProjectXClient,
    contract_id: str,
    start_dt: datetime,
    end_dt: datetime,
    timeframe: str = "m15",
    live: bool = False,
    include_partial: bool = False,
    sleep_between_chunks: float = 0.3,
) -> pd.DataFrame:
    """
    Récupère TOUTES les barres entre start_dt et end_dt, en plusieurs
    appels API si nécessaire (max 20 000 bars/req).

    Stratégie :
      • Découpe la fenêtre [start, end] en sous-fenêtres de durée
        _chunk_window_minutes(timeframe), depuis end_dt en remontant le
        temps. C'est cohérent avec l'usage typique « les N derniers jours ».
      • Chaque sous-fenêtre est récupérée avec limit=MAX_BARS_PER_REQUEST.
      • Les DataFrames de chunks sont concaténés puis dédupliqués par index.
      • Petite pause `sleep_between_chunks` pour ne pas mitrailler l'API.

    `start_dt` et `end_dt` doivent être tz-aware UTC ou tz-naive (UTC supposé).
    """
    if end_dt <= start_dt:
        raise ValueError(f"end_dt ({end_dt}) doit être > start_dt ({start_dt})")

    # Normalise en tz-aware UTC pour le calcul, puis on repassera en naïf
    def _to_utc(dt: datetime) -> datetime:
        return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)

    s_utc, e_utc = _to_utc(start_dt), _to_utc(end_dt)
    chunk_minutes = _chunk_window_minutes(timeframe)
    unit, unit_number, _ = _resolve_timeframe(timeframe)

    parts: list[pd.DataFrame] = []
    cursor_end = e_utc
    n_calls = 0

    while cursor_end > s_utc:
        cursor_start = max(s_utc, cursor_end - timedelta(minutes=chunk_minutes))
        n_calls += 1
        _log.info(
            "[%d] fetch %s → %s (tf=%s)",
            n_calls,
            cursor_start.strftime("%Y-%m-%d %H:%M"),
            cursor_end.strftime("%Y-%m-%d %H:%M"),
            timeframe,
        )
        try:
            bars = client.get_bars(
                contract_id=contract_id,
                start_dt=cursor_start.replace(tzinfo=None),
                end_dt=cursor_end.replace(tzinfo=None),
                unit=unit,
                unit_number=unit_number,
                limit=MAX_BARS_PER_REQUEST,
                live=live,
                include_partial=include_partial,
            )
        except Exception as exc:
            _log.error("Chunk %d échec : %s — on continue", n_calls, exc)
            cursor_end = cursor_start
            time.sleep(sleep_between_chunks * 2)
            continue

        df_chunk = _bars_to_df(bars)
        if not df_chunk.empty:
            parts.append(df_chunk)
            _log.debug(
                "  → %d bougies (de %s à %s)", len(df_chunk), df_chunk.index[0], df_chunk.index[-1]
            )
        else:
            _log.warning("  → 0 bougie (chunk %d vide)", n_calls)

        # Si l'API a plafonné à 20 000 sans atteindre `cursor_start`, on
        # recale le curseur sur la première bougie reçue pour ne pas rater
        # la fenêtre. Sinon on déplace simplement à cursor_start.
        if not df_chunk.empty and len(df_chunk) >= MAX_BARS_PER_REQUEST - 1:
            new_end = df_chunk.index[0]
            if new_end <= cursor_start.replace(tzinfo=None):
                cursor_end = cursor_start
            else:
                cursor_end = new_end.to_pydatetime().replace(tzinfo=UTC)
        else:
            cursor_end = cursor_start

        time.sleep(sleep_between_chunks)

    if not parts:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    df = pd.concat(parts)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    # Borne par sécurité au cas où l'API renvoie en dehors de la fenêtre demandée
    df = df.loc[(df.index >= s_utc.replace(tzinfo=None)) & (df.index <= e_utc.replace(tzinfo=None))]
    _log.info("Total : %d bougies sur %d appels API", len(df), n_calls)
    return df


# ═══════════════════════════════════════════════════════════════════════════════
# API haut-niveau
# ═══════════════════════════════════════════════════════════════════════════════


def fetch_history(
    client: ProjectXClient,
    symbol: str,
    days_back: int = 180,
    timeframe: str = "m15",
    end: datetime | None = None,
    live: bool = False,
    include_partial: bool = False,
) -> pd.DataFrame:
    """
    Façade simple : "donne-moi les N derniers jours de <symbol> en <tf>".

    Args:
        client          : ProjectXClient authentifié (client.login() déjà appelé).
        symbol          : alias CATALOG (ex. "MGC") ou raw searchable (ex. "MNQ").
        days_back       : nombre de jours à remonter depuis `end`.
        timeframe       : clé de TIMEFRAMES (m1, m5, m15, h1, h4, d1…).
        end             : datetime de fin (UTC) ; défaut = maintenant.
        live            : True pour la souscription live, False pour sim.
        include_partial : inclure la barre courante en cours de formation.

    Returns:
        DataFrame OHLCV au format projet (index UTC naïf, colonnes
        open/high/low/close/volume).

    Lève ValueError si le symbole ne résout pas, ou si le timeframe est inconnu.
    """
    contract = resolve_contract(client, symbol, live=live)
    if contract is None:
        raise ValueError(f"Aucun contrat trouvé pour '{symbol}' (live={live})")
    contract_id = contract.get("id")
    if not contract_id:
        raise ValueError(f"Contrat sans id pour '{symbol}' : {contract}")

    end = end or datetime.now(UTC)
    start = end - timedelta(days=int(days_back))

    return fetch_bars_paged(
        client=client,
        contract_id=contract_id,
        start_dt=start,
        end_dt=end,
        timeframe=timeframe,
        live=live,
        include_partial=include_partial,
    )


def save_to_csv(
    df: pd.DataFrame,
    ticker: str,
    timeframe: str = "m15",
    output_dir: str = "data",
    overwrite: bool = True,
) -> Path:
    """
    Persiste un DataFrame OHLCV au format compatible `core.data.load_csv` :
        {output_dir}/{TICKER}_data_{timeframe}.csv
    avec colonnes : datetime, open, high, low, close, volume.

    Si overwrite=False et le fichier existe → merge (concat + dédup par index).
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{ticker.upper()}_data_{timeframe.lower()}.csv"

    df_out = df.copy()
    df_out.index.name = "datetime"

    if not overwrite and path.exists():
        from core.data import load_csv

        try:
            existing = load_csv(str(path))
            merged = pd.concat([existing, df_out])
            df_out = merged[~merged.index.duplicated(keep="last")].sort_index()
            _log.info("Merge avec existant : %d → %d bougies", len(existing), len(df_out))
        except Exception as exc:
            _log.warning("Merge échoué (%s) — overwrite forcé", exc)

    df_out.to_csv(path, index=True, date_format="%Y-%m-%d %H:%M:%S")
    _log.info("CSV écrit : %s  (%d lignes)", path, len(df_out))
    return path


# ═══════════════════════════════════════════════════════════════════════════════
# Catalogue / découverte
# ═══════════════════════════════════════════════════════════════════════════════


def list_known_symbols() -> list[tuple[str, str]]:
    """Retourne [(alias, description)] du CATALOG raccourci."""
    return [(k, v["desc"]) for k, v in CATALOG.items()]


def list_available_on_topstepx(
    client: ProjectXClient,
    live: bool = False,
    only_active: bool = True,
) -> pd.DataFrame:
    """
    Catalogue COMPLET retourné par /api/Contract/available.
    DataFrame trié par symbolId avec : id, name, description, tickSize,
    tickValue, symbolId, activeContract.
    """
    contracts = client.list_available_contracts(live=live)
    if only_active:
        contracts = [c for c in contracts if c.get("activeContract")]
    df = pd.DataFrame(contracts)
    if df.empty:
        return df
    cols = ["id", "name", "description", "tickSize", "tickValue", "symbolId", "activeContract"]
    df = df[[c for c in cols if c in df.columns]]
    return df.sort_values(["symbolId", "name"]).reset_index(drop=True)


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════


def _build_client_from_env() -> ProjectXClient:
    """Lit .env et instancie un client connecté."""
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
    user = os.environ.get("PROJECTX_USERNAME", "").strip()
    api = os.environ.get("PROJECTX_API_KEY", "").strip()
    if not user or not api:
        raise SystemExit("PROJECTX_USERNAME et PROJECTX_API_KEY doivent être définis (.env)")
    client = ProjectXClient(user, api)
    if not client.login():
        raise SystemExit("Authentification ProjectX échouée")
    return client


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch historique TopstepX → CSV compatible projet",
    )
    parser.add_argument(
        "--symbol", type=str, default=None, help="Alias CATALOG (MGC, MCL, MNQ…) ou raw"
    )
    parser.add_argument("--timeframe", type=str, default="m15", choices=list(TIMEFRAMES.keys()))
    parser.add_argument(
        "--days", type=int, default=180, help="Nombre de jours à remonter (défaut 180)"
    )
    parser.add_argument(
        "--end", type=str, default=None, help="Fin de fenêtre (YYYY-MM-DD) ; défaut maintenant"
    )
    parser.add_argument("--live", action="store_true", help="Souscription live (défaut : sim)")
    parser.add_argument("--save", action="store_true", help="Écrire data/{TICKER}_data_{tf}.csv")
    parser.add_argument(
        "--ticker",
        type=str,
        default=None,
        help="Nom de ticker à utiliser dans le CSV (défaut : --symbol majuscule)",
    )
    parser.add_argument("--output-dir", type=str, default="data")
    parser.add_argument(
        "--merge", action="store_true", help="Au lieu d'écraser, fusionner avec le CSV existant"
    )
    parser.add_argument(
        "--list", action="store_true", help="Lister le CATALOG raccourci et quitter"
    )
    parser.add_argument(
        "--available",
        action="store_true",
        help="Lister tous les contrats disponibles côté ProjectX",
    )
    parser.add_argument(
        "--log-level", type=str, default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s  %(levelname)-7s  %(name)s : %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.list:
        print("\nCATALOG raccourci (alias → description) :\n")
        for alias, desc in list_known_symbols():
            print(f"  {alias:<6}  {desc}")
        print("\n→ Utilise --symbol <alias> ou n'importe quel symbole searchable.\n")
        return 0

    client = _build_client_from_env()

    if args.available:
        df = list_available_on_topstepx(client, live=args.live)
        if df.empty:
            print("Aucun contrat disponible (live=%s)" % args.live)
            return 1
        with pd.option_context("display.max_rows", None, "display.width", 140):
            print(df.to_string(index=False))
        return 0

    if not args.symbol:
        parser.error("--symbol est requis (ou utilise --list / --available)")

    end_dt = None
    if args.end:
        end_dt = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=UTC)

    df = fetch_history(
        client=client,
        symbol=args.symbol,
        days_back=args.days,
        timeframe=args.timeframe,
        end=end_dt,
        live=args.live,
    )

    if df.empty:
        print(
            f"Aucune barre récupérée pour {args.symbol} {args.timeframe} "
            f"sur les {args.days} derniers jours"
        )
        return 1

    print(f"\n✓ {len(df):,} bougies récupérées  [{df.index.min()} → {df.index.max()}]")
    print(df.head(3).to_string())
    print("...")
    print(df.tail(3).to_string())

    if args.save:
        ticker = (args.ticker or args.symbol).upper()
        path = save_to_csv(
            df,
            ticker=ticker,
            timeframe=args.timeframe,
            output_dir=args.output_dir,
            overwrite=not args.merge,
        )
        print(f"\n→ {path}")
        print(f"  Utilise : python backtest.py --strategy <nom> --csv-dir {args.output_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
