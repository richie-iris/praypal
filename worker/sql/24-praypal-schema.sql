-- 24-praypal-schema.sql — PrayPal Sacred Database Schema & Memory Ledger
-- Strictly isolated in schema `pray`. Completely separate from companion schema `rt` and clinical schema `kb`.

CREATE SCHEMA IF NOT EXISTS pray;

-- ── 1. CALLERS & SPIRITUAL PROFILE ──────────────────────────────────────────
-- Hashed with RT_PHONE_HASH_PEPPER. Never stores plaintext E.164 phone numbers.
CREATE TABLE IF NOT EXISTS pray.callers (
    phone_hash TEXT PRIMARY KEY,
    display_name TEXT,
    active_guide TEXT DEFAULT 'god',            -- 'god', 'jesus', 'shiva', 'krishna', 'moses', 'noah', 'mother', 'syncretic'
    spiritual_tradition TEXT DEFAULT 'universal', -- 'christian', 'hindu', 'jewish', 'islamic', 'buddhist', 'universal'
    preferred_name_for_god TEXT DEFAULT 'Lord',
    timezone TEXT DEFAULT 'America/New_York',
    prayer_bank_tokens INTEGER DEFAULT 5,       -- Starting grace tokens
    stripe_customer_id TEXT,
    subscription_tier TEXT DEFAULT 'grace',     -- 'grace', 'devotion', 'sanctuary'
    subscription_status TEXT DEFAULT 'active',
    daily_minutes_used NUMERIC(6,2) DEFAULT 0.0,
    last_call_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pray_callers_tier ON pray.callers(subscription_tier);
CREATE INDEX IF NOT EXISTS idx_pray_callers_tradition ON pray.callers(spiritual_tradition);

-- ── 2. SACRED MEMORY LEDGER ────────────────────────────────────────────────
-- Remembers loved ones, health petitions, confessions, and spiritual milestones.
CREATE TABLE IF NOT EXISTS pray.memories (
    id BIGSERIAL PRIMARY KEY,
    phone_hash TEXT NOT NULL REFERENCES pray.callers(phone_hash) ON DELETE CASCADE,
    category TEXT NOT NULL,                     -- 'loved_one', 'healing_petition', 'confession', 'milestone', 'answered_prayer'
    subject_name TEXT,                          -- e.g. "Grandmother Maya", "Sister Sarah"
    content TEXT NOT NULL,
    sacred_vocabulary TEXT,                     -- e.g. "prefers Psalms", "chanted Om Namah Shivaya"
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pray_memories_caller ON pray.memories(phone_hash);
CREATE INDEX IF NOT EXISTS idx_pray_memories_cat ON pray.memories(category);

-- ── 3. PRAYER INTENTIONS & FELLOWSHIP CIRCLES ──────────────────────────────
CREATE TABLE IF NOT EXISTS pray.intentions (
    id BIGSERIAL PRIMARY KEY,
    phone_hash TEXT NOT NULL REFERENCES pray.callers(phone_hash) ON DELETE CASCADE,
    intention_text TEXT NOT NULL,
    tradition TEXT DEFAULT 'universal',
    is_answered BOOLEAN DEFAULT FALSE,
    answered_at TIMESTAMPTZ,
    answered_gratitude_note TEXT,
    is_community_circle BOOLEAN DEFAULT FALSE,  -- Anonymously shared with fellowship prayer circles
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pray_intentions_caller ON pray.intentions(phone_hash);
CREATE INDEX IF NOT EXISTS idx_pray_intentions_circle ON pray.intentions(is_community_circle) WHERE is_community_circle IS TRUE;

-- ── 4. MULTIMODAL DELIVERIES (Cards, Audio, Video) ─────────────────────────
CREATE TABLE IF NOT EXISTS pray.deliveries (
    id BIGSERIAL PRIMARY KEY,
    phone_hash TEXT NOT NULL REFERENCES pray.callers(phone_hash) ON DELETE CASCADE,
    delivery_type TEXT NOT NULL,                -- 'prayer_card', 'bhajan_audio', 'meditation_video'
    media_url TEXT NOT NULL,
    prompt_used TEXT,
    title TEXT,
    sent_via TEXT DEFAULT 'sms',                -- 'sms', 'mms', 'whatsapp', 'webrtc'
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pray_deliveries_caller ON pray.deliveries(phone_hash);

-- ── 5. PRAYER BANK & RECIPROCITY GRACE FUND ────────────────────────────────
CREATE TABLE IF NOT EXISTS pray.prayer_bank (
    id BIGSERIAL PRIMARY KEY,
    phone_hash TEXT NOT NULL REFERENCES pray.callers(phone_hash) ON DELETE CASCADE,
    tokens_credited INTEGER DEFAULT 0,
    tokens_debited INTEGER DEFAULT 0,
    action_type TEXT NOT NULL,                  -- 'subscription_grant', 'light_candle', 'sponsor_call', 'grace_fund_donation'
    description TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS pray.altar_candles (
    id BIGSERIAL PRIMARY KEY,
    phone_hash TEXT NOT NULL REFERENCES pray.callers(phone_hash) ON DELETE CASCADE,
    intention_text TEXT NOT NULL,
    guide TEXT DEFAULT 'god',
    expires_at TIMESTAMPTZ NOT NULL,            -- 7-day perpetual candle
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pray_candles_active ON pray.altar_candles(is_active) WHERE is_active IS TRUE;

-- ── 6. FELLOWSHIP CHRON WATCHER EVENTS ────────────────────────────────────
CREATE TABLE IF NOT EXISTS pray.fellowship_events (
    id BIGSERIAL PRIMARY KEY,
    event_name TEXT NOT NULL,
    event_type TEXT NOT NULL,                   -- 'disaster_alert', 'holiday_celebration', 'communal_vigil'
    tradition TEXT,
    region TEXT,
    details TEXT,
    stream_audio_url TEXT,
    is_active BOOLEAN DEFAULT TRUE,
    starts_at TIMESTAMPTZ NOT NULL,
    ends_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── 7. ATOMIC FORGET ME (CONFESSIONAL PRIVACY PRIVILEGE) ────────────────────
CREATE OR REPLACE FUNCTION pray.forget_caller(target_phone_hash TEXT)
RETURNS VOID
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, rt, pg_temp
AS $$
BEGIN
    DELETE FROM pray.deliveries WHERE phone_hash = target_phone_hash;
    DELETE FROM pray.memories WHERE phone_hash = target_phone_hash;
    DELETE FROM pray.intentions WHERE phone_hash = target_phone_hash;
    DELETE FROM pray.altar_candles WHERE phone_hash = target_phone_hash;
    DELETE FROM pray.prayer_bank WHERE phone_hash = target_phone_hash;
    DELETE FROM pray.callers WHERE phone_hash = target_phone_hash;
END;
$$;


-- ── 8. PUBLIC REST RPC WRAPPERS FOR WORKER ─────────────────────────────────
-- Exposes pray schema safely to service_role via PostgREST RPC.

CREATE OR REPLACE FUNCTION public.rt_pray_get_caller(p_hash TEXT)
RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public, rt, pg_temp
AS $$
DECLARE
    result jsonb;
BEGIN
    SELECT to_jsonb(c) INTO result
    FROM pray.callers c
    WHERE c.phone_hash = p_hash;
    RETURN result;
END;
$$;

CREATE OR REPLACE FUNCTION public.rt_pray_upsert_caller(
    p_hash TEXT,
    p_display_name TEXT DEFAULT NULL,
    p_active_guide TEXT DEFAULT 'god',
    p_tradition TEXT DEFAULT 'universal',
    p_name_for_god TEXT DEFAULT 'Lord'
)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, rt, pg_temp
AS $$
DECLARE
    result jsonb;
BEGIN
    INSERT INTO pray.callers (phone_hash, display_name, active_guide, spiritual_tradition, preferred_name_for_god, last_call_at, updated_at)
    VALUES (p_hash, p_display_name, p_active_guide, p_tradition, p_name_for_god, NOW(), NOW())
    ON CONFLICT (phone_hash) DO UPDATE SET
        display_name = COALESCE(EXCLUDED.display_name, pray.callers.display_name),
        active_guide = COALESCE(EXCLUDED.active_guide, pray.callers.active_guide),
        spiritual_tradition = COALESCE(EXCLUDED.spiritual_tradition, pray.callers.spiritual_tradition),
        preferred_name_for_god = COALESCE(EXCLUDED.preferred_name_for_god, pray.callers.preferred_name_for_god),
        last_call_at = NOW(),
        updated_at = NOW()
    RETURNING to_jsonb(pray.callers.*) INTO result;
    RETURN result;
END;
$$;

CREATE OR REPLACE FUNCTION public.rt_pray_add_memory(
    p_hash TEXT,
    p_category TEXT,
    p_content TEXT,
    p_subject TEXT DEFAULT NULL,
    p_vocab TEXT DEFAULT NULL
)
RETURNS BIGINT
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, rt, pg_temp
AS $$
DECLARE
    new_id BIGINT;
BEGIN
    INSERT INTO pray.memories (phone_hash, category, subject_name, content, sacred_vocabulary, created_at, updated_at)
    VALUES (p_hash, p_category, p_subject, p_content, p_vocab, NOW(), NOW())
    RETURNING id INTO new_id;
    RETURN new_id;
END;
$$;

CREATE OR REPLACE FUNCTION public.rt_pray_add_intention(
    p_hash TEXT,
    p_text TEXT,
    p_tradition TEXT DEFAULT 'universal',
    p_circle BOOLEAN DEFAULT FALSE
)
RETURNS BIGINT
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, rt, pg_temp
AS $$
DECLARE
    new_id BIGINT;
BEGIN
    INSERT INTO pray.intentions (phone_hash, intention_text, tradition, is_community_circle, created_at)
    VALUES (p_hash, p_text, p_tradition, p_circle, NOW())
    RETURNING id INTO new_id;
    RETURN new_id;
END;
$$;

CREATE OR REPLACE FUNCTION public.rt_pray_forget_caller(p_hash TEXT)
RETURNS VOID
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, rt, pg_temp
AS $$
BEGIN
    PERFORM pray.forget_caller(p_hash);
END;
$$;

CREATE OR REPLACE FUNCTION public.rt_pray_get_bundle(p_hash TEXT)
RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public, rt, pg_temp
AS $$
DECLARE
    result jsonb;
    v_caller jsonb;
    v_memories jsonb;
    v_intentions jsonb;
BEGIN
    SELECT to_jsonb(c) INTO v_caller
    FROM pray.callers c
    WHERE c.phone_hash = p_hash;

    SELECT COALESCE(jsonb_agg(to_jsonb(m)), '[]'::jsonb) INTO v_memories
    FROM (
        SELECT category, subject_name, content, sacred_vocabulary, created_at
        FROM pray.memories
        WHERE phone_hash = p_hash AND is_active = TRUE
        ORDER BY created_at DESC
        LIMIT 20
    ) m;

    SELECT COALESCE(jsonb_agg(to_jsonb(i)), '[]'::jsonb) INTO v_intentions
    FROM (
        SELECT intention_text, tradition, is_answered, created_at
        FROM pray.intentions
        WHERE phone_hash = p_hash
        ORDER BY created_at DESC
        LIMIT 10
    ) i;

    result := jsonb_build_object(
        'caller', v_caller,
        'memories', v_memories,
        'intentions', v_intentions
    );

    RETURN result;
END;
$$;

REVOKE ALL ON FUNCTION public.rt_pray_get_caller(TEXT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.rt_pray_get_bundle(TEXT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.rt_pray_upsert_caller(TEXT, TEXT, TEXT, TEXT, TEXT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.rt_pray_add_memory(TEXT, TEXT, TEXT, TEXT, TEXT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.rt_pray_add_intention(TEXT, TEXT, TEXT, BOOLEAN) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.rt_pray_forget_caller(TEXT) FROM PUBLIC, anon, authenticated;

GRANT EXECUTE ON FUNCTION public.rt_pray_get_caller(TEXT) TO service_role;
GRANT EXECUTE ON FUNCTION public.rt_pray_get_bundle(TEXT) TO service_role;
GRANT EXECUTE ON FUNCTION public.rt_pray_upsert_caller(TEXT, TEXT, TEXT, TEXT, TEXT) TO service_role;
GRANT EXECUTE ON FUNCTION public.rt_pray_add_memory(TEXT, TEXT, TEXT, TEXT, TEXT) TO service_role;
GRANT EXECUTE ON FUNCTION public.rt_pray_add_intention(TEXT, TEXT, TEXT, BOOLEAN) TO service_role;
GRANT EXECUTE ON FUNCTION public.rt_pray_forget_caller(TEXT) TO service_role;

