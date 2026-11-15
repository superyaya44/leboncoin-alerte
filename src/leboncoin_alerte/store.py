"""Dédupliqution SQLite : cycle de vie des annonces par recherche.

États d'une annonce (colonne ``status``) :
- (absente)  = ``new``  : présente sur Leboncoin, pas encore analysée
- ``seen``   : analysée, n'a pas passé les filtres (pas notifiée)
- ``sent``   : qualifiée ET notifiée avec succès (jamais renvoyée)
- ``old``    : disparue des résultats de recherche — dépubliée ou vendue

La dédupliqution est par (listing_id, search) : une annonce déjà envoyée ne sera
jamais renvoyée, les jours suivants seules les NOUVELLES annonces sortent.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .model import Listing

_SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    listing_id TEXT NOT NULL,
    search     TEXT NOT NULL,
    title      TEXT,
    price      INTEGER,
    url        TEXT,
    first_seen TEXT NOT NULL,
    notified   INTEGER NOT NULL DEFAULT 0,
    status     TEXT NOT NULL DEFAULT 'seen',
    sent_at    TEXT,
    search_text TEXT,      -- titre + description + attributs au moment de l'analyse
    reject_reason TEXT,    -- premier critère échoué (filters.first_reject), NULL si notifiée
    PRIMARY KEY (listing_id, search)
);
CREATE TABLE IF NOT EXISTS retail_prices (
    key       TEXT NOT NULL,   -- clefs modèle ('rtx-5090')
    price     INTEGER NOT NULL, -- médiane NEUF de la passe (€)
    n_offers  INTEGER,          -- fiches avec prix
    min_price INTEGER,
    max_price INTEGER,
    pass_at   TEXT NOT NULL,   -- horodatage ISO de la passe hebdo idealo
    PRIMARY KEY (key, pass_at)
);
CREATE TABLE IF NOT EXISTS occasion_prices (
    key        TEXT NOT NULL,   -- clefs modèle ARRÊTÉ ('rtx-4090')
    median     INTEGER,         -- consensus occasion (€) — NULL si divergence/aucune source
    plafond    INTEGER,         -- médiane eBay vendues (€), marché séparé, jamais consensus
    n_total    INTEGER,         -- effectif cumulé des sources valides
    status     TEXT NOT NULL,   -- ok | source-unique | divergence | aucune-source
    sources    TEXT,            -- JSON du détail (sources valides + exclus + raisons)
    fetched_at TEXT NOT NULL,   -- horodatage ISO de la passe (règle J-7 : ≤ 7 j)
    PRIMARY KEY (key, fetched_at)
);
CREATE TABLE IF NOT EXISTS pending_offers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id TEXT NOT NULL,
    search     TEXT NOT NULL,
    title      TEXT,
    price      INTEGER,
    url        TEXT,
    -- Contexte de décision au moment de la mise en attente (verdict budget,
    -- référence marché, etc.) : le rapport de validation humaine en a besoin.
    context    TEXT,
    created_at TEXT NOT NULL,
    sent_at    TEXT,              -- NULL = en attente de validation humaine
    UNIQUE (listing_id, search)
);
CREATE TABLE IF NOT EXISTS listing_prices (
    listing_id TEXT NOT NULL,
    search     TEXT NOT NULL,
    price      INTEGER NOT NULL,
    seen_at    TEXT NOT NULL      -- ISO UTC : changement de prix observé
);
CREATE INDEX IF NOT EXISTS idx_listing_prices ON listing_prices (search, listing_id);
CREATE TABLE IF NOT EXISTS search_runs (
    search      TEXT PRIMARY KEY,
    last_run_at TEXT NOT NULL      -- ISO UTC, dernier passage complet (planification)
);
"""


