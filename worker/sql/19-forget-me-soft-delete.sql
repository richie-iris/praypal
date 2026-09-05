-- 19-forget-me-soft-delete.sql
--
-- "Forget me" was a hard delete with no undo. One misheard "yes" on a phone
-- line — or a scam caller saying it about someone else's number — and years
-- of reminders, facts and call history were gone before the call ended.
--
-- This turns it into a 24-hour soft delete without weakening the erasure:
--   * rt.forgotten_archive holds a JSON copy of every row rt_forget_caller
--     removes, tagged with the table it came from and when.
--   * rt_forget_caller archives each row BEFORE it deletes it, one explicit
--     INSERT ... SELECT per table — inline, in the same order as the deletes,
--     so a reader can see the archive is complete without trusting a loop.
--   * rt_restore_caller(hash) writes rows archived in the last 24h back into
--     their tables and removes them from the archive.
--   * rt_purge_forgotten() drops archive rows older than 24h; the scheduler
--     calls it so the archive never becomes a second, forgotten copy.
--
-- Outside the archive, the live tables look exactly as they did after the
-- old hard delete — the resurrection guards in rt_upsert_fact and
-- rt_get_pending_jobs still see no caller. The archive itself is reachable
-- only through service_role RPCs, like everything else in rt.

-- ── 1. the archive ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS rt.forgotten_archive (
    id              BIGSERIAL   PRIMARY KEY,
    phone_hash      TEXT        NOT NULL,
    table_name      TEXT        NOT NULL,
    "row"           JSONB       NOT NULL,
    forgotten_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_forgotten_hash ON rt.forgotten_archive (phone_hash, forgotten_at);
CREATE INDEX IF NOT EXISTS idx_forgotten_at   ON rt.forgotten_archive (forgotten_at);
ALTER TABLE rt.forgotten_archive DISABLE ROW LEVEL SECURITY;

-- ── 2. archive-before-delete ─────────────────────────────────────────────────
-- Same nine tables as sql/17, same order, same per-table counts in the result.
CREATE OR REPLACE FUNCTION public.rt_forget_caller(p_hash TEXT)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, rt, pg_temp AS $$
DECLARE n_s INT; n_r INT; n_c INT; n_e INT; n_a INT; n_cal INT; n_f INT; n_j INT; n_sj INT;
BEGIN
    INSERT INTO rt.forgotten_archive (phone_hash, table_name, "row")
        SELECT p_hash, 'account_schema_registry', row_to_json(t)::jsonb
          FROM rt.account_schema_registry t WHERE t.phone_hash = p_hash;
    DELETE FROM rt.account_schema_registry WHERE phone_hash = p_hash;
    GET DIAGNOSTICS n_s = ROW_COUNT;

    INSERT INTO rt.forgotten_archive (phone_hash, table_name, "row")
        SELECT p_hash, 'reminders', row_to_json(t)::jsonb
          FROM rt.reminders t WHERE t.phone_hash = p_hash;
    DELETE FROM rt.reminders WHERE phone_hash = p_hash;
    GET DIAGNOSTICS n_r = ROW_COUNT;

    INSERT INTO rt.forgotten_archive (phone_hash, table_name, "row")
        SELECT p_hash, 'facts', row_to_json(t)::jsonb
          FROM rt.facts t WHERE t.phone_hash = p_hash;
    DELETE FROM rt.facts WHERE phone_hash = p_hash;
    GET DIAGNOSTICS n_f = ROW_COUNT;

    BEGIN
        INSERT INTO rt.forgotten_archive (phone_hash, table_name, "row")
            SELECT p_hash, 'postcall_jobs', row_to_json(t)::jsonb
              FROM rt.postcall_jobs t WHERE t.phone_hash = p_hash;
        DELETE FROM rt.postcall_jobs WHERE phone_hash = p_hash;
        GET DIAGNOSTICS n_j = ROW_COUNT;
    EXCEPTION WHEN undefined_table THEN n_j := 0;
    END;

    -- The plaintext number lives in the job payload and nowhere else. It is
    -- archived too — a restore must be able to re-arm the call — but the
    -- archive is service_role-only and purged after 24h.
    BEGIN
        INSERT INTO rt.forgotten_archive (phone_hash, table_name, "row")
            SELECT p_hash, 'scheduled_jobs', row_to_json(t)::jsonb
              FROM rt.scheduled_jobs t WHERE t.phone_hash = p_hash;
        DELETE FROM rt.scheduled_jobs WHERE phone_hash = p_hash;
        GET DIAGNOSTICS n_sj = ROW_COUNT;
    EXCEPTION WHEN undefined_table THEN n_sj := 0;
    END;

    -- traces first (they reference the call), then the caller row
    INSERT INTO rt.forgotten_archive (phone_hash, table_name, "row")
        SELECT p_hash, 'call_events', row_to_json(t)::jsonb
          FROM rt.call_events t
         WHERE t.call_id IN (SELECT call_id FROM rt.calls WHERE phone_hash = p_hash);
    DELETE FROM rt.call_events WHERE call_id IN
        (SELECT call_id FROM rt.calls WHERE phone_hash = p_hash);
    GET DIAGNOSTICS n_e = ROW_COUNT;

    INSERT INTO rt.forgotten_archive (phone_hash, table_name, "row")
        SELECT p_hash, 'calls', row_to_json(t)::jsonb
          FROM rt.calls t WHERE t.phone_hash = p_hash;
    DELETE FROM rt.calls WHERE phone_hash = p_hash;
    GET DIAGNOSTICS n_cal = ROW_COUNT;

    BEGIN
        INSERT INTO rt.forgotten_archive (phone_hash, table_name, "row")
            SELECT p_hash, 'audit_log', row_to_json(t)::jsonb
              FROM rt.audit_log t WHERE t.phone_hash = p_hash;
        DELETE FROM rt.audit_log WHERE phone_hash = p_hash;
        GET DIAGNOSTICS n_a = ROW_COUNT;
    EXCEPTION WHEN undefined_table THEN n_a := 0;
    END;

    INSERT INTO rt.forgotten_archive (phone_hash, table_name, "row")
        SELECT p_hash, 'callers', row_to_json(t)::jsonb
          FROM rt.callers t WHERE t.phone_hash = p_hash;
    DELETE FROM rt.callers WHERE phone_hash = p_hash;
    GET DIAGNOSTICS n_c = ROW_COUNT;

    RETURN jsonb_build_object('schemas', n_s, 'reminders', n_r, 'caller', n_c,
                              'calls', n_cal, 'call_events', n_e, 'audit', n_a,
                              'facts', n_f, 'postcall_jobs', n_j,
                              'scheduled_jobs', n_sj);
END; $$;

-- ── 3. undo, within 24h ──────────────────────────────────────────────────────
-- Callers first so the row every other table hangs off exists again before
-- its dependants. ON CONFLICT DO NOTHING: if the person rang back inside the
-- window, rt_get_caller has already created a fresh callers row and that one
-- wins — the restore must never overwrite live state with a stale copy.
-- Returns the number of rows written back.
CREATE OR REPLACE FUNCTION public.rt_restore_caller(p_hash TEXT)
RETURNS INT LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, rt, pg_temp AS $$
DECLARE n INT := 0; k INT;
BEGIN
    INSERT INTO rt.callers SELECT r.*
      FROM rt.forgotten_archive a, jsonb_populate_record(NULL::rt.callers, a."row") r
     WHERE a.phone_hash = p_hash AND a.table_name = 'callers'
       AND a.forgotten_at >= now() - interval '24 hours'
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS k = ROW_COUNT; n := n + k;

    INSERT INTO rt.account_schema_registry SELECT r.*
      FROM rt.forgotten_archive a, jsonb_populate_record(NULL::rt.account_schema_registry, a."row") r
     WHERE a.phone_hash = p_hash AND a.table_name = 'account_schema_registry'
       AND a.forgotten_at >= now() - interval '24 hours'
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS k = ROW_COUNT; n := n + k;

    INSERT INTO rt.reminders SELECT r.*
      FROM rt.forgotten_archive a, jsonb_populate_record(NULL::rt.reminders, a."row") r
     WHERE a.phone_hash = p_hash AND a.table_name = 'reminders'
       AND a.forgotten_at >= now() - interval '24 hours'
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS k = ROW_COUNT; n := n + k;

    INSERT INTO rt.facts SELECT r.*
      FROM rt.forgotten_archive a, jsonb_populate_record(NULL::rt.facts, a."row") r
     WHERE a.phone_hash = p_hash AND a.table_name = 'facts'
       AND a.forgotten_at >= now() - interval '24 hours'
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS k = ROW_COUNT; n := n + k;

    BEGIN
        INSERT INTO rt.postcall_jobs SELECT r.*
          FROM rt.forgotten_archive a, jsonb_populate_record(NULL::rt.postcall_jobs, a."row") r
         WHERE a.phone_hash = p_hash AND a.table_name = 'postcall_jobs'
           AND a.forgotten_at >= now() - interval '24 hours'
        ON CONFLICT DO NOTHING;
        GET DIAGNOSTICS k = ROW_COUNT; n := n + k;
    EXCEPTION WHEN undefined_table THEN NULL;
    END;

    BEGIN
        INSERT INTO rt.scheduled_jobs SELECT r.*
          FROM rt.forgotten_archive a, jsonb_populate_record(NULL::rt.scheduled_jobs, a."row") r
         WHERE a.phone_hash = p_hash AND a.table_name = 'scheduled_jobs'
           AND a.forgotten_at >= now() - interval '24 hours'
        ON CONFLICT DO NOTHING;
        GET DIAGNOSTICS k = ROW_COUNT; n := n + k;
    EXCEPTION WHEN undefined_table THEN NULL;
    END;

    INSERT INTO rt.calls SELECT r.*
      FROM rt.forgotten_archive a, jsonb_populate_record(NULL::rt.calls, a."row") r
     WHERE a.phone_hash = p_hash AND a.table_name = 'calls'
       AND a.forgotten_at >= now() - interval '24 hours'
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS k = ROW_COUNT; n := n + k;

    INSERT INTO rt.call_events SELECT r.*
      FROM rt.forgotten_archive a, jsonb_populate_record(NULL::rt.call_events, a."row") r
     WHERE a.phone_hash = p_hash AND a.table_name = 'call_events'
       AND a.forgotten_at >= now() - interval '24 hours'
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS k = ROW_COUNT; n := n + k;

    BEGIN
        INSERT INTO rt.audit_log SELECT r.*
          FROM rt.forgotten_archive a, jsonb_populate_record(NULL::rt.audit_log, a."row") r
         WHERE a.phone_hash = p_hash AND a.table_name = 'audit_log'
           AND a.forgotten_at >= now() - interval '24 hours'
        ON CONFLICT DO NOTHING;
        GET DIAGNOSTICS k = ROW_COUNT; n := n + k;
    EXCEPTION WHEN undefined_table THEN NULL;
    END;

    -- Restored (or superseded) rows leave the archive; anything older than
    -- the window stays for rt_purge_forgotten.
    DELETE FROM rt.forgotten_archive
     WHERE phone_hash = p_hash
       AND forgotten_at >= now() - interval '24 hours';
    RETURN n;
END; $$;

-- ── 4. the archive is not a second copy ──────────────────────────────────────
-- No parameters: the scheduler calls it with {}. Returns rows purged.
CREATE OR REPLACE FUNCTION public.rt_purge_forgotten()
RETURNS INT LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, rt, pg_temp AS $$
DECLARE n INT;
BEGIN
    DELETE FROM rt.forgotten_archive
     WHERE forgotten_at < now() - interval '24 hours';
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END; $$;

REVOKE ALL ON FUNCTION public.rt_forget_caller(TEXT)   FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.rt_restore_caller(TEXT)  FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.rt_purge_forgotten()     FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.rt_forget_caller(TEXT)  TO service_role;
GRANT EXECUTE ON FUNCTION public.rt_restore_caller(TEXT) TO service_role;
GRANT EXECUTE ON FUNCTION public.rt_purge_forgotten()    TO service_role;
