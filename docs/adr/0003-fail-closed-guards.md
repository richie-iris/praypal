# ADR 0003: Guards fail closed, telemetry fails open

## Status

Accepted.

## Context

The worker sits between a voice model that can be talked into anything and a
set of side effects that cannot be undone: dialing a phone number, sending an
email, purging a caller's memory, creating a job that will ring someone at a
future time. Each side effect is fronted by a guard (dial policy, recipient
policy, daily job caps, calling windows, wipe confirmation) and most guards
need data from Supabase or another backend to decide.

Those backends fail. The question this ADR settles is what a guard does when
it *cannot evaluate*: the count RPC is missing, the request timed out, the
row is malformed.

## Decision

**A guard that cannot evaluate refuses.** Missing data, a backend error, or
an exception inside the guard is treated as a denial, never as "no rule
applies". Examples pinned by tests:

* `rt_count_jobs_today` unavailable -> job creation refused (fail-closed
  daily cap), not unlimited jobs.
* Callee timezone unknown -> the strict TCPA window is applied against the
  default zone, never skipped.
* `RT_REQUIRE_PEPPER=1` and no pepper -> `RuntimeError`, not an unkeyed hash.
* No verified email on file -> no email is sent, whatever the planner asked
  for.

**Telemetry that fails is swallowed.** Every `rt_obs`, trace, and logger call
sits inside `contextlib.suppress(Exception)` or an equivalent. A metrics
sink being down must never turn a successful guard decision into a crashed
call, and must never block a refusal from being returned.

The two rules are written as a pair because they are easy to confuse under
pressure: the reflex to "not break the call" is right for telemetry and wrong
for guards.

## Documented exceptions

Two places knowingly depart from the rule. They are listed here so nobody
mistakes them for oversights, and so each has a name in the finding log.

* **Per-caller daily minute budget is best-effort (finding #5, open).**
  `agent.py` reads the caller's `daily_minutes` usage from the context bundle
  before answering. If the bundle has no usage rows and one bounded refetch
  also fails, the call is admitted uncapped and `[rt-budget] usage unreadable
  — admitting the call uncapped` is logged with an `ERROR`-level
  `agent.budget_refetch` event. The budget is a cost control, not a safety
  control: turning away an isolated caller over a database hiccup is a worse
  outcome than one over-long day. This stays fail-open until finding #5
  lands a durable per-caller counter that can be read without the bundle.

* **Bridge-ledger cross-session race is an accepted residual.**
  `rt_bridge.check_and_record_dial` reads the per-caller dial ledger, checks
  the daily cap, and writes the incremented count back as a separate RPC.
  Two sessions for the same caller (a live call and a scheduled outbound
  call landing at the same minute) can both read `count = cap - 1` and both
  dial. The read itself fails closed (unreadable ledger -> `DialRefused`),
  so the residual is at most one extra bridge per race, bounded by
  `MAX_BRIDGES_PER_DAY`, and every dial is still recorded for audit. Making
  the check-and-increment atomic needs a dedicated RPC; until then this is
  accepted rather than hidden.

## Consequences

* Outages surface as refusals the caller hears ("I can't set that up right
  now") rather than as unsafe actions discovered later in the audit log.
* Guard code carries a terse comment explaining *why* it refuses on error,
  so a future reader does not "fix" it into a fail-open.
* Each guard emits a `guard.decision` telemetry event with `allowed` and
  `reason`, itself inside a suppress block, so refusals are observable but
  observability cannot cause a refusal.
* Operators must treat a spike in `reason=backend_error` refusals as an
  outage, not as a policy problem; the runbook points at the dependency
  chain to check.
