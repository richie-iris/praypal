-- 18-pin-search-path.sql
--
-- Every rt_* RPC is SECURITY DEFINER (runs as the function owner, postgres)
-- and none of them pinned search_path. A SECURITY DEFINER function without a
-- fixed search_path resolves unqualified names — now(), jsonb_build_object,
-- even operators — through the CALLER's search_path, so a role that can
-- create objects in a schema ahead of pg_catalog can shadow a builtin and
-- run its own code as postgres. Postgres' own docs call this out; Supabase's
-- advisor flags it as "function_search_path_mutable".
--
-- One sweep, driven by the catalog, so nothing already deployed is missed
-- and nothing has to be re-created. Re-running it is harmless: ALTER FUNCTION
-- ... SET simply overwrites the same setting.
--
-- From this migration on, every new SECURITY DEFINER function carries the
-- pin inline in its own CREATE (see 19, 20) — the sweep covers the past, the
-- inline SET covers the future.

DO $$
DECLARE p RECORD;
BEGIN
    FOR p IN
        SELECT pr.oid
          FROM pg_proc pr
          JOIN pg_namespace ns ON ns.oid = pr.pronamespace
         WHERE ns.nspname = 'public'
           AND pr.proname LIKE 'rt\_%'
           AND pr.prosecdef
    LOOP
        -- regprocedure renders the full signature (name + arg types), which
        -- is what ALTER FUNCTION needs to pick one overload.
        EXECUTE format('ALTER FUNCTION %s SET search_path = pg_catalog, public, rt, pg_temp', p.oid::regprocedure);
    END LOOP;
END $$;
