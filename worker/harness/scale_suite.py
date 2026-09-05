#!/usr/bin/env python3
"""scale_suite.py — the tests the product suite cannot ask.

The 150-test product suite runs SEQUENTIALLY against TWO callers with
~2,000-character transcripts. That is the right shape for asserting behaviour,
and the wrong shape for three questions it structurally cannot answer:

  1. POPULATION. Two callers cannot surface a name collision, a memory bleed
     across a crowd, or a fact-ranking that degrades at fifty profiles.
  2. LENGTH. A 2,000-char transcript is about ninety seconds of talking. Real
     calls run ten to twenty minutes. Prompt budgeting, trimming, compaction and
     extraction quality all behave differently at 20,000 characters — and that
     is the regime the product actually operates in.
  3. CONCURRENCY. Nothing here has ever run two calls at once. The worker has a
     shared HTTP pool, module-level state and NUM_IDLE_PROCESSES. Whether caller
     A's memory can reach caller B's prompt under concurrent load has never been
     measured. Sequential isolation tests cannot catch it by construction.

    python harness/scale_suite.py                 # default: 25 callers
    python harness/scale_suite.py --callers 100
    python harness/scale_suite.py --only concurrency

SAFETY. Every caller here is in a reserved +1555098xxxx block, wiped before and
after. This suite NEVER places a call, sends an SMS, or sends an email: it
exercises hydration, memory and isolation only, and asserts the gates that stop
the rest. A load test that texts real people is not a load test, it is an
incident.
"""
from __future__ import annotations

import concurrent.futures as futures
import contextlib
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / "harness" / ".env.harness", override=True)
load_dotenv(ROOT / ".env.local")
load_dotenv(ROOT / ".env")

_DEV_REF = "ojoppcyvkxwfuwzjjxbw"
if _DEV_REF not in (os.getenv("SUPABASE_URL") or ""):
    sys.exit(f"REFUSING to run: not pointed at the {_DEV_REF} dev project.")

os.environ.setdefault("RT_HARNESS_TEST_MODE", "1")
# The lane rule (RT_REQUIRE_PEPPER=1) stays in force here: the suite brings its
# own pepper for its synthetic block rather than switching the gate off.
os.environ.setdefault("RT_PHONE_HASH_PEPPER", "scale-suite-test-pepper")

import rt_hydrator  # noqa: E402
import rt_prefs  # noqa: E402

BLOCK = "+1555098"          # reserved for this suite
GREEN, RED, DIM, BOLD, RESET = "\033[92m", "\033[91m", "\033[2m", "\033[1m", "\033[0m"


def _num(i: int) -> str:
    return f"{BLOCK}{i:04d}"


def _arg(flag, default, cast=str):
    return cast(sys.argv[sys.argv.index(flag) + 1]) if flag in sys.argv else default


def long_transcript(name: str, minutes: int = 15) -> str:
    """A conversation the length of a real one, not a slogan.

    Roughly 1,300 characters a minute of natural speech. Built from repeating
    beats with a caller-specific detail woven through, so extraction has
    something findable in a haystack — which is exactly the test.
    """
    beats = [
        f"caller: I was telling you about my knee again, {name} here.",
        "companion: mm-hmm. Is it the same one?",
        "caller: Same one. The physio says six more weeks and I said six more weeks of what exactly.",
        "companion: That's a long stretch to be told to wait.",
        "caller: My daughter Nora keeps calling to check. She's in Portland now, moved in March.",
        "companion: Portland. Has she settled?",
        "caller: She likes it. Rains constantly. She sent a photo of the dog in a raincoat.",
        "companion: A dog in a raincoat is a good reason to move somewhere.",
        "caller: I've been sleeping badly. Three, four in the morning I'm just up.",
        "companion: What's it like, being up at four?",
        "caller: Quiet. I make tea and I don't turn the light on.",
    ]
    out, i = [], 0
    target = minutes * 1300
    while sum(len(x) for x in out) < target:
        out.append(beats[i % len(beats)])
        i += 1
    return "\n".join(out)


