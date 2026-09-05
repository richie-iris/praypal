#!/usr/bin/env python3
"""
sql_push.py — Push arbitrary SQL to Supabase via the Management API.
Usage:
    python3 sql_push.py "SELECT 1"
    python3 sql_push.py "$(cat sql/my_migration.sql)"
    python3 sql_push.py --wipe --confirm <project-ref>   # truncate all rt.* tables
    python3 sql_push.py --file sql/foo.sql [--confirm <project-ref>]
    python3 sql_push.py "UPDATE ..." --confirm <ref> --i-know-this-is-prod   # a prod write

Target project: SUPABASE_PROJECT_REF, else RT_ALLOWED_SUPABASE_REF (single
value). There is no default — a script that can wipe a database must never
guess which one.

Production (a ref in RT_PROD_SUPABASE_REFS, or a prod AGENT_NAME) is read-only
from here: the payload must scan as one SELECT / WITH … SELECT, and what goes
over the wire is EXACTLY ONE statement —
    SELECT * FROM public.rt_readonly_exec($<random tag>$ <your text> $<random tag>$)
— never the text itself as top-level SQL. rt_readonly_exec (sql/22) turns the
transaction read-only (the guarantee: a COMMIT cannot run inside a function,
so the setting cannot be ended early), refuses any `;` outside a literal, and
runs the text as a subquery, so the server's own lexer, not this script's
scanner, decides what the text is. EXPLAIN and SHOW are reads but not
subqueries — rt_readonly_exec cannot run them — so on production they are
refused here with a plain message instead of failing on the server; they
still run as typed on dev, or on prod under the double acknowledgement. A prod
write needs BOTH `--confirm <ref>` and `--i-know-this-is-prod`; a wipe
(TRUNCATE, rt_wipe_all_data) is never accepted on prod. Elsewhere, anything
that can lose data or change schema needs `--confirm <ref>`.
"""
import sys
import os
import re
import json
import hashlib
import secrets
import urllib.request
from dotenv import load_dotenv

load_dotenv(".env.local")
load_dotenv(".env")

try:
    import config as _config
except Exception:  # noqa: BLE001 - run from anywhere; the fallback below is the same tuple
    _config = None

# The names agent.py boots under on the production boxes; a worker whose
# AGENT_NAME starts with one of these is prod whatever ref it was handed.
PROD_AGENT_NAMES: tuple[str, ...] = tuple(
    getattr(_config, "PROD_AGENT_NAMES", ("iris-phone", "phone-pal-prod"))
)

# Ledger filenames are interpolated into SQL; the migration naming convention
# is the whitelist (same shape tests/test_migrations.py enforces on sql/).
_MIGRATION_NAME = re.compile(r"^\d{2}-[a-z0-9_-]+\.sql$")

# A Supabase project ref is exactly 20 lower-case alphanumerics; anything else
# is a typo, a URL pasted whole, or a name — refuse rather than build a URL
# out of it.
_REF_SHAPE = re.compile(r"^[a-z0-9]{20}$")

# Statements that lose data, change schema or run code. Matched on the raw
# text, comments and strings included: a false positive costs one --confirm,
# a miss costs a table. DO excludes the ON CONFLICT DO NOTHING/UPDATE forms
# (UPDATE catches the latter on its own).
_DESTRUCTIVE = re.compile(
    r"\b(?:TRUNCATE|DROP|DELETE|UPDATE|ALTER|GRANT|REVOKE|EXECUTE|CREATE\s+OR\s+REPLACE)\b"
    r"|\bDO\b(?!\s+(?:NOTHING|UPDATE)\b)"
    r"|\brt_(?:wipe_all_data|forget_caller|purge_\w*)\s*\(",
    re.I,
)

# The wipe class: no flag combination sends these to production from here.
_NEVER_ON_PROD = re.compile(
    r"\bTRUNCATE\b|\bDROP\s+(?:TABLE|SCHEMA|DATABASE)\b|\brt_wipe_all_data\s*\(", re.I
)

