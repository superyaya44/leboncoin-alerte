"""Chargement et validation de la configuration TOML.

Schéma de référence : config.example.toml.
Toute valeur chaîne peut contenir ${VAR} → remplacé par la variable d'environnement.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path


class ConfigError(Exception):
    """Erreur de configuration lisible par l'utilisateur."""


WEEKDAYS = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]


@dataclass
class Schedule:
    """Planification d'une alerte (voir schedule.py). Vide = à chaque passage."""

    every_minutes: int | None = None
    at: list[str] = field(default_factory=list)  # "HH:MM", heure de Paris
    weekdays: list[int] = field(default_factory=list)  # 0 = lundi
    month_days: list[int] = field(default_factory=list)  # 1..31


@dataclass
class Search:
    name: str
    url: str
    max_price: int | None = None
    min_price: int | None = None
    keywords_include: list[str] = field(default_factory=list)
    keywords_exclude: list[str] = field(default_factory=list)
    # RAM minimale en Go, évaluée sur titre+description+attributs (parseur numérique :
    # "512 Go SSD" ne compte pas comme de la RAM).
    min_ram_gb: int | None = None
    # Stockage total minimal en Go (1 To = 1024 Go), sur les capacités détectées
    # en contexte stockage ("SSD 477go + 300 go hdd" = 777 Go).
    min_disk_gb: int | None = None
    # VRAM minimale en Go (annonces GPU) : explicite ("32 Go GDDR7", "RTX 5090
    # 32 Go") ou défaut modèle connu ; modèle ambigu (5060 Ti 8/16 Go) →
    # rejet, SAUF vram_ask=True : annonce mise de côté avec un message de
    # question au vendeur (voir filters.vram_verdict).
    min_vram_gb: int | None = None
    # True = un modèle ambigu sans VRAM précisée n'est pas rejeté : il passe
    # en statut 'question' avec un message prêt à envoyer au vendeur.
    vram_ask: bool = False
    # Capacité minimale d'UNE barrette RAM (Go) : "32 Go (2x16)" = 16 par
    # barrette → rejeté avec min 32 ; "64 Go (2x32)" → accepté.
    min_stick_gb: int | None = None
    # Marché alimenté par cette recherche : "gpu" ou "ram". Toutes les annonces
    # de la recherche (même rejetées) nourrissent les références de prix.
    market_kind: str | None = None
    # Regex appliquées au texte complet : TOUTES doivent matcher (AND), casse ignorée.
    # Ex : GPU >= RTX 3050 -> r"rtx[ -]?(30[5-9]\d?|40[5-9]\d?|50\d{2,3})"
    regex_all: list[str] = field(default_factory=list)
    # Idem regex_all mais en OU : au moins une doit matcher.
    regex_any: list[str] = field(default_factory=list)
    # Mots-clés sur le TEXTE COMPLET (description incluse). include = au moins un
    # présent ; exclude = aucun présent.
    text_keywords_include: list[str] = field(default_factory=list)
    text_keywords_exclude: list[str] = field(default_factory=list)
    # Villes : include = la ville de l'annonce doit contenir un de ces mots ;
    # exclude = rejetée si elle en contient un. Ignoré si vide.
    cities_include: list[str] = field(default_factory=list)
    cities_exclude: list[str] = field(default_factory=list)
    # Fraîcheur : ignorer les annonces publiées il y a plus de N heures.
    max_age_hours: int | None = None
    # Date fixe "AAAA-MM-JJ" : ignorer les annonces publiées avant ce jour (heure de Paris).
    published_after: str | None = None
    # Champs structurés Leboncoin (extraits de la page détail, None si absents).
    computer_type: str | None = None  # ex. "Fixe", "Portable" (ignoré si absent de l'annonce)
    seller_type: str | None = None  # "particulier" ou "pro"
    condition_include: list[str] = field(default_factory=list)  # ex. ["neuf", "comme neuf"]
    min_year: int | None = None  # année du matériel (attribut Leboncoin)
    shippable_only: bool = False  # true = uniquement les annonces expédiables
    # Pages de résultats parcourues par cycle (35 annonces/page). Par défaut
    # MAX_PAGES (10) ; réduire (2-3) pour les recherches larges triées par
    # date : les nouveautés sont en page 1, inutile de tout rescanner.
    pages: int = 10
    # Baisse de prix (%) : une annonce déjà notifiée qui baisse d'au moins ce
    # pourcentage est renotifiée ; une annonce rejetée pour prix trop élevé qui
    # repasse sous max_price est réanalysée. None = désactivé.
    price_drop_percent: int | None = None
    # Planification propre à l'alerte ; None = à chaque passage du daemon.
    schedule: Schedule | None = None

    def __post_init__(self) -> None:
        # "particulier"/"pro" → code Leboncoin, quelle que soit la voie de construction.
        if self.seller_type:
            self.seller_type = _seller_code(self.seller_type)


