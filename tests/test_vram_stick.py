"""Tests VRAM (min_vram_gb) et barrettes (min_stick_gb).

Les deux premiers tests utilisent le TEXTE RÉEL d'annonces Leboncoin qui ont
faussement déclenché une alerte avant ces filtres (26/10/2026) :
- 3295646159 : « RTX 5060 » 405 € → 5060 Ti 8 Go (VRAM insuffisante)
- 3295641976 : « Klevv cras x rgb » 160 € → kit 2x16 (voire 2x8) DDR4
"""

from leboncoin_alerte.filters import _stick_ok, _vram_ok, matches
from leboncoin_alerte.model import Listing


def _listing(title: str, price: int, body: str = "") -> Listing:
    return Listing(listing_id="1", title=title, price=price, url="https://x", body=body)


# ── VRAM ──────────────────────────────────────────────────────────────────────


def test_vram_annonce_reelle_5060ti_8go_refusee():
    """Annonce réelle 3295646159 : VRAM explicite « 8Go » dans le corps."""
    text = (
        "Carte graphique NVIDIA RTX 5060 \n Je mets en vente ma carte graphique "
        "NVIDIA GeForce RTX 5060, idéale pour le gaming.\n"
        "- Type d'accessoire : Carte graphique\n- Marque : NVIDIA\n"
        "- Modèle : GeForce RTX 5060 ti 8Go\nPas de boîte d'origine car vendu avec PC"
    )
    assert _vram_ok(text, 16) is False


def test_vram_explicite_gddr():
    assert _vram_ok("RTX 5090 - 32 Go GDDR7", 16)
    assert _vram_ok("16 Go de VRAM, carte 4080 Super", 16)
    assert not _vram_ok("8 Go VRAM", 16)


def test_vram_collee_au_modele():
    assert _vram_ok("RTX 5090 32 Go", 16)
    assert _vram_ok("RTX-5060Ti 16GB", 16)
    assert not _vram_ok("RTX 5060 Ti 8Go", 16)


def test_vram_defaut_modele_connu():
    # Pas de VRAM explicite, mais une 4090 fait toujours 24 Go.
    assert _vram_ok("Carte RTX 4090 Gigabyte, très bon état", 16)


def test_vram_modele_ambigu_refuse():
    # 5060 Ti existe en 8 ET 16 Go : sans précision, on ne peut pas établir 16.
    assert not _vram_ok("RTX 5060 Ti triple fan", 16)


def test_vram_ram_systeme_ne_compte_pas():
    # 32 Go DDR5 = RAM système, pas VRAM ; modèle 4070 (12 Go) → défaut < 16.
    assert not _vram_ok("RTX 4070 + 32 Go DDR5", 16)


# ── Barrettes (capacité par barrette) ─────────────────────────────────────────


def test_stick_annonce_reelle_kit_2x16_refusee():
    """Annonce réelle 3295641976 : 2x16 puis contradictoirement 2x8 → rejet."""
    text = (
        "Klevv cras x rgb \n Je vends un kit de mémoire RAM haut de gamme KLEVV "
        "CRAS X RGB DDR4 de 32 Go, composé de deux barrettes de 16 Go (2 x 16 Go). "
        "Idéal pour le gaming.\n• Capacité : 16 Go (Kit Dual Channel 2 x8 Go pour "
        "des performances optimales)\n• Type de mémoire : DDR4"
    )
    assert _stick_ok(text, 32) is False


def test_stick_kits_classiques():
    assert not _stick_ok("Corsair Vengeance 32 Go (2x16 Go) DDR5 6000", 32)  # 16/barrette
    # Règle 31/10 : kit 4×16 (total 64) accepté malgré des barrettes de 16.
    assert _stick_ok("Kit 64 Go DDR4 3200 (4x16)", 32)
    assert _stick_ok("64 Go DDR4 (4 x 16 Go)", 32)
    assert _stick_ok("64 Go DDR5 (2x32)", 32)  # barrettes de 32 → OK


def test_stick_monobarelle():
    assert _stick_ok("Barrette 32 Go DDR5 6000 CL30", 32)
    assert _stick_ok("1x48 Go DDR5 ECC", 32)
    assert not _stick_ok("Barrette 16 Go DDR4 3200", 32)


def test_stick_variante_go_x2():
    # "32Go x2" = deux barrettes de 32 Go → conforme.
    assert _stick_ok("32Go x2 DDR4", 32)
    assert not _stick_ok("16Go x2 DDR4", 32)  # deux barrettes de 16 → rejet


