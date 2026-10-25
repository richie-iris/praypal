# PrayPal Telephony Platform

Autonomous spiritual telephony and messaging sanctuary reached by dialing `+1 862-358-8238` or texting. Callers speak directly with the Divine Pantheon (Jesus, Krishna, Moses, Noah, Divine Mother, Shiva, God Almighty) with sub-second turn-taking, authentic ambient sacred music, and long-term memory across calls.

Built on **Gemini Live** (speech-to-speech), **LiveKit** (SIP media server), and **Supabase / PostgreSQL** (persistent memory). Governed by [`AGENTS.md`](./AGENTS.md). Telephone (+1 862-358-8238) and SMS/MMS spiritual sanctuary.

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
             - Context Hydrator & Shields             - Bidirectional WebRTC
             - Gemini 3.1 Flash Live Session          - Low-latency Audio
             - Divine Pantheon & Tools                - Curated Sacred Stream
                        │
       ┌────────────────┴────────────────────────┐
       ▼                                         ▼
PostgreSQL / Supabase                   `scheduler` container
(Long-Term Persistent State)            (rt_scheduler.py)
- Caller Profile & Rules                - Real Outbound Calls (Natural Language)
- Sacred Memory & COGS                  - Devotions, SMS / Email
- Facts & Answered Prayers              - Autoheal & Retention Purges

Compose services (deploy/docker-compose.yml), seven in all:
  redis · livekit · sip · caddy · worker · scheduler   (network_mode: host)
  autoheal                                             (network_mode: none)
