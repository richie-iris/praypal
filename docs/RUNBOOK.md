# Phone-Pal Operations Runbook

Short, ordered checklists for the pages that actually wake someone up. Each
section walks the call path from the outside in, so the first check that fails
is the layer to fix. Do not skip ahead: a dead worker looks identical to a dead
carrier from the caller's side.

Conventions:

* `<lane>` is `dev`, `test`, or `prod`; the box for each is listed in
  [`deploy/provisioning/`](../deploy/provisioning) `<lane>-inventory.md`.
* All commands run on the lane host unless stated otherwise.
* Health ports are loopback-only: `8080` is the Docker health server
  (`/health`, `/ready`, `/live`), `8082` is the LiveKit worker's own HTTP port.
* For multi-agent swarm architecture, agent roles, and invariant rules, see [`AGENTS.md`](../AGENTS.md).


---

## Silent line at 3am

Symptom: a caller dials the number and hears silence, a fast busy, or the call
drops after one ring. Work the checks in this order — each one depends on the
one above it being healthy.

1. **Carrier (Twilio).** Confirm the number still routes to the lane's Elastic
   SIP Trunk and the trunk still originates to this box's IP. In the Twilio
   console check *Phone Numbers -> Active Numbers -> the number -> Voice*
   (should read "SIP Trunk", not a TwiML app) and *Elastic SIP Trunking ->
   Trunks -> phone-pal-<lane> -> Origination*; `bash
   deploy/provisioning/twilio-setup.sh` prints both without changing anything.
   *Monitor -> Logs -> Calls* shows whether the call reached Twilio at all, and
   `deploy/call-logs.sh` shows whether any INVITE reached us. Nothing in the
   Twilio logs means the problem is upstream of this box (number ported, trunk
   suspended, unpaid invoice) — stop here and escalate to the carrier.

2. **LiveKit SIP bridge.** `docker compose -f deploy/docker-compose.yml logs
   --since 15m sip` should show `INVITE` lines and a matching dispatch rule
   hit. If the INVITE arrives but is rejected, the inbound trunk or dispatch
   rule is gone: re-run
   [`deploy/provisioning/recreate-routing.sh`](../deploy/provisioning/recreate-routing.sh).
   If the container is restarting, check `sip.yaml` against
   `sip.yaml.example` — the most common cause is a rotated LiveKit API key.

3. **Worker readiness (`/ready`).** `curl -s http://127.0.0.1:8080/ready`. A
   `200` means every registered readiness check passed. A `503` with
   `"status": "degraded"` lists the failing checks by name in the body — the
   `autoheal` sidecar restarts the container after three consecutive
   failures, so a restart loop here is expected until the underlying check
   is fixed. `docker inspect --format '{{.RestartCount}}' <container>` shows
   the restart count (`docker compose ps` only shows status). A common
   self-inflicted cause is `RT_REQUIRE_PEPPER=1` without
   `RT_PHONE_HASH_PEPPER` set — or set to something `config.pepper_ok()`
   rejects (a placeholder such as `changeme`, or under 16 characters), which
   counts as missing. `config.missing_config()` lists the pepper, so
   `config.validate_startup_config()` prints
   `[config] ❌ STARTUP CONFIGURATION ERRORS` with a
   `RT_PHONE_HASH_PEPPER is missing (<reason>)` line at boot — it
   deliberately does not exit; the worker comes up, the `pepper` readiness check fails, `/ready`
   reports `degraded`, and autoheal restart-loops the container. A worker
   booted under a prod agent name (`config.PROD_AGENT_NAMES`: `iris-phone`,
   `phone-pal-prod`) never gets that far: `agent.py` raises `RuntimeError`
   at import when the pepper is missing or `RT_REQUIRE_PEPPER` is switched
   off, so `docker compose logs worker` shows that traceback and none of the
   `[rt-startup]` database-probe lines. Set the pepper and let it come up —
   do not unset the requirement on a deployed lane (see
   [ADR 0002](adr/0002-phone-hash-pepper.md)).
   Port `8082` is the LiveKit worker HTTP port (loopback); if `8080` answers
   but the agent never picks up rooms, `curl -s http://127.0.0.1:8082/` tells
   you whether the LiveKit worker registered with the server.

4. **Supabase.** The hydrator needs the context bundle RPC before the greeting
   plays. `python scripts/check_migrations.py <lane>` (lane is positional;
   no argument checks every lane) verifies the RPC functions the worker
   expects actually exist; `python scripts/migrate.py --status`
   shows unapplied or gapped migrations. Job scheduling fails closed on
   `rt_count_jobs_today` (migration `20`), so a lane that skipped migrations
   18-21 will answer calls but refuse every reminder — apply them in order.
   Check the Supabase dashboard for a paused project (free tier pauses after
   inactivity) and for the `service_role` key matching `worker.env`.

