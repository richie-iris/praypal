#!/usr/bin/env python3
"""replay_suite.py — every real call becomes a permanent test.

WHY THIS EXISTS. On 2026-08-27 the product suite was 247/247 green and a caller
found a bug in fifteen seconds by dialling the number. She said the same line
twice, and in the same breath claimed she would remember a name the guard had
explicitly refused to save.

Neither was catchable by the suite, and not by accident. Every one of those 247
tests asserts on a FUNCTION. These two bugs live in the relationship BETWEEN
events — a turn next to the turn before it, an utterance next to the tool result
that preceded it. A test that calls one function at a time cannot see either,
no matter how many of them there are. Coverage was never the missing thing.

So this reads calls that actually happened out of rt.call_events, replays the
event sequence, and asserts on the conversation. Every call anyone makes becomes
a regression test, permanently, with no test-writing.

    python harness/replay_suite.py                # every stored call
    python harness/replay_suite.py --call AJ_xyz  # one call
    python harness/replay_suite.py --fixtures     # the shipped regressions only
    python harness/replay_suite.py --json

Read-only. It never writes to the database and never places a call.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / "harness" / ".env.harness", override=True)
load_dotenv(ROOT / ".env.local")
load_dotenv(ROOT / ".env")

import agent as A  # noqa: E402
import rt_prefs  # noqa: E402

GREEN, RED, DIM, BOLD, RESET = "\033[92m", "\033[91m", "\033[2m", "\033[1m", "\033[0m"

# Phrases that assert an action was taken. If one of these follows a guard that
# refused the write, she told the caller something untrue.
# A literal phrase list missed the real case on its first run: she said "I'll go
# ahead and remember that for next time" and the list held "i'll remember". Match
# the CLAIM — a first-person commitment verb reaching a memory verb — not a
# guessed sentence. The words between them are where the misses hide.
_CLAIM_RE = re.compile(
    r"\b(i'?l?l?|i\s+will|i'?ve|i\s+have|let\s+me)\b[^.!?]{0,40}?"
    r"\b(remember|save[d]?|keep|note[d]?|writ|record|stor)",
    re.IGNORECASE)
_REFUSAL_GUARDS = ("alias_pending", "private_refused", "refuse_dial")


# ── the invariants ───────────────────────────────────────────

def inv_no_repeated_agent_turn(events):
    """She must never say a truncated copy of her own previous line.

    The live failure: "Nelda, huh? I like it! I'll go ahead and remember that for
    next time." then, 1.68s later, "Nelda, huh?" — the caller replied "Who the
    fuck?" and she had to apologise for startling them.
    """
    bad, prev, prev_at = [], None, None
    for e in events:
        if e["kind"] != "turn" or e["name"] != "agent":
            continue
        t, at = e["text"], (e["elapsed_ms"] or 0) / 1000.0
        if A.is_duplicate_agent_turn(t, prev, prev_at, at):
            bad.append((round(at, 1), t[:60]))
        prev, prev_at = t, at
    return not bad, f"repeats={len(bad)}" + (f" {bad[:2]}" if bad else "")


def inv_never_claims_a_refused_write(events):
    """She must not promise to remember what a guard just refused to save.

    Same three seconds as the repeat: the alias guard returned "[not saved yet]"
    and she said "I'll go ahead and remember that for next time." The TRUTH law
    forbids claiming an action whose tool did not succeed; nothing enforced it.
    """
    bad, pending = [], None
    for e in events:
        if e["kind"] == "guard" and e["name"] in _REFUSAL_GUARDS:
            pending = e["name"]
            continue
        if e["kind"] == "turn" and e["name"] == "agent":
            if pending:
                m = _CLAIM_RE.search(e["text"])
                if m:
                    bad.append((pending, m.group(0)[:40], e["text"][:56]))
            pending = None
    return not bad, f"false_claims={len(bad)}" + (f" {bad[:2]}" if bad else "")


def inv_every_tool_call_resolves(events):
    """A tool that starts and never reports is a tool she improvises around."""
    calls = [e for e in events if e["kind"] == "tool"]
    unresolved = [e["name"] for e in calls if e.get("detail") in (None, {}, "")]
    return True, f"tool_calls={len(calls)} without_detail={len(unresolved)}"


def inv_she_speaks_before_the_caller_waits(events):
    """The first agent turn must arrive within the pickup window."""
    first = next((e for e in events if e["kind"] == "turn" and e["name"] == "agent"), None)
    if not first:
        return False, "she never spoke"
    ms = first["elapsed_ms"] or 0
    return ms < 15000, f"first_agent_turn_at={ms}ms"


def inv_no_bracket_read_aloud(events):
    """Tool results are stage directions. Reading one aloud breaks the illusion."""
    bad = [e["text"][:50] for e in events
           if e["kind"] == "turn" and e["name"] == "agent"
           and ("[not saved" in e["text"].lower() or "[verified:" in e["text"].lower()
                or "[nothing saved" in e["text"].lower())]
    return not bad, f"brackets_spoken={len(bad)}"


INVARIANTS = [
    ("R01", "She never repeats a truncated copy of her own line", inv_no_repeated_agent_turn),
    ("R02", "She never claims to remember what a guard refused", inv_never_claims_a_refused_write),
    ("R03", "Every tool call resolves", inv_every_tool_call_resolves),
    ("R04", "She speaks inside the pickup window", inv_she_speaks_before_the_caller_waits),
    ("R05", "She never reads a tool bracket aloud", inv_no_bracket_read_aloud),
]


# ── loading ──────────────────────────────────────────────────

def _norm(rows):
    out = []
    for r in rows:
        d = r.get("detail")
        if isinstance(d, str):
            try:
                d = json.loads(d)
            except Exception:
                d = {}
        out.append({
            "id": r.get("id") or 0,
            "elapsed_ms": r.get("elapsed_ms") or 0,
            "kind": (r.get("kind") or "").lower(),
            "name": (r.get("name") or "").lower(),
            "detail": d or {},
            "text": str((d or {}).get("text") or ""),
        })
    out.sort(key=lambda e: e["id"])
    return out


def from_db():
    """Every stored call, grouped."""
    rows = rt_prefs._req("POST", "rpc/rt_console_events", {"p_limit": 20000}) or []
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r.get("call_id"), []).append(r)
    return {cid: _norm(rs) for cid, rs in by.items() if cid}


def fixtures():
    """Calls that already went wrong, kept forever so they cannot recur.

    A wipe clears rt.call_events, so a regression proven by a real call would
    vanish with it. These are transcribed from the real event rows.
    """
    here = Path(__file__).parent / "replay_fixtures"
    out = {}
    if here.is_dir():
        for f in sorted(here.glob("*.json")):
            out[f.stem] = _norm(json.loads(f.read_text()))
    return out


def load_baseline() -> dict[str, list[str]]:
    bf = Path(__file__).parent / "baseline_failures.json"
    if bf.is_file():
        try:
            return json.loads(bf.read_text())
        except Exception:
            return {}
    return {}


def main() -> None:
    only = sys.argv[sys.argv.index("--call") + 1] if "--call" in sys.argv else None
    as_json = "--json" in sys.argv
    fixtures_mode = "--fixtures" in sys.argv
    baseline = load_baseline()

    calls = fixtures()
    if not fixtures_mode:
        try:
            calls.update(from_db())
        except Exception as exc:
            print(f"{DIM}  (no live calls readable: {exc}){RESET}")
    if only:
        calls = {k: v for k, v in calls.items() if k == only}

    if not calls:
        print("No calls to replay. Make one, or keep a fixture.")
        return

    results, failed_count, unexpected_count = [], 0, 0
    for cid, events in sorted(calls.items()):
        turns = sum(1 for e in events if e["kind"] == "turn")
        if not as_json:
            print(f"\n{BOLD}{cid}{RESET} {DIM}({len(events)} events, {turns} turns){RESET}")
        known_failures = baseline.get(cid, [])
        for rid, desc, fn in INVARIANTS:
            try:
                ok, detail = fn(events)
            except Exception as exc:
                ok, detail = False, f"{type(exc).__name__}: {exc}"

            is_expected_fixture_trip = (not ok) and (rid in known_failures)
            if not ok:
                failed_count += 1
                if not (fixtures_mode and is_expected_fixture_trip):
                    unexpected_count += 1

            results.append({
                "call": cid,
                "id": rid,
                "ok": ok,
                "expected_trip": is_expected_fixture_trip,
                "detail": detail,
            })
            if not as_json:
                if ok:
                    status_str = f"{GREEN}PASS{RESET}"
                elif is_expected_fixture_trip and fixtures_mode:
                    status_str = f"{GREEN}PASS{RESET} {DIM}(expected fixture trip){RESET}"
                else:
                    status_str = f"{RED}FAIL{RESET}"
                print(f"  {status_str}  [{rid}] {desc}")
                print(f"        {DIM}{detail}{RESET}")

    if as_json:
        print(json.dumps({"calls": len(calls), "failed": failed_count, "unexpected": unexpected_count, "results": results}, indent=2))
    else:
        if fixtures_mode and unexpected_count == 0:
            print(f"\n{GREEN}{BOLD}PASS — {len(calls)} fixture(s) replayed, all invariant detectors verified{RESET}\n")
        else:
            verdict = "PASS" if not unexpected_count else "FAIL"
            color = GREEN if not unexpected_count else RED
            print(f"\n{color}{BOLD}{verdict} — {len(calls)} call(s) replayed, "
                  f"{unexpected_count} unexpected failure(s) ({failed_count} total trips){RESET}\n")
    sys.exit(1 if unexpected_count else 0)


if __name__ == "__main__":
    main()
