-- ============================================================
-- iris-realtime bootstrap schema v5.2
-- Run once via Supabase SQL editor on a fresh project.
-- v5.2 adds: rt_set_loved_ones, rt_wipe_all_data, rt_get_all_callers
-- ============================================================

CREATE SCHEMA IF NOT EXISTS rt;

DROP FUNCTION IF EXISTS rt_set_display_name(text, text);
DROP FUNCTION IF EXISTS rt_set_agent_alias(text, text);


-- ─── TABLE 1: Caller Profiles ────────────────────────────────
CREATE TABLE IF NOT EXISTS rt.callers (
    phone_hash          TEXT PRIMARY KEY,
    display_name        TEXT        DEFAULT 'Friend',
    agent_alias         TEXT        DEFAULT 'your companion',

    pronunciation_note  TEXT        DEFAULT 'Spoken clearly as provided',
    voice_pref          TEXT        DEFAULT 'Aoede',
    summary             TEXT,
    loved_ones          TEXT,
    active_loop         TEXT,
    fenced_topics       TEXT,
    caller_rules        TEXT,
    persona_directives  TEXT,
    next_call_context   TEXT,
    call_count          INT         DEFAULT 0,
    last_call_at        TIMESTAMPTZ DEFAULT NOW(),
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

-- ─── TABLE 2: Dynamic Domain Schema Registry ─────────────────
CREATE TABLE IF NOT EXISTS rt.account_schema_registry (
    id                  BIGSERIAL   PRIMARY KEY,
    phone_hash          TEXT        NOT NULL,
    table_name          TEXT        NOT NULL,
    category            TEXT        NOT NULL,
    data_summary        TEXT        NOT NULL,
    last_discussed_at   TIMESTAMPTZ DEFAULT NOW(),
    created_at          TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_asr_hash_recency
    ON rt.account_schema_registry (phone_hash, last_discussed_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_asr_hash_category_unique
    ON rt.account_schema_registry (phone_hash, lower(category));

-- ─── TABLE 3: Reminders ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS rt.reminders (
    id              BIGSERIAL   PRIMARY KEY,
    phone_hash      TEXT        NOT NULL,
    reminder_text   TEXT        NOT NULL,
    due_time_str    TEXT,
    is_done         BOOLEAN     DEFAULT FALSE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_reminders_hash ON rt.reminders (phone_hash);


-- ════════════════════════════════════════════════════════════
-- RPCs
-- ════════════════════════════════════════════════════════════

-- RPC: rt_get_caller
CREATE OR REPLACE FUNCTION public.rt_get_caller(p_hash TEXT)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE v JSONB;
BEGIN
    SELECT jsonb_build_object(
        'voice_pref', voice_pref, 'call_count', call_count,
        'last_call_at', last_call_at, 'display_name', display_name,
        'agent_alias', agent_alias, 'caller_rules', caller_rules,
        'persona_directives', persona_directives,
        'pronunciation_note', pronunciation_note, 'summary', summary,
        'loved_ones', loved_ones, 'active_loop', active_loop,
        'fenced_topics', fenced_topics, 'next_call_context', next_call_context
    ) INTO v FROM rt.callers WHERE phone_hash = p_hash;
    RETURN COALESCE(v, jsonb_build_object(
        'voice_pref', NULL, 'call_count', 0, 'last_call_at', NULL,
        'display_name', 'Friend', 'agent_alias', 'your companion',

        'caller_rules', NULL, 'persona_directives', NULL,
        'pronunciation_note', 'Spoken clearly as provided',
        'summary', NULL, 'loved_ones', NULL, 'active_loop', NULL,
        'fenced_topics', NULL, 'next_call_context', NULL
    ));
END; $$;

-- RPC: rt_set_agent_alias
CREATE OR REPLACE FUNCTION public.rt_set_agent_alias(p_hash TEXT, p_alias TEXT)
RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    INSERT INTO rt.callers (phone_hash, agent_alias, last_call_at)
    VALUES (p_hash, p_alias, NOW())
    ON CONFLICT (phone_hash) DO UPDATE SET agent_alias = p_alias;
END; $$;

-- RPC: rt_set_caller_rules
CREATE OR REPLACE FUNCTION public.rt_set_caller_rules(p_hash TEXT, p_rules TEXT)
RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    INSERT INTO rt.callers (phone_hash, caller_rules, last_call_at)
    VALUES (p_hash, p_rules, NOW())
    ON CONFLICT (phone_hash) DO UPDATE SET caller_rules = p_rules;
END; $$;

-- RPC: rt_set_persona_directives
CREATE OR REPLACE FUNCTION public.rt_set_persona_directives(p_hash TEXT, p_directives TEXT)
RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    INSERT INTO rt.callers (phone_hash, persona_directives, last_call_at)
    VALUES (p_hash, p_directives, NOW())
    ON CONFLICT (phone_hash) DO UPDATE SET persona_directives = p_directives;
END; $$;

-- RPC: rt_set_voice
CREATE OR REPLACE FUNCTION public.rt_set_voice(p_hash TEXT, p_voice TEXT)
RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    INSERT INTO rt.callers (phone_hash, voice_pref, last_call_at)
    VALUES (p_hash, p_voice, NOW())
    ON CONFLICT (phone_hash) DO UPDATE SET voice_pref = p_voice, last_call_at = NOW();
END; $$;

-- RPC: rt_bump_call
CREATE OR REPLACE FUNCTION public.rt_bump_call(p_hash TEXT)
RETURNS INT LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE v_count INT;
BEGIN
    INSERT INTO rt.callers (phone_hash, call_count, last_call_at)
    VALUES (p_hash, 1, NOW())
    ON CONFLICT (phone_hash)
    DO UPDATE SET call_count = rt.callers.call_count + 1, last_call_at = NOW()
    RETURNING call_count INTO v_count;
    RETURN v_count;
END; $$;

-- RPC: rt_set_display_name
CREATE OR REPLACE FUNCTION public.rt_set_display_name(p_hash TEXT, p_name TEXT)
RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    INSERT INTO rt.callers (phone_hash, display_name, last_call_at)
    VALUES (p_hash, p_name, NOW())
    ON CONFLICT (phone_hash) DO UPDATE SET display_name = p_name;
END; $$;

-- RPC: rt_set_loved_ones  [NEW in v5.2]
CREATE OR REPLACE FUNCTION public.rt_set_loved_ones(p_hash TEXT, p_loved_ones TEXT)
RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    INSERT INTO rt.callers (phone_hash, loved_ones, last_call_at)
    VALUES (p_hash, p_loved_ones, NOW())
    ON CONFLICT (phone_hash) DO UPDATE SET loved_ones = p_loved_ones;
END; $$;

-- RPC: rt_set_caller_context
CREATE OR REPLACE FUNCTION public.rt_set_caller_context(p_hash TEXT, p_context TEXT)
RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    INSERT INTO rt.callers (phone_hash, next_call_context, last_call_at)
    VALUES (p_hash, p_context, NOW())
    ON CONFLICT (phone_hash) DO UPDATE SET next_call_context = p_context;
END; $$;

-- RPC: rt_get_caller_full_bundle
CREATE OR REPLACE FUNCTION public.rt_get_caller_full_bundle(p_hash TEXT)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE v_caller JSONB; v_schemas JSONB; v_reminders JSONB;
BEGIN
    SELECT jsonb_build_object(
        'voice_pref', voice_pref, 'call_count', call_count,
        'last_call_at', last_call_at, 'display_name', display_name,
        'agent_alias', agent_alias, 'caller_rules', caller_rules,
        'persona_directives', persona_directives,
        'pronunciation_note', pronunciation_note, 'summary', summary,
        'loved_ones', loved_ones, 'active_loop', active_loop,
        'fenced_topics', fenced_topics, 'next_call_context', next_call_context
    ) INTO v_caller FROM rt.callers WHERE phone_hash = p_hash;

    v_caller := COALESCE(v_caller, jsonb_build_object(
        'voice_pref', NULL, 'call_count', 0, 'last_call_at', NULL,
        'display_name', 'Friend', 'agent_alias', 'your companion',

        'caller_rules', NULL, 'persona_directives', NULL,
        'pronunciation_note', 'Spoken clearly as provided',
        'summary', NULL, 'loved_ones', NULL, 'active_loop', NULL,
        'fenced_topics', NULL, 'next_call_context', NULL
    ));

    SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'table_name', table_name, 'category', category,
        'data_summary', data_summary, 'last_discussed_at', last_discussed_at
    ) ORDER BY last_discussed_at DESC), '[]') INTO v_schemas
    FROM rt.account_schema_registry WHERE phone_hash = p_hash;

    SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'id', id, 'reminder_text', reminder_text, 'due_time_str', due_time_str
    )), '[]') INTO v_reminders
    FROM rt.reminders WHERE phone_hash = p_hash AND is_done = FALSE;

    RETURN jsonb_build_object('caller', v_caller, 'schemas', v_schemas, 'reminders', v_reminders);
