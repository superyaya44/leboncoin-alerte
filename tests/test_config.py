"""Tests config : parsing, expansion ${VAR}, erreurs de validation."""

import pytest

from leboncoin_alerte.config import ConfigError, parse_config

VALID = {
    "general": {"interval_minutes": 15, "data_dir": "./data"},
    "search": [
        {
            "name": "ps5",
            "url": "https://www.leboncoin.fr/recherche?category=15&text=ps5",
            "max_price": 300,
        }
    ],
    "notify": {
        "discord": {
            "enabled": True,
            "webhook_url": "https://discord.com/api/webhooks/123/abc",
        }
    },
}


def test_valid_config():
    cfg = parse_config(VALID)
    assert cfg.searches[0].name == "ps5"
    assert cfg.searches[0].max_price == 300
    assert cfg.notify.enabled_channels() == ["discord"]


def test_env_expansion(monkeypatch):
    monkeypatch.setenv("MY_HOOK", "https://discord.com/api/webhooks/9/xyz")
    raw = {
        **VALID,
        "notify": {"discord": {"enabled": True, "webhook_url": "${MY_HOOK}"}},
    }
    cfg = parse_config(raw)
    assert cfg.notify.discord.webhook_url == "https://discord.com/api/webhooks/9/xyz"


def test_missing_search_rejected():
    raw = {**VALID, "search": []}
    with pytest.raises(ConfigError):
        parse_config(raw)


def test_bad_url_rejected():
    raw = {
        **VALID,
        "search": [{"name": "x", "url": "https://vinted.fr/q"}],
    }
    with pytest.raises(ConfigError, match="leboncoin.fr"):
        parse_config(raw)


def test_no_channel_rejected():
    raw = {**VALID, "notify": {"discord": {"enabled": False}}}
    with pytest.raises(ConfigError, match="canal"):
        parse_config(raw)


def test_discord_enabled_requires_url():
    raw = {
        **VALID,
        "notify": {"discord": {"enabled": True, "webhook_url": ""}},
    }
    with pytest.raises(ConfigError, match="webhook_url"):
        parse_config(raw)


def test_published_after_date():
    import datetime as dt

    import pytest

    from leboncoin_alerte.config import ConfigError, parse_config

    base = {"search": [{"name": "s", "url": "https://www.leboncoin.fr/recherche?text=x"}],
            "notify": {"webhook": {"enabled": True, "url": "https://h"}}}
    base["search"][0]["published_after"] = "2026-11-01"
    assert parse_config(base).searches[0].published_after == "2026-11-01"
    base["search"][0]["published_after"] = dt.date(2026, 11, 2)  # date TOML native
    assert parse_config(base).searches[0].published_after == "2026-11-02"
    base["search"][0]["published_after"] = "01/11/2026"
    with pytest.raises(ConfigError):
        parse_config(base)
