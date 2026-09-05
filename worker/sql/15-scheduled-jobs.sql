-- ============================================================
-- Scheduled jobs — actually applied (2026-08-15)
--
-- rt_scheduler.py has been complete, correct-looking code since it was
-- written, but rt.scheduled_jobs and its three RPCs were NEVER migrated to
-- this database — confirmed empirically tonight (information_schema shows
-- zero rows for the table, zero rows for all three functions). Every
-- scheduled job — outbound calls, future SMS/email, delayed research — has
-- been silently impossible since day one. This is the real fix, not just the
-- runner deployment.
--
-- Extracted from sql/10-scheduled-jobs.sql rather than re-running it whole:
-- that file ALSO redefines rt_get_caller and rt_get_caller_full_bundle with
-- OLDER bodies that predate the fact-store bundle (sql/13). Re-running it
-- verbatim would have silently regressed tonight's streamline work. Only the
-- genuinely-missing pieces are here: the table, the three job RPCs, and
-- rt_set_caller_email (also confirmed missing — save_email has been
-- silently broken the same way).
--
-- Also fixes a double-execution risk in rt_get_pending_jobs: the original
-- returned every job 'running' in the last 5 minutes, not just the ones THIS
-- call just claimed — a runner cycle slower than the poll interval could
-- re-fetch and re-execute a job still mid-flight from the previous cycle.
-- For an outbound_call job that means dialing someone twice. Fixed to return
-- only the exact rows this invocation transitioned to 'running', via an
-- UPDATE ... RETURNING CTE.
--
-- Apply: python sql_push.py --file sql/15-scheduled-jobs.sql
-- ============================================================

CREATE TABLE IF NOT EXISTS rt.scheduled_jobs (
    id              BIGSERIAL       PRIMARY KEY,
    phone_hash      TEXT            NOT NULL,
    job_type        TEXT            NOT NULL,   -- outbound_call | research | send_sms | send_email | reminder
    payload         JSONB           NOT NULL DEFAULT '{}',
    run_at          TIMESTAMPTZ     NOT NULL,
    status          TEXT            DEFAULT 'pending',   -- pending | running | done | failed
    attempts        INT             DEFAULT 0,
    result          JSONB,
    created_at      TIMESTAMPTZ     DEFAULT NOW(),
    updated_at      TIMESTAMPTZ     DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sj_pending ON rt.scheduled_jobs (status, run_at) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_sj_hash ON rt.scheduled_jobs (phone_hash);
ALTER TABLE rt.scheduled_jobs DISABLE ROW LEVEL SECURITY;

CREATE OR REPLACE FUNCTION public.rt_schedule_job(
    p_hash TEXT, p_type TEXT, p_payload TEXT, p_run_at TIMESTAMPTZ
)
RETURNS BIGINT LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE v_id BIGINT;
BEGIN
    INSERT INTO rt.scheduled_jobs (phone_hash, job_type, payload, run_at)
    VALUES (p_hash, p_type, p_payload::jsonb, p_run_at)
    RETURNING id INTO v_id;
    RETURN v_id;
END; $$;

-- Claims and returns ONLY the rows this call transitions to 'running' — the
-- fix for the double-execution risk described above.
CREATE OR REPLACE FUNCTION public.rt_get_pending_jobs()
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE v_result JSONB;
BEGIN
    WITH claimed AS (
        UPDATE rt.scheduled_jobs
           SET status = 'running', attempts = attempts + 1, updated_at = NOW()
         WHERE status = 'pending' AND run_at <= NOW() AND attempts < 3
        RETURNING id, phone_hash, job_type, payload, run_at, attempts
    )
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'id', id, 'phone_hash', phone_hash, 'job_type', job_type,
        'payload', payload, 'run_at', run_at, 'attempts', attempts
    )), '[]') INTO v_result FROM claimed;
    RETURN v_result;
END; $$;

CREATE OR REPLACE FUNCTION public.rt_update_job_status(
    p_id BIGINT, p_status TEXT, p_result TEXT DEFAULT NULL
)
RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    UPDATE rt.scheduled_jobs
       SET status = p_status,
           result = CASE WHEN p_result IS NOT NULL THEN p_result::jsonb ELSE result END,
           updated_at = NOW()
     WHERE id = p_id;
END; $$;

ALTER TABLE rt.callers ADD COLUMN IF NOT EXISTS email TEXT;

CREATE OR REPLACE FUNCTION public.rt_set_caller_email(p_hash TEXT, p_email TEXT)
RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    INSERT INTO rt.callers (phone_hash, email, last_call_at)
    VALUES (p_hash, p_email, NOW())
    ON CONFLICT (phone_hash) DO UPDATE SET email = p_email;
END; $$;

-- Deliberately PUBLIC-free: worker-only, matching every other job/trace RPC
-- tonight (migration 10 had granted these TO PUBLIC — corrected here).
REVOKE ALL ON FUNCTION public.rt_schedule_job(TEXT,TEXT,TEXT,TIMESTAMPTZ) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.rt_get_pending_jobs()                     FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.rt_update_job_status(BIGINT,TEXT,TEXT)    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.rt_set_caller_email(TEXT,TEXT)            FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.rt_schedule_job(TEXT,TEXT,TEXT,TIMESTAMPTZ) TO service_role;
GRANT EXECUTE ON FUNCTION public.rt_get_pending_jobs()                     TO service_role;
GRANT EXECUTE ON FUNCTION public.rt_update_job_status(BIGINT,TEXT,TEXT)    TO service_role;
GRANT EXECUTE ON FUNCTION public.rt_set_caller_email(TEXT,TEXT)            TO service_role;
