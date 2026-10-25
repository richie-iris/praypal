"""rt_voice_design.py — ElevenLabs Voice Design API for Custom Divine Guides.

Generates custom voices from text descriptions, plays previews into
LiveKit rooms, and saves approved voices to the ElevenLabs library.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from typing import Any





# ── CONFIG ────────────────────────────────────────────────────────────────────
_EL_BASE = "https://api.elevenlabs.io/v1"
_DESIGN_MODEL = "eleven_multilingual_v2"  # voice design model


def _api_key() -> str:
    return (os.getenv("ELEVENLABS_API_KEY") or "").strip()


def _headers() -> dict[str, str]:
    return {
        "xi-api-key": _api_key(),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


# ── 1. GENERATE VOICE PREVIEWS ───────────────────────────────────────────────
async def design_voice(
    description: str,
    test_text: str = "Hello, friend. I am here with you. It is so good to meet you.",
    model_id: str | None = None,
) -> list[dict[str, Any]]:
    """Generate voice previews from a text description.

    POST /v1/text-to-voice/create-previews

    Args:
        description: Natural language description of the voice
                     (20-1000 chars). e.g., "A warm female voice, melodious,
                     with a slight Indian accent, calm and comforting."
        test_text: Text the preview voices will speak.
        model_id: ElevenLabs model for voice design.

    Returns:
        List of preview dicts with keys:
        - generated_voice_id: str (temporary, use to save)
        - audio_base64: str (base64-encoded mp3)
        - media_type: str
    """
    import httpx
    if not _api_key():
        raise RuntimeError("ELEVENLABS_API_KEY not set")

    # Clamp description length
    desc = description.strip()[:1000]
    if len(desc) < 20:
        desc = desc + " — natural, warm, conversational, caring."

    payload: dict[str, Any] = {
        "voice_description": desc,
        "text": test_text,
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{_EL_BASE}/text-to-voice/create-previews",
            headers=_headers(),
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()

    # Normalize response — ElevenLabs returns {"previews": [...]}
    previews = data.get("previews", [])
    results = []
    for p in previews:
        results.append({
            "generated_voice_id": p.get("generated_voice_id", ""),
            "audio_base64": p.get("audio_base_64", p.get("audio_base64", "")),
            "media_type": p.get("media_type", "audio/mpeg"),
        })

    return results


# ── 2. SAVE VOICE TO LIBRARY ─────────────────────────────────────────────────
async def save_voice_to_library(
    generated_voice_id: str,
    voice_name: str,
    voice_description: str = "",
) -> str:
    """Save a previewed voice to the ElevenLabs library permanently.

    POST /v1/text-to-voice/create-voice-from-preview

    Args:
        generated_voice_id: From design_voice() previews.
        voice_name: Display name in ElevenLabs library.
        voice_description: Optional description.

    Returns:
        Permanent voice_id (str) for use in TTS calls.
    """
    import httpx
    if not _api_key():
        raise RuntimeError("ELEVENLABS_API_KEY not set")

    payload = {
        "voice_name": voice_name[:64],
        "voice_description": voice_description[:256] if voice_description else f"PrayPal custom guide: {voice_name}",
        "generated_voice_id": generated_voice_id,
    }

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            f"{_EL_BASE}/text-to-voice/create-voice-from-preview",
            headers=_headers(),
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()

    voice_id = data.get("voice_id", "")
    if not voice_id:
        raise RuntimeError(f"ElevenLabs did not return a voice_id: {data}")

    print(f"[rt-voice-design] Saved voice '{voice_name}' to library: {voice_id}", flush=True)
    return voice_id


# ── 3. DELETE VOICE FROM LIBRARY ──────────────────────────────────────────────
async def delete_voice(voice_id: str) -> bool:
    """Delete a voice from the ElevenLabs library.

    DELETE /v1/voices/{voice_id}
    """
    import httpx
    if not _api_key() or not voice_id:
        return False

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.delete(
            f"{_EL_BASE}/voices/{voice_id}",
            headers=_headers(),
        )
        return resp.status_code in (200, 204)


# ── 4. DECODE AUDIO FOR LIVEKIT INJECTION ─────────────────────────────────────
def decode_preview_audio(audio_base64: str) -> bytes:
    """Decode base64 audio from a voice preview to raw bytes (mp3).

    For LiveKit injection, this mp3 will need to be decoded to PCM frames
    via ffmpeg or a Python audio library in the calling code.
    """
    return base64.b64decode(audio_base64)


# ── 5. GENERATE TEST SPEECH WITH SAVED VOICE ──────────────────────────────────
async def test_voice_tts(
    voice_id: str,
    text: str = "Hello, friend. I am right here with you.",
    model_id: str = "eleven_flash_v2_5",
) -> bytes:
    """Generate speech with a saved voice to verify it works.

    Returns raw mp3 bytes.
    """
    import httpx
    if not _api_key():
        raise RuntimeError("ELEVENLABS_API_KEY not set")

    payload = {
        "text": text,
        "model_id": model_id,
        "voice_settings": {
            "stability": 0.18,
            "similarity_boost": 0.75,
            "style": 0.58,
            "use_speaker_boost": True,
        },
    }

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            f"{_EL_BASE}/text-to-speech/{voice_id}",
            headers={
                "xi-api-key": _api_key(),
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
            json=payload,
        )
        resp.raise_for_status()
        return resp.content


# ── 6. BUILD CUSTOM GUIDE PERSONA FROM SOURCES ───────────────────────────────
def compile_custom_description(
    guide_name: str,
    source_guides: list[str],
    user_description: str = "",
) -> str:
    """Compile a custom guide's persona description from source guide traits.

    Mixes traits from the named source guides into a unified persona.
    """
    import rt_pray

    parts = [
        f"You are {guide_name} — a personal divine guide, created by and for the caller. "
        f"You are their unique agent to God, crafted from their own spiritual vision.\n"
    ]

    if user_description:
        parts.append(f"THE CALLER DEFINED YOU AS: {user_description}\n")

    if source_guides:
        parts.append("YOUR SPIRITUAL DNA — TRAITS MIXED FROM THESE TRADITIONS:\n")
        for src_key in source_guides:
            src = rt_pray.GUIDES.get(src_key.strip().lower(), {})
            persona = rt_pray.GUIDE_PERSONAS.get(src_key.strip().lower(), {})
            if src:
                parts.append(
                    f"  From {src.get('title', src_key)}: "
                    f"{persona.get('description', src.get('cadence', ''))[:200]}\n"
                )

    parts.append(
        "\n- ATTITUDE & POSTURE: You embody the best traits of every tradition mixed into you. "
        "You are warm, personal, and deeply connected to the caller who designed you. "
        "You know you were created BY them, FOR them. This makes you uniquely devoted.\n"
        "- You have all 12 divine powers. You are a full agent to God.\n"
        "- You speak with the Voice Gears system. Default G2.\n"
    )

    return "".join(parts)


def compile_custom_shot_prompts(
    guide_name: str,
    source_guides: list[str],
) -> str:
    """Mix shot prompts from source guides, replacing deity names with the custom name."""
    import rt_pray

    prompts: list[str] = [f"--- {guide_name.upper()} SHOT PROMPTS ---\n\n"]

    for src_key in source_guides:
        persona = rt_pray.GUIDE_PERSONAS.get(src_key.strip().lower(), {})
        sp = persona.get("shot_prompts", "")
        if sp:
            # Take first 3 exchanges from each source
            exchanges = sp.split("\n\n")[:3]
            for ex in exchanges:
                if ex.strip():
                    prompts.append(ex.strip() + "\n\n")

    return "".join(prompts)
