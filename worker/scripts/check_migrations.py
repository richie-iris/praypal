#!/usr/bin/env python3
"""check_migrations.py — is a lane's database actually on the migrations in sql/?

Read-only. Makes no writes and touches no caller data.

Does NOT trust public.schema_migrations. That table only gets a row when a file
goes in through `sql_push.py --file`; anything applied through the Supabase SQL
editor leaves no trace, so the ledger under-reports and reads as drift that isn't
there. This asks the database what it actually has instead: every migration
declares the functions it creates, and a migration counts as applied only when
all of them exist.

When SUPABASE_ACCESS_TOKEN is available (lane file or environment) it also
reads pg_proc through the Management API and fails the lane if any SECURITY
DEFINER public.rt_* function is missing its search_path pin (sql/18): PostgREST
cannot see proconfig, so without the token that check is skipped with a note.

Usage:
    python scripts/check_migrations.py              # every worker/.env.* lane on disk
    python scripts/check_migrations.py dev test     # named lanes (worker/.env.dev, .env.test)
    python scripts/check_migrations.py --env-file /path/to/lane.env   # that file's values
    python scripts/check_migrations.py --env        # the process environment's values

--env-file and --env read the lane's values from where the caller put them —
a provisioning script's private temp file or its exported environment — and
never from a worker/.env.* file, so a developer's .env.local cannot answer for
a lane it is not (#20).

Exits 1 if any lane is behind or unreachable, so it can gate a deploy, and 2
when NO lane was checked (nothing selected, or every selection lacked a project
ref / service key): a check that ran against nothing is not a pass.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
LANES = {"dev": ".env.dev", "test": ".env.test", "local": ".env.local"}

# The trailing \( is what keeps a non-public schema out. Without it,
# "CREATE OR REPLACE FUNCTION kb.search(" matched with the optional public.
# absent and captured "kb" — a function name that cannot exist — so sql/23
# reported BEHIND on a lane where it had applied cleanly, and stand-up-lane.sh
# stopped at the database gate (2026-09-03, standing up trial-pal). Only
# public functions are PostgREST RPCs, and only those can be checked here;
# kb.search and its siblings are called through their public rt_kb_* wrappers.
_CREATE_FN = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+(?:public\.)?([a-z0-9_]+)\s*\(",
    re.IGNORECASE,
)


def declared_functions() -> dict[str, set[str]]:
    """migration filename -> the public RPCs it defines."""
    out: dict[str, set[str]] = {}
    for path in sorted((ROOT / "sql").glob("*.sql")):
        fns = set(_CREATE_FN.findall(path.read_text()))
        if fns:
            out[path.name] = fns
    return out


def live_rpcs(ref: str, key: str) -> set[str]:
    """Every RPC PostgREST currently exposes on this project."""
    req = urllib.request.Request(
        f"https://{ref}.supabase.co/rest/v1/",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Accept": "application/openapi+json",
        },
    )
    with urllib.request.urlopen(req, timeout=25) as resp:  # noqa: S310 - fixed https Supabase URL
        spec = json.loads(resp.read())
    return {p[len("/rpc/"):] for p in spec.get("paths", {}) if p.startswith("/rpc/")}


# A SECURITY DEFINER function whose proconfig carries no search_path resolves
# names through the caller's path — the hole sql/18 closed. proconfig is NULL
# when nothing is SET, hence the COALESCE.
UNPINNED_SECDEF_SQL = """
SELECT p.oid::regprocedure::text AS fn
  FROM pg_proc p
  JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname = 'public'
   AND p.proname LIKE 'rt\\_%'
   AND p.prosecdef
   AND NOT EXISTS (SELECT 1 FROM unnest(COALESCE(p.proconfig, '{}'::text[])) AS c
                    WHERE c LIKE 'search_path=%')
 ORDER BY 1
