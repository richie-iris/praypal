-- 25-praypal-atrium-and-streaks.sql — Atrium Guide, Deeper Roots Memory, Streaks & Prayer Chains
-- Strictly isolated in schema `pray`. Exposes public RPCs granted only to `service_role`.

-- ── 1. SCHEMA EVOLUTION FOR PRAY.CALLERS ────────────────────────────────────
ALTER TABLE pray.callers
    ADD COLUMN IF NOT EXISTS spiritual_leader TEXT,
    ADD COLUMN IF NOT EXISTS fellowship_place TEXT,
    ADD COLUMN IF NOT EXISTS consecutive_days_count INTEGER DEFAULT 1,
    ADD COLUMN IF NOT EXISTS last_call_date DATE DEFAULT CURRENT_DATE;

-- ── 2. FELLOWSHIP PRAYER CHAIN BLESSINGS TABLE ─────────────────────────────
CREATE TABLE IF NOT EXISTS pray.prayer_chain_blessings (
    id BIGSERIAL PRIMARY KEY,
    intention_id BIGINT NOT NULL REFERENCES pray.intentions(id) ON DELETE CASCADE,
    blessed_by_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pray_blessings_intention ON pray.prayer_chain_blessings(intention_id);
CREATE INDEX IF NOT EXISTS idx_pray_blessings_by ON pray.prayer_chain_blessings(blessed_by_hash);

-- ── 3. RPC: SWITCH ACTIVE GUIDE ─────────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.rt_pray_switch_guide(p_hash TEXT, p_guide TEXT)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, rt, pg_temp
AS $$
DECLARE
    result jsonb;
BEGIN
    UPDATE pray.callers
    SET active_guide = LOWER(TRIM(p_guide)),
        updated_at = NOW()
    WHERE phone_hash = p_hash
    RETURNING to_jsonb(pray.callers.*) INTO result;
    RETURN result;
END;
$$;

-- ── 4. RPC: GET COMMUNITY INTENTION FOR PRAYER CHAIN ────────────────────────
CREATE OR REPLACE FUNCTION public.rt_pray_get_community_intention(p_exclude_hash TEXT)
RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public, rt, pg_temp
AS $$
DECLARE
    result jsonb;
BEGIN
    SELECT jsonb_build_object(
        'id', i.id,
        'intention_text', i.intention_text,
        'tradition', i.tradition,
        'blessing_count', (SELECT COUNT(*) FROM pray.prayer_chain_blessings b WHERE b.intention_id = i.id)
    ) INTO result
    FROM pray.intentions i
    WHERE i.is_community_circle IS TRUE
      AND i.phone_hash != p_exclude_hash
      AND i.is_answered IS FALSE
    ORDER BY RANDOM()
    LIMIT 1;

    RETURN result;
END;
$$;

-- ── 5. RPC: RECORD PRAYER CHAIN BLESSING ────────────────────────────────────
CREATE OR REPLACE FUNCTION public.rt_pray_record_chain_blessing(p_intention_id BIGINT, p_hash TEXT)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, rt, pg_temp
AS $$
DECLARE
    v_total_blessings BIGINT;
    v_owner_hash TEXT;
    v_intention_text TEXT;
BEGIN
    INSERT INTO pray.prayer_chain_blessings (intention_id, blessed_by_hash)
    VALUES (p_intention_id, p_hash);

    SELECT COUNT(*), i.phone_hash, i.intention_text
    INTO v_total_blessings, v_owner_hash, v_intention_text
    FROM pray.prayer_chain_blessings b
    JOIN pray.intentions i ON i.id = b.intention_id
    WHERE b.intention_id = p_intention_id
    GROUP BY i.phone_hash, i.intention_text;

    RETURN jsonb_build_object(
        'intention_id', p_intention_id,
        'total_blessings', v_total_blessings,
        'owner_hash', v_owner_hash,
        'intention_text', v_intention_text
    );
END;
$$;

-- ── 6. UPDATE RPC: UPSERT CALLER WITH SPIRITUAL ROOTS & STREAK ──────────────
CREATE OR REPLACE FUNCTION public.rt_pray_upsert_caller(
    p_hash TEXT,
    p_display_name TEXT DEFAULT NULL,
    p_active_guide TEXT DEFAULT 'atrium',
    p_tradition TEXT DEFAULT 'universal',
    p_name_for_god TEXT DEFAULT 'Lord',
    p_spiritual_leader TEXT DEFAULT NULL,
    p_fellowship_place TEXT DEFAULT NULL
)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, rt, pg_temp
AS $$
DECLARE
    result jsonb;
    v_prev_date DATE;
    v_prev_streak INT;
    v_new_streak INT;
    v_today DATE := CURRENT_DATE;
BEGIN
    SELECT last_call_date, consecutive_days_count
    INTO v_prev_date, v_prev_streak
    FROM pray.callers
    WHERE phone_hash = p_hash;

    IF v_prev_date IS NULL THEN
        v_new_streak := 1;
    ELSIF v_prev_date = v_today THEN
        v_new_streak := COALESCE(v_prev_streak, 1);
    ELSIF v_prev_date = v_today - 1 THEN
        v_new_streak := COALESCE(v_prev_streak, 1) + 1;
    ELSE
        v_new_streak := 1;
    END IF;

    INSERT INTO pray.callers (
        phone_hash, display_name, active_guide, spiritual_tradition,
        preferred_name_for_god, spiritual_leader, fellowship_place,
        consecutive_days_count, last_call_date, last_call_at, updated_at
    )
    VALUES (
        p_hash, p_display_name, p_active_guide, p_tradition,
        p_name_for_god, p_spiritual_leader, p_fellowship_place,
        v_new_streak, v_today, NOW(), NOW()
    )
    ON CONFLICT (phone_hash) DO UPDATE SET
        display_name = COALESCE(EXCLUDED.display_name, pray.callers.display_name),
        active_guide = COALESCE(EXCLUDED.active_guide, pray.callers.active_guide),
        spiritual_tradition = COALESCE(EXCLUDED.spiritual_tradition, pray.callers.spiritual_tradition),
        preferred_name_for_god = COALESCE(EXCLUDED.preferred_name_for_god, pray.callers.preferred_name_for_god),
        spiritual_leader = COALESCE(EXCLUDED.spiritual_leader, pray.callers.spiritual_leader),
        fellowship_place = COALESCE(EXCLUDED.fellowship_place, pray.callers.fellowship_place),
        consecutive_days_count = v_new_streak,
        last_call_date = v_today,
        last_call_at = NOW(),
        updated_at = NOW()
    RETURNING to_jsonb(pray.callers.*) INTO result;
    RETURN result;
END;
$$;

-- ── 7. UPDATE RPC: GET BUNDLE TO INCLUDE ROOTS & STREAK ─────────────────────
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
        SELECT id, intention_text, tradition, is_answered, is_community_circle, created_at
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

-- ── 8. PERMISSIONS ──────────────────────────────────────────────────────────
REVOKE ALL ON FUNCTION public.rt_pray_switch_guide(TEXT, TEXT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.rt_pray_get_community_intention(TEXT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.rt_pray_record_chain_blessing(BIGINT, TEXT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.rt_pray_upsert_caller(TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.rt_pray_get_bundle(TEXT) FROM PUBLIC, anon, authenticated;

GRANT EXECUTE ON FUNCTION public.rt_pray_switch_guide(TEXT, TEXT) TO service_role;
GRANT EXECUTE ON FUNCTION public.rt_pray_get_community_intention(TEXT) TO service_role;
GRANT EXECUTE ON FUNCTION public.rt_pray_record_chain_blessing(BIGINT, TEXT) TO service_role;
GRANT EXECUTE ON FUNCTION public.rt_pray_upsert_caller(TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT) TO service_role;
GRANT EXECUTE ON FUNCTION public.rt_pray_get_bundle(TEXT) TO service_role;
