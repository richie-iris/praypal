#!/usr/bin/env python3
"""check_observability.py — prove the observability contract, or fail.

Reads obs_contract.py and checks the source: is every promised event actually
emitted somewhere, and is every module actually exercised by the harness?

This exists because "we log everything" is unfalsifiable. A client asking "how
do you know?" deserves a command they can run themselves, not our assurance.
Run it in CI and the claim stops being a promise and becomes a build gate.

    python scripts/check_observability.py            # full report
    python scripts/check_observability.py --strict   # exit 1 on ANY gap
    python scripts/check_observability.py --json     # machine readable

Exits 1 when a CRITICAL event has no emit site, or a module has no test.
"""
from __future__ import annotations

import ast
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import obs_contract as C  # noqa: E402

_SRC = {os.path.basename(p)[:-3]: open(p).read()
        for p in glob.glob(os.path.join(ROOT, "*.py"))}
_HARNESS = "\n".join(open(p).read() for p in
                     glob.glob(os.path.join(ROOT, "harness", "*.py")))


def emitted_events() -> dict[str, list[str]]:
    """event name -> modules that emit it.

    Matches the literal first argument of obs.event/debug/warn/error/critical
    and obs.span, plus rt_trace.event(kind, name) pairs. A dynamically built
    event name will read as missing — which is correct: an event nobody can
    grep for is an event nobody can alert on.
    """
    found: dict[str, list[str]] = {}
    call_names = {"event", "debug", "warn", "error", "critical", "span"}
    for mod, src in _SRC.items():
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute) and fn.attr in call_names):
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant):
                continue
            val = node.args[0].value
            if not isinstance(val, str):
                continue
            for name in ({val, f"{val}.ok", f"{val}.failed", f"{val}.start"}
                         if fn.attr == "span" else {val}):
                found.setdefault(name, []).append(mod)
    return found


def tested_modules() -> set[str]:
    """Modules the harness actually references."""
    return {m for m in _SRC if re.search(rf"\b{re.escape(m)}\b", _HARNESS)}


def silent_handlers() -> list[tuple[str, int]]:
    """except: blocks that log nothing. Each is a failure that looks like success."""
    out = []
    for mod, src in _SRC.items():
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                dumped = " ".join(ast.dump(s) for s in node.body)
                if not any(k in dumped.lower() for k in ("print", "log", "obs", "caught", "trace")):
                    out.append((mod, node.lineno))
    return sorted(out)


def main() -> None:
    strict = "--strict" in sys.argv
    as_json = "--json" in sys.argv

    emitted = emitted_events()
    tested = tested_modules()
    silent = silent_handlers()

    missing, missing_critical = [], []
    for ev in C.ALL_EVENTS:
        if ev.name not in emitted:
            missing.append(ev)
            if ev.critical:
                missing_critical.append(ev)

    untested = [m for m in C.MUST_BE_TESTED if m not in tested]

    total = len(C.ALL_EVENTS)
    covered = total - len(missing)
    ev_pct = (covered / total * 100) if total else 100.0
    mod_pct = ((len(C.MUST_BE_TESTED) - len(untested)) / len(C.MUST_BE_TESTED) * 100)

    if as_json:
        print(json.dumps({
            "events_total": total, "events_covered": covered,
            "events_pct": round(ev_pct, 1),
            "critical_missing": [e.name for e in missing_critical],
            "missing": [e.name for e in missing],
            "modules_pct": round(mod_pct, 1), "modules_untested": untested,
            "silent_handlers": len(silent),
        }, indent=2))
    else:
        print("\n=== OBSERVABILITY CONTRACT ===")
        print(C.summary())
        print(f"\nEVENT COVERAGE   {covered}/{total}  ({ev_pct:.1f}%)")
        for group, evs in C.GROUPS.items():
            gap = [e.name for e in evs if e.name not in emitted]
            mark = "ok" if not gap else f"{len(evs) - len(gap)}/{len(evs)}"
            print(f"  {group:<11} {mark}")
            for n in gap:
                crit = " [CRITICAL]" if any(e.name == n and e.critical for e in evs) else ""
                print(f"       missing: {n}{crit}")
        print(f"\nHARNESS COVERAGE {len(C.MUST_BE_TESTED) - len(untested)}/"
              f"{len(C.MUST_BE_TESTED)}  ({mod_pct:.1f}%)")
        for m in untested:
            print(f"       untested: {m}")
        print(f"\nSILENT HANDLERS  {len(silent)}  (exceptions that log nothing)")
        for mod, line in silent[:12]:
            print(f"       {mod}.py:{line}")
        if len(silent) > 12:
            print(f"       ... and {len(silent) - 12} more")

    failed = bool(missing_critical) or bool(untested) or (strict and (missing or silent))
    if failed:
        print(f"\nFAIL — {len(missing_critical)} critical events unemitted, "
              f"{len(untested)} modules untested", file=sys.stderr)
        sys.exit(1)
    print("\nPASS — contract satisfied")


if __name__ == "__main__":
    main()
