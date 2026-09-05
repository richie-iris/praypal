#!/usr/bin/env bash
# stand-up-lane.sh — a bare host + this repo + lane.env, to a ringing number.
#
#   cp deploy/provisioning/lane.env.example deploy/provisioning/lane.env
#   $EDITOR deploy/provisioning/lane.env
#   bash deploy/provisioning/stand-up-lane.sh            # show the plan
#   APPLY=1 bash deploy/provisioning/stand-up-lane.sh    # do it
#
# The whole point: everything a lane needs is either in this repository or in
# lane.env. Nothing lives only in a portal, only on a box, or only in someone's
# memory. Losing the machine should cost a rebuild, not the product.
#
# The order matters and is not arbitrary:
#
#   1  host        Docker + the directory layout          provision-box.sh
#   2  config      worker.env / livekit.yaml / sip.yaml   rendered from lane.env
#   3  firewall    before anything listens                firewall-setup.sh
#   4  carrier     Twilio trunk, credentials, number      twilio-setup.sh
#   5  stack       the seven containers                   docker compose up
#   6  routing     inbound trunk + dispatch rule          recreate-routing.sh
#   7  outbound    optional, two switches                 outbound-trunk.sh
#   8  verify      answer a probe without dialling anyone
#
# DRY RUN IS THE DEFAULT and every step below is itself dry-run by default.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "${HERE}/../.." && pwd)"
ENV_FILE="${ENV_FILE:-${HERE}/lane.env}"
APPLY="${APPLY:-0}"

[ -f "$ENV_FILE" ] || { echo "no lane.env — cp lane.env.example lane.env and fill it in" >&2; exit 1; }
set -a; . "$ENV_FILE"; set +a
: "${BOX_HOST:?set BOX_HOST in lane.env}"
: "${AGENT_NAME:?set AGENT_NAME in lane.env}"
: "${NUMBER:?set NUMBER in lane.env}"
TARGET="${TARGET:-/opt/phone-pal}"
step() { printf '\n\033[1m==> %s. %s\033[0m\n' "$1" "$2"; }
run()  { if [ "$APPLY" = "1" ]; then "$@"; else echo "    would run: $*"; fi; }

if [ "$APPLY" != "1" ]; then
  echo "DRY RUN — this prints the plan and changes nothing. APPLY=1 to execute."
fi
echo "    lane=${LANE:-dev}  agent=${AGENT_NAME}  number=${NUMBER}  host=${BOX_HOST}"

# ── 0. Refuse to rebuild over a live call ────────────────────────────────────
if [ "$APPLY" = "1" ]; then
  ACT=$(ssh -o BatchMode=yes "$BOX_HOST" \
        "docker logs --since 3m phone-pal-worker-1 2>&1 | grep -c 'SIP caller' || true" 2>/dev/null || echo 0)
  END=$(ssh -o BatchMode=yes "$BOX_HOST" \
        "docker logs --since 3m phone-pal-worker-1 2>&1 | grep -c '\"event\": \"call.ended\"' || true" 2>/dev/null || echo 0)
  if [ "${ACT:-0}" -gt "${END:-0}" ]; then
    echo "REFUSING: a call is in progress. Wait, or FORCE=1." >&2
    [ "${FORCE:-0}" = "1" ] || exit 1
  fi
fi

step 1 "Host — Docker and the directory layout"
run ssh "$BOX_HOST" "mkdir -p ${TARGET}"
run rsync -az --exclude '.venv/' --exclude '__pycache__/' --exclude '*.pyc' \
      --exclude '.env' --exclude '.env.*' \
      "${REPO}/worker/" "${BOX_HOST}:${TARGET}/worker/"
run rsync -az --exclude 'worker.env' --exclude 'livekit.yaml' --exclude 'sip.yaml' \
      --exclude 'Caddyfile' --exclude 'lane.env' \
      "${REPO}/deploy/" "${BOX_HOST}:${TARGET}/deploy/"
run ssh "$BOX_HOST" "bash ${TARGET}/deploy/provision-box.sh"

step 2 "Config — rendered from lane.env, never committed"
# render-config.sh writes worker.env / livekit.yaml / sip.yaml / Caddyfile on the
# box from lane.env. It is the only thing that ever holds both halves at once.
run bash "${HERE}/render-config.sh"

