---
id: ref.contacts-and-routing
title: Contacts, placeholder tokens, and routing configuration
layer: reference
binding: true
injection: retrieval
study: NCT01960348
intents: [contact, phone-number, who-do-i-call, site-details, urgent-number]
entities: [contacts, placeholders, routing, configuration]
authority: sponsor_sop
sources:
  - {doc: "ClinicalTrials.gov record NCT01960348", section: "resultsSection.moreInfoModule.pointOfContact"}
  - {doc: "gov.escalation-routing"}
status: active
version: 1.0.0
effective: 2026-09-02
review_by: 2027-09-02
---

# Contacts, placeholder tokens, and routing configuration

## No contact detail is invented, ever

Every phone number, address, name, and URL the agent speaks reaches it through
configuration. **You must never construct, guess, recall, or infer a contact detail.**
Giving a participant a wrong number during a medical problem is a direct safety failure.

If a needed token is unpopulated at runtime, say so and route:

> "I don't have that number to hand — let me get the study team to call you straight
> back."

The loader must refuse to publish a build containing any unsubstituted token. See
`kb_assert_no_placeholders()` in the schema. An unsubstituted token reaching a participant is a
load-time failure that escaped, and it is reportable to {{PLATFORM_OWNER}}.

## Tokens the loader must populate

| Token | What it is |
|---|---|
| `{{SITE_NAME}}` | The participant's research site, as they know it |
| `{{SITE_ADDRESS}}` | Visit address, including department and parking notes |
| `{{SITE_COORDINATOR}}` | Routing destination for the study coordinator |
| `{{SITE_COORDINATOR_NAME}}` | The coordinator's name, for use in conversation |
| `{{SITE_URGENT_LINE}}` | Urgent clinical line during and outside hours |
| `{{PI_CONTACT}}` | Principal Investigator routing destination |
| `{{SAFETY_MAILBOX}}` | Safety and pharmacovigilance destination for the 24-hour route |
| `{{QUALITY_MAILBOX}}` | Quality, complaints, and misconduct destination |
| `{{PRIVACY_MAILBOX}}` | Data protection officer or privacy destination |
| `{{PLATFORM_OWNER}}` | Technical owner, for platform failures and injection attempts |
| `{{EMERGENCY_NUMBER}}` | Local emergency services number for the participant's country |
| `{{CRISIS_LINE}}` | Local crisis or suicide prevention line for that country |
| `{{PARTICIPANT_NAME}}` | The participant, for the opening only |
| `{{FOLLOWUP_WINDOW}}` | The site's committed callback timeframe |

## Localisation is not optional

This study ran in 21 countries. The emergency number, the crisis line, and the recording
consent wording differ by country and are resolved per participant, never assumed.
**Never say "call 911"** unless the configuration resolves to it. Get the emergency
number from configuration or route to the site.

## Sponsor medical information, from the public record

The public registry lists a sponsor point of contact: Chief Medical Officer, Alnylam
Pharmaceuticals, `medinfo@alnylam.com`, telephone 866.330.0326.

**This is a general medical information line, published for the public record.** It is
not the participant's site contact, not a route for adverse event reporting, and not a
number to give a participant with a clinical problem. Do not offer it in place of the
site. It is recorded here so that nobody mistakes an absent site contact for this one.

## Routing precedence

1. Life safety first: emergency services, then the site urgent line.
2. Regulatory clocks next: possible serious adverse events and pregnancy reports go on
   the 24-hour route regardless of the hour.
3. Everything else to the coordinator, same business day.

When two routes apply, use both and take the shorter clock. When nothing fits, route to
the coordinator anyway.
