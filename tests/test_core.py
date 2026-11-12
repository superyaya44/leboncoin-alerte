"""Tests filtres + dédupliqution SQLite."""

from datetime import UTC

from leboncoin_alerte.config import Search
from leboncoin_alerte.filters import apply_filters, matches
from leboncoin_alerte.model import Listing
from leboncoin_alerte.store import Store

SEARCH = Search(
    name="ps5",
    url="https://www.leboncoin.fr/recherche?text=ps5",
    max_price=300,
    keywords_include=["ps5"],
    keywords_exclude=["manette"],
)


def _listing(id_: str, title: str, price: int) -> Listing:
    return Listing(listing_id=id_, title=title, price=price, url=f"https://www.leboncoin.fr/ad/{id_}.htm")


def test_filters():
    listings = [
        _listing("1", "PS5 édition digitale", 280),      # OK
        _listing("2", "PS5 trop chère", 500),            # prix > max
        _listing("3", "Xbox Series X", 300),             # pas de keyword
        _listing("4", "Manette PS5", 50),                # exclu
    ]
    kept = apply_filters(listings, SEARCH)
    assert [lst.listing_id for lst in kept] == ["1"]


def test_regex_and_ram():
    gpu = r"rtx[ -]?(30[5-9]\d?|40[5-9]\d?|50\d{2,3})"
    search_with = lambda **kw: Search(
        name="pc", url="https://www.leboncoin.fr/recherche?text=pc", **kw
    )

    # GPU regex seule, sur le texte complet (titre foireux + description claire)
    s = search_with(regex_all=[gpu])
    assert matches(Listing("1", "Pc fixe Gaming", 600, "https://x", body="Config : RTX 3060, i5"), s)
    assert not matches(Listing("1", "PC Gamer GTX 1660", 600, "https://x", body="GTX 1660 Super"), s)

    # RAM numérique : description passe, stockage ne compte pas
    s = search_with(min_ram_gb=32)
    ok = Listing("1", "Pc fixe", 600, "https://x", body="16 Go en titre mais 32 Go de RAM DDR5")
    assert matches(ok, s)
    ko_storage = Listing("1", "PC", 600, "https://x", body="SSD 512 Go SSD, 16 Go de RAM")
    assert not matches(ko_storage, s)
    ko_low = Listing("1", "PC", 600, "https://x", body="32 Go SSD + 16 Go RAM")
    assert not matches(ko_low, s)  # 32 Go = stockage, RAM = 16 Go

    # Combiné GPU + RAM (cas mainteneur)
    s = search_with(regex_all=[gpu], min_ram_gb=32)
    assert matches(
        Listing("1", "Pc fixe Gaming", 650, "https://x",
                body="RTX 4070, Ryzen 7, 32 Go DDR5, SSD 1 To"), s)
    assert not matches(
        Listing("1", "Pc fixe Gaming", 650, "https://x",
                body="RTX 4070, 16 Go, SSD 512 Go de stockage"), s)


def test_store_dedup(tmp_path):
    store = Store(tmp_path / "test.db")
    first = store.unseen_listings("ps5", [_listing("1", "PS5", 280)])
    assert len(first) == 1
    store.mark_seen("ps5", first)
    second = store.unseen_listings("ps5", [_listing("1", "PS5", 280), _listing("2", "PS5 pro", 400)])
    assert [lst.listing_id for lst in second] == ["2"]
    store.mark_seen("ps5", second)
    assert store.known_count("ps5") == 2
    store.close()


def test_detected_specs():
    from leboncoin_alerte.filters import detected_specs
    gpu, ram = detected_specs("RTX 4060, Ryzen 7, 32 Go DDR5, SSD 512 Go SSD 1 To")
    assert gpu == "RTX 4060"
    assert ram == 32  # 512 Go = stockage, ignoré
    assert detected_specs("aucune config") == (None, None)


def test_ram_context_detection():
    from leboncoin_alerte.filters import _ram_value
    # Cas réel LBC : "Stockage : SSD 477go et 300 go hdd" ne doit PAS être de la RAM
    body = ("Je vends cette tour gaming. Processeur i5-13600K. Mémoire vive : 16 Go DDR4. "
            "Carte graphique RTX 3060 Ti (8 Go). Stockage : SSD 477go et 300 go hdd")
    assert _ram_value(body) == 16  # pas 477 !
    assert _ram_value("32go ddr5 - 1to ssd") == 32
    assert _ram_value("RAM 64 GB extensible") == 64
    assert _ram_value("aucune capacité") is None
    # Filtre : ce PC 16 Go ne doit pas matcher min 32
    from leboncoin_alerte.config import Search
    from leboncoin_alerte.filters import matches
    from leboncoin_alerte.model import Listing
    s = Search(name="t", url="https://www.leboncoin.fr/recherche?text=x", min_ram_gb=32)
    assert not matches(Listing("1", "PC fixe", 900, "https://x", body=body), s)


def test_ram_segment_context():
    from leboncoin_alerte.filters import _ram_value, detected_storage
    # Le DDR5 du premier segment ne doit pas contaminer le SSD du second
    assert _ram_value("Ryzen 7, 32 Go DDR5, SSD 512 Go SSD 1 To") == 32
    assert detected_storage("SSD 477go et 300 go hdd") == "477 Go + 300 Go"
    assert detected_storage("1,5 To NVMe / 512 Go SSD") == "1.5 To + 512 Go"


