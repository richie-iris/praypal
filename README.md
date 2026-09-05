# Phone-Pal Platform `v5.3 (ALPHA-1a)`

A voice companion reached by dialing a real phone number. No app, no account, no scrolling. She answers, speaks naturally with sub-second turn-taking, and remembers callers across conversations.

Built with **Gemini Live** (Speech-to-Speech), **LiveKit** (WebRTC & SIP media server), and **Supabase / PostgreSQL** (long-term memory and caller state). Governed by an autonomous multi-agent swarm detailed in [`AGENTS.md`](./AGENTS.md).

---

## Architecture Overview


```
                                  INBOUND CALL (PSTN)
                                           │
                                           ▼
                                 Twilio Elastic SIP Trunk
                                           │
                                           ▼
                              `sip` (livekit/sip, pinned)
                                           │
                                           ▼
                        `livekit` (livekit-server, WebRTC media)
                                           │
                        ┌──────────────────┴──────────────────┐
                        ▼                                     ▼
             `worker` container (agent.py)            Live Audio Stream
             - Pre-call Context Hydrator              - Bidirectional WebRTC
             - Gemini 3.1 Flash Live Session          - Low-latency Audio Frames
             - Function Tool Invocations              - Earcon Sound Cues
             - ScamGuard & Privacy Shields            - 3-Way Conferencing & Bridges
                        │
       ┌────────────────┴────────────────────────┐
       ▼                                         ▼
PostgreSQL / Supabase                   `scheduler` container
(Long-Term Persistent State)            (rt_scheduler.py)
- Caller Profile & Rules                - Real Outbound Calls (Natural Language)
- Fact Store & Supersession             - Future SMS / Email
- Call Traces & COGS Tracking           - Recovery, Queue Healing, Retention Purges

Compose services (deploy/docker-compose.yml), seven in all:
  redis · livekit · sip · caddy · worker · scheduler   (network_mode: host)
  autoheal                                             (network_mode: none)
```

---

## Directory Layout

