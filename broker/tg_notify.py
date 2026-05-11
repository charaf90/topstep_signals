#!/usr/bin/env python3
"""
Utilitaire Telegram — envoi de notifications depuis les skills Claude.

Usage CLI :
    python broker/tg_notify.py "Mon message"
    python broker/tg_notify.py --html "<b>Titre</b>\nCorps"

Usage Python :
    from broker.tg_notify import send
    send("<b>Titre</b>\nCorps")
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import requests


def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for candidate in [Path(".env"), Path(__file__).resolve().parent.parent / ".env"]:
        if candidate.exists():
            for line in candidate.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    env[k.strip()] = v.strip()
            break
    return env


def send(message: str, parse_mode: str = "HTML") -> bool:
    """
    Envoie un message au chat Telegram configuré dans .env.
    Retourne True si succès, False sinon (échoue silencieusement).
    """
    env      = _load_env()
    token    = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id  = env.get("TELEGRAM_CHAT_ID",   "").strip()

    if not token or not chat_id:
        print("[tg_notify] Token ou chat_id manquant — message non envoyé",
              file=sys.stderr)
        return False

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": parse_mode},
            timeout=8,
        )
        resp.raise_for_status()
        return True
    except Exception as exc:
        print(f"[tg_notify] Erreur envoi : {exc}", file=sys.stderr)
        return False


def send_code(title: str, body: str) -> bool:
    """Envoie un bloc de texte avec titre gras et corps en <pre>."""
    return send(f"<b>{title}</b>\n\n<pre>{body}</pre>")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print("Usage : python broker/tg_notify.py \"message\"")
        sys.exit(1)

    parse_mode = "HTML"
    if args[0] == "--plain":
        parse_mode = "Markdown"
        args = args[1:]

    msg = " ".join(args).replace("\\n", "\n")
    ok  = send(msg, parse_mode=parse_mode)
    sys.exit(0 if ok else 1)
