#!/usr/bin/env bash
# twilio-setup.sh — the carrier half of a lane, from lane.env.
#
#   bash deploy/provisioning/twilio-setup.sh            # inspect only (default)
#   APPLY=1 bash deploy/provisioning/twilio-setup.sh    # create what is missing
#
# Twilio is the only carrier since 2026-09-02 (ADR 0006): voice through an
# Elastic SIP Trunk into the LiveKit SIP bridge on this box, texts through the
# same account. This replaced telnyx-setup.sh, and keeps its rules: it reads
# EVERY value from lane.env and prints none of them; DRY RUN is the default;
# it NEVER deletes and never edits an object it did not create. If something is
# there under the right name with the wrong settings, it says so and stops —
# the same refusal recreate-routing.sh makes, for the same reason.
#
# What Twilio cannot do that Telnyx did: refuse an outbound call once a daily
# spend cap is hit. A Twilio usage trigger only calls a webhook. So the cap
# lives in the worker (rt_carrier.py asks the account what today has cost
# before every unattended dial), and the trigger created below is the alarm
# (/twilio/usage), not the brake.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${ENV_FILE:-${HERE}/lane.env}"
APPLY="${APPLY:-0}"

[ -f "$ENV_FILE" ] || { echo "no lane.env at $ENV_FILE (copy lane.env.example)" >&2; exit 1; }
set -a; . "$ENV_FILE"; set +a

: "${TWILIO_ACCOUNT_SID:?set TWILIO_ACCOUNT_SID in lane.env}"
: "${TWILIO_AUTH_TOKEN:?set TWILIO_AUTH_TOKEN in lane.env}"
: "${NUMBER:?set NUMBER in lane.env}"
: "${BOX_IP:?set BOX_IP in lane.env}"
TRUNK_NAME="${TWILIO_TRUNK_NAME:-phone-pal-${LANE:-dev}}"
TRUNK_DOMAIN="${TWILIO_TRUNK_DOMAIN:-}"
CRED_LIST_NAME="phone-pal-${LANE:-dev}-outbound"
CAP="${TWILIO_DAILY_SPEND_USD:-25}"
TRUNKING="https://trunking.twilio.com/v1"
API="https://api.twilio.com/2010-04-01/Accounts/${TWILIO_ACCOUNT_SID}"
PUBLIC="${PUBLIC_HOSTNAME:-}"

say()   { printf '    %s\n' "$*"; }
head1() { printf '\n==> %s\n' "$*"; }
# tw METHOD URL [form pairs...] — Basic auth from lane.env, never echoed.
tw() {
  local m="$1" u="$2"; shift 2
  local args=()
  for kv in "$@"; do args+=(--data-urlencode "$kv"); done
  if [ "$m" = "GET" ]; then
    curl -sS -u "${TWILIO_ACCOUNT_SID}:${TWILIO_AUTH_TOKEN}" "$u"
  else
    curl -sS -u "${TWILIO_ACCOUNT_SID}:${TWILIO_AUTH_TOKEN}" -X "$m" "$u" "${args[@]}"
  fi
}
err_of() { jq -r '.message // .detail // empty' 2>/dev/null; }
command -v jq >/dev/null || { echo "jq required" >&2; exit 1; }

if [ "$APPLY" != "1" ]; then
  echo "DRY RUN — nothing will be created. Re-run with APPLY=1 to act."
fi

# ── 1. The account answers at all ────────────────────────────────────────────
head1 "Twilio account"
ACCT=$(tw GET "${API}.json" || true)
STATUS=$(echo "$ACCT" | jq -r '.status // empty' 2>/dev/null || true)
if [ "$STATUS" != "active" ]; then
  echo "    the credentials were rejected or the account is not active — check" >&2
  echo "    TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN in lane.env ($(echo "$ACCT" | err_of))" >&2
  exit 1
fi
say "credentials accepted"

