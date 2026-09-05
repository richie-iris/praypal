-- ============================================================
-- iris-realtime migration v5.3
-- 1. rt_complete_reminder: single-target completion (the old keyword
--    fallback marked EVERY reminder sharing a >=4-char word done —
--    killed Rosa's flowers + mariachi when her cake completed)
-- 2. rt_save_call_metrics: checked into version control (was deployed
--    out-of-band only)
-- 3. rt.audit_log + rt_audit: every write attributable — "who deleted
--    the records" becomes one query
-- Apply: python sql_push.py --file sql/03-reminders-metrics-audit.sql
-- ============================================================

-- ─── 1. Single-target reminder completion ────────────────────
CREATE OR REPLACE FUNCTION public.rt_complete_reminder(p_hash TEXT, p_text TEXT)
RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_id BIGINT;
    v_word TEXT;
    v_count INT;
BEGIN
    IF lower(p_text) IN ('all', 'everything', 'wipe', 'all reminders') THEN
        UPDATE rt.reminders SET is_done = TRUE WHERE phone_hash = p_hash;
        RETURN;
    END IF;

    -- Pass 1: substring match — complete only the single best (shortest) row
    SELECT id INTO v_id FROM rt.reminders
    WHERE phone_hash = p_hash AND is_done = FALSE
      AND (lower(reminder_text) LIKE '%' || lower(p_text) || '%'
           OR lower(p_text) LIKE '%' || lower(reminder_text) || '%')
    ORDER BY length(reminder_text) ASC, id ASC
    LIMIT 1;

    IF v_id IS NOT NULL THEN
        UPDATE rt.reminders SET is_done = TRUE WHERE id = v_id;
        RETURN;
    END IF;

    -- Pass 2: significant-word match — complete ONLY when exactly one
    -- pending reminder matches (ambiguity leaves reminders pending;
    -- a lost completion beats a mass-kill)
    FOR v_word IN SELECT unnest(string_to_array(lower(p_text), ' ')) LOOP
        IF length(v_word) >= 4 AND v_word NOT IN
           ('called', 'already', 'took', 'care', 'done', 'that', 'with', 'about', 'just', 'have') THEN
            SELECT count(*), min(id) INTO v_count, v_id FROM rt.reminders
            WHERE phone_hash = p_hash AND is_done = FALSE
              AND lower(reminder_text) LIKE '%' || v_word || '%';
            IF v_count = 1 THEN
                UPDATE rt.reminders SET is_done = TRUE WHERE id = v_id;
                RETURN;
            END IF;
        END IF;
    END LOOP;
END; $$;
GRANT EXECUTE ON FUNCTION public.rt_complete_reminder(TEXT,TEXT) TO PUBLIC;


-- ─── 2. Call metrics (now versioned; unchanged from deployed) ─
CREATE OR REPLACE FUNCTION public.rt_save_call_metrics(
    p_hash TEXT, p_transcript TEXT DEFAULT NULL, p_in_tokens INT DEFAULT 0, p_out_tokens INT DEFAULT 0
)
RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    INSERT INTO rt.callers (phone_hash, last_transcript, last_input_tokens, last_output_tokens,
                            total_input_tokens, total_output_tokens, last_call_at)
    VALUES (p_hash, p_transcript, COALESCE(p_in_tokens, 0), COALESCE(p_out_tokens, 0),
            COALESCE(p_in_tokens, 0), COALESCE(p_out_tokens, 0), NOW())
    ON CONFLICT (phone_hash) DO UPDATE SET
        last_transcript = COALESCE(p_transcript, rt.callers.last_transcript),
        last_input_tokens = COALESCE(p_in_tokens, 0),
        last_output_tokens = COALESCE(p_out_tokens, 0),
        total_input_tokens = COALESCE(rt.callers.total_input_tokens, 0) + COALESCE(p_in_tokens, 0),
        total_output_tokens = COALESCE(rt.callers.total_output_tokens, 0) + COALESCE(p_out_tokens, 0),
        last_call_at = NOW();
END; $$;
GRANT EXECUTE ON FUNCTION public.rt_save_call_metrics(TEXT,TEXT,INT,INT) TO PUBLIC;


-- ─── 3. Audit log ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS rt.audit_log (
    id          BIGSERIAL   PRIMARY KEY,
    ts          TIMESTAMPTZ DEFAULT NOW(),
    phone_hash  TEXT,
    actor       TEXT,       -- live | postcall | admin | test
    op          TEXT,       -- rpc name
    args        JSONB
);
CREATE INDEX IF NOT EXISTS idx_audit_hash_ts ON rt.audit_log (phone_hash, ts DESC);
ALTER TABLE rt.audit_log DISABLE ROW LEVEL SECURITY;

CREATE OR REPLACE FUNCTION public.rt_audit(p_hash TEXT, p_actor TEXT, p_op TEXT, p_args JSONB)
RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    INSERT INTO rt.audit_log (phone_hash, actor, op, args)
    VALUES (p_hash, p_actor, p_op, p_args);
END; $$;
GRANT EXECUTE ON FUNCTION public.rt_audit(TEXT,TEXT,TEXT,JSONB) TO PUBLIC;
