"""Session navigateur persistante (Playwright).

Le profil Chromium vit dans <data_dir>/profile : cookies et session DataDome survivent
d'une exécution à l'autre — c'est ce qui fait passer le bot pour un humain régulier.
NE JAMAIS supprimer ce dossier (règle AGENTS.md).
"""

from __future__ import annotations

import random
import time
from contextlib import contextmanager
from pathlib import Path

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

# Chaîne de détection DataDome présente dans les pages de challenge.
DATADOME_MARKER = "datadome"

# Signaux d'un blocage (contenu de la page ou code HTTP).
BLOCK_MARKERS = ("cf-chl", "captcha", "pardon our interruption")


def human_pause(min_s: float = 1.0, max_s: float = 3.5) -> None:
    """Pause aléatoire entre deux actions, comme un humain qui lit."""
    time.sleep(random.uniform(min_s, max_s))


def is_blocked(page: Page) -> bool:
    """Détecte un challenge/blocage sur la page courante."""
    try:
        content = page.content().lower()
    except Exception:  # noqa: BLE001 — page fermée/crashée : considérer non bloquée
        return False
    return any(marker in content for marker in (DATADOME_MARKER, *BLOCK_MARKERS)) and (
        "classifieds" not in content
    )


class BrowserSession:
    """Contexte navigateur persistant.

    Deux modes :
    - cdp_url (recommandé) : se connecte au Chromium persistant du VPS (profil
      validé DataDome, vrai UA). Ouvre un onglet dédié, le referme après.
    - sinon : lance un Chromium Playwright avec user_data_dir persistant.
    """

    def __init__(self, profile_dir: Path, headless: bool = True, cdp_url: str | None = None):
        self.profile_dir = profile_dir
        self.headless = headless
        self.cdp_url = cdp_url
        profile_dir.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def page(self) -> Page:
        if self.cdp_url:
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(self.cdp_url)
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                page = context.new_page()
                try:
                    yield page
                finally:
                    page.close()
                    browser.close()  # ferme la connexion CDP, PAS le navigateur
            return
        with sync_playwright() as p:
            context: BrowserContext = p.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                headless=self.headless,
                locale="fr-FR",
                timezone_id="Europe/Paris",
                viewport={"width": 1366, "height": 900},
                args=["--disable-blink-features=AutomationControlled"],
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                yield page
            finally:
                context.close()

    def interactive(self, url: str | None = None) -> None:
        """Ouvre le navigateur VISIBLE pour résoudre un challenge une bonne fois pour
        toutes (lbc-alerte login). Sur serveur headless : xvfb-run uv run lbc-alerte login."""
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                headless=False,
                locale="fr-FR",
                timezone_id="Europe/Paris",
            )
            page = context.pages[0] if context.pages else context.new_page()
            if url:
                page.goto(url)
            print("Navigateur ouvert. Résous le challenge si besoin, puis Ctrl+C pour quitter.")
            try:
                while True:
                    time.sleep(5)
            except KeyboardInterrupt:
                context.close()
                print("Session sauvegardée dans le profil.")


# Ré-export pratique pour les tests.
__all__ = ["Browser", "BrowserSession", "Page", "human_pause", "is_blocked"]
