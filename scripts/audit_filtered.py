#!/usr/bin/env python3
"""Audit nocturne : annonces écartées qui auraient peut-être mérité une alerte.

Lit la base du bot (lecture seule) et classe les annonces rejetées des
dernières 24 h (paramétrable --days) en deux familles :

- CERTAINES : le rejet est sûr (prix hors borne, demande d'achat, matériel
  HS, mot-clé visé absent). Comptées par raison, non listées.
- À VÉRIFIER : le rejet repose sur une DÉTECTION (VRAM/RAM/barrette/disque,
  regex, type de PC, ville, fraîcheur…). Un raté du parseur est possible —
  on les liste avec un extrait de la description pour décision humaine.

Sortie texte bornée, destinée au contexte du cron « lbc-alerte — revue
nocturne ». Aucune dépendance hors stdlib : tourne avec n'importe quel
Python ≥ 3.12, y compris le venv Hermes (règle WAL : jamais le sqlite3 CLI).

Usage :
    python scripts/audit_filtered.py [--db data/listings.db] [--days 1] [--max 15]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from pathlib import Path

# Détection VRAM du package (mêmes regex que le daemon — pas de copie qui
# dérive). Le script tourne depuis la racine du repo (lbc_review.sh y cd).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
try:
    from leboncoin_alerte.filters import _vram_model_default, _vram_value
except ImportError:  # pragma: no cover - chemin cassé = audit dégradé, pas fatal
    _vram_value = None  # type: ignore[assignment]
    _vram_model_default = None  # type: ignore[assignment]

# Scope GPU acté avec mainteneur (config : min_vram_gb = 16 sur PC fixes et GPU).
# Un rejet min_vram_gb avec VRAM (ou modèle) ÉTABLIE sous ce seuil est un
# rejet VOLONTAIRE, pas un raté de parseur — il ne doit jamais remonter dans
# la section « à vérifier » du rapport du matin (bug 31/10 : le cron a proposé
# une 3080 Ti 12 Go comme affaire manquée alors qu'elle est hors scope).
MIN_VRAM_SCOPE = 16

# Rejets SÛRS : le critère porte sur une information fiable (prix annoncé,
# titre/texte contenant mot pour mot la formulation exclue). Rien à vérifier.
CERTAIN = {
    "min_price": "prix sous le minimum",
    "max_price": "prix au-dessus du maximum",
    "keywords_include": "titre sans le mot-clé visé",
    "keywords_exclude": "demande d'achat (titre)",
    "text_keywords_include": "texte sans le mot-clé visé",
    "text_keywords_exclude": "matériel HS / formulation exclue (texte)",
}

# Rejets à DÉTECTION : le critère dépend d'un parseur (Go/GB, modèles GPU,
# regex) ou d'un attribut structuré parfois absent/erroné → faux rejet possible.
LABELS = {
    **CERTAIN,
    "min_ram_gb": "RAM non détectée ou sous le seuil",
    "min_stick_gb": "barrette sous le seuil (ou kit mal parsé)",
    "min_vram_gb": "VRAM non détectée ou sous le seuil",
    "min_disk_gb": "stockage non détecté ou sous le seuil",
    "regex_all": "regex obligatoire non trouvée",
    "regex_any": "aucune regex ciblée trouvée",
    "cities_include": "ville non détectée/hors zone",
    "cities_exclude": "ville exclue",
    "max_age_hours": "annonce trop ancienne",
    "computer_type": "pas un PC fixe (attribut)",
    "seller_type": "type de vendeur",
    "condition_include": "état non retenu",
    "min_year": "année trop ancienne",
    "shippable_only": "non expédiable",
}

EXCERPT = 220  # caractères de description affichés par annonce


def _vram_reject_sure(text: str) -> str | None:
    """Label du rejet min_vram_gb s'il est SÛR (hors scope), sinon None.

    Sûr = la VRAM explicite détectée est < seuil, ou le modèle reconnu est
    toujours < seuil (ex. 3080 Ti = 12 Go). None = rien d'établi → le rejet
    repose sur l'absence de détection → un raté de parseur reste possible.
    """
    if _vram_value is None:
        return None
    value = _vram_value(text)
    if value is not None and value < MIN_VRAM_SCOPE:
        return f"hors scope : VRAM {value} Go < {MIN_VRAM_SCOPE} Go"
    default = _vram_model_default(text) if _vram_model_default else None
    if default is not None and default < MIN_VRAM_SCOPE:
        return f"hors scope : modèle toujours < {MIN_VRAM_SCOPE} Go"
    return None


def audit(db_path: str, days: int, max_rows: int) -> str:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT search, title, price, url, reject_reason, COALESCE(search_text, title) "
        "FROM listings "
        "WHERE reject_reason IS NOT NULL AND status = 'seen' "
        "AND first_seen >= datetime('now', ?) "
        "ORDER BY first_seen DESC",
        (f"-{days} days",),
    ).fetchall()
    con.close()

    certain = Counter(r[4] for r in rows if r[4] in CERTAIN)
    to_check = []
    for r in rows:
        if r[4] in CERTAIN:
            continue
        if r[4] == "min_vram_gb":
            sure = _vram_reject_sure(r[5])
            if sure is not None:
                certain[sure] += 1
                continue
        to_check.append(r)

    lines = [(
        f"écartées sur {days} j : {len(rows)} "
        f"(certaines : {sum(certain.values())}, à vérifier : {len(to_check)})"
    )]
    if certain:
        detail = ", ".join(f"{LABELS.get(k, k)} × {n}" for k, n in certain.most_common())
        lines.append(f"  rejets sûrs : {detail}")
    if not to_check:
        lines.append("  à vérifier : aucune — rien à signaler.")
        return "\n".join(lines)

    lines.append("  À VÉRIFIER (rejet sur critère de détection — lire la description) :")
    for search, title, price, url, reason, text in to_check[:max_rows]:
        excerpt = " ".join(text.split())[:EXCERPT]
        lines.append(
            f"  - [{search}] {title} — {price if price is not None else 'prix ?'} € "
            f"— motif : {LABELS.get(reason, reason)}\n"
            f"    « {excerpt} »\n"
            f"    {url}"
        )
    if len(to_check) > max_rows:
        lines.append(f"  … et {len(to_check) - max_rows} autres (limite d'affichage).")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit des annonces écartées par les filtres.")
    parser.add_argument("--db", default="data/listings.db", help="chemin de listings.db")
    parser.add_argument("--days", type=int, default=1, help="fenêtre d'audit en jours")
    parser.add_argument("--max", type=int, default=15, help="max annonces listées")
    args = parser.parse_args()
    print(audit(args.db, args.days, args.max))
    return 0


if __name__ == "__main__":
    sys.exit(main())
