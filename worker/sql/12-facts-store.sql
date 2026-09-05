-- ============================================================
-- The fact store — Phase 1 of the memory plan (2026-08-14)
--
-- Until now a caller's memory was prose in flat text columns on rt.callers:
-- `loved_ones` is literally a sentence. No row per fact, no provenance, no
-- confidence, no per-fact timestamps, and contradiction handling was
-- overwrite-or-append. This table is the record of truth the later phases
-- (entities, hybrid retrieval, budgeted hydration) build on.
--
-- Two design laws, fixed here and not renegotiated later:
--   · DETERMINISM OWNS TRUTH. A fact's identity is its norm_key (kind+subject,
--     normalized). Re-hearing the same value INCREMENTS confirmations — never
--     duplicates. A different value SUPERSEDES the old row — never deletes it.
--     Probability (ranking, similarity, decay) never decides what is true.
--   · NOTHING IS ERASED EXCEPT BY THE CALLER. Supersession keeps history;
--     "forget me" (extended below) removes everything, to the letter.
--
-- Phase 1 is write-side only: the post-call worker dual-writes facts alongside
-- the existing prose columns. Nothing reads this table into the prompt yet, so
-- live call behavior is unchanged. Phase 4 adds embeddings (column is already
-- here); Phase 7 switches hydration to read ranked facts under per-kind caps.
--
-- Apply: python sql_push.py --file sql/12-facts-store.sql
-- ============================================================

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS rt.facts (
    id            BIGSERIAL    PRIMARY KEY,
    phone_hash    TEXT         NOT NULL,
    kind          TEXT         NOT NULL,   -- family|pet|health|hobby|rule|persona|wellbeing|thread|identity|misc
    subject       TEXT         NOT NULL,   -- who/what it is about ("Mary", "migraines", "chess")
    norm_key      TEXT         NOT NULL,   -- kind:slug(subject) — the deterministic identity
    predicate     TEXT,                    -- short relation ("daughter", "condition", "likes")
    value_json    JSONB        NOT NULL DEFAULT '{}'::jsonb,
    value_text    TEXT,                    -- rendered one-line form for prompts & search
    confidence    REAL         NOT NULL DEFAULT 1.0,
    confirmations INT          NOT NULL DEFAULT 1,
    contradictions INT         NOT NULL DEFAULT 0,
    status        TEXT         NOT NULL DEFAULT 'active',  -- active|superseded|retired
    superseded_by BIGINT,
    source_call_id TEXT,                   -- provenance: the call that taught it
    first_seen    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_seen     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_confirmed_at TIMESTAMPTZ,
    embedding     vector(768)              -- Phase 4 fills this; NULL until then
);
-- One ACTIVE row per fact identity per caller. Superseded/retired history may
-- share the key, so the uniqueness is partial.
CREATE UNIQUE INDEX IF NOT EXISTS uq_facts_active_key
    ON rt.facts (phone_hash, norm_key) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_facts_hash_kind ON rt.facts (phone_hash, kind, status);
CREATE INDEX IF NOT EXISTS idx_facts_ranked
    ON rt.facts (phone_hash, status, confidence DESC, last_seen DESC);
ALTER TABLE rt.facts DISABLE ROW LEVEL SECURITY;


-- ─── Upsert: the only write path ─────────────────────────────
-- Same key + same value  -> confirmations+1, confidence+0.5 (capped), touch.
-- Same key + new value   -> old row status='superseded' (+contradictions),
--                           new active row pointing back via superseded_by
--                           chain (old.superseded_by = new.id).
-- No active row          -> insert.
-- Value equality is on value_text (the rendered form), which is what a human
-- would call "the same thing"; value_json rides along as the structured form.
CREATE OR REPLACE FUNCTION public.rt_upsert_fact(
    p_hash TEXT, p_kind TEXT, p_subject TEXT, p_norm_key TEXT,
    p_predicate TEXT, p_value_json JSONB, p_value_text TEXT,
    p_source_call_id TEXT DEFAULT NULL
)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_old rt.facts%ROWTYPE;
    v_new_id BIGINT;
