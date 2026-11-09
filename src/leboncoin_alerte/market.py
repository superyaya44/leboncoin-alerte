"""Référentiel de prix : compare le prix d'une annonce au NEUF idealo.

Source : table `retail_prices` (passe hebdo du cron lbc-retail-hebdo, retail.py).
Décision mainteneur 09/11 : plus de prix d'occasion auto-appris (système démonté —
il se polluait par construction, cartes mortes « pour pièces » comptées comme
des ventes normales). L'écart % est donc TOUJOURS un écart vs prix NEUF.

Usage : `deal(listing, refs)` retourne (label, ref_eur, pct) où pct > 0 signifie
sous le neuf de pct % — ou None si aucun modèle reconnu.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from .filters import _stick_config
from .model import Listing

# Patterns GPU ordonnés : variantes (super/ti) AVANT les modèles nus pour que
# "RTX 4080 Super" ne matche pas la future référence d'un 4080 nu.
_GPU_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("rtx 5090", re.compile(r"rtx[ -]?5090", re.IGNORECASE)),
    ("rtx 5080", re.compile(r"rtx[ -]?5080", re.IGNORECASE)),
    ("rtx 5070 ti", re.compile(r"rtx[ -]?5070[ -]?ti", re.IGNORECASE)),
    ("rtx 5060 ti", re.compile(r"rtx[ -]?5060[ -]?ti", re.IGNORECASE)),
    ("rtx 4090", re.compile(r"rtx[ -]?4090", re.IGNORECASE)),
    ("rtx 4080 super", re.compile(r"rtx[ -]?4080[ -]?super", re.IGNORECASE)),
    ("rtx 4080", re.compile(r"rtx[ -]?4080", re.IGNORECASE)),
    ("rtx 3090", re.compile(r"rtx[ -]?3090", re.IGNORECASE)),
    ("rtx 3080", re.compile(r"rtx[ -]?3080", re.IGNORECASE)),
]

_GPU_MENTION = re.compile(r"\b(?:rtx|gtx)\b", re.IGNORECASE)


class MarketConflictError(Exception):
    """Modèles GPU suivis contradictoires (titre vs description) : le choix de
    la référence de prix n'est pas tranchable automatiquement — la revue
    nocturne (agent LLM) décide. Décision mainteneur 09/11 : au doute, arbitrage
    LLM, jamais une heuristique figée qui peut se tromper dans les deux sens."""

    def __init__(self, models: list[str]):
        super().__init__(f"modèles GPU contradictoires : {', '.join(models)}")
        self.models = models


def _model_counts(title: str, text: str) -> tuple[dict[str, int], dict[str, int]]:
    """Occurrences par modèle, SANS double comptage des variantes (4080 Super
    ne compte pas aussi comme 4080) : patterns consommés dans l'ordre de
    spécificité. Retourne (comptes_titre, comptes_texte)."""
    title_counts: dict[str, int] = {}
    text_counts: dict[str, int] = {}
    for source, acc in ((title, title_counts), (text, text_counts)):
        remaining = source
        for label, pattern in _GPU_PATTERNS:
            n = len(pattern.findall(remaining))
            if n:
                acc[label] = acc.get(label, 0) + n
                remaining = pattern.sub(" ", remaining)
    return title_counts, text_counts


def gpu_ref_resolved(
    title: str, body: str, refs: dict[str, dict[str, int]]
) -> tuple[str, int] | None:
    """(label, réf €) — None si aucun modèle suivi — MarketConflictError au doute.

    `body` = description + attributs SANS le titre (le titre est compté à part).

    Résolution (v2, décision mainteneur 09/11 : au doute, arbitrage LLM nocturne) :
    1. Titre qui nomme un GPU NON suivi (RTX 4070) → pas de ligne marché,
       même si la description cite un modèle suivi (« comme une RTX 5090 »).
    2. Un seul modèle suivi → lui.
    3. Titre suivi + corps qui le CONFIRME ou ex æquo (spam keyword type
       « RTX5090 / RTX5080 / RTX4090 ») → le modèle du titre.
    4. Titre suivi + corps qui le CONTREDIT avec un dominant clair (specs
       répètent une autre carte : le titre est faux) → MarketConflictError.
    5. Titre sans modèle suivi : dominant strict du corps, sinon conflit.
    """
    table = refs.get("gpu", {})
    title_counts, body_counts = _model_counts(title, body)
    followed = {
        label: int(table[label.replace(" ", "-")])
        for label in set(title_counts) | set(body_counts)
        if label.replace(" ", "-") in table
    }
    if not followed:
        return None
    title_followed = [l for l in followed if title_counts.get(l, 0) > 0]
    body_followed = {l: body_counts.get(l, 0) for l in followed if body_counts.get(l, 0) > 0}

    if not title_followed:
        if _GPU_MENTION.search(title):
            return None  # le titre nomme un GPU non suivi : pas de comparaison
        if len(followed) == 1:
            label = next(iter(followed))
            return label, followed[label]
        if body_followed:
            dom = max(body_followed, key=lambda l: body_followed[l])
            rest = [c for l, c in body_followed.items() if l != dom]
            if not rest or body_followed[dom] > max(rest):
                return dom, followed[dom]
        raise MarketConflictError(sorted(followed))

    if len(title_followed) == 1:
        t = title_followed[0]
        if body_followed:
            dom = max(body_followed, key=lambda l: body_followed[l])
            rest = [c for l, c in body_followed.items() if l != dom]
            strict = not rest or body_followed[dom] > max(rest)
            if dom != t and strict:
                # Le corps désigne clairement une AUTRE carte que le titre :
                # l'un des deux ment — la revue nocturne (LLM) tranche.
                raise MarketConflictError(sorted(followed))
        return t, followed[t]

    raise MarketConflictError(sorted(followed))  # plusieurs modèles suivis au titre

_DDR = re.compile(r"\bddr([45])\b", re.IGNORECASE)

# Capacités RAM ayant une référence (les autres : pas de ligne marché).
_RAM_CAPS = (32, 48, 64)


def gpu_ref(text: str, refs: dict[str, dict[str, int]]) -> tuple[str, int] | None:
    """Premier modèle GPU reconnu dans le texte AVEC une référence disponible."""
    gpu_table = refs.get("gpu", {})
    for label, pattern in _GPU_PATTERNS:
        if pattern.search(text):
            key = label.replace(" ", "-")
            if key in gpu_table:
                return label, int(gpu_table[key])
            return None  # modèle reconnu mais sans référence : pas de ligne marché
    return None


def ram_ref(text: str, refs: dict[str, dict[str, int]]) -> tuple[str, int] | None:
    """Config RAM (DDR + capacité PAR BARRETTE) reconnue avec une référence.

    Les références RAM sont exprimées PAR BARRETTE : pour un kit 2x32, la
    référence comparée est réf_barrette × 2. Cohérent pour barrettes seules
    et kits, quelle que soit la configuration détectée.
    """
    ram_table = refs.get("ram", {})
    if not ram_table:
        return None
    ddr = _DDR.search(text)
    if not ddr:
        return None
    config = _stick_config(text)
    if config is None:
        return None
    per_stick, count = config
    if per_stick not in _RAM_CAPS:
        return None
    key = f"ddr{ddr.group(1)}-{per_stick}"
    if key not in ram_table:
        return None
    return key, int(ram_table[key]) * max(1, count)


def deal(
    listing: Listing,
    refs: dict[str, dict[str, int]],
    kinds: tuple[str, ...] | None = None,
) -> tuple[str, int, int] | None:
    """(label, réf €, pct) pour une annonce, ou None.

    pct = (réf - prix) / réf × 100, arrondi : positif = sous le marché.
    Priorité GPU (un PC avec GPU > RAM seule), sinon RAM — SAUF si `kinds`
    restreint (recherche RAM : une description qui mentionne un GPU reste
    une annonce RAM, jamais comparée au prix d'une carte).

    GPU mentionné sans référence disponible (ex. RTX 4070 non suivie) :
    PAS de fallback RAM — sinon un PC complet à 1000 € se retrouverait
    comparé au prix d'une barrette (bug réel 27/10 : "+567 %", réf ddr4-32).
    """
    if not refs or listing.price is None or listing.price <= 0:
        return None
    text = listing.search_text()
    allowed = kinds or ("gpu", "ram")
    found = None
    if "gpu" in allowed:
        # Résolution v2 (décision mainteneur 09/11) : titre vs CORPS de l'annonce
        # (description + attributs, sans le titre) ; contradiction réelle →
        # MarketConflictError → le runner retient l'annonce en question pour
        # arbitrage LLM (revue nocturne). JAMAIS de choix automatique figé
        # quand le doute est légitime.
        body = " \n ".join(t for t in (listing.body, listing.attributes_text) if t)
        found = gpu_ref_resolved(listing.title, body, refs)
        if found is None and _GPU_MENTION.search(text):
            return None  # GPU présent mais non suivi : pas de ligne marché
    if found is None and "ram" in allowed:
        found = ram_ref(text, refs)
    if not found:
        return None
    label, ref = found
    pct = round((ref - listing.price) / ref * 100)
    return label, ref, pct


# ── Références de prix ───────────────────────────────────────────────────────
# Décision mainteneur 09/11 : PLUS DE PRIX D'OCCASION APPRIS. Le système
# auto-apprenant (observations Leboncoin → médiane glissante 30 j) a été
# démonté : il se polluait par construction (cartes mortes « pour pièces »
# enregistrées comme des ventes normales → réf 4090 tombée à ~470 € alors
# que le marché réel est à ~1600 €). Seule référence désormais : le NEUF
# idealo, rafraîchi chaque semaine (retail.py + cron lbc-retail-hebdo).
# L'écart % affiché compare donc TOUJOURS au prix neuf — pas d'ambiguïté.


def retail_refs(store) -> dict[str, dict[str, int]]:
    """Références NEUF idealo (passe hebdo) → {"gpu"/"ram": {key: prix}}.

    On ne retient que la ligne la plus récente de chaque clé : une semaine
    sans donnée pour un modèle ne fait pas régresser sa référence vers
    l'occasion (le NEUF prime, décision 29/10/2026).
    """
    refs: dict[str, dict[str, int]] = {}
    seen: set[str] = set()
    for key, price, _n, _mn, _mx, _pass_at in store.retail_rows():  # pass_at DESC
        if key in seen:
            continue
        seen.add(key)
        kind = "gpu" if key.startswith("rtx") else "ram"
        refs.setdefault(kind, {})[key] = int(price)
    return refs


def occasion_refs(store, max_age_days: int = 7) -> dict[str, dict[str, int]]:
    """Références OCCASION des modèles arrêtés (scan multi-sources ≤ 7 j).

    Règle J-7 (mainteneur 09/11) : une référence de plus de 7 jours est ignorée —
    le bot retombe alors sur le neuf idealo pour cette clé. Seuls les statuts
    ok / source-unique donnent une valeur ; divergence = pas de consensus.
    """
    limite = (datetime.now(UTC) - timedelta(days=max_age_days)).isoformat()
    refs: dict[str, dict[str, int]] = {}
    seen: set[str] = set()
    for key, median, _plafond, status, fetched_at in store.occasion_rows():
        if key in seen or median is None:
            continue
        if fetched_at < limite:
            continue  # plus fraîche ligne utile déjà trouvée ou règle J-7
        seen.add(key)
        refs.setdefault("gpu", {})[key] = int(median)
    return refs
