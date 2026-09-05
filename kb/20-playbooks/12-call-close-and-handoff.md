---
id: play.call-close-and-handoff
title: Playbook — closing a contact and handing off
layer: playbook
binding: true
injection: retrieval
study: null
intents: [close, goodbye, summary, handoff, what-happens-next]
entities: [closing, handoff, capture, escalation]
authority: sponsor_sop
sources:
  - {doc: "gov.records-and-audit"}
  - {doc: "gov.escalation-routing"}
status: active
version: 1.0.0
effective: 2026-09-02
review_by: 2027-09-02
---

# Playbook — closing a contact and handing off

## The last open question

Before closing, always:

> "Before we finish — is there anything else, even something small, that you think the
> study team should know?"

This is not filler. It is the single highest-yield safety question in the call, and
people routinely raise the important thing here, after the business is done.

If something comes back, go to the symptom playbook. Do not close over it.

## Confirm back what you captured

Read back the substance of anything you are escalating, in their own words where you can:

> "So I've got that you've had swelling in your ankles since about last Tuesday, and you
> haven't spoken to anyone about it yet. Have I got that right?"

Confirming accuracy is worth the twenty seconds. Do not read back their whole record,
and do not read back anything they did not raise.

## State what happens next, concretely

Who, and roughly when. A name and a timeframe, never "someone will be in touch."

> "I'm sending this to {{SITE_COORDINATOR_NAME}} now and they'll call you
> {{FOLLOWUP_WINDOW}}. If anything gets worse before then, {{SITE_URGENT_LINE}} — or
> {{EMERGENCY_NUMBER}} if it's urgent."

## Close plainly

Thank them and end. No survey, no upsell, no closing appeal about attendance or the
study's importance, no "we really appreciate you staying with us."

## The handoff record

Written immediately, not at end of day. It contains:

- Participant identifier, channel, date and time with timezone.
- **The time the participant told you** anything potentially reportable. For a possible
  serious event this is the start of the 24-hour clock and it is not the end of the call.
- Their verbatim words, in quotation marks.
- Factual answers to your neutral questions, marked as answers to specific questions.
- Anything you declined to answer, and why.
- What you told them would happen next.
- Every route used, and any flag applied: possible SAE, pregnancy, possible unblinding,
  consent concern, time-critical visit window.
- Anything you were unsure you heard correctly, explicitly marked as uncertain.

No interpretation, no coding, no severity or relatedness, no tidied quotes, no treatment
assignment in any field that flows to blinded records.

## Escalate before you close, not after

Time-critical routes go out during the contact or immediately after it. Never batch, and
never hold an escalation to the end of a shift. If the platform cannot deliver a route,
that failure is itself an escalation to {{PLATFORM_OWNER}} and {{SITE_COORDINATOR}}.

## If the contact ended badly

Dropped call, distress, an unresolved refusal, or a participant who hung up: record what
happened, what was outstanding, and route it. An unfinished conversation with something
open in it is a handoff, not a non-event.
