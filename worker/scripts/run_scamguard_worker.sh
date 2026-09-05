#!/usr/bin/env bash
# Scam Guard worker — registers agent "iris-scamguard" on LiveKit CLOUD
# (project iris), where the SIP trunks live. While the guard experiment
# runs, the 888 number (+18887261924, trunk ST_kdUWULwVhbY7) dispatches
# here instead of to Iris staging.
#
# Revert 888 back to the Iris realtime brain:
#   lk sip dispatch delete <scamguard rule id>   # lk sip dispatch list
#   lk sip dispatch create --name iris-realtime-888 --trunks ST_kdUWULwVhbY7 \
#      --individual "phone-" --agent-name iris-realtime
set -euo pipefail
cd "$(dirname "$0")/.."

DEV_JSON="$HOME/.iris/iris-test.json"
export AGENT_NAME=iris-scamguard
export GEMINI_LIVE_VOICE=Aoede
export RT_HEALTH_PORT=8086          # 8082 belongs to the staging Iris worker
# A guard listens to LONG calls — much bigger context window before compression
# than the companion lane (it must recall the scammer's opening 20 min later).
export RT_CTX_TRIGGER_TOKENS=16000
export RT_CTX_TARGET_TOKENS=12000

LK_CFG="$HOME/.livekit/cli-config.yaml"
export LIVEKIT_URL="$(.venv/bin/python -c "import yaml;d=yaml.safe_load(open('$LK_CFG'));p=[x for x in d['projects'] if x['name']=='iris'][0];print(p['url'])")"
export LIVEKIT_API_KEY="$(.venv/bin/python -c "import yaml;d=yaml.safe_load(open('$LK_CFG'));p=[x for x in d['projects'] if x['name']=='iris'][0];print(p['api_key'])")"
export LIVEKIT_API_SECRET="$(.venv/bin/python -c "import yaml;d=yaml.safe_load(open('$LK_CFG'));p=[x for x in d['projects'] if x['name']=='iris'][0];print(p['api_secret'])")"
# Dev DB creds are only for agent.py's read-only boot probe (imported as the
# platform module) — the guard itself never reads or writes any database.
export SUPABASE_URL="$(python3 -c "import json;print(json.load(open('$DEV_JSON'))['url'])")"
export SUPABASE_SERVICE_ROLE_KEY="$(python3 -c "import json;print(json.load(open('$DEV_JSON'))['service_role_key'])")"
export SUPABASE_PROJECT_REF="$(python3 -c "import json;print(json.load(open('$DEV_JSON'))['ref'])")"

echo "[scamguard] agent=$AGENT_NAME voice=$GEMINI_LIVE_VOICE port=$RT_HEALTH_PORT"
exec .venv/bin/python scamguard.py dev
