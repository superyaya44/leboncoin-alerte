"""Décompte complet : toutes les annonces de la recherche, filtres actuels, SANS envoyer.

Usage : uv run python scripts/count_matches.py [-c config.toml]
Lit toutes les pages de résultats, charge le détail de chaque annonce, applique
matches() en mémoire et imprime la liste des qualifiées. Aucune écriture en base,
aucune notification.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from leboncoin_alerte.browser import BrowserSession, human_pause, is_blocked
from leboncoin_alerte.config import load_config
from leboncoin_alerte.filters import matches
from leboncoin_alerte.search import enrich_with_details, extract_listings


def main() -> int:
    config_path = Path(
        sys.argv[sys.argv.index("-c") + 1] if "-c" in sys.argv else "config.toml"
    )
    config = load_config(config_path)
    search = config.searches[0]
    session = BrowserSession(config.data_dir / "profile", headless=True, cdp_url=config.cdp_url)

    with session.page() as page:
        return _run(page, config, search)


def _run(page, config, search) -> int:

    print(f"[{search.name}] extraction des résultats…", flush=True)
    listings = extract_listings(page, search.url)
    print(f"[{search.name}] {len(listings)} annonces — lecture des détails…", flush=True)

    kept = []
    for i, lst in enumerate(listings, 1):
        try:
            full = enrich_with_details(page, lst)
        except Exception as exc:  # noqa: BLE001 — on compte, on ne meurt pas
            print(f"  {i}/{len(listings)} ERREUR {lst.listing_id}: {exc}", flush=True)
            continue
        ok = matches(full, search)
        if ok:
            kept.append(full)
            print(f"  {i}/{len(listings)} MATCH {full.price}€ {full.title[:70]}", flush=True)
        elif i % 25 == 0:
            print(f"  … {i}/{len(listings)} traitées, {len(kept)} matchs", flush=True)
        if is_blocked(page):
            print("  BLOQUÉ (DataDome) — arrêt du décompte", flush=True)
            break
        human_pause(0.4, 1.0)

    print(f"\n=== RÉSULTAT : {len(kept)}/{len(listings)} annonces qualifiées ===", flush=True)
    for k in kept:
        print(f"  {k.price or '?':>5} €  {k.title[:80]}")
        print(f"         {k.url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