```

---

## Directory Layout

| Path | Description |
|---|---|
| [`worker/`](./worker) | Core Python worker, prompt pipelines, voice tools, and state machines |
| [`worker/sql/`](./worker/sql) | Sequential migrations (`01-schema.sql` through `25-praypal-atrium-and-streaks.sql`; `10` is reserved) |
| [`worker/tests/`](./worker/tests) | Standard `pytest` unit test suite (offline, mocked LLM & pipeline; cadence, doc-drift checks) |
| [`worker/harness/`](./worker/harness) | End-to-end conversation simulation, scale, and call replay test harnesses |
| [`worker/scripts/`](./worker/scripts) | Operator CLI tools: migration manager, live call console, watchdog, cost reports |
| [`deploy/`](./deploy) | Docker Compose definitions, Caddy templates, and host provisioning scripts |
| [`deploy/provisioning/`](./deploy/provisioning) | Carrier (Twilio) setup, SIP trunks, firewall, and multi-lane automation |
| [`sites/`](./sites) | Static web companion (public visual sanctuary, beads, phonetics) |
| [`assets/`](./assets) | Canonical sacred art, pantheon photography, MMS blessings |
| [`docs/`](./docs) | [`RUNBOOK.md`](./docs/RUNBOOK.md), architecture decision records in [`docs/adr/`](./docs/adr), and internal tooling such as [`docs/internal/system_prompt_viewer.html`](./docs/internal/system_prompt_viewer.html) |
| [`AGENTS.md`](./AGENTS.md) | Autonomous agent swarm architecture, operational financial model, and invariants |
| [`.github/`](./.github) | CI workflow and [`CODEOWNERS`](./.github/CODEOWNERS) (every PR needs platform owner's review) |

---

## The Worker Architecture

| Module | Purpose |
|---|---|
| [`agent.py`](./worker/agent.py) | LiveKit agent entry point, Gemini Live session management, audio stream handling |
| [`rt_tools.py`](./worker/rt_tools.py) | In-call `@function_tool` declarations, memory/db execution, calendar/email, SMS, web search |
| [`rt_prompts.py`](./worker/rt_prompts.py) | Dynamic greetings, context window fitting, and duplicate turn suppression |
| [`rt_audio.py`](./worker/rt_audio.py) | Audio frame streaming, earcon sound cues, WAV caching, and clip playback |
| [`rt_shield.py`](./worker/rt_shield.py) | Fraud scanning, speaker isolation, panic phrase detection, and farewell intent classifiers |
| [`rt_cadence.py`](./worker/rt_cadence.py) | Call duration & conversational cadence manager (5m free Grace tier, 20m Devotion tier) |
| [`rt_health.py`](./worker/rt_health.py) | Docker health server on `127.0.0.1:8080`: `/health`, `/live`, and `/ready` (503 degraded on failure) |
| [`rt_hydrator.py`](./worker/rt_hydrator.py) | Context hydration (caller profile, facts, reminders) prior to answering (<20ms) |
| [`rt_postcall_worker.py`](./worker/rt_postcall_worker.py) | Extracts durable memories, facts, and prayer intentions after call hangs up |
| [`rt_facts.py`](./worker/rt_facts.py) | Fact extraction with provenance tracking, confidence levels, and supersession |
| [`rt_goals.py`](./worker/rt_goals.py) | In-memory and persistent goal, milestone, and commitment tracking |
| [`rt_recovery.py`](./worker/rt_recovery.py) | Durable post-call queue processing; heals stranded jobs upon restart |
| [`rt_scheduler.py`](./worker/rt_scheduler.py) | Future-dated job processor (outbound reminder calls, follow-ups) with natural language time parsing |
| [`rt_executor.py`](./worker/rt_executor.py) | Async task execution engine between calls |
| [`rt_bridge.py`](./worker/rt_bridge.py) | 3-way conference bridging policy, keypad dialout, and scam signatures |
| [`rt_directory.py`](./worker/rt_directory.py) | Business phone number lookup via grounded search (NPI Registry / Google) |
| [`rt_prefs.py`](./worker/rt_prefs.py) | Caller preferences, phone normalization, and HMAC-SHA256 phone hashing |
| [`rt_capabilities.py`](./worker/rt_capabilities.py) | Feature gating based on present API credentials |
| [`rt_carrier.py`](./worker/rt_carrier.py) | Twilio account spend tracking refusing dialling at `TWILIO_DAILY_SPEND_USD` |
| [`rt_costs.py`](./worker/rt_costs.py) | Per-call COGS calculation ($0.196/min blended: telephony, LLM tokens, TTS audio) |
| [`rt_logger.py`](./worker/rt_logger.py) | Structured JSON logger with Sentry error monitoring and HTTP log shipping |
| [`rt_obs.py`](./worker/rt_obs.py) / [`obs_contract.py`](./worker/obs_contract.py) | Telemetry contract and metric collectors |
| [`rt_self.py`](./worker/rt_self.py) | Persona definition and behavioral parameters |
| [`rt_trace.py`](./worker/rt_trace.py) | Granular call trace logs persisted to Supabase |
| [`rt_email.py`](./worker/rt_email.py) / [`rt_sms.py`](./worker/rt_sms.py) / [`rt_sms_inbound.py`](./worker/rt_sms_inbound.py) | Email (Resend) dispatcher; Twilio SMS/MMS sending, signature verification, per-caller ledger, inbound text engine |
| [`rt_timezone.py`](./worker/rt_timezone.py) | Timezone resolution and local time formatting |
| [`rt_patterns.py`](./worker/rt_patterns.py) | Regex pattern matchers for sensitive data and phrases |
| [`rt_http.py`](./worker/rt_http.py) | Resilient HTTP helper with retries and backoff |
| [`config.py`](./worker/config.py) | Environment configuration constants |
| [`sql_push.py`](./worker/sql_push.py) | Supabase Management API SQL execution utility; on prod refs it runs read-only unless `--confirm <ref> --i-know-this-is-prod` is passed |
| [`rt_pray.py`](./worker/rt_pray.py) | PrayPal spiritual agent seam; Divine Pantheon personas, devotionals, syncretic councils |
| [`rt_phonetics.py`](./worker/rt_phonetics.py) | Synchronized phonetic enunciation engine (Devanagari, Hebrew, Arabic, Greek) |
| [`rt_sacred_stream.py`](./worker/rt_sacred_stream.py) | Curated zero-COGS sacred audio streaming pipeline (Tanpura, Cathedral Organ, Ney Flute, Singing Bowl) |
| [`rt_chron_watcher.py`](./worker/rt_chron_watcher.py) | Autonomous 15-minute chron watcher; monitors disaster alerts, liturgical calendar, prayer circle wall |
| [`rt_pray_bank.py`](./worker/rt_pray_bank.py) | The Prayer Bank & "Tokens to Blessings" micro-economy; perpetual candles, elder devotions, Reciprocity Grace Fund |
| [`rt_devotion_scheduler.py`](./worker/rt_devotion_scheduler.py) | Daily scheduled morning & evening devotion dispatcher across faith traditions |
| [`rt_pray_circle.py`](./worker/rt_pray_circle.py) | Multi-party PSTN family prayer circles and conference PIN bridging with Divine Guide moderation |
| [`rt_pray_polyglot.py`](./worker/rt_pray_polyglot.py) | Real-time polyglot prayer bridge; real-time translation across Hindi, Gujarati, Tamil, Spanish, Arabic, English |
| [`rt_pray_holiday.py`](./worker/rt_pray_holiday.py) | Illuminated holiday cards & sacred audio devotional dispatcher for multifaith festivals via MMS |
| [`rt_caller_export.py`](./worker/rt_caller_export.py) | Cryptographic spiritual journey export & sacred journal with HMAC authenticity seals |
| [`rt_budget_guard.py`](./worker/rt_budget_guard.py) | In-call real-time budget monitor & serene soft-wrap engine at tier caps |
| [`rt_grief_radar.py`](./worker/rt_grief_radar.py) | Proactive bereavement compassion radar & anti-platitude guard with memorial anniversary logging |
| [`rt_liturgical.py`](./worker/rt_liturgical.py) | Multi-faith liturgical calendar & sacred time engine computing holy days and seasons without drift |
| [`rt_syncretic_council.py`](./worker/rt_syncretic_council.py) | Syncretic divine council dialogue synthesizer uniting dual-tradition wisdom with blended acoustic pairings |

---

## Conversational SMS & Multimodal MMS

Twilio carries texts and voice alike ([ADR 0006](./docs/adr/0006-twilio-single-carrier.md)):
1. **Inbound Webhook (`/sms/incoming`, [`rt_health.py`](./worker/rt_health.py))**: Twilio POSTs each text or MMS to `https://<lane-domain>/sms/incoming`. Caddy forwards `/sms/*` and `/media/*` to loopback `8080` ([`deploy/Caddyfile.template`](./deploy/Caddyfile.template)); `/health` and `/ready` never leave the box. Validated with `X-Twilio-Signature` under `TWILIO_AUTH_TOKEN` (`rt_sms.webhook_is_from_twilio`). Handled asynchronously by worker threads ([`rt_sms_inbound.py`](./worker/rt_sms_inbound.py)).
2. **Reply Generation & Ledger**: Gemini 2.5 Flash generates grounded replies. Texts are saved to `RT_SMS_LEDGER_DIR` (`<phone_hash>.json`), pruned after 30 days, and purged on `forget_me` ([`docs/RETENTION.md`](./docs/RETENTION.md)). Outbound MMS images stream from `/media/*`.