END; $$;

-- RPC: rt_add_schema_entry
CREATE OR REPLACE FUNCTION public.rt_add_schema_entry(
    p_hash TEXT, p_table TEXT, p_cat TEXT, p_summary TEXT
)
RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_new_json JSONB;
    v_old_json JSONB;
    v_merged JSONB;
BEGIN
    BEGIN
        v_new_json := p_summary::jsonb;
    EXCEPTION WHEN OTHERS THEN
        v_new_json := jsonb_build_object('notes', p_summary);
    END;

    SELECT data_summary::jsonb INTO v_old_json
    FROM rt.account_schema_registry
    WHERE phone_hash = p_hash AND lower(category) = lower(p_cat);

    IF v_old_json IS NOT NULL AND jsonb_typeof(v_old_json) = 'object' AND jsonb_typeof(v_new_json) = 'object' THEN
        v_merged := v_old_json || v_new_json;
    ELSE
        v_merged := v_new_json;
    END IF;

    INSERT INTO rt.account_schema_registry
        (phone_hash, table_name, category, data_summary, last_discussed_at)
    VALUES (p_hash, COALESCE(p_table, 'caller_schema_' || lower(p_cat)), lower(p_cat), v_merged::text, NOW())
    ON CONFLICT (phone_hash, lower(category))
    DO UPDATE SET data_summary = v_merged::text,
                  table_name = COALESCE(EXCLUDED.table_name, rt.account_schema_registry.table_name),
                  last_discussed_at = NOW();