# First keyword of a read-shaped statement (is_read_only).
_READ_HEAD = re.compile(r"^(?:SELECT|WITH|EXPLAIN|SHOW)\b", re.I)
# Reads rt_readonly_exec cannot run: it wraps the text as `FROM (<text>) t`,
# and EXPLAIN / SHOW are statements, not subqueries. Refused client-side on the
# un-acknowledged prod path so the operator gets a message, not a server
# syntax error (and so a future server relaxation is not silently relied on).
_NOT_A_SUBQUERY = re.compile(r"^(?:EXPLAIN|SHOW)\b", re.I)
# Words that make a SELECT-shaped statement write, lock or run code
# (SELECT INTO, FOR UPDATE, EXPLAIN ANALYZE, data-modifying CTEs).
_READ_BANNED = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|MERGE|INTO|ANALYZE|ANALYSE|CREATE|ALTER|DROP|TRUNCATE|GRANT|REVOKE"
    r"|COPY|LOCK|EXECUTE|DO|CALL|SET|COMMIT|ROLLBACK|BEGIN|START|SAVEPOINT|PREPARE|DEALLOCATE"
    r"|LISTEN|NOTIFY|REFRESH|CLUSTER|VACUUM|REINDEX|COMMENT|SECURITY)\b",
    re.I,
)
_DOLLAR_TAG = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")
# The single statement a production read travels as (sql/22). The tag is
# random per call and checked against the text, so the text can never close
# the quote early and become top-level SQL.
_READONLY_EXEC = "SELECT * FROM public.rt_readonly_exec({tag}{body}\n{tag})"

_REF_SPLIT = re.compile(r"[,\s;]+")
PROD_ACK_FLAG = "--i-know-this-is-prod"


def _norm_ref(ref: str) -> str:
    return (ref or "").strip().lower()


def _resolve_ref() -> str:
    ref = _norm_ref(os.getenv("SUPABASE_PROJECT_REF", ""))
    if not ref:
        allowed = os.getenv("RT_ALLOWED_SUPABASE_REF", "").strip()
        if "," in allowed:
            # last-wins on a list silently picked whichever ref happened to be
            # listed last; refuse instead of guessing.
            print("refused: RT_ALLOWED_SUPABASE_REF lists several refs; set SUPABASE_PROJECT_REF "
                  "to the one you mean", file=sys.stderr)
            sys.exit(2)
        ref = _norm_ref(allowed)
    if not ref:
        print("refused: no project ref configured — set SUPABASE_PROJECT_REF "
              "(or a single RT_ALLOWED_SUPABASE_REF)", file=sys.stderr)
        sys.exit(2)
    if not _REF_SHAPE.match(ref):
        print(f"refused: {ref!r} is not a Supabase project ref (20 lower-case alphanumerics)",
              file=sys.stderr)
        sys.exit(2)
    return ref


REF = _resolve_ref()
PAT = os.getenv("SUPABASE_ACCESS_TOKEN", "")
SVC = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
BASE = f"https://{REF}.supabase.co"


def _refuse(msg: str) -> None:
    print(f"refused: {msg}", file=sys.stderr)
    sys.exit(2)


def _is_prod(ref: str) -> bool:
    """A ref listed in RT_PROD_SUPABASE_REFS (comma, semicolon or whitespace
    separated), or a worker whose AGENT_NAME is one of the prod names or
    starts with one, is production regardless of what the caller typed."""
    ref = _norm_ref(ref)
    prod_refs = {_norm_ref(r) for r in _REF_SPLIT.split(os.getenv("RT_PROD_SUPABASE_REFS", "")) if r.strip()}
    if ref and ref in prod_refs:
        return True
    agent = os.getenv("AGENT_NAME", "").strip().lower()
    return bool(agent) and any(agent == n or agent.startswith(n) for n in PROD_AGENT_NAMES)


def _confirmed(ref: str, confirm: str | None) -> bool:
    return bool(confirm) and _norm_ref(confirm) == _norm_ref(ref)


def _prod_ack(ref: str, confirm: str | None, prod_ack: bool) -> bool:
    """Both halves of the prod write acknowledgement, or nothing."""
    return _confirmed(ref, confirm) and bool(prod_ack)


def _ident_char(c: str) -> bool:
    """A character that continues a Postgres identifier: letters, digits, `_`,
    `$` and every non-ASCII byte (scan.l's ident_cont)."""
    return c.isalnum() or c in "_$" or ord(c) >= 128