def test_stick_ssd_ne_compte_pas():
    # "SSD 512 Go" sans multiplieur : retombe sur la RAM détectée (32 DDR5).
    assert _stick_ok("Barrette 32 Go DDR5, SSD 512 Go", 32)


# ── Intégration : filtre complet via matches() ────────────────────────────────


def test_matches_gpu_reel_5060ti8go_refuse():
    from leboncoin_alerte.config import Search

    search = Search(
        name="gpu",
        url="https://www.leboncoin.fr/recherche?category=83",
        min_price=250,
        min_vram_gb=16,
        regex_any=["rtx[ -]?5060[ -]?ti"],
    )
    listing = _listing(
        "Carte graphique NVIDIA RTX 5060",
        405,
        body="- Modèle : GeForce RTX 5060 ti 8Go\nPas de boîte d'origine car vendu avec PC",
    )
    assert not matches(listing, search)


# ── Marché : scope par recherche + demandes d'achat ──────────────────────────


def test_deal_scope_ram_ignore_gpu_mentionne():
    """Annonce réelle 26/10 : kit RAM dont la description cite la config du
    vendeur (Ryzen 3950X + RTX 3090) — comparée comme GPU (-55 % bidon).
    Avec kinds=("ram",) : comparaison RAM (par barrette), jamais GPU.
    """
    from leboncoin_alerte.market import deal

    lst = Listing(
        listing_id="1",
        title="Memoire vive RAM DDR 4 G.Skill RipJaws 5 Series Noir 64 Go (2 x 32 Go) 3600 MHz CL18",
        price=450,
        url="https://x",
        body="Issue de ma config Ryzen 3950X / RTX 3090. 64 Go DDR4 3600 CL18.",
    )
    refs = {"gpu": {"rtx-3090": 1000}, "ram": {"ddr4-32": 210}}
    # Scopée RAM : réf = 210 €/barrette × 2 = 420 €.
    label, ref, pct = deal(lst, refs, kinds=("ram",))
    assert (label, ref) == ("ddr4-32", 420)
    assert pct == round((420 - 450) / 420 * 100)
    # Sans scope (recherche PC) : priorité GPU, comportement inchangé.
    gpu_deal = deal(lst, refs)
    assert gpu_deal is not None and gpu_deal[0] == "rtx 3090"



def test_exclut_materiel_hs():
    """Annonce réelle 26/10 : RTX 5090 'pour pièces' notifiée à tort."""
    from leboncoin_alerte.config import Search

    search = Search(
        name="gpu",
        url="https://x",
        min_price=250,
        regex_any=["rtx[ -]?5090"],
        text_keywords_exclude=["pour pièces", "en panne", "défectueux"],
    )
    assert not matches(_listing("Carte graphique RTX 5090 pour pièces avec Pcb 🛠️", 400, body="Vendue pour pièces."), search)
    assert not matches(_listing("RTX 4090", 900, body="Carte en panne, ventilateur HS."), search)
    assert matches(_listing("PNY GeForce RTX 5090 32 Go GDDR7 (Neuve)", 2400), search)




def test_vram_adj_au_dela_du_max_modele_est_ram():
    """Bug réel 09/11 : « RTX 2070 SUPER 16Go » à 750 € — la 2070 Super
    n'existe qu'en 8 Go, le 16 Go est la RAM. L'adjacence ne doit PAS
    valoir 16 Go de VRAM, et le verdict tombe sur le défaut modèle : 8."""
    from leboncoin_alerte.filters import _vram_value, vram_verdict

    assert _vram_value("RTX 2070 SUPER 16Go") is None
    assert vram_verdict("RTX 2070 SUPER 16Go", 16) == "reject"


def test_vram_adj_plausible_passe_toujours():
    """Les adjacences CRIBLES restent crues : 5090 32 Go, 3080 Ti 12Gb,
    4080 Super 16GB (sans préfixe rtx)."""
    from leboncoin_alerte.filters import _vram_value

    assert _vram_value("RTX 5090 32 Go") == 32
    assert _vram_value("Zotac 3080 Ti 12Gb") == 12
    assert _vram_value("4080 Super 16GB") == 16
    assert _vram_value("RTX 3060 12 Go") == 12


