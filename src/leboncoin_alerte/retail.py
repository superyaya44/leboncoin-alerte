"""Prix NEUF hebdo via idealo.fr (références marché, refonte 29/10/2026).

Chaque samedi→dimanche, un cron parcourt les modèles suivis sur idealo.fr
(recherche → fiches produit → prix « à partir de ») et enregistre la
médiane de chaque modèle dans ``retail_prices``. Les comparaisons de prix
du bot utilisent ensuite le NEUF comme référence (décision mainteneur :
« on peut passer à 100 % neuf »).

Le scraping passe par le Chromium persistant en CDP (même session que le
daemon leboncoin — anti-détection mutualisée).
"""

from __future__ import annotations

import re
import statistics
import time

from playwright.sync_api import sync_playwright

# (clef modèle, requête idealo, regex de filtrage des fiches).
MODELS: dict[str, tuple[str, str]] = {
    "rtx-3090": ("rtx 3090", r"\b3090\b"),
    "rtx-4080-super": ("rtx 4080 super", r"4080\s*super"),
    "rtx-4080": ("rtx 4080", r"4080(?!\s*super)"),
    "rtx-4090": ("rtx 4090", r"\b4090\b"),
    "rtx-5060-ti": ("rtx 5060 ti", r"5060\s*ti"),
    "rtx-5070-ti": ("rtx 5070 ti", r"5070\s*ti"),
    "rtx-5080": ("rtx 5080", r"\b5080\b"),
    "rtx-5090": ("rtx 5090", r"\b5090\b"),
    "ddr4-32": ("barrette ddr4 32go", r"ddr4"),
    "ddr4-64": ("kit ddr4 64go", r"ddr4"),
    "ddr5-32": ("barrette ddr5 32go", r"ddr5"),
    "ddr5-64": ("kit ddr5 64go", r"ddr5"),
}

MAX_FICHES = 6  # fiches produit visitées par modèle (anti-ban)
PAUSE_FICHE = 3.0  # s entre fiches
PAUSE_MODELE = 6.0  # s entre modèles
CDP_ENDPOINT = "http://127.0.0.1:9222"

# Éditions premium (watercooling/ultra-OC) qui tirent la médiane vers le haut :
# une Noctua/AORUS 4080S à 5 000 € ne représente pas le prix neuf de référence.
# On les écarte SEULEMENT s'il reste assez de fiches standard (bug 02/11 :
# médiane 4080S à 3 320 € vs 1 858 € pour la 4080 simple).
_PREMIUM = re.compile(
    r"noctua|aorus|suprim|strix|water|liquid|vulcan|amp\s?extreme"
    r"|hall\s?of\s?fame|\bhof\b|\bgamerock\b|expedition|signature|limited",
    re.IGNORECASE,
)


def filter_premium(links: list[tuple[str, str]], cap: int) -> list[tuple[str, str]]:
    """Écarte les éditions premium si assez de fiches standard restent."""
    plain = [(h, n) for h, n in links if not _PREMIUM.search(n)]
    return plain if len(plain) >= cap else links

_PRICE_RE = re.compile(r"(\d{1,3}(?:[\s .,]\d{3})*(?:[.,]\d{2})?)\s*€")


def _parse_price(txt: str) -> float | None:
    m = _PRICE_RE.search(txt)
    if not m:
        return None
    raw = m.group(1).replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def product_links(page, query: str, pattern: str) -> list[tuple[str, str]]:
    """Recherche idealo → liens de fiches produit (sans prix, filtrées)."""
    url = f"https://www.idealo.fr/prechcat.html?q={query.replace(' ', '+')}"
    page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    page.wait_for_timeout(2500)
    # la liste charge au scroll
    page.evaluate(
        """async () => {
        let last = -1, stable = 0;
        for (let i = 0; i < 20 && stable < 3; i++) {
            window.scrollBy(0, 1200);
            await new Promise(r => setTimeout(r, 600));
            const n = document.querySelectorAll('a[href*="/prix/"]').length;
            if (n === last) stable++; else { stable = 0; last = n; }
        }
    }"""
    )
    rx = re.compile(pattern, re.IGNORECASE)
    seen: set[str] = set()
    links: list[tuple[str, str]] = []
    for href, name in page.evaluate(
        """() => [...document.querySelectorAll('a[href*="/prix/"]')]
             .map(a => [a.href.split('?')[0], (a.innerText || '').trim()])"""
    ):
        if href in seen or not name or len(name) > 200:
            continue
        seen.add(href)
        if rx.search(name):
            links.append((href, name))
    return filter_premium(links, MAX_FICHES)[:MAX_FICHES]


