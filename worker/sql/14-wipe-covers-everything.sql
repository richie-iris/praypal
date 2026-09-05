-- ============================================================
-- rt_wipe_all_data: dynamic, complete, incapable of going stale (2026-08-15)
--
-- The old function truncated a hand-maintained list of THREE tables and the
-- schema had grown to eleven. A "complete wipe" for a fresh-start test would
-- have left transcripts, facts, call history, audit rows, and queued jobs
-- behind — and since facts now ride the pickup bundle, the "brand new" caller
-- would have been greeted by their own supposedly-erased memory.
--
-- Fixed the anti-patchwork way: enumerate rt.* from the catalog at run time.
-- A table added next month is covered the day it exists. Returns per-table
-- pre-wipe row counts so the caller can see exactly what was destroyed.
--
-- DEV-LANE TOOL. The prod worker refuses to boot against this database, and
-- this function must never be created on a production project.
--
-- Apply: python sql_push.py --file sql/14-wipe-covers-everything.sql
-- ============================================================

CREATE OR REPLACE FUNCTION public.rt_wipe_all_data()
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    r RECORD;
    n BIGINT;
    counts JSONB := '{}'::jsonb;
BEGIN
    FOR r IN
        SELECT table_name FROM information_schema.tables
         WHERE table_schema = 'rt' AND table_type = 'BASE TABLE'
         ORDER BY table_name
    LOOP
        EXECUTE format('SELECT count(*) FROM rt.%I', r.table_name) INTO n;
        EXECUTE format('TRUNCATE rt.%I RESTART IDENTITY CASCADE', r.table_name);
        counts := counts || jsonb_build_object(r.table_name, n);
    END LOOP;
    RETURN counts;
END; $$;

REVOKE ALL ON FUNCTION public.rt_wipe_all_data() FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.rt_wipe_all_data() TO service_role;
