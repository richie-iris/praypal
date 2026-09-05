-- ============================================================
-- The trial-pal knowledge base: documents, chunks, hybrid search, audit.
--
-- Written 2026-09-02 from kb/schema/001_kb_schema.sql, which arrived with the
-- knowledge base itself. Three things changed on the way in, and each one is a
-- rule this repo already lives by:
--
--   1. The tables sit in their OWN schema, `kb`, not in `rt` and not in public.
--      rt_forget_caller walks the rt tables and archives every row it finds for
--      a caller. The knowledge base is a protocol, not a person: a forget-me
--      must not delete the study's own text, and a schema boundary is the only
--      guarantee of that which survives someone adding a table later.
--   2. EXECUTE and SELECT are revoked from anon and authenticated, the way
--      migration 09 does for every rt_* function. The service role reads it;
--      nothing reachable from a browser does.
--   3. search_path is pinned on every function, the way migration 18 does, so a
--      SECURITY DEFINER body cannot be hijacked by a caller's search_path.
--
-- Apply: python scripts/migrate.py --apply
-- ============================================================

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE SCHEMA IF NOT EXISTS kb;

DO $$ BEGIN
    CREATE TYPE kb.layer     AS ENUM ('governance','study','playbook','reference');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    CREATE TYPE kb.injection AS ENUM ('system','retrieval');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    CREATE TYPE kb.authority AS ENUM ('protocol','regulation','icf','registry','sponsor_sop','derived');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    CREATE TYPE kb.status    AS ENUM ('active','superseded','draft','withdrawn');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ─── Documents: one row per markdown file ────────────────────
-- The frontmatter contract in kb/README.md maps 1:1 onto these columns. The
-- loader rejects a file missing any required key rather than defaulting it:
-- a document with no `sources` is an unciteable claim, and an unciteable claim
-- spoken to a trial participant is a protocol deviation.
CREATE TABLE IF NOT EXISTS kb.document (
    id            text PRIMARY KEY,                  -- 'gov.blinding-integrity'
    title         text          NOT NULL,
    layer         kb.layer      NOT NULL,
    binding       boolean       NOT NULL,            -- true => departure is a deviation/finding
    injection     kb.injection  NOT NULL,
    study         text              NULL,            -- NCT id; NULL = study-agnostic, reusable
    study_status  text              NULL,            -- e.g. COMPLETED — gates "are you recruiting?"
    intents       text[]        NOT NULL DEFAULT '{}',
    entities      text[]        NOT NULL DEFAULT '{}',
    authority     kb.authority  NOT NULL,
    sources       jsonb         NOT NULL DEFAULT '[]',
    status        kb.status     NOT NULL DEFAULT 'active',
    version       text          NOT NULL,
    effective     date          NOT NULL,
    review_by     date          NOT NULL,
    body          text          NOT NULL,
    content_hash  text          NOT NULL,
    path          text          NOT NULL,
    loaded_at     timestamptz   NOT NULL DEFAULT now(),

    -- A binding governance rule must be PINNED. If a compliance rule could be
    -- missed by a retrieval query, then a bad embedding is a regulatory finding.
    CONSTRAINT binding_rules_are_pinned
        CHECK (NOT (binding AND layer = 'governance' AND injection <> 'system')),
    CONSTRAINT sources_present CHECK (jsonb_array_length(sources) > 0)
);

CREATE INDEX IF NOT EXISTS kb_document_study_idx  ON kb.document (study, status);
CREATE INDEX IF NOT EXISTS kb_document_layer_idx  ON kb.document (layer, injection);
CREATE INDEX IF NOT EXISTS kb_document_review_idx ON kb.document (review_by) WHERE status = 'active';

-- ─── Chunks: only for injection = 'retrieval' ────────────────
-- binding / study / sources are denormalised onto the chunk on purpose: a
-- retrieved chunk has to be self-describing, because the thing that reads it
-- is a language model that will not go and look up its parent.
CREATE TABLE IF NOT EXISTS kb.chunk (
    chunk_id     bigserial PRIMARY KEY,
    document_id  text     NOT NULL REFERENCES kb.document(id) ON DELETE CASCADE,
    ordinal      int      NOT NULL,
    heading      text     NOT NULL,
    body         text     NOT NULL,
    token_count  int      NOT NULL,
    binding      boolean  NOT NULL,
    study        text         NULL,
    intents      text[]   NOT NULL DEFAULT '{}',
    sources      jsonb    NOT NULL,
    embedding    halfvec(1536),
    tsv          tsvector GENERATED ALWAYS AS
                   (to_tsvector('english', coalesce(heading,'') || ' ' || coalesce(body,''))) STORED,
    UNIQUE (document_id, ordinal)
);

