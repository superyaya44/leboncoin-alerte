# leboncoin-alerts

Surveille des recherches Leboncoin avec un vrai navigateur (comportement humain) et envoie
des alertes sur les canaux de ton choix : Discord, email, Telegram, webhook générique.

Chaque recherche a ses propres filtres, et si un référentiel de prix marché est fourni
([`market_prices.toml`](market_prices.toml)), l'alerte Discord affiche l'écart en %
entre le prix de l'annonce et le marché (embed vert dès 10 % sous le marché).

Conçu pour être **donné à un agent LLM** : lis [`AGENTS.md`](AGENTS.md) si tu es un agent.

## Principe

1. Tu crées ta recherche sur leboncoin.fr (filtres, ville, prix…) et tu copies l'URL.
2. Tu colles cette URL dans `config.toml` avec tes canaux de notification.
3. Le bot visite cette URL périodiquement avec Chromium, détecte les **nouvelles**
   annonces (dédupliqution SQLite) et notifie celles qui passent tes filtres.

## Démarrage rapide (uv — recommandé)

```bash
git clone https://github.com/superyaya44/leboncoin-alerts.git && cd leboncoin-alerts
uv sync                      # installe les deps dans .venv
uv run playwright install chromium   # télécharge le navigateur
uv run lbc-alerte init               # wizard : config manuelle, import d'un bundle, ou exemple
uv run lbc-alerte notify-test        # vérifie que les notifications partent
uv run lbc-alerte once               # un cycle de vérification
uv run lbc-alerte daemon             # boucle permanente (interval_minutes)
uv run lbc-alerte status             # compteurs new/seen/sent/old + dernières alertes
```

Première fois si Leboncoin affiche un challenge DataDome :

```bash
uv run lbc-alerte login       # ouvre le navigateur (xvfb-run sur serveur headless) —
                              # résous le challenge une fois, la session est conservée
```

Sur le VPS, option la plus fiable : réutiliser le Chromium persistant via CDP
(déjà validé DataDome) dans `[general]` :

```toml
cdp_url = "http://127.0.0.1:9222"
```

## Config portable : init / export / import

Le repo est un **template vierge** : tes recherches et filtres vivent dans
`config.toml` (gitignoré), jamais committés. Trois commandes gèrent la config :

- **`lbc-alerte init`** — wizard interactif. Trois modes : configurer à la main
  (URL leboncoin, prix, filtres, canal de notif), **importer un bundle** exporté
  depuis une autre machine (migration), ou partir de l'exemple.
  Les secrets (webhook, token, mot de passe SMTP) vont dans `.env`, référencés
  dans config.toml via `${VAR}`.
- **`lbc-alerte export [--with-db] [--with-profile]`** — fige la config courante
  en bundle portable : `config.toml` **sanitisé** (secrets remplacés par
  `${VAR}`), `market_prices.toml`, `listings.db` en option (état des annonces
  déjà vues — évite les re-notifications), `manifest.json` (sha256 + vars
  d'env requises). Le bundle se transfère par scp/rsync, jamais par git.
- **`lbc-alerte import <bundle> [--force]`** — restaure un bundle dans le
  répertoire courant (vérifie les sha256, n'écrase rien sans `--force`,
  pré-remplit `.env` avec les vars requises vides).

Migration vers une nouvelle machine (ex: infra GitLab) :

```bash
# machine source
lbc-alerte export --with-db            # → lbc-bundle-<date>/
rsync -a lbc-bundle-<date>/ cible:bundle/

# machine cible (repo cloné)
lbc-alerte init --from-bundle bundle/  # ou ./scripts/deploy.sh bundle/
```

## Démarrage (Docker — alternative)

```bash
cp config.example.toml config.toml  # édite
docker compose up -d                # image Playwright officielle, daemon en boucle
```

## Modifier le webhook Discord

L'URL du webhook vit dans **`.env`** (à la racine, jamais commité — règle : aucun secret
dans git) :

```
DISCORD_WEBHOOK_URL=https://discordapp.com/api/webhooks/…
```

Édite la ligne, c'est tout. Le daemon relit `.env` et `config.toml` **à chaque cycle** :
aucun redémarrage nécessaire. Vérification après modif :

```bash
uv run lbc-alerte notify-test
```

Pour créer un webhook : Discord → Paramètres du salon → Intégrations → Webhooks →
Nouveau webhook → Copier l'URL. Un webhook dédié par outil permet de le révoquer
sans casser les autres.

## Modifier ses critères de recherche

Tout est dans **`config.toml`**, bloc `[[search]]` :

