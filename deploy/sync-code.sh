#!/usr/bin/env bash
# sync-code.sh — put this checkout onto a box.
#
#   HOST=root@173.255.235.130 bash deploy/sync-code.sh
#
# The host is laid out to MIRROR this repo:
#
#   /opt/phone-pal/worker/    <- worker/   (code)
#   /opt/phone-pal/deploy/    <- deploy/   (compose + this lane's config)
#
# so the compose file's `build: ../worker` means the same thing in both places
# and never needs rewriting per host. The compose project name is pinned to
# "phone-pal" inside the file, so container names do not depend on the path.
#
# Deliberately manual. There is no CI deploy: a rebuild drops whatever call is in
# flight, so taking new code live should be something a person decides. Nothing
# here restarts a container — the restart command is printed at the end.
set -euo pipefail

HOST="${HOST:?set HOST, e.g. root@173.255.235.130}"
TARGET="${TARGET:-/opt/phone-pal}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"

# Refuse to sync while someone is on the phone.
#
# A rebuild drops the call in progress. This was checked by hand once and the
# check itself lied: it grepped for a live caller and then printed "(blank =
# clear)" from an unconditional echo, so the reassurance appeared underneath the
# evidence contradicting it. A real caller was cut off mid-conversation, and
# redialled forty seconds later.
#
# So the check exits non-zero instead of printing anything reassuring. Set
# FORCE=1 to override, which is a thing you should have to type on purpose.
if [ "${FORCE:-0}" != "1" ]; then
  ACTIVE=$(ssh -o BatchMode=yes "$HOST" \
    "docker logs --since 3m phone-pal-worker-1 2>&1 | grep -c 'SIP caller' || true" 2>/dev/null || echo 0)
  ENDED=$(ssh -o BatchMode=yes "$HOST" \
    "docker logs --since 3m phone-pal-worker-1 2>&1 | grep -c '\"event\": \"call.ended\"' || true" 2>/dev/null || echo 0)
  if [ "${ACTIVE:-0}" -gt "${ENDED:-0}" ]; then
    echo "REFUSING: a call started in the last 3 minutes and has not ended." >&2
    echo "  calls started: $ACTIVE   calls ended: $ENDED" >&2
    echo "  Rebuilding now would drop it. Wait, or re-run with FORCE=1." >&2
    exit 1
  fi
  echo "==> no active call (started=$ACTIVE ended=$ENDED)"
fi

echo "==> Syncing worker/ -> ${HOST}:${TARGET}/worker"
# --delete so a module deleted here is deleted there. Excludes keep local venvs,
# caches and any real .env from ever travelling.
rsync -az --delete \
  --exclude '.venv/' --exclude '__pycache__/' --exclude '*.pyc' \
  --exclude '.env' --exclude '.env.*' \
  --exclude 'harness/.env.harness' --exclude 'harness/*_test_results.json' \
  "${HERE}/worker/" "${HOST}:${TARGET}/worker/"

echo "==> Syncing deploy/ -> ${HOST}:${TARGET}/deploy"
# --delete is NOT used here, and the config files are excluded: worker.env,
# livekit.yaml, sip.yaml and Caddyfile live on the box and hold its credentials.
# Overwriting them from a template would take the lane down.
rsync -az \
  --exclude 'worker.env' --exclude 'livekit.yaml' --exclude 'sip.yaml' \
  --exclude 'Caddyfile' \
  "${HERE}/deploy/" "${HOST}:${TARGET}/deploy/"

cat <<NEXT

==> Synced. Nothing is running the new code yet. To take it live:

    ssh ${HOST} 'cd ${TARGET}/deploy && docker compose build worker scheduler \\
                 && docker compose up -d --no-deps worker scheduler'

    A rebuild drops any call in flight. Check first:
      ssh ${HOST} 'docker logs --since 5m phone-pal-worker-1 | grep -i "SIP caller"'

    Then confirm it came back:
      ssh ${HOST} 'docker compose -p phone-pal ps; docker logs --tail 20 phone-pal-worker-1'

NEXT