# ── 2. The trunk ─────────────────────────────────────────────────────────────
# One trunk per lane. Its domain is what the LiveKit outbound trunk dials
# (<domain>.pstn.twilio.com) and it is unique across all of Twilio, so it is a
# lane.env value, not something generated here.
head1 "Elastic SIP Trunk '${TRUNK_NAME}'"
TRUNKS=$(tw GET "${TRUNKING}/Trunks?PageSize=100")
TRUNK_SID=$(echo "$TRUNKS" | jq -r --arg n "$TRUNK_NAME" '.trunks[]? | select(.friendly_name==$n) | .sid' | head -1)
if [ -n "$TRUNK_SID" ]; then
  HAVE_DOMAIN=$(echo "$TRUNKS" | jq -r --arg n "$TRUNK_NAME" '.trunks[] | select(.friendly_name==$n) | .domain_name // empty')
  say "exists: ${TRUNK_SID}  domain=${HAVE_DOMAIN:-none}"
  if [ -n "$TRUNK_DOMAIN" ] && [ "$HAVE_DOMAIN" != "${TRUNK_DOMAIN}.pstn.twilio.com" ]; then
    echo "    REFUSING: the trunk's domain is '${HAVE_DOMAIN}', lane.env says" >&2
    echo "             TWILIO_TRUNK_DOMAIN=${TRUNK_DOMAIN}. Fix one of them by hand." >&2
    exit 1
  fi
elif [ "$APPLY" = "1" ]; then
  if [ -n "$TRUNK_DOMAIN" ]; then
    NEW=$(tw POST "${TRUNKING}/Trunks" "FriendlyName=${TRUNK_NAME}" "DomainName=${TRUNK_DOMAIN}.pstn.twilio.com")
  else
    NEW=$(tw POST "${TRUNKING}/Trunks" "FriendlyName=${TRUNK_NAME}")
  fi
  TRUNK_SID=$(echo "$NEW" | jq -r '.sid // empty')
  [ -n "$TRUNK_SID" ] || { echo "    create failed: $(echo "$NEW" | err_of)"; exit 1; }
  say "created: ${TRUNK_SID}${TRUNK_DOMAIN:+  domain=${TRUNK_DOMAIN}.pstn.twilio.com}"
else
  say "MISSING — would create it${TRUNK_DOMAIN:+ with domain ${TRUNK_DOMAIN}.pstn.twilio.com}"
  [ -n "$TRUNK_DOMAIN" ] || say "(no TWILIO_TRUNK_DOMAIN in lane.env: inbound only; outbound-trunk.sh will refuse)"
fi

# ── 3. Origination: the trunk sends inbound calls to THIS box ────────────────
# Twilio routes to the box by IP, exactly as Telnyx did. That is why the
# Linode address is reserved and why a rebuild that changes it breaks inbound
# with no error anywhere.
ORIG_URI="sip:${BOX_IP}:5060"
head1 "Origination -> ${ORIG_URI}"
if [ -n "$TRUNK_SID" ]; then
  ORIGS=$(tw GET "${TRUNKING}/Trunks/${TRUNK_SID}/OriginationUrls?PageSize=100")
  ORIG_SID=$(echo "$ORIGS" | jq -r --arg u "$ORIG_URI" '.origination_urls[]? | select(.sip_url==$u) | .sid' | head -1)
  OTHERS=$(echo "$ORIGS" | jq -r --arg u "$ORIG_URI" '[.origination_urls[]? | select(.sip_url!=$u) | .sip_url] | join(", ")')
  if [ -n "$ORIG_SID" ]; then
    say "exists: ${ORIG_SID}"
  elif [ "$APPLY" = "1" ]; then
    NEW=$(tw POST "${TRUNKING}/Trunks/${TRUNK_SID}/OriginationUrls" \
          "FriendlyName=phone-pal-${LANE:-dev}-linode" "SipUrl=${ORIG_URI}" \
          "Priority=10" "Weight=10" "Enabled=true")
    ORIG_SID=$(echo "$NEW" | jq -r '.sid // empty')
    [ -n "$ORIG_SID" ] || { echo "    create failed: $(echo "$NEW" | err_of)"; exit 1; }
    say "created: ${ORIG_SID}"
  else
    say "MISSING — would add it (priority 10, weight 10, enabled)"
  fi
  [ -z "$OTHERS" ] || say "WARNING: the trunk also originates to: ${OTHERS} — a moved box leaves a stale entry here"
else
  say "skipped — no trunk yet"
fi

# ── 4. Termination credentials (outbound) ────────────────────────────────────
# The LiveKit outbound trunk authenticates to <domain>.pstn.twilio.com with
# this username and password. Twilio wants 12+ chars with upper, lower and a
# digit; it is checked here so the refusal is ours and early, not the API's.
head1 "Termination credential list '${CRED_LIST_NAME}'"
if [ -z "${TWILIO_OUTBOUND_USER:-}" ] || [ -z "${TWILIO_OUTBOUND_PASSWORD:-}" ]; then
  say "SKIPPED — TWILIO_OUTBOUND_USER/PASSWORD not set in lane.env"
  say "outbound calling stays off, which is the safe state"
elif [ -z "$TRUNK_SID" ]; then
  say "skipped — no trunk yet"
