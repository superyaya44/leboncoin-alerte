#!/usr/bin/env python3
"""Rapport budget nocturne : analyse RAM + verdict des annonces PC récentes.

Exécuté chaque nuit par le cron Hermes (après le cycle du daemon). Scanne
les annonces non rejetées (statuts sent/question/seen sans reject) des
derniers --days jours, calcule le prix ajusté RAM (prorata kit neuf,
barrettes ≤ 8 Go = 0) et le verdict budget DDR4 < 2000 € / DDR5 < 2500 €.

Usage : uv run python scripts/budget_review.py [--days 2] [--db data/listings.db]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from leboncoin_alerte.budget import BUDGETS, analyze

VERDICT_LABEL = {
    "good_deal": "BONNE AFFAIRE",
    "over_budget": "hors budget",
    "unknown": "à vérifier",
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=2)
    ap.add_argument("--db", default="data/listings.db")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"aucune base : {db}")
        return 1
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    cutoff = (datetime.now(UTC) - timedelta(days=args.days)).isoformat()
    cols = {row[1] for row in con.execute("PRAGMA table_info(listings)")}
    # Pendant la migration (ancienne DB sans les colonnes question), on dégrade
    # proprement : pas de question vendeur dans le rapport.
    question_col = "seller_question" if "seller_question" in cols else "NULL"
    rows = con.execute(
        "SELECT search, title, price, url, status, COALESCE(search_text, title), "
        f"{question_col} FROM listings "
        "WHERE first_seen > ? AND (reject_reason IS NULL OR status = 'question') "
        "ORDER BY price",
        (cutoff,),
    ).fetchall()

    print(f"**Revue budget — {len(rows)} annonce(s) non rejetée(s) sur {args.days} j**\n")
    for search, title, price, url, status, text, question in rows:
        r = analyze(text, price)
        budget = BUDGETS.get(r["ram_type"] or "", "?")
        print(
            f"- [{VERDICT_LABEL[r['verdict']]}] {title[:70]} — {price} € "
            f"({search}, {status})\n"
            f"  RAM {r['ram_type'] or '?'} {r['ram_gb'] if r['ram_gb'] is not None else '?'} Go"
            f" → +{r['ram_cost'] if r['ram_cost'] is not None else '?'} €"
            f" = {r['adjusted_price'] if r['adjusted_price'] is not None else '?'} €"
            f" (budget {budget} €)\n  {url}"
        )
        if question:
            print(f"  Question vendeur en attente : « {question} »")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
