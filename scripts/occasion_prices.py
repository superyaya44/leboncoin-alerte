#!/usr/bin/env python3
"""Passe prix OCCASION multi-sources — cron 2×/semaine (mercredi + dimanche).

Scrape les agrégateurs (Flipdeal, Racingtech, PCPrice/eBay en plafond),
applique les garde-fous (règle J-7, n ≥ 20, écart inter-sources ≤ 25 %),
enregistre le consensus dans occasion_prices et imprime une synthèse.

Usage : uv run python scripts/occasion_prices.py [clef ...]
Exemple : uv run python scripts/occasion_prices.py rtx-4090
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from leboncoin_alerte.config import load_config
from leboncoin_alerte.occas import MODELS, check_arrets, fetch_zeus_arrets, run_pass
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
        detail = {k: r.get(k) for k in ("clef", "status", "mediane", "detail")}
        detail["sources"] = [
            {k2: s.get(k2) for k2 in ("source", "mediane", "n", "date", "exclu")}
            for s in r.get("sources", []) + r.get("exclus", [])
        ]
        print(json.dumps(detail, ensure_ascii=False, default=str), flush=True)

    # Tenue de la liste des modèles arrêtés : source Zeus (marquage par
    # modèle) + corroboration idealo — signalement, jamais d'ajout auto
    try:
        zeus = fetch_zeus_arrets()
    except Exception as e:  # noqa: BLE001 — le check ne doit pas casser la passe
        print(f"check arrêts : Zeus injoignable ({e!r:.80})")
        zeus = None
    for ligne in check_arrets(store, zeus):
        print(ligne)

    n = store.record_occasion_pass(results)
    print(f"\n**Passe occasion multi-sources — {n} clé(s) enregistrée(s)**\n")
    print("| clef | statut | consensus | plafond eBay | sources |")
    print("|---|---|---|---|---|")
    for r in results:
        srcs = ", ".join(
            f"{s['source']} {s['mediane']}€ (n={s.get('n')}, {s.get('date') or '?'})"
            for s in r.get("sources", [])) or "—"
        plafond = r.get("plafond") or {}
        plaf = f"{plafond.get('mediane')}€" if plafond.get("mediane") else "—"
        print(f"| {r.get('clef')} | {r.get('status')} | {r.get('mediane') or '—'} | {plaf} | {srcs} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