def _scan(sql: str) -> tuple[str, bool, str | None]:
    """The scanner behind strip_sql: (stripped text, clean, ambiguity).

    `ambiguity` names the first construct where this scanner and the Postgres
    lexer could read the text differently — a dollar-quote-looking token
    touching an identifier (`x$a$` is one identifier to Postgres) — or a
    character that must never be in a payload (NUL, bare CR: Postgres ends a
    `--` comment at CR, so an LF-only scan would hide what follows). The
    production read path refuses on any ambiguity; the scanner itself still
    reads the way Postgres does so the split can only get stricter."""
    out: list[str] = []
    i, n = 0, len(sql)
    clean = True
    ambiguity: str | None = None
    if "\x00" in sql:
        ambiguity = "payload contains a NUL byte"
    elif "\r" in sql:
        ambiguity = "payload contains a carriage return (CR); use LF line endings"
    while i < n:
        two = sql[i:i + 2]
        if two == "--":
            # Postgres ends a line comment at either newline character.
            ends = [j for j in (sql.find("\n", i), sql.find("\r", i)) if j >= 0]
            i = min(ends) if ends else n  # the newline itself is kept as a separator
            continue
        if two == "/*":
            depth, i = 1, i + 2
            while i < n and depth:  # block comments nest in Postgres
                if sql[i:i + 2] == "/*":
                    depth, i = depth + 1, i + 2
                elif sql[i:i + 2] == "*/":
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            clean = clean and depth == 0
            out.append(" ")
            continue
        c = sql[i]
        if c in ("'", '"'):
            j = sql.find(c, i + 1)
            out.append(c + c)
            if j < 0:
                clean, i = False, n
            else:
                i = j + 1
            continue
        if c == "$":
            m = _DOLLAR_TAG.match(sql, i)
            if m and i > 0 and _ident_char(sql[i - 1]):
                # `x$a$` continues the identifier for Postgres, so it does for
                # this scanner too — but a client-side scan must not be the
                # thing deciding that on production: flag it.
                ambiguity = ambiguity or (
                    f"dollar-quote-looking token {m.group(0)!r} touches an identifier "
                    f"(column {i}); put a space or newline before it"
                )
                m = None
            if m:
                tag = m.group(0)
                j = sql.find(tag, m.end())
                out.append("''")
                if j < 0:
                    clean, i = False, n
                else:
                    i = j + len(tag)
                continue
        out.append(c)
        i += 1
    return "".join(out), clean, ambiguity


def strip_sql(sql: str) -> tuple[str, bool]:
    """`sql` with comments removed and every quoted literal blanked to ''/"",
    plus whether every literal/comment was terminated.

    Backslashes are never treated as escapes: the only way this scanner can
    disagree with Postgres (E'\\'' strings) ends a literal EARLIER, which can
    only split one statement into more — the refusing direction."""
    text, clean, _ambiguity = _scan(sql)
    return text, clean


def raw_payload_problem(sql: str) -> str | None:
    """Why `sql` may not be judged by the client-side scanner at all (None =
    no objection): a NUL, a bare CR, or a dollar-quote-looking token adjacent
    to an identifier. Each is a place where this scanner and the Postgres
    lexer could disagree about where a statement ends."""
    return _scan(sql or "")[2]


def statements(sql: str) -> list[str]:
    """Non-empty statements of `sql` split on top-level semicolons (comments
    and literals already removed); [] when a literal was left open."""
    text, clean = strip_sql(sql or "")
    if not clean:
        return []
    return [s.strip() for s in text.split(";") if s.strip()]


def is_read_only(sql: str) -> bool:
    """True only for exactly one SELECT / WITH … SELECT / EXPLAIN / SHOW with
    no writing, locking or executing word in it. The READ ONLY transaction
    wrapper is the real guard; this keeps writes from even reaching it."""
    stmts = statements(sql)
    if len(stmts) != 1:
        return False
    s = stmts[0]
    if not _READ_HEAD.match(s) or _READ_BANNED.search(s):
        return False
    if s[:6].upper() != "SELECT" and s[:4].upper() != "SHOW" and not re.search(r"\bSELECT\b", s, re.I):
        return False  # WITH / EXPLAIN must resolve to a SELECT
    return True


