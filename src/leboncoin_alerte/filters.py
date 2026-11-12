"""Filtres post-extraction, appliqués côté bot sur le texte complet de l'annonce.

Le texte filtré = titre + description + attributs (voir Listing.search_text()) :
les titres Leboncoin seuls sont trop peu fiables.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import Search
from .model import Listing

# Capacités en Go : "32go", "32 Go", "32 gb"… Le chiffre seul ne suffit pas :
# "SSD 477go", "RTX 3060 Ti (8 Go)" (VRAM)… On décide par CONTEXTE :
# - positif (RAM certaine) : ddr4/ddr5, ram, mémoire (vive), sodimm à proximité
# - négatif (stockage)     : ssd, hdd, nvme, disque, stockage, m.2 à proximité
_RAM_TOKEN = re.compile(r"(\d{1,4})\s?(?:go|gb)\b", re.IGNORECASE)
_RAM_POS = re.compile(r"\b(?:ddr\d?|ram|m[eé]moire(?:\s+vive)?|vive|sodimm)\b", re.IGNORECASE)
_RAM_NEG = re.compile(r"\b(?:ssd|hdd|nvme|disque|stockage|m\.?2)\b", re.IGNORECASE)
# Découpage en segments de phrase : le contexte d'un token est son segment.
# "32 Go DDR5, SSD 512 Go" → 2 segments ("32 Go DDR5" / "SSD 512 Go") : la RAM du
# premier ne "contamine" pas le stockage du second. La virgule décimale ("1,5 To")
# n'est pas séparatrice (pas d'espace après).
_SEGMENT_SEP = re.compile(r"[.;\n]|,\s|\s{2,}|\||/|\s\+\s|\s+et\s+")


def _segments(text: str) -> list[tuple[int, int, str]]:
    """Segments (start, end, contenu) selon les séparateurs de phrase."""
    out: list[tuple[int, int, str]] = []
    start = 0
    for m in _SEGMENT_SEP.finditer(text):
        out.append((start, m.start(), text[start : m.start()]))
        start = m.end()
    out.append((start, len(text), text[start:]))
    return out


def _token_context(text: str, m: re.Match) -> str:
    """Classe un token Go/GB selon son SEGMENT : 'pos' (RAM), 'neg' (stockage), 'neu'."""
    for s_start, s_end, seg in _segments(text):
        if s_start <= m.start() < s_end:
            if _RAM_POS.search(seg):
                return "pos"
            if _RAM_NEG.search(seg):
                return "neg"
            return "neu"
    return "neu"


def _ram_value(text: str) -> int | None:
    """Meilleure estimation de la RAM en Go, ou None.

    Priorité : max des tokens à contexte RAM certain (ddr/ram/mémoire adjacent) ;
    à défaut, max des tokens neutres (chiffre nu). Les tokens en contexte
    stockage sont ignorés ("SSD 477go", "300 go hdd").
    """
    positives: list[int] = []
    neutrals: list[int] = []
    for m in _RAM_TOKEN.finditer(text):
        ctx = _token_context(text, m)
        if ctx == "pos":
            positives.append(int(m.group(1)))
        elif ctx == "neu":
            neutrals.append(int(m.group(1)))
    if positives:
        return max(positives)
    return max(neutrals) if neutrals else None


def _ram_ok(text: str, min_gb: int) -> bool:
    """True si la RAM détectée (contexte) est ≥ min_gb."""
    value = _ram_value(text)
    return value is not None and value >= min_gb


# ── VRAM (mémoire de la carte graphique) ──────────────────────────────────────
# Trois sources, par ordre de fiabilité :
# 1. Go/GB en contexte VRAM certain : "vram", "GDDR6X" dans le SEGMENT du token
#    ("24 Go GDDR6X", "16 Go de VRAM").
# 2. Go/GB collé au modèle GPU : "RTX 5090 32 Go", "4080 Super 16GB".
# 3. Défaut par modèle connu (une 4090 fait 24 Go, toutes variantes confondues) ;
#    None si le modèle est ambigu (5060 Ti = 8 OU 16 Go).
_VRAM_POS = re.compile(r"\b(?:vram|gddr\d[xr]?)\b", re.IGNORECASE)
# « rtx 5090 32 go » / « Zotac 3080 Ti 12Gb » : modèle puis capacité à ≤ 3
# caractères. Bug réel 30/10 : sans « rtx » dans le titre, « 3080 Ti 12Gb »
# n'était pas reconnu. Review 30/10 : le nombre seul ne suffit PAS (« Ryzen
# 5800 32 Go ») — _vram_value exige en plus ti/super ou le préfixe rtx.
_GPU_VRAM_ADJ = re.compile(
    r"(?:rtx[ -]?)?(\d{3,4})\s?(ti|super)?[ -]{0,3}(\d{1,2})\s?(?:go|gb)\b", re.IGNORECASE
)
# VRAM constructeur (Go) par modèle ; None = ambigu (pas de valeur sûre).
_KNOWN_VRAM: dict[str, int | None] = {
    "5090": 32, "5080": 16, "5070ti": 16, "5070": 12, "5060ti": None, "5060": 8,
    "4090": 24, "4080super": 16, "4080": 16, "4070ti": 12, "4070": 12,
    "3090": 24, "3080ti": 12, "3080": 10,
}
_GPU_MODEL_NUM = re.compile(r"rtx[ -]?(\d{3,4})\s*(ti|super)?", re.IGNORECASE)
# Annonces de DEMANDE (achat/échange), pas des ventes : titre qui COMMENCE par
# un marqueur d'intention (bug réel 09/11 : « Cherche carte graphique AMD
# RX9070xt ou Nvidia 5070ti » et « Échange Rx 9070xt contre rtx 5070ti »
# notifiées comme des affaires). Ancré ^ : « PC gamer échange possible » reste
# une vente et ne doit PAS être rejeté.
_WANTED_AD = re.compile(
    r"^\s*(cherche|recherche|échange|echange|trade|wtt[bp])\b", re.IGNORECASE
)
# Intention ÉVOQUÉE sans ouvrir le titre (« RTX 5070 Ti, échange contre 9070XT »,
# « Vends 4070, cherche 5070Ti ») : pas un rejet automatique — l'annonce peut
# rester une vraie vente. Décision mainteneur 09/11 : au doute → question, la revue
# nocturne (agent LLM) tranche. Bug réel 09/11 : seuls les titres COMMENÇANT
# par le marqueur étaient filtrés, tout le reste passait.
_WANTED_AD_ANY = re.compile(
    r"\b(cherche|recherche|échange|echange|trade|wtt[bp])\b", re.IGNORECASE
)


def wanted_ad_weak(title: str) -> bool:
    """True si le titre évoque une recherche/un échange SANS en être clairement
    une (marqueur en milieu de titre). Le runner retient alors l'annonce en
    question pour arbitrage LLM — ni rejet bête, ni notification à tort."""
    return bool(_WANTED_AD_ANY.search(title)) and not _WANTED_AD.match(title)
# VRAM MAX constructeur par modèle (générations courantes en occasion) : une
# capacité adjacente > max = forcément la RAM du PC, pas la VRAM de la carte
# (bug réel 09/11 : « RTX 2070 SUPER 16Go » à 750 € alerté alors que le 16 Go
# désignait la RAM — la 2070 Super n'existe qu'en 8 Go).
_MAX_VRAM: dict[str, int] = {
    "5090": 32, "5080": 16, "5070ti": 16, "5070": 12, "5060ti": 16, "5060": 8,
    "4090": 24, "4080super": 16, "4080": 16, "4070tisuper": 16, "4070ti": 16,
    "4070": 12, "4060ti": 16, "4060": 8,
    "3090": 24, "3080ti": 12, "3080": 12, "3070ti": 8, "3070": 8,
    "3060ti": 8, "3060": 12, "3050": 8,
    "2080ti": 11, "2080super": 8, "2080": 8, "2070super": 8, "2070": 8,
    "2060super": 8, "2060": 6, "1660": 6,
    "1080ti": 11, "1070ti": 8, "1070": 8, "1060": 6,
}


def _vram_value(text: str) -> int | None:
    """Meilleure VRAM explicite détectée en Go, ou None si rien d'explicite.

    Capacité « adjacente » (nombre 3-4 chiffres puis capacité) crusée
    UNIQUEMENT si le nombre est un modèle GPU du texte : suffixe ti/super
    dans le match, ou préfixe rtx pour ce même numéro (review 30/10 :
    « Ryzen 7 5800 32 Go de RAM » ne doit PAS valoir 32 Go de VRAM).
    Si le modèle est connu de _MAX_VRAM, la capacité n'est crue que si
    ≤ VRAM max constructeur — au-delà c'est la RAM du PC (09/11 :
    « 2070 SUPER 16Go » → 8 Go réels, le 16 Go est la RAM).
    """
    rtx_models = {m.group(1) for m in _GPU_MODEL_NUM.finditer(text)}
    candidates: list[int] = []
    for m in _GPU_VRAM_ADJ.finditer(text):
        model, suffix, cap = m.group(1), m.group(2), int(m.group(3))
        key = f"{model}{(suffix or '').replace(' ', '').lower()}"
        max_vram = _MAX_VRAM.get(key, _MAX_VRAM.get(model))
        if max_vram is not None:
            if cap <= max_vram:
                candidates.append(cap)
        elif suffix or model in rtx_models:
            candidates.append(cap)
    for s_start, s_end, seg in _segments(text):
        if _VRAM_POS.search(seg):
            for m in _RAM_TOKEN.finditer(seg):
                candidates.append(int(m.group(1)))
    return max(candidates) if candidates else None


def _vram_model_default(text: str) -> int | None:
    """VRAM du premier modèle GPU reconnu (None si ambigu ou aucun modèle)."""
    m = _GPU_MODEL_NUM.search(text)
    if not m:
        return None
    num = m.group(1)
    suffix = (m.group(2) or "").replace(" ", "").lower()
    return _KNOWN_VRAM.get(f"{num}{suffix}", _KNOWN_VRAM.get(num))


def _vram_ok(text: str, min_gb: int) -> bool:
    """True si la VRAM est établie ≥ min_gb.

    Strict par design (exigence explicite) : explicite d'abord, sinon défaut
    modèle NON ambigu ; modèle ambigu (5060 Ti sans précision) → rejet.
    """
    value = _vram_value(text)
    if value is not None:
        return value >= min_gb
    default = _vram_model_default(text)
    return default is not None and default >= min_gb


# ── Verdict VRAM à trois issues (refonte 29/10/2026) ──────────────────────────
# "ok"       : VRAM établie ≥ min (explicite ou modèle toujours ≥ min)
# "reject"   : VRAM établie < min, modèle connu < min, modèle inconnu,
#              ou AUCUN modèle GPU détecté (exigence explicite)
# "question" : modèle connu AMBIGU (existe en 8 et 16 Go, ex. 5060 Ti) sans
#              précision → question à poser au vendeur, pas un rejet.
def vram_verdict(text: str, min_gb: int) -> str:
    """Classe l'annonce pour le critère VRAM ≥ min_gb."""
    value = _vram_value(text)
    if value is not None:
        return "ok" if value >= min_gb else "reject"
    m = _GPU_MODEL_NUM.search(text)
    if not m:
        return "reject"  # aucun modèle GPU mentionné
    num = m.group(1)
    suffix = (m.group(2) or "").replace(" ", "").lower()
    key = f"{num}{suffix}"
    if key not in _KNOWN_VRAM and num not in _KNOWN_VRAM:
        return "reject"  # modèle inconnu : pas de valeur sûre
    default = _KNOWN_VRAM.get(key, _KNOWN_VRAM.get(num))
    if default is None:
        return "question"  # ambigu : 5060 Ti = 8 OU 16 Go
    return "ok" if default >= min_gb else "reject"


