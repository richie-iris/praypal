#!/usr/bin/env bash
# outbound-trunk.sh — the LiveKit outbound trunk, so reminder calls can dial out.
#
#   bash deploy/provisioning/outbound-trunk.sh           # inspect only
#   APPLY=1 bash deploy/provisioning/outbound-trunk.sh   # create it
#
# recreate-routing.sh deliberately left this out, because it carries a carrier
# credential password and "does not belong in a repo". The password still does
# not: it is read from lane.env, which is not in the repo, and is never
# printed. What belongs in the repo is the SHAPE of the trunk, so a rebuild
# does not have to remember it.
#
# The trunk dials <TWILIO_TRUNK_DOMAIN>.pstn.twilio.com and authenticates with
# the credential list twilio-setup.sh attached to the Twilio trunk. The
# credential pair MUST be the same one in both places, or every dial is
# challenged and refused with nothing in this repo's logs to say why.
#
# Creating this trunk is only ONE of the two switches. The worker also needs
# RT_SCHEDULER_ENABLED=1 before any phone rings, and that stays off by default
# on purpose — on, the scheduler places real calls with no human review.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${ENV_FILE:-${HERE}/lane.env}"
APPLY="${APPLY:-0}"
[ -f "$ENV_FILE" ] || { echo "no lane.env at $ENV_FILE (copy lane.env.example)" >&2; exit 1; }
set -a; . "$ENV_FILE"; set +a
: "${NUMBER:?set NUMBER in lane.env}"
: "${LIVEKIT_API_KEY:?set LIVEKIT_API_KEY in lane.env}"
: "${LIVEKIT_API_SECRET:?set LIVEKIT_API_SECRET in lane.env}"

NAME="phone-pal-${LANE:-dev}-outbound"
URL="${LK_URL:-http://127.0.0.1:7880}"
CLI="docker run --rm --network host livekit/livekit-cli:latest"
AUTH=(--url "$URL" --api-key "$LIVEKIT_API_KEY" --api-secret "$LIVEKIT_API_SECRET")

[ "$APPLY" = "1" ] || echo "DRY RUN — nothing will be created. Re-run with APPLY=1 to act."

echo; echo "==> Outbound trunk '${NAME}'"
EXISTING=$($CLI sip outbound list "${AUTH[@]}" 2>/dev/null || true)
if echo "$EXISTING" | grep -q -- "$NAME"; then
  ID=$(echo "$EXISTING" | awk -v n="$NAME" '$0 ~ n {print $2; exit}')
  echo "    exists: ${ID}"
  echo "    Refusing to touch it. Put this in worker.env if it is not there:"
  echo "      SIP_OUTBOUND_TRUNK_ID=${ID}"
  exit 0
fi

if [ -z "${TWILIO_TRUNK_DOMAIN:-}" ]; then
  echo "    REFUSING: TWILIO_TRUNK_DOMAIN is not set in lane.env." >&2
  echo "    The trunk has nowhere to send a call. Set it (the label before" >&2
  echo "    .pstn.twilio.com), run twilio-setup.sh, then come back here." >&2
  exit 1
fi
if [ -z "${TWILIO_OUTBOUND_USER:-}" ] || [ -z "${TWILIO_OUTBOUND_PASSWORD:-}" ]; then
  echo "    REFUSING: TWILIO_OUTBOUND_USER/PASSWORD are not set in lane.env." >&2
  echo "    A trunk without them would be created and then fail at dial time," >&2
  echo "    which is a worse outcome than not having one." >&2
  exit 1
fi
ADDRESS="${TWILIO_TRUNK_DOMAIN}.pstn.twilio.com"

if [ "$APPLY" != "1" ]; then
  echo "    MISSING — would create: address ${ADDRESS}, number ${NUMBER},"
  echo "              auth user ${TWILIO_OUTBOUND_USER} (password from lane.env)"
  exit 0
fi

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
umask 077
jq -nc --arg n "$NAME" --arg num "$NUMBER" --arg a "$ADDRESS" \
       --arg u "$TWILIO_OUTBOUND_USER" --arg p "$TWILIO_OUTBOUND_PASSWORD" \
  '{trunk:{name:$n, address:$a, numbers:[$num],
           auth_username:$u, auth_password:$p}}' > "$TMP/trunk.json"

docker run --rm --network host -v "$TMP:/w" livekit/livekit-cli:latest \
  sip outbound create "${AUTH[@]}" /w/trunk.json >/dev/null
ID=$($CLI sip outbound list "${AUTH[@]}" 2>/dev/null | awk -v n="$NAME" '$0 ~ n {print $2; exit}')
echo "    created: ${ID}"
cat <<NEXT

    Add to worker.env, then restart the worker:
      SIP_OUTBOUND_TRUNK_ID=${ID}

    The phone still will not ring outbound until RT_SCHEDULER_ENABLED=1 as well.
    Two switches, not one. And every unattended dial first asks Twilio what
    today has cost (TWILIO_DAILY_SPEND_USD in worker.env) — see rt_carrier.py.
NEXT
