#!/usr/bin/env python3
"""Gestion des offres en attente de validation humaine.

Sans argument : liste les offres non envoyées (le rapport quotidien).
--send <id> : envoie l'offre sur Discord (webhook configuré) et la marque
envoyée — c'est le geste de validation, déclenché par un humain uniquement.
--all : envoie toutes les offres en attente.

Usage : uv run python scripts/send_pending.py [--send 12 | --all] [--config config.toml]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from leboncoin_alerte.config import load_config
from leboncoin_alerte.model import Listing
from leboncoin_alerte.notify import discord as discord_notify
from leboncoin_alerte.store import Store


def _listing_like(row, store: Store) -> Listing:
    """Reconstruit un Listing pour le canal Discord.

    La file pending_offers ne stocke pas le texte de l'annonce : sans lui,
    l'embed part avec GPU/RAM/CPU à « ? » et sans comparaison marché
    (incident 03/11 — vidage --all dégradé). On récupère search_text depuis
    la table listings pour que l'embed soit identique à un envoi direct.
    """
    _id, listing_id, search, title, price, url = row[:6]
    text = store.listing_text(search, listing_id) or ""
    return Listing(listing_id=listing_id, title=title or "", price=price or 0, url=url or "", body=text)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--send", type=int, metavar="ID")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--config", default="config.toml")
    args = ap.parse_args()

    config = load_config(Path(args.config))
    store = Store(config.data_dir / "listings.db")

    if (args.send or args.all) and not (
        config.notify.discord.enabled and config.notify.discord.webhook_url
    ):
        # Review 30/10 : la validation ne route QUE vers Discord — sans
        # webhook actif, toutes les validations échoueraient en boucle.
        print("notify.discord doit être activé (webhook_url) pour --send.")
        return 2

    if not args.send and not args.all:
        rows = store.pending_list()
        if not rows:
            print("Aucune offre en attente.")
            return 0
        print(f"**{len(rows)} offre(s) en attente de validation**\n")
        for row in rows:
            oid, _lid, search, title, price, url, context, created, _sent = row
            print(f"#{oid} [{search}] {title[:60]} — {price} € ({context or ''})")
            print(f"   {url} (en attente depuis {created[:16]})")
        print("\nPour valider : uv run python scripts/send_pending.py --send <id>")
        return 0

    targets = []
    if args.send:
        row = next((r for r in store.pending_list() if r[0] == args.send), None)
        if not row:
            print(f"Pas d'offre # {args.send} en attente.")
            return 1
        targets = [row]
    else:  # --all
        targets = store.pending_list()

    ok = 0
    for row in targets:
        oid, _lid, search = row[0], row[1], row[2]
        listing = _listing_like(row, store)
        sent = discord_notify.send(
            config.notify.discord, search, listing, None, None
        )
        if sent:
            store.mark_pending_sent(oid)
            ok += 1
            print(f"#{oid} ENVOYÉ : {listing.title}")
        else:
            print(f"#{oid} ÉCHEC d'envoi (non marqué, retente)")
    print(f"{ok}/{len(targets)} offre(s) envoyée(s).")
    return 0 if ok == len(targets) else 1


if __name__ == "__main__":
    raise SystemExit(main())
