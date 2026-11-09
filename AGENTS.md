# AGENTS.md — Mode d'emploi pour agent LLM

Tu reprends ce repo pour le faire évoluer. Lis ce fichier en entier avant de coder.

## Mission

Bot d'alertes Leboncoin : visiter des URLs de recherche avec un navigateur réel,
détecter les nouvelles annonces, notifier (Discord, email, Telegram, webhook générique).

## État du projet

Ce qui est **fonctionnel** (plomberie testée) :
- CLI complète (`uv run lbc-alerte --help`) : `once`, `daemon`, `login`, `notify-test`, `init-config`.
- Chargement/validation de config TOML avec expansion `${ENV_VAR}`.
- Notifications Discord / email SMTP / Telegram / webhook générique (module `notify/`).
- Dédupliqution SQLite (`store.py`).

Ce qui est **à valider en priorité** (voir `search.py` — marqueurs `# TODO(agent)`) :
- Les sélecteurs CSS d'extraction des annonces. Ils sont plausibles mais **non vérifiés**
  contre le DOM réel. Ta première tâche : lancer `uv run lbc-alerte once` (ou un script
  debug avec `page.content()`), inspecter le DOM live, corriger `LISTING_SELECTORS`,
  ajouter un test avec une fixture HTML.

## Règles non négociables (anti-ban)

0. **Une notification Discord est irréversible** (décision mainteneur 09/11) : on ne
   pousse QUE quand tous les doutes sont levés. Tout doute (modèle GPU
   contradictoire titre/description, intention vente vs demande ambiguë, VRAM
   ambiguë…) → `mark_question` → la revue nocturne (agent LLM) tranche → envoi
   manuel via `send_pending.py --send <id>` une fois l'annonce élucidée. Ne
   JAMAIS envoyer « à peu près sûr » : un faux positif ne se rétracte pas.
1. **Jamais de requêtes HTTP brutes** sur leboncoin.fr (pas de httpx/requests sur le site) :
   DataDome bloque en minutes. Toujours le navigateur Playwright avec profil persistant.
2. **Un seul fetch par recherche et par cycle**, intervalle ≥ 10 min.
3. **Pauses aléatoires** entre actions (`browser.py` fournit `human_pause()`).
4. Si un challenge DataDome est détecté : **arrêter le cycle**, logger, et signaler à
   l'utilisateur qu'il doit faire `lbc-alerte login`. Ne jamais réessayer en boucle.
5. Profil navigateur dans `data_dir/profile` — ne jamais le supprimer ni le committer.

## Stack & conventions

- Python ≥ 3.12, gestion par **uv** uniquement (`uv sync`, `uv run`, `uv add`).
- Playwright (navigateur), httpx (notifications sortantes), stdlib au maximum sinon
  (sqlite3, tomllib, smtplib, argparse).
- Config : `config.example.toml` = source de vérité du schéma. Toute nouvelle option doit
  y être documentée et validée dans `config.py`.
- Style : ruff (`uv run ruff check .`), tests pytest (`uv run pytest`). Un commit ne passe
  que si les deux sont verts.
- Commits : impératif, une ligne claire, ex. `fix(search): sélecteurs validés sur DOM réel`.
- Langue du code : identifiants/commits en anglais, commentaires/docstrings en français.

## Roadmap suggérée

1. Valider les sélecteurs sur DOM réel (voir plus haut).
2. Dédupliqution fine : re-détecter les changements de prix d'une annonce déjà vue
   (table `listings` : colonne `last_price`, alerte "prix baissé").
3. Scheduling propre : option systemd timer (fournir `dist/leboncoin-alerte.service` +
   `.timer`) en alternative au daemon interne.
4. Resume/graceful shutdown du daemon (SIGTERM → finir le cycle en cours).

## Commandes utiles

```bash
uv sync                              # setup
uv run playwright install chromium   # navigateur (une fois)
uv run lbc-alerte once               # un cycle
uv run lbc-alerte notify-test        # test des canaux configurés
uv run pytest                        # tests
uv run ruff check .                  # lint
```
