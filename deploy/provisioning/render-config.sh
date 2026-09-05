#!/usr/bin/env bash
# render-config.sh — turn lane.env into the four config files, ON THE BOX.
#
#   bash deploy/provisioning/render-config.sh           # show what would change
#   APPLY=1 bash deploy/provisioning/render-config.sh   # write them
#   OUT_DIR=/some/tmp bash .../render-config.sh         # render locally, no ssh
#
# OUT_DIR is for inspection and the test suite: it renders the four files into
# that directory (mode 600) and touches no box. It refuses a directory inside
# the working tree, because that is the one place a rendered secret must never
# land.
#
# This is the one place both halves of a lane are held at once: the repo's
# templates, and the credentials from lane.env. It writes ONLY to the box, over
# ssh, mode 600, and never into the working tree — there is no step here whose
# output could be committed by accident.
#
# It refuses to overwrite a config that already exists unless FORCE=1. A lane's
# livekit.yaml holds the key pair the SIP bridge and the worker both authenticate
# with; rewriting it from a half-filled lane.env takes the lane down, and the
# failure is a phone that rings out rather than an error anyone sees.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "${HERE}/../.." && pwd)"
ENV_FILE="${ENV_FILE:-${HERE}/lane.env}"
APPLY="${APPLY:-0}"; FORCE="${FORCE:-0}"
[ -f "$ENV_FILE" ] || { echo "no lane.env at $ENV_FILE" >&2; exit 1; }
set -a; . "$ENV_FILE"; set +a

OUT_DIR="${OUT_DIR:-}"
[ -n "$OUT_DIR" ] || : "${BOX_HOST:?set BOX_HOST in lane.env}"
: "${LIVEKIT_API_KEY:?}" ; : "${LIVEKIT_API_SECRET:?}"
: "${GOOGLE_API_KEY:?}"  ; : "${SUPABASE_URL:?}"
: "${SUPABASE_SERVICE_ROLE_KEY:?}" ; : "${AGENT_NAME:?}" ; : "${NUMBER:?}"
# The pepper is required unless the lane says otherwise, with the same
# fail-closed reading as config.pepper_required(): only 0/false/no/off opt out.
# Empty or unset RT_REQUIRE_PEPPER keeps the template's `=1`, so an empty
# pepper would ship as a worker that dies at boot and restart-loops (#20).
#
# And "set" is not enough: config.pepper_problem() rejects a value that is a
# template comment (starts with #), shorter than 16 chars, or a placeholder
# (contains replace/example/changeme/todo/xxxx, any case) — so the same rule
# is applied HERE, before the value ever lands on a box, rather than letting
# the worker discover it at boot on a lane nobody is watching.
pepper_problem() {
  local v="$1" low
  [ -n "$v" ] || { echo "empty"; return; }
  case "$v" in '#'*) echo "starts with #"; return ;; esac
  [ "${#v}" -ge 16 ] || { echo "too short (< 16 chars)"; return; }
  low=$(printf '%s' "$v" | tr '[:upper:]' '[:lower:]')
  case "$low" in
    *replace*|*example*|*changeme*|*todo*|*xxxx*) echo "placeholder"; return ;;
  esac
}
PEPPER_HELP="Generate one (openssl rand -hex 32) — once, before the first call — unless the box already has one: then copy THAT value into lane.env, because a new pepper orphans every phone hash already stored. RT_REQUIRE_PEPPER=0 is for a dev-only lane."
case "$(printf '%s' "${RT_REQUIRE_PEPPER:-}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')" in
  0|false|no|off) ;;
  *)
    : "${RT_PHONE_HASH_PEPPER:?RT_PHONE_HASH_PEPPER is empty in lane.env but RT_REQUIRE_PEPPER is on. ${PEPPER_HELP}}"
    WHY=$(pepper_problem "${RT_PHONE_HASH_PEPPER}")
    if [ -n "$WHY" ]; then
      # The reason is printed; the value never is.
      echo "REFUSING: RT_PHONE_HASH_PEPPER in lane.env is ${WHY}, and RT_REQUIRE_PEPPER is on." >&2
      echo "  ${PEPPER_HELP}" >&2
      exit 1
    fi ;;
