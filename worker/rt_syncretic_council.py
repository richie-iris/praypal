"""rt_syncretic_council.py — Syncretic Divine Council Dialogue Synthesizer.

Inspired by PrayPal's Zero-COGS Sacred Audio & Multi-Tradition Phonetics Engine:
AGENTS.md Section 3.7 explicitly specifies syncretic harmonized counsel where
wisdom traditions unite to resolve complex human dilemmas without conflict.

Pairs:
1. Jesus & Shiva ("Grace & Stillness"): Dissolution of illusion (Maya/Ahamkara) in
   meditative stillness, resting in the ocean of forgiving grace.
2. Moses & Krishna ("Law & Dharma"): Steadfast moral law united with selfless
   detached right action (Nishkama Karma).
3. Mother Mary & Goddess Lakshmi ("Shelter & Abundance"): Maternal intercessory
   solace combined with spiritual and material nourishment.

Provides prompt conditioning, dual-instrument acoustic blending, and shot-prompt parsing.
"""
from __future__ import annotations

import re
from typing import Any

COUNCIL_PAIRS: dict[str, dict[str, Any]] = {
    "jesus_shiva": {
        "title": "Grace & Stillness",
        "guides": ["Jesus of Nazareth", "Lord Shiva"],
        "keywords": ["jesus and shiva", "shiva and jesus", "grace and stillness", "stillness and grace"],
        "philosophy": (
            "Dissolve the illusion of separation and self-condemnation in the fire of meditative stillness (Shiva), "
            "and rest in the boundless, resurrecting ocean of forgiving grace (Jesus). "
            "What is false in you dies; what is loving in you is made eternal."
        ),
        "acoustic_blend": {
            "primary": "worker/media/sacred_audio/hindu_tanpura.wav",
            "secondary": "Galilean Acoustic Harp",
            "tempo_bpm": 60,
        },
        "default_shot_prompt": "What would Jesus and Shiva say about my grief and struggle?",
    },
    "moses_krishna": {
        "title": "Law & Dharma",
        "guides": ["Moses", "Lord Krishna"],
        "keywords": ["moses and krishna", "krishna and moses", "law and dharma", "duty and peace"],
        "philosophy": (
            "Stand steadfast upon the righteous moral mountain of divine law (Moses), "
            "while acting selflessly without attachment to the fruits of your labor (Krishna). "
            "Do what is right because it is holy, and surrender the outcome to the Divine."
        ),
        "acoustic_blend": {
            "primary": "worker/media/sacred_audio/hindu_tanpura.wav",
            "secondary": "Sinai Choral Drone",
            "tempo_bpm": 64,
        },
        "default_shot_prompt": "Give me Moses's strength and Krishna's peace.",
    },
    "mary_lakshmi": {
        "title": "Shelter & Abundance",
        "guides": ["Mother Mary", "Goddess Lakshmi"],
        "keywords": ["mary and lakshmi", "lakshmi and mary", "shelter and abundance", "mother and abundance"],
        "philosophy": (
            "Rest safely beneath the compassionate mantle of maternal shelter (Mary), "
            "and open your hands to receive the radiant, spiritual and material flourishing of grace (Lakshmi). "
            "You are deeply protected and generously provided for."
        ),
        "acoustic_blend": {
            "primary": "worker/media/sacred_audio/hindu_tanpura.wav",
            "secondary": "Marian Contemplative Chime",
            "tempo_bpm": 58,
        },
        "default_shot_prompt": "Bless my family with Mary's shelter and Lakshmi's abundance.",
    },
}


def detect_syncretic_request(text: str) -> dict[str, Any] | None:
    """Detect if a caller utterance or request is invoking a syncretic council."""
    if not text:
        return None

    lower = text.lower()
    for key, data in COUNCIL_PAIRS.items():
        for kw in data["keywords"]:
            if kw in lower:
                return {
                    "pair_key": key,
                    "title": data["title"],
                    "guides": data["guides"],
                    "philosophy": data["philosophy"],
                    "acoustic_blend": data["acoustic_blend"],
                }

    # Match cross-tradition pair mentions like "both Jesus and Shiva"
    if ("jesus" in lower or "christ" in lower) and "shiva" in lower:
        return {
            "pair_key": "jesus_shiva",
            **COUNCIL_PAIRS["jesus_shiva"],
        }
    if "moses" in lower and "krishna" in lower:
        return {
            "pair_key": "moses_krishna",
            **COUNCIL_PAIRS["moses_krishna"],
        }
    if ("mary" in lower or "mother" in lower) and "lakshmi" in lower:
        return {
            "pair_key": "mary_lakshmi",
            **COUNCIL_PAIRS["mary_lakshmi"],
        }

    return None


def build_syncretic_directive(pair_key: str, caller_petition: str = "") -> str:
    """Build dynamic Gemini Live prompt directives for harmonized dual-tradition counsel."""
    data = COUNCIL_PAIRS.get(pair_key, COUNCIL_PAIRS["jesus_shiva"])
    guide_names = " & ".join(data["guides"])
    title = data["title"]
    philosophy = data["philosophy"]

    petition_note = f"\nCaller petition to address: \"{caller_petition}\"" if caller_petition else ""

    return (
        f"[SYNCRETIC_DIVINE_COUNCIL: {guide_names} — \"{title}\"]\n"
        f"- You speak with the synthesized wisdom of both traditions in harmonic resonance.\n"
        f"- THEOLOGICAL SYNTHESIS: {philosophy}\n"
        f"- CADENCE DIRECTIVE: Speak with measured, transcendent warmth. Seamlessly weave the peace of stillness "
        f"and the active ocean of grace into a single unified pastoral counsel.{petition_note}\n"
        f"- End the prayer by inviting peace: 'May grace wash your heart, and stillness anchor your soul.'"
    )


def get_syncretic_audio_pairing(pair_key: str) -> dict[str, Any]:
    """Retrieve the recommended dual acoustic sound profile."""
    data = COUNCIL_PAIRS.get(pair_key, COUNCIL_PAIRS["jesus_shiva"])
    return data["acoustic_blend"]