BEGIN
    -- RESURRECTION GUARD (review finding, blocking): the recovery drain can
    -- process a call whose caller has since said "forget me" — their
    -- rt.callers row is gone, and writing facts for the hash would rebuild a
    -- profile they asked us to erase. No caller row, no facts. On the live
    -- path rt_bump_call upserts the row before extraction ever runs, so this
    -- never refuses a real call.
    IF NOT EXISTS (SELECT 1 FROM rt.callers WHERE phone_hash = p_hash) THEN
        RETURN jsonb_build_object('op', 'refused', 'reason', 'no caller row');
    END IF;

    SELECT * INTO v_old FROM rt.facts
     WHERE phone_hash = p_hash AND norm_key = p_norm_key AND status = 'active'
     FOR UPDATE;

    IF v_old.id IS NULL THEN
        -- Two first-writes can race here; the loser hits the partial unique
        -- index. Retry once as a confirm/supersede against the winner.
        BEGIN
            INSERT INTO rt.facts (phone_hash, kind, subject, norm_key, predicate,
                                  value_json, value_text, source_call_id)
            VALUES (p_hash, p_kind, p_subject, p_norm_key, p_predicate,
                    COALESCE(p_value_json, '{}'::jsonb), p_value_text, p_source_call_id)
            RETURNING id INTO v_new_id;
            RETURN jsonb_build_object('op', 'insert', 'id', v_new_id);
        EXCEPTION WHEN unique_violation THEN
            SELECT * INTO v_old FROM rt.facts
             WHERE phone_hash = p_hash AND norm_key = p_norm_key AND status = 'active'
             FOR UPDATE;
            IF v_old.id IS NULL THEN
                RETURN jsonb_build_object('op', 'failed', 'reason', 'race unresolved');
            END IF;
        END;
    END IF;

    IF COALESCE(v_old.value_text, '') = COALESCE(p_value_text, '') THEN
        UPDATE rt.facts SET
            confirmations = confirmations + 1,
            confidence = LEAST(confidence + 0.5, 10.0),
            last_seen = NOW(), last_confirmed_at = NOW()
            -- source_call_id deliberately untouched: it names the call that
            -- TAUGHT the fact. Confirmations are counted, not re-attributed.
        WHERE id = v_old.id;
        RETURN jsonb_build_object('op', 'confirm', 'id', v_old.id,
                                  'confirmations', v_old.confirmations + 1);
    END IF;

    -- Contradiction: supersede, never delete. History keeps the old value so
    -- the companion can say "you'd told me Denver before — Boston now?"
    -- ORDER MATTERS: the partial unique index allows one ACTIVE row per key,
    -- so the old row must leave 'active' BEFORE the new row is inserted.
    -- All three statements share this function's transaction — no window
    -- where the fact has zero active rows is visible to any other reader.
    UPDATE rt.facts SET
        status = 'superseded',
        contradictions = contradictions + 1,
        last_seen = NOW()
    WHERE id = v_old.id;

    INSERT INTO rt.facts (phone_hash, kind, subject, norm_key, predicate,
                          value_json, value_text, source_call_id,
                          first_seen, contradictions)
    VALUES (p_hash, p_kind, p_subject, p_norm_key, p_predicate,
            COALESCE(p_value_json, '{}'::jsonb), p_value_text, p_source_call_id, NOW(),
            v_old.contradictions + 1)   -- churn count survives supersession,
                                        -- so Phase 6 can see a flip-flopping key
    RETURNING id INTO v_new_id;

    UPDATE rt.facts SET superseded_by = v_new_id WHERE id = v_old.id;

    RETURN jsonb_build_object('op', 'supersede', 'id', v_new_id, 'old_id', v_old.id);
END; $$;