5. **Gemini.** If the line connects and the greeting plays but the assistant
   never responds, the Live session failed to open. Look for `[rt]` and
   `[rt-guard]` errors in `docker compose logs worker` (bridge problems log
   as `[rt-bridge]`, outbound ones as `[rt-scheduler]` in the scheduler
   container): quota exhaustion, an invalid `GOOGLE_API_KEY`, or a voice
   name outside the prebuilt catalog.
   Rotate the key in `worker.env` and `docker compose up -d worker`. The
   worker cannot process audio without Gemini; there is no fallback model.

When all five pass and the line is still silent, capture the lane
(`deploy/provisioning/capture-lane.sh`) and compare against the last known
good inventory before changing anything else.

---

## Worker restart-looping

1. `docker compose ps` shows `unhealthy`; `docker inspect --format
   '{{.RestartCount}}' <container>` shows the count rising. Together they
   mean `/ready` is failing and `autoheal` is doing its job.
2. `curl -s http://127.0.0.1:8080/ready | python -m json.tool` — read the
   failing check names.
3. Fix the named dependency (see the ordered checks above). Do **not** disable
   the healthcheck to stop the loop; a worker that answers calls while its
   database is unreachable loses every memory from those calls.

---

## Scheduled calls not going out

1. `docker compose logs --since 1h scheduler` — the scheduler writes a
   heartbeat every loop; a stale heartbeat trips its own healthcheck.
2. Look for `REFUSE outbound call outside quiet hours` or `DEFER outbound
   dial`: these are TCPA calling-window decisions (8am-9pm at the callee's
   local time) and are correct behaviour, not failures. See
   [ADR 0004](adr/0004-tcpa-calling-windows.md).
3. `rt_count_jobs_today` missing (migration 20 not applied) makes every job
   creation fail closed. Apply the migration; nothing else needs restarting.

---

## She refuses to call anyone

Symptom: reminder calls stop going out, or `bridge_call` answers "we've used up
today's calling budget" / "I can't confirm our phone budget right now".

Twilio has no per-day spend cap that refuses a call the way Telnyx's outbound
voice profile did, so the cap lives in the worker: before every unattended dial
`rt_carrier.outbound_allowed()` reads today's `totalprice` from the Twilio
account and refuses at or over `TWILIO_DAILY_SPEND_USD`. It fails closed — an
unreadable bill refuses exactly like a spent one.

1. `docker logs --since 1h phone-pal-worker-1 | grep carrier_cap` gives the
   reason on every refusal: `daily_cap_spent` (with `spend_usd` and `cap_usd`),
   `usage_unreadable` (Twilio API unreachable, or credentials rejected), or
   `no_twilio_credentials` (`TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` missing
   from `worker.env` — the dial tools are not even offered in that state).
2. A genuinely spent cap is a decision, not a fault: raise
   `TWILIO_DAILY_SPEND_USD` in `worker.env` and restart the worker only if the
   spend is expected. Check *Monitor -> Usage* in the console first.
3. The figure is cached for 60 s, so a raised cap takes effect within a minute.
4. The `/twilio/usage` alarm is separate and only logs (`budget.carrier_cap`);
   it never stops a call, and a lane with no `PUBLIC_HOSTNAME` has no alarm at
   all while the cap above still applies.

---

## SMS & MMS Not Receiving or Replying

Symptom: A user texts the active number and gets no response, or texts an image that isn't recognized.

1. **Carrier Webhook (Twilio).** In the Twilio Console under *Phone Numbers -> Manage -> Active Numbers -> +1 (908) 774-8864*, verify the Messaging webhook:
   - **A MESSAGE COMES IN**: `Webhook`
   - **URL**: `https://173-255-235-130.sslip.io/sms/incoming` (or current lane domain)
   - **HTTP METHOD**: `HTTP POST`
   Check *Monitor -> Logs -> Messaging* for incoming SMS error codes (e.g. 11200 HTTP retrieval failure).

2. **Caddy Reverse Proxy.** Confirm Caddy is routing `/sms/*` and `/media/*` to `127.0.0.1:8080`:
   `docker logs --since 15m phone-pal-caddy-1`
   Ensure Caddyfile includes:
   ```caddy
   handle /media/* { reverse_proxy 127.0.0.1:8080 }
   handle /sms/* { reverse_proxy 127.0.0.1:8080 }
   ```

3. **Signature.** Twilio's log shows `403` for every message: the worker could not
   verify `X-Twilio-Signature`. `docker logs --since 15m phone-pal-worker-1 | grep sms.webhook_rejected`
   gives the reason — `no_auth_token` means `TWILIO_AUTH_TOKEN` is empty in
   `worker.env`; `bad_signature` almost always means `RT_SMS_WEBHOOK_URL` is not
   character-for-character the URL in the Twilio console (scheme, host, path,
   query string). Set it and restart the worker. Never work around a refusal
   by removing the check.

