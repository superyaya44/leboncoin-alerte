"""Tests pending_offers : file d'attente, dédoublonnage, marquage envoyé."""

from leboncoin_alerte.model import Listing
from leboncoin_alerte.store import Store


def _listing(id_: str = "1", price: int = 1200) -> Listing:
    return Listing(
        listing_id=id_, title=f"PC RTX 5080 {id_}", price=price, url=f"https://x/{id_}"
    )


def test_file_attente_dedoublonnee(tmp_path):
    store = Store(tmp_path / "t.db")
    assert store.add_pending("pc-fixe", _listing("a"), context="prix=1200") is True
    assert store.add_pending("pc-fixe", _listing("a")) is False  # pas de doublon
    rows = store.pending_list()
    assert len(rows) == 1
    assert rows[0][3].startswith("PC RTX 5080")
    assert rows[0][6] == "prix=1200"
    assert rows[0][8] is None  # sent_at NULL = en attente


def test_marquage_envoye_et_relis(tmp_path):
    store = Store(tmp_path / "t.db")
    store.add_pending("pc-fixe", _listing("a"))
    oid = store.pending_list()[0][0]
    assert store.mark_pending_sent(oid) is True
    assert store.mark_pending_sent(oid) is False  # déjà envoyé
    assert store.pending_list() == []  # plus rien en attente
    all_rows = store.pending_list(only_unsent=False)
    assert len(all_rows) == 1 and all_rows[0][8] is not None


def test_offres_independantes_par_recherche(tmp_path):
    """La même annonce vue par deux recherches = deux entrées distinctes."""
    store = Store(tmp_path / "t.db")
    store.add_pending("gpu", _listing("a"))
    store.add_pending("pc-fixe", _listing("a"))
    assert len(store.pending_list()) == 2


def test_listing_text_retrouve_le_texte_stocke(tmp_path):
    """Incident 03/11 : le vidage de la file partait sans texte → embeds « ? ».

    listing_text() doit restituer le search_text de la table listings pour
    que send_pending.py reconstruise un embed aussi riche qu'un envoi direct.
    """
    store = Store(tmp_path / "t.db")
    listing = Listing(
        listing_id="a",
        title="PC Gamer",
        price=800,
        url="https://x/a",
        body="RTX 4080 Super 16 Go, 64 Go DDR5, i9",
    )
    store.mark_seen("pc-fixe", [listing], {})  # stocke search_text
    assert "RTX 4080" in (store.listing_text("pc-fixe", "a") or "")


def test_listing_text_absent_retourne_none(tmp_path):
    store = Store(tmp_path / "t.db")
    assert store.listing_text("pc-fixe", "inconnu") is None