def ambiguous_gpu_model(text: str) -> str | None:
    """Modèle ambigu détecté (ex. "5060ti"), affiché dans la question vendeur."""
    m = _GPU_MODEL_NUM.search(text)
    if not m:
        return None
    num = m.group(1)
    suffix = (m.group(2) or "").replace(" ", "").lower()
    key = f"{num}{suffix}"
    if key in _KNOWN_VRAM and _KNOWN_VRAM[key] is None:
        return f"RTX {num}{' Ti' if suffix == 'ti' else (' Super' if suffix == 'super' else '')}"
    if key not in _KNOWN_VRAM and num in _KNOWN_VRAM and _KNOWN_VRAM[num] is None:
        return f"RTX {num}"
    return None


def seller_question(model: str) -> str:
    """Message de prise d'information à envoyer au vendeur, comme un humain."""
    return (
        f"Bonjour, votre annonce mentionne une {model} : pourriez-vous me confirmer "
        "la version exacte (notamment la quantité de mémoire vidéo, 8 ou 16 Go) ? "
        "Merci d'avance !"
    )


# ── Barrettes RAM : capacité d'UNE barrette ───────────────────────────────────
# "32 Go (2x16 Go)" = kit de deux barrettes de 16 → 16 par barrette, PAS 32.
# Patterns : "2x16", "2 x 32Go", "1x48", "4x16" et la variante "32Go x2"
# (unité Go optionnelle : "(2x32)" est courant). Plausibilité : 4-128 Go.
_STICK_KIT = re.compile(r"\b(\d)\s?[x×]\s?(\d{1,3})(?:\s?(?:go|gb))?\b", re.IGNORECASE)
_STICK_KIT_REV = re.compile(r"\b(\d{1,3})\s?(?:go|gb)\s?[x×]\s?(\d)\b", re.IGNORECASE)


