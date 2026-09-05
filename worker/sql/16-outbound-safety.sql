-- ============================================================
-- Outbound-call safety — dial-safety fixes from the reminder-call review
-- (2026-08-15, same night as sql/15)
--
-- sql/15 made the scheduled-jobs subsystem exist for the first time. A
-- 25-agent adversarial review of the feature it unlocked (a tool that places
-- a REAL, unattended, no-human-in-the-loop phone call) found the subsystem
-- had never been exercised and had three real gaps a first real dial would
-- have hit: no protection against scheduling the same callback twice (a
-- caller correcting the time, or two independent code paths reacting to one
-- promise, would have queued two real rings), no reclaim for a job stranded
-- 'running' by a crashed scheduler process, and a missing 'email' field in
-- the caller bundle that silently broke save_email's round trip even after
-- sql/15 added the column and the writer RPC.
--
-- Apply: python sql_push.py --file sql/16-outbound-safety.sql
-- ============================================================

-- ─── Supersede, not stack, for outbound calls ────────────────
-- Atomic: cancel any pending outbound_call row for this caller, then insert
-- the new one, in one statement — no window where two processes scheduling
-- around the same moment could both insert. Scoped to outbound_call only;
-- other job types (research, reminders, sms/email) are harmless to stack.
CREATE OR REPLACE FUNCTION public.rt_schedule_job_superseding(
    p_hash TEXT, p_type TEXT, p_payload TEXT, p_run_at TIMESTAMPTZ
)
RETURNS BIGINT LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE v_id BIGINT;
BEGIN
    UPDATE rt.scheduled_jobs
       SET status = 'cancelled', updated_at = NOW()
     WHERE phone_hash = p_hash AND job_type = p_type AND status = 'pending';

    INSERT INTO rt.scheduled_jobs (phone_hash, job_type, payload, run_at)
    VALUES (p_hash, p_type, p_payload::jsonb, p_run_at)
    RETURNING id INTO v_id;
    RETURN v_id;
END; $$;

-- ─── Reclaim jobs stranded 'running' by a crashed scheduler ──
-- Mirrors rt_recovery.py's rt_postcall_recover() pattern for the identical
-- failure mode: a process dies between claiming a row and finishing it.
-- attempts is already incremented at claim time, so a reclaimed job
-- naturally exhausts MAX_ATTEMPTS after a few stale cycles — no separate
-- backoff bookkeeping needed.
CREATE OR REPLACE FUNCTION public.rt_reclaim_stale_jobs(p_minutes INT DEFAULT 10)
RETURNS INT LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE v_n INT;
BEGIN
    UPDATE rt.scheduled_jobs
       SET status = 'pending', updated_at = NOW()
     WHERE status = 'running'
       AND updated_at < NOW() - make_interval(mins => GREATEST(p_minutes, 1));
    GET DIAGNOSTICS v_n = ROW_COUNT;
    RETURN v_n;
END; $$;

-- ─── rt_get_caller_full_bundle: add the missing email field ──
-- Byte-identical to the live sql/13 body otherwise — verified against a
-- fresh pg_get_functiondef read before writing this, specifically so the
-- fact-store bundle (sql/13) is not regressed. save_email/
-- rt_set_caller_email (sql/15) wrote rt.callers.email correctly; nothing
-- ever read it back until now.
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
        'created_at', created_at, 'email', email
    ) INTO v_caller FROM rt.callers WHERE phone_hash = p_hash;

    v_caller := COALESCE(v_caller, jsonb_build_object(
        'voice_pref', NULL, 'call_count', 0, 'last_call_at', NULL,
        'display_name', 'Friend', 'last_name', NULL, 'agent_alias', 'Iris',
        'caller_rules', NULL, 'persona_directives', NULL,
        'pronunciation_note', 'Spoken clearly as provided',
        'summary', NULL, 'loved_ones', NULL, 'active_loop', NULL,
        'fenced_topics', NULL, 'next_call_context', NULL, 'created_at', NULL,
        'email', NULL
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

REVOKE ALL ON FUNCTION public.rt_schedule_job_superseding(TEXT,TEXT,TEXT,TIMESTAMPTZ) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.rt_reclaim_stale_jobs(INT)                             FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.rt_get_caller_full_bundle(TEXT)                        FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.rt_schedule_job_superseding(TEXT,TEXT,TEXT,TIMESTAMPTZ) TO service_role;
GRANT EXECUTE ON FUNCTION public.rt_reclaim_stale_jobs(INT)                             TO service_role;
GRANT EXECUTE ON FUNCTION public.rt_get_caller_full_bundle(TEXT)                        TO service_role;
