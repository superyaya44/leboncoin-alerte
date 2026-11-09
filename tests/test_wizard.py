"""Tests wizard : rendu TOML valide (parsé par config.parse_config) + .env."""

from leboncoin_alerte.config import parse_config
from leboncoin_alerte.wizard import render_config_toml

ANSWERS_DISCORD = {
    "interval_minutes": 15,
    "data_dir": "./data",
    "searches": [
        {
            "name": "gpu",
            "url": "https://www.leboncoin.fr/recherche?category=83&text=rtx",
            "max_price": 900,
            "min_price": None,
            "min_vram_gb": 16,
            "min_ram_gb": None,
            "keywords_exclude": ["recherche", "j'achète"],
            "pages": 3,
        }
    ],
    "channel": "discord",
    "discord_webhook": "https://discord.com/api/webhooks/1/abc",
}


def test_render_discord_valid(monkeypatch):
    toml_text, secrets = render_config_toml(ANSWERS_DISCORD)
    assert secrets == {"DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/1/abc"}
    assert "discord.com/api/webhooks/1/abc" not in toml_text  # secret jamais en clair
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/abc")
    import tomllib
    cfg = parse_config(tomllib.loads(toml_text))
    assert cfg.searches[0].min_vram_gb == 16
    assert cfg.searches[0].keywords_exclude == ["recherche", "j'achète"]
    assert cfg.notify.enabled_channels() == ["discord"]
    assert cfg.interval_minutes == 15


def test_render_telegram_valid(monkeypatch):
    answers = {
        "interval_minutes": 10,
        "data_dir": "./data",
        "searches": [dict(ANSWERS_DISCORD["searches"][0], max_price=None)],
        "channel": "telegram",
        "tg_token": "123:ABC",
        "tg_chat_id": "424242",
    }
    toml_text, secrets = render_config_toml(answers)
    assert secrets == {"TELEGRAM_BOT_TOKEN": "123:ABC"}
    assert "123:ABC" not in toml_text
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    import tomllib
    cfg = parse_config(tomllib.loads(toml_text))
    assert cfg.notify.telegram.chat_id == "424242"
    assert cfg.searches[0].max_price is None
