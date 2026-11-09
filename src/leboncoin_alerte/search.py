"""Extraction des annonces depuis le JSON __NEXT_DATA__ embarqué par Leboncoin.

Leboncoin est une SPA Next.js : chaque page de résultats embarque le JSON complet
(``script#__NEXT_DATA__`` → props.pageProps.searchData.ads, validé en live le
2026-10-20). On parse ce JSON — plus robuste que des sélecteurs CSS. La page détail
contient de même ``props.pageProps.ad`` avec la description complète (``body``) et
les attributs structurés.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta

from playwright.sync_api import Page

from .browser import human_pause, is_blocked
from .filters import _parse_dt
from .model import Listing

# Pages de résultats parcourues par cycle (35 annonces/page) — anti-ban :
# plafond DUR, jamais dépassé même au premier lancement (backfill progressif).
MAX_PAGES = 10
# Pages détail ouvertes max par cycle (uniquement les NOUVELLES annonces).
MAX_DETAILS_PER_CYCLE = 40
# Âge maximal d'une annonce (jours) : au-delà, elle est ignorée et la
# pagination s'arrête (tri par date décroissante → tout ce qui suit est
# plus vieux). Couvre le stock « dormeur » sans rescanner l'historique.
MAX_AGE_DAYS = 90


class BlockedError(Exception):
    """DataDome / challenge détecté : arrêter le cycle, ne pas insister."""


def _next_data(page: Page) -> dict:
    """Retourne le JSON __NEXT_DATA__ de la page courante."""
    raw = page.eval_on_selector("script#__NEXT_DATA__", "el => el.textContent")
    return json.loads(raw)


def _listing_from_ad(ad: dict) -> Listing | None:
    list_id = ad.get("list_id")
    if not list_id:
        return None
    price = None
    if ad.get("price_cents") is not None:
        price = ad["price_cents"] // 100
    elif isinstance(ad.get("price"), list) and ad["price"]:
        price = ad["price"][0]
    images = ad.get("images") or {}
    return Listing(
        listing_id=str(list_id),
        title=str(ad.get("subject") or "(sans titre)"),
        price=price,
        url=str(ad.get("url") or f"https://www.leboncoin.fr/ad/{list_id}"),
        image_url=images.get("small_url") or images.get("thumb_url"),
        city=ad.get("city") or ad.get("location") or None,
        published_at=ad.get("index_date") or None,
    )


def triage_page(
    page_listings: list[Listing],
    seen_ids: set[str] | None,
    cutoff: datetime,
) -> tuple[list[Listing], bool]:
    """Trie une page de résultats (tri par date décroissante) et décide du stop.

    Retourne (annonces à garder, arrêter la pagination ?) :
    - une annonce plus vieille que ``cutoff`` est écartée ET provoque le stop
      (tout ce qui suit est plus vieux) ;
    - une page dont toutes les annonces sont déjà connues (``seen_ids``) →
      stop : en régime établi, une seule page suffit par cycle ;
    - ``published_at`` absent → conservée, sans déclencher le stop âge.
    """
    seen_ids = seen_ids or set()
    kept: list[Listing] = []
    stop = False
    for lst in page_listings:
        published = _parse_dt(lst.published_at)
        if published is not None and published < cutoff:
            # Tri par date décroissante : tout ce qui suit est plus vieux.
            stop = True
            break
        kept.append(lst)
    if page_listings and all(lst.listing_id in seen_ids for lst in kept):
        stop = True
    return kept, stop


def extract_listings(
    page: Page,
    search_url: str,
    max_pages_cap: int = MAX_PAGES,
    seen_ids: set[str] | None = None,
    max_age_days: int = MAX_AGE_DAYS,
) -> list[Listing]:
    """Parcourt les pages de résultats (≤ max_pages_cap) et retourne les annonces.

    Pagination adaptative : on s'arrête dès que la page courante ne contient
    plus rien d'inédit (annonce déjà vue ou plus vieille que ``max_age_days``).
    En régime établi une seule page est chargée ; le critère d'âge couvre le
    backlog des premières exécutions.
    """
    cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
    page.goto(search_url, wait_until="domcontentloaded", timeout=60_000)
    human_pause(2.0, 4.0)  # laisser la SPA s'hydrater

    if is_blocked(page):
        raise BlockedError(
            "Challenge DataDome détecté. Lance `lbc-alerte login` pour valider la session."
        )

    data = _next_data(page)
    search_data = data["props"]["pageProps"]["searchData"]
    raw = [lst for ad in search_data.get("ads", []) if (lst := _listing_from_ad(ad))]
    listings, stop = triage_page(raw, seen_ids, cutoff)

    # Pages suivantes (35/page). Anti-ban : pause humaine entre chaque page.
    pageno = 1
    max_pages = min(int(search_data.get("max_pages") or 1), max_pages_cap)
    while not stop and pageno < max_pages:
        pageno += 1
        sep = "&" if "?" in search_url else "?"
        page.goto(f"{search_url}{sep}page={pageno}", wait_until="domcontentloaded", timeout=60_000)
        human_pause(2.0, 4.0)
        if is_blocked(page):
            raise BlockedError("Challenge DataDome détecté (pagination).")
        data = _next_data(page)
        search_data = data["props"]["pageProps"]["searchData"]
        more_raw = [lst for ad in search_data.get("ads", []) if (lst := _listing_from_ad(ad))]
        if not more_raw:
            break
        more, stop = triage_page(more_raw, seen_ids, cutoff)
        listings.extend(more)

    # Dédupliquer par ID (une annonce peut apparaître deux fois sur la grille).
    seen: dict[str, Listing] = {}
    for lst in listings:
        seen.setdefault(lst.listing_id, lst)
    return list(seen.values())


# Attributs sans valeur informative pour le filtrage.
_SKIP_ATTRS = {"rating_score", "rating_count", "profile_picture_url"}


def enrich_with_details(page: Page, listing: Listing) -> Listing:
    """Ouvre la page détail et retourne le Listing enrichi (description + attributs).

    À n'appeler QUE pour les nouvelles annonces (dédupliquées) : le JSON de recherche
    ne contient pas la description.
    """
    page.goto(listing.url, wait_until="domcontentloaded", timeout=60_000)
    human_pause(1.5, 3.0)
    if is_blocked(page):
        raise BlockedError("Challenge DataDome détecté (page détail).")
    ad = _next_data(page)["props"]["pageProps"].get("ad") or {}
    body = str(ad.get("body") or "")
    attrs = " ".join(
        f"{a.get('key', '')} {a.get('value_label') or a.get('value', '')}"
        for a in ad.get("attributes", [])
        if a.get("key") not in _SKIP_ATTRS
    )
    # La recherche donne ville/date de publication ; le détail les confirme/complète.
    location = ad.get("location") or {}
    city = (
        location.get("city_label")
        or location.get("city")
        or listing.city
    )
    published_at = ad.get("first_publication_date") or ad.get("index_date") or listing.published_at
    # Champs structurés (présents sur la plupart des annonces catégorie ordinateurs).
    attrs_map = {a.get("key"): a.get("value_label") for a in ad.get("attributes", [])}
    owner = ad.get("owner") or {}
    year_raw = attrs_map.get("electronic_year")
    ship_raw = attrs_map.get("shippable")
    return dataclasses.replace(
        listing,
        body=body,
        attributes_text=attrs,
        city=city,
        published_at=published_at,
        seller_type=owner.get("type"),
        condition=attrs_map.get("condition"),
        computer_type=attrs_map.get("computer_type"),
        year=int(year_raw) if year_raw and str(year_raw).isdigit() else None,
        shippable=None if ship_raw is None else str(ship_raw).lower() == "true",
    )
