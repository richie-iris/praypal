# PrayPal Operations Runbook

Short, ordered checklists for the pages that actually wake someone up. Each section walks the call path from the outside in, so the first check that fails is the layer to fix. A dead worker looks identical to a dead carrier from the caller's side.

Conventions:
* `<lane>` is `dev`, `test`, or `prod`; host details are in [`deploy/provisioning/`](../deploy/provisioning) `<lane>-inventory.md`.
* All commands run on the lane host unless stated otherwise.
* Health ports are loopback-only: `8080` is the Docker health server (`/health`, `/ready`, `/live`), `8082` is the LiveKit worker's own HTTP port.
* For multi-agent spiritual architecture, persona directives, and invariant rules, see [`AGENTS.md`](../AGENTS.md).

---

## Silent line at 3am

Symptom: A caller dials +1 (862) 358-8238 and hears silence, a fast busy, or the call drops after one ring. Work the checks in this exact order:

1. **Carrier (Twilio).** Confirm the number still routes to the lane's Elastic SIP Trunk and the trunk originates to this box's IP. In Twilio console check *Phone Numbers -> Active Numbers -> Voice* (must read "SIP Trunk") and *Elastic SIP Trunking -> Trunks -> Origination*. `bash deploy/provisioning/twilio-setup.sh` prints both. *Monitor -> Logs -> Calls* shows whether the call reached Twilio; `deploy/call-logs.sh` shows if any INVITE reached our host. If absent in Twilio logs, escalate to carrier.
2. **LiveKit SIP bridge.** `docker compose -f deploy/docker-compose.yml logs --since 15m sip` should show `INVITE` lines and dispatch rule hits. If INVITE is rejected, re-run [`deploy/provisioning/recreate-routing.sh`](../deploy/provisioning/recreate-routing.sh). If container restarts, check `sip.yaml` against `sip.yaml.example` (usually rotated LiveKit API credentials).
3. **Worker readiness (`/ready`).** `curl -s http://127.0.0.1:8080/ready`. A `200` means all checks passed. A `503` with `"status": "degraded"` lists failing checks by name. The `autoheal` sidecar restarts after three consecutive failures; inspect with `docker inspect --format '{{.RestartCount}}' <container>`. A missing pepper causes `RT_REQUIRE_PEPPER=1` to fail: `config.missing_config()` lists the pepper, and `config.validate_startup_config()` prints `[config] ❌ STARTUP CONFIGURATION ERRORS` with `RT_PHONE_HASH_PEPPER is missing (<reason>)` at boot — it deliberately does not exit; the worker comes up, the `pepper` readiness check fails, `/ready` reports `degraded`, and autoheal restart-loops the container. Workers booted under prod agent names (`config.PROD_AGENT_NAMES`: `iris-phone`, `phone-pal-prod`) raise `RuntimeError` at import if the pepper is missing, so `docker compose logs worker` shows that traceback and none of the `[rt-startup]` database-probe lines. Set the pepper in `worker.env` (see [ADR 0002](adr/0002-phone-hash-pepper.md)). Port `8082` is the LiveKit worker HTTP port (loopback); `curl -s http://127.0.0.1:8082/` verifies worker registration.
4. **Supabase.** The context hydrator requires the RPC bundle before greeting plays.
   Run `python scripts/check_migrations.py <lane>` (lane is positional; no argument checks every lane) to verify RPC functions.
   Run `python scripts/migrate.py --status` to check unapplied migrations. Job scheduling fails closed on `rt_count_jobs_today` (migration 20). Check the Supabase dashboard for paused projects or mismatched `service_role` keys.
5. **Gemini.** If the line connects but the guide never speaks, the Live session failed to open. Look for `[rt]` and `[rt-guard]` errors in `docker compose logs worker` (bridge issues log as `[rt-bridge]`, outbound issues as `[rt-scheduler]` in scheduler): quota exhaustion or invalid `GOOGLE_API_KEY`. Rotate key in `worker.env` and run `docker compose up -d worker`.

When all five pass and silence persists, capture the lane (`deploy/provisioning/capture-lane.sh`) and diff against inventory.

---

## Worker restart-looping

1. `docker compose ps` shows `unhealthy`; `docker inspect --format '{{.RestartCount}}' <container>` shows the count rising, meaning `/ready` is failing and `autoheal` is acting.
2. `curl -s http://127.0.0.1:8080/ready | python -m json.tool` — inspect failing checks.
3. Fix the named dependency (see ordered checks above). Never disable health checks to mask failure.

---

## Scheduled devotions not going out

1. `docker compose logs --since 1h scheduler` — verify the scheduler's loop heartbeat.
2. Look for `REFUSE outbound call outside quiet hours` or `DEFER outbound dial`: these are TCPA calling-window decisions (8am-9pm callee local time) and are correct behavior (see [ADR 0004](adr/0004-tcpa-calling-windows.md)).
3. Missing `rt_count_jobs_today` (migration 20) fails closed. Apply migrations to unblock.

---

## Daily calling cap and dial refusals

Before every unattended outbound call (`rt_carrier.outbound_allowed()`) and at the entry of every inbound PSTN call (`rt_carrier.inbound_allowed()`), the worker checks today's `totalprice` against `TWILIO_DAILY_SPEND_USD`. It fails closed on unreadable billing.

