-- ============================================================
-- iris-realtime migration v5.2 — new RPCs
-- Run this in the Supabase SQL Editor (Dashboard → SQL Editor)
-- or via: python sql_push.py --file sql/02-loved-ones-and-admin-rpcs.sql
-- ============================================================

-- RPC: rt_set_loved_ones
-- Persists the loved_ones summary string for a caller.
CREATE OR REPLACE FUNCTION public.rt_set_loved_ones(p_hash TEXT, p_loved_ones TEXT)
RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    INSERT INTO rt.callers (phone_hash, loved_ones, last_call_at)
    VALUES (p_hash, p_loved_ones, NOW())
    ON CONFLICT (phone_hash) DO UPDATE SET loved_ones = p_loved_ones;
END; $$;
GRANT EXECUTE ON FUNCTION public.rt_set_loved_ones(TEXT,TEXT) TO PUBLIC;


-- RPC: rt_wipe_all_data
-- Truncates all rt.* tables. Used by sql_push.py --wipe.
-- WARNING: Irreversible. Wipes ALL caller data.
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
GRANT EXECUTE ON FUNCTION public.rt_wipe_all_data() TO PUBLIC;


-- RPC: rt_get_all_callers
-- Admin/debug: returns all caller rows with summary fields (no phone_hash exposure).
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
GRANT EXECUTE ON FUNCTION public.rt_get_all_callers() TO PUBLIC;


-- Also update rt_remove_schema_entry to clear loved_ones on full wipe
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
GRANT EXECUTE ON FUNCTION public.rt_remove_schema_entry(TEXT,TEXT) TO PUBLIC;
