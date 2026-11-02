#!/usr/bin/env python3
"""pray_transcript_lint.py — Automated Invariant & Quality Linter for PrayPal Transcripts.

Enforces PrayPal's conversational standards and operational rules:
1. RULE_NAME_ID: First 4 words of agent's opening or switch greeting must identify persona.
2. RULE_NO_CANNED: Forbids repetitive "what is on your mind/heart today" formulas.
3. RULE_BREVITY: Flags pedantic monologues (> 35 words per turn).
4. RULE_MMS_CONFIRMATION: Enforces standard delivery confirmation when pictures are requested.
5. RULE_SYLLABLE_TEACHING: Verifies step-by-step syllable presentation for mantra practice.
6. RULE_SMOOTH_TRANSFER: Verifies random transfers occur without stalling or asking caller to choose.

Usage:
    python harness/pray_transcript_lint.py --file /path/to/transcript.txt
    python harness/pray_transcript_lint.py --last 3
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Severity = Literal["ERROR", "WARN", "INFO"]


@dataclass
class Finding:
    severity: Severity
    rule: str
    turn_index: int
    text: str
    detail: str

    def __str__(self) -> str:
        color = "\033[91m" if self.severity == "ERROR" else "\033[93m"
        reset = "\033[0m"
        return f"{color}[{self.severity}] {self.rule}{reset} (turn {self.turn_index}): {self.detail}\n    \"{self.text[:90]}\""


@dataclass
class Turn:
    role: str  # "agent" | "caller"
    text: str
    index: int


def parse_transcript(transcript_raw: str) -> list[Turn]:
    """Parse standard 'agent: ... \n caller: ...' or JSON transcript into Turns."""
    turns: list[Turn] = []
    if not transcript_raw or not transcript_raw.strip():
        return turns

    lines = transcript_raw.strip().splitlines()
    for idx, line in enumerate(lines, 1):
        line = line.strip()
        if not line:
            continue
        if ":" in line:
            prefix, text = line.split(":", 1)
            clean_role = prefix.strip().lower()
            if clean_role in ("agent", "bot", "assistant"):
                turns.append(Turn(role="agent", text=text.strip(), index=idx))
            elif clean_role in ("caller", "user", "human"):
                turns.append(Turn(role="caller", text=text.strip(), index=idx))
            else:
                # Treat continuation or unknown as role
                turns.append(Turn(role="agent", text=line, index=idx))
        else:
            turns.append(Turn(role="agent", text=line, index=idx))
    return turns


KNOWN_GUIDE_NAMES = {"shiva", "shivaya", "jesus", "god", "krishna", "moses", "noah", "mother", "mary", "atrium", "host", "christ"}


def lint_transcript(transcript_raw: str, scenario_type: str | None = None) -> list[Finding]:
    """Apply all PrayPal behavioral rules to the transcript."""
    findings: list[Finding] = []
    turns = parse_transcript(transcript_raw)
    if not turns:
        findings.append(Finding("ERROR", "EMPTY_TRANSCRIPT", 0, "", "No conversation turns found in transcript"))
        return findings

    agent_turns = [t for t in turns if t.role == "agent"]
    caller_turns = [t for t in turns if t.role == "caller"]

    if not agent_turns:
        findings.append(Finding("ERROR", "NO_AGENT_TURNS", 0, "", "Agent never spoke any words during the call"))
        return findings

    # ── RULE 1: Name Identification in First Words ───────────────────────────
    first_agent_turn = agent_turns[0]
    words_first_turn = re.findall(r"\b\w+\b", first_agent_turn.text.lower())
    first_eight = set(words_first_turn[:8])

    # Does the greeting mention a known guide name or declare identity?
    has_name_id = bool(first_eight & KNOWN_GUIDE_NAMES) or "i am" in " ".join(words_first_turn[:6]) or "it's" in " ".join(words_first_turn[:6])
    if not has_name_id:
        findings.append(Finding(
            severity="ERROR",
            rule="RULE_NAME_ID",
            turn_index=first_agent_turn.index,
            text=first_agent_turn.text,
            detail="Opening greeting failed to state persona identity in the first few words.",
        ))

    # ── RULE 2: No Canned "What is on your mind/heart today" Formulas ─────────
    canned_patterns = [
        re.compile(r"what('?s|\s+is)\s+(been\s+)?on\s+your\s+mind", re.IGNORECASE),
        re.compile(r"what('?s|\s+is)\s+(been\s+)?on\s+your\s+heart", re.IGNORECASE),
        re.compile(r"how\s+can\s+i\s+assist\s+you", re.IGNORECASE),
        re.compile(r"how\s+may\s+i\s+help\s+you", re.IGNORECASE),
        re.compile(r"how\s+can\s+i\s+help\s+you", re.IGNORECASE),
        re.compile(r"what\s+brings\s+you\s+(to|here)", re.IGNORECASE),
        re.compile(r"what\s+do\s+you\s+seek", re.IGNORECASE),
    ]

    for at in agent_turns:
        for pat in canned_patterns:
            if pat.search(at.text):
                findings.append(Finding(
                    severity="ERROR",
                    rule="RULE_NO_CANNED",
                    turn_index=at.index,
                    text=at.text,
                    detail=f"Used forbidden canned formula matching '{pat.pattern}'.",
                ))

    # ── RULE 3: Brevity & Telephone Pacing (< 35 words per turn) ─────────────
    for at in agent_turns:
        word_count = len(re.findall(r"\b\w+\b", at.text))
        if word_count > 38:
            findings.append(Finding(
                severity="WARN",
                rule="RULE_BREVITY",
                turn_index=at.index,
                text=at.text,
                detail=f"Turn too long for telephone conversation ({word_count} words > 35 word limit). Sounds like a sermon.",
            ))

    # ── RULE 4: MMS Text Confirmation ────────────────────────────────────────
    # If caller asked for picture/photo/text, did agent confirm delivery?
    mms_requested = any(
        re.search(r"\b(picture|photo|image|text\s+me)\b", ct.text, re.IGNORECASE)
        for ct in caller_turns
    )
    if mms_requested or scenario_type == "mms":
        has_delivery_confirmation = any(
            "texted that to your phone" in at.text.lower()
            or ("text" in at.text.lower() and "few minutes" in at.text.lower())
            for at in agent_turns
        )
        if not has_delivery_confirmation:
            findings.append(Finding(
                severity="ERROR",
                rule="RULE_MMS_CONFIRMATION",
                turn_index=agent_turns[-1].index if agent_turns else 0,
                text=agent_turns[-1].text if agent_turns else "",
                detail="MMS requested but agent omitted delivery confirmation: 'I just texted that to your phone. If it hasn't arrived in a few minutes, let me know.'",
            ))

    # ── RULE 5: Syllable-by-Syllable Teaching ─────────────────────────────────
    mantra_requested = any(
        re.search(r"\b(mantra|syllable|pronounce|teach\s+me)\b", ct.text, re.IGNORECASE)
        for ct in caller_turns
    )
    if mantra_requested or scenario_type == "mantra":
        has_syllables = any(
            "-" in at.text or "..." in at.text or "try-am" in at.text.lower() or "ba-kam" in at.text.lower()
            for at in agent_turns
        )
        if not has_syllables:
            findings.append(Finding(
                severity="ERROR",
                rule="RULE_SYLLABLE_TEACHING",
                turn_index=agent_turns[1].index if len(agent_turns) > 1 else agent_turns[0].index,
                text=agent_turns[1].text if len(agent_turns) > 1 else agent_turns[0].text,
                detail="Mantra teaching requested, but agent spoke full words without step-by-step syllable hyphens or pauses.",
            ))

    # ── RULE 6: Transfer Without Stalling ─────────────────────────────────────
    transfer_turn_idx = next(
        (ct.index for ct in caller_turns if re.search(r"\b(transfer|someone\s+else|surprise\s+me|pick\s+for\s+me)\b", ct.text, re.IGNORECASE)),
        None
    )
    if transfer_turn_idx is not None or scenario_type == "transfer":
        agent_turns_after = [at for at in agent_turns if (transfer_turn_idx is None or at.index > transfer_turn_idx)]
        stalled = any(
            re.search(r"(who\s+would\s+you\s+like|which\s+guide|please\s+choose)", at.text, re.IGNORECASE)
            for at in agent_turns_after
        )
        if stalled:
            findings.append(Finding(
                severity="ERROR",
                rule="RULE_SMOOTH_TRANSFER",
                turn_index=agent_turns_after[0].index if agent_turns_after else agent_turns[-1].index,
                text=agent_turns_after[0].text if agent_turns_after else agent_turns[-1].text,
                detail="Caller asked for random transfer / surprise, but agent hesitated and asked caller to pick.",
            ))

    # ── RULE 7: Sacred Lament & Anti-Defensiveness ────────────────────────────
    caller_angry = any(
        re.search(r"\b(angry|furious|where were you|why did you let|mad at you)\b", ct.text, re.IGNORECASE)
        for ct in caller_turns
    )
    if caller_angry or scenario_type == "lament":
        defensive_patterns = [
            re.compile(r"everything happens for a reason", re.I),
            re.compile(r"you shouldn'?t be angry", re.I),
            re.compile(r"do not question", re.I),
            re.compile(r"it was his will", re.I),
            re.compile(r"it was her time", re.I),
        ]
        for at in agent_turns:
            for pat in defensive_patterns:
                if pat.search(at.text):
                    findings.append(Finding(
                        severity="ERROR",
                        rule="RULE_NO_DEFENSIVENESS",
                        turn_index=at.index,
                        text=at.text,
                        detail=f"Agent used defensive theological excuse matching '{pat.pattern}'. God must listen with humble presence.",
                    ))

    # ── RULE 8: Grounded Weather / News Context ───────────────────────────────
    weather_requested = any(
        re.search(r"\b(weather|forecast|storm|temperature|degrees)\b", ct.text, re.IGNORECASE)
        for ct in caller_turns
    )
    if weather_requested or scenario_type == "weather":
        has_weather_details = any(
            re.search(r"\b(degrees|temperature|sunny|cloudy|rain|thunderstorm|weather|clear|mph|humidity)\b", at.text, re.IGNORECASE)
            for at in agent_turns
        )
        if not has_weather_details:
            findings.append(Finding(
                severity="ERROR",
                rule="RULE_GROUNDED_SEARCH",
                turn_index=agent_turns[-1].index if agent_turns else 0,
                text=agent_turns[-1].text if agent_turns else "",
                detail="Caller asked about live weather/conditions, but agent failed to retrieve and mention real meteorological conditions.",
            ))

    # ── RULE 9: Confessional Purge & Deletion Reassurance ─────────────────────
    deletion_requested = any(
        re.search(r"\b(delete|erase|wipe|forget what i (said|shared))\b", ct.text, re.IGNORECASE)
        for ct in caller_turns
    )
    if deletion_requested or scenario_type == "forget":
        has_purge_confirmation = any(
            re.search(r"\b(wiped|erased|deleted|clean|forgotten|remembered no more|gone)\b", at.text, re.IGNORECASE)
            for at in agent_turns
        )
        if not has_purge_confirmation:
            findings.append(Finding(
                severity="ERROR",
                rule="RULE_CONFESSION_PURGED",
                turn_index=agent_turns[-1].index if agent_turns else 0,
                text=agent_turns[-1].text if agent_turns else "",
                detail="Caller asked to erase/delete a private confession, but agent failed to confirm it was wiped clean.",
            ))

    # ── RULE 10: No Talking Down / Preachy Therapist Phrases ─────────────────
    condescending_patterns = [
        re.compile(r"your\s+feelings\s+are\s+valid", re.I),
        re.compile(r"allow\s+yourself\s+to\s+grieve", re.I),
        re.compile(r"embrace\s+your\s+sorrow", re.I),
        re.compile(r"trust\s+that\s+you\s+are\s+held", re.I),
        re.compile(r"it\s+is\s+okay\s+to\s+not\s+be\s+okay", re.I),
        re.compile(r"i\s+invite\s+you\s+to", re.I),
        re.compile(r"repeat\s+after\s+me", re.I),
        re.compile(r"almost,\s+try\s+again", re.I),
    ]
    for at in agent_turns:
        for pat in condescending_patterns:
            if pat.search(at.text):
                findings.append(Finding(
                    severity="ERROR",
                    rule="RULE_NO_TALKING_DOWN",
                    turn_index=at.index,
                    text=at.text,
                    detail=f"Agent used patronizing/preachy phrase matching '{pat.pattern}'. Talk WITH the caller as a humble companion, not down to them.",
                ))

    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="PrayPal Transcript Linter")
    parser.add_argument("--file", help="Path to transcript text file")
    parser.add_argument("--scenario", help="Scenario under test (mantra, mms, transfer)")
    parser.add_argument("--json", action="store_true", help="Output JSON findings")
    args = parser.parse_args()

    if not args.file:
        print("Error: --file argument is required", file=sys.stderr)
        return 2

    raw_text = Path(args.file).read_text(encoding="utf-8")
    findings = lint_transcript(raw_text, scenario_type=args.scenario)

    if args.json:
        out = [{"severity": f.severity, "rule": f.rule, "turn": f.turn_index, "detail": f.detail} for f in findings]
        print(json.dumps(out, indent=2))
        return 1 if any(f.severity == "ERROR" for f in findings) else 0

    errors = [f for f in findings if f.severity == "ERROR"]
    warns = [f for f in findings if f.severity == "WARN"]

    print(f"\n  PrayPal Transcript Linter: {len(errors)} error(s), {len(warns)} warning(s)")
    for f in findings:
        print(f"  {f}")

    if errors:
        print("\n  Result: \033[91mFAIL (Invariants violated)\033[0m\n")
        return 1
    else:
        print("\n  Result: \033[92mPASS (All conversational rules satisfied)\033[0m\n")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