CREATE INDEX IF NOT EXISTS kb_chunk_tsv_idx    ON kb.chunk USING gin (tsv);
CREATE INDEX IF NOT EXISTS kb_chunk_vec_idx    ON kb.chunk USING hnsw (embedding halfvec_cosine_ops);
CREATE INDEX IF NOT EXISTS kb_chunk_study_idx  ON kb.chunk (study);
CREATE INDEX IF NOT EXISTS kb_chunk_intent_idx ON kb.chunk USING gin (intents);

-- ─── Pinned prompt builds ────────────────────────────────────
-- The exact governance text the agent ran with, hashed. This is the answer to
-- "what rules was the agent operating under on 3 May?", and it is why the
-- pinned block is generated and recorded rather than assembled at each boot.
--
-- `source_hash` is the hash of the FULL governance text the pinned block was
-- distilled from. The full text lives in kb.document and is what a reviewer
-- reads; the pinned block is what the model is given, because the full text is
-- roughly ten times the whole prompt budget (worker/.env.example
-- RT_PROMPT_BUDGET). Recording both hashes is what makes the distillation
-- auditable rather than a shortcut: a change to any governance file changes
-- source_hash, and a build whose source_hash has moved is stale.
CREATE TABLE IF NOT EXISTS kb.prompt_build (
    build_id     bigserial PRIMARY KEY,
    study        text        NULL,
    document_ids text[]      NOT NULL,
    prompt_text  text        NOT NULL,
    prompt_hash  text        NOT NULL UNIQUE,
    source_hash  text        NOT NULL,
    chars        int         NOT NULL,
    built_at     timestamptz NOT NULL DEFAULT now()
);

