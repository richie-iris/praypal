#!/usr/bin/env bash
# recreate-routing.sh — create this lane's inbound SIP trunk and dispatch rule.
#
# Run ON the box, from the deploy/ directory's sibling:
#   bash provisioning/recreate-routing.sh
#
# Reads the LiveKit key pair from ../livekit.yaml. Creates nothing that already
# exists and DELETES NOTHING, ever — see 606-call-path.md for why that matters.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
LK_YAML="${LK_YAML:-${HERE}/../livekit.yaml}"
URL="${LK_URL:-http://127.0.0.1:7880}"
CLI="docker run --rm --network host livekit/livekit-cli:latest"

NUMBER="${NUMBER:-+19736069515}"
AGENT="${AGENT:-phone-pal-dev}"
LANE="${LANE:-phone-pal-dev-606}"
ROOM_PREFIX="${ROOM_PREFIX:-phone-}"
# Twilio SIP signaling egress, every region (a trunk fails over across them).
# Anything else must not be able to present a call. Same list as
# firewall-setup.sh; change both or neither.
TWILIO_CIDRS='["54.172.60.0/30","54.244.51.0/30","54.171.127.192/30","35.156.191.128/30","54.65.63.192/30","54.169.127.128/30","54.252.254.64/30","177.71.206.192/30"]'

[ -f "$LK_YAML" ] || { echo "no livekit.yaml at $LK_YAML" >&2; exit 1; }
# One getline used to be enough. The rendered keys: block opens with a comment
# — "# <API_KEY>: <API_SECRET>" — so a single getline read THAT, and the pair
# became key "#<API_KEY>" and secret "<API_SECRET>". Both non-empty, so the
# guard below passed, and the only symptom was LiveKit answering every call
# here with 401 Unauthorized (2026-09-03, standing up trial-pal).
# render-config.sh already skips comments when it writes this block; this reads
# it back the same way. Comments and blank lines are skipped, a dedent ends the
# block, and the split is on the FIRST colon so the pair comes from one line.
read -r KEY SECRET <<<"$(awk '
  /^keys:/ { inblock = 1; next }
  inblock {
    if ($0 ~ /^[ \t]*#/ || $0 ~ /^[ \t]*$/) next   # comment or blank
    if ($0 !~ /^[ \t]/) exit                        # dedented: block is over
    line = $0
    sub(/^[ \t]+/, "", line)
    i = index(line, ":")
    if (i == 0) exit
    k = substr(line, 1, i - 1); v = substr(line, i + 1)
    gsub(/^[ \t]+|[ \t]+$/, "", k); gsub(/^[ \t]+|[ \t]+$/, "", v)
    print k, v
    exit
  }' "$LK_YAML")"
case "$KEY" in
  ""|"#"*) echo "could not read a key pair from $LK_YAML (got ${KEY:-empty})" >&2; exit 1 ;;
esac
[ -n "$SECRET" ] || { echo "no secret for key $KEY in $LK_YAML" >&2; exit 1; }
AUTH=(--url "$URL" --api-key "$KEY" --api-secret "$SECRET")

echo "==> Checking for an existing trunk claiming ${NUMBER}"
if $CLI sip inbound list "${AUTH[@]}" 2>/dev/null | grep -q -- "$NUMBER"; then
  echo "    A trunk already claims ${NUMBER}. Refusing to touch it."
  echo "    Inspect it, and delete it BY ID yourself if that is really what you want:"
  echo "      lk sip inbound list"
  exit 1
fi

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
cat > "$TMP/trunk.json" <<JSON
{ "trunk": {
    "name": "${LANE}",
    "numbers": ["${NUMBER}"],
    "allowed_addresses": ${TWILIO_CIDRS}
} }
JSON

echo "==> Creating inbound trunk"
docker run --rm --network host -v "$TMP:/w" livekit/livekit-cli:latest \
  sip inbound create "${AUTH[@]}" /w/trunk.json
TRUNK_ID=$($CLI sip inbound list "${AUTH[@]}" 2>/dev/null | awk -v n="$NUMBER" '$0 ~ n {print $2; exit}')
echo "    trunk: ${TRUNK_ID}"

# Wrapped in "dispatch_rule", exactly as trunk.json is wrapped in "trunk".
# The flat form — name/trunk_ids/rule/room_config at the top level — is the
# deprecated half of CreateSIPDispatchRuleRequest, and lk 2.18.5 reads the
# nested one: the flat form was rejected with a bare "missing rule" while a
# top-level "rule" key was sitting right there (2026-09-03, trial-pal).
# Field names are camelCase to match what `sip dispatch list --json` returns,
# so what this writes and what the server reports back are the same shape.
cat > "$TMP/rule.json" <<JSON
{ "dispatch_rule": {
    "name": "${LANE}",
    "trunkIds": ["${TRUNK_ID}"],
    "rule": { "dispatchRuleIndividual": { "roomPrefix": "${ROOM_PREFIX}" } },
    "roomConfig": { "agents": [ { "agentName": "${AGENT}" } ] }
} }
JSON

echo "==> Creating dispatch rule -> agent ${AGENT}"
docker run --rm --network host -v "$TMP:/w" livekit/livekit-cli:latest \
  sip dispatch create "${AUTH[@]}" /w/rule.json

cat <<NEXT

==> Done. Two things this script does NOT do, on purpose:

  1. The OUTBOUND trunk. It carries the Twilio termination password, which
     does not belong in a repo. outbound-trunk.sh reads it from lane.env; put
     the id it prints in worker.env as SIP_OUTBOUND_TRUNK_ID.

  2. Anything on the Twilio side: the Elastic SIP Trunk whose origination
     points at this box's IP, the credential list, the number, and the daily
     spend alarm. twilio-setup.sh does all of it; see 606-call-path.md.

Verify without dialling a real person:
     lk dispatch create --agent-name ${AGENT} --room readiness-probe-1
     lk room participants list readiness-probe-1
NEXT
