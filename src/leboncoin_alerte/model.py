"""Modèle de données partagé."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Listing:
    """Une annonce extraite d'une page de résultats (+ page détail)."""

    listing_id: str  # identifiant Leboncoin (list_id)
    title: str
    price: int | None  # euros ; None si non trouvé/GRATUIT
    url: str
    image_url: str | None = None
    city: str | None = None
    published_at: str | None = None  # index_date Leboncoin (heure de publication)
    body: str = ""  # description complète (page détail)
    attributes_text: str = ""  # attributs structurés concaténés (page détail)
    # Champs structurés extraits du JSON détail (None si absents de l'annonce) :
    seller_type: str | None = None  # "private" (particulier) | "pro"
    condition: str | None = None  # "Neuf", "Comme neuf", "Très bon état"…
    computer_type: str | None = None  # "Fixe" | "Portable" | …
    year: int | None = None  # année du matériel (attribut electronic_year)
    shippable: bool | None = None  # envoi possible

    def search_text(self) -> str:
        """Texte complet sur lequel s'appliquent les filtres : titre + description + attributs.

        Les titres Leboncoin sont souvent incomplets ("Pc fixe Gaming") — les filtres
        doivent voir la description et les attributs pour être fiables.
        """
        return " \n ".join(t for t in (self.title, self.body, self.attributes_text) if t)

    def format(self) -> str:
        prix = f"{self.price} €" if self.price is not None else "prix ?"
        lieu = f" — {self.city}" if self.city else ""
        return f"{self.title} — {prix}{lieu}\n{self.url}"
