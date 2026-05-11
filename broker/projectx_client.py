"""
Client REST synchrone pour l'API TopstepX / ProjectX.

Base URL : https://api.topstepx.com
Auth     : POST /api/Auth/loginKey → Bearer JWT
Docs     : https://gateway.docs.projectx.com/
"""

import logging
import time
from datetime import datetime
from typing import Dict, List, Optional

import requests

_log = logging.getLogger(__name__)

BASE_URL = "https://api.topstepx.com"
_TOKEN_TTL = 23 * 3600  # re-login préventif après 23 h (durée exacte non documentée)


class AuthError(Exception):
    pass


class ProjectXClient:
    """
    Wrapper synchrone autour de l'API REST ProjectX.

    Usage :
        client = ProjectXClient(username, api_key)
        client.login()
        accounts = client.get_accounts()
    """

    def __init__(self, username: str, api_key: str,
                 base_url: str = BASE_URL):
        self.username = username
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._token: Optional[str] = None
        self._token_ts: float = 0.0
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})

    # ─────────────────────────────────────────────────────────────────────
    # Authentification
    # ─────────────────────────────────────────────────────────────────────

    def login(self) -> bool:
        """Authentifie via la clé API et stocke le JWT. Retourne True si OK."""
        url = f"{self.base_url}/api/Auth/loginKey"
        payload = {"userName": self.username, "apiKey": self.api_key}
        try:
            r = self._session.post(url, json=payload, timeout=10)
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            _log.error("login(): erreur réseau ou HTTP : %s", exc)
            return False

        if not data.get("success"):
            _log.error("login(): refusé — %s", data.get("errorMessage", "?"))
            return False

        self._token = data["token"]
        self._token_ts = time.time()
        self._session.headers["Authorization"] = f"Bearer {self._token}"
        _log.info("Authentification ProjectX OK")
        return True

    def _maybe_reauth(self):
        """Re-login si le token est absent ou trop vieux."""
        if self._token is None or (time.time() - self._token_ts) > _TOKEN_TTL:
            if not self.login():
                raise AuthError("Impossible de s'authentifier à ProjectX")

    # ─────────────────────────────────────────────────────────────────────
    # Requête POST générique avec retry sur 401
    # ─────────────────────────────────────────────────────────────────────

    def _post(self, path: str, body: dict, _retry: bool = True) -> dict:
        self._maybe_reauth()
        url = f"{self.base_url}{path}"
        try:
            r = self._session.post(url, json=body, timeout=15)
        except requests.RequestException as exc:
            _log.error("POST %s — réseau : %s", path, exc)
            raise

        if r.status_code == 401 and _retry:
            _log.warning("401 reçu sur %s — re-login", path)
            self._token = None
            self._maybe_reauth()
            r = self._session.post(url, json=body, timeout=15)

        try:
            data = r.json()
        except Exception:
            _log.error("POST %s — réponse non-JSON (%d) : %.200s",
                       path, r.status_code, r.text)
            raise ValueError(f"Réponse non-JSON sur {path}")

        if not data.get("success", True):
            _log.warning("POST %s — errorCode=%s : %s",
                         path, data.get("errorCode", "?"),
                         data.get("errorMessage", ""))
        return data

    # ─────────────────────────────────────────────────────────────────────
    # Compte
    # ─────────────────────────────────────────────────────────────────────

    def get_accounts(self, only_active: bool = True) -> List[Dict]:
        """
        Retourne les comptes du profil.
        Chaque compte : {id, name, balance, canTrade, isVisible}.
        """
        data = self._post("/api/Account/search",
                          {"onlyActiveAccounts": only_active})
        return data.get("accounts", [])

    # ─────────────────────────────────────────────────────────────────────
    # Contrats
    # ─────────────────────────────────────────────────────────────────────

    def search_contract(self, symbol: str,
                        live: bool = True) -> Optional[Dict]:
        """
        Recherche le contrat front-month pour le symbole (ex. "MES", "MNQ").

        Retourne le premier résultat brut de l'API (front-month par défaut).
        Le champ `id` de l'objet retourné est utilisé comme contractId
        dans toutes les requêtes (ordres ET données historiques).

        Exemple de réponse : {"id": "CON.F.US.MES.M26", "name": "MES", ...}
        """
        data = self._post("/api/Contract/search",
                          {"searchText": symbol, "live": live})
        contracts = data.get("contracts", [])
        if not contracts:
            _log.warning("Aucun contrat trouvé pour '%s'", symbol)
            return None
        return contracts[0]

    def search_contracts(self, symbol: str,
                         live: bool = True) -> List[Dict]:
        """
        Recherche TOUS les contrats matchant le texte (max 20 par l'API).
        Utile pour récupérer la liste des échéances d'un même sous-jacent
        ou pour vérifier le mapping symbole → contract_id.

        Format des entrées :
          {id, name, description, tickSize, tickValue,
           activeContract, symbolId}
        """
        data = self._post("/api/Contract/search",
                          {"searchText": symbol, "live": live})
        return data.get("contracts", [])

    def list_available_contracts(self, live: bool = False) -> List[Dict]:
        """
        Liste tous les contrats disponibles à la souscription (sim ou live).
        Endpoint POST /api/Contract/available.

        Réponse (per item) :
          id          : "CON.F.US.MES.M26"
          name        : "MESM6"  (root + month code)
          description : "Micro E-mini S&P 500: June 2026"
          tickSize    : float
          tickValue   : float
          activeContract : bool
          symbolId    : "F.US.MES"
        """
        data = self._post("/api/Contract/available", {"live": live})
        return data.get("contracts", [])

    # ─────────────────────────────────────────────────────────────────────
    # Données historiques — barres OHLCV
    # ─────────────────────────────────────────────────────────────────────

    def get_bars(
        self,
        contract_id,            # str "CON.F.US.MES.M26" ou int selon l'API
        start_dt: datetime,
        end_dt: datetime,
        unit: int = 2,          # 2 = Minute
        unit_number: int = 15,  # 15 minutes
        limit: int = 2000,
        live: bool = True,
        include_partial: bool = False,
    ) -> List[Dict]:
        """
        Fetch bougies OHLCV.

        Retourne une liste de dicts {t, o, h, l, c, v} triée par timestamp
        croissant. La liste peut être vide si aucun data disponible.

        Note : l'API accepte un contractId string ou int selon la version.
        On passe tel quel ; si l'API retourne une erreur, vérifier le format.
        """
        body = {
            "contractId": contract_id,
            "live": live,
            "startTime": start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endTime":   end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "unit": unit,
            "unitNumber": unit_number,
            "limit": limit,
            "includePartialBar": include_partial,
        }
        data = self._post("/api/History/retrieveBars", body)
        bars = data.get("bars", [])
        return sorted(bars, key=lambda b: b.get("t", ""))

    # ─────────────────────────────────────────────────────────────────────
    # Ordres
    # ─────────────────────────────────────────────────────────────────────

    def place_limit_order(
        self,
        account_id: int,
        contract_id: str,
        side: int,          # 0=Buy, 1=Sell
        size: int,
        limit_price: float,
        sl_ticks: int,
        tp_ticks: int,
        custom_tag: Optional[str] = None,
    ) -> Optional[int]:
        """
        Place un ordre LIMIT avec bracket SL (Stop) et TP (Limit).

        Les brackets SL/TP sont exprimés en ticks depuis le prix d'entrée.
        Retourne l'orderId si succès, None sinon.
        """
        body: Dict = {
            "accountId": account_id,
            "contractId": contract_id,
            "type": 1,          # Limit
            "side": side,
            "size": size,
            "limitPrice": limit_price,
            "stopLossBracket":   {"ticks": sl_ticks,  "type": 4},   # Stop
            "takeProfitBracket": {"ticks": tp_ticks, "type": 1},    # Limit
        }
        if custom_tag:
            body["customTag"] = custom_tag[:64]  # troncature sécurité

        data = self._post("/api/Order/place", body)
        if not data.get("success"):
            _log.error("place_limit_order(%s, %s) : %s",
                       contract_id, custom_tag, data.get("errorMessage", "?"))
            return None
        order_id = data.get("orderId")
        _log.info("Ordre placé : id=%s tag=%s %s×%d @ %.4f",
                  order_id, custom_tag, "BUY" if side == 0 else "SELL",
                  size, limit_price)
        return order_id

    def place_market_order(
        self,
        account_id: int,
        contract_id: str,
        side: int,
        size: int,
        custom_tag: Optional[str] = None,
    ) -> Optional[int]:
        """Place un ordre Market (clôture de position). Retourne l'orderId."""
        body: Dict = {
            "accountId": account_id,
            "contractId": contract_id,
            "type": 2,          # Market
            "side": side,
            "size": size,
        }
        if custom_tag:
            body["customTag"] = custom_tag[:64]
        data = self._post("/api/Order/place", body)
        if not data.get("success"):
            _log.error("place_market_order(%s) : %s",
                       contract_id, data.get("errorMessage", "?"))
            return None
        return data.get("orderId")

    def cancel_order(self, account_id: int, order_id: int) -> bool:
        """Annule un ordre ouvert. Retourne True si succès."""
        data = self._post("/api/Order/cancel",
                          {"accountId": account_id, "orderId": order_id})
        return bool(data.get("success"))

    def get_open_orders(self, account_id: int) -> List[Dict]:
        """Retourne les ordres ouverts (statut PENDING ou WORKING)."""
        data = self._post("/api/Order/searchOpen", {"accountId": account_id})
        return data.get("orders", [])

    def get_orders_since(self, account_id: int,
                         start_dt: datetime) -> List[Dict]:
        """Retourne les ordres (historique + ouverts) depuis start_dt."""
        data = self._post("/api/Order/search", {
            "accountId": account_id,
            "startTimestamp": start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
        return data.get("orders", [])

    # ─────────────────────────────────────────────────────────────────────
    # Positions
    # ─────────────────────────────────────────────────────────────────────

    def get_positions(self, account_id: int) -> List[Dict]:
        """
        Retourne les positions ouvertes.
        Chaque position : {id, accountId, contractId, type, size, averagePrice}.
        type = 0 → Long, 1 → Short (à confirmer selon la doc live).
        """
        data = self._post("/api/Position/searchOpen", {"accountId": account_id})
        return data.get("positions", [])

    # ─────────────────────────────────────────────────────────────────────
    # Trades (executions / fills)
    # ─────────────────────────────────────────────────────────────────────

    def get_trades_since(self, account_id: int,
                         start_dt: datetime) -> List[Dict]:
        """
        Retourne les exécutions depuis start_dt.

        Chaque trade : {id, contractId, price, profitAndLoss, fees, side, size,
                        voided, orderId, creationTimestamp}.
        profitAndLoss = None si c'est un aller (opening trade).
        profitAndLoss ≠ None si c'est un retour (closing trade, en dollars).
        """
        data = self._post("/api/Trade/search", {
            "accountId": account_id,
            "startTimestamp": start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
        return data.get("trades", [])
