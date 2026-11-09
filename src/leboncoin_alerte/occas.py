"""Passe prix OCCASION multi-sources — cron 2×/semaine (modèles arrêtés).

Décision mainteneur 09/11 : plus de prix d'occasion appris en observant les
annonces croisées (système démonté — il se polluait par construction).
Pour les modèles ARRÊTÉS (4090, 3090…), le neuf idealo est une référence
morte (stock raréfié à prix gonflé) : la référence devient un scan
DÉTERMINISTE d'agrégateurs, avec la contrainte de fraîcheur absolue :

    RÈGLE J-7 — une source dont les données ont plus de 7 jours ne
    nourrit JAMAIS le consensus (Zeus = mensuel → hors consensus par
    construction ; eBay/pcprice = marché séparé, plafond uniquement).

Garde-fous (validés avec mainteneur 09/11) :
    - sous-échantillon n < 20 → source exclue pour la clé
    - écart > 25 % entre sources valides → PAS de consensus, statut
      "divergence" (la référence précédente ≤ 7 j est conservée ;
      à défaut le bot retombe sur le neuf idealo en signalant)
    - eBay (pcprice.watch) = plafond ventes réelles, hors consensus
      (biais international : acheteurs IA/pro cross-border)

Sources (page par page, navigateur CDP comme idealo — bot-walls) :
    - Flipdeal   : flipdeal.fr/prix/carte-graphique/<clé> (màj quotidienne)
    - Racingtech : racingtech.fr/fr/gpu/<clé> (30 j glissants, lissée 7 j)
    - PCPrice    : pcprice.watch/search/<MODÈLE>/FR/GPU (eBay vendues, plafond)
"""

from __future__ import annotations

import re
import time
from datetime import UTC, date, datetime

# Modèles ARRÊTÉS seulement — les modèles en production restent sur le neuf
# idealo (l'écart occasion/neuf y est lisible directement). Étendre cette
# liste = décision mainteneur.
MODELS: dict[str, str] = {
    "rtx-4090": "RTX 4090",
    "rtx-3090": "RTX 3090",
}

CDP_ENDPOINT = "http://127.0.0.1:9222"
PAUSE_MODELE = 6.0  # s entre modèles
MAX_AGE_DAYS = 7  # règle J-7 — au-delà, la source ne nourrit pas le consensus
MIN_N = 20  # sous cet effectif, la source est jugée non représentative
MAX_GAP = 0.25  # écart relatif max entre sources valides d'un même consensus

_MOIS_FR = {
    "janvier": 1, "février": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
    "juillet": 7, "août": 8, "septembre": 9, "octobre": 10, "novembre": 11,
    "décembre": 12,
}
_MOIS_EN = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}
# Abréviations anglaises (« as of Nov 9, 2026 »)
_MOIS_EN.update({m[:3].lower(): i for m, i in _MOIS_EN.items()})

# La locale FR utilise espaces fines/insécables comme séparateur de milliers.
_NUM = r"([\d\u00a0\u202f\u2009 ]+)"


def _num(s: str) -> int | None:
    digits = re.sub(r"[^\d]", "", s)
    return int(digits) if digits else None


def _age_days(d: date | None, today: date | None = None) -> int | None:
    if d is None:
        return None
    return ((today or datetime.now(UTC).date()) - d).days


def _date_fr(text: str, pattern: str) -> date | None:
    m = re.search(pattern.replace("(\\d{4})", r"(\d{4})"), text, re.IGNORECASE)
    if not m:
        return None
    jour, mois, annee = m.group(1), _MOIS_FR.get(m.group(2).lower()), m.group(3)
    if not mois:
        return None
    try:
        return date(int(annee), mois, int(jour))
    except ValueError:
        return None


# ── Flipdeal ─────────────────────────────────────────────────────────────────
# meta description : « 1 700 € en prix médian sur 31 annonces ces 30 jours
# (de 1 400 € à 3 000 €) » + « Prix relevés le 9 novembre 2026 ».
_RE_FLIP_MED = re.compile(
    rf"{_NUM}\s*€ en prix médian sur (\d+) annonces ces 30 jours"
    rf" \(de {_NUM}\s*€ à {_NUM}\s*€\)")


def parse_flipdeal(html: str) -> dict:
    m = _RE_FLIP_MED.search(html)
    out: dict = {"source": "flipdeal"}
    if not m:
        out["erreur"] = "médiane introuvable (structure de page changée ?)"
        return out
    out["mediane"] = _num(m.group(1))
    out["n"] = int(m.group(2))
    out["min"] = _num(m.group(3))
    out["max"] = _num(m.group(4))
    d = _date_fr(html, r"relevés le (\d{1,2}) (\w+) (\d{4})")
    out["date"] = d.isoformat() if d else None
    return out


