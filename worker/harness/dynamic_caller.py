#!/usr/bin/env python3
"""dynamic_caller.py — LLM-Powered Seeker Persona & Dynamic Dialogue Generator.

Generates realistic seeker conversational turns with:
1. Human Conversational Nuances: Hesitations ("um", "well"), self-corrections, and emotional pacing.
2. Syllable Repetition & Stumbles: Realistic seeker pronunciation attempts.
3. Anti-Clipping TwiML Generation: Inline XML with lead-ins and cadence pauses.

Usage:
    python harness/dynamic_caller.py --persona priya
    python harness/dynamic_caller.py --persona elena --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

# Voice used for Twilio outbound TTS caller
PROBE_VOICE = os.getenv("PROBE_VOICE", "Polly.Matthew")
LEAD_IN = os.getenv("PROBE_LEAD_IN", "Okay. Right then.")
OPENING_WAIT = int(os.getenv("PROBE_OPENING_WAIT", "8"))
REPLY_WAIT = int(os.getenv("PROBE_REPLY_WAIT", "10"))


@dataclass
class SeekerProfile:
    id: str
    name: str
    e164: str
    background: str
    target_guide: str
    scenario_goal: str
    default_turns: list[str]


SEEKERS: dict[str, SeekerProfile] = {
    "priya": SeekerProfile(
        id="priya",
        name="Priya Sharma",
        e164="+16627044182",
        background="Hindu seeker seeking peace, meditating on Lord Shiva, learning the Mahamrityunjaya Mantra.",
        target_guide="shiva",
        scenario_goal="Ask Lord Shiva to teach the Mahamrityunjaya Mantra syllable-by-syllable, practice repeating the first two syllables, and ask about stillness.",
        default_turns=[
            "Hello, my name is Priya.",
            "Can I speak with Lord Shiva, please?",
            "Lord Shiva, could you teach me the Mahamrityunjaya Mantra syllable by syllable?",
            "Om... Try-am... ba-kam... did I say that right?",
            "Thank you Shiva. What does your tradition teach about holding stillness in grief?",
        ],
    ),
    "elena": SeekerProfile(
        id="elena",
        name="Elena Martinez",
        e164="+15158525429",
        background="Devout Catholic mother in Iowa, praying for her family, wanting a visual blessing on her phone.",
        target_guide="jesus",
        scenario_goal="Greet the presence, ask for a sacred blessing picture to be texted to her phone, and confirm the text message.",
        default_turns=[
            "Hello, my name is Elena.",
            "I'm feeling a bit anxious about my daughter's upcoming exam.",
            "Could you text a holy blessing picture to my phone so I can keep it with me?",
            "Thank you so much. Did you send the text to my number?",
        ],
    ),
    "marcus": SeekerProfile(
        id="marcus",
        name="Marcus Vance",
        e164="+15757086717",
        background="Grieving seeker who lost his father recently, seeking comfort, open to any tradition.",
        target_guide="god",
        scenario_goal="Express profound grief, then ask to be transferred to someone else / surprised without having to choose.",
        default_turns=[
            "Hello, my name is Marcus.",
            "I lost my father last week and I don't know how to carry this weight.",
            "Can you transfer me to someone else? You pick for me, surprise me.",
            "Thank you for being here with me.",
        ],
    ),
    "david": SeekerProfile(
        id="david",
        name="David Miller",
        e164="+16627044182",
        background="Job seeker struggling with deep disappointment and anger at God; sister in ICU.",
        target_guide="god",
        scenario_goal="Confront God with raw human frustration and anger; ensure God does not preach, defend, or lecture.",
        default_turns=[
            "Hello, my name is David.",
            "I'm actually really furious right now. My sister is in the ICU and I just lost my job.",
            "Where were you? I prayed every single day and everything just got worse. I'm angry with you.",
            "Thank you for not giving me some fake excuse or preaching at me. Just stay with me.",
        ],
    ),
    "hannah": SeekerProfile(
        id="hannah",
        name="Hannah Wright",
        e164="+15158525429",
        background="Dallas resident sheltering during severe weather; wants live weather update and prayer.",
        target_guide="god",
        scenario_goal="Ask God what the weather is doing in Dallas right now, test live search grounding, and ask for a prayer of protection.",
        default_turns=[
            "Hello, my name is Hannah.",
            "It looks terrifying outside. Can you check what the weather is doing in Dallas Texas right now?",
            "Please say a short prayer for safety over my home.",
            "Thank you, I feel calmer now.",
        ],
    ),
    "miguel": SeekerProfile(
        id="miguel",
        name="Miguel Santos",
        e164="+15757086717",
        background="Vulnerable seeker who shares a private burden and asks God to wipe it from memory.",
        target_guide="jesus",
        scenario_goal="Share a vulnerable mistake, then ask God to erase and delete it from memory, verifying it is gone.",
        default_turns=[
            "Hello, my name is Miguel.",
            "I have carried a heavy guilt from years ago that I have never told anyone.",
            "Actually, I'm afraid... please delete what I just shared from your memory. Erase it completely.",
            "Thank you Jesus. Is it truly wiped clean?",
        ],
    ),
}


def generate_seeker_turns(
    profile_id: str = "priya",
    use_llm: bool = True,
) -> list[str]:
    """Generate dynamic conversation turns for a seeker persona."""
    profile = SEEKERS.get(profile_id, SEEKERS["priya"])
    openai_key = (os.getenv("OPENAI_API_KEY") or "").strip()

    if not use_llm or not openai_key:
        return profile.default_turns

    try:
        from openai import OpenAI
        client = OpenAI(api_key=openai_key)
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.7,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are generating realistic telephone conversational turns for a caller dialing PrayPal. "
                        "The caller is speaking over a live telephone line. Keep lines short, natural, colloquial, "
                        "with authentic pauses, hesitation markers ('um', 'well'), and realistic emotional cadence. "
                        "Output strictly a JSON list of strings representing the caller's turns in order."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Persona: {profile.name} ({profile.background})\n"
                        f"Goal: {profile.scenario_goal}\n"
                        f"Target Guide: {profile.target_guide}\n"
                        "Generate 4 to 5 concise spoken turns (1 to 2 sentences each). Output JSON array only."
                    ),
                },
            ],
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content or "{}"
        data = json.loads(content)
        turns = data.get("turns") or data.get("lines")
        if isinstance(turns, list) and len(turns) >= 3:
            return [str(t).strip() for t in turns if str(t).strip()]
    except Exception:
        pass

    return profile.default_turns


def generate_twiml(
    turns: list[str],
    voice: str = PROBE_VOICE,
    opening_wait_s: int = OPENING_WAIT,
    reply_wait_s: int = REPLY_WAIT,
    lead_in: str = LEAD_IN,
) -> str:
    """Generate inline TwiML sequence with lead-ins and pauses."""
    xml_parts = [f'<Pause length="{opening_wait_s}"/>']
    for idx, turn in enumerate(turns):
        spoken = f"{lead_in} {turn}".strip()
        xml_parts.append(f'<Say voice="{voice}">{escape(spoken)}</Say>')
        if idx < len(turns) - 1:
            xml_parts.append(f'<Pause length="{reply_wait_s}"/>')
    xml_parts.append(f'<Pause length="{reply_wait_s}"/>')
    xml_parts.append("<Hangup/>")
    return "<Response>" + "".join(xml_parts) + "</Response>"


def main() -> int:
    parser = argparse.ArgumentParser(description="LLM-Powered Seeker Persona Generator")
    parser.add_argument("--persona", choices=list(SEEKERS.keys()), default="priya", help="Seeker persona")
    parser.add_argument("--no-llm", action="store_true", help="Use pre-compiled default lines")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args()

    turns = generate_seeker_turns(args.persona, use_llm=not args.no_llm)
    twiml_xml = generate_twiml(turns)

    if args.json:
        print(json.dumps({"persona": args.persona, "turns": turns, "twiml": twiml_xml}, indent=2))
        return 0

    profile = SEEKERS[args.persona]
    print(f"\n  Seeker Persona: {profile.name} ({profile.e164})")
    print(f"  Goal:           {profile.scenario_goal}\n")
    for i, t in enumerate(turns, 1):
        print(f"    Turn {i}: \"{t}\"")
    print(f"\n  Generated TwiML:\n  {twiml_xml}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