def read_only_wrap(sql: str) -> str:
    """The one statement a production read travels as:

        SELECT * FROM public.rt_readonly_exec($ro_<random>$ <text> $ro_<random>$)

    The text is a dollar-quoted literal whose tag is random and verified not
    to occur in it, so it cannot close the quote early: whatever the scanner
    thought, the server sees one function call, and rt_readonly_exec (sql/22)
    turns the transaction read-only before running the text. That setting,
    plus the fact that a COMMIT cannot run inside a function, is what stops
    a write — not the subquery shape, which RETURN QUERY EXECUTE would happily
    run as a statement list; sql/22 also refuses any `;` outside a literal so
    a list never reaches EXECUTE. The client-side transaction wrapper of old
    (BEGIN … read-only … COMMIT) is gone: a smuggled COMMIT ended it."""
    body = (sql or "").strip()
    while body.endswith(";"):  # a trailing `;` inside the subquery is a syntax error
        body = body[:-1].rstrip()
    for _ in range(32):
        tag = f"$ro_{secrets.token_hex(8)}$"
        if tag not in body:
            break
    else:  # 32 collisions of 64 random bits: not happening, but never send anyway
        _refuse("could not pick a dollar-quote tag absent from the payload")
    # The newline before the closing tag lets a trailing `--` comment end
    # before the `) t` the server appends.
    return _READONLY_EXEC.format(tag=tag, body=body)


def prod_payload(ref: str, sql: str, confirm: str | None = None, prod_ack: bool = False) -> str:
    """The text that may go to production `ref`: the caller's SQL verbatim
    under the double acknowledgement, else one rt_readonly_exec call carrying
    a single read. Exits 2 for anything else."""
    if _NEVER_ON_PROD.search(sql or ""):
        _refuse(f"production ref {ref!r} never takes TRUNCATE / DROP TABLE / rt_wipe_all_data from here")
    if _prod_ack(ref, confirm, prod_ack):
        return sql
    if is_destructive(sql):
        _refuse(f"destructive SQL on production ref {ref!r} needs --confirm {ref} {PROD_ACK_FLAG}")
    problem = raw_payload_problem(sql)
    if problem:
        # Belt and braces: the server-side guard holds regardless, but a text
        # the scanner cannot read the way Postgres does is not sent at all.
        _refuse(f"production ref {ref!r}: {problem}")
    if not is_read_only(sql):
        _refuse(f"production ref {ref!r} is read-only from here (one SELECT / WITH … SELECT); "
                f"a write needs --confirm {ref} {PROD_ACK_FLAG}")
    head = statements(sql)[0]
    if _NOT_A_SUBQUERY.match(head):
        word = head.split(None, 1)[0].upper()
        _refuse(f"production ref {ref!r}: {word} cannot run inside rt_readonly_exec (it wraps the text as a "
                f"subquery, and {word} is not one); on production use SELECT / WITH … SELECT, or run it as typed "
                f"with --confirm {ref} {PROD_ACK_FLAG}")
    return read_only_wrap(sql)


