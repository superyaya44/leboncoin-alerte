"""Webhook Discord (embeds riches).

Format validé par mainteneur (2026-10-20) : image de l'annonce en grand, PAS de pavé
de description, champs synthétiques — Prix / Ville / Publiée / GPU / RAM / Stockage.

Contraintes éprouvées en prod (retour d'expérience ill-backup-flow) :
- User-Agent EXPLICITE obligatoire : Discord est derrière Cloudflare, qui renvoie 403
  aux User-Agents "python-httpx/x" / "Python-urllib/x" sans en-tête personnalisé.
- Jamais d'exception remontée : un échec d'alerte ne doit pas casser le cycle.
- Webhooks : ~5 messages / 2 s → 429 possible en rafale, on réessaie une fois.
"""

from __future__ import annotations

import time

import httpx

from ..config import DiscordConfig
from ..filters import detected_cpu, detected_specs, detected_storage
from ..market import MarketConflictError, deal
from ..model import Listing

USER_AGENT = "leboncoin-alerte/0.1"


def build_payload(
    search_name: str,
    listing: Listing,
    refs: dict[str, dict[str, int]] | None = None,
    kinds: tuple[str, ...] | None = None,
    occasion_keys: set[str] | None = None,
) -> dict:
    """Construit l'embed Discord : config extraite du texte complet de l'annonce.

    Si le référentiel de prix NEUF idealo est fourni et qu'un
    modèle GPU / config RAM est reconnu, un champ affiche l'écart
    en % ; l'embed passe au vert dès 10 % sous le marché.
    """
    text = listing.search_text()
    gpu, ram = detected_specs(text)
    storage = detected_storage(text)
    cpu = detected_cpu(text)

    color = 0xE86A17  # orange Leboncoin
    market_field = None
    try:
        market = deal(listing, refs, kinds) if refs else None
    except MarketConflictError:
        # Modèles contradictoires : pas de ligne marché (l'annonce a été ou
        # sera arbitrée par la revue nocturne) — l'embed part sans comparaison.
        market = None
    if market:
        label, ref, pct = market
        # Modèle arrêté suivi par le scan occasion J-7 ? (clé type "rtx-4090")
        est_occasion = any(
            k.replace("-", " ") in label.lower() for k in (occasion_keys or ())
        )
        if pct >= 0:
            market_str = f"**-{pct} %** sous le marché"
        else:
            market_str = f"**+{-pct} %** au-dessus du marché"
        market_field = {
            "name": "📊 vs occasion (scan ≤7 j)" if est_occasion else "📊 vs neuf (idealo)",
            "value": f"{market_str} (réf. {ref} € — {label})",
            "inline": False,
        }
        if pct >= 10:
            color = 0x2ECC40  # vert : bon plan

    fields = [
        {
            "name": "💰 Prix",
            "value": f"**{listing.price} €**" if listing.price is not None else "?",
            "inline": True,
        },
        {"name": "📍 Ville", "value": listing.city or "?", "inline": True},
        {"name": "🕒 Publiée", "value": listing.published_at or "?", "inline": True},
        {"name": "⚙️ CPU", "value": cpu or "?", "inline": True},
        {"name": "🎮 GPU", "value": gpu or "?", "inline": True},
        {"name": "🧠 RAM", "value": f"{ram} Go" if ram else "?", "inline": True},
        {"name": "💾 Stockage", "value": storage or "?", "inline": True},
    ]
    if market_field:
        fields.append(market_field)
    embed = {
        "title": listing.title[:250],
        "url": listing.url,
        "color": color,
        "fields": fields,
        "footer": {"text": f"Recherche : {search_name} — leboncoin-alerte"},
    }
    if listing.image_url:
        embed["thumbnail"] = {"url": listing.image_url}  # petite image (préférence mainteneur)
    return {"username": "Leboncoin Alerte", "embeds": [embed]}


def send(
    config: DiscordConfig,
    search_name: str,
    listing: Listing,
    refs: dict[str, dict[str, int]] | None = None,
    kinds: tuple[str, ...] | None = None,
    occasion_keys: set[str] | None = None,
) -> bool:
    payload = build_payload(search_name, listing, refs, kinds, occasion_keys)
    for attempt in range(2):
        try:
            resp = httpx.post(
                config.webhook_url,
                json=payload,
                timeout=10,
                headers={"User-Agent": USER_AGENT},
            )
            if resp.status_code in (200, 204):
                return True
            if resp.status_code == 429 and attempt == 0:
                retry_after = float(resp.json().get("retry_after", 1.5))
                time.sleep(min(retry_after, 10) + 0.2)
                continue
            return False
        except httpx.HTTPError:
            return False
    return False