def test_vram_3070_32go_est_ram_pas_vram():
    """« RTX 3070 32 Go » : 32 Go > max 3070 (8) → RAM. Sans autre précision,
    verdict = défaut modèle 8 Go → reject."""
    from leboncoin_alerte.filters import _vram_value, vram_verdict

    assert _vram_value("PC RTX 3070 32 Go de RAM") is None
    assert vram_verdict("PC RTX 3070 32 Go", 16) == "reject"


def test_vram_contexte_explicite_garde_priorite():
    """Une VRAM explicite en contexte certain prime sur la plausibilité
    d'adjacence : « 4060 8 Go GDDR6 » reste 8 (déjà ≤ max, mais le segment
    explicite doit rester la source n°1)."""
    from leboncoin_alerte.filters import _vram_value

    assert _vram_value("RTX 5090 32 Go GDDR7") == 32


# ── Keyword-spam description vs titre (bug 09/11) ────────────────────────────

def test_deal_titre_prime_sur_description_spam():
    """Bug réel 09/11 : Palit RTX 5080 à 1600 € comparée à la réf 5090 parce
    que la description finit par « RTX5090 / RTX5080 / RTX4090 » (spam de
    mots-clés vendeur). Le modèle du TITRE doit primer."""
    from leboncoin_alerte.market import deal
    from leboncoin_alerte.model import Listing

    refs = {"gpu": {"rtx-5090": 6000, "rtx-5080": 1200}}
    l = Listing(
        listing_id="1", title="Palit RTX 5080 GamingPro 16 Go GDDR7",
        price=1600, url="u",
        body="Je vends ma carte. RTX5090 / RTX5080 / RTX4090",
    )
    assert deal(l, refs) == ("rtx 5080", 1200, -33)  # 1600 vs médiane 1200 : au-dessus


def test_deal_modele_titre_non_suivi_pas_de_fallback_description():
    """Modèle reconnu dans le titre mais non suivi (RTX 4070) : PAS de fallback
    sur la réf d'un modèle spamé en description."""
    from leboncoin_alerte.market import deal
    from leboncoin_alerte.model import Listing

    refs = {"gpu": {"rtx-5090": 6000}}
    l = Listing(
        listing_id="2", title="RTX 4070 très propre", price=400, url="u",
        body="comme une RTX 5090 dans les jeux",
    )
    assert deal(l, refs) is None


def test_deal_sans_modele_dans_titre_fallback_description():
    """Titre sans modèle (« Carte graphique haut de gamme ») : la description
    reste utilisée normalement."""
    from leboncoin_alerte.market import deal
    from leboncoin_alerte.model import Listing

    refs = {"gpu": {"rtx-5090": 6000}}
    l = Listing(
        listing_id="3", title="Carte graphique haut de gamme", price=2000, url="u",
        body="RTX 5090 32 Go impeccable",
    )
    assert deal(l, refs) == ("rtx 5090", 6000, 67)


# ── Annonces de demande / échange (bug 09/11) ────────────────────────────────

def test_demande_achat_rejetee():
    """Bug réel 09/11 : « Cherche carte graphique AMD RX9070xt ou Nvidia
    5070ti » notifiée comme une affaire — c'est une demande d'achat."""
    from leboncoin_alerte.filters import first_reject
    from leboncoin_alerte.model import Listing

    l = Listing(
        listing_id="4", title="Cherche carte graphique AMD RX9070xt ou Nvidia 5070ti",
        price=500, url="u",
    )
    assert first_reject(l, _gpu_search()) == "wanted_ad"


def test_echange_rejete():
    """« Échange Rx 9070xt contre rtx 5070ti ou 5080 » : annonce d'échange,
    pas une vente."""
    from leboncoin_alerte.filters import first_reject
    from leboncoin_alerte.model import Listing

    l = Listing(
        listing_id="5", title="Échange Rx 9070xt contre rtx 5070ti ou 5080 avec rajout si besoin",
        price=950, url="u",
    )
    assert first_reject(l, _gpu_search()) == "wanted_ad"


def test_vente_avec_echange_possible_pas_rejetee():
    """« Pc gamer puissant échange possible » reste une VENTE : l'ancre ^ du
    filtre wanted_ad ne doit pas le toucher."""
    from leboncoin_alerte.filters import first_reject
    from leboncoin_alerte.model import Listing

    l = Listing(
        listing_id="6", title="Pc gamer puissant échange possible RTX 5070 Ti 16Go",
        price=1500, url="u",
    )
    assert first_reject(l, _gpu_search()) != "wanted_ad"