def mgmt_query(sql: str, *, confirm: str | None = None, prod_ack: bool = False) -> list:
    """Run SQL via Supabase Management API (needs PAT). On a production ref
    the payload goes through prod_payload() — the guard lives here, not in
    the CLI, so a script importing this function gets it too."""
    if not PAT:
        raise RuntimeError("SUPABASE_ACCESS_TOKEN not set")
    if "\x00" in (sql or ""):
        _refuse("payload contains a NUL byte")  # never valid SQL; a NUL only ever hides something
    if _is_prod(REF):
        sql = prod_payload(REF, sql, confirm, prod_ack)
    req = urllib.request.Request(
        f"https://api.supabase.com/v1/projects/{REF}/database/query",
        data=json.dumps({"query": sql}).encode(),
        headers={"Authorization": f"Bearer {PAT}",
                 "Content-Type": "application/json",
                 "User-Agent": "phone-pal-admin/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - fixed https Supabase management URL
        return json.loads(r.read())


def rest_rpc(fn: str, body: dict | None = None) -> dict:
    """Call a public PostgREST RPC (uses service role key)."""
    body = body or {}
    if not SVC:
        raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY not set")
    req = urllib.request.Request(  # noqa: S310 - BASE is a fixed https Supabase URL
        f"{BASE}/rest/v1/rpc/{fn}",
        data=json.dumps(body).encode(),
        headers={"apikey": SVC, "Authorization": f"Bearer {SVC}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310 - BASE is a fixed https Supabase URL
        return json.loads(r.read())


def is_destructive(sql: str) -> bool:
    return bool(_DESTRUCTIVE.search(sql or ""))


def guard_destructive(ref: str, sql: str, confirm: str | None, prod_ack: bool = False) -> None:
    """Exit 2 unless destructive `sql` may run on `ref`: the wipe class never
    on prod, other destructive SQL on prod only with --confirm <ref> AND
    --i-know-this-is-prod, and with --confirm <ref> elsewhere. Non-destructive
    SQL passes (mgmt_query still applies the prod read-only gate)."""
    prod = _is_prod(ref)
    if prod and _NEVER_ON_PROD.search(sql or ""):
        _refuse(f"production ref {ref!r} never takes TRUNCATE / DROP TABLE / rt_wipe_all_data from here")
    if not is_destructive(sql):
        return
    if prod:
        if not _prod_ack(ref, confirm, prod_ack):
            _refuse(f"destructive SQL on production ref {ref!r} needs --confirm {ref} {PROD_ACK_FLAG}")
        return
    if not _confirmed(ref, confirm):
        _refuse(f"destructive SQL on {ref!r} needs --confirm {ref}")


def wipe_db(ref: str, confirm: str | None) -> dict:
    """Truncate all rt.* tables on `ref`. Returns counts of rows wiped.

    Refuses production outright (RuntimeError) and demands `confirm == ref`
    (ValueError) so a wipe can never be one typo away from the wrong database.
    """
    if _is_prod(ref):
        raise RuntimeError(f"refused: production ref {ref!r} is never wiped")
    if not _confirmed(ref, confirm):
        raise ValueError(f"wipe of {ref!r} needs --confirm {ref}")
    return rest_rpc("rt_wipe_all_data")


def ledger_sql(basename: str, sql: str) -> str:
    """Wrap a migration so it is recorded in public.schema_migrations exactly
    the way scripts/migrate.py records it (version + sha256 checksum), so the
    two tools read one ledger instead of drifting past each other."""
    if not _MIGRATION_NAME.match(basename):
        raise ValueError(f"refused: {basename!r} is not a migration name (NN-lower-case.sql)")
    version = basename.replace("'", "''")  # the regex already forbids quotes; belt and braces
    checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
    return (
        "CREATE TABLE IF NOT EXISTS public.schema_migrations ("
        "version TEXT PRIMARY KEY, checksum TEXT NOT NULL, "
        "applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW());\n"
        f"{sql}\n"
        "INSERT INTO public.schema_migrations (version, checksum, applied_at) "
        f"VALUES ('{version}', '{checksum}', NOW()) "
        "ON CONFLICT (version) DO UPDATE SET checksum = EXCLUDED.checksum, applied_at = NOW();"
    )


def _take_value(args: list[str], flag: str) -> str | None:
    """Remove `flag VALUE` from args and return VALUE; a bare flag is a usage error."""
    if flag not in args:
        return None
    idx = args.index(flag)
    if idx + 1 >= len(args) or args[idx + 1].startswith("--"):
        print(f"usage: {flag} needs a value", file=sys.stderr)
        sys.exit(2)
    value = args[idx + 1]
    del args[idx:idx + 2]
    return value


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)

    confirm = _take_value(args, "--confirm")
    path = _take_value(args, "--file")
    prod_ack = PROD_ACK_FLAG in args
    while PROD_ACK_FLAG in args:
        args.remove(PROD_ACK_FLAG)

    if "--wipe" in args:
        try:
            result = wipe_db(REF, confirm)
        except RuntimeError as e:
            print(f"refused: {e}", file=sys.stderr)
            sys.exit(2)
        print(f"✅ Database wiped: {result}")
        sys.exit(0)

    if path is not None:
        basename = os.path.basename(path)
        if not _MIGRATION_NAME.match(basename):
            raise ValueError(f"refused: {basename!r} is not a migration name (NN-lower-case.sql)")
        sql = open(path).read()
        guard_destructive(REF, sql, confirm, prod_ack)
        result = mgmt_query(ledger_sql(basename, sql), confirm=confirm, prod_ack=prod_ack)
    else:
        sql = " ".join(args)
        guard_destructive(REF, sql, confirm, prod_ack)
        result = mgmt_query(sql, confirm=confirm, prod_ack=prod_ack)

    print(json.dumps(result, indent=2))
