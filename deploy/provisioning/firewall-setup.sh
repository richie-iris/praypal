#!/usr/bin/env bash
# firewall-setup.sh — the Linode cloud firewall, which IS the whole firewall.
#
#   bash deploy/provisioning/firewall-setup.sh          # inspect only (default)
#   APPLY=1 bash deploy/provisioning/firewall-setup.sh  # create it
#
# There is no ufw on the host. If this is not attached and correct, either the
# box is open to the internet or the phone does not ring, and both are quiet.
#
# The two media ranges are NOT interchangeable and both must be open:
#   10000-20000/udp  the SIP leg   (sip.yaml)
#   50000-60000/udp  LiveKit WebRTC (livekit.yaml)
# With only one open, calls connect and then carry no audio — which reads as a
# model problem for about an hour.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${ENV_FILE:-${HERE}/lane.env}"
APPLY="${APPLY:-0}"
[ -f "$ENV_FILE" ] || { echo "no lane.env at $ENV_FILE (copy lane.env.example)" >&2; exit 1; }
set -a; . "$ENV_FILE"; set +a
: "${LINODE_TOKEN:?set LINODE_TOKEN in lane.env}"
: "${LINODE_INSTANCE_ID:?set LINODE_INSTANCE_ID in lane.env}"
command -v jq >/dev/null || { echo "jq required" >&2; exit 1; }

FW_NAME="${FIREWALL_NAME:-phone-pal-${LANE:-dev}}"
API="https://api.linode.com/v4"
# Twilio Elastic SIP Trunking egress (docs: sip-trunking/ip-addresses, read
# 2026-09-02). Signaling comes from one /30 per region and Twilio says to allow
# ALL of them — a trunk fails over across regions; media comes from one global
# /18 on 10000-60000/udp, of which sip.yaml uses 10000-20000. Anything else
# must not be able to present a call.
TWILIO_SIGNALING='["54.172.60.0/30","54.244.51.0/30","54.171.127.192/30","35.156.191.128/30","54.65.63.192/30","54.169.127.128/30","54.252.254.64/30","177.71.206.192/30"]'
TWILIO_MEDIA='["168.86.128.0/18"]'
SSH_CIDRS="${SSH_ALLOW_CIDRS:-}"

lin() { local m="$1" p="$2" d="${3:-}"
  if [ -n "$d" ]; then curl -sS -X "$m" "${API}${p}" -H "Authorization: Bearer ${LINODE_TOKEN}" \
       -H "Content-Type: application/json" -d "$d"
  else curl -sS -X "$m" "${API}${p}" -H "Authorization: Bearer ${LINODE_TOKEN}"; fi; }

[ "$APPLY" = "1" ] || echo "DRY RUN — nothing will be created. Re-run with APPLY=1 to act."

echo; echo "==> Firewall '${FW_NAME}'"
LIST=$(lin GET "/networking/firewalls")
if echo "$LIST" | jq -e '.errors' >/dev/null 2>&1; then
  echo "    token rejected — check LINODE_TOKEN in lane.env" >&2; exit 1; fi
FW_ID=$(echo "$LIST" | jq -r --arg n "$FW_NAME" '.data[]? | select(.label==$n) | .id' | head -1)

if [ -n "$FW_ID" ]; then
  echo "    exists: ${FW_ID}"
  echo "    inbound policy: $(echo "$LIST" | jq -r --arg n "$FW_NAME" '.data[]|select(.label==$n)|.rules.inbound_policy')"
  echo "    attached to:    $(echo "$LIST" | jq -r --arg n "$FW_NAME" '.data[]|select(.label==$n)|.entities|map(.id)|join(",")')"
  echo "    rules:"
  echo "$LIST" | jq -r --arg n "$FW_NAME" '.data[]|select(.label==$n)|.rules.inbound[]? |
        "      \(.action)  \(.protocol)  \(.ports // "-")  \(.addresses.ipv4|join(","))"'
  echo
  echo "    Not modified. Compare against the table in 606-call-path.md; edit by"
  echo "    hand if it has drifted. This script does not rewrite a live firewall."
  exit 0
fi

if [ -z "$SSH_CIDRS" ]; then
  echo "    REFUSING: SSH_ALLOW_CIDRS is empty in lane.env." >&2
  echo "    Creating this firewall with 22 open to 0.0.0.0/0 is not a default" >&2
  echo "    anyone should get by leaving a field blank. Set your address." >&2
  exit 1
fi
SSH_JSON=$(printf '%s' "$SSH_CIDRS" | jq -Rc 'split(",")|map(gsub("^\\s+|\\s+$";""))|map(select(length>0))')

RULES=$(jq -nc --argjson sig "$TWILIO_SIGNALING" --argjson media "$TWILIO_MEDIA" --argjson ssh "$SSH_JSON" '{
  inbound_policy:"DROP", outbound_policy:"ACCEPT", outbound:[],
  inbound:[
    {label:"ssh",         action:"ACCEPT", protocol:"TCP", ports:"22",           addresses:{ipv4:$ssh}},
    {label:"http-acme",   action:"ACCEPT", protocol:"TCP", ports:"80",           addresses:{ipv4:["0.0.0.0/0"]}},
    {label:"https-caddy", action:"ACCEPT", protocol:"TCP", ports:"443",          addresses:{ipv4:["0.0.0.0/0"]}},
    {label:"sip-tcp",     action:"ACCEPT", protocol:"TCP", ports:"5060",         addresses:{ipv4:$sig}},
    {label:"sip-udp",     action:"ACCEPT", protocol:"UDP", ports:"5060",         addresses:{ipv4:$sig}},
    {label:"sip-media",   action:"ACCEPT", protocol:"UDP", ports:"10000-20000",  addresses:{ipv4:$media}},
    {label:"webrtc-media",action:"ACCEPT", protocol:"UDP", ports:"50000-60000",  addresses:{ipv4:["0.0.0.0/0"]}}
  ]}')

if [ "$APPLY" != "1" ]; then
  echo "    MISSING — would create it, DROP by default, attached to ${LINODE_INSTANCE_ID}:"
  echo "$RULES" | jq -r '.inbound[] | "      \(.action)  \(.protocol)  \(.ports)  \(.addresses.ipv4|join(","))"'
  exit 0
fi

BODY=$(jq -nc --arg l "$FW_NAME" --argjson r "$RULES" --argjson id "$LINODE_INSTANCE_ID" \
  '{label:$l, rules:$r, devices:{linodes:[$id]}}')
NEW=$(lin POST "/networking/firewalls" "$BODY")
FW_ID=$(echo "$NEW" | jq -r '.id // empty')
[ -n "$FW_ID" ] || { echo "    create failed:"; echo "$NEW" | jq -r '.errors[]?.reason'; exit 1; }
echo "    created: ${FW_ID}, attached to linode ${LINODE_INSTANCE_ID}"
