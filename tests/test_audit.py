"""Tests audit : raison de rejet persistée + script scripts/audit_filtered.py."""

import subprocess
import sys
from datetime import UTC, datetime, timedelta

from leboncoin_alerte.config import Search
from leboncoin_alerte.filters import first_reject, matches
from leboncoin_alerte.model import Listing
from leboncoin_alerte.store import Store

SEARCH = Search(
    name="gpu",
    url="https://www.leboncoin.fr/recherche?text=rtx",
    min_price=250,
    min_vram_gb=16,
    regex_any=[r"rtx[ -]?50[6-9]"],
)


def _listing(id_: str, title: str, price: int, body: str = "") -> Listing:
    return Listing(listing_id=id_, title=title, price=price,
                   url=f"https://www.leboncoin.fr/ad/{id_}.htm", body=body)


def test_first_reject_labels():
    # Prix sous le minimum : premier critère évalué.
    assert first_reject(_listing("1", "RTX 5080", 100), SEARCH) == "min_price"
    # Modèle visé mais VRAM insuffisante : rejet VRAM, pas regex.
    assert first_reject(_listing("2", "RTX 5060 Ti 8 Go", 450), SEARCH) == "min_vram_gb"
    # Modèle non visé (regex_any échoue) mais VRAM suffisante : "3090 24 Go".
    assert first_reject(_listing("3", "RTX 3090 24 Go GDDR6X", 700), SEARCH) == "regex_any"
    # Tout passe : None.
    ok = _listing("4", "RTX 5080 16 Go GDDR7", 900)
    assert first_reject(ok, SEARCH) is None


def test_first_reject_matches_equivalence():
    cases = [
        _listing("1", "RTX 5080", 100),
        _listing("2", "RTX 5060 Ti 8 Go", 450),
        _listing("3", "RTX 3060 12 Go", 380),
        _listing("4", "RTX 5080 16 Go GDDR7", 900),
    ]
    for lst in cases:
        assert matches(lst, SEARCH) == (first_reject(lst, SEARCH) is None)


def test_store_persists_search_text_and_reason(tmp_path):
    store = Store(tmp_path / "t.db")
    kept = _listing("1", "RTX 5080 16 Go GDDR7", 900, body="Carte parfaite, 16 Go de VRAM.")
    rejected = _listing("2", "RTX 3060", 180)
    store.mark_seen("gpu", [kept, rejected], {"2": "min_price"})
    rows = {
        r[0]: r[1:]
        for r in store.conn.execute("SELECT listing_id, search_text, reject_reason FROM listings")
    }
    assert rows["1"][1] is None  # retenue : pas de raison
    assert "16 Go de VRAM" in rows["1"][0]  # description persistée
    assert rows["2"][1] == "min_price"
    store.close()


def test_store_migrates_existing_db(tmp_path):
    # Base créée AVANT les colonnes audit : la migration doit les ajouter.
    import sqlite3

    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE listings (listing_id TEXT NOT NULL, search TEXT NOT NULL, title TEXT,"
        " price INTEGER, url TEXT, first_seen TEXT NOT NULL,"
        " notified INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'seen',"
        " sent_at TEXT, PRIMARY KEY (listing_id, search))"
    )
    con.execute(
        "INSERT INTO listings (listing_id, search, title, price, url, first_seen)"
        " VALUES ('x', 'gpu', 'RTX 5080', 900, 'u', ?)",
        (datetime.now(UTC).isoformat(),),
    )
    con.commit()
    con.close()
    store = Store(db)  # ne doit pas lever
    cols = {r[1] for r in store.conn.execute("PRAGMA table_info(listings)")}
    assert {"search_text", "reject_reason"} <= cols
    # L'ancienne ligne survit.
    assert store.conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 1
    store.close()


def test_audit_script(tmp_path):
    db = tmp_path / "t.db"
    store = Store(db)
    now = datetime.now(UTC).isoformat()
    store.conn.execute(
        "INSERT INTO listings (listing_id, search, title, price, url, first_seen, status,"
        " search_text, reject_reason) VALUES (?,?,?,?,?,?,?,?,?)",
        ("1", "gpu", "RTX 5060 Ti 8go", 450, "https://lbc/1", now, "seen",
         "RTX 5060 ti 8go vendue avec sa boîte", "min_vram_gb"),
    )
    store.conn.execute(
        "INSERT INTO listings (listing_id, search, title, price, url, first_seen, status,"
        " search_text, reject_reason) VALUES (?,?,?,?,?,?,?,?,?)",
        ("2", "gpu", "Pour pièce RTX 5090", 130, "https://lbc/2", now, "seen",
         "vendue pour pièces", "min_price"),
    )
    # Hors fenêtre : ignorée.
    store.conn.execute(
        "INSERT INTO listings (listing_id, search, title, price, url, first_seen, status,"
        " search_text, reject_reason) VALUES (?,?,?,?,?,?,?,?,?)",
        ("3", "gpu", "RTX 5080", 900, "https://lbc/3",
         (datetime.now(UTC) - timedelta(days=5)).isoformat(), "seen", "vieux", "regex_any"),
    )
    store.conn.commit()
    store.close()

    out = subprocess.run(
        [sys.executable, "scripts/audit_filtered.py", "--db", str(db)],
        capture_output=True, text=True, check=True,
    ).stdout
    # Refonte 31/10 : VRAM explicite < 16 Go = rejet SÛR (hors scope), pas
    # « à vérifier » — le rapport du matin ne doit plus le proposer (bug
    # 31/10 : 3080 Ti 12 Go présentée comme affaire manquée).
    assert "à vérifier : 0" in out
    assert "hors scope : VRAM 8 Go < 16 Go × 1" in out
    assert "RTX 5060 Ti 8go" not in out  # rejet sûr → compté, pas listé
    assert "Pour pièce" not in out  # rejet sûr → compté, pas listé
    assert "RTX 5080" not in out  # hors fenêtre
