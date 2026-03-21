"""
Fonctions d'envoi Telegram (texte + images).
"""

import time
from pathlib import Path

import requests

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID


CHAT_ID_FILE = Path(__file__).parent.parent / ".chat_id"


def get_chat_id() -> str:
    """Récupère le chat_id. Si inconnu, attend un /start sur le bot."""
    # Fichier local
    if CHAT_ID_FILE.exists():
        return CHAT_ID_FILE.read_text().strip()

    # Config
    if TELEGRAM_CHAT_ID:
        return str(TELEGRAM_CHAT_ID)

    # Interroger l'API pour les derniers messages
    print("  ⏳ En attente d'un message sur le bot Telegram...")
    print(f"     → Envoie /start à @MyTopStep_bot")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"

    while True:
        resp = requests.get(url, timeout=30)
        data = resp.json()
        if data.get("result"):
            chat_id = str(data["result"][-1]["message"]["chat"]["id"])
            CHAT_ID_FILE.write_text(chat_id)
            print(f"  ✓ Chat ID enregistré : {chat_id}")
            return chat_id

        time.sleep(5)


def send_message(chat_id: str, text: str):
    """Envoie un message texte sur Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, json={
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }, timeout=30)
    if not resp.ok:
        print(f"  [!] Erreur Telegram: {resp.text}")


def send_photo(chat_id: str, photo_path: str, caption: str = ""):
    """Envoie une image sur Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    with open(photo_path, "rb") as f:
        resp = requests.post(url, data={
            "chat_id": chat_id,
            "caption": caption,
        }, files={"photo": f}, timeout=60)
    if not resp.ok:
        print(f"  [!] Erreur Telegram photo: {resp.text}")
