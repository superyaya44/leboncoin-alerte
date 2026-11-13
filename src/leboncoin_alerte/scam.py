"""Score de vigilance anti-arnaque (EXPÉRIMENTAL, informatif uniquement).

Ne rejette jamais une annonce : ajoute un avertissement dans la notification
quand plusieurs signaux classiques d'arnaque sont réunis. Les signaux viennent du
texte complet (titre + description + attributs) et de l'écart au prix de marché.
"""

from __future__ import annotations

import re

from .model import Listing

# (motif, poids, libellé affiché)
_SIGNALS: list[tuple[re.Pattern, int, str]] = [
    (re.compile(r"western\s*union|mandat\s*cash|transcash|neosurf|(coupon|recharge|carte)\s*pcs\b"
                r"|carte\s*prépayée|coupon\s*recharge|(paiement|payer)\s*en\s*(crypto|bitcoin)", re.IGNORECASE), 45, "paiement non traçable demandé"),
    (re.compile(r"virement\s*(bancaire\s*)?(uniquement|seulement|obligatoire)"
                r"|paiement\s*(par\s*)?virement\s*(uniquement|seulement)", re.IGNORECASE),
     25, "virement uniquement"),
    (re.compile(r"paypal\s*(entre\s*)?(amis|proches|famille)", re.IGNORECASE), 30, "PayPal « entre proches »"),
    (re.compile(r"whats\s*app|telegram|contact(ez|er)[- ]moi\s*(par|sur|au)\s*"
                r"(mail|e-mail|sms|whats)", re.IGNORECASE), 25, "contact hors Leboncoin"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[a-z]{2,}", re.IGNORECASE), 15, "adresse e-mail dans l'annonce"),
    (re.compile(r"(?:\+33|\b0)[67](?:[ .-]?\d{2}){4}\b"), 10, "numéro de téléphone dans l'annonce"),
    (re.compile(r"(je\s*suis|actuellement|résid\w*)\s*(à\s*l'étranger|en\s*mission|expatri\w*)"
                r"|militaire\s*(en\s*mission|muté)|mutation\s*à\s*l'étranger", re.IGNORECASE),
     30, "vendeur « à l'étranger »"),
    (re.compile(r"(envoi|expédition)\s*(uniquement|seulement)|pas\s*de\s*(remise\s*en\s*main"
                r"\s*propre|main\s*propre)|aucune\s*remise", re.IGNORECASE), 15, "envoi uniquement"),
    (re.compile(r"urgent|faut\s*que\s*ça\s*parte|premier\s*arrivé", re.IGNORECASE), 5, "pression « urgent »"),
]

LEVELS = [(60, "élevée"), (30, "moyenne")]


def assess(listing: Listing, market_pct: int | None = None) -> tuple[int, list[str]]:
    """(score 0-100, raisons). market_pct = % sous le prix de marché (positif = moins cher)."""
    text = listing.search_text()
    score = 0
    reasons: list[str] = []
    for pattern, weight, label in _SIGNALS:
        if pattern.search(text):
            score += weight
            reasons.append(label)
    if market_pct is not None and market_pct >= 45:
        score += 35 if market_pct >= 60 else 20
        reasons.append(f"prix {market_pct} % sous le marché")
    return min(score, 100), reasons


def level(score: int) -> str | None:
    """Libellé de vigilance, ou None si rien à signaler."""
    return next((label for threshold, label in LEVELS if score >= threshold), None)
