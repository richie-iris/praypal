-- 21-forget-atomic-restore-newest-retention-all.sql
--
-- Three fixes to what sql/19 and sql/20 shipped:
--
--   rt_forget_caller   — 19 archived each table with an INSERT ... SELECT and
--       then DELETEd in a separate statement. Inside one function both run in
--       the same transaction, but a row written between the two (a turn logged
--       mid-forget, a reminder saved by a concurrent request) was deleted and
--       never archived. Each table is now ONE statement: the DELETE's RETURNING
--       feeds the archive INSERT, so exactly the rows that vanished are the
--       rows that were kept.
--
--   rt_restore_caller  — 19 restored every archived row of the last 24h. A
--       caller forgotten twice in a day (forget, ring back, forget again) had
--       two copies of every row in the archive and ON CONFLICT DO NOTHING kept
--       whichever the scan met first. Only the most recent forget batch is
--       restored now, newest archive copy first, and only that batch leaves
--       the archive.
--
--   rt_purge_old_transcripts — 20 nulled rt.calls.transcript and nothing else,
--       so the verbatim conversation lived on as kind='turn' rows in
--       rt.call_events (sql/11) and as rt.callers.last_transcript (sql/04).
--       All three go now; the RPC returns per-target counts.
--
-- All SECURITY DEFINER with search_path pinned inline (sql/18).

-- ── 1. archive and delete in one statement per table ─────────────────────────
-- Same nine tables as sql/17 and sql/19, same order, same per-table counts.
-- ROW_COUNT after a data-modifying CTE is the number of rows the INSERT wrote,
-- which is exactly the number the DELETE returned.
CREATE OR REPLACE FUNCTION public.rt_forget_caller(p_hash TEXT)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, rt, pg_temp AS $$
DECLARE n_s INT; n_r INT; n_c INT; n_e INT; n_a INT; n_cal INT; n_f INT; n_j INT; n_sj INT;
BEGIN
    WITH gone AS (DELETE FROM rt.account_schema_registry WHERE phone_hash = p_hash RETURNING *)
    INSERT INTO rt.forgotten_archive (phone_hash, table_name, "row")
        SELECT p_hash, 'account_schema_registry', row_to_json(gone)::jsonb FROM gone;
    GET DIAGNOSTICS n_s = ROW_COUNT;

    WITH gone AS (DELETE FROM rt.reminders WHERE phone_hash = p_hash RETURNING *)
    INSERT INTO rt.forgotten_archive (phone_hash, table_name, "row")
        SELECT p_hash, 'reminders', row_to_json(gone)::jsonb FROM gone;
    GET DIAGNOSTICS n_r = ROW_COUNT;

    WITH gone AS (DELETE FROM rt.facts WHERE phone_hash = p_hash RETURNING *)
    INSERT INTO rt.forgotten_archive (phone_hash, table_name, "row")
        SELECT p_hash, 'facts', row_to_json(gone)::jsonb FROM gone;
    GET DIAGNOSTICS n_f = ROW_COUNT;

    BEGIN
        WITH gone AS (DELETE FROM rt.postcall_jobs WHERE phone_hash = p_hash RETURNING *)
        INSERT INTO rt.forgotten_archive (phone_hash, table_name, "row")
            SELECT p_hash, 'postcall_jobs', row_to_json(gone)::jsonb FROM gone;
        GET DIAGNOSTICS n_j = ROW_COUNT;
    EXCEPTION WHEN undefined_table THEN n_j := 0;
    END;

    -- The plaintext number lives in the job payload and nowhere else. It is
    -- archived too — a restore must be able to re-arm the call — but the
    -- archive is service_role-only and purged after 24h.
    BEGIN
        WITH gone AS (DELETE FROM rt.scheduled_jobs WHERE phone_hash = p_hash RETURNING *)
        INSERT INTO rt.forgotten_archive (phone_hash, table_name, "row")
            SELECT p_hash, 'scheduled_jobs', row_to_json(gone)::jsonb FROM gone;
        GET DIAGNOSTICS n_sj = ROW_COUNT;
    EXCEPTION WHEN undefined_table THEN n_sj := 0;
    END;

    -- traces first (they reference the call), then the caller row
    WITH gone AS (DELETE FROM rt.call_events
                   WHERE call_id IN (SELECT call_id FROM rt.calls WHERE phone_hash = p_hash)
                   RETURNING *)
    INSERT INTO rt.forgotten_archive (phone_hash, table_name, "row")
        SELECT p_hash, 'call_events', row_to_json(gone)::jsonb FROM gone;
    GET DIAGNOSTICS n_e = ROW_COUNT;

    WITH gone AS (DELETE FROM rt.calls WHERE phone_hash = p_hash RETURNING *)
    INSERT INTO rt.forgotten_archive (phone_hash, table_name, "row")
        SELECT p_hash, 'calls', row_to_json(gone)::jsonb FROM gone;
    GET DIAGNOSTICS n_cal = ROW_COUNT;

    BEGIN
        WITH gone AS (DELETE FROM rt.audit_log WHERE phone_hash = p_hash RETURNING *)
        INSERT INTO rt.forgotten_archive (phone_hash, table_name, "row")
            SELECT p_hash, 'audit_log', row_to_json(gone)::jsonb FROM gone;
        GET DIAGNOSTICS n_a = ROW_COUNT;
    EXCEPTION WHEN undefined_table THEN n_a := 0;
    END;

    WITH gone AS (DELETE FROM rt.callers WHERE phone_hash = p_hash RETURNING *)
    INSERT INTO rt.forgotten_archive (phone_hash, table_name, "row")
        SELECT p_hash, 'callers', row_to_json(gone)::jsonb FROM gone;
    GET DIAGNOSTICS n_c = ROW_COUNT;

    RETURN jsonb_build_object('schemas', n_s, 'reminders', n_r, 'caller', n_c,
                              'calls', n_cal, 'call_events', n_e, 'audit', n_a,
                              'facts', n_f, 'postcall_jobs', n_j,
                              'scheduled_jobs', n_sj);