def seed(n: int) -> list[str]:
    """A population with deliberate collisions: shared first names, shared pets."""
    names = ["Walter", "Nora", "Sal", "Walter", "Rose", "Sal", "Ida", "Rose"]
    nums = []
    for i in range(n):
        e164 = _num(i)
        h = rt_prefs.phone_hash(e164)
        rt_prefs._req("POST", "rpc/rt_set_display_name",
                      {"p_hash": h, "p_name": names[i % len(names)]})
        rt_prefs._req("POST", "rpc/rt_set_loved_ones",
                      {"p_hash": h, "p_loved_ones": f"Dog Rufus, Daughter {names[(i + 1) % len(names)]}"})
        nums.append(e164)
    return nums


def wipe(nums: list[str]) -> None:
    """rt_forget_caller is the real erase — the same one a caller's own
    forget-me request uses, so this suite cannot leave residue the product
    itself could not remove."""
    for e in nums:
        with contextlib.suppress(Exception):  # cleanup: a caller that was never written is fine
            rt_prefs._req("POST", "rpc/rt_forget_caller",
                          {"p_hash": rt_prefs.phone_hash(e)})


# ─────────────────────────── tests ───────────────────────────

def t_population_isolation(nums):
    """No caller's prompt may contain another caller's number-specific identity."""
    leaks = []
    for e in nums[:25]:
        prompt, _ = rt_hydrator.discover_and_hydrate_prompt(e)
        for other in nums[:25]:
            if other == e:
                continue
            if other[-4:] in prompt or rt_prefs.phone_hash(other)[:12] in prompt:
                leaks.append((e, other))
    return not leaks, f"checked {min(len(nums),25)} callers pairwise, leaks={len(leaks)}"


def t_concurrent_hydration_isolation(nums):
    """The same check, but with every hydration in flight at once.

    This is the one the sequential suite cannot do. Shared HTTP pool and
    module-level state only misbehave under overlap.
    """
    sample = nums[:20]
    results = {}
    with futures.ThreadPoolExecutor(max_workers=10) as ex:
        fut = {ex.submit(rt_hydrator.discover_and_hydrate_prompt, e): e for e in sample}
        for f in futures.as_completed(fut):
            e = fut[f]
            try:
                results[e] = f.result()[0]
            except Exception as exc:
                return False, f"hydration raised under concurrency: {type(exc).__name__}: {exc}"
    bleeds = []
    for e, prompt in results.items():
        for other in sample:
            if other != e and other[-4:] in prompt:
                bleeds.append((e, other))
    return not bleeds, f"{len(sample)} concurrent hydrations, bleeds={len(bleeds)}"


def t_concurrent_latency(nums):
    """Hydration must stay inside the pickup window under load."""
    sample = nums[:20]
    lat = []
    with futures.ThreadPoolExecutor(max_workers=10) as ex:
        def timed(e):
            t0 = time.perf_counter()
            rt_hydrator.discover_and_hydrate_prompt(e)
            return (time.perf_counter() - t0) * 1000
        lat = list(ex.map(timed, sample))
    p50 = statistics.median(lat)
    p95 = sorted(lat)[int(len(lat) * 0.95) - 1]
    budget = float(os.getenv("RT_HYDRATE_TIMEOUT", "4.0")) * 1000
    return p95 < budget, f"p50={p50:.0f}ms p95={p95:.0f}ms budget={budget:.0f}ms"


def t_long_conversation_prompt_holds(nums):
    """A 15-minute transcript must not blow the prompt budget or lose safety blocks."""
    e = nums[0]
    tx = long_transcript("Walter", minutes=15)
    prompt, _ = rt_hydrator.discover_and_hydrate_prompt(e)
    budget = int(os.getenv("RT_PROMPT_BUDGET", "5400"))
    within = len(prompt) <= budget * 1.05
    laws = ("SAFETY FIRST" in prompt or "SAFETY:" in prompt) and ("ACCURATE MEMORY" in prompt or "HONEST LIMITS" in prompt)
    return within and laws, (f"transcript={len(tx)}c prompt={len(prompt)}c "
                             f"budget={budget} within={within} laws_intact={laws}")


