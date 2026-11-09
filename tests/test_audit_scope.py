# tests/test_audit_scope.py
"""Audit nocturne : un rejet VRAM établi sous le seuil est SÛR (hors scope).

Bug 31/10 : le rapport du matin a proposé une 3080 Ti 12 Go comme affaire
manquée alors que le scope GPU acté est ≥ 16 Go. Le rejet doit compter dans
les « certains », jamais dans la section « à vérifier ».
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from audit_filtered import audit

COLS = (
    "listing_id TEXT PRIMARY KEY, search TEXT, title TEXT, price INTEGER, "
    "url TEXT, first_seen TEXT, notified INTEGER DEFAULT 0, "
    "status TEXT DEFAULT 'seen', sent_at TEXT, search_text TEXT, "
    "reject_reason TEXT, seller_question TEXT, question_status TEXT"
)


def _db(tmp_path, rows):
    db = tmp_path / "listings.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE listings (" + COLS + ")")
    for i, (reason, text) in enumerate(rows):
        con.execute(
            "INSERT INTO listings (listing_id, search, title, price, url, "
            "first_seen, status, search_text, reject_reason) "
            "VALUES (?, 'gpu-lyon', ?, 599, 'https://x', "
            "datetime('now'), 'seen', ?, ?)",
            (f"ad{i}", text[:60], text, reason),
        )
    con.commit()
    con.close()
    return str(db)


def test_vram_explicite_sous_seuil_est_sur(tmp_path):
    """« 3080 Ti 12 Go » : VRAM explicite 12 < 16 → rejet sûr, pas à vérifier."""
    out = audit(_db(tmp_path, [("min_vram_gb", "MSI RTX 3080 Ti 12 Go GDDR6X")]), 1, 15)
    assert "à vérifier : aucune" in out
    assert "hors scope : VRAM 12 Go" in out


def test_modele_toujours_sous_seuil_est_sur(tmp_path):
    """« RTX 3080 » sans capacité : modèle connu 10 Go → rejet sûr."""
    out = audit(_db(tmp_path, [("min_vram_gb", "KFA2 RTX 3080, très bon état")]), 1, 15)
    assert "à vérifier : aucune" in out
    assert "hors scope : modèle toujours < 16 Go" in out


def test_vram_non_detectee_reste_a_verifier(tmp_path):
    """Aucun modèle détecté : raté de parseur possible → reste listée."""
    out = audit(_db(tmp_path, [("min_vram_gb", "Carte graphique haut de gamme")]), 1, 15)
    assert "À VÉRIFIER" in out
    assert "hors scope" not in out


def test_rejet_prix_inchange(tmp_path):
    """min_price reste un rejet sûr compté, non listé."""
    out = audit(_db(tmp_path, [("min_price", "RTX 4090 24 Go à 20 €")]), 1, 15)
    assert "à vérifier : aucune" in out
    assert "prix sous le minimum × 1" in out