END; $$;

-- ── 2. undo the most recent forget only ──────────────────────────────────────
-- One forget is one transaction, so every row it archives shares one
-- forgotten_at (now() is fixed for the transaction): that timestamp IS the
-- batch id. Only the newest batch inside the 24h window comes back; older
-- batches stay for rt_purge_forgotten. Within the batch the newest archive
-- copy (highest id) is inserted first so ON CONFLICT DO NOTHING keeps it.
-- A live row — the person rang back and rt_get_caller re-created them —
-- still wins over any archived copy; the restore never overwrites live state.
-- Callers first so the row every other table hangs off exists before its
-- dependants. Returns the number of rows written back.
CREATE OR REPLACE FUNCTION public.rt_restore_caller(p_hash TEXT)
RETURNS INT LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, rt, pg_temp AS $$
DECLARE n INT := 0; k INT; v_batch TIMESTAMPTZ;
BEGIN
    SELECT max(forgotten_at) INTO v_batch
      FROM rt.forgotten_archive
     WHERE phone_hash = p_hash
       AND forgotten_at >= now() - interval '24 hours';
    IF v_batch IS NULL THEN
        RETURN 0;
    END IF;

    INSERT INTO rt.callers SELECT r.*
      FROM rt.forgotten_archive a, jsonb_populate_record(NULL::rt.callers, a."row") r
     WHERE a.phone_hash = p_hash AND a.table_name = 'callers' AND a.forgotten_at = v_batch
     ORDER BY a.id DESC
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS k = ROW_COUNT; n := n + k;

    INSERT INTO rt.account_schema_registry SELECT r.*
      FROM rt.forgotten_archive a, jsonb_populate_record(NULL::rt.account_schema_registry, a."row") r
     WHERE a.phone_hash = p_hash AND a.table_name = 'account_schema_registry' AND a.forgotten_at = v_batch
     ORDER BY a.id DESC
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS k = ROW_COUNT; n := n + k;

    INSERT INTO rt.reminders SELECT r.*
      FROM rt.forgotten_archive a, jsonb_populate_record(NULL::rt.reminders, a."row") r
     WHERE a.phone_hash = p_hash AND a.table_name = 'reminders' AND a.forgotten_at = v_batch
     ORDER BY a.id DESC
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS k = ROW_COUNT; n := n + k;

    INSERT INTO rt.facts SELECT r.*
      FROM rt.forgotten_archive a, jsonb_populate_record(NULL::rt.facts, a."row") r
     WHERE a.phone_hash = p_hash AND a.table_name = 'facts' AND a.forgotten_at = v_batch
     ORDER BY a.id DESC
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS k = ROW_COUNT; n := n + k;

    BEGIN
        INSERT INTO rt.postcall_jobs SELECT r.*
          FROM rt.forgotten_archive a, jsonb_populate_record(NULL::rt.postcall_jobs, a."row") r
         WHERE a.phone_hash = p_hash AND a.table_name = 'postcall_jobs' AND a.forgotten_at = v_batch
         ORDER BY a.id DESC
        ON CONFLICT DO NOTHING;
        GET DIAGNOSTICS k = ROW_COUNT; n := n + k;
    EXCEPTION WHEN undefined_table THEN NULL;
    END;

    BEGIN
        INSERT INTO rt.scheduled_jobs SELECT r.*
          FROM rt.forgotten_archive a, jsonb_populate_record(NULL::rt.scheduled_jobs, a."row") r
         WHERE a.phone_hash = p_hash AND a.table_name = 'scheduled_jobs' AND a.forgotten_at = v_batch
         ORDER BY a.id DESC
        ON CONFLICT DO NOTHING;
        GET DIAGNOSTICS k = ROW_COUNT; n := n + k;
    EXCEPTION WHEN undefined_table THEN NULL;
    END;

    INSERT INTO rt.calls SELECT r.*
      FROM rt.forgotten_archive a, jsonb_populate_record(NULL::rt.calls, a."row") r
     WHERE a.phone_hash = p_hash AND a.table_name = 'calls' AND a.forgotten_at = v_batch
     ORDER BY a.id DESC
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS k = ROW_COUNT; n := n + k;

    INSERT INTO rt.call_events SELECT r.*
      FROM rt.forgotten_archive a, jsonb_populate_record(NULL::rt.call_events, a."row") r
     WHERE a.phone_hash = p_hash AND a.table_name = 'call_events' AND a.forgotten_at = v_batch
     ORDER BY a.id DESC
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS k = ROW_COUNT; n := n + k;

    BEGIN
        INSERT INTO rt.audit_log SELECT r.*
          FROM rt.forgotten_archive a, jsonb_populate_record(NULL::rt.audit_log, a."row") r
         WHERE a.phone_hash = p_hash AND a.table_name = 'audit_log' AND a.forgotten_at = v_batch
         ORDER BY a.id DESC
        ON CONFLICT DO NOTHING;
        GET DIAGNOSTICS k = ROW_COUNT; n := n + k;
    EXCEPTION WHEN undefined_table THEN NULL;
    END;

    -- Only the restored batch leaves the archive; older batches inside the
    -- window are still undoable until rt_purge_forgotten takes them.
    DELETE FROM rt.forgotten_archive
     WHERE phone_hash = p_hash
       AND forgotten_at = v_batch;
    RETURN n;
