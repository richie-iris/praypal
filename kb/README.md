# trial-pal — patient-facing clinical trial agent knowledge base

A Postgres/pgvector retrieval knowledge base for a voice and SMS agent that speaks
with clinical trial participants. Built so that **compliance is structural, not
statistical**: the rules that keep the agent lawful are pinned into every prompt,
while study facts are retrieved.

## The core design decision

Compliance guardrails are **never** left to vector similarity.

| Layer | Directory | `injection` | How it reaches the model |
|---|---|---|---|
| Governance | `kb/00-governance/` | `system` | Concatenated verbatim into the system prompt on every turn. Never chunked, never embedded, never ranked. |
| Study facts | `kb/10-study/` | `retrieval` | Chunked at `##` boundaries, embedded, retrieved by hybrid search. |
| Playbooks | `kb/20-playbooks/` | `retrieval` | Same, plus intent-tag routing so a matched intent forces its playbook into context. |
| Reference | `kb/30-reference/` | mixed | Glossary and contacts retrieved; the unanswerable register is pinned. |

If a retrieval miss can cause a compliance failure, the document belongs in
governance. That is the whole test.

## Frontmatter contract

Every file carries YAML frontmatter that maps 1:1 to columns in `kb_document`
(see `schema/001_kb_schema.sql`). The loader rejects any file missing a required key.

```yaml
id: gov.blinding-integrity      # stable primary key, dot-namespaced, never reused
title: Blinding integrity
layer: governance               # governance | study | playbook | reference
binding: true                   # true = departure is a protocol deviation or regulatory finding
injection: system               # system | retrieval
study: NCT01960348              # null when the document is study-agnostic and reusable
intents: [...]                  # routing tags for hybrid search
entities: [...]                 # filter facets
authority: protocol             # protocol | regulation | icf | registry | sponsor_sop | derived
sources: [...]                  # citation objects; every asserted fact traces to one
status: active
version: 1.0.0
effective: 2026-09-02
review_by: 2027-09-02
```

## Two layers, two lifecycles

`kb/00-governance/`, `kb/20-playbooks/` and `kb/30-reference/regulatory-basis.md` are
**study-agnostic**. They carry `study: null` and are reused unchanged for the next
protocol.

`kb/10-study/` is **instantiated per protocol**. Here it is filled from APOLLO
(ALN-TTR02-004, NCT01960348, patisiran in hereditary transthyretin-mediated
amyloidosis with polyneuropathy), using the Version 6.0 protocol PDF and the
ClinicalTrials.gov record in `source/`.

APOLLO completed in August 2017. The study layer is therefore a **worked example on
real protocol text**, not a live deployment. `kb/10-study/study-identity.md` carries
`study_status: COMPLETED` and the operational gate that stops the agent from telling
anyone the trial is open. Replace that directory to point the agent at a live trial.

## Placeholders

Site phone numbers, coordinator names, and portal URLs are **not** invented. They
appear as `{{TOKEN}}` and the loader must substitute them from the study
configuration. `kb/30-reference/contacts-and-routing.md` lists every token. A
`{{TOKEN}}` that survives into a running agent is a load-time failure, not a
runtime one — the loader must refuse to publish.

## Layout

```
kb/00-governance/    13 binding files, pinned to the system prompt
kb/10-study/         18 protocol-derived files, retrieved
kb/20-playbooks/     12 conversation playbooks, retrieved by intent
kb/30-reference/      4 files: glossary, contacts, regulatory basis, source register
schema/               Postgres DDL: documents, chunks, hybrid search, audit
source/               Primary sources the study layer is derived from
```

## Loading

1. Parse frontmatter; validate against the contract; fail closed on any violation.
2. Upsert into `kb_document` keyed on `id`, bumping `version` on content hash change.
3. For `injection: retrieval`, split at `##` headings into `kb_chunk`, carrying the
   parent document's `binding`, `study`, and `sources` onto every chunk.
4. Embed chunks; populate `tsv` for lexical search.
5. For `injection: system`, store the document whole and skip embedding.
6. Emit the pinned system prompt as an ordered concatenation of governance files by
   filename, and record its content hash in `kb_prompt_build` for audit.

## Retrieval at runtime

Hybrid: lexical `tsv` match plus vector similarity, fused, then filtered to the
active `study` and `status: active`. Governance is already in the prompt, so
retrieval only ever adds facts, never permissions.

The agent may assert only what a retrieved chunk supports. That rule lives in
`kb/00-governance/03-grounding-and-refusal.md` and is the single most important
line in this repository.
