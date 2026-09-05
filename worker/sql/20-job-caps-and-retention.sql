-- 20-job-caps-and-retention.sql
--
-- Two RPCs the worker needs and the schema did not have:
--
--   rt_count_jobs_today(hash)   — how many jobs this caller has scheduled since
--       UTC midnight, total and research-only. The scheduler enforces a per-day
--       cap with it; without a server-side count the cap was whatever the
--       in-memory session remembered, i.e. nothing across reconnects.
--       Cancelled and failed rows count too: the cap is on scheduling, and a
--       caller must not be able to reset it by cancelling.
--
--   rt_purge_old_transcripts(days) — transcript retention. Transcripts live on
--       rt.calls (sql/07), keyed by started_at; the rest of the row (tokens,
--       bridge, postcall, meta) is kept for cost and audit reporting, only the
--       verbatim conversation is nulled.
--
-- Both SECURITY DEFINER with search_path pinned inline (sql/18).

-- ── 1. daily job count for the cap ───────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.rt_count_jobs_today(p_hash TEXT)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, rt, pg_temp AS $$
DECLARE v JSONB;
BEGIN
    -- date_trunc on "now() at time zone 'utc'" is UTC midnight as a naive
    -- timestamp; the trailing AT TIME ZONE turns it back into timestamptz so
    -- the comparison does not depend on the session's TimeZone setting.
    SELECT jsonb_build_object(
               'total',    COUNT(*),
               'research', COUNT(*) FILTER (WHERE job_type = 'research'))
      INTO v
      FROM rt.scheduled_jobs
     WHERE phone_hash = p_hash
       AND created_at >= date_trunc('day', now() at time zone 'utc') at time zone 'utc';
    RETURN v;
END; $$;

-- ── 2. transcript retention ──────────────────────────────────────────────────
-- Returns the number of calls whose transcript was nulled.
CREATE OR REPLACE FUNCTION public.rt_purge_old_transcripts(p_days INT DEFAULT 30)
RETURNS INT LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, rt, pg_temp AS $$
DECLARE n INT;
BEGIN
    -- A NULL or non-positive window would null every transcript, including
    -- the call in progress. Refuse rather than guess.
    IF p_days IS NULL OR p_days < 1 THEN
        RAISE EXCEPTION 'rt_purge_old_transcripts: p_days must be >= 1, got %', p_days;
    END IF;
    UPDATE rt.calls
       SET transcript = NULL
     WHERE started_at < now() - p_days * interval '1 day'
       AND transcript IS NOT NULL;
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END; $$;

REVOKE ALL ON FUNCTION public.rt_count_jobs_today(TEXT)     FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.rt_purge_old_transcripts(INT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.rt_count_jobs_today(TEXT)     TO service_role;
GRANT EXECUTE ON FUNCTION public.rt_purge_old_transcripts(INT) TO service_role;