def test_store_lifecycle(tmp_path):
    from leboncoin_alerte.model import Listing
    from leboncoin_alerte.store import Store
    s = Store(tmp_path / "t.db")
    l1 = Listing("1", "PC RTX 4060", 900, "https://x/1")
    l2 = Listing("2", "PC RTX 3060", 700, "https://x/2")
    assert s.unseen_listings("r", [l1, l2]) == [l1, l2]
    s.mark_seen("r", [l1, l2])
    assert s.unseen_listings("r", [l1]) == []
    # succès notification → sent, jamais renvoyée
    s.mark_sent("r", l1)
    assert s.counts()["r"] == {"seen": 1, "sent": 1}
    # l2 disparaît des résultats → old
    s.mark_stale("r", ["1"])
    assert s.counts()["r"] == {"sent": 1, "old": 1}
    # échec d'envoi → unmark → redevient new
    s.mark_seen("r", [l2]); s.mark_stale("r", ["1", "2"])
    s.unmark("r", l2)
    assert s.unseen_listings("r", [l2]) == [l2]


def test_detected_cpu():
    from leboncoin_alerte.filters import detected_cpu
    assert detected_cpu("Intel Core i5-13600K (3.50 GHz)") == "i5-13600K"
    assert detected_cpu("AMD Ryzen 7 5700X, très bon état") == "Ryzen 7 5700X"
    assert detected_cpu("ryzen 5 5600x3d") == "Ryzen 5 5600X3D"
    assert detected_cpu("i9 13900HX laptop") == "i9 13900HX"
    assert detected_cpu("pas de cpu cité") is None


def test_min_disk_gb():
    from leboncoin_alerte.config import Search
    from leboncoin_alerte.filters import matches
    from leboncoin_alerte.model import Listing
    s = Search(name="d", url="https://www.leboncoin.fr/recherche", min_disk_gb=1000)
    ok = Listing("1", "PC fixe", 900, "https://x", body="SSD 1 To NVMe / 32 Go DDR5")
    ko = Listing("2", "PC fixe", 900, "https://x", body="SSD 500 Go, 32 Go DDR5")
    ko_deux_disques = Listing("3", "PC fixe", 900, "https://x",
                              body="SSD 477go et 300 go hdd, 32 Go DDR5")  # 777 < 1000
    ok_somme = Listing("4", "PC fixe", 900, "https://x",
                       body="SSD 500 Go + HDD 1 To, 32 Go DDR5")  # 1524 >= 1000
    assert matches(ok, s)
    assert not matches(ko, s)
    assert not matches(ko_deux_disques, s)
    assert matches(ok_somme, s)


def test_criteres_optionnels():
    """Chaque critère vide est ignoré ; défini, il filtre. Champs structurés : permissif si absents."""
    from datetime import datetime, timedelta

    from leboncoin_alerte.config import Search
    from leboncoin_alerte.filters import matches
    from leboncoin_alerte.model import Listing

    base = {"name": "t", "url": "https://www.leboncoin.fr/recherche"}
    l = Listing(
        "1", "PC Gamer", 900, "https://x", city="Lyon",
        body="RTX 4060, 32 Go DDR5, SSD 1 To",
        seller_type="private", condition="Très bon état",
        computer_type="Fixe", year=2024, shippable=False,
        published_at=datetime.now(UTC).isoformat(),
    )
    # Tous critères vides → passe
    assert matches(l, Search(**base))
    # Chaque critère défini filtre comme attendu
    assert not matches(l, Search(**base, min_price=1000))
    assert matches(l, Search(**base, min_price=500))
    assert not matches(l, Search(**base, cities_exclude=["lyon"]))
    assert matches(l, Search(**base, cities_include=["lyo"]))
    assert not matches(l, Search(**base, cities_include=["nimes"]))
    assert not matches(l, Search(**base, computer_type="Portable"))
    assert matches(l, Search(**base, computer_type="fixe"))
    assert not matches(l, Search(**base, seller_type="pro"))
    assert matches(l, Search(**base, seller_type="particulier"))
    assert not matches(l, Search(**base, condition_include=["neuf"]))
    assert matches(l, Search(**base, condition_include=["très bon"]))
    assert not matches(l, Search(**base, min_year=2025))
    assert matches(l, Search(**base, min_year=2020))
    assert not matches(l, Search(**base, shippable_only=True))
    assert not matches(l, Search(**base, regex_any=["rtx[ -]?30\\d\\d", "gtx"]))
    assert matches(l, Search(**base, regex_any=["rtx[ -]?40\\d\\d", "gtx"]))
    assert not matches(l, Search(**base, text_keywords_exclude=["ssd"]))
    assert matches(l, Search(**base, text_keywords_include=["ddr5"]))
    # Fraîcheur : annonce publiée maintenant → passe ; publiée hier → rejetée à 2h
    vieux = Listing("2", "PC", 900, "https://x",
                    published_at=(datetime.now(UTC) - timedelta(hours=25)).isoformat())
    assert matches(vieux, Search(**base, max_age_hours=48))
    assert not matches(vieux, Search(**base, max_age_hours=2))
    hier = (datetime.now(UTC) - timedelta(days=3)).date().isoformat()
    demain = (datetime.now(UTC) + timedelta(days=1)).date().isoformat()
    assert matches(vieux, Search(**base, published_after=hier))
    assert not matches(vieux, Search(**base, published_after=demain))
    # Champ structuré absent de l'annonce (None) → critère ignoré (permissif)
    nu = Listing("3", "PC", 900, "https://x", body="RTX 4060 32 Go DDR5")
    assert matches(nu, Search(**base, computer_type="Fixe", seller_type="particulier",
                              min_year=2025, shippable_only=True, condition_include=["neuf"]))


def test_seller_code():
    from leboncoin_alerte.config import ConfigError, _seller_code
    assert _seller_code("particulier") == "private"
    assert _seller_code("PRO") == "pro"
    try:
        _seller_code("nimporte")
        raised = False
    except ConfigError:
        raised = True
    assert raised
