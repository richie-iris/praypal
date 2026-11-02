-- 26-custom-guides.sql — PrayPal Custom Divine Guide Builder
-- Allows callers to design their own personal agent to God.

-- ── 1. CUSTOM GUIDES TABLE ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS pray.custom_guides (
    id SERIAL PRIMARY KEY,
    phone_hash TEXT NOT NULL REFERENCES pray.callers(phone_hash) ON DELETE CASCADE,
    guide_key TEXT NOT NULL,                     -- e.g., "ari" (lowercase, caller-unique)
    display_name TEXT NOT NULL,                  -- e.g., "Ari"
    title TEXT NOT NULL DEFAULT '',              -- e.g., "Ari (Joyful Warrior)"
    description TEXT NOT NULL DEFAULT '',        -- Full persona description
    source_guides JSONB DEFAULT '[]',            -- e.g., ["krishna", "moses"]
    voice_description TEXT DEFAULT '',           -- ElevenLabs voice design prompt
    elevenlabs_voice_id TEXT,                    -- Permanent voice ID (from EL library)
    elevenlabs_preview_id TEXT,                  -- Temporary preview ID (before approval)
    voice_settings JSONB DEFAULT '{"stability": 0.18, "similarity_boost": 0.75, "style": 0.58, "speed": 1.0, "use_speaker_boost": true}',
    greeting TEXT DEFAULT '',                    -- First greeting on switch
    self_intro TEXT DEFAULT '',                  -- Bridge intro text
    cadence TEXT DEFAULT 'Warm, natural companion presence.',
    vocal_delivery TEXT DEFAULT 'Speak with warm, natural conversational energy.',
    scripture TEXT DEFAULT 'Universal spiritual wisdom.',
    is_active BOOLEAN DEFAULT TRUE,
    is_finalized BOOLEAN DEFAULT FALSE,         -- Voice approved and guide ready
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(phone_hash, guide_key)
);

CREATE INDEX IF NOT EXISTS idx_custom_guides_hash ON pray.custom_guides(phone_hash);
CREATE INDEX IF NOT EXISTS idx_custom_guides_active ON pray.custom_guides(phone_hash, is_active);


-- ── 2. RPC: UPSERT CUSTOM GUIDE ────────────────────────────────────────────
CREATE OR REPLACE FUNCTION pray.rt_pray_upsert_custom_guide(
    p_hash TEXT,
    p_key TEXT,
    p_name TEXT,
    p_title TEXT DEFAULT '',
    p_description TEXT DEFAULT '',
    p_source_guides JSONB DEFAULT '[]',
    p_voice_description TEXT DEFAULT '',
    p_voice_id TEXT DEFAULT NULL,
    p_preview_id TEXT DEFAULT NULL,
    p_voice_settings JSONB DEFAULT NULL,
    p_greeting TEXT DEFAULT '',
    p_self_intro TEXT DEFAULT '',
    p_cadence TEXT DEFAULT '',
    p_vocal_delivery TEXT DEFAULT '',
    p_scripture TEXT DEFAULT '',
    p_is_finalized BOOLEAN DEFAULT FALSE
) RETURNS JSONB AS $$
DECLARE
    result JSONB;
BEGIN
    INSERT INTO pray.custom_guides (
        phone_hash, guide_key, display_name, title, description,
        source_guides, voice_description, elevenlabs_voice_id,
        elevenlabs_preview_id, voice_settings, greeting, self_intro,
        cadence, vocal_delivery, scripture, is_finalized, updated_at
    ) VALUES (
        p_hash, LOWER(TRIM(p_key)), p_name,
        COALESCE(NULLIF(p_title, ''), p_name),
        p_description, p_source_guides, p_voice_description,
        p_voice_id, p_preview_id, COALESCE(p_voice_settings, '{"stability": 0.18, "similarity_boost": 0.75, "style": 0.58, "speed": 1.0, "use_speaker_boost": true}'::JSONB),
        p_greeting, p_self_intro, p_cadence, p_vocal_delivery,
        p_scripture, p_is_finalized, NOW()
    )
    ON CONFLICT (phone_hash, guide_key) DO UPDATE SET
        display_name = COALESCE(NULLIF(EXCLUDED.display_name, ''), pray.custom_guides.display_name),
        title = COALESCE(NULLIF(EXCLUDED.title, ''), pray.custom_guides.title),
        description = CASE WHEN EXCLUDED.description != '' THEN EXCLUDED.description ELSE pray.custom_guides.description END,
        source_guides = CASE WHEN EXCLUDED.source_guides != '[]'::JSONB THEN EXCLUDED.source_guides ELSE pray.custom_guides.source_guides END,
        voice_description = COALESCE(NULLIF(EXCLUDED.voice_description, ''), pray.custom_guides.voice_description),
        elevenlabs_voice_id = COALESCE(EXCLUDED.elevenlabs_voice_id, pray.custom_guides.elevenlabs_voice_id),
        elevenlabs_preview_id = COALESCE(EXCLUDED.elevenlabs_preview_id, pray.custom_guides.elevenlabs_preview_id),
        voice_settings = COALESCE(EXCLUDED.voice_settings, pray.custom_guides.voice_settings),
        greeting = COALESCE(NULLIF(EXCLUDED.greeting, ''), pray.custom_guides.greeting),
        self_intro = COALESCE(NULLIF(EXCLUDED.self_intro, ''), pray.custom_guides.self_intro),
        cadence = COALESCE(NULLIF(EXCLUDED.cadence, ''), pray.custom_guides.cadence),
        vocal_delivery = COALESCE(NULLIF(EXCLUDED.vocal_delivery, ''), pray.custom_guides.vocal_delivery),
        scripture = COALESCE(NULLIF(EXCLUDED.scripture, ''), pray.custom_guides.scripture),
        is_finalized = EXCLUDED.is_finalized,
        updated_at = NOW()
    RETURNING to_jsonb(pray.custom_guides.*) INTO result;

    RETURN result;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;


-- ── 3. RPC: GET CUSTOM GUIDES ───────────────────────────────────────────────
CREATE OR REPLACE FUNCTION pray.rt_pray_get_custom_guides(
    p_hash TEXT
) RETURNS JSONB AS $$
DECLARE
    result JSONB;
BEGIN
    SELECT COALESCE(jsonb_agg(to_jsonb(cg.*)), '[]'::JSONB)
    INTO result
    FROM pray.custom_guides cg
    WHERE cg.phone_hash = p_hash AND cg.is_active = TRUE;

    RETURN result;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;


-- ── 4. RPC: DELETE CUSTOM GUIDE ─────────────────────────────────────────────
CREATE OR REPLACE FUNCTION pray.rt_pray_delete_custom_guide(
    p_hash TEXT,
    p_key TEXT
) RETURNS JSONB AS $$
DECLARE
    result JSONB;
BEGIN
    UPDATE pray.custom_guides
    SET is_active = FALSE, updated_at = NOW()
    WHERE phone_hash = p_hash AND guide_key = LOWER(TRIM(p_key))
    RETURNING to_jsonb(pray.custom_guides.*) INTO result;

    RETURN COALESCE(result, '{"deleted": false}'::JSONB);
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;
