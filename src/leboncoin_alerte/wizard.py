"""Wizard interactif `lbc-alerte init`.

Deux modes :
  1. Manuel   : questions/réponses → génère config.toml (+ .env pour les secrets)
  2. Import   : pointe vers un bundle issu de `lbc-alerte export` (migration)

Le rendu TOML est une fonction pure (render_config_toml) → testable sans TTY.
"""

from __future__ import annotations

import json
from pathlib import Path

from .bundle import import_bundle

# Noms alignés sur .env.example / docker-compose.yml.
ENV_VARS = {
    "discord": "DISCORD_WEBHOOK_URL",
    "telegram": "TELEGRAM_BOT_TOKEN",
    "email": "SMTP_PASSWORD",
    "webhook": "WEBHOOK_URL",
}


def _ask(prompt: str, default: str | None = None, *, optional: bool = False, stream=input) -> str:
    """Pose une question, retourne la réponse (Entrée = défaut ; vide interdit sauf optional)."""
    suffix = f" [{default}]" if default is not None else ""
    while True:
        raw = stream(f"{prompt}{suffix} : ").strip()
        if raw:
            return raw
        if default is not None:
            return default
        if optional:
            return ""
        print("  → valeur requise (ou Ctrl+C pour abandonner)")


def _ask_int(prompt: str, *, optional: bool = True, stream=input) -> int | None:
    while True:
        raw = _ask(prompt, optional=optional, stream=stream)
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            print("  → entier attendu")


def _ask_list(prompt: str, stream=input) -> list[str]:
    raw = _ask(prompt, optional=True, stream=stream)
    return [x.strip() for x in raw.split(",") if x.strip()]


def collect_answers(stream=input) -> dict:
    """Collecte les réponses interactives. Structure consommée par render_config_toml."""
    print("=== Configuration lbc-alerte ===\n")
    answers: dict = {
        "interval_minutes": _ask_int("Intervalle entre cycles en minutes (>= 10 recommandé, anti-ban)",
                                     stream=stream) or 15,
        "data_dir": _ask("Dossier de données", "./data", stream=stream),
        "searches": [],
    }

    print("\n--- Recherches (une à la fois, Entrée sur le nom pour terminer) ---")
    while True:
        name = _ask("Nom de la recherche (vide = terminer)", optional=True, stream=stream)
        if not name:
            break
        url = ""
        while not url.startswith("https://www.leboncoin.fr/"):
            url = _ask("URL de recherche leboncoin (copiée depuis le site)", stream=stream)
            if not url.startswith("https://www.leboncoin.fr/"):
                print("  → doit commencer par https://www.leboncoin.fr/")
        s: dict = {"name": name, "url": url}
        s["max_price"] = _ask_int("Prix max € (vide = aucun)", stream=stream)
        s["min_price"] = _ask_int("Prix min € (vide = aucun)", stream=stream)
        s["min_vram_gb"] = _ask_int("VRAM min en Go, annonces GPU (vide = aucun)", stream=stream)
        s["min_ram_gb"] = _ask_int("RAM min en Go (vide = aucun)", stream=stream)
        s["keywords_exclude"] = _ask_list("Mots-clés à exclure (séparés par des virgules)", stream=stream)
        s["pages"] = _ask_int("Pages à scanner par cycle (35 annonces/page)", stream=stream) or 3
        answers["searches"].append(s)
        print(f"  ✓ {name} ajoutée ({len(answers['searches'])} au total)\n")

    print("\n--- Notifications ---")
    print("  1) Discord (recommandé)  2) Telegram  3) Email  4) Webhook générique")
    choice = ""
    while choice not in {"1", "2", "3", "4"}:
        choice = _ask("Canal", "1", stream=stream)
    channel = {"1": "discord", "2": "telegram", "3": "email", "4": "webhook"}[choice]
    answers["channel"] = channel

    if channel == "discord":
        answers["discord_webhook"] = _ask("URL du webhook Discord", stream=stream)
    elif channel == "telegram":
        answers["tg_token"] = _ask("Bot token Telegram (via @BotFather)", stream=stream)
        answers["tg_chat_id"] = _ask("Chat ID (message au bot puis /getUpdates)", stream=stream)
    elif channel == "email":
        answers["smtp_host"] = _ask("Serveur SMTP", stream=stream)
        answers["smtp_port"] = _ask_int("Port SMTP", stream=stream) or 587
        answers["smtp_user"] = _ask("Utilisateur SMTP", stream=stream)
        answers["smtp_password"] = _ask("Mot de passe SMTP (stocké dans .env, pas dans config.toml)", stream=stream)
        answers["mail_from"] = _ask("Expéditeur", stream=stream)
        answers["mail_to"] = _ask_list("Destinataires (virgules)", stream=stream)
    else:
        answers["webhook_url"] = _ask("URL du webhook (POST JSON)", stream=stream)
    return answers


def _t(v: object) -> str:
    """Sérialise une valeur en TOML (json.dumps produit des chaînes TOML valides)."""
    return json.dumps(v, ensure_ascii=False)