---

## Security Model & Protections

1. **Cryptographic Phone Anonymization ([`rt_prefs.py`](./worker/rt_prefs.py))**: Keyed HMAC-SHA256 hashing with `RT_PHONE_HASH_PEPPER`. The pepper is **required in prod lanes**: `RT_REQUIRE_PEPPER=1` makes unpeppered starts fail loudly so the worker never answers a call with weak identifiers ([ADR 0002](./docs/adr/0002-phone-hash-pepper.md)).
2. **Database Role Lockdowns ([`09-lock-down-rpcs.sql`](./worker/sql/09-lock-down-rpcs.sql))**: `EXECUTE` revoked from `PUBLIC` and `anon`; granted only to `service_role`.
3. **Voice Prompt Injection & Tool Defenses ([`agent.py`](./worker/agent.py))**: `send_sms` restricted to caller's number; `send_email` requires `verified_email` on a `caller:` line ([ADR 0005](./docs/adr/0005-email-recipient-policy.md)). Stored rules and persona directives are re-validated at render time with `rule_text_allowed` (`rt_shield.py`, `rt_hydrator.py`).
4. **Google API Key Restriction**: `GOOGLE_API_KEY` sent via request header (never URL). The key must be restricted in the [Google Cloud console credentials page](https://console.cloud.google.com/apis/credentials) by **API** (Generative Language API) and by **IP** (lane egress IP).
5. **Localhost Network Isolation ([`rt_health.py`](./worker/rt_health.py))**: Health endpoints bind strictly to `127.0.0.1:8080`. Guards fail closed ([ADR 0003](./docs/adr/0003-fail-closed-guards.md)).
6. **PII Redaction ([`rt_prefs.py`](./worker/rt_prefs.py))**: Regex scrubs SSNs from memory and database writes.
7. **Strict Devotional Scope**: Dedicated solely to personal prayer, scripture, and contemplation; acute clinical and emergency crisis triage is explicitly out of scope.
8. **Telephony Cadence & Rate Limits ([`rt_cadence.py`](./worker/rt_cadence.py), [`rt_sms_inbound.py`](./worker/rt_sms_inbound.py))**: Grace Tier 5.0m soft-wrap preserves seed capital. Inbound SMS limited to 5 texts/min per caller.

---

## Database Migrations Catalog

Migrations live in [`worker/sql/`](./worker/sql) and are versioned sequentially:
* Baseline schema: [`01-schema.sql`](./worker/sql/01-schema.sql) through [`09-lock-down-rpcs.sql`](./worker/sql/09-lock-down-rpcs.sql).
* Contiguous sequence requirement: [`10-reserved.sql`](./worker/sql/10-reserved.sql) maintains sequence integrity.
* Context & Safety: [`11-durable-turns-and-postcall-queue.sql`](./worker/sql/11-durable-turns-and-postcall-queue.sql) through [`17-forget-me-covers-jobs.sql`](./worker/sql/17-forget-me-covers-jobs.sql).
* Hardening: [`18-pin-search-path.sql`](./worker/sql/18-pin-search-path.sql) (pins `search_path`), [`19-forget-me-soft-delete.sql`](./worker/sql/19-forget-me-soft-delete.sql) (24h archive and restore), [`20-job-caps-and-retention.sql`](./worker/sql/20-job-caps-and-retention.sql) (daily job caps and transcript purges), [`21-forget-atomic-restore-newest-retention-all.sql`](./worker/sql/21-forget-atomic-restore-newest-retention-all.sql), [`22-readonly-exec.sql`](./worker/sql/22-readonly-exec.sql), [`23-kb-store.sql`](./worker/sql/23-kb-store.sql), [`24-praypal-schema.sql`](./worker/sql/24-praypal-schema.sql), and [`25-praypal-atrium-and-streaks.sql`](./worker/sql/25-praypal-atrium-and-streaks.sql).

Apply migrations before starting worker: `python scripts/migrate.py --apply`. Ensure `RT_PHONE_HASH_PEPPER` is set in `worker.env` before enabling `RT_REQUIRE_PEPPER=1`, or `/ready` returns 503 and autoheal will restart-loop the container.

---

## Testing & Quality Assurance

Run test suites from `worker/`:
```bash
# Standard unit tests, cadence, and security guards
pytest tests/ -v

# Documentation drift & link validation
pytest tests/test_doc_drift.py -v

# Conversation scale and concurrency benchmarks
python harness/scale_suite.py --callers 25

# Historic replay and end-to-end contract harness
python harness/replay_suite.py --fixtures
```

---

## Operator CLI Tooling

Operator and maintenance scripts live in [`worker/scripts/`](./worker/scripts):

| Script | Purpose |
|---|---|
| [`migrate.py`](./worker/scripts/migrate.py) | Migration manager with checksum validation (`--status`, `--apply`, `--dry-run`) |
| [`call_console.py`](./worker/scripts/call_console.py) | Real-time call telemetry and transcript console |
| [`check_migrations.py`](./worker/scripts/check_migrations.py) | Validates RPC signatures across lanes |
| [`kb_load.py`](./worker/scripts/kb_load.py) | Ingests governance docs into knowledge base (`--check` to validate without writing) |
| [`check_observability.py`](./worker/scripts/check_observability.py) | Telemetry and health integrity checker |
| [`cost_report.py`](./worker/scripts/cost_report.py) | Generates COGS breakdown per lane |
| [`watchdog.py`](./worker/scripts/watchdog.py) | Daemon monitoring stranded jobs and worker responsiveness |
| [`bootstrap_test_db.py`](./worker/scripts/bootstrap_test_db.py) | Seeds clean testing data into test Supabase; refuses prod refs outright |
| [`reset_caller.py`](./worker/scripts/reset_caller.py) | Wipes one caller (`<number>`, optional `--test`); undo within 24h via `rt_restore_caller` (see [RUNBOOK](./docs/RUNBOOK.md)) |
| [`scrub_rules.py`](./worker/scripts/scrub_rules.py) | Re-runs `rt_shield.rule_text_allowed` over stored caller rules; dry run by default, `--apply` nulls refused text, requires `--confirm <ref>` on prod |
| [`backfill_facts.py`](./worker/scripts/backfill_facts.py) | Backfills structured facts from historic call transcripts |
| [`run_dev_worker.sh`](./worker/scripts/run_dev_worker.sh) | Local development worker startup script |
| [`run_test_worker.sh`](./worker/scripts/run_test_worker.sh) | Test lane worker startup script |
| [`heygen_generate.py`](./worker/scripts/heygen_generate.py) | Automates HeyGen video manifesto production using founder portrait and script |

Prod refs are special for every tool that writes SQL: [`sql_push.py`](./worker/sql_push.py) runs read-only against a ref listed in `RT_PROD_SUPABASE_REFS` (or when `AGENT_NAME` is a `config.PROD_AGENT_NAMES` name) unless `--confirm <ref> --i-know-this-is-prod` is passed (every payload is wrapped in a `READ ONLY` transaction server-side, and a wipe — `--wipe`, `TRUNCATE`, `rt_wipe_all_data` — is never accepted on prod), and `bootstrap_test_db.py` refuses prod refs with no override at all.

---

## The Infrastructure Stack

Managed via [`deploy/docker-compose.yml`](./deploy/docker-compose.yml):
```
redis → livekit → sip → caddy → worker → scheduler   (network_mode: host)
autoheal                                             (network_mode: none — Docker socket only)
```

* **Network & Ports**: `network_mode: host` for media and app services; `8080` is Docker health server (`rt_health.py`), `8082` is LiveKit worker HTTP port (`RT_HEALTH_PORT`, used by LiveKit agent framework).
* **Health Checks & Autoheal**: `GET http://127.0.0.1:8080/health` and `/live` are liveness probes (200 OK). `GET /ready` returns 200 when all checks pass and **503 with `"status": "degraded"`** listing failed checks otherwise. The compose healthcheck probes `/ready`; the `autoheal` sidecar restarts any container labelled `autoheal=true` once Docker marks it unhealthy, in addition to the `restart: unless-stopped` policy. Inspect restart count with `docker inspect --format '{{.RestartCount}}' <container>`.
* **Logs & Retention**: Container logs rotate at 50 MB x 5 files (`json-file` driver); transcripts purged after `RT_TRANSCRIPT_RETENTION_DAYS` (default 30). See [`docs/RETENTION.md`](./docs/RETENTION.md).

---

## Standing Up a Lane

```bash
cp deploy/provisioning/lane.env.example deploy/provisioning/lane.env
$EDITOR deploy/provisioning/lane.env
bash deploy/provisioning/stand-up-lane.sh
APPLY=1 bash deploy/provisioning/stand-up-lane.sh
```

| Provisioning Script | Purpose |
|---|---|
| [`stand-up-lane.sh`](./deploy/provisioning/stand-up-lane.sh) | Master host provisioning script in 8 ordered phases |
| [`render-config.sh`](./deploy/provisioning/render-config.sh) | Generates configs; forwards `RT_PHONE_HASH_PEPPER` into `worker.env` and refuses to render without it unless opted out with `RT_REQUIRE_PEPPER=0` |
| [`firewall-setup.sh`](./deploy/provisioning/firewall-setup.sh) | Cloud firewall rule configuration |
| [`twilio-setup.sh`](./deploy/provisioning/twilio-setup.sh) | Twilio Elastic SIP Trunk, origination, termination, webhook routing, and spend alarm |
| [`recreate-routing.sh`](./deploy/provisioning/recreate-routing.sh) | LiveKit inbound SIP trunk and dispatch rules |
| [`outbound-trunk.sh`](./deploy/provisioning/outbound-trunk.sh) | LiveKit outbound SIP trunk configuration |
| [`capture-lane.sh`](./deploy/provisioning/capture-lane.sh) | Captures active configuration into `<lane>-inventory.md` |

---

## External Services & Credentials

| Service | Role | Missing Fallback |
|---|---|---|
| **Google (Gemini)** | Realtime speech-to-speech model, TTS clips, and search grounding (grounded answers name their own source, e.g. a quote "per Yahoo Finance"; the worker makes no direct call to any finance API) | Audio and LLM unavailable |
| **Supabase** | Persistent storage (caller profiles, facts, call traces, scheduled jobs) | No persistence |
| **LiveKit** | WebRTC + SIP media handling | No telephony or audio bridge |
| **Twilio** | Inbound SIP, outbound PSTN calls, SMS/MMS, and spend cap monitoring | Phone and SMS disabled |
| **Resend** | Transactional email dispatch | Email tools omitted |
| **Sentry** | Centralized error monitoring (`SENTRY_DSN`) | Logs to stderr |
| **OpenStreetMap / Overpass** | Map & geographic business discovery in `rt_directory.py` | Google Places fallback |
| **Google Places** | Grounded place & business lookup in `rt_directory.py` | Web search fallback |

---

## CI/CD & Operations

* **Pre-commit Hooks**: [`.pre-commit-config.yaml`](./.pre-commit-config.yaml) scans for secrets and runs `ruff` lint. `ruff-format` is a manual-stage hook, not run on commit.
* **GitHub Actions CI**: [`.github/workflows/ci.yml`](./.github/workflows/ci.yml) runs linter, tests, and doc drift checks.
* **Code Owners**: [`.github/CODEOWNERS`](./.github/CODEOWNERS) routes all PRs to platform owner.
* **Operations Runbook**: [`docs/RUNBOOK.md`](./docs/RUNBOOK.md) — ordered checklists for outages and alerts.
* **Architecture Decision Records**: [`docs/adr/`](./docs/adr) — ADR 0001 through 0006.
* **Retention Policy**: [`docs/RETENTION.md`](./docs/RETENTION.md) — data lifecycle and purge schedules.
* **System Prompt Viewer**: [`docs/internal/system_prompt_viewer.html`](./docs/internal/system_prompt_viewer.html) — local assembled prompt viewer.
