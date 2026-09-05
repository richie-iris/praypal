-- ============================================================
-- Continuity — 2026-08-06
-- The hydrator needs the caller's first-contact date to render
-- "You first met Richie on August 6, 2026" (stage invented a
-- last-call date because the prompt carried no real timestamps).
-- Adds created_at to both caller getters. Call history itself
-- lives in account_schema_registry under category 'call_log'
-- (no DDL needed). Apply to dev (lbbshvoozkqeazdbeeyt) and
-- stage (ojoppcyvkxwfuwzjjxbw); never the old-stack prod.
-- ============================================================

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
        'fenced_topics', fenced_topics, 'next_call_context', next_call_context,
        'created_at', created_at
    ) INTO v FROM rt.callers WHERE phone_hash = p_hash;
    RETURN COALESCE(v, jsonb_build_object(
        'voice_pref', NULL, 'call_count', 0, 'last_call_at', NULL,
        'display_name', 'Friend', 'agent_alias', 'your companion',
        'caller_rules', NULL, 'persona_directives', NULL,
        'pronunciation_note', 'Spoken clearly as provided',
        'summary', NULL, 'loved_ones', NULL, 'active_loop', NULL,
        'fenced_topics', NULL, 'next_call_context', NULL,
        'created_at', NULL
    ));
END; $$;

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
        'fenced_topics', fenced_topics, 'next_call_context', next_call_context,
        'created_at', created_at
    ) INTO v_caller FROM rt.callers WHERE phone_hash = p_hash;

    v_caller := COALESCE(v_caller, jsonb_build_object(
        'voice_pref', NULL, 'call_count', 0, 'last_call_at', NULL,
        'display_name', 'Friend', 'agent_alias', 'your companion',
        'caller_rules', NULL, 'persona_directives', NULL,
        'pronunciation_note', 'Spoken clearly as provided',
        'summary', NULL, 'loved_ones', NULL, 'active_loop', NULL,
        'fenced_topics', NULL, 'next_call_context', NULL,
        'created_at', NULL
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
