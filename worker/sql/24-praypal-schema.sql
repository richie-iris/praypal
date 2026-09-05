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
RETURNS VOID AS $$
BEGIN
    DELETE FROM pray.deliveries WHERE phone_hash = target_phone_hash;
    DELETE FROM pray.memories WHERE phone_hash = target_phone_hash;
    DELETE FROM pray.intentions WHERE phone_hash = target_phone_hash;
    DELETE FROM pray.altar_candles WHERE phone_hash = target_phone_hash;
    DELETE FROM pray.prayer_bank WHERE phone_hash = target_phone_hash;
    DELETE FROM pray.callers WHERE phone_hash = target_phone_hash;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;
