"""Tests bundle : sanitisation des secrets, export→import round-trip, intégrité.

test_sanitize_omnibus est le garde-fou anti-fuite : une config utilisant TOUS
les canaux + sous-table headers + commentaire de section + secret multi-lignes
ne doit laisser AUCUNE valeur secrète dans le bundle, et le résultat doit
re-parser en TOML valide.
"""

import json
import tomllib

import pytest

from leboncoin_alerte.bundle import (
    BUNDLE_VERSION,
    export_bundle,
    import_bundle,
    sanitize_config_text,
)

CONFIG = """# config locale
[general]
interval_minutes = 15
data_dir = "./data"

[[search]]
name = "gpu"
url = "https://www.leboncoin.fr/recherche?category=83&text=rtx"
min_vram_gb = 16

[notify.discord]
enabled = true
webhook_url = "https://discord.com/api/webhooks/123/SECRET"

[notify.telegram]
enabled = false
bot_token = "${TELEGRAM_BOT_TOKEN}"
"""

# Tous les vecteurs adversariaux des reviews dans un seul fichier TOML valide.
# La sanitisation étant sémantique (parse → remplacement → réécriture),
# toutes ces formes syntaxiques sont neutralisées d'office.
OMNIBUS = """[general]
interval_minutes = 20
data_dir = "./data"

[[search]]
name = "s"
url = "https://www.leboncoin.fr/recherche?category=15&text=x"

# webhook_url = "https://discord.com/api/webhooks/9/COMMENTED-OLD-SECRET"

[notify.discord]
enabled = true
webhook_url = "https://discord.com/api/webhooks/9/DISCORD-LEAK"

[notify.email]  # alertes mail
enabled = true
smtp_host = "smtp.example.fr"
password = '''SMTP-LEAK
sur plusieurs lignes'''
username = "moi"
to = ["moi@example.fr"]

[notify.telegram]
enabled = false
bot_token = "TG-LEAK"

[notify.webhook]
enabled = false
url = "https://hooks.example/WH-LEAK"

[notify.webhook.headers]
X-Auth = "SUBTABLE-LEAK"

[notify.webhook.headers.sous]
Y = "NESTED-LEAK"
"""


def test_sanitize_removes_secrets():
    out, required = sanitize_config_text(CONFIG)
    parsed = tomllib.loads(out)
    assert "SECRET" not in out
    assert parsed["notify"]["discord"]["webhook_url"] == "${DISCORD_WEBHOOK_URL}"
    # secret en clair → remplacé ; ${VAR} déjà présent → gardé ET listé requis
    assert required == ["DISCORD_WEBHOOK_URL", "TELEGRAM_BOT_TOKEN"]
    assert parsed["notify"]["telegram"]["bot_token"] == "${TELEGRAM_BOT_TOKEN}"


def test_sanitize_dotted_and_quoted_keys():
    # Clé pointée sous [notify] et clé quotée : meme arbre parsé que la forme
    # standard → neutralisées par la sanitisation sémantique.
    out1, _ = sanitize_config_text(
        '[notify]\ndiscord.webhook_url = "https://discord.com/api/webhooks/9/DOTTED-KEY-LEAK"\n')
    assert "DOTTED-KEY-LEAK" not in out1
    assert tomllib.loads(out1)["notify"]["discord"]["webhook_url"] == "${DISCORD_WEBHOOK_URL}"
    out2, _ = sanitize_config_text(
        '[notify.discord]\n"webhook_url" = "https://discord.com/api/webhooks/9/QUOTED-KEY-LEAK"\n')
    assert "QUOTED-KEY-LEAK" not in out2
    assert tomllib.loads(out2)["notify"]["discord"]["webhook_url"] == "${DISCORD_WEBHOOK_URL}"


def test_sanitize_quoted_and_spaced_headers():
    # deux déclarations de la même table interdit : on teste en deux fois
    out1, _ = sanitize_config_text('[notify.webhook]\nurl = "https://x/hook"\n[notify . webhook . headers]\nAuthorization = "Bearer SPACED-LEAK"\n')
    assert "SPACED-LEAK" not in out1
    assert "Bearer" not in out1
    out2, _ = sanitize_config_text('[notify.webhook]\nurl = "https://x/hook"\n["notify".webhook.headers]\nX = "QUOTED-LEAK"\n')
    assert "QUOTED-LEAK" not in out2


