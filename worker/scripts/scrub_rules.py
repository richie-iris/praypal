#!/usr/bin/env python3
"""scrub_rules.py — re-check stored caller rules against today's rt_shield.

Walks rpc/rt_get_all_callers and re-validates every caller_rules /
persona_directives scalar and every skills-category registry value with
rt_shield.rule_text_allowed — the same check the post-call worker applies
before a rule is stored. Anything the current shield refuses is listed; with
--apply it is nulled (scalars via rt_set_*, skill keys via
rt_add_schema_entry {key: null}, which the upsert merges over the old value).

Dry run is the default and sends only reads. A production ref (sql_push's
rule: RT_PROD_SUPABASE_REFS or a prod AGENT_NAME) is refused, dry run
included, unless --confirm <ref> is retyped.

Usage:
    python scripts/scrub_rules.py                       # dry run
    python scripts/scrub_rules.py --apply               # write
    python scripts/scrub_rules.py --apply --confirm <ref>   # on a production ref
    python scripts/scrub_rules.py --env-file .env.dev
"""
from __future__ import annotations

import json
import sys
import urllib.parse
import os
from pathlib import Path

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

SKILL_CATS = ("skills", "routines", "custom_skills")
_SCALARS = (("caller_rules", "rt_set_caller_rules", "p_rules"),
            ("persona_directives", "rt_set_persona_directives", "p_directives"))


def _take_value(args: list[str], flag: str) -> str | None:
    if flag not in args:
        return None
    idx = args.index(flag)
    if idx + 1 >= len(args) or args[idx + 1].startswith("--"):
        print(f"usage: {flag} needs a value", file=sys.stderr)
        sys.exit(2)
    value = args[idx + 1]
    del args[idx:idx + 2]
    return value


def _url_ref() -> str:
    """Project ref of SUPABASE_URL's host — the project rt_prefs will write to."""
    try:
        host = (urllib.parse.urlsplit(os.getenv("SUPABASE_URL", "")).hostname or "").lower()
    except ValueError:
        host = ""
    return host.removesuffix(".supabase.co") if host.endswith(".supabase.co") else ""


def plan_for(h: str, caller: dict, schemas: list[dict], rule_text_allowed) -> list[dict]:
    """Writes that would null every value the shield refuses; [] when clean."""
    out: list[dict] = []
    for field, rpc, param in _SCALARS:
        val = caller.get(field)
        if not isinstance(val, str) or not val.strip():
            continue
        ok, why = rule_text_allowed(val)
        if not ok:
            out.append({"hash": h, "what": field, "why": why, "rpc": rpc, "body": {"p_hash": h, param: None}})
    for e in schemas or ():
        cat = (e.get("category") or "").lower()
        if cat not in SKILL_CATS:
            continue
        raw = e.get("data_summary")
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            data = raw
        if isinstance(data, dict):
            bad = {}
            for key, val in data.items():
                text = val if isinstance(val, str) else json.dumps(val)
                ok, why = rule_text_allowed(text)
                if not ok:
                    bad[key] = why
            if bad:
                out.append({"hash": h, "what": f"{cat}:{','.join(sorted(bad))}", "why": "; ".join(bad.values()),
                            "rpc": "rt_add_schema_entry",
                            "body": {"p_hash": h, "p_table": None, "p_cat": cat,
                                     "p_summary": json.dumps(dict.fromkeys(bad))}})
        elif isinstance(raw, str) and raw.strip():
            ok, why = rule_text_allowed(raw)
            if not ok:  # a non-object row is replaced whole by the upsert
                out.append({"hash": h, "what": cat, "why": why, "rpc": "rt_add_schema_entry",
                            "body": {"p_hash": h, "p_table": None, "p_cat": cat, "p_summary": "{}"}})
    return out


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    confirm = _take_value(args, "--confirm")
    env_file = _take_value(args, "--env-file")
    apply = "--apply" in args
    if env_file:
        load_dotenv(env_file)
    load_dotenv(ROOT / ".env.local")
    load_dotenv(ROOT / ".env")

    import sql_push  # after the env is loaded: it resolves the ref at import
    import rt_prefs
    import rt_shield

    ref = sql_push.REF
    url_ref = _url_ref()
    if url_ref != ref:
        # rt_prefs writes to SUPABASE_URL's project; the prod check is on REF.
        # Two different projects means the check would not cover the write.
        print(f"refused: SUPABASE_URL points at {url_ref or '?'!r} but the project ref is {ref!r}",
              file=sys.stderr)
        return 2
    if sql_push._is_prod(ref) and not sql_push._confirmed(ref, confirm):
        print(f"refused: {ref!r} is a production ref; scrub_rules needs --confirm {ref}", file=sys.stderr)
        return 2

    callers = rt_prefs._req("POST", "rpc/rt_get_all_callers", {}) or []
    if not isinstance(callers, list):
        print("could not list callers", file=sys.stderr)
        return 1
    plan: list[dict] = []
    for c in callers:
        h = c.get("phone_hash") if isinstance(c, dict) else None
        if not h:
            continue
        bundle = rt_prefs._req("POST", "rpc/rt_get_caller_full_bundle", {"p_hash": h}) or {}
        caller = bundle.get("caller") if isinstance(bundle, dict) else None
        schemas = bundle.get("schemas") if isinstance(bundle, dict) else None
        plan.extend(plan_for(h, caller if isinstance(caller, dict) else c, schemas or [], rt_shield.rule_text_allowed))

    verb = "nulling" if apply else "would null"
    for item in plan:
        print(f"  {item['hash'][:12]}… {verb} {item['what']}: {item['why']}")
    if not plan:
        print(f"{len(callers)} caller(s) checked; nothing to scrub.")
        return 0
    if not apply:
        print(f"{len(callers)} caller(s) checked; {len(plan)} value(s) would be nulled. Re-run with --apply.")
        return 0
    failed = 0
    for item in plan:
        try:
            rt_prefs._req("POST", f"rpc/{item['rpc']}", item["body"])
        except Exception as e:  # noqa: BLE001 - report each row; the tally decides the exit code
            failed += 1
            print(f"  [❌] {item['hash'][:12]}… {item['what']}: {e}", file=sys.stderr)
    print(f"{len(callers)} caller(s) checked; {len(plan) - failed}/{len(plan)} value(s) nulled.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