else
  P="${TWILIO_OUTBOUND_PASSWORD}"
  if [ "${#P}" -lt 12 ] || ! printf '%s' "$P" | grep -q '[A-Z]' || ! printf '%s' "$P" | grep -q '[a-z]' || ! printf '%s' "$P" | grep -q '[0-9]'; then
    echo "    REFUSING: TWILIO_OUTBOUND_PASSWORD must be 12+ chars with an upper, a lower and a digit." >&2
    exit 1
  fi
  LISTS=$(tw GET "${API}/SIP/CredentialLists.json?PageSize=100")
  CL_SID=$(echo "$LISTS" | jq -r --arg n "$CRED_LIST_NAME" '.credential_lists[]? | select(.friendly_name==$n) | .sid' | head -1)
  if [ -n "$CL_SID" ]; then
    say "exists: ${CL_SID}"
  elif [ "$APPLY" = "1" ]; then
    NEW=$(tw POST "${API}/SIP/CredentialLists.json" "FriendlyName=${CRED_LIST_NAME}")
    CL_SID=$(echo "$NEW" | jq -r '.sid // empty')
    [ -n "$CL_SID" ] || { echo "    create failed: $(echo "$NEW" | err_of)"; exit 1; }
    say "created: ${CL_SID}"
  else
    say "MISSING — would create it"
  fi
  if [ -n "$CL_SID" ]; then
    CREDS=$(tw GET "${API}/SIP/CredentialLists/${CL_SID}/Credentials.json?PageSize=100")
    if echo "$CREDS" | jq -e --arg u "$TWILIO_OUTBOUND_USER" '.credentials[]? | select(.username==$u)' >/dev/null; then
      say "credential '${TWILIO_OUTBOUND_USER}' exists (password not compared — rotate by hand)"
    elif [ "$APPLY" = "1" ]; then
      NEW=$(tw POST "${API}/SIP/CredentialLists/${CL_SID}/Credentials.json" \
            "Username=${TWILIO_OUTBOUND_USER}" "Password=${TWILIO_OUTBOUND_PASSWORD}")
      echo "$NEW" | jq -e '.sid' >/dev/null || { echo "    credential failed: $(echo "$NEW" | err_of)"; exit 1; }
      say "credential '${TWILIO_OUTBOUND_USER}' created (password read from lane.env, never printed)"
    else
      say "MISSING — would add credential '${TWILIO_OUTBOUND_USER}'"
    fi
    ATTACHED=$(tw GET "${TRUNKING}/Trunks/${TRUNK_SID}/CredentialLists?PageSize=100")
    if echo "$ATTACHED" | jq -e --arg s "$CL_SID" '.credential_lists[]? | select(.sid==$s)' >/dev/null; then
      say "attached to the trunk"
    elif [ "$APPLY" = "1" ]; then
      NEW=$(tw POST "${TRUNKING}/Trunks/${TRUNK_SID}/CredentialLists" "CredentialListSid=${CL_SID}")
      echo "$NEW" | jq -e '.sid' >/dev/null || { echo "    attach failed: $(echo "$NEW" | err_of)"; exit 1; }
      say "attached to the trunk"
    else
      say "would attach it to the trunk for termination auth"
    fi
  fi
fi

# ── 5. The number: voice on the trunk, texts to the webhook ──────────────────
head1 "Number ${NUMBER}"
NUMS=$(tw GET "${API}/IncomingPhoneNumbers.json?PhoneNumber=$(printf '%s' "$NUMBER" | sed 's/+/%2B/')")
PN_SID=$(echo "$NUMS" | jq -r '.incoming_phone_numbers[0].sid // empty')
if [ -z "$PN_SID" ]; then
  say "NOT ON THIS ACCOUNT — buy or port it first; nothing else here can help"