END; $$;

-- ── 3. transcript retention covers every copy ────────────────────────────────
-- Returns {"calls": n, "turns": n, "callers": n}. The return type changes from
-- INT, which CREATE OR REPLACE cannot do, hence the DROP; the scheduler prints
-- whatever comes back, so the shape change needs no worker change.
DROP FUNCTION IF EXISTS public.rt_purge_old_transcripts(INT);
CREATE OR REPLACE FUNCTION public.rt_purge_old_transcripts(p_days INT DEFAULT 30)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, rt, pg_temp AS $$
DECLARE n_calls INT; n_turns INT; n_callers INT;
BEGIN
    -- A NULL or non-positive window would null every transcript, including
    -- the call in progress. Refuse rather than guess.
    IF p_days IS NULL OR p_days < 1 THEN
        RAISE EXCEPTION 'rt_purge_old_transcripts: p_days must be >= 1, got %', p_days;
    END IF;

    -- the durable turn stream (sql/11) is the transcript verbatim
    DELETE FROM rt.call_events
     WHERE kind = 'turn'
       AND call_id IN (SELECT call_id FROM rt.calls
                        WHERE started_at < now() - p_days * interval '1 day');
    GET DIAGNOSTICS n_turns = ROW_COUNT;

    UPDATE rt.calls
       SET transcript = NULL
     WHERE started_at < now() - p_days * interval '1 day'
       AND transcript IS NOT NULL;
    GET DIAGNOSTICS n_calls = ROW_COUNT;

    -- the pre-sql/07 copy on the caller row (sql/04)
    UPDATE rt.callers
       SET last_transcript = NULL
     WHERE last_call_at < now() - p_days * interval '1 day'
       AND last_transcript IS NOT NULL;
    GET DIAGNOSTICS n_callers = ROW_COUNT;

    RETURN jsonb_build_object('calls', n_calls, 'turns', n_turns, 'callers', n_callers);
END; $$;

REVOKE ALL ON FUNCTION public.rt_forget_caller(TEXT)        FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.rt_restore_caller(TEXT)       FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.rt_purge_old_transcripts(INT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.rt_forget_caller(TEXT)        TO service_role;
GRANT EXECUTE ON FUNCTION public.rt_restore_caller(TEXT)       TO service_role;
GRANT EXECUTE ON FUNCTION public.rt_purge_old_transcripts(INT) TO service_role;
