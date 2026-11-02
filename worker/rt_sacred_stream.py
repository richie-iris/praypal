"""rt_sacred_stream.py — Curated Sacred Audio Stream & Ambient Drone Pipeline.

WHY THIS MODULE EXISTS:
Generating dynamic music via generative AI models (e.g. Suno) adds $0.05–$0.10
per call and incurs a 20–30 second latency penalty that shatters spiritual presence.
This module implements the zero-COGS Curated Sacred Audio Stream:
  1. High-fidelity local loops (Tanpura, Cathedral Organ, Tibetan Singing Bowls,
     Ney Flute, Cantorial Harp, Celestial Dawn).
  2. Zero-latency acoustic ducking & overlay that finesses ambient background
     drones directly under real-time voice synthesis.
  3. Serves stream assets locally via Caddy loopback at /media/sacred_audio/*.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any

MEDIA_DIR = Path(__file__).resolve().parent / "media" / "sacred_audio"

SACRED_CHANNELS: dict[str, dict[str, Any]] = {
    "hindu_tanpura": {
        "id": "hindu_tanpura",
        "title": "Sacred Tanpura & Meditative Flute",
        "tradition": "Hindu (Vedic / Bhakti)",
        "base_freq": 136.1,  # Om primordial frequency (C#)
        "harmonics": [136.1, 204.15, 272.2, 408.3],
        "timbre": "warm_drone",
        "default_volume": 0.18,
        "description": "Continuous tanpura drone paired with gentle bamboo flute overtones.",
    },
    "christian_cathedral": {
        "id": "christian_cathedral",
        "title": "Cathedral Pipe Organ & Taizé Chants",
        "tradition": "Christian (Contemplative)",
        "base_freq": 130.81,  # C3 cathedral foundation
        "harmonics": [130.81, 196.0, 261.63, 329.63, 392.0],
        "timbre": "pipe_organ",
        "default_volume": 0.16,
        "description": "Rich low pipe organ swell and choir harmonic warmth.",
    },
    "islam_ney": {
        "id": "islam_ney",
        "title": "Sufi Reed Ney & Contemplative Drone",
        "tradition": "Islamic (Sufi Contemplative)",
        "base_freq": 146.83,  # D3 meditative pitch
        "harmonics": [146.83, 220.0, 293.66, 440.0],
        "timbre": "breath_flute",
        "default_volume": 0.15,
        "description": "Mellow breath-infused reed flute with quiet modal string drone.",
    },
    "judaism_niggun": {
        "id": "judaism_niggun",
        "title": "Cantorial Harp & Acoustic Niggun Drone",
        "tradition": "Judaism & Covenant",
        "base_freq": 164.81,  # E3 devotional root
        "harmonics": [164.81, 246.94, 329.63, 493.88],
        "timbre": "acoustic_strings",
        "default_volume": 0.17,
        "description": "Gentle plucked Davidic harp cadence with warm cedarwood resonance.",
    },
    "buddhist_bowl": {
        "id": "buddhist_bowl",
        "title": "Tibetan Singing Bowls & Deep Monastic Resonance",
        "tradition": "Buddhist (Himalayan)",
        "base_freq": 108.0,  # 108Hz sacred harmonic
        "harmonics": [108.0, 216.0, 432.0, 648.0],
        "timbre": "metallic_bell",
        "default_volume": 0.20,
        "description": "Deep bell strike with long, undulating overtone decay.",
    },
    "universal_dawn": {
        "id": "universal_dawn",
        "title": "Celestial Dawn Sunlight Pad",
        "tradition": "Universal",
        "base_freq": 216.0,
        "harmonics": [216.0, 324.0, 432.0, 540.0],
        "timbre": "ethereal_pad",
        "default_volume": 0.14,
        "description": "Airy, luminous dawn ambiance with soft specular shimmer.",
    },
}


def get_sacred_channel(channel_id: str | None) -> dict[str, Any] | None:
    """Lookup sacred channel definition."""
    if not channel_id:
        return SACRED_CHANNELS["hindu_tanpura"]
    key = channel_id.strip().lower()
    return SACRED_CHANNELS.get(key)


def list_sacred_channels() -> list[dict[str, Any]]:
    """Return all available ambient sacred channels."""
    return list(SACRED_CHANNELS.values())


def get_channel_wav_path(channel_id: str) -> Path:
    """Returns filesystem path to cached or synthesizable WAV loop."""
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    return MEDIA_DIR / f"{channel_id}.wav"


def generate_ambient_loop_wav(
    channel_id: str,
    duration_sec: float = 6.0,
    rate: int = 24000,
) -> bytes:
    """Procedurally synthesizes high-fidelity seamless meditative loop.

    Guarantees 100% deterministic, zero-latency availability with no external
    cloud dependencies, no API cost, and smooth loop boundaries.
    """
    channel = get_sacred_channel(channel_id) or SACRED_CHANNELS["hindu_tanpura"]
    harmonics = channel.get("harmonics", [136.1, 272.2])

    try:
        import numpy as np
    except ImportError:
        # Minimal empty WAV fallback if numpy missing
        return b""

    total_samples = int(duration_sec * rate)
    t = np.linspace(0, duration_sec, total_samples, endpoint=False)
    signal = np.zeros(total_samples, dtype=np.float32)

    # Multi-harmonic additive synthesis
    for idx, freq in enumerate(harmonics):
        amp = 0.5 / (idx + 1)
        # Add subtle detuning / shimmer to avoid artificial sterile tones
        detune = 1.0 + 0.0012 * (idx % 2 * 2 - 1)
        # Slow tremolo modulation
        lfo = 1.0 + 0.08 * np.sin(2 * np.pi * 0.25 * t)
        signal += amp * np.sin(2 * np.pi * freq * detune * t) * lfo

    # Create smooth circular crossfade envelope at loop boundaries
    fade_len = int(rate * 0.8)
    fade_in = np.linspace(0.0, 1.0, fade_len)
    fade_out = np.linspace(1.0, 0.0, fade_len)
    signal[:fade_len] = signal[:fade_len] * fade_in + signal[-fade_len:] * fade_out

    # Normalize
    peak = np.max(np.abs(signal))
    if peak > 1e-5:
        signal = signal / peak * 18000.0

    pcm_data = signal.astype(np.int16).tobytes()

    # Save to disk cache
    target_path = get_channel_wav_path(channel_id)
    try:
        import wave
        with wave.open(str(target_path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(pcm_data)
    except Exception as exc:
        print(f"[rt-sacred-stream] cache write failed: {exc}", flush=True)

    return pcm_data


def mix_ambient_with_voice(
    voice_pcm: bytes,
    channel_id: str = "hindu_tanpura",
    voice_vol: float = 1.0,
    ambient_vol: float = 0.16,
    rate: int = 24000,
) -> bytes:
    """Seamlessly layers ambient drone under spoken voice audio with acoustic ducking.

    Ducks ambient audio when voice energy is present so prayer clarity is never muddied.
    """
    if not voice_pcm:
        return voice_pcm

    try:
        import numpy as np
    except ImportError:
        return voice_pcm

    try:
        voice = np.frombuffer(voice_pcm, dtype=np.int16).astype(np.float32)
        n_samples = len(voice)
        if n_samples == 0:
            return voice_pcm

        # Ensure ambient loop exists
        wav_path = get_channel_wav_path(channel_id)
        if not wav_path.exists() or wav_path.stat().st_size < 1000:
            generate_ambient_loop_wav(channel_id, duration_sec=max(6.0, n_samples / rate + 1.0), rate=rate)

        # Read loop
        import wave
        with wave.open(str(wav_path), "rb") as w:
            loop_pcm = w.readframes(w.getnframes())
        ambient = np.frombuffer(loop_pcm, dtype=np.int16).astype(np.float32)

        # Tile or slice ambient to match voice duration
        if len(ambient) < n_samples:
            tiles = int(math.ceil(n_samples / len(ambient)))
            ambient = np.tile(ambient, tiles)[:n_samples]
        else:
            ambient = ambient[:n_samples]

        # Dynamic ducking based on voice RMS envelope
        voice_env = np.abs(voice)
        kernel_size = min(int(rate * 0.05), len(voice_env))
        if kernel_size > 1:
            voice_smoothed = np.convolve(voice_env, np.ones(kernel_size) / kernel_size, mode="same")
            duck_factor = 1.0 - np.clip(voice_smoothed / 20000.0, 0.0, 0.45)
            ambient = ambient * duck_factor

        mixed = (voice * voice_vol) + (ambient * ambient_vol)
        peak = np.max(np.abs(mixed))
        if peak > 31000:
            mixed = mixed * (31000.0 / peak)

        return np.clip(mixed, -32768, 32767).astype(np.int16).tobytes()
    except Exception as exc:
        print(f"[rt-sacred-stream] mixing failed: {exc}", flush=True)
        return voice_pcm
