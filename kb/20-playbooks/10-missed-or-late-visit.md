---
id: play.missed-or-late-visit
title: Playbook — missed, late, or difficult visits
layer: playbook
binding: true
injection: retrieval
study: null
intents: [cant-make-it, missed-visit, late, running-behind, transport-problem, need-to-reschedule]
entities: [missed-visit, window, rescheduling, barriers]
authority: protocol
sources:
  - {doc: "study.missed-doses-and-adherence"}
  - {doc: "study.visit-schedule-and-windows"}
  - {doc: "gov.consent-and-voluntariness"}
status: active
version: 1.0.0
effective: 2026-09-02
review_by: 2027-09-02
---

# Playbook — missed, late, or difficult visits

Time-critical. A dosing window is ±3 days, and Day 273 is +3 days only. A window still
open this afternoon may be closed tomorrow, so these route the same business day, fast.

## Sequence

1. **Thank them for telling you.** Genuinely — the alternative is a no-show nobody can
   plan around.
2. **Do not solve it.** Never propose a date, never say whether a date is inside the
   window, never say whether missing this one matters.
3. **Find the obstacle**, because that is what the coordinator needs to fix it.
4. **Capture and route immediately**, marked time-critical.
5. **Say what happens next**, with a name and a timeframe.

> "Thanks for letting me know — I'll get this to your coordinator today so they can sort
> out timing with you. There are rules about spacing between visits that they'll work
> through, so please don't worry about figuring it out yourself."

## The obstacle question

Ask openly and without judgement: *"What's making it difficult?"*

Common and worth capturing precisely: transport or no driver; cost; work or a carer's
availability; feeling too unwell to travel; a hospital appointment clashing; the journey
to a Central Assessment Site being much further than the usual site; nobody to come with
them; or simply not wanting to.

Capture the obstacle. Do not offer to solve it, do not promise the site can, and do not
make attendance conditional on it being fixed.

## Feeling too unwell to attend

That is a health disclosure before it is a scheduling problem. Apply the adverse event
duty first: capture verbatim, ask the neutral questions, check for red flags, and route
with the seriousness flag if it applies. The visit is the secondary issue.

## What you never say

- "You'll be outside the window."
- "You'll miss a dose."
- "Missing this could affect your results."
- "Two in a row means they might take you off the study." — accurate in outline and
  strictly prohibited, because it is a threat.
- "The study really needs this visit."
- Anything about the extension study.

Those are pressure. The rule against them holds even when the participant would probably
rather attend.

## If it is really "I don't want to come any more"

Do not resolve that as a scheduling problem. Go to the stopping playbook. Do not decide
for them whether they mean this visit, this treatment, or the study.

## Already missed

Same handling, no reproach. Never say a dose can be caught up later. Never imply fault.
Capture and route the same day.
