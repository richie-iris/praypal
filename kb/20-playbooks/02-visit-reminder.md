---
id: play.visit-reminder
title: Playbook — visit reminder
layer: playbook
binding: false
injection: retrieval
study: null
intents: [reminder, appointment, upcoming-visit, confirm-visit, what-do-i-bring]
entities: [reminder, appointment, logistics]
authority: sponsor_sop
sources:
  - {doc: "study.dosing-visit-and-premedication"}
  - {doc: "study.visit-schedule-and-windows"}
  - {doc: "gov.escalation-routing"}
status: active
version: 1.0.0
effective: 2026-09-02
review_by: 2027-09-02
---

# Playbook — visit reminder

The commonest call, and the one where safety information most often arrives by
accident. Treat the reminder as the frame and the health check-in as the substance.

## Sequence

1. Open and verify.
2. **Ask how they have been.** Listen properly. Apply the adverse event duty to anything
   that comes back before continuing.
3. Confirm date, time, and location from their record.
4. Confirm the location deliberately if this is a nine-month or eighteen-month
   assessment, since it may be a different site.
5. Set expectations for the day: how long, roughly, and whether it is an assessment
   visit with no infusion.
6. Cover practical items — transport, whether someone is coming with them, anything the
   site has asked them to bring or do.
7. Ask whether anything would make attending difficult. Route obstacles rather than
   solving them.
8. Close per the closing playbook.

## What you may tell them about the day

Approximate duration, that premedication is given at least an hour before the infusion,
that the infusion takes around 70 minutes and may be longer for some people, and that
everyone stays an hour afterwards. For the two long assessment visits, that no study
drug is given that day and testing takes most of the day.

## What you never do here

- Never confirm a time you cannot see in the record.
- Never move, offer to move, or say whether a different date would work.
- Never say what will happen at the visit clinically.
- Never tell them to take, skip, or adjust anything, including premedication and the
  vitamin A supplement, beyond a plain reminder of what the protocol already asks.
- Never pressure attendance. If they say they may not come, capture and route it without
  persuasion.

## The eye examination detail

Some eye tests involve drops that blur vision for a while, so a participant may want
someone to drive them. Say the site will confirm whether that applies to their
appointment. Do not assert that it does.

## Confirming attendance

Ask once. If they say yes, record it. If they are unsure, record that and route it. Do
not ask a second time and do not seek a commitment.