class Store:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.executescript(_SCHEMA)
        # Bases créées avant l'ajout de status/sent_at : migration légère.
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(listings)")}
        if "status" not in cols:
            self.conn.execute("ALTER TABLE listings ADD COLUMN status TEXT NOT NULL DEFAULT 'seen'")
        if "sent_at" not in cols:
            self.conn.execute("ALTER TABLE listings ADD COLUMN sent_at TEXT")
        if "search_text" not in cols:
            self.conn.execute("ALTER TABLE listings ADD COLUMN search_text TEXT")
        if "reject_reason" not in cols:
            self.conn.execute("ALTER TABLE listings ADD COLUMN reject_reason TEXT")
        if "seller_question" not in cols:
            # Question de prise d'info vendeur (modèle GPU ambigu 8/16 Go) :
            # message prêt à envoyer, NULL si pas nécessaire.
            self.conn.execute("ALTER TABLE listings ADD COLUMN seller_question TEXT")
        if "question_status" not in cols:
            # 'draft' | 'sent' | 'answered' — suivi du cycle de vie de la question.
            self.conn.execute("ALTER TABLE listings ADD COLUMN question_status TEXT")
        self.conn.commit()

    # --- cycle de vie ------------------------------------------------------

    def known_ids(self, search_name: str) -> set[str]:
        """IDs de toutes les annonces déjà enregistrées pour cette recherche.

        Alimente le stop de pagination adaptative : une page entièrement
        connue signifie qu'on a rejoint le point d'arrêt du cycle précédent.
        """
        return {
            row[0]
            for row in self.conn.execute(
                "SELECT listing_id FROM listings WHERE search = ?", (search_name,)
            )
        }

    def unseen_listings(self, search_name: str, listings: list[Listing]) -> list[Listing]:
        """Annonces jamais vues (new), SANS les enregistrer.

        Permet de n'enregistrer que celles réellement analysées dans le cycle,
        sous le plafond anti-ban ; les autres le seront aux cycles suivants.
        """
        fresh: list[Listing] = []
        for lst in listings:
            cur = self.conn.execute(
                "SELECT 1 FROM listings WHERE listing_id = ? AND search = ?",
                (lst.listing_id, search_name),
            )
            if cur.fetchone() is None:
                fresh.append(lst)
        return fresh

    def mark_question(self, search_name: str, listing: Listing, question: str) -> None:
        """Annonce au modèle GPU ambigu : statut 'question', pas notifiée.

        La question au vendeur est stockée prête à envoyer (envoi MANUEL ou
        sur validation explicite — jamais automatique, geste à risque pour
        le compte). L'annonce n'est pas re-analysée aux cycles suivants.
        """
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT OR IGNORE INTO listings "
            "(listing_id, search, title, price, url, first_seen, status, search_text, "
            " seller_question, question_status) "
            "VALUES (?, ?, ?, ?, ?, ?, 'question', ?, ?, 'draft')",
            (
                listing.listing_id,
                search_name,
                listing.title,
                listing.price,
                listing.url,
                now,
                listing.search_text(),
                question,
            ),
        )
        self.conn.commit()

    # --- références marché NEUF (idealo, passe hebdo) ----------------------

    def record_retail_pass(self, results: list[dict], pass_at: str | None = None) -> int:
        """Enregistre une passe idealo : une ligne par modèle avec prix.

        Seules les clefs ayant donné au moins un prix sont écrites — une
        clé sans donnée ne doit pas écraser la référence de la semaine
        précédente.
        """
        pass_at = pass_at or datetime.now(UTC).isoformat()
        n = 0
        for r in results:
            if r.get("mediane") is None:
                continue
            self.conn.execute(
                "INSERT OR REPLACE INTO retail_prices "
                "(key, price, n_offers, min_price, max_price, pass_at) VALUES (?,?,?,?,?,?)",
                (
                    r["clef"],
                    round(r["mediane"]),
                    r.get("n_prix"),
                    round(r["min"]) if r.get("min") is not None else None,
                    round(r["max"]) if r.get("max") is not None else None,
                    pass_at,
                ),
            )
            n += 1
        self.conn.commit()
        return n

    def record_occasion_pass(self, results: list[dict], fetched_at: str | None = None) -> int:
        """Enregistre une passe occasion : une ligne par clé, statut compris.

        Une clé sans consensus (divergence / aucune source) est écrite avec
        median=NULL : le passé reste lisible et occasion_refs saura qu'il n'y
        a PAS de valeur fraîche à utiliser (au lieu de deviner).
        """
        import json as _json

        fetched_at = fetched_at or datetime.now(UTC).isoformat()
        n = 0
        for r in results:
            if "clef" not in r:
                continue
            self.conn.execute(
                "INSERT OR REPLACE INTO occasion_prices "
                "(key, median, plafond, n_total, status, sources, fetched_at) VALUES (?,?,?,?,?,?,?)",
                (
                    r["clef"],
                    round(r["mediane"]) if r.get("mediane") is not None else None,
                    round(r["plafond"]["mediane"]) if r.get("plafond") and r["plafond"].get("mediane") else None,
                    sum(s.get("n") or 0 for s in r.get("sources", [])),
                    r.get("status", "aucune-source"),
                    _json.dumps(
                        {"sources": r.get("sources", []), "exclus": r.get("exclus", []),
                         "plafond": r.get("plafond"), "detail": r.get("detail")},
                        ensure_ascii=False, default=str),
                    fetched_at,
                ),
            )
            n += 1
        self.conn.commit()
        return n

    def occasion_rows(self) -> list[tuple]:
        """Lignes occasion (key, median, plafond, status, fetched_at), récentes d'abord."""
        return list(
            self.conn.execute(
                "SELECT key, median, plafond, status, fetched_at "
                "FROM occasion_prices ORDER BY fetched_at DESC, key"
            )
        )

    def retail_rows(self) -> list[tuple]:
        """Toutes les lignes retail (key, price, pass_at), les + récentes d'abord."""
        return list(
            self.conn.execute(
                "SELECT key, price, n_offers, min_price, max_price, pass_at "
                "FROM retail_prices ORDER BY pass_at DESC, key"
            )
        )

    # --- offres en attente de validation humaine -----------------------------

    def add_pending(self, search_name: str, listing: Listing, context: str | None = None) -> bool:
        """Met une offre en file d'attente (hold_notifications). Retourne True
        si c'est une NOUVELLE attente (déjà présente → False, pas de doublon)."""
        now = datetime.now(UTC).isoformat()
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO pending_offers "
            "(listing_id, search, title, price, url, context, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                listing.listing_id,
                search_name,
                listing.title,
                listing.price,
                listing.url,
                context,
                now,
            ),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def listing_text(self, search: str, listing_id: str) -> str | None:
        """search_text stocké pour une annonce (titre + description + attributs).

        Utilisé par send_pending.py pour reconstruire un embed complet :
        pending_offers ne stocke pas le texte (incident 03/11).
        """
        row = self.conn.execute(
            "SELECT search_text FROM listings WHERE search = ? AND listing_id = ?",
            (search, listing_id),
        ).fetchone()
        return row[0] if row else None

    def pending_list(self, only_unsent: bool = True) -> list[tuple]:
        """Offres en attente : (id, listing_id, search, title, price, url, context,
        created_at, sent_at), les plus récentes d'abord."""
        where = "WHERE sent_at IS NULL" if only_unsent else ""
        return list(
            self.conn.execute(
                f"SELECT id, listing_id, search, title, price, url, context, created_at, sent_at "
                f"FROM pending_offers {where} ORDER BY id DESC"
            )
        )

    def mark_pending_sent(self, offer_id: int) -> bool:
        """Marque une offre comme envoyée (après validation humaine)."""
        now = datetime.now(UTC).isoformat()
        cur = self.conn.execute(
            "UPDATE pending_offers SET sent_at = ? WHERE id = ? AND sent_at IS NULL",
            (now, offer_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def mark_seen(
        self,
        search_name: str,
        listings: list[Listing],
        reject_reasons: dict[str, str] | None = None,
    ) -> None:
        """Enregistre des annonces comme analysées (status 'seen').

        Persiste aussi le texte complet (titre + description + attributs) et,
        pour les rejetées, le premier critère échoué (`filters.first_reject`) —
        permet d'auditer a posteriori si un filtre a rejeté trop large.
        `reject_reasons` mappe listing_id → raison ; absent = annonce retenue.
        """
        now = datetime.now(UTC).isoformat()
        for lst in listings:
            self.conn.execute(
                "INSERT OR IGNORE INTO listings "
                "(listing_id, search, title, price, url, first_seen, status, search_text, reject_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, 'seen', ?, ?)",
                (
                    lst.listing_id,
                    search_name,
                    lst.title,
                    lst.price,
                    lst.url,
                    now,
                    lst.search_text(),
                    (reject_reasons or {}).get(lst.listing_id),
                ),
            )
        self.conn.commit()

    def mark_sent(self, search_name: str, listing: Listing) -> None:
        """Annonce notifiée avec succès : 'sent' — ne sera jamais renvoyée."""
        self.conn.execute(
            "UPDATE listings SET status = 'sent', notified = 1, sent_at = ? "
            "WHERE listing_id = ? AND search = ?",
            (datetime.now(UTC).isoformat(), listing.listing_id, search_name),
        )
        self.conn.commit()

    def unmark(self, search_name: str, listing: Listing) -> None:
        """Échec de notification : on retire la trace pour réessayer au prochain cycle."""
        self.conn.execute(
            "DELETE FROM listings WHERE listing_id = ? AND search = ?",
            (listing.listing_id, search_name),
        )
        self.conn.commit()

    def mark_stale(self, search_name: str, current_ids: list[str]) -> None:
        """Passe en 'old' les annonces connues absentes des résultats actuels.

        Une annonce disparue = dépubliée ou vendue. On garde la trace : si le
        vendeur la republie avec le même listing_id, elle n'est pas renotifiée.
        """
        if not current_ids:
            return
        placeholders = ",".join("?" * len(current_ids))
        self.conn.execute(
            f"UPDATE listings SET status = 'old' "
            f"WHERE search = ? AND status != 'old' AND listing_id NOT IN ({placeholders})",
            (search_name, *current_ids),
        )
        self.conn.commit()

    def counts(self) -> dict[str, dict[str, int]]:
        """Compteurs par recherche et par statut, pour `lbc-alerte status`."""
        out: dict[str, dict[str, int]] = {}
        for search_name, status, n in self.conn.execute(
            "SELECT search, status, COUNT(*) FROM listings GROUP BY search, status"
        ):
            out.setdefault(search_name, {})[status] = int(n)
        return out

    def recent_sent(self, search_name: str, limit: int = 10) -> list[tuple]:
        """Dernières annonces notifiées (titre, prix, date d'envoi, url)."""
        cur = self.conn.execute(
            "SELECT title, price, sent_at, url FROM listings "
            "WHERE search = ? AND status = 'sent' ORDER BY sent_at DESC LIMIT ?",
            (search_name, limit),
        )
        return cur.fetchall()

    # --- marché (observations de prix) --------------------------------------

    def known_count(self, search_name: str) -> int:
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM listings WHERE search = ?", (search_name,)
        )
        return int(cur.fetchone()[0])

    # --- prix ----------------------------------------------------------------

    def known_prices(self, search_name: str) -> dict[str, tuple[int | None, str, str | None]]:
        """listing_id → (dernier prix connu, status, reject_reason)."""
        return {
            row[0]: (row[1], row[2], row[3])
            for row in self.conn.execute(
                "SELECT listing_id, price, status, reject_reason FROM listings WHERE search = ?",
                (search_name,),
            )
        }

    def update_price(self, search_name: str, listing_id: str, old: int | None, new: int) -> None:
        """Nouveau prix observé : met à jour l'annonce et garde l'historique."""
        now = datetime.now(UTC).isoformat()
        has_history = self.conn.execute(
            "SELECT 1 FROM listing_prices WHERE search = ? AND listing_id = ? LIMIT 1",
            (search_name, listing_id),
        ).fetchone()
        if not has_history and old is not None:
            first = self.conn.execute(
                "SELECT first_seen FROM listings WHERE search = ? AND listing_id = ?",
                (search_name, listing_id),
            ).fetchone()
            self.conn.execute(
                "INSERT INTO listing_prices (listing_id, search, price, seen_at) VALUES (?,?,?,?)",
                (listing_id, search_name, old, first[0] if first else now),
            )
        self.conn.execute(
            "INSERT INTO listing_prices (listing_id, search, price, seen_at) VALUES (?,?,?,?)",
            (listing_id, search_name, new, now),
        )
        self.conn.execute(
            "UPDATE listings SET price = ? WHERE search = ? AND listing_id = ?",
            (new, search_name, listing_id),
        )
        self.conn.commit()

    # --- planification -----------------------------------------------------

    def mark_run(self, search_name: str, at: datetime | None = None) -> None:
        when = (at or datetime.now(UTC)).isoformat()
        self.conn.execute(
            "INSERT INTO search_runs (search, last_run_at) VALUES (?, ?) "
            "ON CONFLICT(search) DO UPDATE SET last_run_at = excluded.last_run_at",
            (search_name, when),
        )
        self.conn.commit()

    def last_runs(self) -> dict[str, datetime]:
        return {
            name: datetime.fromisoformat(ts)
            for name, ts in self.conn.execute("SELECT search, last_run_at FROM search_runs")
        }

    def close(self) -> None:
        self.conn.close()