```toml
[[search]]
name = "pc-fixe-lyon"
url = "https://www.leboncoin.fr/recherche?category=15&text=pc+fixe&price=500-max&sort=time&order=desc"
min_ram_gb = 32                     # RAM minimale (Go) — ligne optionnelle
regex_all = ['rtx[ -]?(30[5-9]\d?|40[5-9]\d?|50\d{2,3})']   # GPU ≥ RTX 3050 — optionnel
min_disk_gb = 1000                  # stockage total min en Go (1 To = 1024) — optionnel
```

### Ajouter une nouvelle recherche demain (recettes prêtes à copier)

**Disque minimum** — critère numérique, comme la RAM :
```toml
[[search]]
name = "pc-fixe-1to"
url = "…"                  # ta nouvelle recherche Leboncoin
min_disk_gb = 1000         # 1 To de stockage total min ("SSD 500 Go + 500 Go" = 1000)
```

**CPU minimum** — pas d'échelle numérique universelle (un i7 Intel face à un Ryzen 7),
donc on l'exprime en regex sur le texte complet. Recettes courantes :
```toml
[[search]]
name = "pc-fixe-cpu-haut-de-gamme"
url = "…"
# i7/i9 avec modèle 4 chiffres, OU Ryzen 7/9 :
regex_all = ['i[79][\s-]?\d{4}|ryzen\s?[79]\s?\d{3,4}']

# Intel 12e génération ou plus (12100, 13600K, 14900K…) — cumulable avec la ligne
# ci-dessus en DEUX entrées regex_all (toutes doivent matcher = ET) :
# regex_all = ['i[79][\s-]?\d{4}|ryzen\s?[79]\s?\d{3,4}', 'i[3579][\s-]?\d{4}']
```

Tous les critères se combinent librement dans un même bloc `[[search]]` :
`min_ram_gb` + `min_disk_gb` + `regex_all` + `max_price` — tous optionnels, tous en ET.

- **URL** : le plus simple est de faire ta recherche sur leboncoin.fr (ville, rayon,
  prix, catégorie…), puis de copier l'URL du navigateur et la coller telle quelle.
  Ajoute `&sort=time&order=desc` pour trier par plus récentes.
  `locations=Ville_34__lat_lng_id_RAYON-METRES` encode le rayon (ex. `100000` = 100 km).
- **Filtres côté bot** (en plus de ceux de l'URL) — tous optionnels, tous combinables (ET),
  ignorés quand vides :

| Critère | Type | S'applique à | Exemple |
|---|---|---|---|
| `min_price` / `max_price` | € | prix | `max_price = 1200` |
| `min_ram_gb` | Go | RAM (détection par contexte, "SSD 512 Go" ≠ RAM) | `32` |
| `min_disk_gb` | Go | stockage total (1 To = 1024 Go, disques sommés) | `1000` |
| `regex_all` | regex (ET) | texte complet titre+description+attributs | GPU, CPU… |
| `regex_any` | regex (OU) | idem | `['i7', 'ryzen 7']` |
| `keywords_include`/`_exclude` | mots | titre seul | `['tour']` |
| `text_keywords_include`/`_exclude` | mots | texte complet | `['ddr5']` / `['revend']` |
| `cities_include`/`_exclude` | mots | ville (sous-chaîne) | `['lyon', 'sète']` |
| `max_age_hours` | h | fraîcheur de publication | `48` |
| `computer_type` | texte | attribut LBC | `"Fixe"` (exclut les portables) |
| `seller_type` | texte | vendeur LBC | `"particulier"` ou `"pro"` |
| `condition_include` | liste | état LBC | `['neuf', 'comme neuf']` |
| `min_year` | année | attribut LBC electronic_year | `2023` |
| `shippable_only` | bool | envoi possible | `true` |

- **Plusieurs recherches** : duplique le bloc `[[search]]` (une recherche LBC = un bloc,
  avec ses propres filtres), chacune notifie sur les mêmes canaux.

Les champs structurés (`computer_type`, `seller_type`, `condition`, `min_year`,
`shippable_only`) sont permissifs : si l'annonce ne renseigne pas l'information,
elle passe — le critère ne filtre que sur les valeurs connues. `cities_include`
est strict : ville inconnue = rejet.

### Compter sans envoyer : scripts/count_matches.py

```bash
uv run python scripts/count_matches.py    # [-c config.toml] pour un autre fichier
```

Relit toutes les annonces de la recherche, applique les filtres **en lecture
seule** (aucun envoi, aucune écriture en base) et imprime le décompte exact +
la liste des qualifiées avec URLs. Utile pour valider un changement de critères
avant de le laisser tourner.

Les critères s'appliquent après lecture de la **page détail** (description complète) —
les titres Leboncoin seuls sont trop peu fiables.

## Comment ça marche : garantir de ne rien rater

Chaque cycle (toutes les `interval_minutes`, 15 par défaut) :

1. **Extraction** : pagination **adaptative** — les pages de résultats (≤ 10, ~35
   annonces/page) sont lues depuis le JSON embarqué de la page (pas de sélecteurs
   CSS fragiles), et le parcours **s'arrête** dès que la page courante ne contient
   plus rien d'inédit : annonce déjà connue (rattrapage du point d'arrêt du cycle
   précédent) ou annonce de plus de 90 jours (tri par date ⇒ tout ce qui suit est
   plus vieux). En régime établi, une seule page est chargée par cycle.