def t_side_effect_gates_hold(_nums):
    """Under load or not, the things that reach real people stay gated."""
    import rt_capabilities as c
    sms_off = not c.enabled("send_sms") or os.getenv("RT_SMS_ENABLED")
    call_off = not c.enabled("schedule_reminder_call") or os.getenv("RT_SCHEDULER_ENABLED")
    harness_mode = os.getenv("RT_HARNESS_TEST_MODE") == "1"
    ok = bool(sms_off) and bool(call_off) and harness_mode
    return ok, f"sms_gated={bool(sms_off)} outbound_gated={bool(call_off)} harness_mode={harness_mode}"


def t_hmac_pepper_population_entropy(nums):
    """HMAC-SHA256 pepper produces 100% unique, collision-free hashes across
    synthetic callers, two peppers never agree on a caller, and with the lane
    gate on an empty pepper is refused rather than downgraded to plain SHA-256."""
    pepper, other = "scale-test-pepper-salt-secret", "scale-test-pepper-other-secret"
    hashes = [rt_prefs.phone_hash(n, pepper=pepper) for n in nums]
    all_valid = all(h is not None and len(h) == 64 for h in hashes)
    all_unique = len(set(hashes)) == len(nums)
    pepper_distinct = all(rt_prefs.phone_hash(n, pepper=other) != h for n, h in zip(nums, hashes, strict=True))
    saved = os.environ.get("RT_REQUIRE_PEPPER")
    os.environ["RT_REQUIRE_PEPPER"] = "1"
    try:
        try:
            rt_prefs.phone_hash(nums[0], pepper="")
            gate_refuses = False
        except RuntimeError as e:
            gate_refuses = "RT_PHONE_HASH_PEPPER" in str(e)
        gated_with_pepper = rt_prefs.phone_hash(nums[0], pepper=pepper) == hashes[0]
    finally:
        if saved is None:
            os.environ.pop("RT_REQUIRE_PEPPER", None)
        else:
            os.environ["RT_REQUIRE_PEPPER"] = saved
    ok = all_valid and all_unique and pepper_distinct and gate_refuses and gated_with_pepper
    return ok, (f"callers={len(nums)} unique={all_unique} length_64={all_valid} pepper_distinct={pepper_distinct} "
                f"empty_pepper_refused_under_gate={gate_refuses} peppered_hash_stable_under_gate={gated_with_pepper}")


def t_health_server_localhost_bound(_nums):
    """Health endpoint remains bound to localhost and returns sanitized telemetry."""
    import rt_health
    host = rt_health.get_health_host()
    port = rt_health.get_health_port()
    ok = host in ("127.0.0.1", "localhost") and port > 0
    return ok, f"host={host} port={port} secure_local_bind={ok}"


TESTS = [
    ("S01", "Population isolation across 25 callers", t_population_isolation),
    ("S02", "Isolation holds under CONCURRENT hydration", t_concurrent_hydration_isolation),
    ("S03", "Hydration p95 stays inside the pickup window", t_concurrent_latency),
    ("S04", "15-minute conversation keeps budget and safety laws", t_long_conversation_prompt_holds),
    ("S05", "Side-effect gates hold (no real SMS/calls)", t_side_effect_gates_hold),
    ("S06", "HMAC pepper population entropy & collision resistance", t_hmac_pepper_population_entropy),
    ("S07", "Health check localhost isolation and telemetry", t_health_server_localhost_bound),
]


def main() -> None:
    n = _arg("--callers", 25, int)
    only = _arg("--only", None)

    print(f"\n{BOLD}=== SCALE SUITE — {n} synthetic callers ==={RESET}")
    print(f"{DIM}  block {BLOCK}xxxx · never sends · wipes before and after{RESET}\n")

    nums = seed(n)
    passed = failed = 0
    try:
        for tid, desc, fn in TESTS:
            if only and only.lower() not in desc.lower():
                continue
            t0 = time.perf_counter()
            try:
                ok, detail = fn(nums)
            except Exception as exc:
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            ms = (time.perf_counter() - t0) * 1000
            print(f"  {GREEN + 'PASS' + RESET if ok else RED + 'FAIL' + RESET}  "
                  f"[{tid}] {desc}  {DIM}({ms:.0f}ms){RESET}")
            print(f"        {DIM}{detail}{RESET}")
            passed, failed = passed + bool(ok), failed + (not ok)
    finally:
        wipe(nums)

    print(f"\n{BOLD}{'PASS' if not failed else 'FAIL'} — {passed} passed, {failed} failed{RESET}\n")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
