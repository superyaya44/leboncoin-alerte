"""Budget PC : analyse RAM et verdict « bonne affaire » ajusté.

Spécification validée avec mainteneur (29/10/2026) :
- objectif final : 64 Go de RAM ;
- les barrettes ≤ 8 Go comptent pour 0 (inutilisables pour la cible) ;
- coût de la RAM manquante au PRORATA du kit neuf (DDR4 64 Go = 750 €,
  DDR5 64 Go = 1000 €) ;
- bonne affaire si prix_annonce + RAM manquante < 2000 € (DDR4) ou
  < 2500 € (DDR5) ;
- type de RAM non détecté → « à vérifier » (jamais de verdict inventé).
"""

from __future__ import annotations

import re

from .filters import _ram_value, _stick_config

# Cible de RAM pour les futurs PC (Go).
TARGET_RAM_GB = 64
# Une barrette ≤ 8 Go n'est pas réutilisable pour atteindre 64 Go.
UNUSABLE_STICK_GB = 8
# Prix d'un kit neuf de 64 Go (semis 30/10, remplacés par les réfs idealo
# hebdo quand la table retail_prices sera alimentée).
DEFAULT_KIT_PRICES: dict[str, int] = {"ddr4": 750, "ddr5": 1000}
# Budget total (annonce + RAM manquante au prorata) par type.
BUDGETS: dict[str, int] = {"ddr4": 2000, "ddr5": 2500}

_DDR_TYPE = re.compile(r"\bddr([45])\b", re.IGNORECASE)


def ram_type(text: str) -> str | None:
    """Type de RAM détecté ("ddr4" | "ddr5"), ou None."""
    m = _DDR_TYPE.search(text)
    return f"ddr{m.group(1)}" if m else None


def usable_ram_gb(text: str) -> int | None:
    """RAM réutilisable (Go) : somme des barrettes > 8 Go, ou None.

    Kits explicites ("2x16", "4x8 Go") : chaque barrette ≤ 8 Go compte 0.
    Sans kit détecté : capacité totale en contexte RAM ("64 Go DDR5"),
    hors tokens stockage ("SSD 512 Go" ne compte pas).
    """
    total = 0
    found = False
    for cap, count in _iter_kits(text):
        found = True
        if cap > UNUSABLE_STICK_GB:
            total += cap * count
    if found:
        return total
    value = _ram_value(text)
    return value if value and value > UNUSABLE_STICK_GB else (0 if value else None)


def _iter_kits(text: str) -> list[tuple[int, int]]:
    """Kits (capacité_par_barrette, nombre) plausibles mentionnés dans le texte."""
    kits = _stick_config(text)
    return [kits] if kits else []


def ram_upgrade_cost(ram_kind: str | None, current_gb: int | None) -> int | None:
    """Coût de la RAM manquante pour atteindre 64 Go (prorata du kit neuf).

    None si le type est inconnu ou la RAM actuelle indétectable → pas de
    verdict inventé. RAM déjà ≥ 64 Go → 0.
    """
    if ram_kind is None:
        return None
    if current_gb is None:
        return None
    if current_gb >= TARGET_RAM_GB:
        return 0
    kit = DEFAULT_KIT_PRICES[ram_kind]
    return round(kit * (TARGET_RAM_GB - current_gb) / TARGET_RAM_GB)


def adjusted_price(price: int | None, ram_kind: str | None, current_gb: int | None) -> int | None:
    """Prix total réel = annonce + RAM à acheter, ou None si indéterminable."""
    if price is None:
        return None
    cost = ram_upgrade_cost(ram_kind, current_gb)
    return None if cost is None else price + cost


def budget_verdict(adjusted: int | None, ram_kind: str | None) -> str:
    """Verdict budget : "good_deal" | "over_budget" | "unknown"."""
    if adjusted is None or ram_kind is None:
        return "unknown"
    return "good_deal" if adjusted < BUDGETS[ram_kind] else "over_budget"


def analyze(text: str, price: int | None) -> dict:
    """Profil complet d'une annonce PC pour le rapport nocturne."""
    kind = ram_type(text)
    gb = usable_ram_gb(text)
    adj = adjusted_price(price, kind, gb)
    return {
        "ram_type": kind,
        "ram_gb": gb,
        "ram_cost": ram_upgrade_cost(kind, gb),
        "adjusted_price": adj,
        "verdict": budget_verdict(adj, kind),
    }
