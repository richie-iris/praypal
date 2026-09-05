---
id: ref.source-register
title: Source register and maintenance
layer: reference
binding: true
injection: retrieval
study: NCT01960348
intents: [source, provenance, version, maintenance, where-did-this-come-from]
entities: [provenance, versioning, maintenance, review]
authority: sponsor_sop
sources:
  - {doc: "ALN-TTR02-004 Protocol v6.0, 08 September 2015", section: "whole document"}
  - {doc: "ClinicalTrials.gov record NCT01960348", section: "whole record, last update posted 2024-04-22"}
status: active
version: 1.0.0
effective: 2026-09-02
review_by: 2027-09-02
---

# Source register and maintenance

## Primary sources

| Source | File | What it grounds |
|---|---|---|
| Clinical Study Protocol ALN-TTR02-004, Version 6.0 (Global Amendment 5), 8 September 2015, 109 pages | `source/Prot_000.pdf` | Everything in `kb/10-study/`. The authoritative source for design, eligibility, dosing, visits, assessments, safety reporting, and study conduct |
| ClinicalTrials.gov record NCT01960348, results posted 2018-09-06, last updated 2024-04-22 | `source/NCT01960348.json` | Registration identifiers, status, enrolment, locations, sponsor contact, and reported adverse event frequencies |

**Precedence: the protocol governs over the registry record.** Where they differ, the
protocol wins, and the difference is routed to the site as a documentation discrepancy
rather than resolved in this knowledge base.

## Not held here, and therefore not answerable

These documents ground statements the agent may not make, and their absence is why
certain questions always route to the site:

- The **Informed Consent Form**, in each approved local version. This is the
  authoritative risk and procedure document for a participant. Not in the registry
  record and not in this knowledge base.
- The **Investigator's Brochure**, which the protocol repeatedly defers to for safety
  guidance.
- The **Pharmacy Manual**, the **Study Manual**, and the **Statistical Analysis Plan**
  (referenced in the registry as `SAP_001.pdf`, not held here).
- Site-specific procedures, contact details, and local ethics approvals.

## Known gaps and deliberate omissions

- Sponsor and CRO contact details, the safety reporting mailbox, and the SAE hotline are
  redacted in the protocol PDF as published. They must come from configuration.
- Adverse event frequencies from the registry results are held for recognition only and
  are never quoted to a participant.
- Site addresses for the 52 locations are in the registry record but are not loaded,
  because the agent serves one configured site at a time.

## Maintenance obligations

**On any protocol amendment**, `kb/10-study/` is rebuilt in full before the agent runs.
An agent answering from a superseded protocol gives participants unapproved information,
which is a deviation regardless of whether the changed text was one it used.

**On any consent form revision**, review every study-layer file for statements that now
conflict with what participants have been told.

**On any change to site contacts**, update configuration, not these files.

**Annually at minimum**, review every file against its `review_by` date. A file past
review is flagged in the load report and, for governance files, blocks the build.

**Every governance file requires sign-off** by the sponsor's or site's regulatory or
quality function before it takes effect. Content changes bump the minor version;
anything that changes what the agent may or may not do bumps the major version and
requires re-approval.

## Change discipline

Document `id` values are stable and never reused. A withdrawn document is marked
`status: withdrawn` and kept, never deleted, so that any past conversation can be
reconstructed against the rules in force at the time. `kb_prompt_build` stores the exact
governance text and its hash for each build, and `kb_retrieval_log` records which chunks
grounded each turn. Together those answer the question an inspector will actually ask:
what was this agent operating under when it said that, and what did it say it from.
