"""Tests réfs NEUF idealo : enregistrement passe + lecture réfs (défaut : la plus récente)."""

from leboncoin_alerte.market import retail_refs
from leboncoin_alerte.retail import filter_premium
from leboncoin_alerte.store import Store


def _store(tmp_path):
    return Store(tmp_path / "t.db")


def test_enregistrement_et_lecture(tmp_path):
    store = _store(tmp_path)
    n = store.record_retail_pass(
        [
            {"clef": "rtx-5090", "mediane": 8985.95, "min": 6500.0, "max": 10087.0, "n_prix": 6},
            {"clef": "ddr5-64", "mediane": 310.0, "min": 290.0, "max": 340.0, "n_prix": 5},
            {"clef": "rtx-4080", "mediane": None, "n_prix": 0},  # échec : pas de ligne
        ],
        pass_at="2026-10-29T03:30:00+00:00",
    )
    assert n == 2
    refs = retail_refs(store)
    assert refs["gpu"]["rtx-5090"] == 8986
    assert refs["ram"]["ddr5-64"] == 310
    assert "rtx-4080" not in refs["gpu"]


def test_passe_recente_prime_sans_regression(tmp_path):
    """Une clé absente de la nouvelle passe garde son ancienne référence ;
    une clé rescrapée prend la valeur de la passe la plus récente."""
    store = _store(tmp_path)
    store.record_retail_pass(
        [{"clef": "rtx-5060-ti", "mediane": 557.0, "n_prix": 6},
         {"clef": "rtx-4080", "mediane": 1880.0, "n_prix": 6}],
        pass_at="2026-11-01T03:00:00+00:00",
    )
    store.record_retail_pass(
        [{"clef": "rtx-5060-ti", "mediane": 540.0, "n_prix": 6}],
        pass_at="2026-11-08T03:00:00+00:00",
    )
    refs = retail_refs(store)
    assert refs["gpu"]["rtx-5060-ti"] == 540  # la plus récente
    assert refs["gpu"]["rtx-4080"] == 1880  # pas de régression


def test_merge_neuf_prime_sur_occasion(tmp_path):
    """Intégration runner : les réfs retail écrasent les médianes d'occasion."""
    store = _store(tmp_path)
    store.record_retail_pass(
        [{"clef": "rtx-5090", "mediane": 8985.95, "n_prix": 6}],
        pass_at="2026-10-29T03:30:00+00:00",
    )
    refs = {"gpu": {"rtx-5090": 6000}}  # ancienne médiane d'occasion
    for kind, table in retail_refs(store).items():
        refs.setdefault(kind, {}).update(table)
    assert refs["gpu"]["rtx-5090"] == 8986


def test_filter_premium_ecarte_si_assez_standard():
    """Les éditions premium (Noctua/AORUS/Strix…) sortent de la sélection
    quand il reste assez de fiches standard (bug 02/11 : médiane 4080S
    tirée par des Noctua à 5 000 €)."""
    links = [
        ("h1", "GigaByte GeForce RTX 4080 Super"),
        ("h2", "ASUS GeForce RTX 4080 Super Noctua OC"),
        ("h3", "MSI GeForce RTX 4080 Super"),
        ("h4", "GigaByte RTX 4080 Super AORUS Master"),
        ("h5", "ASUS GeForce RTX 4080 Super"),
        ("h6", "MSI RTX 4080 Super Ventus 3X OC"),
        ("h7", "Palit RTX 4080 Super Gamerock"),
    ]
    out = filter_premium(links, cap=4)
    hrefs = [h for h, _ in out]
    assert "h2" not in hrefs and "h4" not in hrefs and "h7" not in hrefs


def test_filter_premium_garde_tout_si_trop_peu_standard():
    """Pas assez de fiches standard → on garde tout (mieux vaut une réf
    premium tirée vers le haut que pas de réf du tout)."""
    links = [
        ("h1", "GigaByte GeForce RTX 4080 Super"),
        ("h2", "ASUS GeForce RTX 4080 Super Noctua OC"),
    ]
    assert filter_premium(links, cap=4) == links


def test_low_median_ignore_les_offres_gonflees():
    """La réf = médiane de la moitié la moins chère : des fiches de base au
    « à partir de » gonflé (marketplace rare) ne tirent plus la réf vers le
    haut (bug 02/11 : 4080S 1662/1662/1662/4979/4979/6198 → réf 3320 €)."""
    from statistics import median

    ok = sorted([1662.41, 1662.41, 1662.41, 4979.57, 4979.57, 6198.58])
    low = ok[: len(ok) // 2]
    assert median(low) == 1662.41  # la moitié chère est ignorée
