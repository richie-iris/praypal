-- ============================================================
-- Lock down every caller RPC — 2026-08-07
--
-- The bootstrap granted every rt_* function TO PUBLIC with RLS disabled, and
-- the anon role inherits PUBLIC. Sprint 0 defused exactly two of them and left
-- a note saying the rest would come later. Later is now: real testers are about
-- to call, and today anyone holding the anon key — the credential Supabase
-- designs to be embedded in clients — can pull rt_get_caller_full_bundle for a
-- known phone hash and read an elderly caller's medications, money worries,
-- family names and vaulted door codes, or quietly rewrite what Iris believes
-- about them before the next call.
--
-- Done as a SWEEP over pg_proc rather than a list of names: the list drifts
-- (the surname migration added three functions and re-granted them), and a
-- DROP/CREATE resets an ACL silently. The default-privileges line at the end
-- means a function added tomorrow is locked from birth.
--
-- The worker and the harness authenticate with the service role and are
-- unaffected. Apply: python sql_push.py --file sql/09-lock-down-rpcs.sql
-- ============================================================

DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN
        SELECT p.oid::regprocedure AS sig
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public' AND p.proname LIKE 'rt\_%'
    LOOP
        EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC', r.sig);
        EXECUTE format('REVOKE ALL ON FUNCTION %s FROM anon', r.sig);
        EXECUTE format('REVOKE ALL ON FUNCTION %s FROM authenticated', r.sig);
        EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO service_role', r.sig);
    END LOOP;
END $$;

-- A function created later must not be world-executable by default.
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

-- Belt and braces on the tables themselves: nothing reaches them except through
-- the SECURITY DEFINER functions above, which only service_role may call now.
REVOKE ALL ON ALL TABLES IN SCHEMA rt FROM PUBLIC, anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA rt REVOKE ALL ON TABLES FROM PUBLIC, anon, authenticated;

-- Verify (should list zero rows granting EXECUTE to PUBLIC/anon/authenticated):
--   SELECT p.proname, a.grantee
--   FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
--   CROSS JOIN LATERAL aclexplode(p.proacl) a
--   WHERE n.nspname='public' AND p.proname LIKE 'rt\_%'
--     AND a.grantee IN (0, 'anon'::regrole::oid, 'authenticated'::regrole::oid);
