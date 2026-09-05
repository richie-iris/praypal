#!/usr/bin/env bash
# run_test_worker.sh — Launch dedicated Test instance agent worker

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$ROOT_DIR"

if [ -f ".env.test" ]; then
    echo "[test-worker] Loading environment from .env.test..."
    set -a
    source .env.test
    set +a
elif [ -f ".env.local" ]; then
    echo "[test-worker] Loading environment from .env.local..."
    set -a
    source .env.local
    set +a
fi

export AGENT_NAME="${AGENT_NAME:-phone-pal-test}"
export RT_HEALTH_PORT="${RT_HEALTH_PORT:-8086}"

# Clear any stale worker holding the health port
lsof -ti :"$RT_HEALTH_PORT" | xargs kill -9 2>/dev/null || true


echo "=========================================================="
echo "  LAUNCHING DEDICATED TEST INSTANCE WORKER"
echo "=========================================================="
echo "  Agent Name:    $AGENT_NAME"
echo "  Phone Number:  ${RT_PUBLIC_NUMBER:-(not set)}"
echo "  Health Port:   $RT_HEALTH_PORT"
echo "  Supabase:      ${SUPABASE_URL:-(not set)}"
echo "=========================================================="
echo ""

if command -v uv >/dev/null 2>&1; then
    exec uv run python agent.py dev
elif [ -x "$ROOT_DIR/.venv/bin/python" ]; then
    exec "$ROOT_DIR/.venv/bin/python" agent.py dev
else
    exec python3 agent.py dev
fi
