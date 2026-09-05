-- ============================================================
-- Call trace — 2026-08-07
-- Everything a call does, kept per call instead of overwritten.
--
-- Before this, the only record of a call was rt.callers.last_transcript,
-- which the NEXT call replaced. Debugging meant SSH'ing into the droplet and
-- reading journald. These two tables hold what actually happened: the exact
-- system prompt the model received, the transcript, every tool call with its
-- arguments and result, every guard decision, the bridge, and both postcall
-- passes — so a call can be reconstructed long after it ended.
--
-- Apply: python sql_push.py --file sql/07-call-trace.sql
-- ============================================================

CREATE TABLE IF NOT EXISTS rt.calls (
    call_id         TEXT        PRIMARY KEY,      -- LiveKit job id
    phone_hash      TEXT        NOT NULL,
    room            TEXT,
    started_at      TIMESTAMPTZ DEFAULT NOW(),
    ended_at        TIMESTAMPTZ,
    duration_sec    NUMERIC,
    agent_version   TEXT,                          -- git sha the worker was running
    model           TEXT,
    voice           TEXT,
    display_name    TEXT,
    agent_alias     TEXT,
    call_number     INT,                           -- visit # at the time
    greeting        TEXT,
    system_prompt   TEXT,                          -- verbatim, what the model got
    transcript      TEXT,
    in_tokens       INT         DEFAULT 0,
    out_tokens      INT         DEFAULT 0,
    bridge_number   TEXT,
    bridge_mode     TEXT,
    postcall        JSONB,                         -- pass 1 extraction + pass 2 canvas
    meta            JSONB
);
CREATE INDEX IF NOT EXISTS idx_calls_hash_time ON rt.calls (phone_hash, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_calls_time ON rt.calls (started_at DESC);
ALTER TABLE rt.calls DISABLE ROW LEVEL SECURITY;

CREATE TABLE IF NOT EXISTS rt.call_events (
    id          BIGSERIAL   PRIMARY KEY,
    call_id     TEXT        NOT NULL,
    at          TIMESTAMPTZ DEFAULT NOW(),
    elapsed_ms  INT,                               -- since the call started
    kind        TEXT,                              -- tool | guard | bridge | cue | postcall | note
    name        TEXT,                              -- db_tool, web_search, find_number, …
    detail      JSONB                              -- args, result, reason
);
CREATE INDEX IF NOT EXISTS idx_call_events ON rt.call_events (call_id, id);
ALTER TABLE rt.call_events DISABLE ROW LEVEL SECURITY;


-- ─── Writers (worker side) ───────────────────────────────────

CREATE OR REPLACE FUNCTION public.rt_call_start(
    p_call_id TEXT, p_hash TEXT, p_room TEXT, p_version TEXT, p_model TEXT,
    p_voice TEXT, p_name TEXT, p_alias TEXT, p_number INT, p_greeting TEXT,
    p_prompt TEXT
)
RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    INSERT INTO rt.calls (call_id, phone_hash, room, agent_version, model, voice,
                          display_name, agent_alias, call_number, greeting, system_prompt)
    VALUES (p_call_id, p_hash, p_room, p_version, p_model, p_voice,
            p_name, p_alias, p_number, p_greeting, p_prompt)
    ON CONFLICT (call_id) DO UPDATE SET
        system_prompt = EXCLUDED.system_prompt,
        greeting = EXCLUDED.greeting;
END; $$;

CREATE OR REPLACE FUNCTION public.rt_call_event(
    p_call_id TEXT, p_elapsed INT, p_kind TEXT, p_name TEXT, p_detail JSONB
)
RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    INSERT INTO rt.call_events (call_id, elapsed_ms, kind, name, detail)
    VALUES (p_call_id, p_elapsed, p_kind, p_name, p_detail);
END; $$;

CREATE OR REPLACE FUNCTION public.rt_call_finish(
    p_call_id TEXT, p_transcript TEXT, p_in INT, p_out INT,
    p_bridge_number TEXT, p_bridge_mode TEXT, p_postcall JSONB, p_meta JSONB
)
RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    UPDATE rt.calls SET
        ended_at = NOW(),
        duration_sec = EXTRACT(EPOCH FROM (NOW() - started_at)),
        transcript = COALESCE(p_transcript, transcript),
        in_tokens = COALESCE(p_in, 0),
        out_tokens = COALESCE(p_out, 0),
        bridge_number = p_bridge_number,
        bridge_mode = p_bridge_mode,
        postcall = COALESCE(p_postcall, postcall),
        meta = COALESCE(p_meta, meta)
    WHERE call_id = p_call_id;
END; $$;


-- ─── Readers (console side) ──────────────────────────────────

CREATE OR REPLACE FUNCTION public.rt_console_calls(p_limit INT DEFAULT 100)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    RETURN (SELECT COALESCE(jsonb_agg(row_to_json(c)::jsonb ORDER BY c.started_at DESC), '[]')
            FROM (SELECT * FROM rt.calls ORDER BY started_at DESC LIMIT p_limit) c);
END; $$;

CREATE OR REPLACE FUNCTION public.rt_console_events(p_limit INT DEFAULT 4000)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    RETURN (SELECT COALESCE(jsonb_agg(row_to_json(e)::jsonb ORDER BY e.id), '[]')
            FROM (SELECT * FROM rt.call_events ORDER BY id DESC LIMIT p_limit) e);
END; $$;

CREATE OR REPLACE FUNCTION public.rt_console_state()
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    RETURN jsonb_build_object(
      'callers', (SELECT COALESCE(jsonb_agg(row_to_json(c)::jsonb), '[]') FROM rt.callers c),
      'schemas', (SELECT COALESCE(jsonb_agg(row_to_json(s)::jsonb), '[]') FROM rt.account_schema_registry s),
      'reminders', (SELECT COALESCE(jsonb_agg(row_to_json(r)::jsonb), '[]') FROM rt.reminders r),
      'audit', (SELECT COALESCE(jsonb_agg(row_to_json(a)::jsonb), '[]')
                FROM (SELECT * FROM rt.audit_log ORDER BY id DESC LIMIT 500) a)
    );
END; $$;

-- Trace writers are worker-only; console readers expose everything about every
-- caller, so they stay service-role just like rt_get_all_callers.
REVOKE EXECUTE ON FUNCTION public.rt_console_calls(INT)  FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.rt_console_events(INT) FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.rt_console_state()     FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.rt_console_calls(INT)  TO service_role;
GRANT EXECUTE ON FUNCTION public.rt_console_events(INT) TO service_role;
GRANT EXECUTE ON FUNCTION public.rt_console_state()     TO service_role;
GRANT EXECUTE ON FUNCTION public.rt_call_start(TEXT,TEXT,TEXT,TEXT,TEXT,TEXT,TEXT,TEXT,INT,TEXT,TEXT) TO service_role;
GRANT EXECUTE ON FUNCTION public.rt_call_event(TEXT,INT,TEXT,TEXT,JSONB) TO service_role;
GRANT EXECUTE ON FUNCTION public.rt_call_finish(TEXT,TEXT,INT,INT,TEXT,TEXT,JSONB,JSONB) TO service_role;
