# ADR 0005: Email and SMS go only to the caller's own verified address

## Status

Accepted. Amended 2026-09-02: the carrier became Twilio for messaging and voice
alike ([ADR 0006](0006-twilio-single-carrier.md)), and the Twilio auth token also
authenticates inbound webhooks (`rt_sms.webhook_is_from_twilio`), so it is not a
send-only credential; the recipient policy itself is unchanged.

## Context

The assistant can send email (Resend) and SMS (Twilio) during and after a
call. The recipient is text that originates, one way or another, from the
voice model: a caller says "email that to my daughter", a post-call planner
extracts `to_email` from a transcript, or an injected phrase in the audio
asks for a copy to be sent somewhere. An LLM-chosen recipient is an
exfiltration channel: the caller's memory bundle, reminders, and facts could
be mailed to an attacker with one convincing sentence.

The message *content* is model-written too. A subject or body that reaches
the HTML part unescaped, or an event title that reaches the `.ics` text
unescaped, can smuggle markup or extra iCalendar properties into the
caller's inbox.

## Decision

Three rules, each enforced in code rather than in the prompt.

### 1. The recipient is the address on file, and nothing else

A message is dispatched only when the destination exactly matches (case
insensitive, after `strip()`) the contact on file for the authenticated
caller:

* `rt_email.is_allowed_recipient(to_email, verified_email)` is the single
  decision point for email, and it gates the dispatcher itself:
  `rt_email.send_email(..., verified_email=<address on file>)` refuses with
  `{"error": True, "message": "recipient not verified"}` unless
  `is_allowed_recipient(to_email, verified_email)` passes. A call site that
  omits `verified_email` therefore cannot send at all. Every call site
  (`agent.py` `send_email` / `send_calendar_invite`, the post-call planner)
  passes the on-file address. In `agent.py`, `_resolve_email_recipient`
  refuses when nothing is on file ("no verified address on file") and when
  the model-supplied `to_email` differs from it ("destination mismatch").
  The post-call planner's `payload["to_email"]` is discarded and replaced
  with the verified address; if there is no verified email on file the job
  completes with `message="no verified email on file"` and nothing is sent.
* `send_sms` has no recipient parameter at all. It texts `self._caller_e164`,
  the number the session was hashed from; there is no argument through which
  the model could name a third party.
* Per ADR 0003 the check fails closed: a missing or malformed on-file value
  is a refusal, not a pass.

### 2. "On file" means the caller said it, on a `caller:` line

The address on file is where every recap goes, so the only way an address
gets there is by the caller speaking it on the line:

* `save_email` (`agent.py`) writes `rt_set_caller_email` only when
  `_email_spoken_by_caller(addr, transcript_lines)` is true — the exact
  address (token-bounded, case-insensitive) appears on at least one
  transcript line that starts with `caller:`. Lines the agent spoke do not
  count, so an address the model "read back" ("I still have x@y on file,
  right?") is not evidence. `rt_shield.email_spoken_by_caller` is the shared
  rule; `agent.py` and `rt_postcall_worker._email_spoken_by_caller` delegate
  to it and carry the same rule inline as a fallback.
* The post-call worker applies the same test before it writes a
  `caller_email` extracted from the transcript. An address that appears only
  in an `agent:` line, or only in the extractor's output, is dropped.
* There is no read-back confirmation flow and no allowlist of additional
  recipients; the model cannot create a usable address in the same turn it
  uses it unless the caller has already said it.

### 3. Model-written content is encoded on the way out

* `rt_email.send_email` builds the HTML part from
  `html.escape(body_text, quote=True)`; the plain-text part carries the raw
  text, and the subject is a header field that never enters the HTML part.
  A body containing `<script>` renders as text.
* `rt_email.generate_ics` escapes every TEXT value (`SUMMARY`, `LOCATION`,
  `DESCRIPTION`) per RFC 5545 §3.3.11 — backslash, semicolon, comma, and
  newline — and strips CR/LF first, so a model-written title cannot end the
  line and start a new iCalendar property. `ORGANIZER` is hard-coded to the
  platform sender, never derived from the event.
* `send_email` sends exactly once: `retries=0` on the pooled client, and an
  `Idempotency-Key` header (a fresh `uuid4` per call) so a transport-level
  retry cannot deliver the same message twice.

## Consequences

* "Send this to my daughter" is refused. The tool result tells the model it
  can only send to the address on file and that a new address must be spoken
  by the caller and saved with `save_email` first. That friction is
  deliberate.
* There is no allowlist of extra recipients per caller. Adding one would
  need a verification flow with its own confirmation step and is a new ADR.
* Every email refusal is visible in telemetry: `rt_email.send_email` warns
  `guard.decision` with `guard="send_email"`, `reason="recipient_not_verified"`,
  and the `agent.py` guards trace `guard.decision` with the tool name
  (`send_email`, `send_calendar_invite`, `save_email`) and the reason, so a
  caller repeatedly asking for a third party (or an injection attempt) shows
  up in the call trace.
* The Resend credential can be scoped to send-only, since the application
  never needs to enumerate or read email. The Twilio auth token cannot: it
  is what signs every inbound webhook, so it is kept out of every log line
  and telemetry event instead (`rt_sms._net_where` logs host and path only).
