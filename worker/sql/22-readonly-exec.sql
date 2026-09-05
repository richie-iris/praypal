-- 22-readonly-exec.sql
--
-- Why this exists: sql_push.py let a production read through only after a
-- client-side scan said "exactly one SELECT", then wrapped the caller's text in
-- BEGIN; SET TRANSACTION READ ONLY; ...; COMMIT. The scan is not the Postgres
-- lexer and never will be: `SELECT 1 AS x$a$` is an alias to Postgres but a
-- dollar-quote opener to a naive scan, and a `--` comment ends at a bare CR
-- for Postgres but not for a scan that only looks for LF. Either disagreement
-- smuggles `COMMIT; INSERT ...` past the scan as "part of one statement"; the
-- COMMIT ends the read-only transaction and the INSERT runs on production.
-- No client-side scanner can be trusted to agree with the server's lexer, so
-- the guard moves server-side, where the lexer is the lexer.
--
-- rt_readonly_exec(p_sql) runs the caller's text as a SUBQUERY of one
-- statement, inside a transaction it has just made read-only. What actually
-- guarantees that nothing is written, in order of strength:
--
--   * SET LOCAL transaction_read_only = on — any INSERT / UPDATE / DELETE /
--     DDL the text manages to reach, directly or through a function it calls,
--     is refused by Postgres itself ("cannot execute ... in a read-only
--     transaction"). LOCAL scopes it to this transaction, so nothing leaks
--     into the calling session; a transaction can always be turned read-only,
--     never back.
--   * A COMMIT / ROLLBACK cannot run inside a plain function at all: only a
--     procedure called from a top-level CALL may end a transaction, and even
--     there not while a SET LOCAL is in force. So the read-only setting cannot
--     be ended early by anything the text contains.
--   * The semicolon guard below: p_sql is scanned the way the Postgres lexer
--     reads it and refused if a `;` appears anywhere outside a single-quoted
--     or dollar-quoted literal. This is defence in depth, NOT the guarantee —
--     see the next paragraph for why it is needed at all.
--
-- The subquery shape — format('SELECT to_jsonb(t) FROM (%s) t', p_sql) — is
-- NOT a guarantee on its own, and an earlier version of this header wrongly
-- said it was. RETURN QUERY EXECUTE hands the string to SPI, which happily
-- runs a STATEMENT LIST: a text such as `SELECT 1 AS a) t; SELECT pg_sleep(0);
-- SELECT to_jsonb(t) FROM (SELECT 3 AS c` closes the parenthesis, runs a
-- second statement, and reopens the shape so the last statement still returns
-- jsonb. Only the read-only transaction (and the impossibility of COMMIT)
-- stops such a list from writing. The semicolon guard exists so that a list
-- never reaches EXECUTE in the first place, which keeps the operator's error
-- honest ("one statement only") instead of whatever the second statement
-- happens to fail with — and so that a future relaxation of either server
-- guard is not silently one `;` away from a write.
--
-- Guard scanner rules (conservative on purpose — when in doubt, refuse):
--   * 'single-quoted' literals are skipped; '' doubling works because the
--     scanner pairs quotes. Backslash escapes (E'\'') are NOT honoured, which
--     can only END a literal earlier than Postgres would and so can only
--     refuse more, never less. Write E'it''s', not E'it\'s'.
--   * $tag$ dollar-quoted literals are skipped when the `$` starts a token; a
--     `$` that continues an identifier (`x$a$`, `$1`) is an identifier
--     character, exactly as scan.l reads it.
--   * -- and /* */ comments (nested) are stepped over so a quote inside one
--     cannot open a phantom literal, but a `;` INSIDE a comment is still
--     refused: it can never be needed, and refusing it is one less thing to
--     reason about. Unterminated literals and comments are refused too.
--   * EXPLAIN and SHOW are not subqueries: they fail here with a syntax
--     error rather than run outside the guard. sql_push.py refuses them on
--     production before they are sent, so the operator sees a plain message.
--
-- Result shape: to_jsonb(t) builds one object per row from the column NAMES.
-- Duplicate or unnamed columns collapse — `SELECT 1, 2` yields
-- {"?column?": 2}, and two columns both called `id` keep only the last — so
-- alias every column you want to see: `SELECT a.id AS a_id, b.id AS b_id`.
--
-- sql_push.py sends the text as ONE dollar-quoted argument to this function
-- (random tag that does not occur in the text), so on a production ref the
-- caller's text is never top-level SQL unless the operator passes BOTH
-- --confirm <ref> and --i-know-this-is-prod.
--
-- SECURITY DEFINER with the search_path pin (sql/18) like every other rt_*
-- RPC, and locked to service_role so PostgREST never exposes it to anon /
-- authenticated: an arbitrary-SELECT endpoint reads every transcript.
CREATE OR REPLACE FUNCTION public.rt_readonly_exec(p_sql TEXT)
RETURNS SETOF JSONB LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, rt, pg_temp AS $fn$
DECLARE
    v_i     INT := 1;
    v_n     INT := length(p_sql);
    v_c     TEXT;
    v_tag   TEXT;
    v_close INT;
    v_depth INT;
    -- true while the previous character was part of an identifier, so a `$`
    -- that follows it continues the identifier instead of opening a quote
    v_ident BOOLEAN := false;
BEGIN
    SET LOCAL transaction_read_only = on;
    IF p_sql IS NULL THEN
        RAISE EXCEPTION 'rt_readonly_exec: p_sql is NULL';
    END IF;
    -- Semicolon guard (see header): refuse the text before EXECUTE ever sees
    -- it if a `;` sits anywhere outside a single-quoted or dollar-quoted
    -- literal. Character by character rather than regexp_replace because an
    -- ARE branch takes the greediness of its FIRST quantifier, so a
    -- back-referenced non-greedy $tag$ pattern would swallow `$a$ x $a$ ;
    -- $a$ y $a$` as one literal — the loosening direction.
    WHILE v_i <= v_n LOOP
        v_c := substr(p_sql, v_i, 1);
        IF v_c = ';' THEN
            RAISE EXCEPTION 'rt_readonly_exec: semicolon at character % — one SELECT only, no statement list', v_i;
        ELSIF v_c = '''' THEN
            v_close := position('''' IN substr(p_sql, v_i + 1));
            IF v_close = 0 THEN
                RAISE EXCEPTION 'rt_readonly_exec: unterminated single-quoted literal at character %', v_i;
            END IF;
            v_i := v_i + v_close + 1;
            v_ident := false;
        ELSIF v_c = '$' AND NOT v_ident THEN
            v_tag := substring(substr(p_sql, v_i) FROM '^\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$');
            IF v_tag IS NULL THEN
                v_i := v_i + 1;   -- `$1` and friends: a `$` that opens no quote
                v_ident := true;
            ELSE
                v_close := position(v_tag IN substr(p_sql, v_i + length(v_tag)));
                IF v_close = 0 THEN
                    RAISE EXCEPTION 'rt_readonly_exec: unterminated dollar-quoted literal at character %', v_i;
                END IF;
                v_i := v_i + length(v_tag) + v_close - 1 + length(v_tag);
                v_ident := false;
            END IF;
        ELSIF v_c = '-' AND substr(p_sql, v_i + 1, 1) = '-' THEN
            -- line comment: runs to LF or CR (Postgres ends it at either)
            v_i := v_i + 2;
            WHILE v_i <= v_n AND substr(p_sql, v_i, 1) NOT IN (E'\n', E'\r') LOOP
                IF substr(p_sql, v_i, 1) = ';' THEN
                    RAISE EXCEPTION 'rt_readonly_exec: semicolon inside a comment at character % — remove it', v_i;
                END IF;
                v_i := v_i + 1;
            END LOOP;
            v_ident := false;
        ELSIF v_c = '/' AND substr(p_sql, v_i + 1, 1) = '*' THEN
            -- block comment; these nest in Postgres
            v_depth := 1;
            v_i := v_i + 2;
            WHILE v_i <= v_n AND v_depth > 0 LOOP
                IF substr(p_sql, v_i, 2) = '/*' THEN
                    v_depth := v_depth + 1;
                    v_i := v_i + 2;
                ELSIF substr(p_sql, v_i, 2) = '*/' THEN
                    v_depth := v_depth - 1;
                    v_i := v_i + 2;
                ELSIF substr(p_sql, v_i, 1) = ';' THEN
                    RAISE EXCEPTION 'rt_readonly_exec: semicolon inside a comment at character % — remove it', v_i;
                ELSE
                    v_i := v_i + 1;
                END IF;
            END LOOP;
            IF v_depth > 0 THEN
                RAISE EXCEPTION 'rt_readonly_exec: unterminated block comment';
            END IF;
            v_ident := false;
        ELSE
            v_ident := v_c = '$' OR v_c ~ '^[A-Za-z0-9_]$' OR ascii(v_c) >= 128;
            v_i := v_i + 1;
        END IF;
    END LOOP;
    RETURN QUERY EXECUTE format('SELECT to_jsonb(t) FROM (%s) t', p_sql);
END; $fn$;

REVOKE ALL ON FUNCTION public.rt_readonly_exec(TEXT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.rt_readonly_exec(TEXT) TO service_role;
