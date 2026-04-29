#!/usr/bin/env bash
# LiteLLM setup script — auto-detect existing install and configure
set -euo pipefail

LITELLM_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$LITELLM_DIR/.env"
ENV_EXAMPLE="$LITELLM_DIR/.env.example"
CONFIG_FILE="$LITELLM_DIR/config.yaml"
COMPOSE_FILE="$LITELLM_DIR/docker-compose.yml"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}[litellm]${NC} $*"; }
ok()    { echo -e "${GREEN}[litellm]${NC} $*"; }
warn()  { echo -e "${YELLOW}[litellm]${NC} $*"; }
fail()  { echo -e "${RED}[litellm]${NC} $*"; exit 1; }

echo "=== LiteLLM Setup for MemPalace ==="
echo ""

# ─── Step 1: Detect existing LiteLLM install ────────────────────────────────
LITELLM_MODE=""
# First check: is a LiteLLM proxy already responding on :4000? If so, whoever
# started it (our Docker container, the user's existing Python install, or an
# external proxy) is doing the job — don't start a competing one on the same
# port.
if curl -fs http://127.0.0.1:4000/health/readiness >/dev/null 2>&1; then
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -q mempalace-litellm; then
        LITELLM_MODE="docker-running"
        ok "Detected: LiteLLM Docker container running (ours)"
    else
        LITELLM_MODE="external-running"
        ok "Detected: LiteLLM proxy already responding on :4000 (not ours)"
    fi
elif docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q mempalace-litellm; then
    LITELLM_MODE="docker-stopped"
    ok "Detected: LiteLLM Docker container exists (stopped)"
elif command -v docker &>/dev/null && [ -f "$COMPOSE_FILE" ]; then
    LITELLM_MODE="docker-ready"
    ok "Detected: Docker available, compose file present"
elif command -v litellm &>/dev/null; then
    LITELLM_MODE="python"
    ok "Detected: LiteLLM Python package installed ($(which litellm))"
else
    LITELLM_MODE="none"
    warn "No existing LiteLLM install detected"
fi

# ─── Step 2: Check .env file ────────────────────────────────────────────────
if [ ! -f "$ENV_FILE" ]; then
    info "Creating .env from template..."
    cp "$ENV_EXAMPLE" "$ENV_FILE"
    warn ".env created — edit $ENV_FILE and add your GEMINI_API_KEY"
    warn "Get a key at: https://aistudio.google.com/apikey"
    echo ""
    echo "After editing .env, re-run: bash $0"
    exit 0
fi

# Source .env and check for required keys
set +u  # allow unset vars temporarily
source "$ENV_FILE"
MISSING=""
if [ -z "${GEMINI_API_KEY:-}" ] && [ -z "${VERTEXAI_PROJECT:-}" ]; then
    MISSING="GEMINI_API_KEY or VERTEXAI_PROJECT"
fi
set -u

if [ -n "$MISSING" ]; then
    fail "Missing required env var: $MISSING. Edit $ENV_FILE and re-run."
fi

# Detect backend from .env
BACKEND="gemini"
if [ -n "${VERTEXAI_PROJECT:-}" ]; then
    BACKEND="vertex"
    ok "Backend: Vertex AI (project: ${VERTEXAI_PROJECT}, location: ${VERTEXAI_LOCATION:-global})"
else
    ok "Backend: Gemini API"
fi

# ─── Step 3: Verify config.yaml matches backend ─────────────────────────────
info "Checking config.yaml backend alignment..."
if [ "$BACKEND" = "vertex" ]; then
    if grep -q "model: gemini/" "$CONFIG_FILE" && ! grep -q "model: vertex_ai/" "$CONFIG_FILE"; then
        warn "config.yaml uses gemini/* models but .env has VERTEXAI_PROJECT"
        warn "Uncomment the vertex_ai/* entries in config.yaml and comment out gemini/*"
        warn "See config.yaml comments for instructions"
        exit 1
    fi
    ok "config.yaml aligned with Vertex AI backend"
else
    if grep -q "model: vertex_ai/" "$CONFIG_FILE" && ! grep -q "#.*model: vertex_ai/" "$CONFIG_FILE"; then
        warn "config.yaml has uncommented vertex_ai/* models but .env uses GEMINI_API_KEY"
        warn "Comment out vertex_ai/* entries and uncomment gemini/* entries"
        exit 1
    fi
    ok "config.yaml aligned with Gemini API backend"
fi

# ─── Step 4: Start/restart LiteLLM based on detected mode ───────────────────
case "$LITELLM_MODE" in
    external-running)
        ok "LiteLLM proxy already running on :4000 (not managed by this script)"
        echo ""
        echo "The proxy is responding but wasn't started by this script's Docker"
        echo "container. If you want to use the config in this directory:"
        echo "  1. Stop the existing proxy"
        echo "  2. Re-run: bash $0"
        echo ""
        echo "Or keep using your existing proxy — MemPalace will work as long as"
        echo "~/.mempalace/env points to http://127.0.0.1:4000 with the right key."
        exit 0
        ;;
    docker-running)
        info "Restarting Docker container to pick up config changes..."
        docker compose -f "$COMPOSE_FILE" restart
        ok "Container restarted"
        ;;
    docker-stopped)
        info "Starting existing Docker container..."
        docker compose -f "$COMPOSE_FILE" up -d
        ok "Container started"
        ;;
    docker-ready)
        info "Starting LiteLLM Docker container..."
        docker compose -f "$COMPOSE_FILE" up -d
        ok "Container started"
        ;;
    python)
        warn "Python LiteLLM detected — you'll need to start it manually:"
        echo "  litellm --config $CONFIG_FILE --port 4000"
        echo ""
        echo "Or switch to Docker: docker compose -f $COMPOSE_FILE up -d"
        exit 0
        ;;
    none)
        if command -v docker &>/dev/null; then
            info "Installing LiteLLM via Docker..."
            docker compose -f "$COMPOSE_FILE" up -d
            ok "Container started"
        else
            fail "Docker not found. Install Docker or run: pip install litellm"
        fi
        ;;
esac

# ─── Step 5: Health check ───────────────────────────────────────────────────
info "Waiting for LiteLLM to be ready..."
for i in {1..30}; do
    if curl -fs http://127.0.0.1:4000/health/readiness >/dev/null 2>&1; then
        ok "LiteLLM is ready at http://127.0.0.1:4000"
        echo ""
        echo "✓ Setup complete. MemPalace will use this proxy automatically."
        echo "  (default ~/.mempalace/env points to http://127.0.0.1:4000)"
        echo ""
        echo "Test it:"
        echo "  curl -H 'Authorization: Bearer sk-litellm-local' \\"
        echo "       http://127.0.0.1:4000/v1/models"
        exit 0
    fi
    sleep 1
done

fail "LiteLLM failed to start. Check logs: docker compose -f $COMPOSE_FILE logs"