"""


def secdef_without_search_path(ref: str, pat: str) -> list[str]:
    """SECURITY DEFINER public.rt_* functions with no search_path in proconfig."""
    req = urllib.request.Request(
        f"https://api.supabase.com/v1/projects/{ref}/database/query",
        data=json.dumps({"query": UNPINNED_SECDEF_SQL}).encode(),
        headers={
            "Authorization": f"Bearer {pat}",
            "Content-Type": "application/json",
            "User-Agent": "phone-pal-admin/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=25) as resp:  # noqa: S310 - fixed https Supabase management URL
        rows = json.loads(resp.read())
    return sorted(r["fn"] for r in rows if isinstance(r, dict) and r.get("fn"))


_URL_REF = re.compile(r"^https?://([a-z0-9]{20})\.supabase\.co/?$", re.I)


def ref_from_url(url: str | None) -> str:
    """The project ref carried by a SUPABASE_URL (https://<ref>.supabase.co), or ''."""
    m = _URL_REF.match((url or "").strip())
    return m.group(1).lower() if m else ""


def check(lane: str, env_file: str, expected: dict[str, set[str]], cfg: dict | None = None) -> bool | None:
    """True = up to date, False = behind / unreachable, None = not checked
    (no project ref or service key in the source). `cfg` is the lane's values
    when the caller already has them (--env-file, --env); otherwise they are
    read from worker/<env_file>."""
    named_source = cfg is not None
    if cfg is None:
        cfg = dotenv_values(ROOT / env_file)
    ref = (cfg.get("SUPABASE_PROJECT_REF") or "").strip().lower()
    url_ref = ref_from_url(cfg.get("SUPABASE_URL"))
    key = (cfg.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    # A worker/.env.* lane may borrow the Management API token from the
    # environment (it only enables the pin check, it never picks the target);
    # a named source (--env-file / --env) is the ONLY place its values come from.
    pat = (cfg.get("SUPABASE_ACCESS_TOKEN") or ("" if named_source else os.getenv("SUPABASE_ACCESS_TOKEN")) or "").strip()

    print(f"\n=== lane={lane}  project={ref or url_ref or '(unset)'} ===")
    if ref and url_ref and ref != url_ref:
        # Two names for the target that disagree: check neither.
        print(f"  REFUSED — SUPABASE_PROJECT_REF {ref!r} and SUPABASE_URL ref {url_ref!r} disagree in {env_file}")
        return False
    ref = ref or url_ref
    if not ref or not key:
        print(f"  skipped — {env_file} has no project ref / service key")
        return None

    try:
        have = live_rpcs(ref, key)
    except urllib.error.HTTPError as exc:
        print(f"  UNREACHABLE — HTTP {exc.code}")
        return False
    except Exception as exc:
        print(f"  UNREACHABLE — {exc}")
        return False

    behind = False
    for name, fns in expected.items():
        missing = sorted(fns - have)
        if missing:
            behind = True
            print(f"  {name:<44} BEHIND — missing {', '.join(missing)}")
        else:
            print(f"  {name:<44} ok")

    if not pat:
        print("  search_path pins: skipped — no SUPABASE_ACCESS_TOKEN (pg_proc needs the Management API)")
    else:
        try:
            unpinned = secdef_without_search_path(ref, pat)
        except Exception as exc:  # noqa: BLE001 - a check that cannot run must not read as a pass
            behind = True
            print(f"  search_path pins: UNREACHABLE — {exc}")
        else:
            if unpinned:
                behind = True
                print(f"  search_path pins: BEHIND — SECURITY DEFINER without search_path: {', '.join(unpinned)}")
            else:
                print("  search_path pins: ok")

    print(f"  --> {'BEHIND' if behind else 'up to date'}")
    return not behind


def _parse_argv(argv: list[str]) -> tuple[list[str], list[Path], bool]:
    """(lane names, --env-file paths, --env given). Options are parsed by
    hand so a typo is an error rather than a lane name."""
    lanes: list[str] = []
    files: list[Path] = []
    use_env = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--env-file" or a.startswith("--env-file="):
            if "=" in a:
                value = a.split("=", 1)[1]
            elif i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                value = argv[i + 1]
                i += 1
            else:
                print("usage: --env-file needs a path", file=sys.stderr)
                sys.exit(2)
            files.append(Path(value))
        elif a == "--env":
            use_env = True
        elif a.startswith("--"):
            print(f"unknown option {a!r} — lanes are positional; --env-file <path> / --env read elsewhere",
                  file=sys.stderr)
            sys.exit(2)
        else:
            lanes.append(a)
        i += 1
    return lanes, files, use_env


def main() -> None:
    expected = declared_functions()
    if not expected:
        sys.exit("no migrations found under sql/ — run from the repo, not elsewhere")

    lanes, files, use_env = _parse_argv(sys.argv[1:])
    if not lanes and not files and not use_env:
        lanes = [l for l in LANES if (ROOT / LANES[l]).exists()]
    unknown = [l for l in lanes if l not in LANES]
    if unknown:
        sys.exit(f"unknown lane(s): {', '.join(unknown)} — pick from {', '.join(LANES)}")
    for f in files:
        if not f.is_file():
            print(f"refused: --env-file {f} does not exist", file=sys.stderr)
            sys.exit(2)

    print(f"{len(expected)} migrations declare public RPCs, out of "
          f"{len(list((ROOT / 'sql').glob('*.sql')))} files in sql/")

    results: list[bool | None] = []
    for lane in lanes:
        results.append(check(lane, LANES[lane], expected))
    for f in files:
        # dotenv_values reads THAT file; ROOT never enters into it.
        results.append(check(f"env-file:{f.name}", str(f), expected, cfg=dict(dotenv_values(f))))
    if use_env:
        results.append(check("env", "the process environment", expected, cfg=dict(os.environ)))
    print()
    checked = [r for r in results if r is not None]
    if not checked:
        # Nothing selected, or every selection lacked a ref / key: a check that
        # ran against nothing must not read as a pass.
        print("no lane was checked — name a lane, pass --env-file <path>, or --env with the "
              "lane's SUPABASE_PROJECT_REF and SUPABASE_SERVICE_ROLE_KEY set", file=sys.stderr)
        sys.exit(2)
    sys.exit(0 if all(checked) else 1)


if __name__ == "__main__":
    main()