# ── Racingtech ───────────────────────────────────────────────────────────────
# Page protégée par un mur anti-bot : le HTML capturé trop tôt est une page
# « Vérification RacingTech ». On parse donc le TEXTE du body, après attente.
# « la cote occasion de la RTX 4090 est de 1 675 € » + « 56 annonces » +
# « Mis à jour le 9 novembre 2026 ».
def parse_racingtech(text: str, model_display: str) -> dict:
    out: dict = {"source": "racingtech"}
    nom = re.escape(model_display).replace(r"\ ", r"[ ]?")
    m = re.search(
        rf"cote occasion[^.\n]{{0,60}}?{nom}[^.\n]{{0,60}}?est de {_NUM}\s*€",
        text, re.IGNORECASE)
    if not m:
        out["erreur"] = "cote introuvable (mur anti-bot ou structure changée ?)"
        return out
    out["mediane"] = _num(m.group(1))
    m_n = re.search(r"(\d+) annonces", text)
    out["n"] = int(m_n.group(1)) if m_n else None
    d = _date_fr(text, r"Mis à jour le (\d{1,2}) (\w+) (\d{4})")
    out["date"] = d.isoformat() if d else None
    return out


# ── PCPrice.watch (eBay vendues — PLAFOND, hors consensus) ───────────────────
# « median of €2,447 based on 97 real eBay sold listings » +
# « as of Nov 9, 2026 · refreshed every 8 hours ».
def parse_pcprice(text: str) -> dict:
    out: dict = {"source": "pcprice-ebay", "role": "plafond"}
    m = re.search(
        r"median of €([\d\u00a0\u202f\u2009 ,]+) based on (\d+) real eBay sold", text)
    if not m:
        out["erreur"] = "médiane eBay introuvable"
        return out
    out["mediane"] = _num(m.group(1))
    out["n"] = int(m.group(2))
    m_d = re.search(r"as of (\w+) (\d{1,2}), (\d{4})", text)
    if m_d and m_d.group(1).lower() in _MOIS_EN:
        out["date"] = date(int(m_d.group(3)), _MOIS_EN[m_d.group(1).lower()],
                           int(m_d.group(2))).isoformat()
    else:
        out["date"] = None
    return out


# ── Garde-fous + consensus ───────────────────────────────────────────────────
def _valide(s: dict, today: date) -> tuple[bool, str]:
    """(ok, raison) — une source nourrit le consensus si fraîche et n ≥ 20."""
    if s.get("erreur"):
        return False, s["erreur"]
    if s.get("mediane") is None:
        return False, "pas de médiane"
    if not s.get("date"):
        return False, "date des données absente → exclu (règle J-7)"
    age = _age_days(date.fromisoformat(s["date"]), today)
    if age is None or age < 0 or age > MAX_AGE_DAYS:
        return False, f"données de {s['date']} ({age} j) → exclu (règle J-7)"
    if not s.get("n") or s["n"] < MIN_N:
        return False, f"n={s.get('n')} < {MIN_N} → non représentatif"
    return True, ""


