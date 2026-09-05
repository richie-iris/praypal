-- ============================================================
-- Forget me — 2026-08-07
-- A tester who asks to be forgotten must actually vanish: profile, memory,
-- reminders, audit trail, AND the per-call traces (transcript + system prompt
-- live there too). The old rt_remove_schema_entry "everything" left the caller
-- row, the metrics/transcript, and every rt.calls/rt.call_events row behind.
-- Apply: python sql_push.py --file sql/08-forget-me.sql
-- ============================================================

CREATE OR REPLACE FUNCTION public.rt_forget_caller(p_hash TEXT)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE n_s INT; n_r INT; n_c INT; n_e INT; n_a INT; n_cal INT;
BEGIN
    DELETE FROM rt.account_schema_registry WHERE phone_hash = p_hash;
    GET DIAGNOSTICS n_s = ROW_COUNT;
    DELETE FROM rt.reminders WHERE phone_hash = p_hash;
    GET DIAGNOSTICS n_r = ROW_COUNT;
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
                              'calls', n_cal, 'call_events', n_e, 'audit', n_a);
END; $$;

REVOKE ALL ON FUNCTION public.rt_forget_caller(TEXT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.rt_forget_caller(TEXT) TO service_role;