def fiche_price(page, href: str) -> tuple[float | None, str | None]:
    """Fiche produit idealo → prix « à partir de X € »."""
    page.goto(href, wait_until="domcontentloaded", timeout=45_000)
    # le bloc prix arrive côté client : attendre qu'il apparaisse (bug 02/11 :
    # 1800 ms fixes trop courts au rechargement, toutes les fiches à 0 prix)
    try:
        page.wait_for_function(
            "() => /à partir\\s+de\\s*[\\d\\u202f\\u00a0\\s,.]+\\s*€/i"
            ".test(document.body ? document.body.innerText : '')",
            timeout=10_000,
        )
    except Exception:  # noqa: BLE001, S110 — pas de prix à l'écran : extraction quand même
        pass
    page.wait_for_timeout(500)
    hits = page.evaluate(
        """() => {
        const out = [];
        for (const el of document.querySelectorAll('*')) {
            if (el.children.length) continue;
            const t = (el.textContent || '').trim();
            if (/^\\u00e0 partir de$/i.test(t)) {
                const pool = [];
                let p = el.parentElement;
                for (let i = 0; i < 3 && p; i++) { pool.push(p.textContent || ''); p = p.parentElement; }
                out.push(pool.join(' | '));
            }
        }
        return out.slice(0, 3);
    }"""
    )
    for ctx in hits:
        price = _parse_price(ctx)
        if price:
            return price, ctx[:100]
    body = page.evaluate("() => document.body.innerText.slice(0, 6000)")
    m = re.search(r"à partir de\s*([\d\s ,.]+)\s*€", body)
    if m:
        raw = re.sub(r"[\s\u202f\u00a0]", "", m.group(1)).replace(",", ".")
        try:
            return float(raw), "fallback body"
        except ValueError:
            pass
    return None, None


def scrape_model(page, key: str) -> dict:
    """Un modèle : recherche, fiches, agrégats (min/médiane/max)."""
    query, pattern = MODELS[key]
    links = product_links(page, query, pattern)
    fiches = []
    for href, name in links:
        try:
            price, ctx = fiche_price(page, href)
            fiches.append({"produit": name[:100], "href": href, "prix": price, "ctx": ctx})
        except Exception as e:  # noqa: BLE001 — une fiche HS ne tue pas la passe
            fiches.append({"produit": name[:100], "href": href, "erreur": repr(e)[:120]})
        time.sleep(PAUSE_FICHE)
    ok = sorted(f["prix"] for f in fiches if f.get("prix"))
    # Médiane de la MOITIÉ LA MOINS CHERE des fiches (bug 02/11 : des fiches
    # de base affichent des « à partir de » gonflés par des offers marketplace
    # rares — 4080S de base à 4 979/6 198 € — et tirent la médiane vers le
    # haut). Le prix d'entrée neuf est aussi ce qu'un acheteur compare à une
    # occasion, donc le low-median est la bonne sémantique pour le verdict.
    low = ok[: len(ok) // 2] if len(ok) >= 4 else ok
    return {
        "clef": key,
        "n_fiches": len(fiches),
        "n_prix": len(ok),
        "min": min(ok) if ok else None,
        "max": max(ok) if ok else None,
        "mediane": round(statistics.median(low), 2) if ok else None,
        "fiches": fiches,
    }


def run_pass(keys: list[str] | None = None, cdp: str = CDP_ENDPOINT) -> list[dict]:
    """Passe complète (ou partielle) : scrape tous les modèles demandés."""
    todo = keys or list(MODELS)
    results: list[dict] = []
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(cdp)
        page = browser.contexts[0].new_page()
        try:
            for key in todo:
                try:
                    results.append(scrape_model(page, key))
                except Exception as e:  # noqa: BLE001 — on journalise et on continue
                    results.append({"clef": key, "erreur": repr(e)[:200]})
                time.sleep(PAUSE_MODELE)
        finally:
            page.close()
    return results
