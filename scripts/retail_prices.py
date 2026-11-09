#!/usr/bin/env python3
"""Passe prix NEUF idealo.fr — cron hebdo (samedi → dimanche).

Scrape les modèles suivis (recherche → fiches → « à partir de »), enregistre
les médianes dans la table retail_prices et imprime une synthèse markdown.
Le bot utilise ensuite ces réfs NEUF comme référence marché.

Usage : uv run python scripts/retail_prices.py [clef ...] [--db data/listings.db]
Exemple : uv run python scripts/retail_prices.py rtx-5060-ti rtx-5090
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from leboncoin_alerte.config import load_config
from leboncoin_alerte.retail import MODELS, run_pass
from leboncoin_alerte.store import Store


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("keys", nargs="*", help="clés modèles (défaut : toutes)")
    ap.add_argument("--config", default="config.toml")
    args = ap.parse_args()

    unknown = [k for k in args.keys if k not in MODELS]
    if unknown:
        print(f"clés inconnues {unknown} — valides : {sorted(MODELS)}")
        return 2

    config = load_config(Path(args.config))
    store = Store(config.data_dir / "listings.db")

    results = run_pass(args.keys or None)
    for r in results:
        print(
            json.dumps(
                {k: r.get(k) for k in ("clef", "n_prix", "min", "mediane", "max", "erreur")},
                ensure_ascii=False,
            ),
            flush=True,
        )

    n = store.record_retail_pass(results)
    print(f"\n**Passe neuf idealo — {n} référence(s) enregistrée(s)**\n")
    print("| clef | n | min | médiane | max |")
    print("|---|---|---|---|---|")
    for r in results:
        print(
            f"| {r.get('clef')} | {r.get('n_prix', 0)} | "
            f"{r.get('min') or '—'} | {r.get('mediane') or '—'} | {r.get('max') or '—'} |"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
