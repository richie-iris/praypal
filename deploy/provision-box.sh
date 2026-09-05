#!/usr/bin/env bash
# provision-box.sh — a bare Ubuntu host to a running Phone-Pal lane.
#
# Written against Ubuntu 24.04 LTS on x86_64, which is what the dev box runs.
# Idempotent: safe to re-run.
#
#   scp -r deploy worker root@<host>:/opt/phone-pal-src
#   ssh root@<host> 'bash /opt/phone-pal-src/deploy/provision-box.sh'
#
# It does NOT create secrets and does NOT start anything. It installs Docker and
# lays out the directory; you then fill in worker.env, livekit.yaml, sip.yaml and
# Caddyfile from their templates and run `docker compose up -d`.
set -euo pipefail

TARGET=${TARGET:-/opt/phone-pal}
# Config lives beside the compose file, mirroring the repo: TARGET/deploy/
CONF="${TARGET}/deploy"

echo "==> Docker"
if ! command -v docker >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
else
  echo "    already installed: $(docker --version)"
fi
systemctl enable --now docker

echo "==> Layout at ${TARGET} (mirrors the repo: worker/ + deploy/)"
mkdir -p "${TARGET}/worker" "${CONF}"

echo "==> Config files"
# Never overwrite a filled-in config on a re-run.
for f in worker.env livekit.yaml sip.yaml; do
  src="$(dirname "$0")/${f}.example"
  if [ -f "${CONF}/${f}" ]; then
    echo "    ${f} exists, leaving it"
  else
    cp "${src}" "${CONF}/${f}"
    echo "    ${f} <- ${f}.example  (MUST be filled in)"
  fi
done
if [ ! -f "${CONF}/Caddyfile" ]; then
  echo "    Caddyfile not created — render it with the host you are using:"
  echo "      sed 's/{{HOST}}/<host>/' Caddyfile.template > ${CONF}/Caddyfile"
fi
chmod 600 "${CONF}/worker.env" "${CONF}/livekit.yaml" "${CONF}/sip.yaml" 2>/dev/null || true

cat <<'NEXT'

==> Provisioned. Remaining steps, in order:

  1. Generate a LiveKit key pair FOR THIS BOX ONLY:
         docker run --rm livekit/livekit-server generate-keys
     Put the same pair in livekit.yaml, sip.yaml, and worker.env.
     Never reuse another lane's pair — one shared LiveKit namespace is how a
     dispatch rule got silently overwritten before.

  2. Fill in worker.env. Leave RT_SCHEDULER_ENABLED empty until you mean it:
     set to 1, the scheduler places REAL outbound calls with no human review.

  3. Open the firewall: 5060/udp and 10000-20000/udp restricted to the carrier's
     SIP egress ranges, plus 22 from your address. Twilio egress: signaling
     from 54.172.60.0/30, 54.244.51.0/30 and the other regional /30s listed in
     provisioning/firewall-setup.sh; media from 168.86.128.0/18.

  4. cd '${TARGET}/deploy' && docker compose up -d

  5. Verify without dialling anyone:
         lk dispatch create --agent-name <AGENT_NAME> --room readiness-probe-1
         lk room participants list readiness-probe-1
     Zero participants means the worker never registered.

NEXT