def _gpu_search():
    from leboncoin_alerte.config import Search
    return Search(name="gpu-lyon", url="https://www.leboncoin.fr/")


# ── Arbitrage LLM nocturne au doute (v2, décision mainteneur 09/11) ──────────────

def test_conflit_titre_faux_vs_specs_description():
    """Titre « RTX 5090 » mais la description spécifie une 5080 partout :
    le titre est probablement FAUX. Aucun choix automatique — conflit →
    la revue nocturne (agent LLM) tranche."""
    from leboncoin_alerte.market import MarketConflictError, deal
    from leboncoin_alerte.model import Listing

    refs = {"gpu": {"rtx-5090": 6000, "rtx-5080": 1200}}
    l = Listing(
        listing_id="7", title="RTX 5090 occasion", price=1500, url="u",
        body="GPU RTX 5080 16 Go. La 5080 est intacte, 5080 GamingPro. "
             "RTX5090 / RTX5080 / RTX4090",
    )
    try:
        deal(l, refs)
        raise AssertionError("MarketConflictError attendue")
    except MarketConflictError as e:
        assert "rtx 5080" in e.models and "rtx 5090" in e.models


def test_spam_description_sans_conflit_quand_dominance_claire():
    """Le cas Palit réel : 5080 au titre + spam 5090/4090 une fois en
    description → dominance claire du 5080 titré, pas d'arbitrage inutile."""
    from leboncoin_alerte.market import deal
    from leboncoin_alerte.model import Listing

    refs = {"gpu": {"rtx-5090": 6000, "rtx-5080": 1200, "rtx-4090": 900}}
    l = Listing(
        listing_id="8", title="Palit RTX 5080 GamingPro 16 Go GDDR7",
        price=1600, url="u",
        body="Je vends ma carte. RTX5090 / RTX5080 / RTX4090",
    )
    assert deal(l, refs) == ("rtx 5080", 1200, -33)


def test_conflit_exaequo_sans_titre():
    """Titre sans modèle, description cite 2 modèles suivis à égalité :
    conflit → nocturne."""
    from leboncoin_alerte.market import MarketConflictError, deal
    from leboncoin_alerte.model import Listing

    refs = {"gpu": {"rtx-5090": 6000, "rtx-5080": 1200}}
    l = Listing(
        listing_id="9", title="Carte graphique Nvidia", price=2000, url="u",
        body="RTX 5090 ou RTX 5080 selon arrivage",
    )
    try:
        deal(l, refs)
        raise AssertionError("MarketConflictError attendue")
    except MarketConflictError:
        pass


def test_intention_milieu_de_titre_nest_pas_ignoree():
    """« RTX 5070 Ti, échange possible contre 9070XT » : marqueur en MILIEU
    de titre — pas un rejet bête (ça reste achetable), mais un signal faible
    → wanted_ad_weak → arbitrage LLM nocturne via mark_question."""
    from leboncoin_alerte.filters import first_reject, wanted_ad_weak

    assert wanted_ad_weak("RTX 5070 Ti, échange possible contre 9070XT")
    assert wanted_ad_weak("Vends RTX 4070, cherche aussi 5070Ti")
    assert not wanted_ad_weak("Cherche carte graphique AMD RX9070xt")  # fort → rejet
    assert not wanted_ad_weak("Palit RTX 5080 GamingPro 16 Go")  # rien

    from leboncoin_alerte.model import Listing
    l = Listing(
        listing_id="10", title="RTX 5070 Ti, échange possible contre 9070XT",
        price=1500, url="u",
    )
    # Le filtre dur ne le rejette PAS : c'est le runner qui le met en question.
    assert first_reject(l, _gpu_search()) is None


def test_build_payload_conflit_sans_ligne_marche():
    """Un envoi manuel (send_pending) sur une annonce en conflit part SANS
    comparaison faussée au lieu de crasher."""
    from leboncoin_alerte.market import deal  # noqa: F401
    from leboncoin_alerte.model import Listing
    from leboncoin_alerte.notify.discord import build_payload

    refs = {"gpu": {"rtx-5090": 6000, "rtx-5080": 1200}}
    l = Listing(
        listing_id="11", title="RTX 5090 occasion", price=1500, url="u",
        body="GPU RTX 5080 16 Go, 5080 GamingPro, réf 5080.",
    )
    payload = build_payload("gpu-lyon", l, refs)
    names = [f["name"] for f in payload["embeds"][0]["fields"]]
    assert "📊 vs marché" not in names
