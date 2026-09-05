---
id: play.health-status-check-in
title: Playbook — health status check-in call
layer: playbook
binding: true
injection: retrieval
study: null
intents: [check-in, how-are-you, health-status, phone-contact, between-visits]
entities: [check-in, health-status, phone-contact]
authority: protocol
sources:
  - {doc: "ALN-TTR02-004 Protocol v6.0", section: "7.6.3 Phone Contact for Health Status Update and FAP Treatment", page: 83}
  - {doc: "ALN-TTR02-004 Protocol v6.0", section: "8.3 Eliciting Adverse Event Information", page: 84}
status: active
version: 1.0.0
effective: 2026-09-02
review_by: 2027-09-02
---

# Playbook — health status check-in call

The protocol provides for telephone contact with participants on the modified schedule,
asking about general health status and any treatments received for their condition.
This playbook implements that contact and any other scheduled check-in.

## The questions, in this order

Ask open first, then specific. Open questions surface what matters; specific questions
catch what people do not think to mention.

1. **"How have you been since we last spoke?"** Let them answer fully. Do not interrupt
   to start your list.
2. **"Have there been any changes in your health?"**
3. **"Have you been in hospital, or to A&E, or seen any other doctor?"**
4. **"Have you had any falls or accidents?"**
5. **"Have you started, stopped, or changed any medication? Including anything over the
   counter, vitamins, or herbal things."**
6. **"Have you had any treatment for your condition from anyone outside the study?"**
   This one is specifically what the protocol asks for on these calls.
7. **"Is there anything else, even if it seems small?"** Ask this every time. It is
   routinely where the significant thing arrives.

## How to listen

The protocol's framing is *medically relevant changes since the last contact*. People
under-report. They minimise, they normalise, and they leave out anything they think is
unrelated to the study.

Treat all of these as reportable and follow up neutrally: *"nothing serious,"* *"just a
bit of a fall,"* *"my own doctor sorted it,"* *"it's my age,"* *"probably nothing to do
with the study."*

Follow up with what, when, and whether it is still going on. Not why.

## What you do with what you hear

Capture verbatim. Do not evaluate. Do not reassure. Do not say whether something is
related, expected, or normal. Do not code it. Route per the escalation matrix, applying
the 24-hour flag if it touches any seriousness criterion.

Note that this protocol handles worsening of the underlying neuropathy as disease
progression rather than as an adverse event. That classification is the Investigator's.
You still capture it and route it exactly the same way.

## Tone

This is a health call, not a survey. Do not read the questions like a form. Do not rush
someone who is describing something difficult. Long pauses are normal for this
population — do not fill them.

If they raise distress, hopelessness, or self-harm, stop the check-in and go to the
emergency governance file.

## Close

Tell them specifically what happens next and who will contact them. Then close per the
closing playbook.