END; $$;

-- RPC: rt_remove_schema_entry
CREATE OR REPLACE FUNCTION public.rt_remove_schema_entry(p_hash TEXT, p_item TEXT)
RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    IF lower(p_item) IN ('everything', 'wipe', 'all', 'reset', 'trash all notes and reset') THEN
        DELETE FROM rt.account_schema_registry WHERE phone_hash = p_hash;
        DELETE FROM rt.reminders WHERE phone_hash = p_hash;
        UPDATE rt.callers
        SET next_call_context = NULL,
            agent_alias = 'your companion',
            caller_rules = NULL,
            persona_directives = NULL,
            loved_ones = NULL
        WHERE phone_hash = p_hash;
    ELSE
        DELETE FROM rt.account_schema_registry
        WHERE phone_hash = p_hash
          AND lower(category) LIKE '%' || lower(p_item) || '%';
    END IF;
END; $$;

-- RPC: rt_add_reminder
CREATE OR REPLACE FUNCTION public.rt_add_reminder(p_hash TEXT, p_text TEXT, p_due TEXT DEFAULT NULL)
RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    INSERT INTO rt.reminders (phone_hash, reminder_text, due_time_str)
    VALUES (p_hash, p_text, p_due);
END; $$;

-- RPC: rt_wipe_all_data  [NEW in v5.2 — used by sql_push.py --wipe]
CREATE OR REPLACE FUNCTION public.rt_wipe_all_data()
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    n_callers INT; n_schemas INT; n_reminders INT; n_calls INT := 0;
BEGIN
    SELECT COUNT(*) INTO n_callers FROM rt.callers;
    SELECT COUNT(*) INTO n_schemas FROM rt.account_schema_registry;
    SELECT COUNT(*) INTO n_reminders FROM rt.reminders;
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='rt' AND table_name='calls') THEN
        SELECT COUNT(*) INTO n_calls FROM rt.calls;
        TRUNCATE rt.calls RESTART IDENTITY CASCADE;
    END IF;
    TRUNCATE rt.callers RESTART IDENTITY CASCADE;
    TRUNCATE rt.account_schema_registry RESTART IDENTITY CASCADE;
    TRUNCATE rt.reminders RESTART IDENTITY CASCADE;
    RETURN jsonb_build_object(
        'callers_wiped', n_callers,
        'schemas_wiped', n_schemas,
        'reminders_wiped', n_reminders,
        'calls_wiped', n_calls
    );