| Path | Description |
|---|---|
| [`worker/`](./worker) | Core Python AI worker, prompt pipelines, voice tools, and state machines |
| [`worker/sql/`](./worker/sql) | Sequential database schema migrations (`01-schema.sql` through `23-kb-store.sql`; `10` is a reserved placeholder) |
| [`worker/tests/`](./worker/tests) | Standard `pytest` unit test suite (offline, mocked LLM & pipeline; includes human dynamics, cadence, and doc-drift checks) |
| [`worker/harness/`](./worker/harness) | End-to-end conversation simulation, scale, and call replay test harnesses |
| [`worker/scripts/`](./worker/scripts) | Operator CLI tools: migration manager, live call console, watchdog, cost reports |
| [`deploy/`](./deploy) | Docker Compose definitions, Caddy templates, and host provisioning scripts |
| [`deploy/provisioning/`](./deploy/provisioning) | Carrier (Twilio) setup, SIP trunks, firewall, and multi-lane automation |
| [`sites/`](./sites) | Static web client and marketing portal (public assets only) |
| [`kb/`](./kb) | Clinical trial knowledge base: governance rules, study protocol docs, and playbooks |
| [`docs/`](./docs) | [`RUNBOOK.md`](./docs/RUNBOOK.md), architecture decision records in [`docs/adr/`](./docs/adr), and internal tooling such as [`docs/internal/system_prompt_viewer.html`](./docs/internal/system_prompt_viewer.html) (the system-prompt viewer; not served by `sites/`) |
| [`AGENTS.md`](./AGENTS.md) | Autonomous agent swarm architecture, agent boundaries, invariants, and developer guide |
| [`.github/`](./.github) | CI workflow and [`CODEOWNERS`](./.github/CODEOWNERS) (every PR needs the platform owner's review) |


---

## The Worker Architecture

| Module | Purpose |
|---|---|
| [`agent.py`](./worker/agent.py) | LiveKit agent entry point, Gemini Live session management, audio stream handling, lifecycle orchestrator |
| [`rt_tools.py`](./worker/rt_tools.py) | In-call `@function_tool` declarations, memory/db execution, calendar/email, SMS, web search, and goal management |
| [`rt_prompts.py`](./worker/rt_prompts.py) | Dynamic greetings, onboarding step progression, context window fitting, and duplicate turn suppression |
| [`rt_audio.py`](./worker/rt_audio.py) | Audio frame streaming, earcon sound cues, WAV caching, and clip playback |
| [`rt_shield.py`](./worker/rt_shield.py) | ScamGuard fraud scanning, speaker isolation, panic phrase detection, and farewell intent classifiers |
| [`rt_cadence.py`](./worker/rt_cadence.py) | 3-tier call duration & conversational cadence lifecycle manager (20m soft-wrap, 30m firm-wrap, 35m hard-cap) |
| [`rt_health.py`](./worker/rt_health.py) | Docker health server on `127.0.0.1:8080`: `/health` & `/live` liveness, `/ready` readiness (503 `degraded` when any registered check fails), active session tracking |
| [`rt_hydrator.py`](./worker/rt_hydrator.py) | Context hydration (caller profile, facts, reminders) prior to answering (<20ms) |
| [`rt_postcall_worker.py`](./worker/rt_postcall_worker.py) | Extracts durable memories, facts, and action items after call hangs up |
| [`rt_facts.py`](./worker/rt_facts.py) | Fact extraction with provenance tracking, confidence levels, and supersession |
| [`rt_goals.py`](./worker/rt_goals.py) | In-memory and persistent goal, milestone, and commitment tracking |
| [`rt_recovery.py`](./worker/rt_recovery.py) | Durable post-call queue processing; heals stranded jobs upon restart |
| [`rt_scheduler.py`](./worker/rt_scheduler.py) | Future-dated job processor (outbound reminder calls, follow-ups) with natural language time parsing |
| [`rt_executor.py`](./worker/rt_executor.py) | Async task execution engine between calls |
| [`rt_bridge.py`](./worker/rt_bridge.py) | 3-way conference bridging policy, keypad dialout, and scam signatures |
| [`rt_directory.py`](./worker/rt_directory.py) | Business phone number lookup via grounded search (NPI Registry / Google) |
| [`rt_prefs.py`](./worker/rt_prefs.py) | Caller preferences, phone normalization, and HMAC-SHA256 phone hashing |
| [`rt_capabilities.py`](./worker/rt_capabilities.py) | Feature gating based on present API credentials |
| [`rt_carrier.py`](./worker/rt_carrier.py) | Reads the Twilio account's spend for today and refuses unattended dialling at `TWILIO_DAILY_SPEND_USD`; fails closed when the figure cannot be read |
| [`rt_costs.py`](./worker/rt_costs.py) | Per-call COGS calculation (telephony, LLM tokens, TTS audio) |
| [`rt_logger.py`](./worker/rt_logger.py) | Structured JSON logger with Sentry error monitoring and HTTP log shipping |
| [`rt_obs.py`](./worker/rt_obs.py) / [`obs_contract.py`](./worker/obs_contract.py) | Telemetry contract and metric collectors |
| [`rt_self.py`](./worker/rt_self.py) | Persona definition and behavioral parameters |
| [`rt_trace.py`](./worker/rt_trace.py) | Granular call trace logs persisted to Supabase |
| [`rt_email.py`](./worker/rt_email.py) / [`rt_sms.py`](./worker/rt_sms.py) / [`rt_sms_inbound.py`](./worker/rt_sms_inbound.py) | Email (Resend) dispatcher; Twilio SMS/MMS sending, webhook signature validation and the per-caller ledger; inbound text conversation engine |
| [`rt_timezone.py`](./worker/rt_timezone.py) | Timezone resolution and local time formatting |
| [`rt_patterns.py`](./worker/rt_patterns.py) | Regex pattern matchers for sensitive data and phrases |
| [`rt_http.py`](./worker/rt_http.py) | Resilient HTTP helper with retries and backoff |
| [`config.py`](./worker/config.py) | Environment configuration constants |
| [`scamguard.py`](./worker/scamguard.py) | Background worker for passive fraud analysis |
| [`sql_push.py`](./worker/sql_push.py) | Supabase Management API SQL execution utility; on a prod ref (`RT_PROD_SUPABASE_REFS`, or a `config.PROD_AGENT_NAMES` agent name) it runs read-only unless `--confirm <ref> --i-know-this-is-prod` is passed |
| [`rt_trial.py`](./worker/rt_trial.py) | Clinical trial governance seam; enforces pinned rules, compliance identity, and tool withholding for trial lanes |
| [`rt_pray.py`](./worker/rt_pray.py) | PrayPal spiritual agent seam; manages Divine Pantheon personas, 988 crisis safety shield, syncretic Jesus & Shiva councils, and tool withholding |


---

## Conversational SMS, Multimodal MMS & In-Call Synchronization

Twilio carries texts and voice alike ([ADR 0006](./docs/adr/0006-twilio-single-carrier.md)). The pieces, in the order a text passes through them:

1. **Inbound webhook (`/sms/incoming`, [`rt_health.py`](./worker/rt_health.py))**:
   - Twilio POSTs each text or MMS to `https://<lane-domain>/sms/incoming`. Caddy forwards `/sms/*` and `/media/*` to the worker's loopback port `8080` and nothing else ([`deploy/Caddyfile.template`](./deploy/Caddyfile.template)); `/health` and `/ready` never leave the box.
   - Every POST must carry a valid `X-Twilio-Signature`: HMAC-SHA1 over the webhook URL and the form fields under `TWILIO_AUTH_TOKEN` (`rt_sms.webhook_is_from_twilio`). The URL Twilio signed is `RT_SMS_WEBHOOK_URL`, or is rebuilt from Caddy's forwarded headers when that is empty. Unsigned, forged or tampered requests get `403` and an `sms.webhook_rejected` event; with no auth token configured every request is refused.
   - The handler answers at once with an empty `<Response/>` and hands the message to a worker thread (`rt_sms_inbound.accept_webhook`). Twilio abandons a webhook after 15 s and retries it, so the reply never rides on the HTTP response, and a retried `MessageSid` is acknowledged without being processed again. The server is threaded, so a slow reply never blocks `/ready`.
2. **Reply generation ([`rt_sms_inbound.py`](./worker/rt_sms_inbound.py))**:
   - The caller's bundle (`rt_get_caller_full_bundle`) and the last six texts frame one **Gemini 2.5 Flash** call with Google Search grounding; the reply goes out through the Twilio REST API from the number they texted. Replies obey the same `send_sms` capability gate as the in-call tool: with `RT_SMS_ENABLED` off the text is still recorded and nothing is sent.
   - MMS attachments are fetched only from `api.twilio.com` over https, with the account credentials on that first request alone; redirects to Twilio's CDN are followed without them. Anything over 5 MB or not an image is dropped. A photo sent in the last 15 minutes is carried forward into a follow-up question.
3. **The ledger and in-call synchronization**:
   - The last 30 texts per caller live in `RT_SMS_LEDGER_DIR` (default `/tmp/rt_sms`) as `<phone_hash>.json`, mode `0600` in a `0700` directory, pruned after `RT_SMS_LEDGER_RETENTION_DAYS` (default 30) and removed outright by `forget_me`. The plaintext number never touches the disk; see [RETENTION.md](./docs/RETENTION.md).
   - Calls run in child worker processes, so the ledger is the channel between the webhook and a live call. `_sms_monitor_task` in [`agent.py`](./worker/agent.py) polls it every 800 ms and appends each new inbound text to the transcript under `rt_shield.SMS_ROLE` (`caller (via SMS text): …`). That role never matches the `caller:` prefix the consent and identity guards key on, so a text can never stand in for something said on the line. The live model is not interrupted by a text: it sees it through `recall_earlier` and the post-call extraction, so Iris brings it up when asked rather than unprompted.
4. **Outbound MMS & media server**:
   - Outbound images are served by Caddy at `/media/*` from `worker/media/` and sent as Twilio `MediaUrl` attachments.

---

## Security Model & Protections

The platform implements multi-layer defense-in-depth across telephony, identity, database, and generative AI execution:

1. **Cryptographic Phone Anonymization ([`rt_prefs.py`](./worker/rt_prefs.py))**:
   - Phone numbers are normalized to E.164 and hashed using keyed **HMAC-SHA256** with `RT_PHONE_HASH_PEPPER`. This prevents precomputed rainbow-table and GPU dictionary attacks against caller identifiers.
   - The pepper is **required in prod lanes**: `RT_REQUIRE_PEPPER=1` makes hashing without `RT_PHONE_HASH_PEPPER` raise instead of silently falling back to an unkeyed hash, and the worker's `/ready` check fails so it never answers a call with weak identifiers. The gate fails closed (`config.pepper_required()`: only empty/`0`/`false`/`no`/`off` mean optional) and a placeholder or short pepper counts as missing (`config.pepper_ok()`). The pepper is a one-way door (changing it orphans every stored caller) — see [ADR 0002](./docs/adr/0002-phone-hash-pepper.md).
2. **Database Role Lockdowns ([`09-lock-down-rpcs.sql`](./worker/sql/09-lock-down-rpcs.sql))**:
   - All `rt_*` PostgREST RPCs and `rt.*` schema tables have `EXECUTE` and table access revoked from `PUBLIC`, `anon`, and `authenticated` roles. Only authorized `service_role` workers can execute RPCs.
3. **Voice Prompt Injection & Tool Defenses ([`agent.py`](./worker/agent.py))**:
   - **SMS Isolation**: `send_sms` dispatches exclusively to the authenticated caller's own verified phone number. Third-party number redirection is rejected.
   - **Email Exfiltration Guard**: `send_email` and `send_calendar_invite` dispatch only to the address on file, and `save_email` will only put an address on file that the caller spoke on a `caller:` transcript line (`rt_shield.email_spoken_by_caller`); `rt_email.send_email` refuses outright without a matching `verified_email`. See [ADR 0005](./docs/adr/0005-email-recipient-policy.md).
   - **Stored Rules Re-validated at Render Time**: caller rules and persona directives must pass `rt_shield.rule_text_allowed` (length, injection phrases, tool names, disclosure verbs) and `rt_shield.spoken_by_caller` when written, and `rt_hydrator` re-validates them with `rule_text_allowed` again when it renders the prompt, so a row edited after the write guard (or written by an older worker) is dropped from the prompt instead of rendered.
   - **Memory Wipe Confirmation**: `db_tool` verifies explicit caller spoken intent in the transcript before executing full database purges (`forget_me`).
   - **Dial Policy**: `bridge_call` refuses emergency lines (911, 988), premium rate numbers (900, 976), and international numbers outside US/Canada.
4. **Google API Key Restriction**:
   - `GOOGLE_API_KEY` is sent in a request header (never a URL) to the Gemini Live, TTS, and search-grounding endpoints. The key itself must be restricted in the [Google Cloud console credentials page](https://console.cloud.google.com/apis/credentials): by **API** (Generative Language API only) and by **IP** (the lane host's egress address), so a leaked key is useless from anywhere else and cannot be pointed at other Google APIs.
5. **Localhost Network Isolation ([`rt_health.py`](./worker/rt_health.py))**:
   - Health check endpoints (`/health`, `/ready`) bind strictly to `127.0.0.1` by default, protecting host-networked Docker deployments from public internet port scanning.
   - Payload error messages are sanitized to strip tracebacks and internal connection strings.
   - Guards fail closed (a policy check that cannot evaluate refuses); telemetry fails open (a metrics failure is swallowed, never surfaced as a call failure). See [ADR 0003](./docs/adr/0003-fail-closed-guards.md).
6. **PII Redaction ([`rt_prefs.scrub_ssn`](./worker/rt_prefs.py))**:
   - Regular expressions actively scrub Social Security Numbers from ever being written to memory, reminders, or database tables.

---

## Database Migrations Catalog

Migrations live in [`worker/sql/`](./worker/sql) and are sequentially versioned and tracked via SHA256 checksums:

| Migration | Purpose |
|---|---|
| [`01-schema.sql`](./worker/sql/01-schema.sql) | Core database tables (`rt.callers`, `rt.account_schema_registry`, `rt.reminders`, `rt.call_log`) and initial RPCs |
| [`02-loved-ones-and-admin-rpcs.sql`](./worker/sql/02-loved-ones-and-admin-rpcs.sql) | Adds loved ones columns and administrative read/write RPCs |
| [`03-reminders-metrics-audit.sql`](./worker/sql/03-reminders-metrics-audit.sql) | Reminders tracking, telemetry audit logging, and metric aggregation |
| [`04-cogs-columns.sql`](./worker/sql/04-cogs-columns.sql) | Telephony, token, and TTS audio COGS financial accounting columns |
| [`05-first-contact-date.sql`](./worker/sql/05-first-contact-date.sql) | First contact timestamps for onboarding and relationship tenure |
| [`06-surname.sql`](./worker/sql/06-surname.sql) | Last name / surname storage and dedicated RPC mutators |
| [`07-call-trace.sql`](./worker/sql/07-call-trace.sql) | Granular call event tracing table (`rt.call_traces`) for live console |
| [`08-forget-me.sql`](./worker/sql/08-forget-me.sql) | GDPR-compliant `rt_forget_caller` atomic purge RPC |
| [`09-lock-down-rpcs.sql`](./worker/sql/09-lock-down-rpcs.sql) | Security sweep revoking `EXECUTE` privileges from anon and granting only to service_role |
| [`10-reserved.sql`](./worker/sql/10-reserved.sql) | Reserved placeholder so the sequence stays contiguous (`migrate.py` refuses gaps) |
| [`11-durable-turns-and-postcall-queue.sql`](./worker/sql/11-durable-turns-and-postcall-queue.sql) | Post-call durable processing queue and transcript turn persistence |
| [`12-facts-store.sql`](./worker/sql/12-facts-store.sql) | Structured fact storage (`rt.facts`) with provenance, confidence, and categories |
| [`13-facts-in-bundle.sql`](./worker/sql/13-facts-in-bundle.sql) | Integrates facts into the single-roundtrip full context bundle RPC |
| [`14-wipe-covers-everything.sql`](./worker/sql/14-wipe-covers-everything.sql) | Extends `rt_wipe_all_data` and purge routines to cover facts and queues |
| [`15-scheduled-jobs.sql`](./worker/sql/15-scheduled-jobs.sql) | Asynchronous scheduled jobs table (`rt.scheduled_jobs`) and claim RPCs |
| [`16-outbound-safety.sql`](./worker/sql/16-outbound-safety.sql) | Outbound call safety: atomic supersession and stale job reclamation |
| [`17-forget-me-covers-jobs.sql`](./worker/sql/17-forget-me-covers-jobs.sql) | Ensures caller purges cleanly cancel pending scheduled outbound jobs |
| [`18-pin-search-path.sql`](./worker/sql/18-pin-search-path.sql) | Pins `search_path` on every `SECURITY DEFINER` `rt_*` function (search-path hijack defense) |
| [`19-forget-me-soft-delete.sql`](./worker/sql/19-forget-me-soft-delete.sql) | `rt.forgotten_archive` archive-before-delete, `rt_restore_caller`, and `rt_purge_forgotten` expiry |
| [`20-job-caps-and-retention.sql`](./worker/sql/20-job-caps-and-retention.sql) | `rt_count_jobs_today` per-caller daily job caps and `rt_purge_old_transcripts` retention |
| [`21-forget-atomic-restore-newest-retention-all.sql`](./worker/sql/21-forget-atomic-restore-newest-retention-all.sql) | `rt_forget_caller` archives and deletes in one statement per table, `rt_restore_caller` restores only the newest forget batch, and `rt_purge_old_transcripts` covers every transcript copy (`rt.calls`, turn events, `rt.callers.last_transcript`) |
| [`22-readonly-exec.sql`](./worker/sql/22-readonly-exec.sql) | `rt_readonly_exec` and the guards that refuse write statements, `EXPLAIN` and `SHOW` against a prod ref |
| [`23-kb-store.sql`](./worker/sql/23-kb-store.sql) | The protocol knowledge base in its own `kb` schema: documents, chunks with `halfvec(1536)` embeddings, hybrid and lexical-only search, pinned prompt builds, and the retrieval log. Deliberately outside `rt` so a forget-me erases a caller, never the study |

### Deploy ordering

* Apply migrations `18`-`21` (`python scripts/migrate.py --apply`) **before** shipping this worker: job scheduling fails closed on `rt_count_jobs_today`, so a worker ahead of its schema refuses every reminder, and the scheduler's hourly transcript purge only covers every copy once `21` is in.
* Set `RT_PHONE_HASH_PEPPER` in `worker.env` **before** enabling `RT_REQUIRE_PEPPER=1`, or `/ready` returns 503 and autoheal will restart-loop the container.

---

## Testing & Quality Assurance Harnesses

The repository includes four complementary testing layers:

```
┌────────────────────────────────────────────────────────────────────────┐
│ 1. Standard Unit Tests (pytest tests/ -v)                               │
│    - Fast, offline, mocked LLM & pipeline unit tests                   │
├────────────────────────────────────────────────────────────────────────┤
│ 2. Product Contract Harness (harness/phone_pal_test_suite.py)          │
│    - End-to-end memory retention, persona rules, and adversarial tests │
├────────────────────────────────────────────────────────────────────────┤
│ 3. Scale & Concurrency Suite (harness/scale_suite.py)                  │
│    - Synthetic multi-caller population isolation & latency benchmarks  │
├────────────────────────────────────────────────────────────────────────┤
│ 4. Historic Call Replay Suite (harness/replay_suite.py)                 │
│    - Real-call invariant validation against historic event logs        │
└────────────────────────────────────────────────────────────────────────┘
```

### Running the Test Suites

```bash
# 1. Run standard unit tests & security checks
pytest tests/ -v

# 2. Run documentation drift & link validation
pytest tests/test_doc_drift.py -v

# 3. Run scale & concurrency benchmark (25 synthetic callers)
python harness/scale_suite.py --callers 25

# 4. Run historic replay test suite
python harness/replay_suite.py --fixtures

# 5. Run full end-to-end product contract harness
python harness/phone_pal_test_suite.py
```

---

## Operator CLI Tooling

Operator and maintenance scripts live in [`worker/scripts/`](./worker/scripts):

| Script | Purpose |
|---|---|
| [`migrate.py`](./worker/scripts/migrate.py) | Automated migration manager with checksum validation (`--status`, `--apply`, `--dry-run`) |
| [`call_console.py`](./worker/scripts/call_console.py) | Live real-time call telemetry and transcript streaming console |
| [`check_migrations.py`](./worker/scripts/check_migrations.py) | Validates live PostgREST RPC signatures across dev/test/prod lanes |
| [`kb_load.py`](./worker/scripts/kb_load.py) | Loads `kb/` into the knowledge base: validates frontmatter, substitutes `{{TOKEN}}` placeholders, chunks at `##`, embeds, and builds the pinned governance prompt. Fails closed on any violation (`--check` to validate without writing) |
| [`check_observability.py`](./worker/scripts/check_observability.py) | Telemetry and health integrity checker |
| [`cost_report.py`](./worker/scripts/cost_report.py) | Generates COGS breakdown (telephony + LLM tokens + TTS audio) per lane |
| [`watchdog.py`](./worker/scripts/watchdog.py) | Background daemon monitoring stranded jobs and worker responsiveness |
| [`bootstrap_test_db.py`](./worker/scripts/bootstrap_test_db.py) | Seeds clean testing data and fixtures into a test Supabase instance; refuses prod refs outright |
| [`reset_caller.py`](./worker/scripts/reset_caller.py) | Wipes one caller (`<number>`, optional `--test` for the test lane) so the next call reads as brand-new; undo within 24h with `rt_restore_caller` (see the [RUNBOOK](./docs/RUNBOOK.md)) |
| [`scrub_rules.py`](./worker/scripts/scrub_rules.py) | Re-runs `rt_shield.rule_text_allowed` over every stored caller rule, persona directive, and skills-registry value; dry run by default, `--apply` nulls what today's shield refuses, and a prod ref needs `--confirm <ref>` even for the dry run |
| [`backfill_facts.py`](./worker/scripts/backfill_facts.py) | Backfills structured facts from historic call transcripts |
| [`run_dev_worker.sh`](./worker/scripts/run_dev_worker.sh) | Local development worker startup script |
| [`run_test_worker.sh`](./worker/scripts/run_test_worker.sh) | Test lane worker startup script |
| [`run_scamguard_worker.sh`](./worker/scripts/run_scamguard_worker.sh) | ScamGuard fraud monitoring worker startup script |
| [`heygen_generate.py`](./worker/scripts/heygen_generate.py) | Automates HeyGen video manifesto production using the founder portrait and script |


Prod refs are special for every tool that writes SQL: [`sql_push.py`](./worker/sql_push.py) runs read-only against a ref listed in `RT_PROD_SUPABASE_REFS` (or when `AGENT_NAME` is a `config.PROD_AGENT_NAMES` name) unless `--confirm <ref> --i-know-this-is-prod` is passed (every payload is wrapped in a `READ ONLY` transaction server-side, and a wipe — `--wipe`, `TRUNCATE`, `rt_wipe_all_data` — is never accepted on prod), and `bootstrap_test_db.py` refuses prod refs with no override at all.

---

## The Infrastructure Stack

Seven containers managed via [`deploy/docker-compose.yml`](./deploy/docker-compose.yml):

```
redis → livekit → sip → caddy → worker → scheduler   (network_mode: host)
autoheal                                             (network_mode: none — Docker socket only)
```

* **Network**: `network_mode: host` for the six media/app services (required for WebRTC 10,000-port UDP media ranges); `autoheal` runs with `network_mode: none` because it only needs the Docker socket.
* **Ports** (all loopback, see [ADR 0001](./docs/adr/0001-host-networking.md)): `8080` is the Docker health server (`rt_health.py`, override with `RT_DOCKER_HEALTH_PORT`); `8082` is the LiveKit worker's own HTTP port (`RT_HEALTH_PORT`, used by the LiveKit agent framework, loopback only).
* **Health Checks**: `GET http://127.0.0.1:8080/health` and `/live` are liveness probes and always return 200 (with a `ready` flag). `GET /ready` returns 200 when every registered readiness check passes and **503 with `"status": "degraded"`** listing the failed check names otherwise. The compose healthcheck probes `/ready`; the `autoheal` sidecar (`willfarrell/autoheal`) restarts any container labelled `autoheal=true` once Docker marks it unhealthy, in addition to the `restart: unless-stopped` policy. Read the restart count with `docker inspect --format '{{.RestartCount}}' <container>` (`docker compose ps` shows status, not the count).
* **Logs & retention**: container logs rotate at 50 MB x 5 files (`json-file` driver); transcripts are purged after `RT_TRANSCRIPT_RETENTION_DAYS` (default 30). See [`docs/RETENTION.md`](./docs/RETENTION.md).
* **Images**: every image in the compose file and Dockerfile carries an explicit version tag (no `:latest`); Dependabot proposes bumps.

---

## Standing Up a Lane

To provision a fresh server from scratch:

```bash
cp deploy/provisioning/lane.env.example deploy/provisioning/lane.env
$EDITOR deploy/provisioning/lane.env

# Dry run (prints plan)
bash deploy/provisioning/stand-up-lane.sh

# Apply provisioning
APPLY=1 bash deploy/provisioning/stand-up-lane.sh
```

| Provisioning Script | Purpose |
|---|---|
| [`stand-up-lane.sh`](./deploy/provisioning/stand-up-lane.sh) | Master script executing host setup in 8 ordered phases |
| [`render-config.sh`](./deploy/provisioning/render-config.sh) | Generates `worker.env`, `livekit.yaml`, `sip.yaml`, `Caddyfile`; forwards `RT_PHONE_HASH_PEPPER` from `lane.env` into `worker.env` and refuses to render without it unless the lane opts out with `RT_REQUIRE_PEPPER=0` (same fail-closed reading as `config.pepper_required()`) |
| [`firewall-setup.sh`](./deploy/provisioning/firewall-setup.sh) | Cloud firewall rule configuration |
| [`twilio-setup.sh`](./deploy/provisioning/twilio-setup.sh) | Twilio Elastic SIP Trunk, origination to this box, termination credentials, number routing (voice + SMS webhook), and the daily spend alarm |
| [`recreate-routing.sh`](./deploy/provisioning/recreate-routing.sh) | LiveKit inbound SIP trunk and SIP dispatch rules |
| [`outbound-trunk.sh`](./deploy/provisioning/outbound-trunk.sh) | LiveKit outbound SIP trunk configuration |
| [`capture-lane.sh`](./deploy/provisioning/capture-lane.sh) | Captures active configuration into `<lane>-inventory.md` |

---

## External Services & Credentials

| Service | Role | Missing Fallback |
|---|---|---|
| **Google (Gemini)** | Realtime speech-to-speech model, TTS clips, and search grounding (grounded answers name their own source, e.g. a quote "per Yahoo Finance"; the worker makes no direct call to any finance API) | Worker cannot process audio |
| **Supabase** | Persistent storage (caller profiles, facts, call traces, scheduled jobs) | No persistence |
| **LiveKit** | WebRTC + SIP media handling | No telephony or audio bridge |
| **Twilio** | Phone numbers, inbound SIP routing, outbound PSTN calls, SMS/MMS, and the daily spend figure the worker caps against | Phone does not ring; texts refused; unattended dialling refused |
| **Resend** | Transactional email dispatch | Email tools gracefully omitted |
| **Sentry** | Centralized error monitoring (`SENTRY_DSN`) | Logs errors to stderr |
| **OpenStreetMap / Overpass / Nominatim** | Map & geographic business discovery in `rt_directory.py` | Falls back to Google Places / search grounding |
| **Google Places** | Grounded place & business lookup in `rt_directory.py` | Web search fallback |

---

## CI/CD & Secret Protection

* **Pre-commit Hooks**: [`.pre-commit-config.yaml`](./.pre-commit-config.yaml) scans all commits for hardcoded secrets (API keys, JWT tokens) and runs `ruff` lint (`--fix --exit-non-zero-on-fix`, same rule set as CI). `ruff-format` is a manual-stage hook (`pre-commit run --hook-stage manual ruff-format`), not run on commit.
* **GitHub Actions CI**: [`.github/workflows/ci.yml`](./.github/workflows/ci.yml) executes linter checks, `pytest` unit tests, and documentation drift checks on all PRs and pushes to `main`.
* **Code Owners**: [`.github/CODEOWNERS`](./.github/CODEOWNERS) routes every PR to the platform owner for review.

---

## Operations & Decision Records

* [`docs/RUNBOOK.md`](./docs/RUNBOOK.md) — ordered checklists for pages (silent line, restart loops, scheduled calls not going out, forget-me).
* [`docs/adr/`](./docs/adr) — architecture decision records: host networking, phone-hash pepper, fail-closed guards, TCPA calling windows, email recipient policy.
* [`docs/RETENTION.md`](./docs/RETENTION.md) — what caller data is kept where, for how long, and which job purges it.
* [`docs/internal/system_prompt_viewer.html`](./docs/internal/system_prompt_viewer.html) — local HTML viewer for the assembled system prompt (open in a browser; not deployed).