def _stick_config(text: str) -> tuple[int, int] | None:
    """(capacité par barrette, nombre de barrettes), ou None.

    "64 Go (2x32)" → (32, 2) ; "Barrette 32 Go DDR5" → (32, 1) ;
    "8 Go" seul → (8, 1). None si aucune capacité RAM déterminable.
    """
    per_stick: list[tuple[int, int]] = []
    for m in _STICK_KIT.finditer(text):
        value = int(m.group(2))
        if 4 <= value <= 128:  # hors de cette plage : pas une capacité RAM
            per_stick.append((value, int(m.group(1))))
    for m in _STICK_KIT_REV.finditer(text):
        value = int(m.group(1))
        if 4 <= value <= 128:
            per_stick.append((value, int(m.group(2))))
    if per_stick:
        # Le kit le plus petit mentionné gouverne (descriptions contradictoires).
        return min(per_stick)
    ram = _ram_value(text)
    return (ram, 1) if ram is not None else None


def _stick_ok(text: str, min_gb: int) -> bool:
    """True si chaque barrette fait ≥ min_gb (2x16 avec min 32 → False).

    Exception actée avec mainteneur (31/10) : le kit 4×16 Go (total 64) est
    accepté même si la barrette fait 16 Go — 4×16 = 64, l'objectif final.
    Un 2×16 ou une barrette de 16 seule reste rejetés.
    """
    config = _stick_config(text)
    if config is None:
        return False
    per_stick, count = config
    if per_stick >= min_gb:
        return True
    return (per_stick, count) == (16, 4)


