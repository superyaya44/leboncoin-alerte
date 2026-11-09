"""Telegram via Bot API (sendMessage)."""

from __future__ import annotations

import httpx

from ..config import TelegramConfig
from ..model import Listing


def send(config: TelegramConfig, search_name: str, listing: Listing) -> bool:
    text = f"🔔 *Nouvelle annonce* — {search_name}\n{listing.format()}"
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{config.bot_token}/sendMessage",
            json={
                "chat_id": config.chat_id,
                "text": text[:3900],  # limite Telegram : 4096 caractères
                "parse_mode": "Markdown",
                "disable_web_page_preview": False,
            },
            timeout=10,
        )
        return resp.status_code == 200
    except httpx.HTTPError:
        return False
