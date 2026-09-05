-- 17-forget-me-covers-jobs.sql
--
-- "Forget me" left the one copy of the caller's real phone number behind, and
-- left a robocall armed to ring it.
--
-- rt_forget_caller clears eight tables. rt.scheduled_jobs was never one of them,
-- because the table did not exist when the function was written (sql/10 was
-- never applied; sql/15 finally created it). Its payload carries
-- payload->>'caller_e164' in PLAINTEXT — everywhere else in this schema the
-- number exists only as a SHA-256 hash, so after an erasure that row is the only
-- place the person is still identifiable.
--
-- Worse than the residue: rt_get_pending_jobs selects on status/run_at/attempts
-- and never checks the caller still exists. An outbound_call scheduled before an
-- erasure would still be claimed, still be dialled, and still ring a person who
-- had asked to be forgotten. rt_upsert_fact has had a resurrection guard since
-- sql/12; the job path had none.
--
-- Three changes, all idempotent:
--   1. rt_forget_caller deletes the caller's scheduled jobs and reports the count
--   2. rt_get_pending_jobs refuses to claim work for a caller who is gone
--   3. rt_cancel_jobs_for(hash) — the bulk cancel-by-hash that did not exist, and
--      whose absence forced the test harness to fake one job type at a time

-- ── 1. erasure reaches the job queue ─────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.rt_forget_caller(p_hash TEXT)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE n_s INT; n_r INT; n_c INT; n_e INT; n_a INT; n_cal INT; n_f INT; n_j INT; n_sj INT;
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
    -- The plaintext number lives here and nowhere else. It goes first: a job
    -- claimed between two statements of this function must not outlive the row
    -- it belongs to.
    BEGIN
        DELETE FROM rt.scheduled_jobs WHERE phone_hash = p_hash;
        GET DIAGNOSTICS n_sj = ROW_COUNT;
    EXCEPTION WHEN undefined_table THEN n_sj := 0;
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
                              'facts', n_f, 'postcall_jobs', n_j,
                              'scheduled_jobs', n_sj);
END; $$;

-- ── 2. a job for a caller who no longer exists is never claimed ──────────────
CREATE OR REPLACE FUNCTION public.rt_get_pending_jobs()
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE v JSONB;
BEGIN
    WITH claimed AS (
        UPDATE rt.scheduled_jobs j
           SET status = 'running', attempts = j.attempts + 1, updated_at = NOW()
         WHERE j.status = 'pending'
           AND j.run_at <= NOW()
           AND j.attempts < 3
           -- The resurrection guard rt_upsert_fact has had since sql/12, applied
           -- to the one path that can place a real phone call. Without it an
           -- outbound_call scheduled before "forget me" still rang.
           AND EXISTS (SELECT 1 FROM rt.callers c WHERE c.phone_hash = j.phone_hash)
        RETURNING id, phone_hash, job_type, payload, run_at, attempts
    )
    SELECT COALESCE(jsonb_agg(row_to_json(claimed)::jsonb), '[]'::jsonb) INTO v FROM claimed;
    RETURN v;
END; $$;

-- ── 3. cancel every pending job for one caller, in one statement ─────────────
CREATE OR REPLACE FUNCTION public.rt_cancel_jobs_for(p_hash TEXT)
RETURNS INT LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE n INT;
BEGIN
    UPDATE rt.scheduled_jobs
       SET status = 'cancelled', updated_at = NOW()
     WHERE phone_hash = p_hash AND status IN ('pending', 'running');
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END; $$;

REVOKE ALL ON FUNCTION public.rt_forget_caller(TEXT)     FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.rt_get_pending_jobs()      FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.rt_cancel_jobs_for(TEXT)   FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.rt_forget_caller(TEXT)   TO service_role;
GRANT EXECUTE ON FUNCTION public.rt_get_pending_jobs()    TO service_role;
GRANT EXECUTE ON FUNCTION public.rt_cancel_jobs_for(TEXT) TO service_role;
