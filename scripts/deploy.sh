#!/usr/bin/env bash
# Déploiement lbc-alerte sur une nouvelle machine (ex: runner Illogic).
# Usage : ./scripts/deploy.sh [BUNDLE_DIR]
#
# Prérequis : docker + docker compose v2, repo cloné.
# Le bundle (issu de `lbc-alerte export --with-db` sur la machine source)
# est importé AVANT le premier démarrage pour conserver l'état des annonces.

set -euo pipefail
cd "$(dirname "$0")/.."

BUNDLE="${1:-}"

echo "=== 1/4 Config ==="
if [ -n "$BUNDLE" ] && [ ! -f config.toml ]; then
    # Import côté HOST (jamais via compose run : config monté :ro dans compose,
    # et un conteneur --rm perdrait le .env écrit).
    if [ -x .venv/bin/lbc-alerte ]; then
        .venv/bin/lbc-alerte init --from-bundle "$BUNDLE"
    else
        docker build -q -t lbc-alerte-init . >/dev/null
        # --user : sinon le conteneur (root) écrit un .env root:root illisible
        # pour l'utilisateur ET pour compose.
        docker run --rm --network none --user "$(id -u):$(id -g)" \
            -v "$(realpath "$BUNDLE")":/bundle:ro \
            -v "$PWD":/work -w /work \
            lbc-alerte-init lbc-alerte init --from-bundle /bundle
        # Filet : si l'image a ignoré --user, .env appartient à root.
        if [ -f .env ] && [ "$(stat -c %u .env)" = "0" ]; then
            echo "⚠ .env appartient à root — récupère-le : sudo chown $(id -u):$(id -g) .env"
        fi
    fi
elif [ -f config.toml ]; then
    echo "config.toml déjà présent — bundle ignoré."
else
    echo "Pas de bundle — config vierge : lance 'lbc-alerte init' puis relance ce script."
fi

MISSING=""
if [ -f .env ]; then
    MISSING=$(grep -E '^[A-Za-z_]+=[[:space:]]*$' .env || true)
fi
if [ ! -f config.toml ] || [ ! -f .env ] || [ -n "$MISSING" ]; then
    echo "⚠ Prêt à démarrer mais incomplet :"
    [ -f config.toml ] || echo "  - config.toml absent"
    [ -f .env ] || echo "  - .env absent (cp .env.example .env puis remplis)"
    [ -n "$MISSING" ] && echo "  - .env à compléter : $(echo "$MISSING" | tr '\n' ' ')"
    exit 1
fi

echo "=== 2/4 Build ==="
docker compose build

echo "=== 3/4 Démarrage ==="
docker compose up -d

echo "=== 4/4 Health check (60 s) ==="
sleep 10
for i in $(seq 1 6); do
    if docker compose ps --status running | grep -q lbc-alerte; then
        echo "✓ conteneur running"
        break
    fi
    [ "$i" = 6 ] && { echo "✗ conteneur down — logs :"; docker compose logs --tail 30; exit 1; }
    sleep 10
done
docker compose logs --tail 5

echo ""
echo "Surveillance anti-ban (cron local 1x/h) :"
echo "  docker compose logs --since 1h | grep -ciE '403|captcha'  # > 0 → investigate"
echo "Rollback : docker compose down && git checkout <tag précédent> && docker compose up -d"