def _parse_dt(value: str | None) -> datetime | None:
    """Parse une date Leboncoin (ISO ou "2026-10-06 00:47:21"), naive → UTC."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace(" ", "T"))
    except ValueError:
        return None
    return dt.replace(tzinfo=dt.tzinfo or UTC)


def _day_start(day: str) -> datetime:
    """Minuit (heure de Paris) du jour "AAAA-MM-JJ", en datetime aware."""
    try:
        tz = ZoneInfo("Europe/Paris")
    except ZoneInfoNotFoundError:  # image sans tzdata : UTC, écart d'une heure ou deux
        tz = UTC
    return datetime.combine(date.fromisoformat(day), time.min, tzinfo=tz)


def first_reject(listing: Listing, search: Search) -> str | None:
    """Label du PREMIER critère échoué, ou None si l'annonce passe tout.

    Utilisé pour l'audit : la raison est persistée en base avec le texte
    complet de l'annonce, ce qui permet de rejouer a posteriori la décision
    et de détecter un filtre (ou un parseur) qui rejette trop large.
    Ordre strictement identique à l'évaluation historique de `matches`.

    Règle générale : un critère VIDE (None/[]) n'est pas appliqué ; quand un
    critère est défini mais que l'annonce n'a pas l'information, on est
    permissif SAUF pour les critères où c'est une exigence explicite (prix
    min/max sur prix connu, villes, mots-clés exclude, regex).
    """
    if (
        search.max_price is not None
        and listing.price is not None
        and listing.price > search.max_price
    ):
        return "max_price"
    if (
        search.min_price is not None
        and listing.price is not None
        and listing.price < search.min_price
    ):
        return "min_price"
    text = listing.search_text()
    text_lower = text.lower()
    title = listing.title.lower()
    # Demande d'achat / annonce d'échange : ce n'est pas une vente, aucune
    # recherche n'y peut être une affaire — rejet global avant tout critère.
    if _WANTED_AD.match(title):
        return "wanted_ad"
    if search.keywords_include and not any(k in title for k in search.keywords_include):
        return "keywords_include"
    if search.keywords_exclude and any(k in title for k in search.keywords_exclude):
        return "keywords_exclude"
    if search.text_keywords_include and not any(k in text_lower for k in search.text_keywords_include):
        return "text_keywords_include"
    if search.text_keywords_exclude and any(k in text_lower for k in search.text_keywords_exclude):
        return "text_keywords_exclude"
    if search.min_ram_gb is not None and not _ram_ok(text, search.min_ram_gb):
        return "min_ram_gb"
    if search.min_stick_gb is not None and not _stick_ok(text, search.min_stick_gb):
        return "min_stick_gb"
    if search.min_vram_gb is not None:
        verdict = vram_verdict(text, search.min_vram_gb)
        # Sans vram_ask, comportement historique strict : ambigu → rejet.
        # Avec vram_ask, l'ambigu passe le filtre (le runner le marque
        # 'question' pour prise d'info vendeur au lieu de le rejeter).
        if verdict == "reject" or (verdict == "question" and not search.vram_ask):
            return "min_vram_gb"
    if search.min_disk_gb is not None:
        total = _storage_total_gb(text)
        if total is None or total < search.min_disk_gb:
            return "min_disk_gb"
    # Regex : TOUTES doivent matcher le texte complet (AND), insensible à la casse.
    for pattern in search.regex_all:
        if not re.search(pattern, text, re.IGNORECASE):
            return "regex_all"
    # regex_any : au moins UNE doit matcher (OU).
    if search.regex_any and not any(
        re.search(pattern, text, re.IGNORECASE) for pattern in search.regex_any
    ):
        return "regex_any"
    # Villes : include exige une ville connue qui matche ; exclude rejette.
    if search.cities_include and (
        not listing.city or not any(c in listing.city.lower() for c in search.cities_include)
    ):
        return "cities_include"
    if search.cities_exclude and listing.city and any(
        c in listing.city.lower() for c in search.cities_exclude
    ):
        return "cities_exclude"
    # Fraîcheur : seulement si la date de publication est exploitable.
    if search.max_age_hours is not None:
        published = _parse_dt(listing.published_at)
        if published is not None and published < datetime.now(UTC) - timedelta(
            hours=search.max_age_hours
        ):
            return "max_age_hours"
    if search.published_after:
        published = _parse_dt(listing.published_at)
        if published is not None and published < _day_start(search.published_after):
            return "published_after"
    # Champs structurés : ignorés si l'annonce ne les renseigne pas (None).
    if (
        search.computer_type
        and listing.computer_type
        and listing.computer_type.strip().lower() != search.computer_type.strip().lower()
    ):
        return "computer_type"
    if search.seller_type and listing.seller_type and listing.seller_type != search.seller_type:
        return "seller_type"
    if (
        search.condition_include
        and listing.condition
        and not any(c in listing.condition.lower() for c in search.condition_include)
    ):
        return "condition_include"
    if search.min_year is not None and listing.year is not None and listing.year < search.min_year:
        return "min_year"
    if search.shippable_only and listing.shippable is False:
        return "shippable_only"
    return None


def matches(listing: Listing, search: Search) -> bool:
    """True si l'annonce passe les filtres de la recherche."""
    return first_reject(listing, search) is None


