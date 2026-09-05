# trial-pal lane — captured inventory

Read off the running box, not from notes. Regenerate with:

```bash
HOST=root@45.79.170.137 LANE=trial-pal bash deploy/provisioning/capture-lane.sh
```

Repo commit at capture: `ff279ec`

## Identity

| What | Value |
|---|---|
| Number | `+19086571294` |
| Agent name | `trial-pal` — must equal the dispatch rule's agent |
| Box IP | `45.79.170.137` — Twilio routes here BY IP; keep the reservation |
| LiveKit API key | fingerprint `ebd9026fd952` (never stored) |
| LiveKit secret | fingerprint `7f286e7d1d94` (never stored) |
| Outbound trunk | `?` |

## LiveKit SIP state

### Inbound trunks
```
┌─────────────────┬───────────┬──────────────┬────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┬────────────────┬────────────────┬────────────┬─────────┬──────────┐
│ SipTrunkID      │ Name      │ Numbers      │ AllowedAddresses                                                                                                                       │ AllowedNumbers │ Authentication │ Encryption │ Headers │ Metadata │
├─────────────────┼───────────┼──────────────┼────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┼────────────────┼────────────────┼────────────┼─────────┼──────────┤
│ ST_bPv4LzccXFFR │ trial-pal │ +19086571294 │ 54.172.60.0/30,54.244.51.0/30,54.171.127.192/30,35.156.191.128/30,54.65.63.192/30,54.169.127.128/30,54.252.254.64/30,177.71.206.192/30 │                │                │ DISABLE    │         │          │
└─────────────────┴───────────┴──────────────┴────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┴────────────────┴────────────────┴────────────┴─────────┴──────────┘
```
### Dispatch rules
```
┌───────────────────┬───────────┬─────────────────┬─────────────────────┬──────────────────────────┬─────┬────────────┬───────────┐
│ SipDispatchRuleID │ Name      │ SipTrunks       │ Type                │ RoomName                 │ Pin │ Attributes │ Agents    │
├───────────────────┼───────────┼─────────────────┼─────────────────────┼──────────────────────────┼─────┼────────────┼───────────┤
│ SDR_niGqbdMRuaWH  │ trial-pal │ ST_bPv4LzccXFFR │ Individual (Caller) │ phone-_<caller>_<random> │     │ map[]      │ trial-pal │
└───────────────────┴───────────┴─────────────────┴─────────────────────┴──────────────────────────┴─────┴────────────┴───────────┘
```
### Outbound trunks
```
┌────────────┬──────┬─────────┬───────────┬─────────┬────────────────┬────────────┬─────────┬──────────┐
│ SipTrunkID │ Name │ Address │ Transport │ Numbers │ Authentication │ Encryption │ Headers │ Metadata │
├────────────┼──────┼─────────┼───────────┼─────────┼────────────────┼────────────┼─────────┼──────────┤
└────────────┴──────┴─────────┴───────────┴─────────┴────────────────┴────────────┴─────────┴──────────┘
```
## Containers
```
phone-pal-autoheal-1	willfarrell/autoheal:1.2.0	Up 8 minutes (healthy)
phone-pal-caddy-1	caddy:2.11.4-alpine	Up 8 minutes
phone-pal-livekit-1	livekit/livekit-server:v1.13.6	Up 8 minutes
phone-pal-redis-1	redis:7.4.11-alpine	Up 8 minutes
phone-pal-scheduler-1	phone-pal-scheduler	Up 8 minutes (healthy)
phone-pal-sip-1	livekit/sip:v1.13.0	Up 8 minutes
phone-pal-worker-1	phone-pal-worker	Up About a minute (healthy)
```
## worker.env — keys present (values deliberately absent)

A rebuilt lane needs every one of these set. The values come from
`lane.env`, which is not in this repository.

```
AGENT_NAME
DEFAULT_TZ
ENV_MODE
GOOGLE_API_KEY
LIVEKIT_API_KEY
LIVEKIT_API_SECRET
LIVEKIT_SELF_HOSTED
LIVEKIT_URL
LK_GOOGLE_DEBUG
RESEND_API_KEY
RT_ALERT_EMAIL_TO
RT_ALLOWED_SUPABASE_REF
RT_AUTO_MIGRATE
RT_CALLBACK_HOURS
RT_CALLBACK_HOURS_LIVE
RT_CALL_HARD_CAP_MINS
RT_DAILY_MINUTES_PER_CALLER
RT_DOCKER_HEALTH_HOST
RT_DOCKER_HEALTH_PORT
RT_GREET
RT_HEALTH_PORT
RT_LANGUAGE
RT_LOG_LEVEL
RT_LOG_TRANSCRIPT
RT_MAX_CALL_SECONDS
RT_PHONE_HASH_PEPPER
RT_PUBLIC_NUMBER
RT_RECOVERY
RT_REQUIRE_PEPPER
RT_SCHEDULER_ENABLED
RT_SMS_WEBHOOK_URL
RT_TRACE
RT_WORKER_HTTP_HOST
SIP_OUTBOUND_TRUNK_ID
SUPABASE_PROJECT_REF
SUPABASE_SERVICE_ROLE_KEY
SUPABASE_URL
TWILIO_ACCOUNT_SID
TWILIO_AUTH_TOKEN
TWILIO_DAILY_SPEND_USD
TWILIO_FROM_NUMBER
```
