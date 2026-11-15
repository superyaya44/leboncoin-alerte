"""Dispatch des notifications vers tous les canaux activés."""

from __future__ import annotations

from datetime import UTC, datetime

from ..config import Config
from ..model import Listing
from . import discord, email_smtp, telegram, webhook


def notify_all(
    config: Config,
    search_name: str,
    listing: Listing,
    refs: dict[str, dict[str, int]] | None = None,
    occasion_keys: set[str] | None = None,
) -> dict[str, bool]:
    """Envoie l'annonce sur chaque canal activé. Retourne {canal: succès}.

    refs : référentiel prix → champ marché dans l'embed Discord.
    occasion_keys : clés (ex. "rtx-4090") dont la réf est le marché OCCASION
    (scan J-7) — le libellé du champ devient « vs occasion » au lieu de
    « vs neuf (idealo) ». Rechargé à chaque cycle par runner.run_cycle.
    """
    results: dict[str, bool] = {}
    n = config.notify
    # La comparaison marché est scopée à la recherche : une annonce RAM dont
    # la description mentionne une RTX reste une annonce RAM.
    search = next((s for s in config.searches if s.name == search_name), None)
    kinds = (search.market_kind,) if search and search.market_kind else None
    if n.discord.enabled:
        results["discord"] = discord.send(
            n.discord, search_name, listing, refs, kinds, occasion_keys, config.ui_url)
    if n.email.enabled:
        results["email"] = email_smtp.send(n.email, search_name, listing)
    if n.telegram.enabled:
        results["telegram"] = telegram.send(n.telegram, search_name, listing)
    if n.webhook.enabled:
        results["webhook"] = webhook.send(n.webhook, search_name, listing)
    return results


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")


def test_channels(config: Config) -> None:
    """Envoie une fausse annonce de test sur chaque canal activé (lbc-alerte notify-test)."""
    refs = {}  # test de canal : pas de ligne marché nécessaire
    fake = Listing(
        listing_id="0",
        title="[TEST] Annonce de vérification",
        price=42,
        url="https://www.leboncoin.fr/",
        city="Testville",
        published_at="2026-10-20 20:00:00",
        body="Processeur Intel Core i5-13600K, RTX 4060, 32 Go DDR5, SSD 1 To.",
    )
    print(f"Envoi de test ({_now()}) sur : {', '.join(config.notify.enabled_channels())}")
    for search in config.searches[:1]:
        results = notify_all(config, search.name, fake, refs)
        for canal, ok in results.items():
            print(f"  {canal:<10} {'OK' if ok else 'ÉCHEC'}")
    print("Vérifie la réception côté serveur si tout est OK.")