2. **Dédupliqution** : chaque annonce a un identifiant unique Leboncoin (`listing_id`).
   Une annonce déjà traitée n'est **jamais** renvoyée.
3. **Analyse** : les nouvelles annonces uniquement (plafond de 40 pages détail/cycle
   pour rester discret) → description + attributs → filtres.
4. **Notification** : les annonces qualifiées partent sur Discord — ou, si
   `hold_notifications = true`, en **file de validation humaine** (voir
   [Validation humaine](#validation-humaine-hold_notifications)). Si l'envoi
   échoue, l'annonce redevient "new" et sera **retentée** au prochain cycle.

### Cycle de vie d'une annonce (SQLite, `lbc-alerte status`)

| Statut | Signification |
|--------|---------------|
| `new`  | présente sur Leboncoin, pas encore analysée (aucune ligne en base) |
| `seen` | analysée, n'a pas passé les filtres — pas notifiée |
| `sent` | qualifiée et notifiée (ou mise en file) avec succès — jamais renvoyée |
| `old`  | disparue des résultats : dépubliée ou vendue |
| `question` | GPU ambigu sans VRAM précisée (ex. 5060 Ti = 8 OU 16 Go) : une question vendeur est stockée, envoyée uniquement sur action humaine |

### Cascade de filtrage VRAM (`min_vram_gb`)

Décision dans l'ordre, chaque niveau court-circuite le suivant :

1. **VRAM explicite** dans le texte — y compris sans la marque (« Zotac 3080 Ti
   12Gb », « 5060 Ti 16Go ») : comparée au seuil ;
2. **Défaut par modèle connu** : une 4090 fait 24 Go, une 5080 16 Go — toutes
   variantes confondues ;
3. **Modèle ambigu** (existe en 8 et 16 Go, ex. 5060 Ti) sans précision :
   - `vram_ask = false` (défaut) → rejet `min_vram_gb` ;
   - `vram_ask = true` → statut `question` + message vendeur en attente ;
4. **Modèle inconnu ou absent** → rejet (pas de valeur sûre).

Les annonces disparues passent automatiquement en `old` à chaque cycle : si un vendeur
republie avec le même identifiant, pas de re-notification. Un premier lancement sur une
recherche à 350 annonces rattrape le backlog en quelques cycles (40 analyses/cycle) ;
ensuite, seules les **nouvelles** annonces du jour sont envoyées.

La base vit dans `data/listings.db` — supprime-la pour tout renotifier (reset complet).

## Références marché : le NEUF prime (idealo.fr)

Les comparaisons « vs marché » des alertes utilisent par ordre de priorité :

1. **Prix NEUF** de la table `retail_prices`, alimentée par une passe hebdo
   (samedi → dimanche) sur idealo.fr — médiane des fiches produit par modèle :

   ```bash
   uv run python scripts/retail_prices.py              # tous les modèles suivis
   uv run python scripts/retail_prices.py rtx-5090     # un seul modèle
   ```

   Le scraping passe par le Chromium persistant CDP (`cdp_url`), pauses anti-ban
   incluses. Une clé sans donnée cette semaine garde sa valeur de la semaine
   précédente (pas de régression vers l'occasion).
2. **Médianes d'occasion** glissantes 30 jours, auto-apprises sur les annonces
   vues (`market_obs`) — repli tant qu'une référence neuf n'existe pas.
3. **Amorçage** `market_prices.toml` (optionnel, pour le tout premier lancement).

## Budget PC : verdict « bonne affaire » ajusté RAM (scripts/budget_review.py)

Objectif : 64 Go de RAM. Pour chaque annonce PC non rejetée, le rapport nocturne
calcule :

- le **type** détecté (DDR4/DDR5) et la **RAM réutilisable** — les barrettes
  ≤ 8 Go comptent pour 0 ;
- le **coût de la RAM manquante**, au prorata d'un kit neuf 64 Go
  (750 € DDR4 / 1000 € DDR5 par défaut) ;
- le **prix ajusté** = annonce + RAM manquante, comparé au budget :
  **< 2000 € (DDR4) / < 2500 € (DDR5)** = bonne affaire. Type ou RAM
  indétectable → « à vérifier », jamais de verdict inventé.

```bash
uv run python scripts/budget_review.py --days 2 [--db data/listings.db]
```

## Validation humaine (hold_notifications)

Avec `hold_notifications = true` dans `[general]`, aucune annonce n'est envoyée
directement : elles vont dans la table `pending_offers` et le rapport quotidien
les liste. La validation est un geste **humain** :

```bash
uv run python scripts/send_pending.py           # liste des offres en attente
uv run python scripts/send_pending.py --send 12 # pousse l'offre #12 sur Discord
```

Passer `hold_notifications = false` rétablit l'envoi direct quand la boucle est
devenue fiable.

### Service systemd (VPS)

```bash
sudo tee /etc/systemd/system/lbc-alerte.service > /dev/null << 'EOF'
[Unit]
Description=Leboncoin alerts — daemon de surveillance (leboncoin-alerte)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/projects/leboncoin-alerte
ExecStart=/home/ubuntu/projects/leboncoin-alerte/.venv/bin/lbc-alerte daemon
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now lbc-alerte
systemctl status lbc-alerte          # vérification
journalctl -u lbc-alerte -f          # logs en direct
```

Le service redémarre automatiquement (crash, reboot) et recharge `config.toml`
et `.env` à chaque cycle. Prérequis : le Chromium persistant doit être joignable
si `cdp_url` est configuré — sinon le daemon retente au cycle suivant sans mourir.

## Commandes (récapitulatif)

| Commande | Rôle |
|---|---|
| `uv run lbc-alerte once` | un cycle immédiat |
| `uv run lbc-alerte daemon` | boucle permanente |
| `uv run lbc-alerte status` | compteurs new/seen/sent/old + dernières alertes |
| `uv run lbc-alerte notify-test` | envoi une fausse annonce sur les canaux activés |
| `uv run lbc-alerte login` | navigateur visible pour valider session/DataDome |
| `uv run python scripts/count_matches.py` | décompte des qualifiées, lecture seule |
| `uv run python scripts/budget_review.py` | rapport budget RAM + verdicts (nocturne) |
| `uv run python scripts/retail_prices.py` | passe hebdo prix NEUF idealo → réfs marché |
| `uv run python scripts/send_pending.py` | liste / envoie les offres en attente (`--send <id>`) |
| `uv run python scripts/audit_filtered.py` | audit des rejets récents (diag filtres) |

## Config (config.toml)

Voir [`config.example.toml`](config.example.toml) commenté. Points clés :

- Une `[[search]]` = une URL Leboncoin + filtres optionnels côté bot.
- Chaque canal `[notify.*]` est activable indépendamment (`enabled = true`).
- Toute valeur peut référencer l'environnement : `webhook_url = "${DISCORD_WEBHOOK_URL}"`
  → met tes secrets dans un `.env` ou l'env, pas dans le fichier.

## Architecture

```
src/leboncoin_alerte/
├── cli.py        # entrypoint (once / daemon / login / notify-test / status / init-config)
├── config.py     # chargement + validation TOML (+ ${ENV}, .env)
├── browser.py    # Chromium persistant (Playwright ou CDP), pauses aléatoires
├── search.py     # extraction JSON __NEXT_DATA__ + pagination adaptative + pages détail
├── filters.py    # filtres texte complet + cascade VRAM (explicite → modèle → ambigu)
├── budget.py     # analyse RAM (type, Go réutilisables) + verdict budget DDR4/DDR5
├── market.py     # réfs marché : occasion glissante + retail neuf (prime)
├── retail.py     # scraping idealo.fr hebdo (recherche → fiches → médianes)
├── store.py      # SQLite : cycle de vie, questions vendeur, pending_offers, réfs neuf
└── notify/       # discord / email / telegram / webhook générique
```

## Avertissement

Usage personnel. Respecte les CGU de Leboncoin : intervalles raisonnables (≥ 10 min par
recherche), pas de revente des données, pas de spam. Le projet ralentit et s'arrête s'il
détecte un blocage (DataDome) plutôt que d'insister.