@dataclass
class DiscordConfig:
    enabled: bool = False
    webhook_url: str = ""


@dataclass
class EmailConfig:
    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    username: str = ""
    password: str = ""
    from_addr: str = ""
    to: list[str] = field(default_factory=list)


@dataclass
class TelegramConfig:
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""


@dataclass
class WebhookConfig:
    enabled: bool = False
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class NotifyConfig:
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    email: EmailConfig = field(default_factory=EmailConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    webhook: WebhookConfig = field(default_factory=WebhookConfig)

    def enabled_channels(self) -> list[str]:
        return [
            name
            for name in ("discord", "email", "telegram", "webhook")
            if getattr(self, name).enabled
        ]


@dataclass
class Config:
    interval_minutes: int = 15
    data_dir: Path = Path("./data")
    cdp_url: str | None = None  # ex. http://127.0.0.1:9222 : réutilise le Chromium persistant
    # True : les annonces retenues ne partent PAS sur Discord/Email directement —
    # elles vont dans pending_offers et le cron nocturne en fait un rapport
    # pour validation humaine (scripts/send_pending.py --send <id>).
    # Revenir à False rétablit l'envoi immédiat.
    hold_notifications: bool = False
    # Adresse de l'interface `lbc-alerte ui` vue depuis ton navigateur (ex. via
    # tunnel : http://localhost:3000). Si définie, les notifs Discord ont des
    # liens « Pas intéressé » / « Écrire au vendeur ».
    ui_url: str | None = None
    searches: list[Search] = field(default_factory=list)
    notify: NotifyConfig = field(default_factory=NotifyConfig)


def _expand(value: str) -> str:
    """Remplace ${VAR} par la valeur de l'env (laisse tel quel si absente)."""
    return os.path.expandvars(value)


def _expand_tree(node: object) -> object:
    if isinstance(node, dict):
        return {k: _expand_tree(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_expand_tree(v) for v in node]
    if isinstance(node, str):
        return _expand(node)
    return node


def _seller_code(value: str) -> str:
    """Traduit "particulier"/"pro" vers le code Leboncoin ("private"/"pro")."""
    v = value.strip().lower()
    if v in ("private", "pro"):
        return v
    if v.startswith("part"):
        return "private"
    if v in ("professionnel", "pro.", "boutique"):
        return "pro"
    raise ConfigError(f"seller_type invalide : {value!r} (attendu 'particulier' ou 'pro').")


def _iso_date(value: object, search_name: str) -> str | None:
    """Valide une date "AAAA-MM-JJ" (TOML date ou chaîne), la retourne en texte."""
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError as exc:
        raise ConfigError(
            f"search '{search_name}' : published_after doit être une date AAAA-MM-JJ."
        ) from exc


def _ui_url(value: object) -> str | None:
    if not value:
        return None
    url = str(value).strip().rstrip("/")
    _require(url.startswith(("http://", "https://")), "general.ui_url doit commencer par http(s)://.")
    return url


def _percent(value: object, search_name: str) -> int | None:
    if value is None or value is False:
        return None
    pct = int(value)
    _require(1 <= pct <= 90, f"search '{search_name}' : price_drop_percent entre 1 et 90.")
    return pct


def _schedule(raw: object, search_name: str) -> Schedule | None:
    if not raw:
        return None
    where = f"search '{search_name}' : schedule"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} doit être une table.")
    every = raw.get("every_minutes")
    at = [str(x).strip() for x in raw.get("at", [])]
    _require(bool(every) != bool(at), f"{where} : mettre soit every_minutes, soit at.")
    if every:
        _require(int(every) >= 1, f"{where} : every_minutes doit être >= 1.")
        _require(not raw.get("weekdays") and not raw.get("month_days"),
                 f"{where} : weekdays/month_days vont avec at, pas avec every_minutes.")
        return Schedule(every_minutes=int(every))
    for hhmm in at:
        h, _, m = hhmm.partition(":")
        _require(h.isdigit() and m.isdigit() and int(h) < 24 and int(m) < 60 and len(m) == 2,
                 f"{where} : heure invalide {hhmm!r} (attendu HH:MM).")
    at = [f"{int(x.split(':')[0]):02d}:{x.split(':')[1]}" for x in at]
    weekdays: list[int] = []
    for d in raw.get("weekdays", []):
        key = str(d).strip().lower()[:3]
        _require(key in WEEKDAYS, f"{where} : jour inconnu {d!r} (lun, mar, mer, jeu, ven, sam, dim).")
        weekdays.append(WEEKDAYS.index(key))
    month_days = [int(d) for d in raw.get("month_days", [])]
    _require(all(1 <= d <= 31 for d in month_days), f"{where} : month_days entre 1 et 31.")
    _require(not (weekdays and month_days), f"{where} : weekdays OU month_days, pas les deux.")
    return Schedule(at=sorted(set(at)), weekdays=sorted(set(weekdays)),
                    month_days=sorted(set(month_days)))


def _require(condition: object, message: str) -> None:
    if not condition:
        raise ConfigError(message)


def parse_config(raw: dict) -> Config:
    """Valide le dict issu du TOML et retourne un Config. Lève ConfigError."""
    expanded = _expand_tree(raw)
    assert isinstance(expanded, dict)
    raw = expanded

    general = raw.get("general", {})
    interval = int(general.get("interval_minutes", 15))
    _require(interval >= 1, "general.interval_minutes doit être >= 1 (>= 10 recommandé).")
    data_dir = Path(general.get("data_dir", "./data"))

    searches: list[Search] = []
    for i, s in enumerate(raw.get("search", [])):
        name = str(s.get("name", "")).strip()
        url = str(s.get("url", "")).strip()
        _require(bool(name), f"search[{i}] : 'name' manquant.")
        _require(url.startswith("https://www.leboncoin.fr/"), f"search '{name}' : 'url' doit être une URL leboncoin.fr.")
        searches.append(
            Search(
                name=name,
                url=url,
                max_price=int(s["max_price"]) if s.get("max_price") is not None else None,
                min_price=int(s["min_price"]) if s.get("min_price") is not None else None,
                keywords_include=[str(k).lower() for k in s.get("keywords_include", [])],
                keywords_exclude=[str(k).lower() for k in s.get("keywords_exclude", [])],
                min_ram_gb=int(s["min_ram_gb"]) if s.get("min_ram_gb") is not None else None,
                min_vram_gb=int(s["min_vram_gb"]) if s.get("min_vram_gb") is not None else None,
                vram_ask=bool(s.get("vram_ask", False)),
                min_stick_gb=int(s["min_stick_gb"]) if s.get("min_stick_gb") is not None else None,
                market_kind=str(s["market_kind"]) if s.get("market_kind") else None,
                min_disk_gb=int(s["min_disk_gb"]) if s.get("min_disk_gb") is not None else None,
                regex_all=[str(k) for k in s.get("regex_all", s.get("title_regex_all", []))],
                regex_any=[str(k) for k in s.get("regex_any", [])],
                text_keywords_include=[str(k).lower() for k in s.get("text_keywords_include", [])],
                text_keywords_exclude=[str(k).lower() for k in s.get("text_keywords_exclude", [])],
                cities_include=[str(k).lower() for k in s.get("cities_include", [])],
                cities_exclude=[str(k).lower() for k in s.get("cities_exclude", [])],
                max_age_hours=int(s["max_age_hours"]) if s.get("max_age_hours") is not None else None,
                published_after=_iso_date(s.get("published_after"), name),
                computer_type=str(s["computer_type"]) if s.get("computer_type") else None,
                seller_type=_seller_code(str(s["seller_type"])) if s.get("seller_type") else None,
                condition_include=[str(k).lower() for k in s.get("condition_include", [])],
                min_year=int(s["min_year"]) if s.get("min_year") is not None else None,
                shippable_only=bool(s.get("shippable_only", False)),
                pages=max(1, int(s.get("pages", 10))),
                schedule=_schedule(s.get("schedule"), name),
                price_drop_percent=_percent(s.get("price_drop_percent"), name),
            )
        )
    _require(bool(searches), "Aucune [[search]] définie : ajoute au moins une recherche.")

    n = raw.get("notify", {})
    discord = DiscordConfig(
        enabled=bool(n.get("discord", {}).get("enabled", False)),
        webhook_url=str(n.get("discord", {}).get("webhook_url", "")),
    )
    if discord.enabled:
        _require(discord.webhook_url.startswith("https://"), "notify.discord : webhook_url requis (https).")

    email_raw = n.get("email", {})
    email = EmailConfig(
        enabled=bool(email_raw.get("enabled", False)),
        smtp_host=str(email_raw.get("smtp_host", "")),
        smtp_port=int(email_raw.get("smtp_port", 587)),
        username=str(email_raw.get("username", "")),
        password=str(email_raw.get("password", "")),
        from_addr=str(email_raw.get("from", "")),
        to=[str(x) for x in email_raw.get("to", [])],
    )
    if email.enabled:
        _require(bool(email.smtp_host and email.username and email.to), "notify.email : smtp_host, username, to requis.")

    tg_raw = n.get("telegram", {})
    telegram = TelegramConfig(
        enabled=bool(tg_raw.get("enabled", False)),
        bot_token=str(tg_raw.get("bot_token", "")),
        chat_id=str(tg_raw.get("chat_id", "")),
    )
    if telegram.enabled:
        _require(bool(telegram.bot_token and telegram.chat_id), "notify.telegram : bot_token et chat_id requis.")

    wh_raw = n.get("webhook", {})
    webhook = WebhookConfig(
        enabled=bool(wh_raw.get("enabled", False)),
        url=str(wh_raw.get("url", "")),
        headers={str(k): str(v) for k, v in wh_raw.get("headers", {}).items()},
    )
    if webhook.enabled:
        _require(webhook.url.startswith("http"), "notify.webhook : url requise (http/https).")

    notify = NotifyConfig(discord=discord, email=email, telegram=telegram, webhook=webhook)
    _require(bool(notify.enabled_channels()), "Aucun canal de notification activé (notify.*.enabled).")

    return Config(
        interval_minutes=interval,
        data_dir=data_dir,
        cdp_url=str(general["cdp_url"]) if general.get("cdp_url") else None,
        ui_url=_ui_url(general.get("ui_url")),
        hold_notifications=bool(general.get("hold_notifications", False)),
        searches=searches,
        notify=notify,
    )


def load_config(path: Path) -> Config:
    """Charge un fichier TOML. Charge aussi .env s'il existe à côté (sans dépendance)."""
    env_file = path.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())

    with path.open("rb") as f:
        raw = tomllib.load(f)
    return parse_config(raw)
