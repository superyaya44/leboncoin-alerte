"""Lectures pour l'interface : tester une alerte, statistiques de prix.

Lecture seule sur listings.db (ouverte en mode ro) : rien n'est écrit, aucune
requête vers Leboncoin. On rejoue les filtres sur les annonces déjà vues.
"""

from __future__ import annotations

import re
import sqlite3
import statistics
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from .config import Search
from .filters import first_reject
from .model import Listing

REASONS = {
    "max_price": "prix trop élevé",
    "min_price": "prix trop bas",
    "wanted_ad": "annonce de recherche, pas de vente",
    "keywords_include": "titre sans les mots voulus",
    "keywords_exclude": "mot exclu dans le titre",
    "text_keywords_include": "description sans les mots voulus",
    "text_keywords_exclude": "mot exclu dans la description",
    "min_ram_gb": "pas assez de RAM",
    "min_stick_gb": "barrettes trop petites",
    "min_vram_gb": "pas assez de mémoire graphique",
    "min_disk_gb": "pas assez de stockage",
    "regex_all": "expression régulière (toutes)",
    "regex_any": "expression régulière (au moins une)",
    "cities_include": "ville hors liste",
    "cities_exclude": "ville exclue",
    "max_age_hours": "trop ancienne",
    "published_after": "publiée avant la date",
    "computer_type": "type d'ordinateur",
    "seller_type": "type de vendeur",
    "condition_include": "état",
    "min_year": "année trop ancienne",
    "shippable_only": "pas de livraison",
}
# Critères qui dépendent d'infos non conservées en base : non simulés.
NOT_SIMULATED = ["villes", "vendeur", "état", "livraison", "ancienneté", "année", "type d'ordinateur"]

_GPU = re.compile(r"\b(rtx|gtx)[ -]?(\d{4})(?:[ -]?(ti))?(?:[ -]?(super))?\b", re.IGNORECASE)


def _connect(db: Path) -> sqlite3.Connection | None:
    if not db.exists():
        return None
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def test_search(db: Path, search: Search, days: int = 14) -> dict:
    """Rejoue les filtres de `search` sur les annonces vues ces `days` derniers jours."""
    out: dict = {"days": days, "source": "own", "total": 0, "matched": 0,
                 "examples": [], "reasons": [], "not_simulated": NOT_SIMULATED}
    conn = _connect(db)
    if conn is None:
        out["source"] = "none"
        return out
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    cols = "listing_id, title, price, url, search_text, first_seen"
    with conn:
        rows = conn.execute(
            f"SELECT {cols} FROM listings WHERE search = ? AND first_seen >= ?",
            (search.name, cutoff)).fetchall()
        if len(rows) < 20:
            # Nouvelle alerte : on s'appuie sur tout ce que les autres alertes ont vu.
            out["source"] = "all"
            rows = conn.execute(
                f"SELECT {cols} FROM listings WHERE first_seen >= ? "
                "GROUP BY listing_id ORDER BY first_seen DESC", (cutoff,)).fetchall()
    conn.close()
    reasons: dict[str, int] = {}
    matched: list[tuple] = []
    for lid, title, price, url, text, seen in rows:
        title = title or ""
        body = text if text and text != title else ""
        reason = first_reject(Listing(lid, title, price, url or "", body=body), search)
        if reason is None:
            matched.append((seen, title, price, url))
        else:
            reasons[reason] = reasons.get(reason, 0) + 1
    matched.sort(reverse=True)
    out.update(
        total=len(rows),
        matched=len(matched),
        examples=[{"title": t, "price": p, "url": u} for _, t, p, u in matched[:6]],
        reasons=[{"label": REASONS.get(k, k), "count": n}
                 for k, n in sorted(reasons.items(), key=lambda kv: -kv[1])[:5]],
    )
    return out


def gpu_label(title: str) -> str | None:
    m = _GPU.search(title or "")
    if not m:
        return None
    parts = [m.group(1).upper(), m.group(2)]
    if m.group(3):
        parts.append("Ti")
    if m.group(4):
        parts.append("Super")
    return " ".join(parts)


def _monday(iso: str) -> date:
    d = datetime.fromisoformat(iso).date()
    return d - timedelta(days=d.weekday())


def _weekly(points: list[tuple[str, int]], with_band: bool) -> list[dict]:
    by_week: dict[date, list[int]] = {}
    for seen, price in points:
        by_week.setdefault(_monday(seen), []).append(price)
    out = []
    for week in sorted(by_week):
        prices = sorted(by_week[week])
        row = {"week": week.isoformat(), "median": round(statistics.median(prices)),
               "n": len(prices)}
        if with_band:
            if len(prices) >= 4:
                q = statistics.quantiles(prices, n=4)
                row["p25"], row["p75"] = round(q[0]), round(q[2])
            else:
                row["p25"], row["p75"] = prices[0], prices[-1]
        out.append(row)
    return out


def price_stats(db: Path, search_name: str, market_kind: str | None = None,
                days: int = 120) -> dict:
    """Prix par semaine (médiane + 25e/75e centiles) et, pour les GPU, par modèle
    avec la référence neuf idealo quand elle existe."""
    out: dict = {"search": search_name, "weeks": [], "models": [], "n": 0, "median": None}
    conn = _connect(db)
    if conn is None:
        return out
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    with conn:
        rows = conn.execute(
            "SELECT price, first_seen, title FROM listings "
            "WHERE search = ? AND price >= 5 AND first_seen >= ? "
            "AND COALESCE(reject_reason, '') != 'wanted_ad'",
            (search_name, cutoff)).fetchall()
        try:
            retail = conn.execute("SELECT key, price, pass_at FROM retail_prices "
                                  "ORDER BY pass_at").fetchall()
        except sqlite3.Error:
            retail = []
    conn.close()
    if not rows:
        return out
    out["n"] = len(rows)
    out["median"] = round(statistics.median(p for p, _, _ in rows))
    out["weeks"] = _weekly([(s, p) for p, s, _ in rows], with_band=True)
    if market_kind != "ram":
        groups: dict[str, list[tuple[str, int]]] = {}
        for price, seen, title in rows:
            label = gpu_label(title)
            if label:
                groups.setdefault(label, []).append((seen, price))
        top = sorted(groups.items(), key=lambda kv: -len(kv[1]))[:8]
        for label, pts in top:
            if len(pts) < 3:
                continue
            key = label.lower().replace(" ", "-")
            out["models"].append({
                "name": label,
                "n": len(pts),
                "median": round(statistics.median(p for _, p in pts)),
                "weeks": _weekly(pts, with_band=True),
                "retail": [{"date": d[:10], "price": p} for k, p, d in retail if k == key],
            })
    return out
