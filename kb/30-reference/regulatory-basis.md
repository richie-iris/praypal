---
id: ref.regulatory-basis
title: Regulatory basis for the governance layer
layer: reference
binding: false
injection: retrieval
study: null
intents: [why-that-rule, regulation, legal-basis, gcp, compliance-basis]
entities: [ich-e6, 21-cfr, hipaa, gdpr, tcpa, ai-act, ctr]
authority: regulation
sources:
  - {doc: "ICH E6 Good Clinical Practice"}
  - {doc: "21 CFR Parts 11, 50, 54, 56, 312"}
  - {doc: "45 CFR Parts 160 and 164"}
  - {doc: "Regulation (EU) 2016/679; Regulation (EU) 536/2014; Regulation (EU) 2024/1689"}
status: active
version: 1.0.0
effective: 2026-09-02
review_by: 2027-09-02
---

# Regulatory basis for the governance layer

Why each governance rule exists. **This is orientation for the people maintaining this
knowledge base, not something the agent recites to participants.** Citing regulations at
a worried person is unhelpful and slightly menacing.

**Confirm every citation with the sponsor's regulatory affairs and the site's own
procedures before relying on it.** Requirements differ by country and by site, they
change, and the applicable set for any given trial is a determination for qualified
people, not for this file.

## Good clinical practice

**ICH E6** is the international standard for designing, conducting, recording, and
reporting trials. E6(R2) is the version in force through this protocol's conduct;
**E6(R3)** was adopted in 2025 and supersedes it in adopting regions, though many sites
still operate to R2-era procedures. Both underpin the same governance rules here:
medical decisions rest with a qualified physician investigator; informed consent is a
documented process; records must be accurate, attributable, and verifiable; and tasks
delegated to others must be within a documented scope and appropriately supervised.

The **Declaration of Helsinki** is the ethical foundation, and this protocol names it
explicitly. Voluntariness and the primacy of participant wellbeing come from there.

## United States

| Rule | What it drives |
|---|---|
| 21 CFR 312.7(a) | An investigational drug may not be represented as safe or effective. Drives the promotion file. |
| 21 CFR 50.20 | Consent must be sought without coercion or undue influence, and rights cannot be waived. Drives voluntariness. |
| 21 CFR 50.25(a)(8) | Participation is voluntary; discontinuation carries no penalty or loss of benefits. Drives the stopping playbook. |
| 21 CFR 56 | IRB review, including of information given to participants. Drives the ban on generating new descriptive material. |
| 21 CFR 312.32 | IND safety reporting, including expedited timelines. Drives the 24-hour clock. |
| 21 CFR Part 11 | Electronic records and signatures, including audit trails. Drives records and corrections. |
| 45 CFR 160 and 164 | HIPAA privacy and security, including minimum necessary. Drives privacy and verification. |
| 47 USC 227 and 47 CFR 64.1200 | Telephone consumer protection: calling hours, revocation of consent. Drives contact rules. |

State laws add requirements, notably all-party consent to call recording in some states
and, increasingly, disclosure obligations for automated systems interacting with people.
Both are resolved through per-jurisdiction configuration, not by the agent.

## European Union and United Kingdom

**Regulation (EU) 536/2014**, the Clinical Trials Regulation, now governs trials in the
EU; it replaced Directive 2001/20/EC, which this 2015 protocol still cites. Expedited
reporting of suspected unexpected serious adverse reactions continues under the current
framework, on timelines of the same order the protocol describes.

**GDPR** treats health data as special-category data. Data minimisation, purpose
limitation, and data-subject rights drive the privacy file and the routing of access,
correction, and erasure requests. Research data carry retention obligations that
constrain erasure, which is precisely why the agent never answers such a request itself.

**Regulation (EU) 2024/1689**, the AI Act, requires that people be informed when they
interact with an AI system unless it is obvious. That drives unprompted disclosure.

## Automated systems in regulated research

FDA published draft guidance in January 2025 on the use of artificial intelligence to
support regulatory decision-making for drug and biological products, framing credibility
assessment around the specific context of use. The architectural consequence adopted
here is that a model's output is not treated as source data: the agent captures, and a
qualified person reviews, attributes, and signs.

## The design principle underneath all of it

Every rule above resolves to the same structure. **The agent may inform, capture, and
route. It may not decide, assess, promise, or persuade.** If a proposed capability
requires the agent to decide something, it belongs with a human, and no amount of
prompt engineering changes that.
