# ADR 0002: Phone numbers are keyed HMAC-SHA256 hashes, and the pepper is a one-way door

## Status

Accepted.

## Context

The caller's phone number is the only identity the platform has. It is the
primary key for memory, reminders, facts, and scheduled jobs. Storing it in
the clear would make the database a directory of older adults and their
personal histories; a plain SHA-256 of an E.164 number is no better, because
the whole North American numbering plan (about 10^10 values) can be
brute-forced on one GPU in minutes.

## Decision

`rt_prefs.phone_hash` normalizes the number to E.164 and computes
`HMAC-SHA256(RT_PHONE_HASH_PEPPER, e164)`. The pepper lives only in
`worker.env`, never in the database.

`RT_REQUIRE_PEPPER=1` is set on **every deploy lane** (dev, test, and prod
boxes alike; `worker/.env.example` says the same). `config.pepper_required()`
reads it fail-closed: only an empty value, `0`, `false`, `no`, or `off`
(case-insensitive) mean the pepper is optional; any other value — including
a typo — means required. "Set" also means usable: `config.pepper_ok()`
rejects a value that is empty, starts with `#`, is under 16 characters, or
is a stub (`changeme`, `replace`, `example`, `todo`, `xxxx`), and
`rt_prefs.phone_hash` treats an unusable pepper exactly like a missing one.
With the requirement on, hashing without a usable pepper raises
`RuntimeError` naming `RT_PHONE_HASH_PEPPER` instead of falling back to an
unkeyed hash; `config.missing_config()` lists the pepper so
`config.validate_startup_config()` prints `[config] ❌ STARTUP CONFIGURATION
ERRORS` at boot (it does not exit — see the RUNBOOK); and the
`pepper` readiness check registered by `agent.py` fails `/ready` so a
container that did come up never answers a call with weak identifiers. A worker booted under
a `config.PROD_AGENT_NAMES` name additionally raises at import
(`agent._assert_lane_is_declared`) when the pepper is missing or the
requirement is switched off.

Only a local, undeployed checkout (a laptop running
`scripts/run_dev_worker.sh` against the dev database) may leave
`RT_REQUIRE_PEPPER` unset. In that mode `rt_prefs.phone_hash` prints one
`[rt-prefs] WARNING` per process that hashes are unpeppered SHA-256 and
carries on. That is a convenience for iteration, not a lane configuration:
the moment a box is stood up with `deploy/provisioning/stand-up-lane.sh` it
gets the requirement.

**The pepper is a one-way door.** Every row keyed by `phone_hash` was written
under one pepper. Changing or losing it orphans every caller: the same person
dialing in hashes to a new key and is greeted as a stranger, and their old
memories, reminders, and jobs are unreachable by any query. There is no
rehash path, because the platform deliberately does not store the plaintext
number needed to compute a new hash.

## Consequences

* `RT_PHONE_HASH_PEPPER` must be set before `RT_REQUIRE_PEPPER` is enabled
  (see the deploy-ordering note in the README), and must be backed up with
  the same care as the database itself. Generate it with
  `openssl rand -hex 32`; the worker and scheduler on one lane must share it.
* Rotation is a migration project, not a config change: it needs a
  dual-hash window during which callers are looked up under both keys and
  rewritten under the new one. That project is out of scope until a
  compromise forces it.
* Per-lane peppers are required (dev, test, prod differ), so a test-lane
  dump cannot be joined to prod rows.
* Support tooling (`scripts/reset_caller.py`, `scripts/call_console.py`)
  takes a phone number and hashes it locally; it therefore needs the lane's
  pepper in its environment to find anything.
