#!/usr/bin/env bash
# call-logs.sh — one call, every log source we have, in time order.
#
#   HOST=root@173.255.235.130 bash deploy/call-logs.sh                 # last call
#   HOST=root@... bash deploy/call-logs.sh phone-_+1917_A5tmFyzKSUgN   # a given room
#   HOST=root@... SINCE=2h QUIET=0 bash deploy/call-logs.sh            # keep ICE noise
#
# The worker, the media server and the SIP bridge each hold one piece of a call
# and none holds the whole thing. "She never spoke" looks identical in the worker
# log whether the audio was never generated, generated and dropped, or generated
# and never carried — and those are three different bugs. Reading one source at a
# time is how an afternoon disappears.
#
# NOT here, because it does not exist locally:
#   Twilio  — carrier-side SIP, hangup cause, DTMF. Only the console/API has it
#             (Monitor -> Logs -> Calls). What the SIP bridge sees of Twilio
#             (its IP, codec, sipCallID) IS here.
#   Gemini  — set LK_GOOGLE_DEBUG=1 in worker.env and the plugin logs the full
#             request/response exchange at DEBUG. Off by default: it is enormous
#             and it contains everything the caller said.
set -euo pipefail

HOST="${HOST:?set HOST, e.g. root@173.255.235.130}"
SINCE="${SINCE:-30m}"
QUIET="${QUIET:-1}"          # 1 drops ICE candidate churn, which is constant and rarely the answer
ROOM="${1:-}"

sshq() { ssh -o BatchMode=yes "$HOST" "$@"; }

if [ -z "$ROOM" ]; then
  ROOM=$(sshq "docker logs --since $SINCE phone-pal-worker-1 2>&1" \
         | grep -oE 'room=(phone-|outbound-)[^ ]+' | tail -1 | cut -d= -f2) || true
  [ -n "$ROOM" ] || { echo "no call found in the last $SINCE" >&2; exit 1; }
  echo "==> most recent call: $ROOM" >&2
fi

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
sshq "docker logs --since $SINCE --timestamps phone-pal-worker-1 2>&1"  > "$TMP/worker"  || true
sshq "docker logs --since $SINCE --timestamps phone-pal-livekit-1 2>&1" > "$TMP/livekit" || true
sshq "docker logs --since $SINCE --timestamps phone-pal-sip-1 2>&1"     > "$TMP/sip"     || true

# The worker names the room on its job line and then stops; everything after is
# correlated by call_id. Pull that id out and match on either.
CID=$(grep -F "$ROOM" "$TMP/worker" | grep -oE '"call_id": "[^"]+"' | head -1 | cut -d'"' -f4 || true)
# SIP names the room only on its join line. Its own callID threads the rest.
SCL=$(grep -F "$ROOM" "$TMP/sip" | grep -oE '"callID": "[^"]+"' | head -1 | cut -d'"' -f4 || true)

echo "==> room=$ROOM  worker_call_id=${CID:-none}  sip_call_id=${SCL:-none}" >&2

{
  grep -F "$ROOM" "$TMP/worker" | sed 's/^/WORKER  /'
  [ -n "$CID" ] && grep -F "$CID" "$TMP/worker" | sed 's/^/WORKER  /'
  grep -F "$ROOM" "$TMP/livekit" | sed 's/^/LIVEKIT /'
  [ -n "$SCL" ] && grep -F "$SCL" "$TMP/sip" | sed 's/^/SIP     /'
  true
} | sort -k2,2 | awk '!seen[$0]++' | {
  if [ "$QUIET" = "1" ]; then
    grep -vE 'pion\.ice|candidatepair|Failed to ping|signaling state changed|network is unreachable'
  else
    cat
  fi
}
