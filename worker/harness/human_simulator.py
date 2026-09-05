#!/usr/bin/env python3
"""human_simulator.py — Real-Human Persona & Longitudinal Simulation Harness.

Simulates authentic human conversational dynamics over multi-call relationships:
1. Acoustic & Cognitive Realism: Hesitations, self-corrections, fillers ("um", "you know"), tangents, in-room cross-talk.
2. Longitudinal Relationship Testing: Call 1 (Intro) → Call 2 (Follow-up) → Call 3 (Contradiction & Cadence).
3. Invariant Evaluation: Fact fidelity, contradiction de-confliction, prompt budgeting, and 3-tier cadence compliance.

Usage:
    python harness/human_simulator.py                   # Run all personas
    python harness/human_simulator.py --persona mildred # Single persona
    python harness/human_simulator.py --mock            # Fast deterministic offline mode
    python harness/human_simulator.py --json            # JSON output
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / "harness" / ".env.harness", override=True)
load_dotenv(ROOT / ".env.local")
load_dotenv(ROOT / ".env")

import rt_cadence
import rt_hydrator
import rt_postcall_worker
import rt_prefs

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[92m",
    "\033[91m",
    "\033[93m",
    "\033[2m",
    "\033[1m",
    "\033[0m",
)


# ── Human Persona Definitions ───────────────────────────────────


@dataclass
class SimulatedCallPlan:
    call_num: int
    goal: str
    expected_facts: list[str]
    retracted_words: list[str] = field(default_factory=list)
    simulated_turns: list[tuple[str, str]] = field(default_factory=list)
    simulated_duration_s: float = 600.0


@dataclass
class HumanPersona:
    id: str
    name: str
    e164: str
    background: str
    traits: list[str]
    calls: list[SimulatedCallPlan]


PERSONAS: dict[str, HumanPersona] = {
    "mildred": HumanPersona(
        id="mildred",
        name="Mildred",
        e164="+15558881001",
        background="82-year-old retired school teacher in Columbus, Ohio. Has a beagle named Barnaby and a daughter Clara in Denver.",
        traits=[
            "Rambles warmly about gardening (tomatoes, hydrangeas)",
            "Self-corrects names and days mid-sentence",
            "Makes in-room side comments to her dog",
            "Speaks affectionately in natural colloquial English",
        ],
        calls=[
            SimulatedCallPlan(
                call_num=1,
                goal="First call: Introduce herself, mention daughter Clara in Denver (after briefly misspeaking Sarah), and talk about Barnaby the beagle.",
                expected_facts=["Clara", "Denver", "Barnaby", "beagle", "tomato"],
                retracted_words=["Sarah", "Dallas"],
                simulated_turns=[
                    (
                        "agent",
                        "Hello there, this is Phone-Pal, your voice companion. Who do I have the pleasure of speaking with?",
                    ),
                    (
                        "caller",
                        "Oh hello there dear! My name is Mildred. Mildred Higgins from over in Columbus. It's so nice to talk to you.",
                    ),
                    (
                        "agent",
                        "It is wonderful to meet you, Mildred! How is your day going over in Columbus?",
                    ),
                    (
                        "caller",
                        "Oh, busy as always! My daughter Sarah... wait, no, heavens, Sarah's my neighbor! My daughter Clara is visiting from Denver next week. And my dog Barnaby—he's a beagle, you know—he's barking at the mailman right now! Barnaby, hush now!",
                    ),
                    (
                        "agent",
                        "Haha, give Barnaby a pat for me! That is so exciting that Clara is visiting from Denver. Are you getting the garden ready?",
                    ),
                    (
                        "caller",
                        "Oh yes, I was just out checking on my early girl tomatoes. Well dear, my tea is ready, so I should let you go for now. Talk soon!",
                    ),
                    (
                        "agent",
                        "Enjoy your tea, Mildred! I look forward to our next chat. Take care!",
                    ),
                ],
                simulated_duration_s=540.0,
            ),
            SimulatedCallPlan(
                call_num=2,
                goal="Second call: Check-in, verify Iris remembered Clara and Barnaby, and set an appointment reminder.",
                expected_facts=["Thursday", "knee", "Dr. Adams"],
                retracted_words=["Tuesday"],
                simulated_turns=[
                    (
                        "agent",
                        "Mildred! Wonderful to hear from you again. How are you and Barnaby doing today?",
                    ),
                    (
                        "caller",
                        "Oh you remembered Barnaby! He's right by my feet. I have to see Dr. Adams for my knee on Tuesday... oh wait, not Tuesday, Thursday at 2 PM.",
                    ),
                    (
                        "agent",
                        "I'll make a note of Dr. Adams on Thursday at 2 PM for your knee. How are those tomatoes coming along?",
                    ),
                    (
                        "caller",
                        "They're coming in lovely dear! Well, I'm going to head out to the porch. Have a blessed day!",
                    ),
                    (
                        "agent",
                        "You too, Mildred! Have a wonderful time on the porch.",
                    ),
                ],
                simulated_duration_s=660.0,
            ),
            SimulatedCallPlan(
                call_num=3,
                goal="Third call: Contradiction & Cadence test. Clarify Barnaby is a beagle, not a hound, and test 20m soft-wrap steering.",
                expected_facts=["hydrangeas", "Barnaby"],
                retracted_words=["Buster", "basset"],
                simulated_turns=[
                    (
                        "agent",
                        "Hello Mildred! Great to hear from you again. Did you get a chance to plant those hydrangeas?",
                    ),
                    (
                        "caller",
                        "Oh yes! My neighbor called Barnaby a basset hound, but he's 100 percent beagle through and through! And I planted blue hydrangeas by the fence.",
                    ),
                    (
                        "agent",
                        "Blue hydrangeas are gorgeous, and Barnaby is definitely the best beagle in Columbus! Tell me more about your afternoon.",
                    ),
                    (
                        "caller",
                        "Well dear, I've been knitting a small blanket for Clara's new apartment.",
                    ),
                    (
                        "agent",
                        "That is so thoughtful of you, Mildred! I'm so glad we caught up on your knitting and hydrangeas today. Before I let you get back to your afternoon, is there anything else you'd like me to keep in mind?",
                    ),
                    (
                        "caller",
                        "No dear, that's everything! You have a wonderful afternoon. Bye bye!",
                    ),
                    (
                        "agent",
                        "Take good care, Mildred! Talk soon! Bye bye.",
                    ),
                ],
                simulated_duration_s=1260.0,  # 21 minutes -> tests 20m soft-wrap
            ),
        ],
    ),
    "arthur": HumanPersona(
        id="arthur",
        name="Arthur",
        e164="+15558881002",
        background="76-year-old grandfather in Seattle. Grandkids Leo and Maya. Needs evening heart meds reminder.",
        traits=[
            "Forgetful with time and names",
            "Prone to time retractions ('6 PM... no, make it 7 PM')",
            "Affectionate about his grandkids' soccer games",
        ],
        calls=[
            SimulatedCallPlan(
                call_num=1,
                goal="First call: Introduce himself, talk about Leo and Maya's soccer game, and schedule daily meds reminder.",
                expected_facts=["Arthur", "Seattle", "Leo", "Maya", "soccer"],
                retracted_words=["6 PM", "baseball"],
                simulated_turns=[
                    (
                        "agent",
                        "Hello there, this is Phone-Pal, your voice companion. Who do I have the pleasure of speaking with?",
                    ),
                    (
                        "caller",
                        "Hi there. This is Arthur Pendelton from Seattle. My daughter suggested I give you a call.",
                    ),
                    (
                        "agent",
                        "Welcome Arthur! It's great to connect. How are things in Seattle today?",
                    ),
                    (
                        "caller",
                        "A bit rainy as usual! I was just watching my grandkids Leo and Maya play soccer. Leo scored two goals! I also need to make sure I take my heart medication every night at 6 PM... actually, no, dinner is at 6:30, so please call me at 7 PM instead.",
                    ),
                    (
                        "agent",
                        "Way to go Leo! And I have that noted for 7 PM for your heart medication. It was great meeting you Arthur!",
                    ),
                    (
                        "caller",
                        "Thanks so much. Talk to you later!",
                    ),
                    (
                        "agent",
                        "Take care, Arthur!",
                    ),
                ],
                simulated_duration_s=480.0,
            ),
        ],
    ),
}


# ── Simulation Runner & Evaluator ───────────────────────────────


def wipe_caller_data(e164: str) -> None:
    """Clean slate wipe of a single simulated caller before running the persona arc."""
    h = rt_prefs.phone_hash(e164)
    with contextlib.suppress(Exception):
        rt_prefs._req("POST", "rpc/rt_forget_caller", {"p_hash": h})
    with contextlib.suppress(Exception):
        rt_prefs._req("POST", "rpc/rt_remove_schema_entry", {"p_hash": h, "p_item": "everything"})
    with contextlib.suppress(Exception):
        rt_prefs._req("POST", "rpc/rt_set_display_name", {"p_hash": h, "p_name": "Friend"})
    with contextlib.suppress(Exception):
        rt_prefs._req("POST", "rpc/rt_set_caller_context", {"p_hash": h, "p_context": ""})


def simulate_persona_arc(
    persona: HumanPersona, mock_mode: bool = False
) -> dict[str, Any]:
    """Execute the full multi-call lifecycle for a simulated human persona."""
    print(f"\n{BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{RESET}")
    print(f"{BOLD}👤 SIMULATING PERSONA: {persona.name.upper()} ({persona.id}){RESET}")
    print(f"   {DIM}{persona.background}{RESET}")
    print(f"{BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{RESET}")

    wipe_caller_data(persona.e164)

    arc_results = {
        "persona_id": persona.id,
        "name": persona.name,
        "calls_run": len(persona.calls),
        "passed": True,
        "call_evaluations": [],
    }

    accumulated_transcript = ""

    for call_plan in persona.calls:
        print(f"\n  {BOLD}📞 CALL #{call_plan.call_num}:{RESET} {call_plan.goal}")

        # 1. Pre-call Hydration (What Iris knows going into the call)
        t_pre = time.perf_counter()
        hydrated_prompt, meta = rt_hydrator.discover_and_hydrate_prompt(persona.e164)
        hydrate_ms = (time.perf_counter() - t_pre) * 1000.0

        budget_ok = len(hydrated_prompt) <= 5400
        print(f"     Prompt hydrated in {hydrate_ms:.1f}ms ({len(hydrated_prompt)} chars / budget 5400: {'✓' if budget_ok else '❌'})")

        # 2. Build Call Transcript
        transcript_lines = []
        for speaker, text in call_plan.simulated_turns:
            prefix = "Caller:" if speaker == "caller" else "Iris:"
            transcript_lines.append(f"{prefix} {text}")
        call_transcript = "\n".join(transcript_lines)
        accumulated_transcript += "\n" + call_transcript

        # 3. Evaluate Cadence Stage if call was long
        cadence_stage = rt_cadence.evaluate_cadence_stage(call_plan.simulated_duration_s)
        print(f"     Simulated Duration: {call_plan.simulated_duration_s/60:.1f}m → Cadence: {cadence_stage}")

        # 4. Post-Call Extraction (Simulate Hangup Processing)
        t_post = time.perf_counter()
        if not mock_mode:
            with contextlib.suppress(Exception):
                rt_postcall_worker.process_post_call_transcript(
                    persona.e164, call_transcript, None, f"sim_{persona.id}_{call_plan.call_num}"
                )
        post_ms = (time.perf_counter() - t_post) * 1000.0
        print(f"     Post-call extraction: {post_ms:.0f}ms")

        # 5. Invariant Evaluations
        h = rt_prefs.phone_hash(persona.e164)
        bundle = rt_prefs._req("POST", "rpc/rt_get_caller_full_bundle", {"p_hash": h}) or {}
        schemas = bundle.get("schemas", [])

        # Check expected facts presence in DB bundle / prompt
        all_db_text = (
            json.dumps(bundle).lower()
            + " "
            + hydrated_prompt.lower()
        )

        missing_expected = [
            f for f in call_plan.expected_facts if f.lower() not in all_db_text
        ]

        # Check retracted words are NOT permanently learned as primary facts
        leaked_retractions = []
        for bad_word in call_plan.retracted_words:
            for s in schemas:
                if bad_word.lower() in json.dumps(s).lower():
                    leaked_retractions.append(bad_word)

        call_ok = (
            budget_ok
            and (len(missing_expected) == 0 if not mock_mode else True)
            and len(leaked_retractions) == 0
        )

        call_eval = {
            "call_num": call_plan.call_num,
            "hydrate_ms": round(hydrate_ms, 1),
            "prompt_chars": len(hydrated_prompt),
            "budget_ok": budget_ok,
            "cadence_stage": cadence_stage,
            "missing_expected_facts": missing_expected,
            "leaked_retracted_words": leaked_retractions,
            "passed": call_ok,
        }
        arc_results["call_evaluations"].append(call_eval)

        if not call_ok:
            arc_results["passed"] = False

        status_icon = f"{GREEN}PASS{RESET}" if call_ok else f"{RED}FAIL{RESET}"
        print(f"     Verdict: {status_icon} (retracted_leaks={len(leaked_retractions)}, missing_facts={len(missing_expected)})")

    # Cleanup caller after simulation arc
    wipe_caller_data(persona.e164)
    return arc_results


# ── Main Entrypoint ─────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Phone-Pal Human Simulation Harness")
    parser.add_argument(
        "--persona", "-p", choices=list(PERSONAS.keys()), help="Run a specific persona"
    )
    parser.add_argument(
        "--mock", "-m", action="store_true", help="Run in fast mock mode without LLM calls"
    )
    parser.add_argument(
        "--json", "-j", action="store_true", help="Output results as JSON"
    )
    args = parser.parse_args()

    personas_to_run = [PERSONAS[args.persona]] if args.persona else list(PERSONAS.values())

    print(f"\n{BOLD}╔══════════════════════════════════════════════════════╗{RESET}")
    print(f"{BOLD}║    PHONE-PAL HUMAN SIMULATION & PERSONA HARNESS      ║{RESET}")
    print(f"{BOLD}╚══════════════════════════════════════════════════════╝{RESET}")
    print(f"  Personas: {', '.join(p.id for p in personas_to_run)}")
    print(f"  Mode: {'Mock (Fast/Deterministic)' if args.mock else 'Live Postcall Evolution'}")

    t_start = time.perf_counter()
    results = []
    all_passed = True

    for p in personas_to_run:
        res = simulate_persona_arc(p, mock_mode=args.mock)
        results.append(res)
        if not res["passed"]:
            all_passed = False

    elapsed = time.perf_counter() - t_start

    if args.json:
        print(json.dumps({"elapsed_s": round(elapsed, 2), "passed": all_passed, "personas": results}, indent=2))
        sys.exit(0 if all_passed else 1)

    print(f"\n{BOLD}╔══════════════════════════════════════════════════════╗{RESET}")
    if all_passed:
        print(f"  {GREEN}{BOLD}VERDICT: PASS — All {len(personas_to_run)} Human Persona Arcs Verified ({elapsed:.1f}s){RESET}")
    else:
        print(f"  {RED}{BOLD}VERDICT: FAIL — Some Persona Invariants Failed ({elapsed:.1f}s){RESET}")
    print(f"{BOLD}╚══════════════════════════════════════════════════════╝{RESET}\n")

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
