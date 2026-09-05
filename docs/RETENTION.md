# Data retention

What the platform keeps about a caller, where it lives, how long it stays,
and which job removes it. Everything below is keyed by `phone_hash`
(HMAC-SHA256 of the E.164 number, [ADR 0002](adr/0002-phone-hash-pepper.md));
the plaintext number is never stored.

## What is retained where

| Data | Where | Written by | Purged by |
|---|---|---|---|
| Full call transcript | `rt.calls.transcript` (migration `07`) | worker at call end (`rt_call_finish`) | `rt_purge_old_transcripts(days)` — nulls the column, keeps the call row |
| Per-turn transcript stream | `rt.call_events` rows with `kind='turn'` (migration `11`) | worker as each turn is spoken | `rt_purge_old_transcripts(days)` since migration `21` (deletes the turn rows of calls older than the window); `rt_forget_caller` |
| Last transcript snapshot | `rt.callers.last_transcript` (migration `04`) | the call-metrics upsert on every call | overwritten on the next call; `rt_purge_old_transcripts(days)` since migration `21` when `last_call_at` is older than the window; `rt_forget_caller` |
| Facts, reminders, goals, schema entries, scheduled jobs | `rt.facts`, `rt.reminders`, `rt.account_schema_registry`, `rt.scheduled_jobs` | in-call tools and the post-call worker | `rt_forget_caller` |
| Container logs (`worker`, `scheduler`, `sip`, `livekit`, `caddy`, `redis`) | Docker `json-file` driver on the lane host | every service | log rotation: **50 MB x 5 files** per container (`deploy/docker-compose.yml` `x-logging`) |
| Error events | Sentry (`SENTRY_DSN`) | `rt_logger` with `send_default_pii=False` and a `before_send` PII scrubber | Sentry's project retention setting (managed in Sentry, not here); an event whose scrub fails is dropped, not sent |
| SMS/MMS ledger — the newest 30 texts per caller, both directions, with Twilio media URLs | `RT_SMS_LEDGER_DIR/<phone_hash>.json` on the worker's own disk (default `/tmp/rt_sms`; directory `0700`, files `0600`) | `rt_sms.record_sms`, from the webhook process for inbound texts and from `send_sms` for outbound | entries older than `RT_SMS_LEDGER_RETENTION_DAYS` (default **30**, floor 1) are dropped on every write and swept hourly by `rt_sms.purge_expired`; `rt_sms.forget` (called by `forget_caller_entirely`) deletes the file. See "The SMS ledger" below |

`RT_LOG_TRANSCRIPT` is off by default, so transcript turns do **not** reach
container logs; only the structured `[rt-*]` lines do.

## The 30-day default

`RT_TRANSCRIPT_RETENTION_DAYS` (default **30**, `worker/.env.example`) is the
age after which `rt.calls.transcript` is nulled. The RPC refuses a window
below one day (`p_days < 1` raises) so a misconfiguration cannot wipe every
transcript in one sweep. Call metadata — timing, tokens, COGS, bridge
numbers, the `postcall` extraction — is kept; only the verbatim transcript
goes.

Migration `20` nulled only `rt.calls.transcript`, which left the verbatim
conversation alive as `kind='turn'` rows in `rt.call_events` and as
`rt.callers.last_transcript`. Migration `21` makes the same RPC cover all
three copies and return per-target counts
(`{"calls": n, "turns": n, "callers": n}`); the scheduler prints whatever
comes back, so no worker change was needed. A lane that has not applied `21`
is purging one copy out of three — apply it (see the README deploy
ordering).

## The hourly purge

`rt_scheduler.py` runs `_housekeeping()` every `_HOUSEKEEPING_EVERY_S`
(3600 s) inside its main loop. It calls, each wrapped on its own so one
failure never starves the other:

1. `rt_purge_forgotten()` — expires the forget-me archive (below).
2. `rt_purge_old_transcripts(p_days=RT_TRANSCRIPT_RETENTION_DAYS)`.

Both results are logged as `[rt-scheduler] housekeeping: {...}` and emitted
as a `scheduler.housekeeping` telemetry event. A failing purge is telemetry,
not a guard, so it is swallowed per [ADR 0003](adr/0003-fail-closed-guards.md)
and shows up as `error:<ExceptionName>` in that log line — grep for it when
auditing retention.

## The SMS ledger

The ledger is the one copy of caller data that lives on a worker's disk rather
than in the database, because it is how the webhook process and a live call
process talk to each other (`docs/README` "In-Call Synchronization"). Until
2026-09-02 it was keyed by the digits of the number, world-readable in `/tmp`,
never expired, and untouched by `rt_forget_caller` — three things this
document said do not happen. Now:

* The file name is `phone_hash`; the number itself is never written.
* The hourly housekeeping above runs in the scheduler container, which has its
  own `/tmp`, so the ledger's sweep rides on writes instead: `rt_sms.record_sms`
  calls `purge_expired()` at most once an hour.
* `forget_caller_entirely` removes the file before it calls `rt_forget_caller`.
  `scripts/reset_caller.py` runs SQL only and does not reach the worker's disk;
  after a manual reset, delete `RT_SMS_LEDGER_DIR/<phone_hash>.json` on the
  worker (or restart the container, which recreates `/tmp`) to finish the job.

## The 24-hour forget-me archive

`rt_forget_caller(hash)` (migrations `08`, `14`, `17`, `19`, `21`) is the caller-
initiated wipe, reached from the `db_tool` `forget_me` action (after
`rt_shield.wipe_confirmed` sees the caller say so) or from
`scripts/reset_caller.py <number>`. Since migration `19` it is a soft delete:

* Every row for the caller is moved into `rt.forgotten_archive`
  (service-role only) and out of the live tables — since migration `21` in
  one `DELETE ... RETURNING` per table, so a row written mid-forget cannot be
  deleted without being archived — including `rt.calls`, `rt.call_events`,
  scheduled jobs, and the post-call queue.
* `rt_restore_caller(hash)` writes the archived rows back, but only while
  `forgotten_at` is within the last **24 hours**, and (since migration `21`)
  only the newest forget batch, so a caller forgotten twice in a day is not
  restored with duplicate rows.
* `rt_purge_forgotten()` — run by the hourly housekeeping above — drops
  archive rows older than 24 hours. After that the erasure is final.

The archive exists for the mistaken-request case, not as a backup. Never
hand-delete from `rt.callers`; the RPC is what keeps the archive, the live
tables, and the scheduled-job queue consistent.
