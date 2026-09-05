# ADR 0004: Outbound calls honour TCPA calling windows at the callee's local time

## Status

Accepted.

## Context

The scheduler places real outbound PSTN calls: reminders, follow-ups, and
callbacks the caller asked for. The US Telephone Consumer Protection Act
limits automated calls to 8am-9pm at the *called party's* local time, and
our callers are predominantly older adults for whom a 6am ring is a health
event, not an annoyance. Jobs are written hours or days ahead, are retried
on failure, and may be picked up by a runner that was down overnight, so the
time the job was *written* says nothing about the time it will *dial*.

## Decision

`rt_scheduler.calling_window()` defines two windows:

* `CALLING_WINDOW = (8, 21)` — the TCPA limit, applied to every stored job.
* `CALLING_WINDOW_LIVE = (7, 23)` — a wider window used only while the
  caller is on the line asking for a callback, the one case where consent
  is on the record. It is never applied to a job read back from the
  database, because a stored row carries no proof of that consent.

Both are overridable per lane via `RT_CALLBACK_HOURS` / `RT_CALLBACK_HOURS_LIVE`
(`"lo-hi"`), for test lanes only.

The window is evaluated twice: once at scheduling (a request outside the
window is refused with `reason="quiet_hours"`) and again at the moment of
dialing, against the callee's zone from `rt_timezone`. A job that lands
outside the window at dial time is **deferred** to the next window opening
rather than failed, because the runner polls every 60s with three attempts
and a "failure" would silently retire a promised callback.

Callee timezone resolution follows ADR 0003: an unknown zone falls back to
`DEFAULT_TZ` and is logged as `zone_known=false`; it never widens the
window.

## Consequences

* A reminder set for "tonight at 10" is refused up front and the assistant
  offers the next morning instead; the caller hears the constraint rather
  than discovering a missed call.
* Deferred jobs appear in the scheduler log as `DEFER outbound dial` and
  are expected behaviour; the runbook says so.
* Timezone data quality directly affects compliance. Area-code-to-zone
  mapping is the floor; a caller-stated timezone in preferences overrides it.
* The live window's extra hours are a product decision as much as a legal
  one and can be narrowed per lane without a code change.
