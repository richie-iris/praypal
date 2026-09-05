-- ============================================================
-- Sprint 0 parity — 2026-08-06
-- Captures schema that was live in prod but never in version
-- control (the COGS columns + the current rt_save_call_metrics /
-- rt_get_all_callers bodies were applied out-of-band). Discovered
-- when the dev-DB suite run failed P40/P41. From here, the sql/
-- files ARE the schema; the migration ledger lands in Sprint 4.
-- ============================================================

ALTER TABLE rt.callers ADD COLUMN IF NOT EXISTS last_transcript      TEXT;
ALTER TABLE rt.callers ADD COLUMN IF NOT EXISTS last_input_tokens    INT DEFAULT 0;
ALTER TABLE rt.callers ADD COLUMN IF NOT EXISTS last_output_tokens   INT DEFAULT 0;
ALTER TABLE rt.callers ADD COLUMN IF NOT EXISTS total_input_tokens   INT DEFAULT 0;
ALTER TABLE rt.callers ADD COLUMN IF NOT EXISTS total_output_tokens  INT DEFAULT 0;

-- Verbatim from prod (pg_get_functiondef, 2026-08-06):

CREATE OR REPLACE FUNCTION public.rt_save_call_metrics(p_hash text, p_transcript text DEFAULT NULL::text, p_in_tokens integer DEFAULT 0, p_out_tokens integer DEFAULT 0)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER AS $function$
BEGIN
    INSERT INTO rt.callers (phone_hash, last_transcript, last_input_tokens, last_output_tokens, total_input_tokens, total_output_tokens, last_call_at)
    VALUES (p_hash, p_transcript, COALESCE(p_in_tokens, 0), COALESCE(p_out_tokens, 0), COALESCE(p_in_tokens, 0), COALESCE(p_out_tokens, 0), NOW())
    ON CONFLICT (phone_hash) DO UPDATE SET
        last_transcript = COALESCE(p_transcript, rt.callers.last_transcript),
        last_input_tokens = COALESCE(p_in_tokens, 0),
        last_output_tokens = COALESCE(p_out_tokens, 0),
        total_input_tokens = COALESCE(rt.callers.total_input_tokens, 0) + COALESCE(p_in_tokens, 0),
        total_output_tokens = COALESCE(rt.callers.total_output_tokens, 0) + COALESCE(p_out_tokens, 0),
        last_call_at = NOW();
END; $function$;

CREATE OR REPLACE FUNCTION public.rt_get_all_callers()
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER AS $function$
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
            'next_call_context', next_call_context,
            'total_in_tokens', total_input_tokens,
            'total_out_tokens', total_output_tokens,
            'last_transcript_chars', length(COALESCE(last_transcript, ''))
        ) ORDER BY call_count DESC), '[]')
        FROM rt.callers
    );
END; $function$;

-- CREATE OR REPLACE re-grants default EXECUTE to PUBLIC; re-defuse.
REVOKE EXECUTE ON FUNCTION public.rt_get_all_callers() FROM PUBLIC, anon, authenticated;
GRANT  EXECUTE ON FUNCTION public.rt_get_all_callers() TO service_role;