4. **Worker Webhook Handler.** Verify incoming hits on the worker:
   `docker logs --since 15m phone-pal-worker-1 | grep rt-sms-inbound`
   Look for `[rt-sms-inbound] Received message from ***NNNN` (last four digits;
   the body is never logged) and `[rt-sms-inbound] Replying to ***NNNN`.
   `recorded, not replying: send_sms is not enabled` means `RT_SMS_ENABLED` is
   off — the text is kept for the next call but no reply is sent, by design.
   If `Error generating SMS reply` appears, verify `GOOGLE_API_KEY` is present and has quota.

5. **In-Call SMS Synchronization.** If texts sent during a live call are not known to the voice agent:
   - Verify the ledger: `docker exec phone-pal-worker-1 ls -la /tmp/rt_sms/` (or
     `RT_SMS_LEDGER_DIR`). Files are named by `phone_hash`, never by the number;
     find a caller's file with
     `python -c "import rt_prefs; print(rt_prefs.phone_hash('+1...'))"` inside
     the container.
   - Check in-call injection logs: `docker logs --since 15m phone-pal-worker-1 | grep "Injected incoming SMS"`
   - She does not announce a text unprompted: the live model only sees it when
     it calls `recall_earlier`, and in the post-call extraction. Ask her "did I
     text you?" to confirm the path.

---

## Forget-me request

`python scripts/reset_caller.py <number>` (add `--test` for the test lane;
it targets the dev lane by default and makes you retype the project ref)
calls `rt_forget_caller`, which archives then deletes the caller (migration
19 soft-delete). `rt_restore_caller` exists for the mistaken-request case
within 24 hours (next section); `rt_purge_forgotten` is run hourly by the
scheduler's housekeeping loop to make the archive expire. Never hand-delete
from `rt.callers`. Retention details: [RETENTION.md](RETENTION.md).

---

## Undo an erase within 24h

`rt_forget_caller` archives every row into `rt.forgotten_archive` before it
deletes (migration 19), and `rt_restore_caller` writes the **newest** forget
batch back as long as it is under 24 hours old (migration 21). After that
`rt_purge_forgotten` has expired the archive and nothing comes back.

1. **Get the caller's hash.** It is the keyed HMAC of the E.164 number under
   the lane's pepper — the same value `scripts/reset_caller.py` computes as
   `phone_hash(<number>)` after loading `.env.local` and then
   `deploy/worker.env`. Reproduce exactly that, from `worker/`:

   ```bash
   python -c "from dotenv import load_dotenv; load_dotenv('.env.local'); \\
     load_dotenv('../deploy/worker.env'); from rt_prefs import phone_hash; \\
     print(phone_hash('+15551234567'))"
   ```

   A hash computed under a different pepper matches nothing and the restore
   returns `0`.
2. **Run the restore as the service role.** `EXECUTE` on `rt_restore_caller`
   is revoked from `PUBLIC`, `anon`, and `authenticated` (migration 09/21),
   so run it from the Supabase SQL editor (which uses the service role) or
   with the Management API token via `python sql_push.py --file <file.sql>`:

   ```sql
   SELECT rt_restore_caller('<hash>');
   ```

   It returns the number of rows written back. `0` means no batch inside the
   24-hour window for that hash (wrong pepper, wrong lane, or too late). A
   live row — the caller rang back in the meantime — is never overwritten.
3. Only the newest batch is restored; an earlier erase of the same caller
   stays gone. Do not re-run `rt_forget_caller` "to clean up" first — that
   creates a newer, emptier batch and hides the one you want.

---

## Deploy ordering

1. Apply migrations 18-21 (`python scripts/migrate.py --apply`) **before**
   shipping the worker that references them. Job scheduling fails closed on
   `rt_count_jobs_today`.
2. Set `RT_PHONE_HASH_PEPPER` in `worker.env` **before** enabling
   `RT_REQUIRE_PEPPER=1`, or `/ready` fails and autoheal restart-loops the
   container. The pepper is a one-way door: see
   [ADR 0002](adr/0002-phone-hash-pepper.md).
3. `docker compose pull && docker compose up -d` — images are pinned, so a
   version bump is a reviewed compose change, never a silent `:latest` pull.
4. Ad-hoc SQL against a prod ref (one listed in `RT_PROD_SUPABASE_REFS`, or
   a worker whose `AGENT_NAME` is a `config.PROD_AGENT_NAMES` name) goes
   through `sql_push.py`, which runs **read-only** there: any statement that
   writes is refused unless you pass both `--confirm <ref>` and
   `--i-know-this-is-prod`, and a wipe (`--wipe`, `TRUNCATE`,
   `rt_wipe_all_data`) is never accepted on prod even with both flags.
   `scripts/bootstrap_test_db.py` refuses prod refs outright — there is no
   flag; seed a test project instead.
