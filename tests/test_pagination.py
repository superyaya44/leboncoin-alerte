"""Tests de la pagination adaptative (triage_page).

Règles validées avec mainteneur (29/10/2026) :
- stop à la première annonce de plus de 3 mois (tri par date décroissante) ;
- stop quand toute la page est déjà connue (régime établi : 1 page/cycle) ;
- plafond dur de pages inchangé (MAX_PAGES).
"""

from datetime import UTC, datetime, timedelta

from leboncoin_alerte.model import Listing
from leboncoin_alerte.search import triage_page


def _lst(id_: str, age_days: float | None) -> Listing:
    published = (
        (datetime.now(UTC) - timedelta(days=age_days)).isoformat() if age_days is not None else None
    )
    return Listing(listing_id=id_, title=f"ad {id_}", price=100, url="https://x", published_at=published)


CUTOFF = datetime.now(UTC) - timedelta(days=90)


def test_page_toutes_vues_stop():
    """Régime établi : page entièrement connue → stop, une seule page chargée."""
    kept, stop = triage_page([_lst("a", 1), _lst("b", 2)], {"a", "b"}, CUTOFF)
    assert len(kept) == 2 and stop is True


def test_page_une_nouvelle_continue():
    kept, stop = triage_page([_lst("a", 1), _lst("b", 2)], {"a"}, CUTOFF)
    assert len(kept) == 2 and stop is False


def test_annonce_trop_vieille_ecartee_et_stop():
    """Annonce > 3 mois : écartée + stop (tout ce qui suit est plus vieux)."""
    kept, stop = triage_page([_lst("a", 1), _lst("b", 120)], set(), CUTOFF)
    assert [l.listing_id for l in kept] == ["a"] and stop is True


def test_date_absente_conservee_sans_stop():
    """published_at absent (annonce sans date) : conservée, pas de stop âge."""
    kept, stop = triage_page([_lst("a", None)], set(), CUTOFF)
    assert len(kept) == 1 and stop is False


def test_page_vide_pas_de_stop_boucle():
    """Page vide : la boucle principale gère (break), triage ne dit pas stop."""
    kept, stop = triage_page([], set(), CUTOFF)
    assert kept == [] and stop is False


def test_melange_vieille_puis_nouvelle():
    """Même si une vieille apparaît au milieu (tri imparfait), on stoppe —
    le stop reste correct : en tri par date le reste est plus vieux."""
    kept, stop = triage_page([_lst("a", 1), _lst("old", 200), _lst("c", 1)], set(), CUTOFF)
    assert [l.listing_id for l in kept] == ["a"] and stop is True