-- ─── Retrieval log ───────────────────────────────────────────
-- Every turn records what grounded it. Required to defend any statement the
-- agent made to a participant, and the reason gov.grounding-and-refusal can say
-- "if you cannot name what grounded a sentence, do not say the sentence".
-- A refusal is logged as loudly as an answer: a day of refusals is a knowledge
-- gap, and nobody finds it if only answers are recorded.
CREATE TABLE IF NOT EXISTS kb.retrieval_log (
    log_id       bigserial PRIMARY KEY,
    session_id   text     NOT NULL,
    turn         int      NOT NULL,
    build_id     bigint   REFERENCES kb.prompt_build(build_id),
    query        text     NOT NULL,
    chunk_ids    bigint[] NOT NULL,
    refused      boolean  NOT NULL DEFAULT false,
    escalated_to text         NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS kb_retrieval_log_session_idx ON kb.retrieval_log (session_id, turn);

-- ─── Hybrid search ───────────────────────────────────────────
-- Reciprocal rank fusion of a lexical hit and a vector hit. Lexical carries the
-- weight the agent needs most: a participant asking "vitamin A" wants the
-- vitamin A document, and an embedding will happily rank a warm paragraph about
-- feeling tired above it. Vector search catches the phrasing lexical misses.
--
-- p_study filters to the running protocol, and lets study-agnostic rows
-- (study IS NULL — governance-derived playbooks and the glossary) through.
CREATE OR REPLACE FUNCTION kb.search(
    q            text,
    q_embedding  halfvec(1536),
    p_study      text DEFAULT NULL,
    p_limit      int  DEFAULT 8,
    p_k          int  DEFAULT 60
) RETURNS TABLE (chunk_id bigint, document_id text, heading text, body text,
                 sources jsonb, binding boolean, score double precision)
LANGUAGE sql STABLE
SET search_path = pg_catalog, public, rt, pg_temp
AS $$
    WITH scoped AS (
        SELECT c.* FROM kb.chunk c
        JOIN kb.document d ON d.id = c.document_id
        WHERE d.status = 'active'
          AND (p_study IS NULL OR c.study IS NULL OR c.study = p_study)
    ),
    lex AS (
        SELECT chunk_id,
               row_number() OVER (ORDER BY ts_rank_cd(tsv, plainto_tsquery('english', q)) DESC) AS r
        FROM scoped WHERE tsv @@ plainto_tsquery('english', q) LIMIT 50
    ),
    vec AS (
        SELECT chunk_id, row_number() OVER (ORDER BY embedding <=> q_embedding) AS r
        FROM scoped WHERE q_embedding IS NOT NULL AND embedding IS NOT NULL LIMIT 50
    ),
    fused AS (
        SELECT coalesce(lex.chunk_id, vec.chunk_id) AS chunk_id,
               coalesce(1.0 / (p_k + lex.r), 0) + coalesce(1.0 / (p_k + vec.r), 0) AS score
        FROM lex FULL OUTER JOIN vec USING (chunk_id)
    )
    SELECT s.chunk_id, s.document_id, s.heading, s.body, s.sources, s.binding, f.score
    FROM fused f JOIN scoped s USING (chunk_id)
    ORDER BY f.score DESC
    LIMIT p_limit;
$$;

-- Lexical-only search. The runtime tries this FIRST and only pays for an
-- embedding when it comes back empty or weak: an embedding call measured
-- 280-470ms against a 5-20ms index scan, and on a phone call that gap is the
-- difference between thinking and silence.
CREATE OR REPLACE FUNCTION kb.search_lexical(
    q        text,
    p_study  text DEFAULT NULL,
    p_limit  int  DEFAULT 8
) RETURNS TABLE (chunk_id bigint, document_id text, heading text, body text,
                 sources jsonb, binding boolean, score double precision)
LANGUAGE sql STABLE
SET search_path = pg_catalog, public, rt, pg_temp
AS $$
    SELECT c.chunk_id, c.document_id, c.heading, c.body, c.sources, c.binding,
           ts_rank_cd(c.tsv, plainto_tsquery('english', q))::double precision AS score
    FROM kb.chunk c
    JOIN kb.document d ON d.id = c.document_id
    WHERE d.status = 'active'
      AND (p_study IS NULL OR c.study IS NULL OR c.study = p_study)
      AND c.tsv @@ plainto_tsquery('english', q)
    ORDER BY score DESC
    LIMIT p_limit;
$$;

-- ─── Load-time guards ────────────────────────────────────────
-- A {{TOKEN}} that survives into a running agent is a load-time failure that
-- escaped. kb/30-reference/contacts-and-routing.md is explicit that no contact
-- detail is ever constructed; this is the enforcement of that sentence.
CREATE OR REPLACE FUNCTION kb.assert_no_placeholders() RETURNS void
LANGUAGE plpgsql
SET search_path = pg_catalog, public, rt, pg_temp
AS $$
DECLARE offending text;
BEGIN
    SELECT string_agg(id, ', ') INTO offending
    FROM kb.document WHERE status = 'active' AND body ~ '\{\{[A-Z0-9_]+\}\}';
    IF offending IS NOT NULL THEN
        RAISE EXCEPTION 'unsubstituted placeholders in: %', offending;
    END IF;
END;
$$;

-- Every active retrieval document must have chunks, and every chunk must be
-- embedded. A half-loaded knowledge base answers some questions and silently
-- refuses others, which reads to a participant as the agent being unreliable
-- and to an auditor as a gap nobody noticed.
CREATE OR REPLACE FUNCTION kb.assert_loaded() RETURNS void
LANGUAGE plpgsql
SET search_path = pg_catalog, public, rt, pg_temp
AS $$
DECLARE bad text;
BEGIN
    SELECT string_agg(d.id, ', ') INTO bad
    FROM kb.document d
    WHERE d.status = 'active' AND d.injection = 'retrieval'
      AND NOT EXISTS (SELECT 1 FROM kb.chunk c WHERE c.document_id = d.id);
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'retrieval documents with no chunks: %', bad;
    END IF;

    SELECT string_agg(DISTINCT document_id, ', ') INTO bad
    FROM kb.chunk WHERE embedding IS NULL;
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'chunks with no embedding in: %', bad;
    END IF;
END;
$$;

-- ─── Lockdown (migration 09's rule, applied here) ────────────
-- The knowledge base is readable by the service role and by nothing else. anon
-- and authenticated are what a browser holds; neither has any business reading
-- protocol text or the retrieval log.
REVOKE ALL ON SCHEMA kb FROM PUBLIC, anon, authenticated;
GRANT USAGE ON SCHEMA kb TO service_role;

REVOKE ALL ON ALL TABLES IN SCHEMA kb FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA kb TO service_role;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA kb TO service_role;

REVOKE ALL ON FUNCTION kb.search(text, halfvec, text, int, int) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION kb.search_lexical(text, text, int) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION kb.assert_no_placeholders() FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION kb.assert_loaded() FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION kb.search(text, halfvec, text, int, int) TO service_role;
GRANT EXECUTE ON FUNCTION kb.search_lexical(text, text, int) TO service_role;
GRANT EXECUTE ON FUNCTION kb.assert_no_placeholders() TO service_role;
GRANT EXECUTE ON FUNCTION kb.assert_loaded() TO service_role;

-- PostgREST only exposes what it can see. The worker reaches these through
-- rpc/ wrappers in the public schema, so the kb schema itself stays unexposed.
CREATE OR REPLACE FUNCTION public.rt_kb_search_lexical(
    p_q text, p_study text DEFAULT NULL, p_limit int DEFAULT 8
) RETURNS TABLE (chunk_id bigint, document_id text, heading text, body text,
                 sources jsonb, binding boolean, score double precision)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public, rt, pg_temp
AS $$ SELECT * FROM kb.search_lexical(p_q, p_study, p_limit); $$;

CREATE OR REPLACE FUNCTION public.rt_kb_search(
    p_q text, p_embedding text, p_study text DEFAULT NULL, p_limit int DEFAULT 8
) RETURNS TABLE (chunk_id bigint, document_id text, heading text, body text,
                 sources jsonb, binding boolean, score double precision)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public, rt, pg_temp
AS $$ SELECT * FROM kb.search(p_q, p_embedding::halfvec(1536), p_study, p_limit); $$;

CREATE OR REPLACE FUNCTION public.rt_kb_log(
    p_session text, p_turn int, p_query text, p_chunk_ids bigint[],
    p_refused boolean DEFAULT false, p_escalated_to text DEFAULT NULL,
    p_build_id bigint DEFAULT NULL
) RETURNS void
LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, public, rt, pg_temp
AS $$
    INSERT INTO kb.retrieval_log (session_id, turn, build_id, query, chunk_ids, refused, escalated_to)
    VALUES (p_session, p_turn, p_build_id, p_query, p_chunk_ids, p_refused, p_escalated_to);
$$;

-- The pinned prompt the worker loads at boot: newest build for this study.
CREATE OR REPLACE FUNCTION public.rt_kb_prompt(p_study text DEFAULT NULL)
RETURNS TABLE (build_id bigint, prompt_text text, prompt_hash text, chars int)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public, rt, pg_temp
AS $$
    SELECT b.build_id, b.prompt_text, b.prompt_hash, b.chars
    FROM kb.prompt_build b
    WHERE p_study IS NULL OR b.study IS NULL OR b.study = p_study
    ORDER BY b.built_at DESC
    LIMIT 1;
$$;

REVOKE ALL ON FUNCTION public.rt_kb_search_lexical(text, text, int) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.rt_kb_search(text, text, text, int) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.rt_kb_log(text, int, text, bigint[], boolean, text, bigint) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.rt_kb_prompt(text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.rt_kb_search_lexical(text, text, int) TO service_role;
GRANT EXECUTE ON FUNCTION public.rt_kb_search(text, text, text, int) TO service_role;
GRANT EXECUTE ON FUNCTION public.rt_kb_log(text, int, text, bigint[], boolean, text, bigint) TO service_role;
GRANT EXECUTE ON FUNCTION public.rt_kb_prompt(text) TO service_role;

COMMENT ON SCHEMA kb IS
    'Protocol knowledge base. Deliberately outside rt: a forget-me erases a caller, never the study.';