def test_sanitize_empties_webhook_headers():
    text = '[notify.webhook]\nenabled = true\nurl = "https://x/hook"\nheaders = { Authorization = "Bearer xyz" }\n'
    out, required = sanitize_config_text(text)
    assert "Bearer" not in out
    assert required == ["WEBHOOK_URL"]
    assert tomllib.loads(out)["notify"]["webhook"].get("headers", {}) == {}


def test_sanitize_omnibus_no_leak_and_reparse():
    out, required = sanitize_config_text(OMNIBUS)
    for leak in ("DISCORD-LEAK", "SMTP-LEAK", "TG-LEAK", "WH-LEAK",
                 "COMMENTED-OLD-SECRET", "SUBTABLE-LEAK", "NESTED-LEAK"):
        assert leak not in out, f"fuite détectée : {leak}"
    assert set(required) == {"DISCORD_WEBHOOK_URL", "SMTP_PASSWORD", "TELEGRAM_BOT_TOKEN", "WEBHOOK_URL"}
    parsed = tomllib.loads(out)  # le résultat sanitisé reste du TOML valide
    assert parsed["notify"]["discord"]["webhook_url"] == "${DISCORD_WEBHOOK_URL}"
    assert parsed["notify"]["webhook"].get("headers", {}) == {}


def test_sanitize_section_trailing_comment():
    # Le commentaire trailing ne doit pas casser la détection de section.
    text = '[notify.discord]  # canal\nenabled = true\nwebhook_url = "https://x/LEAK"\n'
    out, _ = sanitize_config_text(text)
    assert "LEAK" not in out
    assert tomllib.loads(out)["notify"]["discord"]["webhook_url"] == "${DISCORD_WEBHOOK_URL}"


def test_sanitize_refuses_if_secret_survives():
    # Post-check : si une valeur secrete reapparait malgre tout, export refuse.
    # Simulé par un chemin inconnu partageant sa valeur avec un chemin connu.
    text = '[notify.discord]\nenabled = true\nwebhook_url = "https://discord.com/api/webhooks/1/abc"\n[[search]]\nname = "https://discord.com/api/webhooks/1/abc"\nurl = "https://www.leboncoin.fr/recherche?category=15&text=x"\n'
    with pytest.raises(ValueError, match="Sanitisation KO"):
        sanitize_config_text(text)


def test_sanitize_non_string_secret_values():
    # password numérique (TOML int, sans guillemets) ou array : remplacés
    # comme n'importe quel secret (review 3).
    out, required = sanitize_config_text(
        '[notify.email]\npassword = 1357902468\nusername = "moi"\n'
        '[notify.discord]\nwebhook_url = ["https://discord.com/api/webhooks/9/ARRAY-LEAK"]\n')
    assert "1357902468" not in out
    assert "ARRAY-LEAK" not in out
    parsed = tomllib.loads(out)
    assert parsed["notify"]["email"]["password"] == "${SMTP_PASSWORD}"
    assert parsed["notify"]["discord"]["webhook_url"] == "${DISCORD_WEBHOOK_URL}"
    assert set(required) == {"SMTP_PASSWORD", "DISCORD_WEBHOOK_URL"}


def test_sanitize_partial_dollar_brace_is_secret():
    # Une valeur CONTENANT ${ mais pas intégralement ${VAR} est un secret :
    # seul le fullmatch est passthrough (review 3).
    out, _ = sanitize_config_text('[notify.telegram]\nbot_token = "123456:${SUFFIX}"\n')
    assert "123456" not in out
    assert tomllib.loads(out)["notify"]["telegram"]["bot_token"] == "${TELEGRAM_BOT_TOKEN}"


def test_sanitize_full_var_passthrough_still_works():
    out, required = sanitize_config_text('[notify.telegram]\nbot_token = "${TELEGRAM_BOT_TOKEN}"\n')
    assert tomllib.loads(out)["notify"]["telegram"]["bot_token"] == "${TELEGRAM_BOT_TOKEN}"
    assert required == ["TELEGRAM_BOT_TOKEN"]


@pytest.fixture()
def fake_project(tmp_path):
    proj = tmp_path / "projet"
    (proj / "data").mkdir(parents=True)
    (proj / "config.toml").write_text(CONFIG, encoding="utf-8")
    (proj / "data" / "listings.db").write_bytes(b"fake-sqlite-bytes")
    return proj


