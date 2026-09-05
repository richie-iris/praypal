#!/usr/bin/env bash
# capture-lane.sh — write down what the RUNNING lane actually is.
#
#   HOST=root@173.255.235.130 bash deploy/provisioning/capture-lane.sh
#
# Produces deploy/provisioning/<LANE>-inventory.md: the live trunk ids, dispatch
# rule, agent name, container images and config KEYS. Commit it.
#
# This exists because 606's routing was once known only by reading it off a
# running server. A box that dies takes that knowledge with it, and every id
# below is one a rebuild has to match or the phone does not ring.
#
# It NEVER writes a secret. Config files are reduced to their key names, and the
# LiveKit key pair is fingerprinted, not printed — enough to tell two lanes
# apart, useless to an attacker.
set -euo pipefail

HOST="${HOST:?set HOST, e.g. root@173.255.235.130}"
TARGET="${TARGET:-/opt/phone-pal}"
LANE="${LANE:-dev}"
HERE="$(cd "$(dirname "$0")" && pwd)"
# OUT is overridable so a local capture (tests, a scratch comparison) never
# lands in the tracked provisioning directory by accident.
OUT="${OUT:-${HERE}/${LANE}-inventory.md}"

# CAPTURE_LOCAL=1 runs each command on THIS host instead of over ssh — for a
# capture taken on the box itself, and for the test suite, which feeds a fixture
# livekit.yaml through the same awk the live capture uses.
if [ "${CAPTURE_LOCAL:-0}" = "1" ]; then
  sshq() { bash -c "$*"; }
else
  sshq() { ssh -o BatchMode=yes "$HOST" "$@"; }
fi

echo "==> Reading the live lane at ${HOST}" >&2

LK_YAML="${TARGET}/deploy/livekit.yaml"
# The first NON-comment, non-blank line under `keys:` is the pair. The tracked
# template opens the block with `# <API_KEY>: <API_SECRET>`, so an awk that
# takes the next line verbatim fingerprints the comment and authenticates the
# lk CLI as "#<API_KEY>" — every SIP list below then reads "(unreadable)" and
# the inventory records a lane that does not exist.
KEY_AWK='/^keys:/{while((getline l)>0){if(l ~ /^[ \t]*(#|$)/)continue; gsub(/[ \t]/,"",l); split(l,a,":"); print a[1]; exit}}'
SECRET_AWK='/^keys:/{while((getline l)>0){if(l ~ /^[ \t]*(#|$)/)continue; sub(/^[ \t]*[^:]*:[ \t]*/,"",l); print l; exit}}'
KEY=$(sshq "awk '${KEY_AWK}' ${LK_YAML}" 2>/dev/null || true)
# Fingerprint, never print. A rebuilt lane MUST generate its own pair, so the
# old key identifies nothing worth reconstructing — it only has to be possible
# to tell two lanes apart, and to notice when a pair was reused across lanes.
KEY_FP=$(printf '%s' "${KEY}" | shasum -a 256 2>/dev/null | cut -c1-12 || echo "?")
SECRET_FP=$(sshq "awk '${SECRET_AWK}' ${LK_YAML} | sha256sum | cut -c1-12" 2>/dev/null || true)
CLI="docker run --rm --network host livekit/livekit-cli:latest"
AUTH="--url http://127.0.0.1:7880 --api-key ${KEY} --api-secret \$(awk '${SECRET_AWK}' ${LK_YAML})"

INBOUND=$(sshq "$CLI sip inbound list $AUTH 2>/dev/null" || echo "(unreadable)")
OUTBOUND=$(sshq "$CLI sip outbound list $AUTH 2>/dev/null" || echo "(unreadable)")
DISPATCH=$(sshq "$CLI sip dispatch list $AUTH 2>/dev/null" || echo "(unreadable)")
PS=$(sshq "cd ${TARGET}/deploy && docker compose -p phone-pal ps --format '{{.Name}}\t{{.Image}}\t{{.Status}}'" 2>/dev/null || echo "(unreadable)")
ENVKEYS=$(sshq "grep -oE '^[A-Z_]+' ${TARGET}/deploy/worker.env | sort" 2>/dev/null || echo "(unreadable)")
AGENT=$(sshq "grep -oP '^AGENT_NAME=\K.*' ${TARGET}/deploy/worker.env" 2>/dev/null || true)
NUMBER=$(sshq "grep -oP '^RT_PUBLIC_NUMBER=\K.*' ${TARGET}/deploy/worker.env" 2>/dev/null || true)
TRUNKOUT=$(sshq "grep -oP '^SIP_OUTBOUND_TRUNK_ID=\K.*' ${TARGET}/deploy/worker.env" 2>/dev/null || true)
COMMIT=$(git -C "${HERE}/../.." rev-parse --short HEAD 2>/dev/null || echo unknown)
IP=${HOST#*@}

{
  echo "# ${LANE} lane — captured inventory"
  echo
  echo "Read off the running box, not from notes. Regenerate with:"
  echo
  echo '```bash'
  echo "HOST=${HOST} LANE=${LANE} bash deploy/provisioning/capture-lane.sh"
  echo '```'
  echo
  echo "Repo commit at capture: \`${COMMIT}\`"
  echo
  echo "## Identity"
  echo
  echo "| What | Value |"
  echo "|---|---|"
  echo "| Number | \`${NUMBER:-?}\` |"
  echo "| Agent name | \`${AGENT:-?}\` — must equal the dispatch rule's agent |"
  echo "| Box IP | \`${IP}\` — Twilio routes here BY IP; keep the reservation |"
  echo "| LiveKit API key | fingerprint \`${KEY_FP:-?}\` (never stored) |"
  echo "| LiveKit secret | fingerprint \`${SECRET_FP:-?}\` (never stored) |"
  echo "| Outbound trunk | \`${TRUNKOUT:-?}\` |"
  echo
  echo "## LiveKit SIP state"
  echo
  echo '### Inbound trunks'; echo '```'; echo "$INBOUND"; echo '```'
  echo '### Dispatch rules'; echo '```'; echo "$DISPATCH"; echo '```'
  echo '### Outbound trunks'; echo '```'; echo "$OUTBOUND"; echo '```'
  echo "## Containers"; echo '```'; echo "$PS"; echo '```'
  echo "## worker.env — keys present (values deliberately absent)"
  echo
  echo "A rebuilt lane needs every one of these set. The values come from"
  echo "\`lane.env\`, which is not in this repository."
  echo
  echo '```'; echo "$ENVKEYS"; echo '```'
} > "$OUT"

echo "==> Wrote ${OUT}" >&2
grep -c '' "$OUT" | xargs echo "    lines:" >&2
echo "    Commit it — this is the target state a rebuild has to match." >&2
