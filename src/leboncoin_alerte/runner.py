"""Boucle d'exécution : un cycle = toutes les recherches une fois.

Flow par recherche :
1. Extraction JSON de toutes les annonces des pages de résultats.
2. Dédupliqution SQLite (les déjà-vues ne sont JAMAIS re-notifiées ni re-téléchargées).
3. Pour les NOUVELLES uniquement : lecture de la page détail (description + attributs).
4. Filtrage sur titre + description + attributs.
5. Notification des annonces retenues.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from .browser import BrowserSession, human_pause
from .config import Config
from .filters import (
    ambiguous_gpu_model,
    first_reject,
    seller_question,
    vram_verdict,
    wanted_ad_weak,
)
from .market import MarketConflictError, deal, occasion_refs, retail_refs
from .model import Listing
from .notify.dispatcher import notify_all
from .schedule import is_due
from .search import MAX_DETAILS_PER_CYCLE, BlockedError, enrich_with_details, extract_listings
from .store import Store


def _price_changes(store: Store, search, listings: list[Listing]) -> list[tuple[Listing, int]]:
    """Compare les prix aux prix connus. Retourne les baisses à renotifier.

    - toujours : historique des prix (alimente la courbe de l'interface) ;
    - si price_drop_percent : annonce déjà envoyée qui baisse d'au moins ce % →
      renotifiée ; annonce rejetée UNIQUEMENT pour prix trop élevé qui repasse
      sous max_price → oubliée, donc réanalysée en entier ce cycle (filtres
      complets : pas d'envoi « à peu près sûr »).
    """
    known = store.known_prices(search.name)
    drops: list[tuple[Listing, int]] = []
    for lst in listings:
        k = known.get(lst.listing_id)
        if not k or lst.price is None or k[0] is None or lst.price == k[0]:
            continue
        old, status, reason = k
        store.update_price(search.name, lst.listing_id, old, lst.price)
        if search.price_drop_percent is None or lst.price > old:
            continue
        pct = round((old - lst.price) * 100 / old) if old else 0
        if status == "sent" and pct >= search.price_drop_percent:
            drops.append((lst, old))
        elif (status == "seen" and reason == "max_price" and search.max_price is not None
              and lst.price <= search.max_price):
            store.unmark(search.name, lst)
            print(f"[{search.name}] repasse sous le prix max ({old} → {lst.price} €), "
                  f"réanalysée : {lst.title}")
    return drops


def _notify_drop(config: Config, store: Store, search, listing: Listing, old: int,
                 refs, occ_keys) -> int:
    pct = round((old - listing.price) * 100 / old)
    titled = replace(listing, title=f"📉 -{pct} % ({old} → {listing.price} €) · {listing.title}")
    if config.hold_notifications:
        store.add_pending(search.name, titled, context=f"baisse {old}→{listing.price}")
        print(f"  → baisse de prix en attente de validation : {listing.title}")
        return 0
    results = notify_all(config, search.name, titled, refs, occ_keys)
    ok = [c for c, success in results.items() if success]
    print(f"  → baisse de prix {old} → {listing.price} € "
          f"({', '.join(ok) or 'ÉCHEC ENVOI'}) : {listing.title}")
    human_pause(1.0, 2.0)
    return 1 if ok else 0


def run_cycle(config: Config, headless: bool = True, force: bool = False) -> int:
    """Exécute un cycle complet. Retourne le nombre de nouvelles annonces notifiées."""
    store = Store(config.data_dir / "listings.db")
    # Références de prix : NEUF idealo uniquement (décision mainteneur 09/11 —
    # le système de prix d'occasion auto-appris a été démonté : il se polluait
    # par construction, cartes mortes « pour pièces » enregistrées comme des
    # ventes normales → réf 4090 à ~470 € contre ~1600 € réels).
    # Rafraîchissement hebdo par le cron lbc-retail-hebdo (retail.py).
    refs = retail_refs(store)
    if refs:
        print(f"[market] {sum(len(t) for t in refs.values())} références NEUF idealo actives")
    # Occasion (modèles arrêtés, scan multi-sources ≤ 7 j — règle J-7) :
    # PRIME sur le neuf idealo, référence morte pour ces modèles (stock
    # raréfié à prix gonflé). Clés transmises aux notifs pour le libellé.
    occ_keys: set[str] = set()
    occ = occasion_refs(store)
    if occ:
        for kind, table in occ.items():
            refs.setdefault(kind, {}).update(table)
        occ_keys = set(next(iter(occ.values())))
        print(f"[market] occasion J-7 : {', '.join(sorted(next(iter(occ.values()))))}")
    # Planification par alerte : seules les recherches « dues » tournent ;
    # aucune due → pas de navigateur du tout.
    now = datetime.now(UTC)
    last_runs = store.last_runs()
    due = [s for s in config.searches
           if force or is_due(s, now, last_runs.get(s.name), config.interval_minutes)]
    if not due:
        print("[planning] aucune alerte à lancer maintenant")
        store.close()
        return 0
    if len(due) < len(config.searches):
        print(f"[planning] {len(due)}/{len(config.searches)} alerte(s) à lancer : "
              f"{', '.join(s.name for s in due)}")
    session = BrowserSession(config.data_dir / "profile", headless=headless, cdp_url=config.cdp_url)
    notified = 0

    with session.page() as page:
        for search in due:
            print(f"[{search.name}] recherche…")
            try:
                listings = extract_listings(
                    page,
                    search.url,
                    max_pages_cap=search.pages,
                    seen_ids=store.known_ids(search.name),
                )
            except BlockedError as exc:
                print(f"[{search.name}] BLOQUÉ : {exc}")
                break  # règle anti-ban : on arrête le cycle, on n'insiste pas
            print(f"[{search.name}] {len(listings)} annonces extraites")
            # Annonces disparues des résultats = dépubliées/vendues → 'old'.
            store.mark_stale(search.name, [lst.listing_id for lst in listings])

            # Dédupliqution AVANT tout : on ne charge le détail que des nouvelles.
            # On ne marque "vues" QUE celles analysées ce cycle (plafond) —
            # les autres restent nouvelles et seront analysées au prochain cycle.
            drops = _price_changes(store, search, listings)
            fresh_all = store.unseen_listings(search.name, listings)
            fresh = fresh_all[:MAX_DETAILS_PER_CYCLE]
            print(f"[{search.name}] {len(fresh_all)} nouvelles annonces, {len(fresh)} analysées ce cycle")

            kept = []
            rejects: dict[str, str] = {}
            questions = 0
            for lst in fresh:
                try:
                    enriched = enrich_with_details(page, lst)
                except BlockedError as exc:
                    print(f"[{search.name}] BLOQUÉ (détail) : {exc}")
                    break
                reason = first_reject(enriched, search)
                if reason is None and wanted_ad_weak(enriched.title):
                    # Intention vente/demande ambiguë (marqueur en milieu de
                    # titre) : arbitrage LLM nocturne, pas de rejet ni d'envoi.
                    store.mark_question(
                        search.name,
                        enriched,
                        "À trancher (revue nocturne) : le titre évoque une "
                        "recherche/un échange — déterminer si c'est une vraie "
                        "vente avant de notifier.",
                    )
                    questions += 1
                    print(f"  → à trancher (nocturne, vente vs demande) : {enriched.title}")
                elif reason is None and (
                    search.vram_ask
                    and search.min_vram_gb is not None
                    and vram_verdict(enriched.search_text(), search.min_vram_gb) == "question"
                ):
                    # Modèle ambigu (8/16 Go) : pas un rejet, une question
                    # vendeur — envoyée uniquement sur action humaine.
                    text = enriched.search_text()
                    model = ambiguous_gpu_model(text) or "carte graphique"
                    store.mark_question(search.name, enriched, seller_question(model))
                    questions += 1
                    print(f"  → question vendeur ({model}) : {enriched.title}")
                elif reason is None:
                    try:
                        # Conflit de modèles GPU (titre vs description) :
                        # arbitrage LLM nocturne, pas de comparaison faussée.
                        deal(enriched, refs, (search.market_kind,) if search.market_kind else None)
                        kept.append(enriched)
                    except MarketConflictError as exc:
                        store.mark_question(
                            search.name,
                            enriched,
                            f"À trancher (revue nocturne) : {exc} — identifier la "
                            "carte réellement vendue (titre vs description) puis "
                            "comparer à la bonne référence.",
                        )
                        questions += 1
                        print(f"  → à trancher (nocturne, conflit modèle) : {enriched.title}")
                else:
                    rejects[lst.listing_id] = reason
            if questions:
                print(f"[{search.name}] {questions} annonce(s) en attente de question vendeur")
            store.mark_seen(search.name, fresh, rejects)
            skipped = len(fresh_all) - len(fresh)
            if skipped > 0:
                print(f"[{search.name}] {skipped} nouvelles restantes, analysées au prochain cycle")

            for listing in kept:
                if config.hold_notifications:
                    # Boucle de validation humaine (refonte 29/10) : pas
                    # d'envoi direct — le rapport nocturne liste l'offre et
                    # send_pending.py --send <id> la pousse après validation.
                    store.add_pending(search.name, listing, context=f"prix={listing.price}")
                    store.mark_sent(search.name, listing)  # pas re-scan : elle est en file
                    print(f"  → en attente de validation : {listing.title}")
                    human_pause(0.5, 1.0)
                    continue
                results = notify_all(config, search.name, listing, refs, occ_keys)
                ok = [canal for canal, success in results.items() if success]
                if ok:
                    store.mark_sent(search.name, listing)
                    print(f"  → notifié ({', '.join(ok)}) : {listing.title}")
                else:
                    # Échec de tous les canaux : on redevient "new" pour
                    # réessayer au prochain cycle — aucune occasion perdue.
                    store.unmark(search.name, listing)
                    print(f"  → ÉCHEC ENVOI (réessayé au prochain cycle) : {listing.title}")
                notified += 1
                human_pause(1.0, 2.0)  # anti rate-limit Discord (~5 msgs / 2 s)

            for listing, old_price in drops:
                notified += _notify_drop(config, store, search, listing, old_price, refs, occ_keys)
            store.mark_run(search.name, now)
            human_pause(3.0, 7.0)  # espacer les recherches entre elles

    store.close()
    return notified
