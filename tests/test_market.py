"""Tests du référentiel prix marché (market.py)."""


from leboncoin_alerte.market import deal, gpu_ref, ram_ref
from leboncoin_alerte.model import Listing

# Mini référentiel représentatif (le vrai vient de la table retail idealo).
REFS = {
    "gpu": {"rtx-4090": 1450, "rtx-4080-super": 1100, "rtx-5090": 2500},
    "ram": {"ddr5-32": 410, "ddr4-32": 150, "ddr4-64": 259},
}


def _listing(title: str, price: int, body: str = "") -> Listing:
    return Listing(listing_id="1", title=title, price=price, url="https://x", body=body)



def test_gpu_ref_super_avant_nu():
    text = "RTX 4080 Super Gaming OC"
    assert gpu_ref(text, REFS) == ("rtx 4080 super", 1100)


def test_gpu_ref_modele_sans_reference():
    assert gpu_ref("RTX 3060 ti", REFS) is None  # reconnu mais pas dans la table


def test_ram_ref_detection():
    # Références PAR BARRETTE : un kit 2x32 compare le prix au prix marché
    # d'une barrette × 2.
    assert ram_ref("Kit 32 Go DDR5 6000 CL30", REFS) == ("ddr5-32", 410)
    assert ram_ref("64 Go DDR4 3200 (2x32)", REFS) == ("ddr4-32", 300)
    assert ram_ref("16 Go DDR5", REFS) is None  # capacité sans référence


def test_ram_ignore_contexte_stockage():
    # "SSD 512 Go" ne doit pas gonfler la RAM détectée : ici 32 Go DDR5 réels.
    assert ram_ref("32 Go DDR5, SSD 512 Go, RTX 4060", REFS) == ("ddr5-32", 410)


def test_deal_sous_le_marche():
    d = deal(_listing("RTX 4090 Gigabyte", 1100), REFS)
    assert d == ("rtx 4090", 1450, 24)  # (1450-1100)/1450 = 24 % sous le marché


def test_deal_au_dessus():
    d = deal(_listing("RTX 4090", 1600), REFS)
    assert d is not None and d[2] < 0  # +10 % au-dessus


def test_deal_sans_prix_ou_sans_ref():
    assert deal(_listing("RTX 4090", 0), REFS) is None
    assert deal(_listing("Clavier mécanique", 50), REFS) is None
    assert deal(_listing("RTX 4090", 1100), {}) is None


def test_deal_priorite_gpu_sur_ram():
    # Un PC complet : le GPU prime sur la RAM pour la ligne marché.
    d = deal(_listing("PC gamer", 900, body="RTX 4090 + 32 Go DDR5"), REFS)
    assert d is not None and d[0] == "rtx 4090"


def test_deal_gpu_sans_ref_jamais_compare_a_la_ram():
    # Bug réel 27/10 : "PC gamer" 1000 €, RTX 4070 (non suivie), 32 Go DDR4.
    # Le fallback RAM comparait le PC entier à la barrette (réf 150 € → "+567 %").
    pc = _listing("PC gamer", 1000, body="Ryzen 7 5800, RTX 4070, 32 Go DDR4, SSD 1 To")
    assert deal(pc, REFS) is None
    # Une barrette RAM seule sans mention de GPU garde sa ligne marché.
    stick = _listing("32 Go DDR4 3200", 120)
    d = deal(stick, REFS)
    assert d is not None and d[0] == "ddr4-32"