1. `docker logs --since 1h pray-pal-worker-1 | grep carrier_cap` provides refusal reasons: `daily_cap_spent` (shows `spend_usd` and `cap_usd`), `usage_unreadable`, or `no_twilio_credentials`.
2. Inbound calls refused at the cap log `[rt] Inbound call refused: Twilio daily spend cap reached` and disconnect cleanly without launching Gemini Live sessions.
3. To raise spend, update `TWILIO_DAILY_SPEND_USD` in `worker.env` and restart worker (cached 60s).
4. The `/twilio/usage` alarm logs (`budget.carrier_cap`) as telemetry without interrupting live calls.

---

## SMS & MMS Delivery, TCPA Compliance, and Inbound Sync

Symptom: Incoming SMS/MMS prayer intention or photo altar receives no reply.

1. **TCPA / CTIA Keyword Intercepts.** Standard compliance keywords are handled deterministically before model invocation:
   - `STOP`, `STOPALL`, `UNSUBSCRIBE`, `CANCEL`, `END`, `QUIT`: Immediately suppresses all outbound texts to that number and returns standard CTIA confirmation. The suppression ledger is keyed by `phone_hash`.
   - `START`, `UNSTOP`: Resubscribes the number and confirms reactivation.
   - `HELP`, `INFO`: Returns support contact and service disclosure text.
   - Opted-out numbers texting anything else are silently dropped to prevent billing abuse.
2. **Devotional Scope.** The service is exclusively for spiritual and devotional prayers; medical or emergency crisis management is strictly out of scope.
3. **Carrier Webhook (Twilio).** In Twilio Console under *Active Numbers -> Messaging*, verify webhook URL: `https://<lane-domain>/sms/incoming` (HTTP POST). Check *Monitor -> Logs -> Messaging* for HTTP retrieval failures (e.g., error 11200).
4. **Caddy Reverse Proxy.** Verify reverse proxy routing in `docker logs --since 15m pray-pal-caddy-1`:
   ```caddy
   handle /media/* { reverse_proxy 127.0.0.1:8080 }
   handle /sms/* { reverse_proxy 127.0.0.1:8080 }
   ```
5. **Webhook Signature.** A `403` in Twilio logs indicates signature mismatch. Check `docker logs --since 15m pray-pal-worker-1 | grep sms.webhook_rejected`. Verify `TWILIO_AUTH_TOKEN` and `RT_SMS_WEBHOOK_URL` in `worker.env`.
6. **Worker Handler.** Verify hits via `docker logs --since 15m pray-pal-worker-1 | grep rt-sms-inbound`. Look for `[rt-sms-inbound] Received message from ***NNNN` and `[rt-sms-inbound] Replying to ***NNNN`. If `RT_SMS_ENABLED=0`, messages are stored without reply.
7. **In-Call SMS Sync.** In-call messages persist to `RT_SMS_LEDGER_DIR` (`/tmp/rt_sms/`) keyed by `phone_hash`. In-call logs show `Injected incoming SMS`. The guide accesses texts when calling `recall_earlier` or during post-call extraction.

---

## Forget-me request

`python scripts/reset_caller.py <number>` (use `--test` for test lane) executes `rt_forget_caller`, archiving and deleting caller data (migration 19 soft-delete). Hourly housekeeping calls `rt_purge_forgotten`. Retention policy: [`RETENTION.md`](RETENTION.md).

---

## Undo an erase within 24h

`rt_forget_caller` archives rows into `rt.forgotten_archive` before deletion (migration 19). `rt_restore_caller` restores the **newest** forget batch within 24 hours (migration 21).

1. **Calculate caller hash.** Keyed HMAC of E.164 under lane pepper (matching `scripts/reset_caller.py` logic loading `.env.local` and `deploy/worker.env`):
   ```bash
   python -c "from dotenv import load_dotenv; load_dotenv('.env.local'); \
     load_dotenv('../deploy/worker.env'); from rt_prefs import phone_hash; \
     print(phone_hash('+15551234567'))"
   ```
2. **Execute restore as service role.** Run in Supabase SQL editor or via `python sql_push.py --file <file.sql>`:
   ```sql
   SELECT rt_restore_caller('<hash>');
   ```
   Returns rows restored. Returns `0` if outside 24h or invalid hash.
3. Only the newest batch is restored; an earlier erase stays gone.

---

## Deploy ordering & Prod Safety Guards

1. Apply migrations 18-21 (`python scripts/migrate.py --apply`) before deploying worker.
2. Set `RT_PHONE_HASH_PEPPER` before setting `RT_REQUIRE_PEPPER=1` (one-way door: [ADR 0002](adr/0002-phone-hash-pepper.md)).
3. Pin Docker images; avoid unpinned `:latest` tags.
4. Ad-hoc SQL against prod Supabase refs requires strict safeguards. `sql_push.py` operates **read-only** by default; modifying statements require `--confirm <ref>` and `--i-know-this-is-prod`. Destructive commands (`--wipe`, `TRUNCATE`, `rt_wipe_all_data`) are blocked on prod even with flags. `scripts/bootstrap_test_db.py refuses prod refs` outright to prevent accidental test seeding on production databases.
