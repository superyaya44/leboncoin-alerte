"""Score de vigilance anti-arnaque (informatif)."""

from leboncoin_alerte.model import Listing
from leboncoin_alerte.notify.discord import build_payload
from leboncoin_alerte.scam import assess, level


def lst(body, title="RTX 4090 neuve"):
    return Listing("1", title, 600, "https://x/1", body=body)


def test_clean_listing_has_no_warning():
    score, reasons = assess(lst("Très bon état, facture, remise en main propre à Nantes."))
    assert score == 0 and reasons == [] and level(score) is None


def test_classic_scam_is_flagged_high():
    body = ("Je suis actuellement à l'étranger, envoi uniquement. Paiement par virement "
            "uniquement, contactez-moi par mail : vendeur.rtx@gmail.com")
    score, reasons = assess(lst(body), market_pct=62)
    assert level(score) == "élevée"
    assert "vendeur « à l'étranger »" in reasons and "prix 62 % sous le marché" in reasons


def test_single_weak_signal_stays_silent():
    score, _ = assess(lst("Urgent, déménagement."))
    assert level(score) is None


def test_discord_field_only_when_risky():
    clean = build_payload("velo", lst("Bon état"))["embeds"][0]["fields"]
    assert not any("Vigilance" in f["name"] for f in clean)
    risky = build_payload("velo", lst("Paiement Western Union, WhatsApp uniquement"))
    assert any("Vigilance" in f["name"] for f in risky["embeds"][0]["fields"])