def consensus(sources: list[dict], today: date | None = None) -> dict:
    """Consensus d'une clé à partir des sources parsées.

    Le plafond eBay (pcprice) ne participe jamais au consensus mais est
    conservé dans le résultat pour la synthèse.
    """
    today = today or datetime.now(UTC).date()
    candidats, exclus, plafond = [], [], None
    for s in sources:
        if s.get("role") == "plafond":
            ok, raison = _valide(s, today)
            plafond = s if ok else {**s, "exclu": raison}
            continue
        ok, raison = _valide(s, today)
        (candidats if ok else exclus).append(s if ok else {**s, "exclu": raison})

    out: dict = {"sources": candidats, "exclus": exclus, "plafond": plafond}
    if not candidats:
        out["status"] = "aucune-source"
        return out
    med = sorted(c["mediane"] for c in candidats)
    if len(med) == 1:
        out["mediane"] = med[0]
        out["status"] = "source-unique"
        return out
    ecart = (max(med) - min(med)) / min(med)
    if ecart > MAX_GAP:
        out["status"] = "divergence"
        out["ecart"] = round(ecart * 100)
        out["detail"] = (
            f"écart {round(ecart * 100)} % entre sources valides "
            f"({', '.join(str(m) for m in med)}) > {int(MAX_GAP * 100)} % — "
            "pas de consensus, la référence reste la dernière valeur ≤ 7 j")
        return out
    n = len(med)
    out["mediane"] = med[n // 2] if n % 2 else round((med[n // 2 - 1] + med[n // 2]) / 2)
    out["status"] = "ok"
    return out


# ── Passe navigateur (CDP, comme idealo — les sites bloquent curl) ────────────
def _urls(key: str) -> dict[str, str]:
    display = MODELS[key]
    return {
        "flipdeal": f"https://flipdeal.fr/prix/carte-graphique/{key}",
        "racingtech": f"https://www.racingtech.fr/fr/gpu/{key}",
        "pcprice": f"https://www.pcprice.watch/search/{display.replace(' ', '')}/FR/GPU",
    }


def run_pass(keys: list[str] | None = None, cdp: str = CDP_ENDPOINT,
             today: date | None = None) -> list[dict]:
    """Passe occasion complète (ou partielle) : scrape + consensus par clé."""
    from playwright.sync_api import sync_playwright

    todo = keys or list(MODELS)
    results: list[dict] = []
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(cdp)
        page = browser.contexts[0].new_page()
        try:
            for key in todo:
                entry: dict = {"clef": key}
                sources: list[dict] = []
                for nom, url in _urls(key).items():
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                        time.sleep(3)
                        if nom == "flipdeal":
                            sources.append(parse_flipdeal(page.content()))
                        elif nom == "racingtech":
                            # Mur anti-bot : le corps peut être la page de
                            # vérification au premier essai → une 2e lecture.
                            r = parse_racingtech(page.inner_text("body"), MODELS[key])
                            if "erreur" in r:
                                time.sleep(8)
                                r = parse_racingtech(page.inner_text("body"), MODELS[key])
                            sources.append(r)
                        else:
                            sources.append(parse_pcprice(page.inner_text("body")))
                    except Exception as e:  # noqa: BLE001 — journaliser, continuer
                        sources.append({"source": nom, "erreur": repr(e)[:150]})
                    time.sleep(2)
                entry.update(consensus(sources, today))
                entry["urls"] = _urls(key)
                results.append(entry)
                time.sleep(PAUSE_MODELE)
        finally:
            page.close()
    return results


# ── Check modèles arrêtés (tenue de la liste MODELS) ──────────────────────────
# Source fiable : l'argus Zeus-PC marque CHAQUE modèle « plus vendue neuve »
# (attribut data-neuf="0" + libellé). L'absence idealo (2 passes hebdo sans
# prix) sert de corroboration, pas de décision. Le check SIGNALE, l'ajout à
# MODELS reste une décision de mainteneur — jamais automatique (règle 09/11).
ZEUS_ARGUS_URL = "https://zeus-pc.com/argus-occasion/"

_RE_ZEUS_ROW = re.compile(r'<tr[^>]*data-name="([^"]+)"[^>]*data-neuf="(\d+)"')
_RE_MODELE = re.compile(r"rtx\s*\d{4}(?:\s*(?:super|ti))?", re.IGNORECASE)


def _key_depuis_nom(nom: str) -> str | None:
    m = _RE_MODELE.search(nom)
    return m.group(0).lower().replace(" ", "-") if m else None


def parse_zeus_arrets(html: str) -> dict[str, bool]:
    """{clé modèle → True si « plus vendue neuve »} pour toute la table Zeus."""
    arrets: dict[str, bool] = {}
    for nom, neuf in _RE_ZEUS_ROW.findall(html):
        key = _key_depuis_nom(nom)
        if key:
            arrets[key] = neuf == "0" or arrets.get(key, False)
    return arrets


def fetch_zeus_arrets(cdp: str = CDP_ENDPOINT) -> dict[str, bool]:
    """Ouvre l'argus Zeus (page statique) et retourne les modèles arrêtés."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(cdp)
        page = browser.contexts[0].new_page()
        try:
            page.goto(ZEUS_ARGUS_URL, wait_until="domcontentloaded", timeout=45_000)
            time.sleep(2)
            return parse_zeus_arrets(page.content())
        finally:
            page.close()


def check_arrets(store, zeus: dict[str, bool] | None = None) -> list[str]:
    """Signale les écarts entre la liste MODELS et la réalité du marché neuf.

    Deux directions : un modèle suivi marqué arrêté par Zeus (ou sans neuf
    idealo sur 2 passes) mais absent de MODELS → candidat occasion ; un
    modèle de MODELS que Zeus dit encore vendu neuf → candidat retrait.
    """
    from leboncoin_alerte.retail import MODELS as RETAIL

    lignes: list[str] = []
    # Corroboration idealo : clés vues sur les 2 dernières passes hebdo
    passes = [r[0] for r in store.conn.execute(
        "SELECT DISTINCT pass_at FROM retail_prices ORDER BY pass_at DESC LIMIT 2")]
    sans_neuf: set[str] = set()
    if len(passes) == 2:
        vus = {r[0] for r in store.conn.execute(
            "SELECT key FROM retail_prices WHERE pass_at IN (?, ?)", passes)}
        sans_neuf = {k for k in RETAIL if k.startswith("rtx") and k not in vus}

    for key in sorted(RETAIL):
        if not key.startswith("rtx"):
            continue
        if key in MODELS:
            if zeus is not None and zeus.get(key) is False:
                lignes.append(
                    f"ℹ {key} : dans la liste occasion mais Zeus la dit ENCORE vendue "
                    f"neuve — candidat retrait ? (décision mainteneur)")
        else:
            arret_zeus = zeus.get(key) if zeus else None
            if arret_zeus or key in sans_neuf:
                raisons = []
                if arret_zeus:
                    raisons.append("Zeus : plus vendue neuve")
                if key in sans_neuf:
                    raisons.append("aucun prix neuf idealo sur les 2 dernières passes")
                lignes.append(
                    f"⚠ {key} : {' + '.join(raisons)} — candidate à la liste "
                    f"occasion (décision mainteneur)")
            elif zeus is not None and key not in zeus:
                lignes.append(f"? {key} : absente de la table Zeus — à vérifier")
    return lignes
