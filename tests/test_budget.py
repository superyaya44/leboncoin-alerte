"""Tests budget : RAM réutilisable (barrettes ≤ 8 Go = 0), prorata, verdict.

Spécification validée avec mainteneur (29/10/2026) :
- cible 64 Go ; DDR4 64 Go neuf = 750 €, DDR5 = 1000 € ;
- coût manquant au prorata (linéaire), budgets 2000 € / 2500 € ;
- type ou RAM indétectable → unknown, jamais de verdict inventé.
"""

from leboncoin_alerte.budget import analyze, ram_type, usable_ram_gb

# ── type + capacité ───────────────────────────────────────────────────────────


def test_type_ddr4_ddr5():
    assert ram_type("PC fixe DDR4 i7") == "ddr4"
    assert ram_type("config ddr5 amd") == "ddr5"
    assert ram_type("PC gamer sans précision") is None


def test_ram_kit_2x16():
    assert usable_ram_gb("32 Go (2x16) DDR4") == 32


def test_ram_barrettes_8go_comptent_zero():
    """Exigence explicite : des unitaires ≤ 8 Go = 0 Go réutilisable."""
    assert usable_ram_gb("32 Go DDR4 (4x8)") == 0
    assert usable_ram_gb("PC 8 Go DDR4") == 0


def test_ram_totale_sans_kit():
    assert usable_ram_gb("64 Go DDR5, RTX 5080") == 64


def test_ram_aucune_detection():
    assert usable_ram_gb("PC fixe, SSD 512 Go, RTX 5080") is None


# ── prorata + verdict ─────────────────────────────────────────────────────────


def test_prorata_ddr4_32go():
    """32 Go manquants sur 64 : 375 € (moitié du kit DDR4 à 750 €)."""
    r = analyze("PC fixe RTX 5080, 32 Go DDR4", 1200)
    assert r["ram_type"] == "ddr4"
    assert r["ram_gb"] == 32
    assert r["ram_cost"] == 375
    assert r["adjusted_price"] == 1575
    assert r["verdict"] == "good_deal"  # 1575 < 2000


def test_prorata_ddr5_16go():
    """2x16 = 32 Go présents, 32 manquants sur DDR5 : 500 € ; budget 2500 €."""
    r = analyze("PC DDR5 RTX 5070 Ti, 2x16 Go", 1900)
    assert r["ram_cost"] == 500
    assert r["adjusted_price"] == 2400
    assert r["verdict"] == "good_deal"  # 2400 < 2500


def test_verdict_over_budget_ddr5():
    r = analyze("PC DDR5 RTX 5080, 16 Go", 2200)
    assert r["ram_cost"] == 750  # 48 Go manquants : 48/64 × 1000
    assert r["adjusted_price"] == 2950
    assert r["verdict"] == "over_budget"  # 2950 ≥ 2500


def test_ram_deja_64go_cout_zero():
    r = analyze("PC complet DDR5 64 Go RTX 5080", 2300)
    assert r["ram_cost"] == 0
    assert r["adjusted_price"] == 2300
    assert r["verdict"] == "good_deal"


def test_type_inconnu_verdict_unknown():
    r = analyze("PC fixe RTX 5080 32 Go de RAM", 1200)
    assert r["ram_type"] is None
    assert r["verdict"] == "unknown"


def test_ram_indetectable_unknown():
    r = analyze("PC fixe RTX 5080", 1500)
    assert r["ram_gb"] is None
    assert r["verdict"] == "unknown"
