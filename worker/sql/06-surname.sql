-- ============================================================
-- Surname — 2026-08-07
-- There was no place to put a last name, so every correction went through
-- action="name" and overwrote the FIRST name. Richie corrected his surname
-- four times on one call; the net result was display_name back to 'Richie'
-- and the original mishearing ('Etwaroo') stranded in a general note.
--
-- display_name stays what she CALLS them. last_name is the family name,
-- corrected independently and never at the cost of the other.
-- Apply: python sql_push.py --file sql/06-surname.sql
-- ============================================================

ALTER TABLE rt.callers ADD COLUMN IF NOT EXISTS last_name TEXT;

CREATE OR REPLACE FUNCTION public.rt_set_last_name(p_hash TEXT, p_last TEXT)
RETURNS TEXT LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE v TEXT;
BEGIN
    INSERT INTO rt.callers (phone_hash, last_name, last_call_at)
    VALUES (p_hash, p_last, NOW())
    ON CONFLICT (phone_hash) DO UPDATE SET last_name = p_last
    RETURNING last_name INTO v;
    RETURN v;   -- returns what is NOW stored, so the agent can read it back
END; $$;

-- The name setters return the stored value too: she claimed twice on one call
-- to have saved a spelling she had not. Now the tool result IS the database.
-- Postgres will not replace a function whose return type changed (VOID -> TEXT),
-- so these two are dropped and rebuilt, then re-granted.
DROP FUNCTION IF EXISTS public.rt_set_display_name(TEXT,TEXT);
DROP FUNCTION IF EXISTS public.rt_set_agent_alias(TEXT,TEXT);

CREATE OR REPLACE FUNCTION public.rt_set_display_name(p_hash TEXT, p_name TEXT)
RETURNS TEXT LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE v TEXT;
BEGIN
    INSERT INTO rt.callers (phone_hash, display_name, last_call_at)
    VALUES (p_hash, p_name, NOW())
    ON CONFLICT (phone_hash) DO UPDATE SET display_name = p_name
    RETURNING display_name INTO v;
    RETURN v;
END; $$;

CREATE OR REPLACE FUNCTION public.rt_set_agent_alias(p_hash TEXT, p_alias TEXT)
RETURNS TEXT LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE v TEXT;
BEGIN
    INSERT INTO rt.callers (phone_hash, agent_alias, last_call_at)
    VALUES (p_hash, p_alias, NOW())
    ON CONFLICT (phone_hash) DO UPDATE SET agent_alias = p_alias
    RETURNING agent_alias INTO v;
    RETURN v;
END; $$;

-- Both getters carry last_name so the prompt can use the whole name.
CREATE OR REPLACE FUNCTION public.rt_get_caller(p_hash TEXT)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE v JSONB;
BEGIN
    SELECT jsonb_build_object(
        'voice_pref', voice_pref, 'call_count', call_count,
        'last_call_at', last_call_at, 'display_name', display_name,
        'last_name', last_name,
        'agent_alias', agent_alias, 'caller_rules', caller_rules,
        'persona_directives', persona_directives,
        'pronunciation_note', pronunciation_note, 'summary', summary,
        'loved_ones', loved_ones, 'active_loop', active_loop,
        'fenced_topics', fenced_topics, 'next_call_context', next_call_context,
        'created_at', created_at
    ) INTO v FROM rt.callers WHERE phone_hash = p_hash;
    RETURN COALESCE(v, jsonb_build_object(
        'voice_pref', NULL, 'call_count', 0, 'last_call_at', NULL,
        'display_name', 'Friend', 'last_name', NULL, 'agent_alias', 'your companion',
        'caller_rules', NULL, 'persona_directives', NULL,
        'pronunciation_note', 'Spoken clearly as provided',
        'summary', NULL, 'loved_ones', NULL, 'active_loop', NULL,
        'fenced_topics', NULL, 'next_call_context', NULL, 'created_at', NULL
    ));
END; $$;

CREATE OR REPLACE FUNCTION public.rt_get_caller_full_bundle(p_hash TEXT)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE v_caller JSONB; v_schemas JSONB; v_reminders JSONB;
BEGIN
    SELECT jsonb_build_object(
        'voice_pref', voice_pref, 'call_count', call_count,
        'last_call_at', last_call_at, 'display_name', display_name,
        'last_name', last_name,
        'agent_alias', agent_alias, 'caller_rules', caller_rules,
        'persona_directives', persona_directives,
        'pronunciation_note', pronunciation_note, 'summary', summary,
        'loved_ones', loved_ones, 'active_loop', active_loop,
        'fenced_topics', fenced_topics, 'next_call_context', next_call_context,
        'created_at', created_at
    ) INTO v_caller FROM rt.callers WHERE phone_hash = p_hash;

    v_caller := COALESCE(v_caller, jsonb_build_object(
        'voice_pref', NULL, 'call_count', 0, 'last_call_at', NULL,
        'display_name', 'Friend', 'last_name', NULL, 'agent_alias', 'your companion',
        'caller_rules', NULL, 'persona_directives', NULL,
        'pronunciation_note', 'Spoken clearly as provided',
        'summary', NULL, 'loved_ones', NULL, 'active_loop', NULL,
        'fenced_topics', NULL, 'next_call_context', NULL, 'created_at', NULL
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

GRANT EXECUTE ON FUNCTION public.rt_set_last_name(TEXT,TEXT)    TO PUBLIC;
GRANT EXECUTE ON FUNCTION public.rt_set_display_name(TEXT,TEXT) TO PUBLIC;
GRANT EXECUTE ON FUNCTION public.rt_set_agent_alias(TEXT,TEXT)  TO PUBLIC;
