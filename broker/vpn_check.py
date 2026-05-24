"""Garde-fou anti-VPN sur le trafic Topstep.

Refuse le démarrage du daemon si l'IP publique du PC tombe dans la
plage Tailscale 100.64.0.0/10 — ce qui signifie que Tailscale est
configuré en mode `exit-node` (route par défaut tunnelisée). Ce mode
viole l'invariant #8 de la ROADMAP_SOLO (jamais de VPN sur le trafic
Topstep) et peut entraîner une fermeture de compte par la prop firm.

Usage typique :
    from broker.vpn_check import check_no_vpn_on_topstep

    # Au tout début du démarrage du daemon (SessionRunner.run)
    check_no_vpn_on_topstep()  # lève RuntimeError si VPN détecté

Le check est best-effort : si la requête vers ifconfig.me échoue
(pas d'internet, timeout), on ne bloque PAS — c'est le job du runner
de gérer l'absence de réseau. On ne veut surtout pas qu'un faux
positif anti-VPN bloque le démarrage légitime.
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Any

logger = logging.getLogger("broker.vpn_check")

# Plage Tailscale officielle (CGNAT 100.64.0.0/10).
TAILSCALE_RANGE = ipaddress.ip_network("100.64.0.0/10")

# Endpoint public utilisé pour récupérer l'IP publique. ifconfig.me est
# léger (renvoie juste l'IP en text/plain), open-source et hébergé par
# Cloudflare. icanhazip.com et api.ipify.org sont des fallbacks équivalents.
DEFAULT_PUBLIC_IP_URL = "https://ifconfig.me"
DEFAULT_TIMEOUT_S = 5.0


def _fetch_public_ip(url: str = DEFAULT_PUBLIC_IP_URL, timeout: float = DEFAULT_TIMEOUT_S) -> str:
    """Récupère l'IP publique vue depuis l'extérieur. Lève RequestException si KO."""
    import requests  # noqa: PLC0415

    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    return response.text.strip()


def is_ip_in_tailscale_range(ip: str) -> bool:
    """Retourne True si `ip` (string IPv4/IPv6) est dans 100.64.0.0/10."""
    try:
        return ipaddress.ip_address(ip) in TAILSCALE_RANGE
    except ValueError:
        return False


def check_no_vpn_on_topstep(
    fetch_ip_fn: Any = _fetch_public_ip,
    on_violation: Any = None,
) -> str | None:
    """Vérifie que l'IP publique ne tombe pas dans la plage Tailscale.

    Args:
        fetch_ip_fn  : fonction à appeler pour récupérer l'IP (injectable
                       pour tests). Doit retourner une string ou lever.
        on_violation : callable optionnel `(ip, message) -> None` appelé
                       avant de lever RuntimeError. Sert à envoyer une
                       alerte Telegram (best-effort).

    Returns:
        L'IP publique si OK (string), ou None si la résolution a échoué
        (pas de réseau — on laisse passer en best-effort, c'est au runner
        de gérer le no-internet).

    Raises:
        RuntimeError : si l'IP publique est dans la plage Tailscale.
    """
    try:
        public_ip = fetch_ip_fn()
    except Exception as exc:
        # Best-effort : pas de réseau ? on laisse le runner gérer.
        logger.warning(
            "VPN check : impossible de résoudre l'IP publique (%s). "
            "Démarrage autorisé en best-effort — c'est au runner de gérer "
            "l'absence de réseau.",
            exc,
        )
        return None

    if is_ip_in_tailscale_range(public_ip):
        message = (
            f"❌ IP publique {public_ip} dans la plage Tailscale {TAILSCALE_RANGE}. "
            "Tailscale est configuré en mode exit-node — le trafic Topstep "
            "transite par Tailscale, ce qui VIOLE les T&C de la prop firm "
            "(invariant ROADMAP_SOLO #8). Désactive l'exit-node avant de "
            "redémarrer le daemon :\n"
            "    sudo tailscale up --reset\n"
            "(ou désinstalle Tailscale si tu ne l'utilises pas pour le dashboard)."
        )
        logger.error(message)
        if on_violation is not None:
            try:
                on_violation(public_ip, message)
            except Exception as exc:
                logger.warning("on_violation callback a échoué : %s", exc)
        raise RuntimeError(message)

    logger.info("VPN check OK : IP publique %s hors plage Tailscale.", public_ip)
    return public_ip
