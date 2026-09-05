-- ============================================================
-- Durable turns + post-call job queue — Phase 0 of the memory plan
--
-- Before this, a call's transcript lived only in a Python list on the worker
-- (`state["transcript_lines"]`) and was first written to the database at
-- hangup. Extraction ran inline in the shutdown callback. So a container
-- rebuild, an OOM, a crash or a SIGKILL mid-call destroyed BOTH the transcript
-- and the extraction, and left no record to recover from. The deploy workflow
-- documents this happening to a real caller: "the caller was cut off
-- mid-sentence and that call's memory was lost."
--
-- A shutdown hook cannot fix it — SIGKILL is never delivered to anyone. So:
--   1. every turn is written as it is spoken (rt.call_events, kind='turn'),
--   2. every call gets a queue row at PICKUP, not at hangup,
--   3. the worker heals on the way up, re-queueing anything left unfinished.
--
-- Apply: python sql_push.py --file sql/11-durable-turns-and-postcall-queue.sql
-- ============================================================

CREATE TABLE IF NOT EXISTS rt.postcall_jobs (
    id           BIGSERIAL   PRIMARY KEY,
    call_id      TEXT        NOT NULL UNIQUE,   -- one job per call; makes enqueue idempotent
    phone_hash   TEXT        NOT NULL,
    status       TEXT        NOT NULL DEFAULT 'pending',  -- pending|running|done|failed|skipped
    attempts     INT         NOT NULL DEFAULT 0,
    priority     INT         NOT NULL DEFAULT 5,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    claimed_at   TIMESTAMPTZ,
    finished_at  TIMESTAMPTZ,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_postcall_queue ON rt.postcall_jobs (status, priority, id);
CREATE INDEX IF NOT EXISTS idx_postcall_hash  ON rt.postcall_jobs (phone_hash);
ALTER TABLE rt.postcall_jobs DISABLE ROW LEVEL SECURITY;

-- Turn lookups during reconstruction. call_events already indexes (call_id, id);
-- this narrows to the turn rows, which are the hot path for recovery.
CREATE INDEX IF NOT EXISTS idx_call_events_turns
    ON rt.call_events (call_id, id) WHERE kind = 'turn';


-- ─── Enqueue ────────────────────────────────────────────────
-- Called at pickup. ON CONFLICT DO NOTHING so a retry, a reconnect, or the
-- recovery sweep can all call it freely.
CREATE OR REPLACE FUNCTION public.rt_postcall_enqueue(
    p_call_id TEXT, p_hash TEXT, p_priority INT DEFAULT 5
)
RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    INSERT INTO rt.postcall_jobs (call_id, phone_hash, priority)
    VALUES (p_call_id, p_hash, COALESCE(p_priority, 5))
    ON CONFLICT (call_id) DO NOTHING;
END; $$;


-- ─── Claim ──────────────────────────────────────────────────
-- Atomically take one job. SKIP LOCKED so two workers never take the same row.
--
-- The age guard is the important part: a job is enqueued at PICKUP, so a live
-- call has a pending row the whole time it is talking. Claiming that would run
-- extraction against a half-finished conversation and then mark it done, so the
-- real post-call would find nothing to do. A job is therefore only claimable
-- once its call has actually ended, or once it is old enough that no plausible
-- call is still running (the silence monitor ends calls at 180s; a bridged call
-- can run longer, hence the generous default).
CREATE OR REPLACE FUNCTION public.rt_postcall_claim(
    p_max_attempts INT DEFAULT 3,
    p_min_age_seconds INT DEFAULT 900
)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_row rt.postcall_jobs%ROWTYPE;
BEGIN
    UPDATE rt.postcall_jobs j
       SET status = 'running',
           attempts = j.attempts + 1,
           claimed_at = NOW()
     WHERE j.id = (
        SELECT j2.id
          FROM rt.postcall_jobs j2
          JOIN rt.calls c ON c.call_id = j2.call_id
         WHERE j2.status = 'pending'
           AND j2.attempts < COALESCE(p_max_attempts, 3)
           AND (c.ended_at IS NOT NULL
                OR c.started_at < NOW() - make_interval(secs => COALESCE(p_min_age_seconds, 900)))
         ORDER BY j2.priority ASC, j2.id ASC
         FOR UPDATE SKIP LOCKED
         LIMIT 1
     )
     RETURNING j.* INTO v_row;

    IF v_row.call_id IS NULL THEN
        RETURN NULL;
    END IF;
    RETURN jsonb_build_object(
        'call_id', v_row.call_id,
        'phone_hash', v_row.phone_hash,
        'attempts', v_row.attempts
    );
END; $$;


-- ─── Complete ───────────────────────────────────────────────
-- ok=true  -> done. ok=false -> back to pending for another attempt, unless the
-- attempt budget is spent, in which case failed (so one poisoned call can never
-- wedge the queue). 'skipped' is its own terminal state: a call with nothing
-- worth extracting is not a failure and should not be retried.
CREATE OR REPLACE FUNCTION public.rt_postcall_complete(
    p_call_id TEXT, p_ok BOOLEAN, p_error TEXT DEFAULT NULL,
    p_skipped BOOLEAN DEFAULT FALSE, p_max_attempts INT DEFAULT 3
)
RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    UPDATE rt.postcall_jobs
       SET status = CASE
                      WHEN p_skipped THEN 'skipped'
                      WHEN p_ok THEN 'done'
                      WHEN attempts >= COALESCE(p_max_attempts, 3) THEN 'failed'
                      ELSE 'pending'
                    END,
           finished_at = CASE WHEN p_ok OR p_skipped OR attempts >= COALESCE(p_max_attempts, 3)
                              THEN NOW() ELSE NULL END,
           error = p_error
     WHERE call_id = p_call_id;
END; $$;


-- ─── Reconstruct a transcript from the durable turn stream ──
-- The in-memory list is gone after a crash; this rebuilds the same text from
-- the rows written while the caller was speaking. Falls back to the transcript
-- column for calls that predate this migration (they have no turn events, but
-- rt_save_call_metrics may still have landed their text).
CREATE OR REPLACE FUNCTION public.rt_call_transcript(p_call_id TEXT)
RETURNS TEXT LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_text TEXT;
BEGIN
    SELECT string_agg(e.name || ': ' || COALESCE(e.detail->>'text', ''), E'\n' ORDER BY e.id)
      INTO v_text
      FROM rt.call_events e
     WHERE e.call_id = p_call_id AND e.kind = 'turn';

    IF v_text IS NULL OR btrim(v_text) = '' THEN
        SELECT c.transcript INTO v_text FROM rt.calls c WHERE c.call_id = p_call_id;
    END IF;
    RETURN COALESCE(v_text, '');
END; $$;


-- ─── Heal on boot ───────────────────────────────────────────
-- Any ended call that holds a real conversation but has no finished job is
-- queued. Idempotent (enqueue is ON CONFLICT DO NOTHING), so it is safe to run
-- on every worker start. Also re-opens jobs stranded in 'running' by a crash,
-- handing the attempt back the way the claim took it.
CREATE OR REPLACE FUNCTION public.rt_postcall_recover(
    p_min_turns INT DEFAULT 2,
    p_lookback_hours INT DEFAULT 168,
    p_limit INT DEFAULT 50
)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_requeued INT := 0;
    v_enqueued INT := 0;
BEGIN
    -- 1. jobs a crash left mid-flight: return them to pending, refund the attempt
    UPDATE rt.postcall_jobs
       SET status = 'pending',
           attempts = GREATEST(attempts - 1, 0),
           claimed_at = NULL
     WHERE status = 'running';
    GET DIAGNOSTICS v_requeued = ROW_COUNT;

    -- 2. calls that were never queued at all (pre-migration, or enqueue lost)
    --
    -- `postcall IS NULL` is the load-bearing condition. rt_call_finish stamps
    -- that column at the end of the live post-call path, so a non-null value
    -- means extraction already landed for this call. Without this guard the
    -- first sweep re-extracts every historical call — on the dev database that
    -- was 5 completed calls queued for a pointless re-run that would have
    -- overwritten current facts with a stale second pass. Recovery is for calls
    -- that never finished, not calls that did.
    WITH candidates AS (
        SELECT c.call_id, c.phone_hash
          FROM rt.calls c
         WHERE c.started_at > NOW() - make_interval(hours => COALESCE(p_lookback_hours, 168))
           AND c.phone_hash IS NOT NULL
           AND c.postcall IS NULL
           AND NOT EXISTS (SELECT 1 FROM rt.postcall_jobs j WHERE j.call_id = c.call_id)
           AND (
                (SELECT COUNT(*) FROM rt.call_events e
                  WHERE e.call_id = c.call_id AND e.kind = 'turn') >= COALESCE(p_min_turns, 2)
             OR COALESCE(btrim(c.transcript), '') <> ''
           )
         ORDER BY c.started_at DESC
         LIMIT COALESCE(p_limit, 50)
    )
    INSERT INTO rt.postcall_jobs (call_id, phone_hash, priority)
    SELECT call_id, phone_hash, 7 FROM candidates
    ON CONFLICT (call_id) DO NOTHING;
    GET DIAGNOSTICS v_enqueued = ROW_COUNT;

    RETURN jsonb_build_object('requeued_running', v_requeued, 'enqueued_missed', v_enqueued);
END; $$;


-- ─── Queue visibility (ops) ─────────────────────────────────
CREATE OR REPLACE FUNCTION public.rt_postcall_queue_stats()
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    RETURN (SELECT COALESCE(jsonb_object_agg(status, n), '{}'::jsonb)
              FROM (SELECT status, COUNT(*) n FROM rt.postcall_jobs GROUP BY status) s);
END; $$;


-- Worker-only, same posture as the other trace writers.
REVOKE EXECUTE ON FUNCTION public.rt_postcall_enqueue(TEXT,TEXT,INT)              FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.rt_postcall_claim(INT,INT)                      FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.rt_postcall_complete(TEXT,BOOLEAN,TEXT,BOOLEAN,INT) FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.rt_call_transcript(TEXT)                        FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.rt_postcall_recover(INT,INT,INT)                FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.rt_postcall_queue_stats()                       FROM PUBLIC, anon, authenticated;

GRANT EXECUTE ON FUNCTION public.rt_postcall_enqueue(TEXT,TEXT,INT)               TO service_role;
GRANT EXECUTE ON FUNCTION public.rt_postcall_claim(INT,INT)                       TO service_role;
GRANT EXECUTE ON FUNCTION public.rt_postcall_complete(TEXT,BOOLEAN,TEXT,BOOLEAN,INT) TO service_role;
GRANT EXECUTE ON FUNCTION public.rt_call_transcript(TEXT)                         TO service_role;
GRANT EXECUTE ON FUNCTION public.rt_postcall_recover(INT,INT,INT)                 TO service_role;
GRANT EXECUTE ON FUNCTION public.rt_postcall_queue_stats()                        TO service_role;
