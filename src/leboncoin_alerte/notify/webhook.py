"""Webhook générique : POST JSON simple, pour n'importe quel backend (n8n, etc.)."""

from __future__ import annotations

import httpx

from ..config import WebhookConfig
from ..model import Listing


def send(config: WebhookConfig, search_name: str, listing: Listing) -> bool:
    payload = {
        "search": search_name,
        "listing_id": listing.listing_id,
        "title": listing.title,
        "price": listing.price,
        "url": listing.url,
        "image_url": listing.image_url,
        "city": listing.city,
    }
    try:
        resp = httpx.post(config.url, json=payload, headers=config.headers, timeout=10)
        return 200 <= resp.status_code < 300
    except httpx.HTTPError:
        return False