END; $$;

-- RPC: rt_get_all_callers  [NEW in v5.2 — admin/debug use]
CREATE OR REPLACE FUNCTION public.rt_get_all_callers()
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    RETURN (
        SELECT COALESCE(jsonb_agg(jsonb_build_object(
            'phone_hash', phone_hash,
            'display_name', display_name,
            'agent_alias', agent_alias,
            'call_count', call_count,
            'last_call_at', last_call_at,
            'caller_rules', caller_rules,
            'loved_ones', loved_ones,
            'ncc_chars', length(COALESCE(next_call_context, ''))
        ) ORDER BY call_count DESC), '[]')
        FROM rt.callers
    );
END; $$;


-- ─── Permissions ─────────────────────────────────────────────
ALTER TABLE rt.callers                 DISABLE ROW LEVEL SECURITY;
ALTER TABLE rt.account_schema_registry DISABLE ROW LEVEL SECURITY;
ALTER TABLE rt.reminders               DISABLE ROW LEVEL SECURITY;

GRANT EXECUTE ON FUNCTION public.rt_get_caller(TEXT)                           TO PUBLIC;
GRANT EXECUTE ON FUNCTION public.rt_set_agent_alias(TEXT,TEXT)                 TO PUBLIC;
GRANT EXECUTE ON FUNCTION public.rt_set_caller_rules(TEXT,TEXT)                TO PUBLIC;
GRANT EXECUTE ON FUNCTION public.rt_set_persona_directives(TEXT,TEXT)          TO PUBLIC;
GRANT EXECUTE ON FUNCTION public.rt_set_voice(TEXT,TEXT)                       TO PUBLIC;
GRANT EXECUTE ON FUNCTION public.rt_bump_call(TEXT)                            TO PUBLIC;
GRANT EXECUTE ON FUNCTION public.rt_set_display_name(TEXT,TEXT)                TO PUBLIC;
GRANT EXECUTE ON FUNCTION public.rt_set_loved_ones(TEXT,TEXT)                  TO PUBLIC;
GRANT EXECUTE ON FUNCTION public.rt_set_caller_context(TEXT,TEXT)              TO PUBLIC;
GRANT EXECUTE ON FUNCTION public.rt_get_caller_full_bundle(TEXT)               TO PUBLIC;
GRANT EXECUTE ON FUNCTION public.rt_add_schema_entry(TEXT,TEXT,TEXT,TEXT)      TO PUBLIC;
GRANT EXECUTE ON FUNCTION public.rt_remove_schema_entry(TEXT,TEXT)             TO PUBLIC;
GRANT EXECUTE ON FUNCTION public.rt_add_reminder(TEXT,TEXT,TEXT)               TO PUBLIC;
GRANT EXECUTE ON FUNCTION public.rt_wipe_all_data()                            TO PUBLIC;
GRANT EXECUTE ON FUNCTION public.rt_get_all_callers()                          TO PUBLIC;