esac
TARGET="${TARGET:-/opt/phone-pal}"
CONF="${TARGET}/deploy"
HOSTNAME_FOR_TLS="${PUBLIC_HOSTNAME:-$(printf '%s' "${BOX_IP:-}" | tr '.' '-').sslip.io}"
export HOSTNAME_FOR_TLS   # the worker.env render derives RT_SMS_WEBHOOK_URL from it

[ "$APPLY" = "1" ] || [ -n "$OUT_DIR" ] || echo "DRY RUN — nothing will be written. APPLY=1 to write."

# Which files already exist there? Never clobber silently.
#
# EXCEPT a file byte-identical to the repo's own .example, which holds no
# credentials at all: it is what provision-box.sh plants on a bare box. Counting
# those as "already configured" is why the first stand-up of a new lane exited 0
# here having written nothing, and stand-up-lane.sh read that as success and
# carried on to the firewall, the carrier and the stack — leaving a box running
# on placeholder values with nothing in the output that looked wrong (2026-09-03,
# found standing up trial-pal).
EXISTS=""
if [ -z "$OUT_DIR" ]; then
  FOUND=$(ssh -o BatchMode=yes "$BOX_HOST" \
    "for f in worker.env livekit.yaml sip.yaml Caddyfile; do \
       [ -f ${CONF}/\$f ] && echo \"\$f \$(openssl dgst -sha256 < ${CONF}/\$f)\"; done" \
    2>/dev/null || true)
  while read -r f rest; do
    [ -n "${f:-}" ] || continue
    ex="${REPO}/deploy/${f}.example"
    if [ -f "$ex" ]; then
      # Last field only: the digest line is prefixed differently across OpenSSL
      # versions ("(stdin)= " vs "SHA2-256(stdin)= "), and the box is not this host.
      want=$(openssl dgst -sha256 < "$ex"); want="${want##* }"
      if [ "${rest##* }" = "$want" ]; then
        echo "    ${f} on the box is still the unfilled example — rendering over it."
        continue
      fi
    fi
    EXISTS="${EXISTS}${f}"$'\n'
  done <<EOF
${FOUND}
EOF
fi
if [ -n "$EXISTS" ] && [ "$FORCE" != "1" ]; then
  echo
  echo "==> Already present on ${BOX_HOST}:"
  printf '      %s\n' $EXISTS
  echo "    Leaving them alone. These hold the credentials the running lane is"
  echo "    authenticating with; rewriting them from lane.env is how a lane goes"
  echo "    quiet. Re-run with FORCE=1 only if you mean to replace them."
  exit 0
fi

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
umask 077

# ── worker.env ───────────────────────────────────────────────────────────────
# Built from the tracked example so a new variable added to the template is
# never silently dropped here: values known to lane.env are substituted, and
# anything else keeps the example's default.
python3 - "$REPO/deploy/worker.env.example" > "$TMP/worker.env" <<'PY'
import os, re, sys
src = open(sys.argv[1]).read()
# lane.env values that map straight onto worker.env keys
# The pepper and its switch ride along: without them a rendered lane booted
# with RT_REQUIRE_PEPPER=1 and no pepper, and died on every start (#20).
m = {k: os.environ.get(k, "") for k in (
    "AGENT_NAME","GOOGLE_API_KEY","SUPABASE_URL","SUPABASE_SERVICE_ROLE_KEY",
    "SUPABASE_PROJECT_REF","LIVEKIT_API_KEY","LIVEKIT_API_SECRET","LIVEKIT_URL",
    "RESEND_API_KEY","RT_ALERT_EMAIL_TO","SIP_OUTBOUND_TRUNK_ID",
    "RT_SCHEDULER_ENABLED","RT_PHONE_HASH_PEPPER","RT_REQUIRE_PEPPER",
    "TWILIO_ACCOUNT_SID","TWILIO_AUTH_TOKEN","TWILIO_DAILY_SPEND_USD")}
