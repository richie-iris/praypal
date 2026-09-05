"""rt_audio.py — Audio processing, earcons, WAV cache, and clip playback.

Extracts audio utilities and earcon sound triggers from agent.py.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import tempfile
import time
import urllib.request
import wave
from typing import Any

import rt_obs

_GREET_CACHE = os.getenv("RT_GREET_CACHE", os.path.join(tempfile.gettempdir(), "phone-pal-greet-cache"))
_GREET_TAG = "greet"

_EARCON_MIN_GAP_SAME = 2.5
_EARCON_MIN_GAP_ANY = 0.7
_EARCON_LAST: dict[str, dict[str, float]] = {}


def _playable_wav(path: str) -> bool:
    """True if `path` is a wav with audio in it, not a half-written stub."""
    try:
        if os.path.getsize(path) <= 44:
            return False
        with wave.open(path, "rb") as w:
            return w.getnframes() > 0
    except Exception as _exc:
        rt_obs.obs.caught("rt_audio._playable_wav", _exc)
        return False


def _write_wav_atomic(path: str, pcm: bytes, rate: int) -> None:
    """Write a mono 16-bit wav so readers only ever see the finished file."""
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with wave.open(tmp, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(pcm)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def _clip_wav(voice: str, text: str, tag: str = _GREET_TAG) -> str | None:
    """Path to a cached 24k mono wav of `text` rendered in `voice`."""
    try:
        import hashlib

        os.makedirs(_GREET_CACHE, exist_ok=True)
        thash = hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()[:8]  # cache key only
        path = os.path.join(_GREET_CACHE, f"{voice}-{tag}-{thash}.wav")
        if _playable_wav(path):
            return path
        body = {
            "contents": [{"parts": [{"text": f"Say exactly this and nothing else: {text}"}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}},
            },
        }
        last_err: Exception | None = None
        api_key = os.getenv("GOOGLE_API_KEY", "")
        for attempt in range(3):
            try:
                # Key rides in a header, never the URL (URLs end up in logs).
                req = urllib.request.Request(
                    url="https://generativelanguage.googleapis.com/v1beta/models/"
                        "gemini-2.5-flash-preview-tts:generateContent",
                    data=json.dumps(body).encode(),
                    headers={"Content-Type": "application/json",
                             "x-goog-api-key": api_key},
                )
                with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed https Google API URL above
                    d = json.load(resp)
                pcm = base64.b64decode(
                    d["candidates"][0]["content"]["parts"][0]["inlineData"]["data"]
                )
                _write_wav_atomic(path, pcm, 24000)
                return path
            except Exception as e:
                rt_obs.obs.caught("rt_audio._clip_wav", e)
                last_err = e
                time.sleep(0.4 * (attempt + 1))
        raise last_err or RuntimeError("render failed")
    except Exception as e:
        print(f"[rt] clip render failed after retries (non-fatal): {e}", flush=True)
        return None


def _get_chime_path() -> str | None:
    """Load the signature chime from the sounds/ directory."""
    try:
        candidates = [
            os.path.join(os.path.dirname(__file__), "sounds", "pal-chime.wav"),
            "/opt/phone-pal/realtime/sounds/pal-chime.wav",
        ]
        for path in candidates:
            if os.path.exists(path):
                return path
        print("[rt] chime not found (searched: " + ", ".join(candidates) + ")", flush=True)
        return None
    except Exception as e:
        print(f"[rt] chime path lookup failed (non-fatal): {e}", flush=True)
        return None


def _earcon_allowed(room: Any, name: str) -> bool:
    """True if this earcon may play now, given what just played on this call."""
    try:
        key = getattr(room, "name", None) or "default"
    except Exception as _exc:
        rt_obs.obs.caught("rt_audio._earcon_allowed", _exc)
        key = "default"
    now = time.time()
    seen = _EARCON_LAST.setdefault(key, {})
    if now - seen.get("__any__", 0.0) < _EARCON_MIN_GAP_ANY:
        return False
    if now - seen.get(name, 0.0) < _EARCON_MIN_GAP_SAME:
        return False
    seen[name] = now
    seen["__any__"] = now
    return True


async def _play_clip(
    room: Any,
    path: str,
    preroll: float,
    exclude_identity: str | None = None,
    set_subscription_fn: Any = None,
) -> None:
    """Play a wav audio clip into the LiveKit room track."""
    from livekit import rtc

    t0 = time.perf_counter()
    with wave.open(path, "rb") as w:
        rate, frames = w.getframerate(), w.readframes(w.getnframes())
    src = rtc.AudioSource(rate, 1)
    track = rtc.LocalAudioTrack.create_audio_track("rt-clip", src)
    pub = await room.local_participant.publish_track(
        track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    )
    if exclude_identity and set_subscription_fn:
        try:
            await set_subscription_fn(room.name, exclude_identity, [pub.sid], False)
        except Exception as e:
            print(f"[rt] clip suppressed — could not keep it private: {e}", flush=True)
            with contextlib.suppress(Exception):
                await room.local_participant.unpublish_track(pub.sid)
            return
    await asyncio.sleep(preroll)
    n = rate // 50
    silence = bytes(n * 2)

    t_start = time.perf_counter()
    with contextlib.suppress(Exception):
        rt_obs.obs.event("audio.cue_latency", name=os.path.basename(path),
                         ms=round((t_start - t0) * 1000, 1))
    frame_count = 0

    for _ in range(15):
        await src.capture_frame(rtc.AudioFrame(silence, rate, 1, n))
        frame_count += 1
        t_target = t_start + (frame_count * 0.02)
        t_sleep = t_target - time.perf_counter()
        if t_sleep > 0:
            await asyncio.sleep(t_sleep)

    for i in range(0, len(frames) - n * 2 + 1, n * 2):
        await src.capture_frame(rtc.AudioFrame(frames[i : i + n * 2], rate, 1, n))
        frame_count += 1
        t_target = t_start + (frame_count * 0.02)
        t_sleep = t_target - time.perf_counter()
        if t_sleep > 0:
            await asyncio.sleep(t_sleep)

    await asyncio.sleep(0.1)
    with contextlib.suppress(Exception):
        await room.local_participant.unpublish_track(pub.sid)


def _trigger_sound(room: Any, sound_name: str) -> None:
    """Play an earcon into the call."""
    try:
        path = os.path.join(os.path.dirname(__file__), "sounds", f"{sound_name.strip().lower()}.wav")
        if not room:
            return
        if not os.path.exists(path):
            print(f"[rt-sound] no such sound: {sound_name!r}", flush=True)
            return
        if not _earcon_allowed(room, sound_name.strip().lower()):
            return
        asyncio.create_task(_play_clip(room, path, preroll=0.05))
    except Exception as e:
        rt_obs.obs.caught("rt_audio._trigger_sound", e)
        print(f"[rt-sound] _trigger_sound failed (non-fatal): {e}", flush=True)