def apply_filters(listings: list[Listing], search: Search) -> list[Listing]:
    return [lst for lst in listings if matches(lst, search)]


# Modèles GPU détectables dans le texte (RTX/GTX + numéro de modèle).
_GPU_MODEL = re.compile(r"\b(?:rtx|gtx)[ -]?(\d{3,4})", re.IGNORECASE)

# CPU détectables : Intel Core i3/i5/i7/i9 ("i5-13600K"), AMD Ryzen ("Ryzen 7 5700X"),
# Threadripper, Xeon.
_CPU_MODEL = re.compile(
    r"\b(?:"
    r"i[3579][\s-]?\d{4,5}[a-z0-9]{0,3}"  # i5-13600K, i9 13900HX
    r"|ryzen\s?\d\s?\d{3,4}[a-z0-9]{0,3}"  # Ryzen 5 5600, Ryzen 7 5700X3D
    r"|threadripper\s?\d{4}[a-z0-9]{0,2}"
    r"|xeon\s?[a-z0-9-]{2,10}"
    r")\b",
    re.IGNORECASE,
)


def detected_cpu(text: str) -> str | None:
    """Extrait le processeur mentionné (premier trouvé), normalisé pour l'affichage."""
    m = _CPU_MODEL.search(text)
    if not m:
        return None
    cpu = " ".join(m.group(0).upper().split())
    return cpu.replace("RYZEN ", "Ryzen ").replace("I5", "i5").replace("I3", "i3") \
        .replace("I7", "i7").replace("I9", "i9").replace("XEON", "Xeon") \
        .replace("THREADRIPPER", "Threadripper")