m["RT_PUBLIC_NUMBER"]        = os.environ.get("NUMBER","")
m["RT_ALLOWED_SUPABASE_REF"] = os.environ.get("SUPABASE_PROJECT_REF","")
# The lane's one number carries voice and texts alike, and Twilio signs each
# webhook over the exact URL it was given — so the URL is derived from the
# same hostname the Caddyfile is rendered with, never typed twice.
m["TWILIO_FROM_NUMBER"]      = os.environ.get("NUMBER","")
_host = os.environ.get("HOSTNAME_FOR_TLS","")
m["RT_SMS_WEBHOOK_URL"]      = f"https://{_host}/sms/incoming" if _host else ""
out = []
for line in src.splitlines():
    k = re.match(r'^([A-Z_]+)=', line)
    if k and k.group(1) in m and m[k.group(1)]:
        out.append(f"{k.group(1)}={m[k.group(1)]}")
    else:
        out.append(line)
print("\n".join(out))
PY

# ── livekit.yaml / sip.yaml — the SAME pair, or the worker never registers ───
sed -e "s|APIxxxxxxxxxxxx|${LIVEKIT_API_KEY}|" \
    -e "s|replace-me-with-the-generated-secret|${LIVEKIT_API_SECRET}|" \
    "$REPO/deploy/livekit.yaml.example" > "$TMP/livekit.yaml"
sed -e "s|APIxxxxxxxxxxxx|${LIVEKIT_API_KEY}|" \
    -e "s|replace-me-with-the-generated-secret|${LIVEKIT_API_SECRET}|" \
    "$REPO/deploy/sip.yaml.example" > "$TMP/sip.yaml"
sed "s|{{HOST}}|${HOSTNAME_FOR_TLS}|" "$REPO/deploy/Caddyfile.template" > "$TMP/Caddyfile"

# Prove the pair matches across both files before shipping them.
# The template's `keys:` block opens with a comment line; skip comments and
# blanks rather than counting lines, so an edited template cannot make the
# check compare "#<API_KEY>" against sip.yaml and refuse a matching pair.
LKK=$(awk '/^keys:/{while((getline l)>0){if(l ~ /^[ \t]*(#|$)/)continue; gsub(/[ \t]/,"",l); split(l,a,":"); print a[1]; exit}}' "$TMP/livekit.yaml")
SPK=$(awk -F': *' '/^api_key:/{print $2; exit}' "$TMP/sip.yaml")
if [ "$LKK" != "$SPK" ]; then
  echo "REFUSING: livekit.yaml key (${LKK}) != sip.yaml key (${SPK})." >&2
  echo "  All three of livekit.yaml, sip.yaml and worker.env must agree." >&2
  exit 1
fi
echo "    key pair consistent across livekit.yaml and sip.yaml"

if [ -n "$OUT_DIR" ]; then
  REPO_P="$(cd "$REPO" && pwd -P)"
  case "$(cd "$OUT_DIR" 2>/dev/null && pwd -P)" in
    "$REPO_P"|"$REPO_P"/*) echo "REFUSING: OUT_DIR is inside the working tree." >&2; exit 1 ;;
    "") echo "OUT_DIR does not exist: $OUT_DIR" >&2; exit 1 ;;
  esac
  for f in worker.env livekit.yaml sip.yaml Caddyfile; do
    cp "$TMP/$f" "$OUT_DIR/$f"; chmod 600 "$OUT_DIR/$f"
  done
  echo "    rendered to ${OUT_DIR} (values not printed)"
  exit 0
fi

if [ "$APPLY" != "1" ]; then
  echo "    would write (mode 600) to ${BOX_HOST}:${CONF}/ —"
  for f in worker.env livekit.yaml sip.yaml Caddyfile; do
    echo "      ${f}  ($(grep -c '' "$TMP/$f") lines)"
  done
  echo "    values are not printed, here or anywhere"
  exit 0
fi

ssh -o BatchMode=yes "$BOX_HOST" "mkdir -p ${CONF}"
for f in worker.env livekit.yaml sip.yaml Caddyfile; do
  scp -q "$TMP/$f" "${BOX_HOST}:${CONF}/$f"
  ssh -o BatchMode=yes "$BOX_HOST" "chmod 600 ${CONF}/$f"
  echo "    wrote ${CONF}/${f}"
done
echo
echo "==> Config is on the box. Restart to pick it up:"
echo "    ssh ${BOX_HOST} 'cd ${CONF} && docker compose up -d'"
