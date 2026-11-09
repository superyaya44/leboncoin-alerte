"""Passe occasion multi-sources — parseurs, règle J-7, garde-fous, consensus.

Fixtures = vraies pages du 09/11/2026 (relevé de validation avec mainteneur).
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from leboncoin_alerte.market import occasion_refs
from leboncoin_alerte.occas import (
    consensus,
    parse_flipdeal,
    parse_pcprice,
    parse_racingtech,
)
from leboncoin_alerte.store import Store

FIX = Path(__file__).parent / "fixtures"
AUJ = date(2026, 11, 9)  # date du relevé fixture


def _sources_4090():
    fd = parse_flipdeal((FIX / "flipdeal-4090.html").read_text())
    rt = parse_racingtech((FIX / "racingtech-4090.txt").read_text(), "RTX 4090")
    pc = parse_pcprice((FIX / "pcprice.txt").read_text())
    return fd, rt, pc


def test_parseurs_pages_reelles():
    fd, rt, pc = _sources_4090()
    assert fd == {"source": "flipdeal", "mediane": 1700, "n": 31, "min": 1400, "max": 3000, "date": "2026-11-09"}
    assert rt["mediane"] == 1675 and rt["n"] == 56 and rt["date"] == "2026-11-09"
    assert pc["mediane"] == 2447 and pc["n"] == 97 and pc["date"] == "2026-11-09"
    assert pc["role"] == "plafond"  # eBay = marché séparé, jamais consensus


def test_consensus_deux_sources_proches():
    fd, rt, pc = _sources_4090()
    c = consensus([fd, rt, pc], today=AUJ)
    assert c["status"] == "ok"
    assert c["mediane"] == 1688  # moyenne 1700/1675
    assert pc not in c["sources"]  # plafond hors consensus


def test_regle_j7_source_pourrie_exclue():
    fd, rt, pc = _sources_4090()
    # Zeus-like : données de 3 semaines → exclu même avec bon n
    vieux = {"source": "zeus", "mediane": 1500, "n": 40, "date": "2026-10-20"}
    c = consensus([fd, rt, pc, vieux], today=AUJ)
    assert vieux not in c["sources"]
    assert any("J-7" in e.get("exclu", "") for e in c["exclus"])
    assert c["mediane"] == 1688


def test_regle_j7_toutes_pourries_aucun_consensus():
    fd, rt, pc = _sources_4090()
    c = consensus([fd, rt, pc], today=date(2026, 11, 18))  # J+9
    assert c["status"] == "aucune-source"
    assert "mediane" not in c


def test_n_faible_exclu():
    fd, rt, pc = _sources_4090()
    maigre = {"source": "autre", "mediane": 900, "n": 14, "date": "2026-11-09"}
    c = consensus([fd, rt, pc, maigre], today=AUJ)
    assert maigre not in c["sources"]
    assert c["mediane"] == 1688


def test_divergence_pas_de_consensus():
    fd, rt, pc = _sources_4090()
    # Une 2e source qui diverge de > 25 % → statut divergence, rien d'écrit
    divergente = {"source": "autre", "mediane": 1000, "n": 30, "date": "2026-11-09"}
    c = consensus([fd, rt, pc, divergente], today=AUJ)
    assert c["status"] == "divergence"
    assert "mediane" not in c


def test_store_et_refs_fenetre_j7(tmp_path):
    store = Store(tmp_path / "t.db")
    fd, rt, _ = _sources_4090()
    c = consensus([fd, rt], today=AUJ)
    assert c["status"] == "ok"
    store.record_occasion_pass([{"clef": "rtx-4090", **c}], fetched_at="2026-11-09T03:00:00+00:00")
    # Frais (J+0 côté horodatage simulé) → réference présente
    from datetime import datetime
    from unittest.mock import patch
    with patch("leboncoin_alerte.market.datetime") as dt:
        dt.now.return_value = datetime(2026, 11, 10, 12, 0)
        refs = occasion_refs(store)
    assert refs == {"gpu": {"rtx-4090": 1688}}
    # Règle J-7 : à J+8, la référence expire → retombée sur le neuf idealo
    with patch("leboncoin_alerte.market.datetime") as dt:
        dt.now.return_value = datetime(2026, 11, 17, 12, 0)
        assert occasion_refs(store) == {}


def test_refs_divergence_jamais_utilisee(tmp_path):
    store = Store(tmp_path / "t.db")
    fd, rt, _ = _sources_4090()
    divergente = {"source": "autre", "mediane": 1000, "n": 30, "date": "2026-11-09"}
    c = consensus([fd, rt, divergente], today=AUJ)
    assert c["status"] == "divergence"
    store.record_occasion_pass([{"clef": "rtx-4090", **c}], fetched_at="2026-11-09T03:00:00+00:00")
    from datetime import datetime
    from unittest.mock import patch
    with patch("leboncoin_alerte.market.datetime") as dt:
        dt.now.return_value = datetime(2026, 11, 10, 12, 0)
        assert occasion_refs(store) == {}  # median NULL → ignoré


def test_discord_libelle_occasion_vs_neuf():
    """Le champ marché affiche « vs occasion » pour une clé du scan J-7,
    « vs neuf (idealo) » sinon — et le bon pourcentage."""
    from leboncoin_alerte.model import Listing
    from leboncoin_alerte.notify.discord import build_payload

    listing = Listing(
        listing_id="x", title="RTX 4090 gaming oc", price=1300,
        url="https://exemple.fr/ad/1", city="Mauguio", published_at="2026-11-09",
    )
    refs = {"gpu": {"rtx-4090": 1688}}
    field = lambda payload: next(f for f in payload["embeds"][0]["fields"] if f["name"].startswith("📊"))
    # Clé occasion → libellé occasion, écart vs 1688 €
    f = field(build_payload("gpu-lyon", listing, refs, ("gpu",), {"rtx-4090"}))
    assert f["name"] == "📊 vs occasion (scan ≤7 j)"
    assert "23" in f["value"] and "1688 €" in f["value"]
    # Sans occasion → libellé neuf idealo (réf retail 6759 € par ex.)
    refs_neuf = {"gpu": {"rtx-4090": 6759}}
    f = field(build_payload("gpu-lyon", listing, refs_neuf, ("gpu",)))
    assert f["name"] == "📊 vs neuf (idealo)"


def test_check_arrets_source_zeus(tmp_path):
    """Le marquage Zeus « plus vendue neuve » (data-neuf="0") est la source
    principale ; l'absence idealo (2 passes) corrobore ; la liste occasion
    est TOUJOURS tranchée par mainteneur — le check ne fait que signaler."""
    from leboncoin_alerte.occas import check_arrets

    store = Store(tmp_path / "t.db")
    # 2 passes retail : 5090/4090 présentes, 4080 absente (corroboration)
    for at in ("2026-10-26T02:30:00+00:00", "2026-11-08T02:30:00+00:00"):
        for key, price in (("rtx-5090", 8500), ("rtx-4090", 6759),
                           ("rtx-5080", 1795), ("rtx-4080-super", 1650),
                           ("rtx-5060-ti", 980), ("rtx-5070-ti", 1243)):
            store.conn.execute(
                "INSERT OR REPLACE INTO retail_prices (key, price, n_offers, min_price, max_price, pass_at) "
                "VALUES (?,?,5,1,99999,?)", (key, price, at))
    store.conn.commit()

    zeus = {"rtx-4090": True, "rtx-5090": False, "rtx-4080": True}
    lignes = check_arrets(store, zeus)
    # 4080 : arrêtée chez Zeus ET sans neuf idealo → double raison
    l4080 = next(l for l in lignes if "rtx-4080" in l and not l.startswith("?"))
    assert "Zeus" in l4080 and "idealo" in l4080
    # 4090 déjà dans MODELS et Zeus la dit arrêtée → silence
    assert not any("rtx-4090 ⚠" in l for l in lignes)
    # 5090 toujours vendue neuve → silence
    assert not any("rtx-5090" in l for l in lignes)
    # clé suivie, vendue neuve chez idealo, mais absente de la table Zeus
    assert any(l.startswith("? rtx-5070-ti") for l in lignes)


def test_check_arrets_retrait_et_zeus_seul(tmp_path):
    from leboncoin_alerte.occas import check_arrets

    store = Store(tmp_path / "t.db")
    # 3090 dans MODELS mais Zeus la dit encore vendue neuve → candidat retrait
    zeus = {"rtx-4090": True, "rtx-3090": False}
    lignes = check_arrets(store, zeus)
    assert any("rtx-3090" in l and "retrait" in l for l in lignes)
    # Zeus seul suffit : arrêtée chez Zeus même si idealo l'a encore
    zeus2 = {"rtx-4090": True, "rtx-3090": True, "rtx-4080": True}
    assert any("rtx-4080" in l and l.startswith("⚠") for l in check_arrets(store, zeus2))


def test_parse_zeus_arrets_fixture():
    """Vraie page Zeus du 09/11 (168 modèles) : 4090 arrêtée, 5090 active."""
    from leboncoin_alerte.occas import parse_zeus_arrets

    arrets = parse_zeus_arrets(
        (FIX / "zeus-argus.html").read_text())
    assert arrets["rtx-4090"] is True
    assert arrets["rtx-5090"] is False
    assert arrets["rtx-3090"] is False  # Zeus : encore du neuf (stock rare)
    assert len(arrets) > 25  # clés RTX uniques (la table contient aussi AMD)