else
  say "on the account: ${PN_SID}"
  if [ -n "$TRUNK_SID" ]; then
    ON_TRUNK=$(tw GET "${TRUNKING}/Trunks/${TRUNK_SID}/PhoneNumbers?PageSize=100")
    if echo "$ON_TRUNK" | jq -e --arg s "$PN_SID" '.phone_numbers[]? | select(.sid==$s)' >/dev/null; then
      say "voice already routed through ${TRUNK_NAME}"
    elif [ "$APPLY" = "1" ]; then
      NEW=$(tw POST "${TRUNKING}/Trunks/${TRUNK_SID}/PhoneNumbers" "PhoneNumberSid=${PN_SID}")
      echo "$NEW" | jq -e '.sid' >/dev/null && say "voice routed -> ${TRUNK_NAME}" || {
        echo "    routing failed: $(echo "$NEW" | err_of)"; exit 1; }
    else
      say "would route voice through ${TRUNK_NAME}"
    fi
  fi
  # "A MESSAGE COMES IN" — the webhook rt_health.py verifies by signature.
  if [ -n "$PUBLIC" ]; then
    WANT_SMS="https://${PUBLIC}/sms/incoming"
    HAVE_SMS=$(echo "$NUMS" | jq -r '.incoming_phone_numbers[0].sms_url // empty')
    if [ "$HAVE_SMS" = "$WANT_SMS" ]; then
      say "texts already go to ${WANT_SMS}"
    elif [ -n "$HAVE_SMS" ]; then
      say "WARNING: texts currently go to '${HAVE_SMS}', not ${WANT_SMS} — not changed; fix by hand"
    elif [ "$APPLY" = "1" ]; then
      NEW=$(tw POST "${API}/IncomingPhoneNumbers/${PN_SID}.json" "SmsUrl=${WANT_SMS}" "SmsMethod=POST")
      echo "$NEW" | jq -e '.sid' >/dev/null && say "texts -> ${WANT_SMS}" || {
        echo "    sms webhook failed: $(echo "$NEW" | err_of)"; exit 1; }
    else
      say "would point texts at ${WANT_SMS} (RT_SMS_WEBHOOK_URL must match it exactly)"
    fi
  else
    say "PUBLIC_HOSTNAME unset in lane.env — the SMS webhook is left for the console"
  fi
fi

# ── 6. The daily spend alarm ─────────────────────────────────────────────────
# Fires once a day at most, when today's total reaches the cap, into the
# worker's /twilio/usage. It stops nothing — the worker does, by reading the
# same number before every unattended dial (rt_carrier.py).
TRIG_NAME="phone-pal-${LANE:-dev}-daily-cap"
head1 "Usage trigger '${TRIG_NAME}' (\$${CAP}/day, totalprice)"
if [ -z "$PUBLIC" ]; then
  say "skipped — PUBLIC_HOSTNAME unset, nowhere to send it (the worker-side cap still applies)"
else
  TRIGS=$(tw GET "${API}/Usage/Triggers.json?PageSize=100")
  TRIG_SID=$(echo "$TRIGS" | jq -r --arg n "$TRIG_NAME" '.usage_triggers[]? | select(.friendly_name==$n) | .sid' | head -1)
  if [ -n "$TRIG_SID" ]; then
    HAVE=$(echo "$TRIGS" | jq -r --arg n "$TRIG_NAME" '.usage_triggers[] | select(.friendly_name==$n) | "\(.usage_category) \(.trigger_by) \(.trigger_value) \(.recurring)"')
    say "exists: ${TRIG_SID}  (${HAVE})"
    case "$HAVE" in
      "totalprice price ${CAP}"*" daily") : ;;
      "totalprice price ${CAP}.0"*" daily") : ;;
      *) echo "    WARNING: the trigger does not read totalprice/price/${CAP}/daily — check it by hand" >&2 ;;
    esac
  elif [ "$APPLY" = "1" ]; then
    NEW=$(tw POST "${API}/Usage/Triggers.json" "FriendlyName=${TRIG_NAME}" \
          "UsageCategory=totalprice" "TriggerBy=price" "TriggerValue=${CAP}" "Recurring=daily" \
          "CallbackUrl=https://${PUBLIC}/twilio/usage" "CallbackMethod=POST")
    TRIG_SID=$(echo "$NEW" | jq -r '.sid // empty')
    [ -n "$TRIG_SID" ] || { echo "    create failed: $(echo "$NEW" | err_of)"; exit 1; }
    say "created: ${TRIG_SID}"
  else
    say "MISSING — would create it, calling https://${PUBLIC}/twilio/usage"
  fi
fi

cat <<NEXT

==> Twilio side $([ "$APPLY" = 1 ] && echo "applied" || echo "inspected (nothing changed)").

    Not done here, because it is not Twilio:
      · the LiveKit inbound trunk + dispatch rule -> recreate-routing.sh
      · the LiveKit outbound trunk                -> outbound-trunk.sh
      · the Linode firewall                       -> firewall-setup.sh

    Twilio routes to the box BY IP. If ${BOX_IP} ever changes, inbound dies
    silently — no error in any log in this repo. The phone just rings out.

    The spend cap is enforced by the worker, not the carrier: worker.env needs
    TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and TWILIO_DAILY_SPEND_USD=${CAP}
    (render-config.sh forwards them) or every unattended dial is refused.
NEXT