-- ─── Ranked read (Phase 7 will consume this; console can use it today) ──
CREATE OR REPLACE FUNCTION public.rt_get_facts(
    p_hash TEXT, p_kind TEXT DEFAULT NULL, p_limit INT DEFAULT 50
)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    RETURN (SELECT COALESCE(jsonb_agg(row_to_json(f)::jsonb), '[]'::jsonb)
        FROM (SELECT id, kind, subject, predicate, value_text, value_json,
                     confidence, confirmations, contradictions,
                     first_seen, last_seen, source_call_id
                FROM rt.facts
               WHERE phone_hash = p_hash AND status = 'active'
                 AND (p_kind IS NULL OR kind = p_kind)
               ORDER BY (kind = 'correction') DESC,
                        confidence DESC, last_seen DESC
               LIMIT COALESCE(p_limit, 50)) f);
END; $$;


-- ─── Soft retire (the model may request; capped by the caller in code) ──
CREATE OR REPLACE FUNCTION public.rt_retire_fact(p_hash TEXT, p_id BIGINT)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE n INT;
BEGIN
    UPDATE rt.facts SET status = 'retired', last_seen = NOW()
     WHERE id = p_id AND phone_hash = p_hash AND status = 'active';
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n > 0;
END; $$;


-- ─── forget_me now erases facts too — the brand law, to the letter ──
CREATE OR REPLACE FUNCTION public.rt_forget_caller(p_hash TEXT)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE n_s INT; n_r INT; n_c INT; n_e INT; n_a INT; n_cal INT; n_f INT; n_j INT;
BEGIN
    DELETE FROM rt.account_schema_registry WHERE phone_hash = p_hash;
    GET DIAGNOSTICS n_s = ROW_COUNT;
    DELETE FROM rt.reminders WHERE phone_hash = p_hash;
    GET DIAGNOSTICS n_r = ROW_COUNT;
    DELETE FROM rt.facts WHERE phone_hash = p_hash;
    GET DIAGNOSTICS n_f = ROW_COUNT;
    BEGIN
        DELETE FROM rt.postcall_jobs WHERE phone_hash = p_hash;
        GET DIAGNOSTICS n_j = ROW_COUNT;
    EXCEPTION WHEN undefined_table THEN n_j := 0;
    END;
    -- traces first (they reference the call), then the caller row
    DELETE FROM rt.call_events WHERE call_id IN
        (SELECT call_id FROM rt.calls WHERE phone_hash = p_hash);
    GET DIAGNOSTICS n_e = ROW_COUNT;
    DELETE FROM rt.calls WHERE phone_hash = p_hash;
    GET DIAGNOSTICS n_cal = ROW_COUNT;
    BEGIN
        DELETE FROM rt.audit_log WHERE phone_hash = p_hash;
        GET DIAGNOSTICS n_a = ROW_COUNT;
    EXCEPTION WHEN undefined_table THEN n_a := 0;
    END;
    DELETE FROM rt.callers WHERE phone_hash = p_hash;
    GET DIAGNOSTICS n_c = ROW_COUNT;
    RETURN jsonb_build_object('schemas', n_s, 'reminders', n_r, 'caller', n_c,
                              'calls', n_cal, 'call_events', n_e, 'audit', n_a,
                              'facts', n_f, 'postcall_jobs', n_j);
END; $$;


REVOKE ALL ON FUNCTION public.rt_upsert_fact(TEXT,TEXT,TEXT,TEXT,TEXT,JSONB,TEXT,TEXT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.rt_get_facts(TEXT,TEXT,INT)   FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.rt_retire_fact(TEXT,BIGINT)   FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.rt_forget_caller(TEXT)        FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.rt_upsert_fact(TEXT,TEXT,TEXT,TEXT,TEXT,JSONB,TEXT,TEXT) TO service_role;
GRANT EXECUTE ON FUNCTION public.rt_get_facts(TEXT,TEXT,INT)   TO service_role;
GRANT EXECUTE ON FUNCTION public.rt_retire_fact(TEXT,BIGINT)   TO service_role;
GRANT EXECUTE ON FUNCTION public.rt_forget_caller(TEXT)        TO service_role;
