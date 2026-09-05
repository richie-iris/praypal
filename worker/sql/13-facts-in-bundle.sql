-- ============================================================
-- Facts ride the pickup bundle — the read half of the streamline (2026-08-15)
--
-- The hydrator gets exactly one prefetched RPC's worth of context at ring
-- pickup; anything not in the bundle costs a second round trip on a path
-- budgeted in milliseconds. To make rt.facts the prompt's knowledge source,
-- the bundle now carries the top-ranked active facts. Ranking: corrections
-- first (a caller's correction outranks everything — never repeat a corrected
-- mistake), then confidence (confirmations), then recency. The hydrator
-- applies per-kind caps and the total prompt budget; the bundle just delivers
-- the shortlist.
--
-- Everything else in this function is byte-identical to the previous live
-- definition (including the missing-caller COALESCE defaults) — verified
-- against pg_get_functiondef before editing. Only v_facts and the 'facts'
-- key are new.
--
-- Apply: python sql_push.py --file sql/13-facts-in-bundle.sql
-- ============================================================

CREATE OR REPLACE FUNCTION public.rt_get_caller_full_bundle(p_hash TEXT)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE v_caller JSONB; v_schemas JSONB; v_reminders JSONB; v_facts JSONB;
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
        'display_name', 'Friend', 'last_name', NULL, 'agent_alias', 'Iris',
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

    SELECT COALESCE(jsonb_agg(row_to_json(f)::jsonb), '[]') INTO v_facts
      FROM (SELECT kind, subject, predicate, value_text,
                   confidence, confirmations
              FROM rt.facts
             WHERE phone_hash = p_hash AND status = 'active'
             ORDER BY (kind = 'correction') DESC,
                      confidence DESC, last_seen DESC
             LIMIT 40) f;

    RETURN jsonb_build_object('caller', v_caller, 'schemas', v_schemas,
                              'reminders', v_reminders, 'facts', v_facts);
END; $$;

REVOKE ALL ON FUNCTION public.rt_get_caller_full_bundle(TEXT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.rt_get_caller_full_bundle(TEXT) TO service_role;
