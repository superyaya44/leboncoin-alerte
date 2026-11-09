"""Tests du verdict VRAM à trois issues + question vendeur (refonte 29/10).

Spécification validée : cascade — VRAM explicite d'abord, défaut modèle
toujours ≥ 16 Go ensuite, modèle ambigu (5060 Ti 8/16) → question vendeur,
modèle inconnu ou absent → rejet.
"""

from leboncoin_alerte.config import Search
from leboncoin_alerte.filters import (
    _vram_ok,
    ambiguous_gpu_model,
    first_reject,
    seller_question,
    vram_verdict,
)
from leboncoin_alerte.model import Listing


def _listing(title: str, body: str = "") -> Listing:
    return Listing(listing_id="1", title=title, price=1000, url="https://x", body=body)


# ── vram_verdict ──────────────────────────────────────────────────────────────


def test_verdict_vram_explicite_ok():
    assert vram_verdict("RTX 5060 Ti 16 Go GDDR7, très bon état", 16) == "ok"


def test_verdict_vram_explicite_insuffisante():
    assert vram_verdict("RTX 5060 ti 8Go sans boîte", 16) == "reject"


def test_verdict_modele_sans_marque():
    """Bug réel 30/10 : « Zotac 3080 Ti 12Gb » sans « rtx » — l'adjacence
    modèle+capacité doit suffire (12 Go explicites, rejet pour min 16)."""
    assert vram_verdict("Zotac 3080 Ti 12Gb très bon état", 16) == "reject"
    assert vram_verdict("PC 5060 Ti 16Go, i7, SSD", 16) == "ok"
    assert vram_verdict("Gainward 5070 Ti 16 Go", 16) == "ok"


def test_vram_cpu_ram_pas_de_faux_positif():
    """Review 30/10 : un CPU suivi de la RAM ne doit PAS valoir de la VRAM —
    sinon un PC sans GPU passe le filtre min_vram_gb."""
    assert vram_verdict("Ryzen 7 5800 32 Go de RAM, pas de carte graphique", 16) == "reject"
    assert vram_verdict("i7 12700 16 Go, PC barebone", 16) == "reject"
    assert vram_verdict("Kit DDR5 6000 32 Go neuf", 16) == "reject"
    # …mais le mixte est bien traité : RTX présent + RAM plus loin.
    assert vram_verdict("RTX 5090 + Ryzen 7 5800, 32 Go de RAM", 16) == "ok"  # 5090 = 32 Go


def test_vram_modele_espaces_multiples():
    """Review 30/10 : « RTX 5060   Ti » (double espace) doit rester AMBIGU
    (question via vram_ask), pas être classé 8 Go en silence."""
    assert vram_verdict("RTX 5060   Ti occasion", 16) == "question"


def test_verdict_modele_toujours_16_ok():
    assert vram_verdict("PC fixe avec RTX 5080, i7, watercooling", 16) == "ok"


def test_verdict_modele_connu_10go_reject():
    assert vram_verdict("PC gamer RTX 3080", 16) == "reject"


def test_verdict_modele_ambigu_question():
    """5060 Ti sans VRAM précisée : question vendeur, pas un rejet."""
    assert vram_verdict("Superbe PC fixe RTX 5060 Ti, SSD 1 To", 16) == "question"


def test_verdict_aucun_modele_reject():
    assert vram_verdict("PC fixe gamer carte NVIDIA récente", 16) == "reject"


def test_verdict_modele_inconnu_reject():
    assert vram_verdict("PC avec RTX 1030 silencieuse", 16) == "reject"


# ── intégration first_reject / vram_ask ──────────────────────────────────────


def _search(**kw) -> Search:
    base: dict = {"name": "t", "url": "https://www.leboncoin.fr/recherche", "min_vram_gb": 16}
    base.update(kw)
    return Search(**base)


def test_first_reject_ambigu_rejete_sans_vram_ask():
    """Comportement historique : ambigu → min_vram_gb."""
    assert first_reject(_listing("PC RTX 5060 Ti"), _search()) == "min_vram_gb"


def test_first_reject_ambigu_passe_avec_vram_ask():
    assert first_reject(_listing("PC RTX 5060 Ti"), _search(vram_ask=True)) is None


def test_first_reject_reject_toujours_rejecte_avec_vram_ask():
    assert first_reject(_listing("PC RTX 3080"), _search(vram_ask=True)) == "min_vram_gb"


def test_comportement_historique_inchange():
    """Sans vram_ask, vram_verdict doit rester équivalent à _vram_ok."""
    for text in (
        "RTX 5090 32 Go",
        "RTX 5070 Ti",
        "RTX 3060 12 Go",
        "RTX 5060",
        "aucun gpu",
    ):
        ok = _vram_ok(text, 16)
        v = vram_verdict(text, 16)
        assert ok == (v == "ok"), text


# ── question vendeur ──────────────────────────────────────────────────────────


def test_ambiguous_model_extrait():
    assert ambiguous_gpu_model("PC fixe RTX 5060 Ti 1 To SSD") == "RTX 5060 Ti"


def test_ambiguous_model_aucun_si_modele_sur():
    assert ambiguous_gpu_model("PC RTX 5090") is None


def test_seller_question_humaine():
    msg = seller_question("RTX 5060 Ti")
    assert msg.startswith("Bonjour")
    assert "5060 Ti" in msg
    assert "16 Go" in msg  # la question porte bien sur la VRAM
