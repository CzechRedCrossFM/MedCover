#!/usr/bin/env bash
# deploy.sh — pull latest code and redeploy the production stack
#
# Prerequisites on the server:
#   - Git repo cloned (git clone git@github.com:CzechRedCrossFM/MedCover.git)
#   - .env.prod file created from .env.prod.example and filled in
#   - Docker and Docker Compose plugin installed
#
# First-time use:
#   cp .env.prod.example .env.prod   # fill in all values
#   ./deploy.sh
#   # Then open http://<server-ip>:5000 and complete the setup wizard
#
# Subsequent deploys:
#   ./deploy.sh

set -euo pipefail

COMPOSE_FILE="docker-compose.prod.yml"
ENV_FILE=".env.prod"
BRANCH="main"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log()  { echo -e "${GREEN}[deploy]${NC} $*"; }
warn() { echo -e "${YELLOW}[ warn ]${NC} $*"; }
err()  { echo -e "${RED}[error ]${NC} $*"; }

# --------------------------------------------------------------------------- #
# Pre-flight checks
# --------------------------------------------------------------------------- #
if [[ ! -f "$ENV_FILE" ]]; then
    err "$ENV_FILE not found."
    echo "  Copy .env.prod.example and fill in all values:"
    echo "    cp .env.prod.example .env.prod"
    exit 1
fi

if ! command -v docker &>/dev/null; then
    err "Docker is not installed or not in PATH."
    exit 1
fi

# --------------------------------------------------------------------------- #
# Pull latest code
# --------------------------------------------------------------------------- #
log "Pulling latest code from origin/$BRANCH..."
git fetch origin
git checkout "$BRANCH"
git pull origin "$BRANCH"

# --------------------------------------------------------------------------- #
# Build images
# --------------------------------------------------------------------------- #
log "Building images (pulling base image if newer)..."
docker compose -f "$COMPOSE_FILE" build --pull

# --------------------------------------------------------------------------- #
# Restart stack
# migrations + schema verification run automatically in docker-entrypoint.sh
# --------------------------------------------------------------------------- #
log "Starting stack..."
docker compose -f "$COMPOSE_FILE" up -d --remove-orphans

# --------------------------------------------------------------------------- #
# Wait for web to become healthy
# --------------------------------------------------------------------------- #
log "Waiting for web container to become healthy..."
TRIES=0
MAX=24   # 24 × 5 s = 2 minutes
until docker compose -f "$COMPOSE_FILE" ps web 2>/dev/null | grep -q "(healthy)"; do
    TRIES=$((TRIES + 1))
    if [[ $TRIES -ge $MAX ]]; then
        err "Web container did not become healthy after $((MAX * 5)) seconds."
        warn "Last 50 lines of web logs:"
        docker compose -f "$COMPOSE_FILE" logs --tail=50 web
        exit 1
    fi
    echo "  waiting... ($TRIES/$MAX)"
    sleep 5
done

# --------------------------------------------------------------------------- #
# Done
# --------------------------------------------------------------------------- #
log "Deploy complete. Stack status:"
docker compose -f "$COMPOSE_FILE" ps

echo ""
log "Access the app at http://$(hostname -I | awk '{print $1}'):5000"
log "If this is the first deploy, complete the setup wizard to create the admin account."
