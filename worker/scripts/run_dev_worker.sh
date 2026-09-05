#!/usr/bin/env bash
# run_dev_worker.sh — Launch dedicated Dev instance agent worker

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$ROOT_DIR"

if [ -f ".env.dev" ]; then
    echo "[dev-worker] Loading environment from .env.dev..."
    set -a
    source .env.dev
    set +a
elif [ -f ".env.local" ]; then
    echo "[dev-worker] Loading environment from .env.local..."
    set -a
    source .env.local
    set +a
fi

export ENV_MODE="dev"
export AGENT_NAME="${AGENT_NAME:-phone-pal-dev}"
export RT_HEALTH_PORT="${RT_HEALTH_PORT:-8084}"

# Clear any stale worker holding the health port
lsof -ti :"$RT_HEALTH_PORT" | xargs kill -9 2>/dev/null || true


echo "=========================================================="
echo "  LAUNCHING DEDICATED DEV INSTANCE WORKER"
echo "=========================================================="
echo "  Agent Name:    $AGENT_NAME"
echo "  Phone Number:  ${RT_PUBLIC_NUMBER:-(not set)}"
echo "  LiveKit URL:   ${LIVEKIT_URL:-(not set)}"
echo "  Supabase Ref:  ${SUPABASE_PROJECT_REF:-(not set)}"
echo "=========================================================="
echo ""

exec uv run python agent.py dev
