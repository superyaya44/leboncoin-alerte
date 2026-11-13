"""Tester une alerte et statistiques de prix (lecture seule de listings.db)."""

from datetime import UTC, datetime, timedelta

from leboncoin_alerte.config import Search
from leboncoin_alerte.insights import gpu_label, price_stats
from leboncoin_alerte.insights import test_search as run_test
from leboncoin_alerte.model import Listing
from leboncoin_alerte.store import Store

URL = "https://www.leboncoin.fr/recherche?text=x"


def fill(tmp_path, n=25):
    db = tmp_path / "listings.db"
    store = Store(db)
    items = [Listing(str(i), f"RTX 3060 Ti carte {i}" if i % 2 else f"Vélo {i}", 100 + i * 10,
                     f"https://x/{i}") for i in range(n)]
    store.mark_seen("gpu", items)
    store.close()
    return db


def test_test_search_counts_and_reasons(tmp_path):
    db = fill(tmp_path)
    r = run_test(db, Search(name="gpu", url=URL, max_price=200, keywords_include=["rtx"]))
    assert r["source"] == "own" and r["total"] == 25
    # impairs ≤ 200 € : 1,3,5,7,9 → 110..190
    assert r["matched"] == 5
    labels = {x["label"] for x in r["reasons"]}
    assert {"prix trop élevé", "titre sans les mots voulus"} <= labels


def test_new_alert_uses_all_listings(tmp_path):
    db = fill(tmp_path)
    r = run_test(db, Search(name="nouvelle", url=URL, keywords_include=["vélo"]))
    assert r["source"] == "all" and r["matched"] == 13


def test_missing_db(tmp_path):
    assert run_test(tmp_path / "absent.db", Search(name="x", url=URL))["source"] == "none"
    assert price_stats(tmp_path / "absent.db", "x")["n"] == 0


def test_price_stats_weeks_and_models(tmp_path):
    db = fill(tmp_path)
    store = Store(db)
    old = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    store.conn.execute("UPDATE listings SET first_seen = ? WHERE CAST(listing_id AS INT) < 10", (old,))
    store.conn.commit()
    store.close()
    st = price_stats(db, "gpu", "gpu")
    assert st["n"] == 25 and len(st["weeks"]) >= 2
    assert all(w["p25"] <= w["median"] <= w["p75"] for w in st["weeks"])
    assert st["models"][0]["name"] == "RTX 3060 Ti"


def test_gpu_label_variants():
    assert gpu_label("Zotac rtx4070ti super 16go") == "RTX 4070 Ti Super"
    assert gpu_label("RTX 3090 24GB") == "RTX 3090"
    assert gpu_label("Vélo") is None