def detected_specs(text: str) -> tuple[str | None, int | None]:
    """Extrait (gpu, ram_gb) d'un texte d'annonce, pour l'affichage dans les alertes.

    gpu = premier modèle trouvé ("RTX 4060"), ram = plus grande capacité
    mentionnée hors contexte stockage (Go).
    """
    gpu_m = _GPU_MODEL.search(text)
    if not gpu_m:
        return None, None
    gpu = " ".join(gpu_m.group(0).upper().replace("GTX", "GTX ").replace("RTX", "RTX ").split())
    return gpu, _ram_value(text)


# Stockage : "1 To", "512 Go"… Un token en To est toujours du stockage ; un Go/GB
# l'est si son contexte adjacent est ssd/hdd/nvme/disque/stockage/m.2.
_STORAGE_TOKEN = re.compile(r"(\d{1,4}(?:[.,]\d+)?)\s?(to|go|gb)\b", re.IGNORECASE)


def detected_storage(text: str) -> str | None:
    """Capacités de stockage mentionnées, formatées ("1 To + 477 Go"), ou None."""
    parts: list[str] = []
    for m in _STORAGE_TOKEN.finditer(text):
        unit = m.group(2).lower()
        value = m.group(1).replace(",", ".").rstrip(".")
        if unit == "to" or _token_context(text, m) == "neg":
            parts.append(f"{value} {'To' if unit == 'to' else 'Go'}")
    return " + ".join(parts) if parts else None


def _storage_total_gb(text: str) -> int | None:
    """Stockage TOTAL en Go (somme des disques, 1 To = 1024 Go), ou None."""
    total = 0
    found = False
    for m in _STORAGE_TOKEN.finditer(text):
        unit = m.group(2).lower()
        if unit != "to" and _token_context(text, m) != "neg":
            continue  # pas identifié comme du stockage
        found = True
        value = float(m.group(1).replace(",", "."))
        total += value * 1024 if unit == "to" else value
    return int(total) if found else None