step "2b" "Database — apply + verify schema migrations (01→24) & RPC readiness"
# check_migrations.py needs the project ref + service key; migrate.py needs the
# ref + Management API token. Both are lane.env's, so a lane that cannot be
# verified is a lane that cannot be stood up — no "skipped" path here (#20).
: "${SUPABASE_URL:?set SUPABASE_URL in lane.env}"
: "${SUPABASE_SERVICE_ROLE_KEY:?set SUPABASE_SERVICE_ROLE_KEY in lane.env}"
: "${SUPABASE_PROJECT_REF:?set SUPABASE_PROJECT_REF in lane.env}"
# Through the worker's locked venv, not a bare python3: both scripts import
# python-dotenv, a worker dependency the host interpreter does not have.
UV_PY=(uv run --frozen --project "${REPO}/worker" python)
# The lane's Supabase values go to the scripts through a private temp file
# (umask 077, removed on exit) and --env-file, so neither script can fall back
# to whatever worker/.env.local or worker/.env happens to hold on THIS machine
# — that fallback is how a dev checkout once verified the wrong project (#20).
# Only the path is ever echoed; the values stay inside the file.
TMPD="${TMPDIR:-/tmp}"
LANE_TMP=$(umask 077 && mktemp "${TMPD%/}/lane-supabase.XXXXXX")
trap 'rm -f "$LANE_TMP"' EXIT
(
  umask 077
  printf '%s=%s\n' \
    SUPABASE_PROJECT_REF "${SUPABASE_PROJECT_REF}" \
    SUPABASE_ACCESS_TOKEN "${SUPABASE_ACCESS_TOKEN:-}" \
    SUPABASE_URL "${SUPABASE_URL}" \
    SUPABASE_SERVICE_ROLE_KEY "${SUPABASE_SERVICE_ROLE_KEY}" > "$LANE_TMP"
)
if [ -n "${SUPABASE_ACCESS_TOKEN:-}" ]; then
  # migrate.py tracks what is applied and refuses checksum drift; sql_push.py
  # only pushes one --file and has no --apply. A failed apply fails the stand-up.
  run "${UV_PY[@]}" "${REPO}/worker/scripts/migrate.py" --apply --env-file "$LANE_TMP" \
    || { echo "FAILED: migrate.py --apply did not complete; the lane's schema is not known-good." >&2; exit 1; }
else
  echo "    no SUPABASE_ACCESS_TOKEN in lane.env — not applying; check_migrations below decides"
fi
# The verify is the gate: a lane whose RPCs are behind the checkout would answer
# calls with tool failures, so it must never proceed to firewall/carrier/stack.
run "${UV_PY[@]}" "${REPO}/worker/scripts/check_migrations.py" --env-file "$LANE_TMP" \
  || { echo "FAILED: check_migrations.py reports the lane BEHIND or unreachable; fix sql/ first." >&2; exit 1; }
rm -f "$LANE_TMP"; trap - EXIT

step 3 "Firewall — before anything listens"
run env APPLY="$APPLY" bash "${HERE}/firewall-setup.sh"

step 4 "Carrier — Twilio trunk, origination, credentials, number, spend alarm"
run env APPLY="$APPLY" bash "${HERE}/twilio-setup.sh"

step 5 "Stack — the seven containers (autoheal has no network: network_mode none)"
run ssh "$BOX_HOST" "cd ${TARGET}/deploy && docker compose up -d"

step 6 "Routing — inbound trunk and dispatch rule"
run ssh "$BOX_HOST" "cd ${TARGET}/deploy && NUMBER='${NUMBER}' AGENT='${AGENT_NAME}' LANE='${LANE}' \
      bash provisioning/recreate-routing.sh"

step 7 "Outbound — optional, and OFF unless you set both switches"
if [ -n "${TWILIO_OUTBOUND_PASSWORD:-}" ]; then
  run env APPLY="$APPLY" bash "${HERE}/outbound-trunk.sh"
else
  echo "    skipped — no TWILIO_OUTBOUND_PASSWORD in lane.env (outbound stays off)"
fi

step 8 "Verify — without dialling a real person"
cat <<VERIFY
    ssh ${BOX_HOST} 'cd ${TARGET}/deploy && docker compose -p phone-pal ps'
    ssh ${BOX_HOST} 'docker logs --tail 30 phone-pal-worker-1'

    A probe room the worker must answer:
      lk dispatch create --agent-name ${AGENT_NAME} --room readiness-probe-1
      lk room participants list readiness-probe-1
    Zero participants means the worker never registered.

    Then record what you actually built, and commit it:
      HOST=${BOX_HOST} LANE=${LANE:-dev} bash deploy/provisioning/capture-lane.sh
      git diff deploy/provisioning/${LANE:-dev}-inventory.md
    A clean diff against the previous capture means the rebuild matches.
VERIFY

if [ "$APPLY" != "1" ]; then
  printf '\n\033[1mNothing was changed.\033[0m Re-run with APPLY=1 when the plan looks right.\n'
fi