def test_export_import_roundtrip(fake_project, tmp_path):
    out = tmp_path / "bundle"
    path, required = export_bundle(fake_project / "config.toml", out, with_db=True)
    assert path == out
    assert set(required) == {"DISCORD_WEBHOOK_URL", "TELEGRAM_BOT_TOKEN"}
    assert "SECRET" not in (out / "config.toml").read_text(encoding="utf-8")
    manifest = json.loads((out / "manifest.json").read_text())
    assert set(manifest["files"]) == {"config.toml", "listings.db"}
    assert manifest["bundle_version"] == BUNDLE_VERSION
    assert manifest["required_env_vars"] == required


def test_import_restores_db_in_data_dir(fake_project, tmp_path):
    """listings.db doit atterrir dans data_dir (lu du config), pas à la racine."""
    out = tmp_path / "bundle"
    export_bundle(fake_project / "config.toml", out, with_db=True)
    dest = tmp_path / "dest"
    import_bundle(out, dest)
    assert (dest / "data" / "listings.db").read_bytes() == b"fake-sqlite-bytes"
    assert not (dest / "listings.db").exists()  # pas de copie orpheline à la racine
    env = (dest / ".env").read_text(encoding="utf-8")
    assert "DISCORD_WEBHOOK_URL=" in env  # var vide, jamais de valeur


def test_import_custom_data_dir(tmp_path):
    proj = tmp_path / "p2"
    proj.mkdir()
    (proj / "config.toml").write_text(
        CONFIG.replace('data_dir = "./data"', 'data_dir = "mondata"'), encoding="utf-8")
    (proj / "mondata").mkdir()
    (proj / "mondata" / "listings.db").write_bytes(b"db-custom")
    out = tmp_path / "bundle2"
    export_bundle(proj / "config.toml", out, with_db=True)
    dest = tmp_path / "dest2"
    import_bundle(out, dest)
    assert (dest / "mondata" / "listings.db").read_bytes() == b"db-custom"


def test_import_env_file_mode_600(fake_project, tmp_path):
    out = tmp_path / "bundle"
    export_bundle(fake_project / "config.toml", out)
    dest = tmp_path / "dest"
    import_bundle(out, dest)
    assert (dest / ".env").exists()
    assert oct((dest / ".env").stat().st_mode & 0o777) == "0o600"


def test_import_refuses_overwrite(fake_project, tmp_path):
    out = tmp_path / "bundle"
    export_bundle(fake_project / "config.toml", out)
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "config.toml").write_text("existant", encoding="utf-8")
    with pytest.raises(FileExistsError):
        import_bundle(out, dest)
    assert (dest / "config.toml").read_text() == "existant"
    import_bundle(out, dest, force=True)  # --force passe
    assert "existant" not in (dest / "config.toml").read_text()


def test_import_detects_tampering(fake_project, tmp_path):
    out = tmp_path / "bundle"
    export_bundle(fake_project / "config.toml", out)
    (out / "config.toml").write_text("modifié après export", encoding="utf-8")
    with pytest.raises(ValueError, match="Intégrité"):
        import_bundle(out, tmp_path / "dest")


def test_import_detects_profile_tampering(tmp_path):
    proj = tmp_path / "p3"
    (proj / "data" / "profile").mkdir(parents=True)
    (proj / "config.toml").write_text(CONFIG, encoding="utf-8")
    (proj / "data" / "profile" / "cookie.txt").write_text("nom-rigolo", encoding="utf-8")
    out = tmp_path / "bundle3"
    export_bundle(proj / "config.toml", out, with_profile=True)
    (out / "profile" / "cookie.txt").write_text("changé", encoding="utf-8")
    with pytest.raises(ValueError, match="Intégrité"):
        import_bundle(out, tmp_path / "dest3")


def test_import_refuses_future_bundle_version(fake_project, tmp_path):
    out = tmp_path / "bundle"
    export_bundle(fake_project / "config.toml", out)
    manifest = json.loads((out / "manifest.json").read_text())
    manifest["bundle_version"] = BUNDLE_VERSION + 1
    (out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="plus récent"):
        import_bundle(out, tmp_path / "dest")


def test_export_refuses_invalid_toml(tmp_path):
    proj = tmp_path / "p4"
    proj.mkdir()
    (proj / "config.toml").write_text("[general\npas du toml valide", encoding="utf-8")
    with pytest.raises(ValueError, match="invalide"):
        export_bundle(proj / "config.toml", tmp_path / "bundle4")


def test_export_refuses_nonempty_dir(fake_project, tmp_path):
    out = tmp_path / "bundle"
    out.mkdir()
    (out / "x.txt").write_text("déjà pris", encoding="utf-8")
    with pytest.raises(FileExistsError):
        export_bundle(fake_project / "config.toml", out)
