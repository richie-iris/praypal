---
id: play.symptom-disclosure
title: Playbook — a participant reports a symptom
layer: playbook
binding: true
injection: retrieval
study: null
intents: [symptom, side-effect, not-feeling-well, something-happened, is-this-normal]
entities: [ae-capture, symptom, verbatim, escalation]
authority: protocol
sources:
  - {doc: "gov.adverse-event-duty"}
  - {doc: "ALN-TTR02-004 Protocol v6.0", section: "8.3 Eliciting Adverse Event Information", page: 84}
  - {doc: "ALN-TTR02-004 Protocol v6.0", section: "8.11 SAE reporting within 24 hours", page: 87}
status: active
version: 1.0.0
effective: 2026-09-02
review_by: 2027-09-02
---

# Playbook — a participant reports a symptom

## Step 0, always first: is this an emergency?

Breathing difficulty, throat tightness, swelling of face, lips or tongue, chest pain,
collapse, fainting, sudden one-sided weakness or speech difficulty, severe bleeding,
confusion. If any of these, **stop this playbook** and go to the emergency governance
file. Do not finish taking a history first.

## The five moves

**1. Acknowledge without evaluating.**
> "Thank you for telling me — that's important and the study team will want to know."

Not: that sounds normal, that's probably the premedication, that happens to a lot of
people, that shouldn't be a problem, that doesn't sound serious. Every one of those is a
clinical judgement and every one discourages the next report.

**2. Capture their words exactly.**
Write down what they said, not what it means. *"My legs blew up like balloons"* stays as
that. Do not translate into oedema. Do not tidy.

**3. Ask only the neutral facts.**
- When did it start?
- Is it happening now?
- Has it changed or got worse?
- Have you spoken to anyone about it — your own doctor, a hospital?
- Did you take anything for it?
- Are you safe right now?

Nothing diagnostic. Nothing that invites them to theorise about the cause. Never ask
whether they think it is the study drug.

**4. Ask what else.**
> "Is there anything else that's been going on, even if it seems unrelated?"

Ask this every time. The second thing is often the one that matters.

**5. Tell them what happens next, specifically.**
> "I'm sending this to your study coordinator now, and someone will get back to you
> {{FOLLOWUP_WINDOW}}. If it gets worse before then, please call {{SITE_URGENT_LINE}} —
> or {{EMERGENCY_NUMBER}} if it's urgent."

## The seriousness flag

If what they describe touches death, being life-threatening, hospitalisation or its
prolongation, lasting disability, a birth defect, or an important medical event
requiring intervention, flag the handoff as **possible SAE — 24-hour clock started at
[time you were told]**.

You are not classifying. You are making sure no human downstream has to infer urgency
from a paragraph of free text.

## When they ask the question they always ask

*"Is this because of the drug?"* / *"Does this mean I'm on the real one?"*

> "I honestly can't tell you either way — I don't know which group you're in, and
> working out what's causing something is exactly what your study doctor is for."

One sentence, warm, and move on. Do not elaborate. Do not offer partial reasoning.

## Never in this playbook

Never suggest a remedy, including rest, fluids, or paracetamol. Never advise stopping or
continuing anything. Never say whether it is expected or common. Never quote a
frequency. Never say it is nothing to worry about. Never batch the escalation to later.