def render_config_toml(a: dict) -> tuple[str, dict[str, str]]:
    """Rend le config.toml ET les secrets à écrire dans .env (var → valeur)."""
    env_secrets: dict[str, str] = {}
    lines = [
        "# Généré par lbc-alerte init — éditable, rechargé à chaque cycle du daemon.",
        "",
        "[general]",
        f"interval_minutes = {a['interval_minutes']}",
        f"data_dir = {_t(a['data_dir'])}",
        "",
    ]
    for s in a["searches"]:
        lines.append("[[search]]")
        lines.append(f"name = {_t(s['name'])}")
        lines.append(f"url = {_t(s['url'])}")
        for key in ("max_price", "min_price", "min_vram_gb", "min_ram_gb", "pages"):
            if s.get(key) is not None:
                lines.append(f"{key} = {s[key]}")
        if s.get("keywords_exclude"):
            lines.append(f"keywords_exclude = {_t(s['keywords_exclude'])}")
        lines.append("")
    ch = a["channel"]
    lines.append("[notify]")
    if ch == "discord":
        env_secrets[ENV_VARS["discord"]] = a["discord_webhook"]
        lines += ["", "[notify.discord]", "enabled = true",
                  f'webhook_url = "${{{ENV_VARS["discord"]}}}"']
    elif ch == "telegram":
        env_secrets[ENV_VARS["telegram"]] = a["tg_token"]
        lines += ["", "[notify.telegram]", "enabled = true",
                  f'bot_token = "${{{ENV_VARS["telegram"]}}}"',
                  f"chat_id = {_t(a['tg_chat_id'])}"]
    elif ch == "email":
        env_secrets[ENV_VARS["email"]] = a["smtp_password"]
        lines += ["", "[notify.email]", "enabled = true",
                  f"smtp_host = {_t(a['smtp_host'])}",
                  f"smtp_port = {a.get('smtp_port', 587)}",
                  f"username = {_t(a['smtp_user'])}",
                  f'password = "${{{ENV_VARS["email"]}}}"',
                  f"from = {_t(a['mail_from'])}",
                  f"to = {_t(a['mail_to'])}"]
    else:
        env_secrets[ENV_VARS["webhook"]] = a["webhook_url"]
        lines += ["", "[notify.webhook]", "enabled = true",
                  f'url = "${{{ENV_VARS["webhook"]}}}"']
    return "\n".join(lines) + "\n", env_secrets


def write_env_secrets(dest_dir: Path, secrets: dict[str, str]) -> None:
    """Ajoute les secrets au .env (créé si absent) sans écraser l'existant."""
    env_path = dest_dir / ".env"
    existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    have = {l.split("=", 1)[0].strip() for l in existing.splitlines()
            if "=" in l and not l.strip().startswith("#")}
    additions = [f"{k}={v}" for k, v in secrets.items() if k not in have]
    if additions:
        env_path.write_text(
            existing.rstrip("\n") + ("\n" if existing else "") + "\n".join(additions) + "\n",
            encoding="utf-8")
    env_path.chmod(0o600)  # contient des secrets


def run_init(config_path: Path, *, force: bool = False, from_bundle: Path | None = None,
             stream=input) -> int:
    """Point d'entrée du wizard. Retourne le code retour CLI."""
    if config_path.exists() and not force:
        print(f"{config_path} existe déjà — relance avec --force pour réécrire.")
        return 1

    if from_bundle is not None:
        print(f"Import du bundle {from_bundle} → {config_path.parent}\n")
        import_bundle(from_bundle, config_path.parent, force=force)
        return 0

    print("1) Configurer à la main (wizard)")
    print("2) Importer un bundle exporté (migration depuis une autre machine)")
    print("3) Partir de l'exemple (config.example.toml)")
    mode = ""
    while mode not in {"1", "2", "3"}:
        mode = _ask("Mode", "1", stream=stream)

    if mode == "2":
        path = _ask("Chemin du bundle (répertoire avec manifest.json)", stream=stream)
        return run_init(config_path, force=force, from_bundle=Path(path), stream=stream)
    if mode == "3":
        import shutil
        src = config_path.parent / "config.example.toml"
        if not src.exists():  # fallback : l'exemple livré avec le paquet
            src = Path(__file__).parent.parent.parent / "config.example.toml"
        if not src.exists():
            src = Path.cwd() / "config.example.toml"
        if not src.exists():
            print("config.example.toml introuvable — mode abandonné.")
            return 1
        shutil.copy(src, config_path)
        print(f"{config_path} créé depuis l'exemple — édite-le puis : lbc-alerte notify-test")
        return 0

    answers = collect_answers(stream=stream)
    if not answers["searches"]:
        print("Aucune recherche définie — abandon (rien n'a été écrit).")
        return 1
    toml_text, secrets = render_config_toml(answers)
    config_path.write_text(toml_text, encoding="utf-8")
    if secrets:
        write_env_secrets(config_path.parent, secrets)
    print(f"\n✓ {config_path} créé"
          + (" + secrets dans .env" if secrets else "")
          + " — teste avec : lbc-alerte notify-test")
    return 0
