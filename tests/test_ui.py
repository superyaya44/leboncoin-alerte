"""Tests interface `lbc-alerte ui` : secrets jamais exposés, écriture sûre."""

import http.client
import json
import threading
import tomllib
from http.server import ThreadingHTTPServer

import pytest

from leboncoin_alerte.config import ConfigError, load_config
from leboncoin_alerte.ui import ConfigStore, ConflictError, check, make_handler, update_env_text

HOOK = "https://discord.com/api/webhooks/1/super-secret"
CONFIG = """\
# commentaire d'origine
[general]
interval_minutes = 15
data_dir = "./data"

[[search]]
name = "gpu"
url = "https://www.leboncoin.fr/recherche?text=rtx"
max_price = 900
champ_futur = "conservé"

[notify.discord]
enabled = true
webhook_url = "${LBC_DISCORD_WEBHOOK}"
"""


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("LBC_DISCORD_WEBHOOK", raising=False)
    (tmp_path / "config.toml").write_text(CONFIG)
    (tmp_path / ".env").write_text(f"# secrets\nLBC_DISCORD_WEBHOOK={HOOK}\n")
    return ConfigStore(tmp_path / "config.toml")


def test_get_never_exposes_secrets(store):
    view = store.get()
    assert HOOK not in json.dumps(view)
    assert "webhook_url" not in view["config"]["notify"]["discord"]
    assert view["secrets"]["discord"] is True
    assert view["secrets"]["telegram"] is False


def test_save_roundtrip_keeps_secret_and_unknown_keys(store, tmp_path):
    view = store.get()
    cfg = view["config"]
    cfg["search"][0]["max_price"] = 700
    res = store.save({"config": cfg, "secrets": {}, "version": view["version"]})
    raw = tomllib.loads((tmp_path / "config.toml").read_text())
    assert raw["search"][0]["max_price"] == 700
    assert raw["search"][0]["champ_futur"] == "conservé"
    assert raw["notify"]["discord"]["webhook_url"] == "${LBC_DISCORD_WEBHOOK}"
    assert any(b.startswith("config.toml.bak-") for b in res["backups"])
    bak = next(tmp_path.glob("config.toml.bak-*"))
    assert "commentaire d'origine" in bak.read_text()
    assert load_config(tmp_path / "config.toml").searches[0].max_price == 700


def test_secret_replacement_goes_to_env(store, tmp_path):
    view = store.get()
    new = "https://discord.com/api/webhooks/2/nouveau"
    store.save({"config": view["config"], "secrets": {"discord": new}, "version": view["version"]})
    assert new not in (tmp_path / "config.toml").read_text()
    env = (tmp_path / ".env").read_text()
    assert f"LBC_DISCORD_WEBHOOK={new}" in env and "# secrets" in env
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.glob(".env.bak-*"))


def test_invalid_config_is_never_written(store, tmp_path):
    view = store.get()
    view["config"]["search"][0]["url"] = "https://exemple.fr"
    with pytest.raises(ConfigError):
        store.save({"config": view["config"], "secrets": {}, "version": view["version"]})
    assert (tmp_path / "config.toml").read_text() == CONFIG
    assert not list(tmp_path.glob("config.toml.bak-*"))


def test_stale_version_conflict(store, tmp_path):
    view = store.get()
    (tmp_path / "config.toml").write_text(CONFIG.replace("900", "800"))
    with pytest.raises(ConflictError):
        store.save({"config": view["config"], "secrets": {}, "version": view["version"]})


def test_secret_with_newline_rejected(store):
    view = store.get()
    with pytest.raises(ConfigError):
        store.save({"config": view["config"], "secrets": {"discord": "https://a\nX=1"},
                    "version": view["version"]})


def test_update_env_text():
    out = update_env_text("# c\nA=1\nB=2", {"B": "3", "C": "4"})
    assert out == "# c\nA=1\nB=3\nC=4\n"


def test_check_writes_nothing(store, tmp_path):
    ok, report = check(tmp_path / "config.toml")
    assert ok and "oui ✓" in report
    assert (tmp_path / "config.toml").read_text() == CONFIG


@pytest.fixture
def server(store):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()


def _req(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers or {})
    r = c.getresponse()
    return r.status, r.read()


def test_http_page_and_api(server):
    status, body = _req(server, "GET", "/")
    assert status == 200 and b"Mes alertes" in body
    status, body = _req(server, "GET", "/api/config")
    assert status == 200 and HOOK.encode() not in body


def test_http_rejects_foreign_host_and_missing_header(server):
    status, _ = _req(server, "GET", "/api/config", headers={"Host": "evil.example:8898"})
    assert status == 403
    status, _ = _req(server, "POST", "/api/save", {"config": {}},
                     headers={"Content-Type": "application/json"})
    assert status == 403


def test_http_save_error_is_readable(server):
    status, body = _req(server, "POST", "/api/preview", {"config": {"general": {}}},
                        headers={"Content-Type": "application/json", "X-LBC-UI": "1"})
    assert status == 422 and "recherche" in json.loads(body)["error"]


def test_http_listing_lookup(server, store):
    from leboncoin_alerte.model import Listing
    from leboncoin_alerte.store import Store

    db = Store(store.db_path())
    db.mark_seen("gpu", [Listing("42", "RTX 3060", 200, "https://x/42")])
    db.close()
    status, body = _req(server, "GET", "/api/listing?search=gpu&id=42")
    assert status == 200 and json.loads(body)["title"] == "RTX 3060"
    status, _ = _req(server, "GET", "/api/listing?search=gpu&id=nope")
    assert status == 404
