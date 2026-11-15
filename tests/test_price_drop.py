"""Baisse de prix : historique, renotification des envoyées, réanalyse sous max_price."""

from leboncoin_alerte.config import Search
from leboncoin_alerte.model import Listing
from leboncoin_alerte.notify.discord import build_payload
from leboncoin_alerte.runner import _price_changes
from leboncoin_alerte.store import Store

URL = "https://www.leboncoin.fr/recherche?text=velo"


def lst(i, price, title="Vélo électrique"):
    return Listing(str(i), title, price, f"https://x/{i}")


def setup(tmp_path, **kw):
    store = Store(tmp_path / "l.db")
    search = Search(name="velo", url=URL, **kw)
    return store, search


def test_history_kept_even_when_disabled(tmp_path):
    store, search = setup(tmp_path)
    store.mark_seen("velo", [lst(1, 500)])
    assert _price_changes(store, search, [lst(1, 450)]) == []
    rows = store.conn.execute("SELECT price FROM listing_prices ORDER BY seen_at").fetchall()
    assert [r[0] for r in rows] == [500, 450]
    assert store.known_prices("velo")["1"][0] == 450


def test_sent_listing_dropping_enough_is_renotified(tmp_path):
    store, search = setup(tmp_path, price_drop_percent=10)
    store.mark_seen("velo", [lst(1, 500), lst(2, 500)])
    store.mark_sent("velo", lst(1, 500))
    store.mark_sent("velo", lst(2, 500))
    drops = _price_changes(store, search, [lst(1, 440), lst(2, 480)])  # -12 % / -4 %
    assert [(d.listing_id, old) for d, old in drops] == [("1", 500)]


def test_price_increase_never_notifies(tmp_path):
    store, search = setup(tmp_path, price_drop_percent=10)
    store.mark_seen("velo", [lst(1, 500)])
    store.mark_sent("velo", lst(1, 500))
    assert _price_changes(store, search, [lst(1, 700)]) == []


def test_rejected_for_price_reanalysed_under_max(tmp_path):
    store, search = setup(tmp_path, price_drop_percent=10, max_price=600)
    store.mark_seen("velo", [lst(1, 700), lst(2, 700)],
                    {"1": "max_price", "2": "keywords_exclude"})
    _price_changes(store, search, [lst(1, 590), lst(2, 590)])
    # 1 oubliée → redevient « nouvelle » ; 2 rejetée pour autre chose → reste vue
    assert [x.listing_id for x in store.unseen_listings("velo", [lst(1, 590), lst(2, 590)])] == ["1"]


def test_discord_hides_hardware_fields_for_non_hardware():
    names = [f["name"] for f in build_payload("velo", lst(1, 500))["embeds"][0]["fields"]]
    assert not any("GPU" in n or "CPU" in n for n in names)
    pc = Listing("2", "PC gamer RTX 4070 32 Go DDR5", 900, "https://x/2")
    names = [f["name"] for f in build_payload("pc", pc)["embeds"][0]["fields"]]
    assert any("GPU" in n for n in names)


def test_discord_action_links_only_with_ui_url():
    plain = build_payload("velo", lst(1, 500))["embeds"][0]["fields"]
    assert not any(f["name"] == "Actions" for f in plain)
    fields = build_payload("mon alerte", lst(7, 500), ui_url="http://localhost:3000")["embeds"][0]["fields"]
    actions = next(f for f in fields if f["name"] == "Actions")["value"]
    assert "(http://localhost:3000/#avis/mon%20alerte/7)" in actions
    assert "(http://localhost:3000/#vendeur/mon%20alerte/7)" in actions


def test_general_ui_url_and_cdp_url_parsing():
    import pytest

    from leboncoin_alerte.config import ConfigError, parse_config

    base = {"search": [{"name": "s", "url": URL}],
            "notify": {"webhook": {"enabled": True, "url": "https://h"}}}
    cfg = parse_config(base)
    assert cfg.ui_url is None and cfg.cdp_url is None  # plus de "None" en chaîne
    cfg = parse_config({**base, "general": {"ui_url": "http://localhost:3000/"}})
    assert cfg.ui_url == "http://localhost:3000"
    with pytest.raises(ConfigError):
        parse_config({**base, "general": {"ui_url": "localhost:3000"}})
