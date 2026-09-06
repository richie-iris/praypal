"""
Phone-Pal Realtime Worker — Gemini Live Speech-to-Speech Agent
=========================================================

Realtime speech-to-speech agent powered by Gemini 3.1 Flash Live.
Handles WebRTC audio, server-side VAD, native turn-taking, and
autonomous database/memory retrieval.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import inspect
import json
import os
import re
import sys
import tempfile
import threading
import urllib.parse
import urllib.request
import wave
import time

from typing import Any
from dotenv import load_dotenv
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    RoomInputOptions,
    RoomOutputOptions,
    RunContext,
    WorkerOptions,
    cli,
    function_tool,
    metrics,
)
from livekit import rtc

# LK_GOOGLE_DEBUG="" is not the same as LK_GOOGLE_DEBUG unset. The plugin runs
# int(os.getenv("LK_GOOGLE_DEBUG", 0)) at import time, the default applies only
# when the key is absent, and int("") raises — so a worker.env rendered from
# deploy/worker.env.example, which ships the key with an empty value, killed the
# container before a line of this file ran. The whole lane crash-looped on a
# debug switch nobody had turned on (2026-09-03, first boot of trial-pal; dev had
# survived only because someone set it to 1 by hand, months earlier).
# Blank means unset, so make it unset before the import reads it.
if not (os.getenv("LK_GOOGLE_DEBUG") or "").strip():
    os.environ.pop("LK_GOOGLE_DEBUG", None)

from livekit.plugins import google

import rt_prefs
import rt_capabilities
import rt_trial
import rt_pray
import rt_obs
import rt_shield
import rt_health

rt_prefs.ACTOR = "live"

load_dotenv(".env.local")
load_dotenv(".env")

AGENT_NAME = os.getenv("AGENT_NAME", "phone-pal-dev")

# LK_GOOGLE_DEBUG=1 tells the Google plugin to log the whole Gemini exchange,
# but it logs it at DEBUG, and nothing was raising that logger above INFO. The
# flag was set on the box for an entire debugging session and produced zero
# lines — present, and doing nothing. Setting the variable has to be enough.
if os.getenv("LK_GOOGLE_DEBUG", "").strip() in ("1", "true", "yes"):
    import logging as _logging
    for _name in ("livekit.plugins.google", "livekit.agents"):
        _logging.getLogger(_name).setLevel(_logging.DEBUG)
    _logging.getLogger().setLevel(_logging.DEBUG)
    print("[rt] LK_GOOGLE_DEBUG on — full Gemini exchange will be logged", flush=True)


try:
    from config import PROD_AGENT_NAMES as _PROD_AGENT_NAMES
except Exception:  # config predates the constant — the lane check must still run
    _PROD_AGENT_NAMES = ("iris-phone", "phone-pal-prod")


def _pepper_ok(value: str | None) -> bool:
    """config.pepper_ok when it exists; the same floor inline until it lands.

    A pepper that is a comment, a placeholder or a handful of characters is
    not a pepper — the hash it produces reverses as fast as an unpeppered one.
    """
    try:
        import config as _cfg
        fn = getattr(_cfg, "pepper_ok", None)
    except Exception:
        fn = None
    if fn is not None:
        return bool(fn(value))
    v = (value or "").strip()
    if not v or v.startswith("#") or len(v) < 16:
        return False
    return not any(w in v.lower() for w in ("replace", "example", "changeme", "todo", "xxxx"))


def _assert_lane_is_declared() -> None:
    """A production agent name is allowed — but only alongside a lane declared to match it.

    This used to be a blocklist: registering under a prod name raised at import,
    so the worker could never ship to the customers it was written for. But the
    name was never the hazard. What puts a development box on a paying caller's
    line is pointing at a development DATABASE and NUMBER, and the name is only a
    proxy for that. So assert the thing itself.

    RT_ALLOWED_SUPABASE_REF defaults to the dev and test refs (rt_prefs), which is
    the right default for a laptop and the wrong one for production — left unset, a
    prod-named worker would answer real callers out of the dev database. Requiring
    it to be declared, and to match the URL actually configured, is what the old
    blocklist was reaching for.
    """
    if AGENT_NAME not in _PROD_AGENT_NAMES:
        return
    declared = os.getenv("RT_ALLOWED_SUPABASE_REF", "").strip()
    if not declared:
        raise RuntimeError(
            f"{AGENT_NAME} requires RT_ALLOWED_SUPABASE_REF to name its own project ref "
            "— unset, this worker falls back to the dev/test refs and would answer real "
            "callers out of a development database")
    refs = {r.strip() for r in declared.split(",") if r.strip()}
    host = (urllib.parse.urlsplit(os.getenv("SUPABASE_URL", "")).hostname or "").lower()
    if not any(host == f"{ref}.supabase.co" for ref in refs):
        raise RuntimeError(
            f"{AGENT_NAME} points at SUPABASE_URL host {host!r}, which is not one of the "
            f"declared refs {sorted(refs)} — the lane is not internally consistent")
    if not os.getenv("RT_PUBLIC_NUMBER", "").strip():
        raise RuntimeError(
            f"{AGENT_NAME} requires RT_PUBLIC_NUMBER to be set explicitly — the default is "
            "the dev line, and she would tell customers to call it back")
    # An unpeppered SHA-256 of a phone number reverses in minutes. On a prod
    # lane the pepper is not optional, and neither is the switch that demands it.
    if os.getenv("RT_REQUIRE_PEPPER", "").strip().lower() in ("0", "false", "no", "off"):
        raise RuntimeError(
            f"{AGENT_NAME} has RT_REQUIRE_PEPPER switched off explicitly — a prod lane "
            "may not opt out of the pepper requirement")
    # Non-empty is not enough: a placeholder or a short string is no pepper.
    if not _pepper_ok(os.getenv("RT_PHONE_HASH_PEPPER")):
        raise RuntimeError(
            f"{AGENT_NAME} requires a real RT_PHONE_HASH_PEPPER (16+ chars, not a "
            "placeholder) — real callers' numbers must never be stored as unpeppered hashes")


_assert_lane_is_declared()

_PROBE_ROOM_PREFIXES = ("keepwarm", "watchdog-", "readiness-probe")

GEMINI_LIVE_RATE = int(os.getenv("GEMINI_LIVE_RATE", "24000"))

DEFAULT_GEMINI_MODEL = os.getenv("GEMINI_LIVE_MODEL", "gemini-3.1-flash-live-preview").strip()


def _log_turn(role: str, text: str) -> None:
    """Echo one transcript turn to stdout — only when RT_LOG_TRANSCRIPT=1.

    Plain print, no rt_obs event: what a caller says on the phone must not
    land in the telemetry stream by default.
    """
    if os.getenv("RT_LOG_TRANSCRIPT") != "1":
        return
    tag = "caller:" if role == "caller" else "agent :"
    print(f"[rt] {tag} {text!r}", flush=True)


def _log_pii(tag: str, text: str) -> None:
    """Print `tag` always; `text` (caller speech, addresses, stored detail) only
    when RT_LOG_TRANSCRIPT=1. Same gate as _log_turn, same reason."""
    if os.getenv("RT_LOG_TRANSCRIPT") == "1":
        print(f"{tag} {text!r}", flush=True)
    else:
        print(tag, flush=True)


def _mask_e164(e164: str | None) -> str:
    """'+19174030642' → '***0642' — enough to correlate a log, not to dial."""
    digits = re.sub(r"\D", "", e164 or "")
    return f"***{digits[-4:]}" if digits else "(none)"


def _startup_rpc_probe() -> None:
    """Say once, at worker boot, whether the caller database is actually reachable.

    Read-only by construction: the only RPC it may call is a plain SELECT.
    Nothing here may ever mutate — this runs on every boot, every deploy and
    every test run.

    Runs from __main__ only. The module is re-executed in every spawned job
    process, and a network round trip there is charged against the framework's
    process-initialization timeout.
    """
    if rt_prefs._db() is None:
        print("[rt-startup] ✗ no SUPABASE_URL / service key — nothing will be "
              "remembered this run", flush=True)
        return
    try:
        rows = rt_prefs._req("POST", "rpc/rt_get_all_callers", {})
        if rows is None:
            print("[rt-startup] ✗ caller database did not answer — memory is off", flush=True)
        else:
            print("[rt-startup] ✓ caller database reachable (read-only probe)", flush=True)
    except Exception as e:
        if "404" in str(e) or "PGRST202" in str(e):
            print("[rt-startup] ✗ RPCs missing — apply sql/ with sql_push.py", flush=True)
        else:
            print(f"[rt-startup] ✗ probe failed ({e})", flush=True)


RT_CTX_MIN_HISTORY = int(os.getenv("RT_CTX_MIN_HISTORY", "1700"))

def _tool_decl_tokens() -> int:
    """Measured, not guessed. Every tool docstring ships in the declarations on
    every turn, and this number sizes the compression floor — a hardcoded 953
    understated the real surface by 353 tokens, so the floor was set below what
    the turn actually costs and a "reasonable" target could still evict the
    whole conversation. Computed once at import, from the docstrings themselves,
    so it can never drift from them again."""
    import inspect
    try:
        total = 0
        for name in _TOOL_NAMES:
            fn = getattr(RtAgent, name, None)
            doc = inspect.getdoc(getattr(fn, "__wrapped__", fn)) if fn else None
            total += len(doc or "")
        return (total // 4) or 1300
    except Exception as _exc:
        rt_obs.obs.caught("agent._tool_decl_tokens", _exc)
        return 1300


_TOOL_NAMES = (
    "db_tool", "web_search", "recall_earlier", "find_number", "bridge_call",
    "press_keys", "listen_only", "end_bridge", "end_call",
    "save_email", "send_email", "send_calendar_invite", "send_sms",
    "schedule_reminder_call",
)
_CALLER_BLOCK_TOKENS = 325


def _prompt_floor_tokens() -> int:
    """The context every turn carries before a single word of conversation."""
    try:
        import rt_hydrator
        return len(rt_hydrator.STENCIL_TEMPLATE) // 4 + _tool_decl_tokens() + _CALLER_BLOCK_TOKENS
    except Exception as _exc:
        rt_obs.obs.caught("agent._prompt_floor_tokens", _exc)
        return _tool_decl_tokens() + _CALLER_BLOCK_TOKENS


def _fit_context_window(trigger: int, target: int) -> tuple[int, int, str]:
    """Keep the window above the prompt floor, raising it when it is set too low.

    Compression caps TOTAL context, and the system prompt is the floor it cannot
    go under — so a target near that floor evicts the entire conversation on
    every compression while looking like a perfectly reasonable number. There is
    no error when that happens; she simply stops remembering what was just said.
    A target that leaves no room is not a preference to honor, it is a
    misconfiguration, and the prompt can grow at any time and create one.
    """
    floor = _prompt_floor_tokens()
    want_target = max(target, floor + RT_CTX_MIN_HISTORY)
    want_trigger = max(trigger, want_target + 2000)
    if want_target == target and want_trigger == trigger:
        return trigger, target, ""
    return want_trigger, want_target, (
        f"prompt floor is ~{floor} tokens and target {target} left only "
        f"{max(target - floor, 0)} for the conversation")


def _known_voices() -> set[str]:
    """The 30 prebuilt voice names, from voices.json.

    That file has sat in the repo describing every voice — gender, age, pitch —
    and NOTHING has ever read it. Meanwhile a typo in GEMINI_LIVE_VOICE fails
    deep inside the API on the first call, as an opaque error, on a line someone
    has already picked up. Same data, now load-bearing.
    """
    try:
        with open(os.path.join(os.path.dirname(__file__), "voices.json")) as fh:
            data = json.load(fh)
        return {v["name"] for v in data.get("voices", data) if isinstance(v, dict) and v.get("name")}
    except Exception as _exc:
        rt_obs.obs.caught("agent._known_voices", _exc)
        return set()


def make_realtime_model(voice_override: str | None = None) -> google.realtime.RealtimeModel:
    """Instantiate the Gemini RealtimeModel with configured voice and context compression."""
    voice = (voice_override or os.getenv("GEMINI_LIVE_VOICE", "Aoede")).strip()
    _known = _known_voices()
    if not _known:
        _known = {"Aoede", "Achernar", "Callirrhoe", "Puck", "Charon", "Fenrir", "Kore", "Zephyr"}
    if voice not in _known:
        print(f"[rt] voice {voice!r} is not a valid Gemini Live voice — falling back to Aoede", flush=True)
        voice = "Aoede"
    mid = os.getenv("GEMINI_LIVE_MODEL", "").strip() or DEFAULT_GEMINI_MODEL

    lang = os.getenv("RT_LANGUAGE", "en-US").strip()
    kwargs = {
        "model": mid,
        "voice": voice,
        "language": lang,
    }
    with contextlib.suppress(Exception):
        from google.genai import types as _gt
        kwargs["input_audio_transcription"] = _gt.AudioTranscriptionConfig(
            language_codes=[lang])
    if os.getenv("GEMINI_LIVE_TEMPERATURE"):
        kwargs["temperature"] = float(os.getenv("GEMINI_LIVE_TEMPERATURE"))

    tb = os.getenv("RT_THINKING_BUDGET", "").strip()
    if tb:
        from google.genai import types as gtypes

        kwargs["thinking_config"] = gtypes.ThinkingConfig(thinking_budget=int(tb))

    trig = os.getenv("RT_CTX_TRIGGER_TOKENS", "6000").strip()
    if trig:
        from google.genai import types as gtypes

        target = os.getenv("RT_CTX_TARGET_TOKENS", "4000").strip()
        trig_n = int(trig)
        target_n = int(target) if target else 0
        if target_n:
            trig_n, target_n, raised = _fit_context_window(trig_n, target_n)
            if raised:
                print(f"[rt] context window raised to trigger={trig_n} target={target_n}: "
                      f"{raised}", flush=True)
        kwargs["context_window_compression"] = gtypes.ContextWindowCompressionConfig(
            trigger_tokens=trig_n,
            sliding_window=gtypes.SlidingWindow(
                target_tokens=target_n or None),
        )

        print(f"[rt] context compression ON trigger={trig_n} "
              f"target={target_n or 'trigger/2'} "
              f"(room for ~{max(target_n - _prompt_floor_tokens(), 0)} tokens of conversation)",
              flush=True)

    return google.realtime.RealtimeModel(**kwargs)


_GREET_CACHE = os.path.join(tempfile.gettempdir(), "rt-greet")
# The cache tag every greeting writer and the one reader must agree on.
# It was written four places and read in a fifth that spelled it
# differently, so all four warmed clips were invisible. One name now.
_GREET_TAG = "greet"


def _playable_wav(path: str) -> bool:
    """True if `path` is a wav with audio in it, not a half-written stub.

    A cache hit is trusted for the life of the container, so a file truncated by
    a process that died mid-write would leave a caller in silence on every call
    from then on. Cheap to open; only ever called before a network render.
    """
    try:
        if os.path.getsize(path) <= 44:
            return False
        with wave.open(path, "rb") as w:
            return w.getnframes() > 0
    except Exception as _exc:
        rt_obs.obs.caught("agent._playable_wav", _exc)
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


def _clip_wav(voice: str, text: str, tag: str) -> str | None:
    """Path to a cached 24k mono wav of `text` rendered in `voice`."""
    try:
        import hashlib

        os.makedirs(_GREET_CACHE, exist_ok=True)
        thash = hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()[:8]  # cache key only
        pray_pfx = "heaven-" if rt_pray.is_pray_lane() else ""
        path = os.path.join(_GREET_CACHE, f"{voice}-{pray_pfx}{tag}-{thash}.wav")
        if _playable_wav(path):
            return path
        if rt_pray.is_pray_lane():
            el_pcm = rt_pray.render_guide_clip_pcm(text, voice=voice)
            if el_pcm:
                pcm = rt_pray.apply_heavenly_sound_profile(el_pcm, 24000, voice=voice)
                _write_wav_atomic(path, pcm, 24000)
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
                # Key rides in a header: query strings land in proxy and
                # access logs, headers do not.
                req = urllib.request.Request(
                    url="https://generativelanguage.googleapis.com/v1beta/models/"
                        "gemini-2.5-flash-preview-tts:generateContent",
                    data=json.dumps(body).encode(),
                    headers={"Content-Type": "application/json",
                             "x-goog-api-key": api_key},
                )
                d = json.load(urllib.request.urlopen(req, timeout=30))  # noqa: S310 - fixed https Google API URL above
                pcm = base64.b64decode(
                    d["candidates"][0]["content"]["parts"][0]["inlineData"]["data"])
                if rt_pray.is_pray_lane():
                    pcm = rt_pray.apply_heavenly_sound_profile(pcm, 24000, voice=voice)
                _write_wav_atomic(path, pcm, 24000)
                return path
            except Exception as e:
                rt_obs.obs.caught("agent._clip_wav", e)
                last_err = e

                time.sleep(0.4 * (attempt + 1))
        raise last_err or RuntimeError("render failed")
    except Exception as e:
        print(f"[rt] clip render failed after retries (non-fatal): {e}", flush=True)
        return None


def _get_chime_path() -> str | None:
    """Load the signature chime from the sounds/ directory."""
    try:
        if rt_pray.is_pray_lane():
            chime = rt_pray.get_heavenly_chime_path()
            if chime and os.path.exists(chime):
                return chime
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


async def _play_clip(room: rtc.Room, path: str, preroll: float,
                     exclude_identity: str | None = None,
                     repeat: int = 1,
                     stop_event: asyncio.Event | None = None) -> None:
    """Play a wav audio clip into the room track.

    exclude_identity: a participant who must NOT hear this clip (the bridged
    stranger, so a private cue to the caller stays private).
    repeat: number of times to loop the clip (e.g. for thinking shimmer).
    stop_event: if set, ceases playback early when the background task finishes.
    """
    with wave.open(path, "rb") as w:
        rate, frames = w.getframerate(), w.readframes(w.getnframes())
    src = rtc.AudioSource(rate, 1)
    track = rtc.LocalAudioTrack.create_audio_track("rt-clip", src)
    pub = await room.local_participant.publish_track(
        track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    )
    if exclude_identity:
        try:
            await _set_subscription(room.name, exclude_identity, [pub.sid], False)
        except Exception as e:
            print(f"[rt] clip suppressed — could not keep it private: {e}", flush=True)
            with contextlib.suppress(Exception):
                await room.local_participant.unpublish_track(pub.sid)
            return
    await asyncio.sleep(preroll)
    n = rate // 50
    silence = bytes(n * 2)

    t_start = time.perf_counter()
    frame_count = 0

    for _ in range(15):
        if stop_event is not None and stop_event.is_set():
            break
        await src.capture_frame(rtc.AudioFrame(silence, rate, 1, n))
        frame_count += 1
        t_target = t_start + (frame_count * 0.02)
        t_sleep = t_target - time.perf_counter()
        if t_sleep > 0:
            await asyncio.sleep(t_sleep)

    for _rep in range(max(1, repeat)):
        if stop_event is not None and stop_event.is_set():
            break
        for i in range(0, len(frames) - n * 2 + 1, n * 2):
            if stop_event is not None and stop_event.is_set():
                break
            await src.capture_frame(rtc.AudioFrame(frames[i : i + n * 2], rate, 1, n))
            frame_count += 1
            t_target = t_start + (frame_count * 0.02)
            t_sleep = t_target - time.perf_counter()
            if t_sleep > 0:
                await asyncio.sleep(t_sleep)

    await asyncio.sleep(0.1)
    with contextlib.suppress(Exception):
        await room.local_participant.unpublish_track(pub.sid)


def prewarm(proc) -> None:
    """Start pre-rendering the generic greeting clips. Returns immediately.

    This runs as the framework's process-initialization function, and a process
    that has not reported ready inside initialize_process_timeout is killed. The
    three renders are network calls to the TTS API and take far longer than that
    on a cold container, where the cache directory is empty — so they belong on
    a thread, not in the caller's way. A call that arrives before they finish
    renders its own greeting on demand, bounded by RT_GREET_RENDER_TIMEOUT.
    """
    def _render() -> None:
        try:
            if rt_trial.is_trial_lane():
                v = os.getenv("GEMINI_LIVE_VOICE", "Aoede")
                _tg = rt_trial.greeting()
                if _tg:
                    _clip_wav(v, _tg, _GREET_TAG)
                return
            if rt_pray.is_pray_lane():
                with contextlib.suppress(Exception):
                    rt_pray.get_heavenly_chime_path()
                for g in rt_pray.GUIDES.values():
                    _gv = g.get("voice", "Puck")
                    _gt = g.get("greeting")
                    if _gt:
                        _clip_wav(_gv, _gt, _GREET_TAG)
                return

            v = os.getenv("GEMINI_LIVE_VOICE", "Aoede")
            # Every clip any caller can hear. All three pools are name-free, so
            # this is the whole greeting surface and it is rendered once.
            for t in _GREET_FIRST + _GREET_KNOWN:
                _clip_wav(v, t, _GREET_TAG)
            for k in range(1, len(_GREET_UNNAMED) + 1):
                _clip_wav(v, _greeting_text(None, None, k), _GREET_TAG)
        except Exception as e:
            print(f"[rt] prewarm greeting render failed (non-fatal): {e}", flush=True)

    threading.Thread(target=_render, name="rt-prewarm", daemon=True).start()


def _fact_spoken_by_caller(text: str, state: dict) -> bool:
    """True if the gist of `text` appears in a caller: line — their own words."""
    lines = state.get("transcript_lines") or []
    caller = " ".join(l.lower() for l in lines if l.lower().startswith("caller:"))
    if not caller:
        return False
    words = [w for w in re.findall(r"[a-z]{4,}", (text or "").lower())
             if w not in _COMMON_WORDS]
    if not words:
        return False
    hits = sum(1 for w in words if w in caller)
    return hits >= max(1, len(words) // 2)


_SEARCH_ECHO_WINDOW = 90.0


def _looks_like_search_echo(text: str, state: dict) -> bool:
    """True if `text` is largely lifted from something she just searched.

    Applies only for a short window after the search. Held for the whole call,
    it would mean one lookup in minute one makes every fact the caller
    volunteers afterwards need re-confirming before she can write it down.
    """
    recent = (state.get("recent_search") or "").lower()
    if not recent or len(text or "") < 12:
        return False
    at = state.get("recent_search_at")
    if at and time.time() - at > _SEARCH_ECHO_WINDOW:
        return False
    words = [w for w in re.findall(r"[a-z]{4,}", (text or "").lower())
             if w not in _COMMON_WORDS]
    if len(words) < 2:
        return False
    recent_words = set(re.findall(r"[a-z]{4,}", recent))
    hits = sum(1 for w in words if w in recent_words)
    return hits >= max(2, (len(words) * 2) // 3)


_BUDGET_CATEGORY = "daily_minutes"


def _daily_minutes_spent(caller_e164: str, bundle: dict | None = None) -> float:
    """Minutes this caller has already spent with Iris today (UTC).

    Pass the bundle the entrypoint already fetched. Without one this makes a
    blocking HTTP call, so never call it that way from the event loop.
    """
    import datetime as _dt
    h = rt_prefs.phone_hash(caller_e164 or "")
    if not h:
        return 0.0
    if bundle is None:
        try:
            bundle = rt_prefs._req("POST", "rpc/rt_get_caller_full_bundle", {"p_hash": h}) or {}
        except Exception as _exc:
            rt_obs.obs.caught("agent._daily_minutes_spent", _exc)
            return 0.0
    day = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    for s in (bundle.get("schemas") or []):
        if (s.get("category") or "").lower() == _BUDGET_CATEGORY:
            with contextlib.suppress(Exception):
                return float((json.loads(s.get("data_summary") or "{}")).get(day) or 0.0)
    return 0.0


def _over_daily_budget(caller_e164: str, bundle: dict | None = None) -> bool:
    cap = float(os.getenv("RT_DAILY_MINUTES_PER_CALLER", "90"))
    return _daily_minutes_spent(caller_e164, bundle) >= cap


def _record_call_minutes(caller_e164: str, started_at: float | None) -> None:
    """Add this call's minutes to today's running total (keeps ~3 days)."""
    import datetime as _dt
    h = rt_prefs.phone_hash(caller_e164 or "")
    if not h or not started_at:
        return
    mins = max(0.0, (_now() - started_at) / 60.0)
    day = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    try:
        bundle = rt_prefs._req("POST", "rpc/rt_get_caller_full_bundle", {"p_hash": h}) or {}
        cur = {}
        for s in (bundle.get("schemas") or []):
            if (s.get("category") or "").lower() == _BUDGET_CATEGORY:
                cur = json.loads(s.get("data_summary") or "{}"); break
        cur = {k: v for k, v in cur.items() if isinstance(k, str) and k >= day} if isinstance(cur, dict) else {}
        cur[day] = round(float(cur.get(day) or 0.0) + mins, 2)
        rt_prefs._req("POST", "rpc/rt_add_schema_entry", {
            "p_hash": h, "p_table": f"caller_{h[:8]}_{_BUDGET_CATEGORY}",
            "p_cat": _BUDGET_CATEGORY, "p_summary": json.dumps(cur)})
        print(f"[rt-budget] ***{caller_e164[-4:]} +{mins:.1f}min, {cur[day]:.1f}min today", flush=True)
    except Exception as e:
        print(f"[rt-budget] record failed (non-fatal): {e}", flush=True)


def _fallback_instructions() -> str:
    """The prompt she opens with when the database is too slow to wait for.

    Warm and safe, with no caller facts in it — better than silence on a line
    someone has just answered.
    """
    return (os.getenv("RT_SYSTEM_PROMPT", "") or
            "You are a warm computer voice companion for older adults — a friend "
            "on the phone who is glad they called. Speak in 1-3 short, unhurried "
            "sentences. You are a computer, never a person. Emergencies → 911; "
            "mental-health crisis → 988. If you seem not to remember something you "
            "should, say honestly that you're having trouble pulling it up right now "
            "and ask them to tell you again — never invent a memory."
            ) + _onboarding_block(list(_ONBOARDING_ORDER))


def _build_instructions(caller_e164: str | None = None, call_count: int = 0, prefetch_bundle: dict | None = None) -> tuple[str, str]:
    """Compile mission prompt stencil and dynamic caller context.

    prefetch_bundle: if provided, passed directly to the hydrator so it skips
    the rt_get_caller_full_bundle RPC call (already fetched in entrypoint).

    Returns (prompt_text, resolved_display_name).
    """
    # A trial lane is not a companion with different wallpaper. The stencil below
    # is 3,600 characters of shared history, a self, and a caller canvas, none of
    # which a trial participant has or should have — 07-privacy-and-verification
    # forbids the store this fills from. So the trial prompt is built here and the
    # hydrator is never called: the identity line, then the pinned governance,
    # and nothing else. governance_or_refuse() raises rather than returning an
    # ungoverned prompt, and the caller of this function lets that out.
    if rt_trial.is_trial_lane():
        _governance = rt_trial.governance_or_refuse()
        _prompt = f"{rt_trial.identity_line()}\n\n{_governance}"
        print(f"[rt-trial] governance prompt {len(_prompt)} chars for "
              f"agent={rt_trial.agent_name()} study={rt_trial.study() or 'any'}", flush=True)
        return _prompt, "the participant"

    if rt_pray.is_pray_lane():
        bundle = prefetch_bundle or {}
        caller_info = bundle.get("caller") or {}
        _guide_key = caller_info.get("active_guide") or os.getenv("PRAY_DEFAULT_GUIDE", "atrium")
        _tradition = caller_info.get("spiritual_tradition") or "universal"
        _comm_int = None
        if caller_e164:
            with contextlib.suppress(Exception):
                _comm_int = rt_pray.get_community_intention(rt_prefs.phone_hash(caller_e164))
        _prompt = rt_pray.build_system_prompt(
            guide_key=_guide_key,
            caller_tradition=_tradition,
            caller_info=caller_info,
            memories=bundle.get("memories") or [],
            intentions=bundle.get("intentions") or [],
            community_intention=_comm_int,
        )
        resolved_name = caller_info.get("display_name") or "the seeker"
        print(f"[rt-pray] sacred prompt {len(_prompt)} chars for "
              f"guide={_guide_key} agent={rt_pray.agent_name()} caller={resolved_name}", flush=True)
        return _prompt, resolved_name

    try:
        import rt_hydrator
        # What she was handed, before anything was built from it. Without this a
        # thin prompt is indistinguishable from a thin memory: you cannot tell
        # whether she forgot the caller or was never given him.
        with contextlib.suppress(Exception):
            _b = prefetch_bundle or {}
            rt_obs.obs.event("prompt.hydrate_in",
                             facts=len(_b.get("facts") or []),
                             schemas=len(_b.get("schemas") or []),
                             reminders=len(_b.get("reminders") or []),
                             source=("prefetch" if prefetch_bundle else "fetch"))
        hydrated, meta = rt_hydrator.discover_and_hydrate_prompt(
            caller_e164, prefetch_bundle=prefetch_bundle or {}
        )
        caller_data = meta.get("caller_data") or (prefetch_bundle.get("caller") if prefetch_bundle else {}) or {}
        raw_alias = (caller_data.get("agent_alias") or "").strip()
        agent_alias = "your companion" if (not raw_alias or raw_alias.lower() in ("iris", "your companion")) else raw_alias
        onb = meta.get("onboarding") or {}
        if not onb.get("done"):
            missing = [k for k in _ONBOARDING_ORDER if not onb.get(k)]
            hydrated += _onboarding_block(
                missing, alias=agent_alias,
                call_count=int(caller_data.get("call_count") or 0))
        resolved_name = caller_data.get("display_name") or "Friend"
        return hydrated, resolved_name
    except Exception as e:
        print(f"[rt] prompt hydration fallback ({e})", flush=True)
        return _fallback_instructions(), "Friend"


_ONBOARDING_ORDER = ("name", "intro", "ownership", "first_fact")

_ONBOARDING_STEP_TEXT = {
    "name": ('Get their name in the flow of talking, and make sure you have the spelling right — '
             'once, lightly, the way a friend would: "Richie — is that with an ie?" Only spell the '
             'whole thing out if it\'s unusual or you genuinely didn\'t catch it. Then save it with '
             'db_tool(action="name", item="<their name>") and just use it. If they ever correct you, '
             'take the correction gracefully and save it — no fuss, no apologizing twice.'),
    "intro": ("Somewhere in the conversation, let them know you're {alias} and you're theirs — "
              "they can call you anytime, about anything or nothing. One warm line, not a speech."),
    "ownership": ("When it fits naturally, mention ONE thing they can shape — that you'll remember "
                  "what they tell you, that they can set rules for you, or that they can even give "
                  "you a different name. One at a time, offered like a gift, never as a feature list."),
    "first_fact": "Draw out one personal thing (family, a pet, a hobby) and save it the moment it's shared, confirming in your own words.",
}


def _onboarding_block(missing: list[str], alias: str = "your companion",
                      call_count: int = 0) -> str:
    """Only the beats not yet completed — woven in over calls, never a checklist.

    One narrow exception to "at most ONE per call": a caller we have already
    spoken to and still cannot name. "Weave one in when it fits" is a fair
    instruction on a first call and a failing one on a sixth — with several
    beats open the model can always pick a different one, and across six real
    calls it never once picked this one. The greeting now asks them outright on
    every such call, so the answer is arriving whether or not the model chose
    the beat; all it has to do is catch what comes back and write it down.
    """
    if not missing:
        return ""
    steps = "\n".join(f"- {_ONBOARDING_STEP_TEXT[k].format(alias=alias)}"
                      for k in _ONBOARDING_ORDER if k in missing)
    catch_the_name = ""
    if "name" in missing and int(call_count or 0) >= 1:
        catch_the_name = (
            "\n\nYou have talked with them before and still have no name for them, so the line "
            "you already opened this call with asked for it out loud. Listen for the answer and "
            "save it the moment it comes with db_tool(action=\"name\", item=\"<their name>\"). "
            "Don't let the call end without it — but don't ask a second time either, they have "
            "been asked once already.")
    return ("\n\n# STILL TO COME (things you haven't gotten to with them yet)\n"
            "You are a friend on the phone FIRST — talk with them, follow what they care about, "
            "let the conversation breathe. These are not a script and not an intake form: weave "
            "in at most ONE of them per call, only when it fits, and never at the cost of "
            "actually listening.\n" + steps + catch_the_name)

# Greeting variants. These are rendered to audio and played as a clip, which is
# NOT interruptible — the caller cannot talk over one. So every line here is kept
# as short as it can be while still doing its job, and every line ends with a
# question, because the model will not speak first on the 3.1 live models and the
# caller needs an unmistakable signal that the turn is theirs.
#
# FIRST contact is the long one and cannot get much shorter: it is the only place
# the brand and the "you are talking to a computer" disclosure are guaranteed to
# be said, and that is a trust commitment, not a greeting flourish.
_GREET_FIRST: tuple[str, ...] = (
    "Hello there, this is Phone-Pal, your voice companion. I don't have a name yet. What'll you call me?",
    "Hello there, this is Phone-Pal, your voice companion. Who do I have the pleasure of speaking with?",
    "Hello there, this is Phone-Pal, your voice companion. What name would you like to give me?",
    "Hello there, this is Phone-Pal, your voice companion. What should I call you?",
)
# Spoken to someone we have met but still cannot name.
_GREET_UNNAMED: tuple[str, ...] = (
    "Hi! I'm {alias}. What's your name?",
    "Hello! I'm {alias}. May I have your name?",
    "Hi there, I'm {alias}. What's your name?",
    "I'm {alias}. What name do you go by?",
)
# Spoken to someone we know. These carry NO name and no alias, which is what
# makes them reusable: one rendered clip serves every caller, so it is warm in
# the cache before the phone rings instead of being synthesised while they wait.
# The name-bearing versions could never be prewarmed — each was unique to one
# person — so every known caller paid three to five seconds of silence for a
# personal touch they had already heard. She can use their name in her first
# real sentence, where it costs nothing.
_GREET_KNOWN: tuple[str, ...] = (
    "Hey! How are you?",
    "Hi there — how's your day?",
    "Hey, good to hear from you. What's new?",
    "Hi! How've you been?",
    "Hey there. What's going on?",
    "Hi — how are things?",
)


def _greeting_text(display_name: str | None, alias: str | None, call_count: int = 1,
                   seed: int = 0, pick: int | None = None, guide_key: str | None = None,
                   caller_info: dict | None = None) -> str:
    """Short, warm, and final — the clip cannot be interrupted, so every word costs.

    TWO INDEPENDENT FACTS decide the opening, and conflating them is what made a
    caller on visit #6 hear the same cold hello as a first-time stranger:
    whether SHE has been named, and whether WE know the caller. The old test was
    `not has_custom_alias or call_count == 0`, so every caller who simply never
    renamed her — the overwhelming majority — got the brand-and-disclosure line
    forever, no matter how much the database knew about them. Six calls and
    thirty-four facts in, she still opened by asking them to name her.

    Only genuine first contact (call 0) earns that line now. After that a caller
    we can name is greeted by it, and a caller we cannot is ASKED for it, out
    loud, on every single call — the greeting is deterministic audio, so this is
    the one channel that cannot decline to ask.

    `seed` varies which first-contact wording a given caller hears, since call
    count is always 0 there and would otherwise pin every stranger to one line.
    Pass something stable per caller so the same person hears the same opening.
    """
    # 01-identity-disclosure-and-recording: a trial lane says what it is at the
    # opening of every call, unprompted, and never claims to be a person or a
    # nurse. Every variant below is the companion's — "this is Phone-Pal, your
    # voice companion" is the sentence the trial-pal number answered with on its
    # very first call (2026-09-03). None of them are lawful on a trial lane.
    _trial = rt_trial.greeting()
    if _trial:
        return _trial

    _pray = rt_pray.greeting(guide_key, caller_info=caller_info)
    if _pray:
        return _pray

    raw_alias = (alias or "").strip()
    has_custom_alias = bool(raw_alias and raw_alias.lower() not in ("your companion", "iris", ""))
    alias_str = raw_alias if has_custom_alias else "your companion"
    name = (display_name or "").strip()
    known_caller = bool(name) and name != "Friend"
    n = int(call_count or 0)

    if n == 0:
        return _GREET_FIRST[int(seed or 0) % len(_GREET_FIRST)]
    if not known_caller:
        return _GREET_UNNAMED[n % len(_GREET_UNNAMED)].format(alias=alias_str)
    # The stored pick wins when the post-call worker chose one; otherwise rotate.
    idx = pick if pick is not None else n
    return _GREET_KNOWN[idx % len(_GREET_KNOWN)]


def _greeted_note(text: str) -> str:
    """Instruction suffix so the model never greets a second time."""
    return (f"\n\n(You already opened this call by saying: \"{text}\". Do not greet again — "
            f"wait for the caller and respond naturally to whatever they say.)")


_DB_CATEGORIES = {"family", "pets", "hobbies", "health", "vehicles", "work",
                  "preferences", "places", "finances", "general", "clarifications",
                  "credentials", "goals"}
_WIPE_WORDS = {"everything", "all", "wipe", "reset", "trash all notes and reset"}
# Mirrors the tuple rt_hydrator renders as LEARNED ROUTINES: anything written
# under these names becomes a standing instruction on every future call, so
# every path that writes them goes through the skill guards.
_ROUTINE_CATEGORIES = ("skills", "routines", "custom_skills")


def is_duplicate_agent_turn(text: str, prev: str | None, prev_at: float | None,
                           now: float, window: float = 12.0) -> bool:
    """Is this assistant item a repeat of the one just before it?

    Live proof, 2026-08-27: she said "Nelda, huh? I like it! I'll remember that
    for next time. What's on your mind today?" and then, as a separate item,
    "Nelda, huh?" — a truncated repeat of her own line. The caller's reaction was
    "Who the fuck?", and she had to apologise for startling them.

    The realtime SDK can surface the same utterance twice, the second one
    truncated. Nothing deduplicated assistant items: there was a guard for a
    greeting echo misattributed to the CALLER, and none for her repeating
    herself. The duplicate also reached the transcript, so post-call extraction
    read it as two separate things she said.

    A repeat is: identical, or one is a prefix of the other, within `window`
    seconds. Prefix rather than similarity because the failure is truncation,
    and two genuinely different sentences almost never prefix one another.
    """
    if not text or not prev or prev_at is None:
        return False
    if now - prev_at > window:
        return False
    a, b = text.strip().lower(), prev.strip().lower()
    if not a or not b:
        return False
    return a == b or a.startswith(b) or b.startswith(a)


def _trace(state, kind: str, name: str, detail: dict | None = None) -> None:
    """Record one call event. Never raises, never blocks the audio loop.

    A tool running counts as the call being alive. The silence monitor watches
    for a caller who has gone away, but it only ever saw spoken turns — so a
    lookup that takes half a minute, or a run of them, read as an empty line and
    she interrupted her own work to ask if anyone was there.
    """
    if kind == "tool" and state is not None:
        with contextlib.suppress(Exception):
            state["last_activity_time"] = _now()
    with contextlib.suppress(Exception):
        import rt_trace
        rt_trace.event(state or {}, kind, name, detail or {})
    # Both streams from one call site. rt_trace is the durable compliance record
    # (full content, in the database); this is the operational stream (sizes and
    # outcomes, on stdout, in real time). Wiring them together here rather than
    # at each of the eighteen call sites is what keeps them from drifting apart
    # — a traced event that never reached telemetry was invisible to anyone
    # watching the line rather than auditing it afterwards.
    with contextlib.suppress(Exception):
        d = detail or {}
        if kind == "tool":
            # Sizes, not content: tool arguments are caller speech and stored
            # detail. The full payload rides along only under the same gate as
            # the transcript itself; rt_trace already keeps it durably.
            full = os.getenv("RT_LOG_TRANSCRIPT") == "1"
            _args = {k: (v if full else len(str(v))) for k, v in d.items() if k != "result"} or None
            rt_obs.obs.event("tool.call", name=name, turn=(state or {}).get("turn_no"),
                             args=_args)
            if d.get("error"):
                rt_obs.obs.error("tool.failed", name=name, turn=(state or {}).get("turn_no"),
                                 err=str(d.get("error"))[:200])
            res = d.get("result")
            rt_obs.obs.event("tool.result", name=name, turn=(state or {}).get("turn_no"),
                             ok=not d.get("error"),
                             result=(res if full else len(str(res or ""))), args=_args)
        elif kind == "guard":
            rt_obs.obs.event("guard.decision", guard=name, allowed=False,
                             reason=d.get("reason") or d.get("why"))
        else:
            full = os.getenv("RT_LOG_TRANSCRIPT") == "1"
            # The dialed number is the third party's PII; stdout gets the
            # masked form unless the transcript gate is open (rt_trace keeps it).
            rt_obs.obs.event(f"{kind}.{name}", **{
                k: (_mask_e164(v) if k == "number" and not full else v)
                for k, v in d.items()
                if k in ("number", "mode", "who", "reason", "label", "chars", "lines")})


def _now() -> float:
    return time.time()


def _dur(seconds: float) -> Any:
    """protobuf Duration for the SIP request fields."""
    from google.protobuf.duration_pb2 import Duration
    d = Duration()
    d.FromSeconds(int(seconds))
    return d


async def _remove_participant(room_name: str, identity: str) -> None:
    """Hang up on one participant. Raises on failure — the caller decides what that means."""
    from livekit import api as _lkapi
    lkapi = _lkapi.LiveKitAPI()
    try:
        await lkapi.room.remove_participant(
            _lkapi.RoomParticipantIdentity(room=room_name, identity=identity))
    finally:
        with contextlib.suppress(Exception):
            await lkapi.aclose()


_BRIDGE_QUARANTINE_S = 3.0


def _bridge_quarantined(state: dict) -> bool:
    if state.get("bridge_active"):
        return True
    ended = state.get("bridge_ended_at") or 0.0
    return bool(ended) and (_now() - ended) < _BRIDGE_QUARANTINE_S


# ─── The sound profile ────────────────────────────────────────────────────────
# Earcons carry meaning, so they have to stay legible. Two rules, enforced here
# rather than trusted to call sites:
#
#   1. Never stack. Tool cues fired through create_task, so a turn that saved
#      three things played three page-flips ON TOP of each other — noise where a
#      single "I wrote that down" was intended.
#   2. Never repeat quickly. The same earcon twice inside a couple of seconds
#      reads as a malfunction, not as two events.
#
# A cue that is dropped is dropped silently. It is a courtesy, not information:
# nothing the caller needs may live only in a sound.
_EARCON_MIN_GAP_SAME = 2.5
_EARCON_MIN_GAP_ANY = 0.7
_EARCON_LAST: dict[str, dict[str, float]] = {}


def _earcon_allowed(room, name: str) -> bool:
    """True if this earcon may play now, given what just played on this call."""
    try:
        key = getattr(room, "name", None) or "default"
    except Exception as _exc:
        rt_obs.obs.caught("agent._earcon_allowed", _exc)
        key = "default"
    now = _now()
    seen = _EARCON_LAST.setdefault(key, {})
    if now - seen.get("__any__", 0.0) < _EARCON_MIN_GAP_ANY:
        return False
    if now - seen.get(name, 0.0) < _EARCON_MIN_GAP_SAME:
        return False
    seen[name] = now
    seen["__any__"] = now
    return True


def _spoken_local_time(when: str, e164: str | None = None) -> str:
    """The caller's wall-clock time for `when`, worded the way it is said aloud.

    The reminder tool used to hand the model back the raw ISO string it was given
    — usually UTC — and ask it to confirm "at [time]". On a live call that came
    out as "that'll be at 4 37 PM, your time" for a 12:36 PM local callback, and
    the caller had to correct her. Converting is not the model's job: it has no
    reliable clock and no reason to be doing arithmetic mid-sentence. Hand it the
    words to say.

    Falls back to the raw string, which is at least honest, rather than inventing
    a time that reads as confident and is wrong.
    """
    try:
        import rt_scheduler
        import rt_timezone
        from zoneinfo import ZoneInfo
        dt = rt_scheduler._parse_when(when, caller_e164=e164)
        zone, _known = rt_timezone.zone_or_default(
            e164, os.getenv("DEFAULT_TZ", "America/New_York"))
        tz = ZoneInfo(zone)
        local = dt.astimezone(tz) if dt.tzinfo else dt.replace(tzinfo=tz)
        hour = local.hour % 12 or 12
        ampm = "AM" if local.hour < 12 else "PM"
        return f"{hour}:{local.minute:02d} {ampm}"
    except Exception as e:
        rt_obs.obs.caught("agent._spoken_local_time", e, when=str(when)[:40])
        return str(when)


def _job_metadata(ctx) -> dict:
    """The dispatch metadata for this job, wherever the SDK happens to put it.

    Proven empty-handed on a live outbound call 2026-08-30: the scheduler set
    metadata on BOTH create_room and create_dispatch, and `ctx.room.metadata`
    was still "" at entrypoint. The reminder call went out, rang, and connected —
    but the worker never learned WHY it was calling. It recovered only because
    that particular reminder also happened to be a row in the database, so the
    hydrated prompt carried it. A reminder whose text is not also stored would
    have opened with a generic hello and no idea what it wanted.

    So all three sources are tried. Room metadata syncs asynchronously and can
    lose the race with the entrypoint; the job's own copy does not.
    """
    for raw in (getattr(getattr(ctx, "room", None), "metadata", None),
                getattr(getattr(ctx, "job", None), "metadata", None),
                getattr(getattr(getattr(ctx, "job", None), "room", None), "metadata", None)):
        if not raw:
            continue
        try:
            d = json.loads(raw)
            if isinstance(d, dict) and d:
                return d
        except Exception as _exc:
            rt_obs.obs.caught("agent._job_metadata", _exc)
    return {}


async def _seed_greeting(state: dict, timeout: float | None = None) -> bool:
    """Make the MODEL greet, by pushing a tiny "Hi." into the session's INPUT.

    The caller never hears it. The model hears someone saying hi and answers, so
    the greeting is ordinary model speech: INTERRUPTIBLE, in her live voice, and
    in her own context so she knows she greeted. A rendered clip is none of those
    — it is an audio track played to completion that the caller must sit through.

    It is done this way because of 3.1: generate_reply() and mid-session
    instruction updates are both no-ops there (the plugin sets mutable=False), so
    the only way to make her speak first is to give her something to answer.

    The caller's own microphone is muted for the duration. Proven necessary on a
    live call 2026-08-30: with the caller's audio flowing at the same time, the
    two streams interleaved, and the model's reply came back as server content
    the plugin discarded with "received server content but no active generation."
    She spoke into a closed pipe. Seeding is a turn, and a turn needs one speaker.

    Returns True only once she is ACTUALLY speaking. The caller hearing nothing
    is the entire failure mode, so nothing here reports success on having pushed
    the audio — only on her having answered.
    """
    model, ses = state.get("model"), state.get("session")
    if model is None:
        return False
    timeout = float(os.getenv("RT_GREET_SEED_TIMEOUT", "8") if timeout is None else timeout)
    muted = False
    try:
        path = await asyncio.to_thread(_clip_wav, "Puck",
                                       os.getenv("RT_GREET_SEED_TEXT", "Hi."), "seed")
        if not path:
            return False
        sessions = list(getattr(model, "_sessions", []))
        if not sessions:
            return False
        rs = sessions[-1]

        with contextlib.suppress(Exception):
            ses.input.set_audio_enabled(False)
            muted = True

        # Let the session settle. Pushing at t=0 produced an off-by-one reply
        # queue in prod — the model answered the seed with the reply meant for
        # the caller's first real turn, three runs in a row.
        await asyncio.sleep(float(os.getenv("RT_GREET_SEED_DELAY", "0.6")))

        with wave.open(path, "rb") as w:
            rate, frames = w.getframerate(), w.readframes(w.getnframes())
        n = rate // 50
        for i in range(0, len(frames) - n * 2 + 1, n * 2):
            rs.push_audio(rtc.AudioFrame(frames[i:i + n * 2], rate, 1, n))
            await asyncio.sleep(0.02)          # real-time pace, for server VAD
        silence = bytes(n * 2)
        for _ in range(40):                    # ~800ms of quiet closes the turn
            rs.push_audio(rtc.AudioFrame(silence, rate, 1, n))
            await asyncio.sleep(0.02)

        deadline = _now() + timeout
        while _now() < deadline:
            if state.get("agent_spoke") or state.get("_last_agent_text"):
                return True
            with contextlib.suppress(Exception):
                if getattr(ses, "agent_state", "listening") == "speaking":
                    return True
            await asyncio.sleep(0.1)
        rt_obs.obs.warn("greeting.seed_timeout", seconds=timeout)
        return False
    except Exception as e:
        rt_obs.obs.caught("agent._seed_greeting", e)
        print(f"[rt] greet seed failed: {e}", flush=True)
        return False
    finally:
        # The caller must get their microphone back no matter how this ended.
        if muted:
            with contextlib.suppress(Exception):
                ses.input.set_audio_enabled(True)


async def _cue(state: dict, name: str) -> None:
    """Play a call-state earcon to the CALLER only (never to the bridged party)."""
    room = state.get("room")
    if room is None:
        return
    path = os.path.join(os.path.dirname(__file__), "sounds", f"{name}.wav")
    if not os.path.exists(path):
        return
    if not _earcon_allowed(room, name):
        return
    with contextlib.suppress(Exception):
        await _play_clip(room, path, preroll=0.02,
                         exclude_identity=state.get("bridge_identity"))


async def _say_to_caller(state: dict, tag: str, text: str) -> None:
    """Speak one sentence to the CALLER only, from pre-rendered audio.

    The model cannot be asked to say these: mid-session prompting is a no-op on
    the live model, so anything routed through it arrives as silence at exactly
    the moments a bridged call is most frightening. Rendering it ourselves also
    costs no model turn, which means the call can afford to be clear.
    """
    room = state.get("room")
    if room is None:
        return
    with contextlib.suppress(Exception):
        voice = state.get("voice") or os.getenv("GEMINI_LIVE_VOICE", "Aoede")
        path = await asyncio.to_thread(_clip_wav, voice, text, tag)
        if path:
            _append_transcript_shared(state, "agent", text)
            await _play_clip(room, path, preroll=0.05,
                             exclude_identity=state.get("bridge_identity"))


def _already_disconnected(err: Exception) -> bool:
    """True when a removal failed because the participant was already gone."""
    s = f"{type(err).__name__} {err}".lower()
    return any(m in s for m in ("not_found", "notfound", "not found", "does not exist",
                                "no such participant", "404"))


async def _delete_room(room_name: str) -> None:
    from livekit import api as _lkapi
    lkapi = _lkapi.LiveKitAPI()
    try:
        await lkapi.room.delete_room(_lkapi.DeleteRoomRequest(room=room_name))
    finally:
        with contextlib.suppress(Exception):
            await lkapi.aclose()


_NUM_WORDS = {"zero": "0", "oh": "0", "o": "0", "one": "1", "two": "2", "three": "3",
              "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9"}
_NUM_TEENS = {"ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13",
              "fourteen": "14", "fifteen": "15", "sixteen": "16", "seventeen": "17",
              "eighteen": "18", "nineteen": "19"}
_NUM_TENS = {"twenty": "2", "thirty": "3", "forty": "4", "fifty": "5",
             "sixty": "6", "seventy": "7", "eighty": "8", "ninety": "9"}


def _spoken_to_digits(words: list[str]) -> str:
    """Fold spoken number words into a digit string ('four hundred' → '400')."""
    out: list[str] = []
    pending_ten = ""
    for w in words:
        if not w:
            continue
        if w in _NUM_TENS:
            if pending_ten:
                out.append(pending_ten + "0")
            pending_ten = _NUM_TENS[w]
            continue
        if pending_ten:
            if w in _NUM_WORDS and w not in ("zero", "oh", "o"):
                out.append(pending_ten + _NUM_WORDS[w])
                pending_ten = ""
                continue
            out.append(pending_ten + "0")
            pending_ten = ""
        if w == "hundred":
            out.append("00")
        elif w == "thousand":
            out.append("000")
        elif w in _NUM_TEENS:
            out.append(_NUM_TEENS[w])
        elif w in _NUM_WORDS:
            out.append(_NUM_WORDS[w])
        elif w.isdigit():
            out.append(w)
    if pending_ten:
        out.append(pending_ten + "0")
    return "".join(out)


# Ten digits, optional +1, with the spoken/typed separators seniors use.
# Anchored on both sides: "1973 4005897" (a year and an extension) must not
# read as +1 973-400-5897.
_PHONE_CANDIDATE = re.compile(r"(?<!\d)(1\s*)?([2-9]\d{2})\s*(\d{3})\s*(\d{4})(?!\d)")
_NUM_TOKENS = frozenset(_NUM_WORDS) | frozenset(_NUM_TEENS) | frozenset(_NUM_TENS) | {"hundred", "thousand"}


def _digit_boundary(prev: str, tok: str) -> bool:
    """True when two adjacent numeric tokens must not fold into one run."""
    p_dig, t_dig = prev.isdigit(), tok.isdigit()
    if p_dig and t_dig:
        return len(prev) >= 3 or len(tok) >= 3
    return (p_dig and len(prev) >= 4) or (t_dig and len(tok) >= 4)


def _numeric_runs(text: str) -> list[str]:
    """Each stretch of consecutive digit/number-word tokens, folded to digits.

    Typed digit tokens keep the grouping the caller gave them: a token of four
    or more digits ("1973", "4005897") is a number in its own right — a year, an
    extension — and never glues onto anything, and two typed groups fold together
    only when both are a digit or two ("58 97"). So "born 1973 4005897" stays
    two runs, while "1 973 400 5897" keeps its country code standing alone and
    the spoken "one nine seven three …" still folds into one.
    """
    runs: list[str] = []
    cur: list[str] = []
    for tok in re.split(r"[^a-z0-9]+", text.lower()):
        if tok and (tok.isdigit() or tok in _NUM_TOKENS):
            if cur and _digit_boundary(cur[-1], tok):
                runs.append(_spoken_to_digits(cur))
                cur = []
            cur.append(tok)
        elif cur:
            runs.append(_spoken_to_digits(cur))
            cur = []
    if cur:
        runs.append(_spoken_to_digits(cur))
    return [r for r in runs if r]


def _number_heard_from_caller(e164: str, state: dict) -> bool:
    """True when the caller themselves spoke this number, in one breath.

    Seniors read numbers aloud both ways ("973-400-5897" and "nine seven three…"),
    so digit words are folded in. Only `caller:` lines count — a number that only
    ever came out of the stranger's mouth is not a number she will dial — and
    each line is judged on its own: digits from two turns are never glued
    together, so a zip code plus a birth year cannot assemble a dialable number.
    """
    want = re.sub(r"\D", "", e164 or "")[-10:]
    lines = state.get("transcript_lines")
    if len(want) != 10 or not lines:
        return False
    for line in lines:
        if not line.lower().startswith("caller:"):
            continue
        text = " ".join(_numeric_runs(line.split(":", 1)[1]))
        for m in _PHONE_CANDIDATE.finditer(text):
            cc = m.group(1) or ""
            # A country code must stand alone ("1 973 …") or belong to one
            # unbroken run ("one nine seven three …"); "1973 400…" is a year.
            if cc and not cc.endswith(" ") and " " in m.group(0):
                continue
            if "".join(m.groups()[1:]) == want:
                return True
    return False


def _contains_phone_number(text: str) -> bool:
    """True when `text` carries a ten-digit number, typed or spoken."""
    return bool(_PHONE_CANDIDATE.search(" ".join(_numeric_runs(text or ""))))


def _number_in_recent_search(e164: str, state: dict) -> bool:
    """True when this number was in something she just web-searched.

    A lookup that returns a number she has already seen in search results is
    a number the model may have fed back in; it must not gain lookup provenance.
    Digits are folded the same way caller speech is, so "973-400-5897" and
    "(973) 400 5897" both count.
    """
    want = re.sub(r"\D", "", e164 or "")[-10:]
    recent = state.get("recent_search") or ""
    if len(want) != 10 or not recent:
        return False
    at = state.get("recent_search_at")
    if at and time.time() - at > _SEARCH_ECHO_WINDOW:
        return False
    if want in re.sub(r"\D", "", recent):
        return True
    return want in {"".join(m.groups()[1:])
                    for m in _PHONE_CANDIDATE.finditer(" ".join(_numeric_runs(recent)))}


# Sources rt_directory's deterministic tiers name themselves by. Anything else
# came out of the Gemini fallback, whose "source" is the model's own claim.
_DIRECTORY_SOURCES = frozenset({
    "the federal provider registry", "google's business listing", "the local business listing",
})


def _lookup_tier(res: dict) -> str:
    """'directory' for a registry/listing hit, 'gemini' for everything else."""
    if (res.get("tier") or "").lower() == "gemini" or "note" in res:
        return "gemini"
    return "directory" if (res.get("source") or "").strip().lower() in _DIRECTORY_SOURCES else "gemini"


async def _set_subscription(room_name: str, identity: str, track_sids: list[str],
                            subscribe: bool) -> None:
    """Server-side: choose whether `identity` receives these tracks."""
    if not (room_name and identity and track_sids):
        return
    from livekit import api as _lkapi
    lkapi = _lkapi.LiveKitAPI()
    try:
        await lkapi.room.update_subscriptions(_lkapi.UpdateSubscriptionsRequest(
            room=room_name, identity=identity, track_sids=track_sids, subscribe=subscribe))
    finally:
        with contextlib.suppress(Exception):
            await lkapi.aclose()


def _audio_track_sids(participant) -> list[str]:
    out = []
    for pub in (getattr(participant, "track_publications", {}) or {}).values():
        kind = getattr(pub, "kind", None)
        if kind in (rtc.TrackKind.KIND_AUDIO, 1) or "audio" in str(kind).lower():
            if getattr(pub, "sid", None):
                out.append(pub.sid)
    return out


async def _shield_set_private(state: dict, room, private: bool) -> bool:
    """Take the floor (private=True) or hand it back (private=False).

    Returns whether the room now matches what was asked for. Privacy here is
    three server calls that can each fail, and a caller telling a secret needs
    to know the difference between "the stranger is deafened" and "we asked".
    Going private is therefore all-or-nothing: if any edge could not be applied,
    or the caller has no identity to protect, we stay public and say so. Coming
    back is best-effort on purpose — a half-restored room must never be left
    believing it is still private.
    """
    bridge_id = state.get("bridge_identity")
    caller_id = state.get("caller_identity")
    if not (room and bridge_id):
        return False
    room_name = getattr(room, "name", None)
    remotes = getattr(room, "remote_participants", {}) or {}
    stranger = remotes.get(bridge_id)
    stranger_tracks = _audio_track_sids(stranger) if stranger else []
    caller_tracks = _audio_track_sids(remotes.get(caller_id)) if caller_id else []
    mine = _audio_track_sids(getattr(room, "local_participant", None))

    if private and not caller_id:
        print("[rt-shield] REFUSING private aside — no caller identity, it would be "
              "private in name only", flush=True)
        _trace(state, "guard", "private_refused", {"reason": "no caller identity"})
        state["shield_private"] = False
        return False

    edges = []
    if mine:
        edges.append((bridge_id, mine))
    if caller_tracks:
        edges.append((bridge_id, caller_tracks))
    if stranger_tracks:
        edges.append((caller_id, stranger_tracks))

    applied, failed = 0, 0
    for identity, sids in edges:
        try:
            await _set_subscription(room_name, identity, sids, not private)
            applied += 1
        except Exception as e:
            failed += 1
            print(f"[rt-shield] subscription edge failed ({identity}): {e}", flush=True)

    if private and (failed or not edges):
        for identity, sids in edges:
            with contextlib.suppress(Exception):
                await _set_subscription(room_name, identity, sids, True)
        print(f"[rt-shield] REFUSING private aside — {failed} of {len(edges)} edges failed",
              flush=True)
        _trace(state, "guard", "private_refused", {"failed": failed, "edges": len(edges)})
        state["shield_private"] = False
        return False

    state["shield_private"] = private
    print(f"[rt-shield] {'PRIVATE — stranger muted and deafened to her' if private else 'floor returned to the room'}"
          f" ({applied} edges)", flush=True)
    return True


def _shield_listen(state: dict) -> None:
    """Silent-watch posture: ears open, voice off."""
    ses = state.get("session")
    if ses is None:
        return
    with contextlib.suppress(Exception):
        ses.output.set_audio_enabled(False)
    state["shield_speaking"] = False


def _shield_take_floor(state: dict, reason: str, instructions: str,
                       seconds: float = 25.0, private: bool = True,
                       chime: bool = True, speak: str = "") -> bool:
    """Give her the floor and make her use it now (scheduled, never blocking).

    Returns False when she already holds it — the caller must NOT then record the
    trigger as handled, or a fraud signal heard while she was mid-sentence would
    be marked delivered and never spoken for the rest of the call.
    """
    if state.get("shield_speaking"):
        return False
    state["shield_speaking"] = True
    state["shield_speak_until"] = _now() + seconds
    asyncio.create_task(_shield_floor_task(state, reason, instructions, private, chime, speak))
    return True


async def _shield_floor_task(state: dict, reason: str, instructions: str,
                             private: bool, chime: bool, speak: str = "") -> None:
    """Give her the floor, optionally with a spoken line she must actually say.

    IMPORTANT: on gemini-3.1 live, generate_reply() and mid-session instruction
    updates are no-ops (the plugin sets mutable=False for 3.1), so a warning that
    depended on either would simply never be heard. Anything she MUST say is
    pre-rendered audio, exactly like the greeting. For the wake-word case there's
    nothing to force: unmuting is enough — the model already heard the question
    and answers it on its own turn.
    """
    ses, room = state.get("session"), state.get("room")
    if ses is None:
        return
    print(f"[rt-shield] taking the floor ({reason})", flush=True)
    got_private = True
    if private and room is not None:
        got_private = await _shield_set_private(state, room, True)
    if chime and room is not None:
        path = _get_chime_path()
        if path:
            with contextlib.suppress(Exception):
                await _play_clip(room, path, preroll=0.05,
                                 exclude_identity=state.get("bridge_identity"))
    if speak and room is not None:
        safe = ("Let's hang up and call them back on the number you already have. "
                "I'll stay right here with you.")
        line = speak if got_private else safe
        if not got_private:
            print("[rt-shield] aside not private — using the overhearable warning", flush=True)
        try:
            voice = state.get("voice") or os.getenv("GEMINI_LIVE_VOICE", "Aoede")
            clip = await asyncio.to_thread(_clip_wav, voice, line, "warn")
            if clip:
                _append_transcript_shared(state, "agent", line)
                await _play_clip(room, clip, preroll=0.1,
                                 exclude_identity=state.get("bridge_identity"))
                print(f"[rt-shield] spoke the warning: {line[:60]!r}", flush=True)
        except Exception as e:
            print(f"[rt-shield] warning clip failed: {e}", flush=True)
    with contextlib.suppress(Exception):
        ses.output.set_audio_enabled(True)
    with contextlib.suppress(Exception):
        ses.generate_reply(instructions=instructions)


def _shield_release_floor(state: dict) -> None:
    """Back to silent watching once her turn is done.

    Say so out loud when a private aside ends. The caller was told only she could
    hear him; the window then closes on a wall clock, and without a word he goes
    on confiding to a room the stranger is back in. Of every signal on a bridged
    call this is the one that must never be silent — missing the chime that opens
    an aside costs him nothing, missing the moment it closes costs him the secret.
    """
    was_private = bool(state.get("shield_private"))
    if state.get("shield_speaking"):
        print("[rt-shield] back to listening", flush=True)
    _shield_listen(state)
    if was_private and state.get("room") is not None:
        asyncio.create_task(_shield_set_private(state, state["room"], False))
        asyncio.create_task(_say_to_caller(state, "he-can-hear", "He can hear us again."))


_PANIC_PHRASES = (
    "hang up on him", "hang up on her", "hang up on them", "hang up on this",
    "get rid of him", "get rid of her", "get rid of them", "get him off",
    "get her off", "get them off", "make it stop", "make him stop",
    "end this call", "i want to hang up", "drop the call", "hang up now",
    "just hang up",
)


def _panic_phrase_present(text: str) -> bool:
    low = " ".join((text or "").lower().split())
    return any(p in low for p in _PANIC_PHRASES)


_FAREWELL = re.compile(
    r"(?i)\s*(ok(ay)?[ ,]*)?(well[ ,]*)?(alright[ ,]*)?(thanks?( you)?[ ,]*)?"
    r"(good ?bye|bye|bye bye|bye now|"
    r"talk (to you )?later|see you( later)?|gotta go|i have to go|"
    r"i'?m going to (go|hang up)|let'?s hang up|i'?ll let you go|"
    r"have a (good|nice|lovely) (day|night|one|evening))"
    r"([ ,]*(now|then|dear|hon(ey)?|[a-z]{2,12})){0,2}\s*")
_NOT_A_FAREWELL = re.compile(
    r"\b(if|unless|don'?t|do not|before|when|whether|said|says|say|told|asked|"
    r"he|she|they|we|used to|should|would|could|maybe)\b", re.I)


def _is_farewell(text: str) -> bool:
    """True only when the WHOLE utterance is a goodbye.

    This is a backstop; end_call is the real path. Substring matching hung up on
    someone mid-conversation for saying "have a good day" about their daughter,
    within 4 seconds and with no goodbye of her own. It now has to be the entire
    thing they said, and short.
    """
    t = " ".join((text or "").split()).strip().rstrip(".!,")
    if not t or len(t.split()) > 6:
        return False
    if _NOT_A_FAREWELL.search(t):
        return False
    return bool(_FAREWELL.fullmatch(t))


_QUIET_REQUESTS = (
    "just listen", "listen only", "only listen", "listen in", "don't talk",
    "do not talk", "stop talking", "stay quiet", "be quiet", "keep quiet",
    "don't say anything", "no need to talk", "hang back", "stay out of it",
)


def _asked_for_quiet(text: str) -> bool:
    """True only when the caller actually asked her to go silent."""
    low = " ".join((text or "").lower().split())
    return any(p in low for p in _QUIET_REQUESTS)


def _wake_word_present(text: str, alias: str | None) -> bool:
    """True when the caller addressed her by name — her cue to answer."""
    words = {(alias or "").strip().lower()} - {"", "your companion"}
    low = (text or "").lower()
    return any(re.search(rf"\b{re.escape(w)}\b", low) for w in words)


def _append_transcript_shared(state: dict, role: str, text: str) -> None:
    """Transcript append usable from tool code (entrypoint owns the closure version)."""
    fn = state.get("append_transcript")
    if fn:
        with contextlib.suppress(Exception):
            fn(role, text)


def _is_synthetic_line(text: str) -> bool:
    """True for injected control turns (greeting nudge) that must never enter the transcript."""
    return text.strip().lower().startswith("(call just connected")


_COMMON_WORDS = frozenset("""
been being name named names think thing things there their they that this with what
when where which would could should about right really little listen again alright
okay yeah sure please thanks thank hello hey morning afternoon evening night today
tomorrow yesterday actually anything everything something nothing because before
after still first last next call called calling caller phone number numbers remember
remembered doctor nurse office hospital pharmacy insurance appointment money card
cards said say says tell told talk talking talked know knows knew good great fine
well were was are you your yours mine ours them him her his she him hers just like
want wants need needs help helps helping give gives given take takes come comes
going gone here have has had did does done make makes made from into over under
much many more most some none other another each every all any own same than then
""".split())


def _heard_in_caller_lines(name: str, transcript_lines: list[str]) -> bool:
    """True if `name` appears in at least one caller-spoken line.

    This is the deterministic gate between the model's belief and the DB:
    a name the caller never said cannot become their identity.
    """
    n = (name or "").strip().lower()
    if not n:
        return False
    import difflib as _dl
    for line in transcript_lines:
        low = line.lower()
        if not low.startswith("caller:"):
            continue
        if n in low or n in rt_prefs.despell(low):
            return True
        for word in re.findall(r"[a-z']{3,}", rt_prefs.despell(low)):
            if word in _COMMON_WORDS:
                continue
            if _dl.SequenceMatcher(None, n, word).ratio() >= 0.72:
                return True
    return False


_FORGET_STOP = frozenset("""
the that this these those all any about from with for and but not you your yours
please just really thing things stuff stored store save saved memory memories
note notes record records everything something anything delete remove erase
forget clear wipe scratch data info information
""".split())

_INTERNAL_CATS = frozenset({
    "clarifications", "onboarding", "call_log", "bridge_log", "scam_reports",
    "daily_minutes", "agent_tasks", "credentials", "wellbeing",
})


def _internal_cats() -> frozenset:
    """Categories the model may neither write nor read through db_tool.

    rt_prefs.INTERNAL_CATS is the shared list once it lands; until then the
    local one plus the ledgers. A write to bridge_log would shallow-merge over
    the day's dial count and reset the cap, so it is refused before any RPC.
    The credential vault is the one exception: the model routes codes there on
    purpose and reads them back for the caller, and nothing else surfaces them.
    """
    shared = getattr(rt_prefs, "INTERNAL_CATS", None)
    cats = set(shared or ()) | set(_INTERNAL_CATS) | {"bridge_log", "scam_reports", "daily_minutes"}
    cats.discard(rt_prefs.CRED_CATEGORY)
    return frozenset(c.lower() for c in cats)


def _email_spoken_by_caller(email: str, transcript_lines: list[str]) -> bool:
    """rt_shield.email_spoken_by_caller when present; the same rule inline until
    it lands. Whole address, token-boundary, caller lines only."""
    fn = getattr(rt_shield, "email_spoken_by_caller", None)
    if fn is not None:
        return bool(fn(email, transcript_lines))
    e = (email or "").strip().lower()
    if not e:
        return False
    pat = re.compile(r"(?<![A-Za-z0-9_.+-])" + re.escape(e) + r"(?![A-Za-z0-9_.-])", re.I)
    return any(pat.search(line.split(":", 1)[1]) for line in transcript_lines or ()
               if line.lower().startswith("caller:"))


_WIPE_CONSENT_PHRASES = ("erase everything about me and start over", "forget me completely")
_WIPE_CONSENT_PREFIXES = ("yes please", "yes", "okay", "ok")


def _wipe_consent_line(line: str) -> bool:
    """rt_shield.wipe_consent when present; the same scripted-phrase rule inline.

    Consent is the exact phrase she asked for, nothing looser: a negation, a
    question or a narrowing ("everything about my sister") never equals it.
    """
    fn = getattr(rt_shield, "wipe_consent", None)
    if fn is not None:
        return bool(fn(line))
    # A question mark anywhere makes the line a question, not consent — the
    # normaliser below would otherwise strip it and let "…start over?" through.
    if "?" in (line or ""):
        return False
    norm = " ".join(re.sub(r"[^a-z0-9\s]", " ", (line or "").lower()).split())
    for phrase in _WIPE_CONSENT_PHRASES:
        if norm == phrase or any(norm == f"{p} {phrase}" for p in _WIPE_CONSENT_PREFIXES):
            return True
    return False


def _wipe_consented_since(transcript_lines: list[str], since_index: int) -> bool:
    """True iff a CALLER line at/after `since_index` is the scripted consent."""
    since = max(int(since_index or 0), 0)
    for line in list(transcript_lines or ())[since:]:
        if not line.lower().startswith("caller:"):
            continue
        if _wipe_consent_line(line.split(":", 1)[1]):
            return True
    return False


def _wipe_confirmed_since(transcript_lines: list[str], since_index: int) -> bool:
    """rt_shield.wipe_confirmed, passing since_index only when the helper takes it."""
    fn = rt_shield.wipe_confirmed
    try:
        params = inspect.signature(fn).parameters
        takes = "since_index" in params or any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    except (TypeError, ValueError):
        takes = False
    if takes:
        return bool(fn(transcript_lines, since_index=since_index))
    return bool(fn(transcript_lines))


_FORGET_MAX_ITEMS = 3


class _TooManyToForget(Exception):
    """A topic delete that would clear more than _FORGET_MAX_ITEMS things."""

    def __init__(self, count: int):
        super().__init__(f"{count} items match")
        self.count = count


def _forget_topic(h: str, target: str) -> list[str]:
    """Erase whatever the caller named, wherever it actually lives.

    `delete` only ever understood the fixed category list, so anything named by
    its own word fell straight through to a refusal. This walks the places a
    fact can actually be: the schema registry, the reminder ledger, the facts table,
    loved_ones, and the two prose columns (rules and persona directives).
    Returns what was cleared, so the caller is told the truth about whether anything happened.

    Matching is whole-word ("Ed" must not blank "medication"), and everything
    is planned before anything is written: more than _FORGET_MAX_ITEMS hits
    raises _TooManyToForget with nothing touched, so a vague word can never
    take out half the file.
    """
    words = [w for w in re.findall(r"[a-z0-9]{3,}", (target or "").lower())
             if w not in _FORGET_STOP]
    if not words:
        return []
    try:
        bundle = rt_prefs._req("POST", "rpc/rt_get_caller_full_bundle", {"p_hash": h}) or {}
    except Exception as _exc:
        rt_obs.obs.caught("agent._forget_topic", _exc)
        return []
    pat = re.compile(r"(?<![a-z0-9])(?:" + "|".join(re.escape(w) for w in words) + r")(?![a-z0-9])")
    hit = lambda t: bool(pat.search((t or "").lower()))

    # (label, rpc_path, body) — executed only once the whole plan fits the cap.
    plan: list[tuple[str, str, dict]] = []

    for entry in (bundle.get("schemas") or []):
        cat = (entry.get("category") or "").lower()
        if cat in _INTERNAL_CATS:
            continue
        try:
            obj = json.loads(entry.get("data_summary") or "{}")
        except Exception as _exc:
            rt_obs.obs.caught("agent._forget_topic", _exc)
            continue
        if not isinstance(obj, dict):
            continue
        doomed = {k: "" for k, v in obj.items()
                  if not k.startswith("_") and (hit(k) or hit(str(v)))}
        if doomed:
            plan.append(("|".join(f"{cat}.{k}" for k in doomed), "rpc/rt_add_schema_entry", {
                "p_hash": h, "p_table": f"caller_{h[:8]}_{cat}",
                "p_cat": cat, "p_summary": json.dumps(doomed)}))

    for r in (bundle.get("reminders") or []):
        txt = r.get("reminder_text") or ""
        if hit(txt):
            plan.append((f"reminder: {txt[:40]}", "rpc/rt_complete_reminder",
                         {"p_hash": h, "p_text": txt}))

    # Prune active facts from rt.facts table
    try:
        active_facts = bundle.get("facts")
        if active_facts is None:
            active_facts = rt_prefs._req("POST", "rpc/rt_get_facts", {"p_hash": h, "p_limit": 100}) or []
        for f in (active_facts or []):
            f_id = f.get("id")
            f_key = f.get("norm_key") or f.get("subject") or ""
            f_val = f.get("value_text") or str(f.get("value_json") or "")
            if f_id and (hit(f_key) or hit(f_val)):
                plan.append((f"fact: {f_key}", "rpc/rt_retire_fact", {"p_hash": h, "p_id": f_id}))
    except Exception as _exc:
        rt_obs.obs.caught("agent._forget_topic.facts", _exc)

    caller = bundle.get("caller") or {}

    # Prune loved_ones column
    lo_raw = (caller.get("loved_ones") or "").strip()
    if lo_raw:
        lo_entries = [e.strip() for e in lo_raw.split(",") if e.strip()]
        kept_lo = [e for e in lo_entries if not hit(e)]
        if len(kept_lo) != len(lo_entries):
            plan.append(("|".join(f"loved_ones: {e[:40]}" for e in lo_entries if hit(e)),
                         "rpc/rt_set_loved_ones", {"p_hash": h, "p_loved_ones": ", ".join(kept_lo)}))

    for col, rpc, param in (("persona_directives", "rt_set_persona_directives", "p_directives"),
                            ("caller_rules", "rt_set_caller_rules", "p_rules")):
        cur = (caller.get(col) or "").strip()
        if not cur:
            continue
        parts = [x.strip() for x in cur.split(";") if x.strip()]
        kept = [x for x in parts if not hit(x)]
        if len(kept) != len(parts):
            plan.append(("|".join(f"{col}: {x[:40]}" for x in parts if hit(x)),
                         f"rpc/{rpc}", {"p_hash": h, param: "; ".join(kept)}))

    labels = [lb for label, _p, _b in plan for lb in label.split("|")]
    if len(labels) > _FORGET_MAX_ITEMS:
        raise _TooManyToForget(len(labels))

    cleared: list[str] = []
    for label, path, body in plan:
        with contextlib.suppress(Exception):
            rt_prefs._req("POST", path, body)
            cleared += label.split("|")
    return cleared


def _add_clarification(h: str, key: str, question: str) -> None:
    """Record an open question for future calls (merged into the clarifications schema)."""
    try:
        rt_prefs._req("POST", "rpc/rt_add_schema_entry", {
            "p_hash": h,
            "p_table": f"caller_{h[:8]}_clarifications",
            "p_cat": "clarifications",
            "p_summary": json.dumps({key: question}),
        })
    except Exception as e:
        print(f"[rt-db] clarification write failed (non-fatal): {e}", flush=True)


def _clear_clarification(h: str, key: str) -> None:
    """Resolve a clarification by blanking its key (jsonb merge overwrites; hydrator skips empties)."""
    _add_clarification(h, key, "")


def _trigger_sound(room, sound_name: str) -> None:
    """Play an earcon into the call. Takes the room — a RunContext has no room."""
    try:
        path = os.path.join(os.path.dirname(__file__), "sounds", f"{sound_name.strip().lower()}.wav")
        if not room:
            print(f"[rt-sound] no room — {sound_name!r} not played", flush=True)
            return
        if not os.path.exists(path):
            print(f"[rt-sound] no such sound: {sound_name!r}", flush=True)
            return
        if not _earcon_allowed(room, sound_name.strip().lower()):
            return
        asyncio.create_task(_play_clip(room, path, preroll=0.05))
    except Exception as e:
        print(f"[rt-sound] _trigger_sound failed (non-fatal): {e}", flush=True)


class RtAgent(Agent):
    """The lane's agent: the companion the caller actually talks to."""

    def __init__(self, caller_e164: str | None, instructions: str = "", room=None, call_state: dict | None = None):
        super().__init__(instructions=instructions)
        self._caller_e164 = caller_e164
        self._room = room
        self._state = call_state
        self._base_instructions = instructions
        self._bridge_lock = asyncio.Lock()

    def _heard(self, name: str) -> bool | None:
        """Guard verdict for a proposed name. None = no transcript available (guard bypassed)."""
        lines = (self._state or {}).get("transcript_lines")
        if lines is None:
            return None
        return _heard_in_caller_lines(name, lines)

    def _routine_guard(self, text: str, kind: str) -> tuple[str, None] | None:
        """A rule or skill is a standing instruction she will act on in future
        calls: it must be clean of tool names and come from the caller's own
        mouth, before anything touches the database. Returns the refusal, or None."""
        ok, why = rt_shield.rule_text_allowed(text)
        if not ok:
            _log_pii(f"[rt-guard] REFUSE {kind} — {why}", text)
            return (f"[not saved] That can't be kept as a {kind} ({why}). "
                    f"Tell them plainly you can't take that one on."), None
        if not rt_shield.spoken_by_caller(text, (self._state or {}).get("transcript_lines") or []):
            _log_pii(f"[rt-guard] REFUSE {kind} — not spoken by the caller", text)
            return ("[not saved] The caller didn't actually say that — ask them "
                    "to tell you in their own words, then save it again."), None
        return None

    @function_tool
    async def end_call(self, context: RunContext) -> str:
        """End the call, when they ask to hang up or say goodbye. Say your one
        warm goodbye FIRST, then call this.
        """
        if (self._state or {}).get("bridge_active"):
            print("[rt-guard] end_call during bridge → dropping the third party instead", flush=True)
            await self._clear_bridge()
            return ("[the other person has been hung up on — the caller is still with you] "
                    "Check in with them warmly; do not end this call.")
        print(f"[rt] end_call tool executed by model — disconnecting line for caller={_mask_e164(self._caller_e164)}", flush=True)
        try:
            if self._room:
                asyncio.create_task(self._room.disconnect())
        except Exception as e:
            print(f"[rt] end_call room disconnect failed: {e}", flush=True)
        return "Call ending now. Goodbye!"

    @function_tool
    async def find_number(self, context: RunContext, what: str) -> str:
        """Look up the official phone number for a place the caller names — "call my
        pharmacy", "reach Dr. Patel's office", "get me the insurance company".
        Put the town or their city in `what`: "Walgreens on Washington Street, Hoboken NJ".

        Never dial off a lookup. Read the number and where it came from back to
        them, get a yes, then bridge_call.
        """
        if self._state is not None:
            self._state["last_activity_time"] = _now()
        import rt_bridge
        import rt_postcall_worker
        if self._state is None:
            self._state = {}
        st = self._state
        if st.get("bridge_active") and st.get("bridge_mode") != rt_bridge.MODE_ASSIST:
            print("[rt-guard] SEALED find_number — stranger on the line", flush=True)
            return ("Not while someone else is on the line. Once we've hung up with "
                    "this person I'll look it up for the caller.")
        # A number inside the request would ride through the lookup prompt and
        # come back out "found" — with lookup provenance it never earned.
        if _contains_phone_number(what):
            print("[rt-guard] REFUSE find_number — phone number in the request", flush=True)
            _trace(st, "guard", "refuse_lookup", {"reason": "phone number in request"})
            return ("[lookup refused] a phone number was in the request — ask the caller "
                    "to read it to you")
        wait_sound = os.path.join(os.path.dirname(__file__), "sounds", "thinking_shimmer.wav")
        if os.path.exists(wait_sound) and self._room:
            asyncio.create_task(_play_clip(self._room, wait_sound, preroll=0.05, repeat=1))
        api_key = os.getenv("GOOGLE_API_KEY", "")
        if not api_key:
            return "I can't look numbers up right now — ask the caller if they have it handy."
        import rt_directory
        res = await asyncio.to_thread(
            rt_directory.lookup, what, rt_postcall_worker._gemini_json, api_key)
        if res.get("refused"):
            return f"[lookup refused] Say this in your own words: {res['reason']}"
        if not res or not res.get("number"):
            return ("[no trustworthy number found] Tell the caller honestly that you "
                    "couldn't find a number you trust, and ask if they have it on a "
                    "statement, a bottle, or the back of their card.")
        state = self._state or {}
        if _number_in_recent_search(res["number"], state):
            print("[rt-guard] REFUSE find_number — number echoes a recent web search", flush=True)
            _trace(state, "guard", "refuse_lookup", {"reason": "number seen in recent web search"})
            return ("[number matches something from a web search, not a verified listing "
                    "— ask the caller to read the digits]")
        res = dict(res)
        res["tier"] = _lookup_tier(res)
        if res["tier"] == "gemini":
            # The model vouched for itself; that is never more than "low".
            res["confidence"] = "low"
        state.setdefault("looked_up", {})[res["number"]] = res
        bits = [x for x in (res.get("detail"), res.get("where")) if x]
        alts = "; ".join(f"{a['name']} in {a.get('where','')} — {a['number']}"
                         for a in (res.get("alternatives") or [])[:2])
        _trace(state, "tool", "find_number", {"asked": what, "result": res})
        return (f"[found: {res['name']} — {res['number']}"
                f"{' (' + ', '.join(bits) + ')' if bits else ''} "
                f"from {res['source']}, confidence {res['confidence']}]"
                f"{(' Other matches: ' + alts) if alts else ''} "
                f"Read the number back to the caller digit by digit, say who it is and "
                f"where it came from, and ask if you should dial it. Only call "
                f"bridge_call after they say yes.")

    @function_tool
    async def bridge_call(self, context: RunContext, number: str, who: str = "",
                          reason: str = "") -> str:
        """Dial a number and bring that person onto THIS call, with the caller.

        `who` names who should answer ("his pharmacy") — the caller hears it.
        `reason` in the caller's own words; an errand means you help, a stranger
        or suspicious callback means you go quiet. Your rules arrive on connect.

        Dial only a number the caller said aloud, or one from find_number they
        approved. Tell them what you're doing before you dial.
        """
        state = self._state or {}
        import rt_bridge

        async with self._bridge_lock:
            if state.get("bridge_active"):
                return ("Someone is already on the line with us. Hang up with them "
                        "first (end_bridge) before calling anyone else.")
            try:
                e164 = rt_bridge.normalize_dialable(number)
            except rt_bridge.DialRefused as e:
                print(f"[rt-bridge] REFUSE dial {_mask_e164(number)}: {e}", flush=True)
                return f"[dial refused] Say this to the caller warmly, in your own words: {e}"

            entry = (state.get("looked_up") or {}).get(e164)
            looked_up = entry is not None
            # A Gemini-tier hit is provenance enough to dial, not to assist:
            # the "listing" was the model's word.
            verified = looked_up and _lookup_tier(entry) != "gemini"
            if not looked_up and not _number_heard_from_caller(e164, state):
                print(f"[rt-bridge] REFUSE dial {_mask_e164(e164)} — not heard from the caller", flush=True)
                _trace(state, "guard", "refuse_dial",
                       {"number": e164, "reason": "no provenance — not spoken by the caller and not from a lookup"})
                return ("[dial refused — do NOT invent a connection problem, tell them "
                        "the truth] You don't have that number from them or from a "
                        "lookup you ran. Say plainly that you want to be sure you have "
                        "the right number, and ask them to read it to you.")

            state["bridge_count"] = int(state.get("bridge_count") or 0) + 1
            # Per-call cap (rt_bridge.MAX_BRIDGES_PER_CALL); getattr so this
            # module imports even against an older rt_bridge.
            if state["bridge_count"] > getattr(rt_bridge, "MAX_BRIDGES_PER_CALL", 4):
                return ("[dial refused] Tell the caller kindly that you've made several "
                        "calls together already and you'd rather stop for now.")
            h = rt_prefs.phone_hash(self._caller_e164 or "")
            if not h:
                # No caller identity means no ledger, and no ledger means no
                # daily cap — an anonymous line would get unlimited dials.
                print("[rt-bridge] REFUSE dial — caller identity unverified", flush=True)
                _trace(state, "guard", "refuse_dial",
                       {"number": e164, "reason": "caller identity unverified"})
                return ("[dial refused] I can't verify who I'm calling for, so I "
                        "can't place calls on this line. Say so plainly.")
            try:
                await asyncio.to_thread(rt_bridge.check_and_record_dial, h, e164)
            except rt_bridge.DialRefused as e:
                print(f"[rt-bridge] REFUSE dial {_mask_e164(e164)} (daily cap)", flush=True)
                return f"[dial refused] Tell the caller, kindly: {e}"

            room_name = getattr(self._room, "name", None)
            if not room_name:
                return "I can't place a call right now."

            mode = rt_bridge.classify_mode(f"{reason} {who}", looked_up=verified)
            if looked_up and not verified:
                mode = rt_bridge.MODE_SHIELD
            display = (state.get("display_name") or "your friend")
            label = (state.get("looked_up") or {}).get(e164, {}).get("name") or who or "Someone"

            import rt_hydrator
            block = ""
            with contextlib.suppress(Exception):
                block = (rt_hydrator.render_assist_block(display, label)
                         if mode == rt_bridge.MODE_ASSIST
                         else rt_hydrator.render_shield_block(display, who or "Someone else"))
                await self.update_instructions(block + self._base_instructions)
            state["bridge_block"] = block

            gen = int(state.get("bridge_gen") or 0) + 1
            identity = f"bridge-{e164[-4:]}-{int(_now())}"
            state.update({
                "bridge_gen": gen, "bridge_active": True, "bridge_identity": identity,
                "bridge_started_at": _now(), "bridge_joined": False, "bridge_number": None,
                "bridge_who": who or "", "bridge_mode": mode, "shield_flagged": [],
                "listen_only": False,
            })
            _shield_listen(state)
            state["listen_only"] = True
            print(f"[rt-bridge] dialing {_mask_e164(e164)} as {identity} mode={mode} gen={gen}", flush=True)
            _trace(state, "bridge", "dialing", {"number": e164, "mode": mode, "who": who, "reason": reason})

            brief = ("I'll be listening, and quiet. Say " + (state.get("agent_alias") or "my name")
                     + " whenever you want me to speak up — every time, so you always "
                     "know it was you. If you hear a soft chime first, that's me in "
                     "your ear only. Say 'hang up on him' and he's gone.")
            asyncio.create_task(_say_to_caller(state, f"brief-{mode}", brief))

        cap = (rt_bridge.MAX_ASSIST_SECONDS if mode == rt_bridge.MODE_ASSIST
               else rt_bridge.MAX_BRIDGE_SECONDS)
        state["bridge_cap"] = cap
        try:
            from livekit import api as _lkapi
            lkapi = _lkapi.LiveKitAPI()
            try:
                await lkapi.sip.create_sip_participant(_lkapi.CreateSIPParticipantRequest(
                    sip_trunk_id=os.getenv("SIP_OUTBOUND_TRUNK_ID", ""),
                    sip_call_to=e164,
                    room_name=room_name,
                    participant_identity=identity,
                    participant_name=f"Bridged {e164}",
                    play_ringtone=True,
                    play_dialtone=True,
                    wait_until_answered=True,
                    ringing_timeout=_dur(45),
                    max_call_duration=_dur(cap),
                ))
            finally:
                with contextlib.suppress(Exception):
                    await lkapi.aclose()
        except Exception as e:
            print(f"[rt-bridge] dial failed {_mask_e164(e164)}: {e}", flush=True)
            if int(state.get("bridge_gen") or 0) == gen:
                await self._clear_bridge(answered=False)
            if "timed out" in str(e).lower() or "408" in str(e):
                return (f"[no answer from {who or e164}] Tell the caller warmly that the line "
                        f"rang but nobody picked up, and ask if they would like to try again later.")
            return ("[dial failed] Tell the caller you couldn't get the call to go "
                    "through, and offer to try again in a moment.")

        if int(state.get("bridge_gen") or 0) != gen or not state.get("bridge_active"):
            print(f"[rt-bridge] dial answered after cancel (gen {gen}) — dropping it", flush=True)
            dropped = False
            for attempt in range(3):
                try:
                    await _remove_participant(room_name, identity)
                    dropped = True
                    break
                except Exception as e:
                    if _already_disconnected(e):
                        dropped = True
                        break
                    print(f"[rt-bridge] post-cancel removal attempt {attempt + 1} failed: {e}",
                          flush=True)
                    if attempt < 2:
                        await asyncio.sleep(0.4 * (attempt + 1))
            if not dropped:
                print("[rt-bridge] post-cancel leg would not drop — tearing down the room",
                      flush=True)
                with contextlib.suppress(Exception):
                    await _delete_room(room_name)
            return ("[the call was cancelled while it was ringing] Tell the caller you "
                    "didn't put that call through.")

        state["bridge_joined"] = True
        state["bridge_number"] = e164
        print(f"[rt-bridge] BRIDGED {_mask_e164(e164)} ({label}) mode={mode}", flush=True)
        _trace(state, "bridge", "answered", {"number": e164, "label": label, "mode": mode})

        await _cue(state, "line_connected")

        announce = f"Hi, this is {state.get('agent_alias') or 'their companion'} — I'm on the line with {display}."
        with contextlib.suppress(Exception):
            path = await asyncio.to_thread(
                _clip_wav, state.get("voice") or os.getenv("GEMINI_LIVE_VOICE", "Aoede"),
                announce, "announce")
            if path and self._room:
                _append_transcript_shared(state, "agent", announce)
                await _play_clip(self._room, path, preroll=0.35)

        block = state.get("bridge_block") or ""
        if mode == rt_bridge.MODE_ASSIST:
            return (f"[connected to {label} — you already announced yourself. "
                    f"These rules govern the rest of this call:]{block}")
        return (f"[bridged: {e164} is on the call — you already announced yourself. "
                f"These rules govern the rest of this call:]{block}")

    @function_tool
    async def listen_only(self, context: RunContext, quiet: bool = True) -> str:
        """Go quiet while someone else is on the line — or come back in.

        quiet=True only when they actually ask ("just listen", "don't talk");
        quiet=False when they want you back in. You always keep hearing.
        """
        state = self._state or {}
        if not state.get("bridge_active"):
            return "We're not on a call with anyone else right now."
        if quiet:
            if not _asked_for_quiet(state.get("last_user_transcript") or ""):
                print("[rt-guard] listen_only refused — no explicit request heard", flush=True)
                return ("They didn't ask you to go quiet. Stay in the conversation "
                        "and keep helping.")
            _shield_listen(state)
            state["listen_only"] = True
            await _cue(state, "listening_on")
            print("[rt-bridge] caller asked for listen-only", flush=True)
            return ("[quiet now — you'll only speak if they say your name or you hear "
                    "something worrying] Tell them briefly that you're right here, "
                    "listening.")
        state["listen_only"] = False
        ses = state.get("session")
        if ses is not None:
            with contextlib.suppress(Exception):
                ses.output.set_audio_enabled(True)
        state["shield_speaking"] = False
        await _cue(state, "listening_off")
        print("[rt-bridge] caller invited her back into the conversation", flush=True)
        return "[back in the conversation] Let them know you're with them again."

    @function_tool
    async def press_keys(self, context: RunContext, digits: str) -> str:
        """Press phone-menu keys on a bridged call ("press 2 for appointments").
        One or more digits, plus * and #.
        """
        state = self._state or {}
        if not state.get("bridge_joined"):
            return "There's no call to press keys on right now."
        seq = [c for c in (digits or "") if c in "0123456789*#"]
        if not seq:
            return "I didn't catch which keys to press."
        room = self._room
        if room is None:
            return "I can't press keys on this call."
        for d in seq[:12]:
            code = {"*": 10, "#": 11}.get(d, int(d) if d.isdigit() else None)
            if code is None:
                continue
            try:
                await room.local_participant.publish_dtmf(code=code, digit=d)
            except Exception as e:
                print(f"[rt-bridge] DTMF {d} failed: {e}", flush=True)
            await asyncio.sleep(0.18)
        print(f"[rt-bridge] pressed {''.join(seq[:12])}", flush=True)
        return f"[pressed {' '.join(seq[:12])}] Tell the caller what you pressed, briefly."

    @function_tool
    async def end_bridge(self, context: RunContext) -> str:
        """Hang up on the other person ONLY — the caller stays on the line with you.

        Use the moment the caller asks ("hang up on him", "we're done"), or when
        you've named a scam and they agree.
        """
        state = self._state or {}
        if not state.get("bridge_active"):
            return "There's no one else on the line right now."
        ok = await self._clear_bridge(answered=bool(state.get("bridge_joined")))
        if not ok:
            return ("[the line would not drop] Tell the caller plainly that you can't "
                    "get that person off the line, and that the safest thing is for "
                    "them to hang up entirely and call you straight back.")
        return ("[the other person has been hung up on — it's just you and the caller now] "
                "Reassure them warmly in your own words; tell them they did the right "
                "thing, and offer to help them call a family member or the official number.")

    async def _clear_bridge(self, answered: bool = True, already_gone: bool = False) -> bool:
        """Remove the bridged leg and restore normal operation.

        FAILS CLOSED: if the leg cannot be removed, the shield stays UP (memory
        sealed, speech still tagged as the stranger's, hangup tripwire disabled)
        rather than reclassifying a connected stranger as the caller. If it still
        won't drop after retries, the whole room is torn down — ending the call is
        far better than leaving someone unprotected on it.
        """
        state = self._state or {}
        identity = state.get("bridge_identity")
        room_name = getattr(self._room, "name", None)

        removed = True
        if identity and room_name and not already_gone:
            removed = False
            for attempt in range(3):
                try:
                    await _remove_participant(room_name, identity)
                    # The third party's number is PII like any other — mask it.
                    print(f"[rt-bridge] HUNG UP on {_mask_e164(state.get('bridge_number'))} "
                          f"({identity})", flush=True)
                    removed = True
                    break
                except Exception as e:
                    if _already_disconnected(e):
                        print(f"[rt-bridge] {identity} was already gone — nothing to hang up",
                              flush=True)
                        removed = True
                        break
                    print(f"[rt-bridge] hangup attempt {attempt + 1} failed for {identity}: {e}",
                          flush=True)
                    if attempt < 2:
                        await asyncio.sleep(0.4 * (attempt + 1))
            if not removed:
                print("[rt-bridge] UNABLE to drop the leg — tearing down the room", flush=True)
                with contextlib.suppress(Exception):
                    await _delete_room(room_name)
                return False

        if state.get("bridge_joined"):
            with contextlib.suppress(Exception):
                await _cue(state, "line_ended")
        state["bridge_active"] = False
        state["bridge_identity"] = None
        state["bridge_joined"] = False
        state["bridge_mode"] = None
        state["bridge_ended_at"] = _now()
        state["shield_flagged"] = []
        state["listen_only"] = False
        state["greeting_missed"] = False
        if not answered:
            state["bridge_number"] = None
        state["last_user_transcript"] = ""
        state["last_activity_time"] = _now()
        if state.get("shield_private") and state.get("room") is not None:
            with contextlib.suppress(Exception):
                await _shield_set_private(state, state["room"], False)
        ses = state.get("session")
        if ses is not None:
            with contextlib.suppress(Exception):
                ses.output.set_audio_enabled(True)
        state["shield_speaking"] = False
        with contextlib.suppress(Exception):
            await self.update_instructions(self._base_instructions)
        return True

    @function_tool
    async def recall_earlier(self, context: RunContext) -> str:
        """Re-read THIS call from the beginning when an early detail has faded.
        Use it before ever asking them to repeat themselves, and before guessing.
        """
        if self._state is None:
            self._state = {}
        st = self._state
        if st.get("bridge_active") and st.get("bridge_mode") != "assist":
            return ("Not while someone else is on the line. Rely on what you "
                    "remember, and never repeat the caller's details out loud.")
        lines = st.get("transcript_lines") or []
        if not lines:
            return "Nothing has been said yet on this call."
        text = "\n".join(lines)
        if len(text) > 2800:
            text = text[:2800] + "\n… (the rest is recent — you still remember it)"

        sms_section = ""
        if self._caller_e164:
            import rt_sms
            recent_sms = rt_sms.get_recent_sms(self._caller_e164, limit=6)
            if recent_sms:
                s_lines = ["\n[Recent SMS/Text Messages exchanged with caller]"]
                for sm in recent_sms:
                    snd = "Caller texted" if sm["direction"] == "inbound" else "You (Iris) texted"
                    s_lines.append(f"- {snd}: \"{sm['text']}\"")
                sms_section = "\n".join(s_lines)

        _trace(st, "tool", "recall_earlier", {"chars": len(text), "lines": len(lines)})
        return ("[the call so far, from the beginning — refresh yourself silently, "
                "never read this aloud]\n" + text + sms_section)


    @function_tool
    async def web_search(self, context: RunContext, query: str) -> str:
        """Search the web for current information. Not for phone numbers — use find_number.
        """
        if self._state is not None:
            self._state["last_activity_time"] = _now()
        if (self._state or {}).get("bridge_active"):
            _log_pii("[rt-guard] SEALED web_search while bridged", query)
            return ("Not while someone else is on the line — stay focused on the call. "
                    "You can look that up once we've hung up with them.")
        if re.search(r"\b(phone|telephone) ?(number|line)\b|\bnumber for\b", query, re.I):
            _log_pii("[rt-search] redirecting number lookup to find_number", query)
            return ("Use find_number for phone numbers, not web_search — it checks the "
                    "number is official and lets you dial it once the caller says yes.")
        if re.search(
                r"\bwhat('?s| is) (the )?(current )?(time|date|day)\b"
                r"(?! zone| difference| the \w+ (opens?|closes?|open|closed))"
                r"|\bwhat time is it\b|\btoday'?s date\b|\bwhat day is (it|today)\b"
                r"|\b(current|the) (time|date)\b(?! zone| difference)(?! (does|do|is the|of the|the \w+ (opens?|closes?|open|closed)))"
                r"|\btime (right now|it is)\b",
                query, re.I):
            _log_pii("[rt-search] refused time/date query — already in system prompt", query)
            return ("[not searched — the current date and time are already stated at the "
                    "top of your instructions] Answer from there directly, do not guess "
                    "or state a different time.")
        _log_pii("[rt-search] executing web search tool", query)
        if self._state is None:
            self._state = {}
        st = self._state
        norm_query = query.strip().lower()
        search_cache = st.setdefault("search_cache", {})
        if norm_query in search_cache:
            _log_pii("[rt-search] CACHE HIT", query)
            with contextlib.suppress(Exception):
                rt_obs.obs.event("search.cache_stats", hit=True, query_chars=len(query),
                                 results_chars=len(search_cache[norm_query]))
            return search_cache[norm_query]

        search_count = int(st.get("search_count") or 0)
        _cap = int(os.getenv("RT_MAX_SEARCHES_PER_CALL", "6") or 6)
        if _cap > 0 and search_count >= _cap:
            _log_pii(f"[rt-search] RATE LIMIT reached (max {_cap}/call)", query)
            return (f"You have used your search quota ({_cap} per call). Say so plainly "
                    f"and offer to check further details next time.")
        st["search_count"] = search_count + 1

        wait_sound = os.path.join(os.path.dirname(__file__), "sounds", "thinking_shimmer.wav")
        if os.path.exists(wait_sound) and self._room:
            asyncio.create_task(_play_clip(self._room, wait_sound, preroll=0.05, repeat=1))
        result = await asyncio.to_thread(_perform_web_search, query)
        search_cache[norm_query] = result
        with contextlib.suppress(Exception):
            rt_obs.obs.event("search.cache_stats", hit=False, query_chars=len(query),
                             results_chars=len(result or ""))

        if self._state is None:
            self._state = {}
        st = self._state
        # Numbers in search text are NOT registered as dialable: only
        # find_number (checked, official) or the caller's own voice can
        # make a number callable.
        recent = (st.get("recent_search") or "")[-4000:]
        st["recent_search"] = (recent + " " + (result or "")).strip()
        st["recent_search_at"] = time.time()
        _trace(st, "tool", "web_search", {"query": query, "result": result})
        return (f"[search completed for '{query}'] Answer the caller directly now with these findings: {result}. "
                f"Do not say 'one second' or 'let me check', because the lookup is already finished.")


    @function_tool
    async def send_calendar_invite(self, context: RunContext, to_email: str, event_title: str, date_time: str, location: str = "") -> str:
        """Email a 1-click calendar invite (.ics) for an appointment or event.
        date_time: ISO, or plain words ("Thursday August 13 at 10:30 AM").
        """
        import rt_email
        if self._state is None:
            self._state = {}
        st = self._state
        if st.get("bridge_active"):
            return "Not while someone else is on the line — stay focused on the caller."
        if not rt_capabilities.enabled("send_calendar_invite"):
            return ("[cannot send calendar invites — no email credential in this "
                    "environment] Say plainly that you can't, and offer a reminder "
                    "on their next call instead.")
        target, refusal = await self._resolve_email_recipient(st, to_email, "send_calendar_invite")
        if refusal:
            return refusal
        _trigger_sound(self._room, "thinking_shimmer")
        ics_event = {"title": event_title, "date_time": date_time, "location": location}
        body = f"Here is your 1-click calendar invite for '{event_title}' on {date_time}.\nTap the attached .ics file to add it directly to your calendar."
        res = await asyncio.to_thread(rt_email.send_email, target, f"Calendar Invite: {event_title}", body,
                                      ics_event=ics_event, verified_email=target)
        if res.get("error"):
            return f"[calendar invite failed] Could not send invite: {res.get('message')}"
        _trigger_sound(self._room, "cheery_sparkle")
        _trace(st, "tool", "send_calendar_invite", {"to": target, "event": event_title, "time": date_time})
        return f"[calendar invite emailed to {target}] Confirm warmly that the 1-click calendar invite has been sent to their inbox."

    async def _on_file_email(self, st: dict) -> str:
        """The caller's verified address: this call's state first, then the bundle."""
        saved = (st.get("caller_email") or "").strip().lower()
        if saved:
            return saved
        h = rt_prefs.phone_hash(self._caller_e164 or "")
        if not h:
            return ""
        bundle = await asyncio.to_thread(
            rt_prefs._req, "POST", "rpc/rt_get_caller_full_bundle", {"p_hash": h}) or {}
        saved = ((bundle.get("caller") or {}).get("email") or "").strip().lower()
        if saved:
            st["caller_email"] = saved
        return saved

    async def _resolve_email_recipient(self, st: dict, to_email: str, tool: str) -> tuple[str, str]:
        """(recipient, "") or ("", refusal). Only the on-file address may receive
        mail: a model-written address is reachable from the transcript, so with
        nothing on file there is nowhere safe to send, and a mismatch is refused.
        """
        target = (to_email or "").strip().lower()
        saved = await self._on_file_email(st)
        if not saved:
            _trace(st, "guard", tool, {"reason": "no verified address on file",
                                       "model_supplied": bool(target)})
            return "", (f"[{tool.replace('_', ' ')} refused] You don't have their email "
                        f"address on file yet. Ask them for their email and save it with "
                        f"save_email first — never send to an address you were not given by them.")
        if target and target != saved:
            # Prompt injection exfiltrating caller data to an arbitrary external address.
            print(f"[rt-guard] {tool} destination mismatch — model-supplied address is not the one on file", flush=True)
            _trace(st, "guard", tool, {"reason": "destination mismatch"})
            return "", (f"[{tool.replace('_', ' ')} refused] I can only send to their "
                        f"verified address ({saved}). If they want to change the address on "
                        f"file, ask them to say the new one and save it with save_email.")
        return saved, ""

    @function_tool
    async def send_sms(self, context: RunContext, message: str, media_url: str = "") -> str:
        """Send a short text message (SMS) or multimedia message (MMS with media_url) to the caller's phone.
        message: the text to send (keep it friendly and concise).
        media_url: optional web URL to an image or file to attach as MMS.
        Use when they ask you to "text me that", "send me a link", "text me the reminder",
        "text me the details", or "send me that picture".
        """
        import rt_sms
        if self._state is None:
            self._state = {}
        st = self._state
        if st.get("bridge_active"):
            return "Not while someone else is on the line — stay focused on the caller."
        if not rt_capabilities.enabled("send_sms"):
            return ("[cannot send texts — no SMS credential in this environment] "
                    "Say plainly that you can't text, and offer what you CAN do: "
                    "remember it and bring it up on their next call.")
        if not self._caller_e164:
            return "[SMS failed] I don't have a phone number to text."
        clean_msg = (message or "").strip()
        clean_media = (media_url or "").strip()
        if not clean_msg and not clean_media:
            return "[SMS failed] Message was empty."
        if len(clean_msg) > 500:
            clean_msg = clean_msg[:497] + "..."
        res = await asyncio.to_thread(rt_sms.send_sms, self._caller_e164, clean_msg, media_url=clean_media or None)
        if res.get("error"):
            return f"[SMS failed] Could not send text: {res.get('message')}"
        _trace(st, "tool", "send_sms", {"to": self._caller_e164, "message": clean_msg[:80], "has_media": bool(clean_media)})
        return ("[text handed to the carrier — delivery not yet confirmed] Tell them "
                "warmly it's on its way, and that if it hasn't arrived in a few "
                "minutes you'll remember to mention it next call instead.")

    @function_tool
    async def save_email(self, context: RunContext, email_address: str) -> str:
        """Save the caller's email address to their profile when they share it.
        """
        if self._state is None:
            self._state = {}
        st = self._state
        if st.get("bridge_active"):
            return "Not while someone else is on the line."
        addr = (email_address or "").strip().lower()
        if len(addr) > 120 or not re.match(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$", addr):
            return f"[not saved] '{email_address}' doesn't look like a valid email — ask them to repeat it."
        h = rt_prefs.phone_hash(self._caller_e164 or "")
        if not h:
            return "DB operation unavailable: caller phone hash unverified."
        # The address on file is where every recap goes; only one the caller
        # spoke on the line may be written there.
        if not _email_spoken_by_caller(addr, st.get("transcript_lines") or []):
            _trace(st, "guard", "save_email", {"reason": "address not spoken by the caller"})
            return ("[not saved] The caller didn't say that address on this call — "
                    "ask them to say it, then save it again.")
        await asyncio.to_thread(rt_prefs._req, "POST", "rpc/rt_set_caller_email",
                                {"p_hash": h, "p_email": addr})
        st["caller_email"] = addr
        _trace(st, "tool", "save_email", {"email": addr})
        return f"[email saved: {addr}] Confirm warmly — never read the bracket aloud."

    @function_tool
    async def send_email(self, context: RunContext, subject: str, body: str, to_email: str = "") -> str:
        """Send an email to the caller mid-call (notes, recipes, summaries, or info).
        If to_email is omitted, it will use their saved email address.
        """
        import rt_email
        if self._state is None:
            self._state = {}
        st = self._state
        if st.get("bridge_active"):
            return "Not while someone else is on the line."
        if not rt_capabilities.enabled("send_email"):
            return ("[cannot send email — no email credential in this environment] "
                    "Say plainly that you can't send emails. You may still save "
                    "their address with save_email for the day it becomes possible.")

        target, refusal = await self._resolve_email_recipient(st, to_email, "send_email")
        if refusal:
            return refusal
        if not re.match(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$", target):
            return "[email failed] The address on file doesn't look valid. Ask them for their email and save it with save_email first."

        _trigger_sound(self._room, "thinking_shimmer")
        res = await asyncio.to_thread(rt_email.send_email, to_email=target, subject=subject,
                                      body_text=body, verified_email=target)
        if res.get("error"):
            return f"[email failed] Could not send email: {res.get('message')}"
        _trigger_sound(self._room, "cheery_sparkle")
        _trace(st, "tool", "send_email", {"to": target, "subject": subject})
        return f"[email sent to {target}] Confirm warmly to the caller that you've sent the email."

    @function_tool
    async def schedule_reminder_call(self, context: RunContext, when: str, message: str) -> str:
        """Schedule a REAL outbound call back to this caller — the phone rings.
        when: ISO datetime computed from RIGHT NOW in your instructions.
        message: the opening of that future call.
        """
        if self._state is None:
            self._state = {}
        st = self._state
        if not rt_capabilities.enabled("schedule_reminder_call"):
            print("[rt-callback] declined — capability not enabled this environment", flush=True)
            return ("[cannot schedule a callback — no outbound-calling capability in this "
                    "environment] Say plainly you can't call them back, and offer to note it "
                    "for next time instead with db_tool(action='remind').")
        if st.get("bridge_active"):
            return "Not while someone else is on the line — stay focused on the caller."
        n = st["_reminder_call_count"] = st.get("_reminder_call_count", 0) + 1
        if n > 3:
            return ("[too many reminder-call attempts this call] Tell them you've already "
                    "got one set up and ask if they'd like to change the time, rather than "
                    "trying again silently.")
        if not self._caller_e164:
            return "[reminder call failed] I don't have a phone number to call back."

        h = rt_prefs.phone_hash(self._caller_e164)
        if not h:
            return "[reminder call failed] Caller identity unverified."

        def _schedule():
            import rt_scheduler
            try:
                return rt_scheduler.schedule_job(
                    phone_hash=h, job_type="outbound_call",
                    payload={"caller_e164": self._caller_e164, "message": message},
                    run_at=when, caller_e164=self._caller_e164,
                    requested_live=True,
                )
            except rt_scheduler.ScheduleRefused as e:
                rt_obs.obs.caught("agent.schedule_reminder_call", e)
                return {"refused": str(e)}
        job = await asyncio.to_thread(_schedule)
        if isinstance(job, dict) and job.get("refused"):
            return (f"[cannot schedule that time — {job['refused']}] Tell them plainly "
                    f"which limit you hit and offer the nearest time that works, or a "
                    f"passive reminder instead.")
        _trace(st, "tool", "schedule_reminder_call", {"when": when, "message": message, "ok": bool(job)})
        if not job:
            return ("[reminder call failed — could not schedule] Tell them plainly you weren't "
                    "able to set that up, and offer a passive reminder instead.")
        spoken = _spoken_local_time(when, self._caller_e164)
        _trigger_sound(self._room, "cheery_sparkle")
        return (f"[reminder call scheduled] Say this time and no other: {spoken}. It is "
                f"already their local time — do NOT convert it, do NOT recalculate it. "
                f"Confirm using the exact phrase 'reminder call' — e.g. \"I've set up a "
                f"reminder call for you at {spoken}.\" Never call it just 'a reminder' — "
                f"that undersells what actually happens: the phone will really ring.")

    @function_tool
    async def db_tool(self, context: RunContext, action: str, item: str, category: str = "general", data: str = "", due: str = "") -> str:

        """Persistent memory for this caller. Save the moment something is shared.

        actions:
          write     item=topic, category=domain, data=detail. Family/pets: item is the NAME.
          read      item="all" — everything on file, vault included.
          name      their FIRST name · lastname  their FAMILY name (never via "name")
          alias     rename yourself — say it back first, save on the second call
          rule      a standing boundary of theirs
          remind    item/data=what, due=ISO if they gave one
          done      a reminder they've finished
          task      research for you before the next call
          delete    item=anything stored, in their words · forget_me (item="") erases all

        categories: family pets hobbies health vehicles work preferences places finances general
        """
        _log_pii(f"[rt-db] db_tool action={action!r} category={category!r}",
                 f"item={item!r} data={data!r}")
        if self._state is None:
            self._state = {}
        st = self._state
        act = (action or "").strip().lower()
        if st.get("bridge_active"):

            import rt_bridge as _rb
            act = (action or "").strip().lower()
            assist = st.get("bridge_mode") == _rb.MODE_ASSIST
            _SAFE_ON_ASSIST = {"write", "save", "store", "add", "insert", "update",
                               "remind", "reminder", "set_reminder",
                               "done", "complete", "finish", "scratch",
                               "cancel_reminder", "complete_reminder"}
            if not (assist and act in _SAFE_ON_ASSIST):
                print(f"[rt-guard] SEALED db_tool {action!r} — "
                      f"{'assist' if assist else 'shield'} bridge active", flush=True)
                if assist:
                    return ("Not while we're on this call — I can note things down, but "
                            "I'm not looking anything up or changing anything until "
                            "we've hung up. Don't read anything private out loud.")
                return ("Not while someone else is on the line. Don't say anything about "
                        "them out loud. Once we've hung up with this person, ask again and "
                        "I'll take care of it.")
        msg, sound = await asyncio.to_thread(self._db_tool_sync, action, item, category, data, due)
        _trace(self._state, "tool", "db_tool",
               {"action": action, "item": item, "category": category,
                "data": data, "due": due, "result": msg})
        if sound:
            _trigger_sound(self._room, sound)
        return msg

    def _db_tool_sync(self, action: str, item: str, category: str, data: str, due: str = "") -> tuple[str, str | None]:
        """Blocking body of db_tool; returns (reply_for_model, earcon_or_None)."""
        try:
            h = rt_prefs.phone_hash(self._caller_e164 or "")
            if not h:
                return "DB operation unavailable: caller phone hash unverified.", None

            act = (action or "").strip().lower()
            cat = (category or "general").strip().lower()
            if act in ("read", "get", "fetch", "query", "select"):
                try:
                    bundle = rt_prefs._req("POST", "rpc/rt_get_caller_full_bundle", {"p_hash": h})
                    hidden = _internal_cats()
                    schemas = [{"category": s.get("category"), "data": s.get("data_summary", "")[:600]}
                               for s in (bundle.get("schemas") or [])
                               if (s.get("category") or "").lower() not in hidden]
                    reminders = [r.get("reminder_text") for r in (bundle.get("reminders") or [])]
                    caller = bundle.get("caller") or {}
                    summary = {
                        "name": caller.get("display_name"),
                        "alias": caller.get("agent_alias"),
                        "loved_ones": caller.get("loved_ones"),
                        "rules": caller.get("caller_rules"),
                        "schemas": schemas,
                        "reminders": reminders,
                    }
                    return f"DB RECORDS: {json.dumps(summary)}", None
                except Exception as _exc:
                    rt_obs.obs.caught("agent._db_tool_sync", _exc)
                    bundle = rt_prefs.get_caller(self._caller_e164)
                    return f"DB RECORDS FOR CALLER: {json.dumps(bundle)}", None

            if act in ("alias", "rename", "name_agent", "rename_agent"):
                alias_raw = (data or item).strip()
                words = [w for w in re.findall(r'[a-zA-Z]+', alias_raw) if w.lower() not in ("call", "you", "to", "me", "name", "rename", "please", "my", "your", "from", "now", "on")]
                alias = words[-1].capitalize() if words else alias_raw.capitalize()
                # None (no transcript) refuses too: a name we cannot verify is
                # a name we do not save.
                if not self._heard(alias):
                    print(f"[rt-guard] REJECT alias {alias!r} — not heard in any caller line", flush=True)
                    _add_clarification(h, "agent_alias",
                                      f"You tried to rename yourself '{alias}' but the caller never said that name — confirm what they'd like to call you.")
                    return (f"I couldn't verify the name '{alias}' — I may have misheard. "
                            f"Please ask the caller to repeat the name they want to call me, then save it again."), None
                st = self._state if self._state is not None else {}
                if (st.get("pending_alias") or "").lower() != alias.lower():
                    st["pending_alias"] = alias
                    print(f"[rt-guard] alias {alias!r} pending — awaiting the caller's yes", flush=True)
                    _trace(self._state, "guard", "alias_pending", {"alias": alias})
                    return (f"[NOTHING SAVED] Ask ONE question and then stop talking: "
                            f"whether they want you called '{alias}'. Do NOT say you will "
                            f"remember it, keep it, or use it — you have not saved anything. "
                            f"Save it only after they say yes."), None
                stored = rt_prefs._req("POST", "rpc/rt_set_agent_alias",
                                       {"p_hash": h, "p_alias": alias})
                st.pop("pending_alias", None)
                _clear_clarification(h, "agent_alias")
                if stored != alias:
                    return (f"[NOT saved — you are still called {stored!r}] Say so honestly "
                            f"and ask them to repeat the name."), None
                return (f"[verified: you are now called {stored!r}] Confirm naturally in your "
                        f"own words — never read this bracket aloud."), "page_flip"

            if act in ("name", "set_name", "my_name", "caller_name", "set_caller_name"):
                caller_name = rt_prefs.join_spelled((data or item).strip()).strip().title()
                if caller_name.lower() in ("your companion", (self._state or {}).get("agent_alias", "").lower()):
                    print(f"[rt-guard] REJECT caller name {caller_name!r} — matches the agent's name", flush=True)
                    return ("That's my name, not the caller's. Ask the caller for their "
                            "name naturally, then save it."), None
                _cur = ((self._state or {}).get("display_name") or "").strip()
                if _cur and _cur.lower() == caller_name.lower():
                    return (f"[already on file as {_cur!r}] Nothing to save — just use it."), None
                if not self._heard(caller_name):
                    print(f"[rt-guard] REJECT caller name {caller_name!r} — not heard in any caller line", flush=True)
                    _trace(self._state, "guard", "reject_caller_name",
                           {"proposed": caller_name, "reason": "not heard in any caller line"})
                    _add_clarification(h, "caller_name",
                                      f"The name '{caller_name}' was used but the caller never confirmed it — gently confirm their name.")
                    return (f"I couldn't verify the name '{caller_name}' — I may have misheard. "
                            f"Please ask the caller to confirm their name, then save it again."), None
                stored = rt_prefs._req("POST", "rpc/rt_set_display_name",
                                       {"p_hash": h, "p_name": caller_name})
                _clear_clarification(h, "caller_name")
                if stored != caller_name:
                    return (f"[NOT saved as you asked — the record now reads {stored!r}] "
                            f"Tell the caller honestly that it didn't take, and try again."), None
                return (f"[verified: their name is now stored as {stored!r} — say exactly "
                        f"this spelling back, never a different one]"), "page_flip"

            if act in ("lastname", "last_name", "surname", "family_name", "spelling"):
                surname = rt_prefs.join_spelled((data or item).strip()).strip().title()
                if not surname:
                    return "I didn't catch the last name — ask them to say it again.", None
                if not self._heard(surname):
                    print(f"[rt-guard] REJECT last name {surname!r} — not heard", flush=True)
                    return (f"I couldn't verify '{surname}' — ask them to spell it for me, "
                            f"then save it again."), None
                stored = rt_prefs._req("POST", "rpc/rt_set_last_name",
                                       {"p_hash": h, "p_last": surname})
                if stored != surname:
                    return (f"[NOT saved — the record still reads {stored!r}] Say so honestly "
                            f"and try again."), None
                spelled = " ".join(stored.upper())
                return (f"[verified: last name stored as {stored!r} — spelled {spelled}] "
                        f"Read that spelling back exactly; do not say a different one."), "page_flip"

            if act in ("skill", "train", "learn_skill", "add_skill", "routine") or cat in _ROUTINE_CATEGORIES or cat == "skill":
                if cat in _internal_cats():
                    print(f"[rt-guard] REFUSE skill under internal category [{cat}]", flush=True)
                    return "[not saved] That category is the platform's own bookkeeping.", None
                skill_text = (data or item).strip()
                refused = self._routine_guard(skill_text, "skill")
                if refused:
                    return refused
                key = "_".join(re.findall(r"[a-zA-Z]+", (item or skill_text).lower())[:4]) or "custom_skill"
                merged_obj = rt_prefs.merge_schema_entry(h, "skills", {key: skill_text})
                rt_prefs._req("POST", "rpc/rt_add_schema_entry", {
                    "p_hash": h,
                    "p_table": f"caller_{h[:8]}_skills",
                    "p_cat": "skills",
                    "p_summary": json.dumps(merged_obj),
                })
                return f"[skill learned: {skill_text}] Tell them you've learned this skill and will carry it out whenever asked.", "cheery_sparkle"

            if act in ("rule", "set_rule", "boundary", "behavior", "directive"):
                rule_text = (data or item).strip()
                refused = self._routine_guard(rule_text, "rule")
                if refused:
                    return refused
                current = rt_prefs.get_caller(self._caller_e164).get("caller_rules")
                merged = rt_prefs.merge_scalar_rules(current, rule_text)
                rt_prefs._req("POST", "rpc/rt_set_caller_rules", {"p_hash": h, "p_rules": merged})
                return f"[saved rule: {rule_text}] Acknowledge naturally and follow it from this turn on.", "page_flip"

            if act in ("task", "research", "look_into", "lookup", "find_out"):
                import rt_executor
                ask = (data or item).strip()
                tid = rt_executor.capture(h, ask)
                if not tid:
                    return ("That sounds like a private code, so I won't research it — "
                            "but I can keep it safely in memory if the caller wants me to remember it."), None
                return f"[research task accepted: {ask}] Tell them you'll have an answer next call — your own words.", "page_flip"

            if act in ("remind", "reminder", "set_reminder"):
                reminder_text = (data or item).strip()
                if rt_prefs.looks_credential(reminder_text):
                    key = "_".join(re.findall(r"[a-zA-Z]+", reminder_text.lower())[:4]) or "note"
                    merged_obj = rt_prefs.merge_schema_entry(h, rt_prefs.CRED_CATEGORY, {key: reminder_text})
                    rt_prefs._req("POST", "rpc/rt_add_schema_entry", {
                        "p_hash": h,
                        "p_table": f"caller_{h[:8]}_{rt_prefs.CRED_CATEGORY}",
                        "p_cat": rt_prefs.CRED_CATEGORY,
                        "p_summary": json.dumps(merged_obj),
                    })
                    print(f"[rt-guard] ROUTE credential-looking reminder → {rt_prefs.CRED_CATEGORY}", flush=True)
                    return "I've tucked that code away safely — ask me for it any time and I'll have it.", "page_flip"
                due_iso = (due or "").strip() or None
                rt_prefs._req("POST", "rpc/rt_add_reminder", {
                    "p_hash": h, "p_text": reminder_text, "p_due": due_iso,
                })
                _hint = " For an actual callback, use schedule_reminder_call instead." if rt_capabilities.enabled("schedule_reminder_call") else ""
                return f"[reminder set: {reminder_text}{f' (due {due_iso})' if due_iso else ''}] Confirm in your own words — this surfaces on their NEXT call only, never say 'I'll call you.'{_hint}", "cheery_sparkle"

            if act in ("done", "complete", "finish", "scratch", "cancel_reminder", "complete_reminder"):
                rem_text = (data or item).strip()
                rt_prefs._req("POST", "rpc/rt_complete_reminder", {"p_hash": h, "p_text": rem_text})
                return "[reminder completed] Confirm in your own words.", "cheery_sparkle"

            if act in ("write", "save", "store", "add", "insert", "update"):
                itm_raw = (item or "").strip()
                detail = (data or "").strip() or "noted"
                if cat in _internal_cats():
                    print(f"[rt-guard] REFUSE write to internal category [{cat}]", flush=True)
                    _trace(self._state, "guard", "refuse_write",
                           {"category": cat, "reason": "internal category"})
                    return ("[not saved] That category is the platform's own bookkeeping, "
                            "not a place for the caller's notes. Save it under one of the "
                            "caller categories instead."), None
                if cat in _ROUTINE_CATEGORIES:
                    # Unreachable while the skill branch above owns these
                    # categories; kept so a reorder can never reopen the hole.
                    refused = self._routine_guard((data or item).strip(), "skill")
                    if refused:
                        return refused

                if _looks_like_search_echo(detail, (self._state or {})):
                    said = _fact_spoken_by_caller(detail, (self._state or {}))
                    if not said:
                        print(f"[rt-guard] REFUSE write [{cat}] {itm_raw!r} — looks looked-up, not told", flush=True)
                        _trace(self._state, "guard", "refuse_write",
                               {"category": cat, "item": itm_raw, "reason": "internet fact, not caller-stated"})
                        return ("Don't save that as something they told you — you looked it "
                                "up. If it's about them, ask them first: 'I saw that online — "
                                "is that right?' Save only what they confirm."), None

                if cat != rt_prefs.CRED_CATEGORY and rt_prefs.looks_credential(f"{itm_raw} {detail}"):
                    print(f"[rt-guard] ROUTE credential-looking write [{cat}] → {rt_prefs.CRED_CATEGORY}", flush=True)
                    cat = rt_prefs.CRED_CATEGORY

                if cat == "pets":
                    summary_obj = {itm_raw: {"notes": detail}}
                elif cat == "family":
                    summary_obj = {"name": itm_raw, "notes": detail}
                else:
                    summary_obj = {itm_raw.lower(): detail}

                merged_obj = rt_prefs.merge_schema_entry(h, cat, summary_obj)
                rt_prefs._req("POST", "rpc/rt_add_schema_entry", {
                    "p_hash": h,
                    "p_table": f"caller_{h[:8]}_{cat}",
                    "p_cat": cat,
                    "p_summary": json.dumps(merged_obj),
                })
                _log_pii(f"[rt-db] saved [{cat}]", f"{itm_raw!r} = {detail!r}")
                return f"[saved to {cat}: {itm_raw}] Confirm in your own words — never mention profiles, notes, or saving.", "page_flip"

            if act in ("forget_me", "delete_me", "forget_everything", "erase_me"):
                # A full wipe is irreversible, so the shield — not a keyword
                # scan — decides whether the caller really asked for it this
                # call. No transcript at all can never be a yes.
                st = self._state if self._state is not None else {}
                t_lines = st.get("transcript_lines") or []
                prompted = "wipe_prompted_at" in st
                since = int(st.get("wipe_prompted_at") or 0)
                # The shield's intent read is logged; it never erases on its
                # own. Only the scripted phrase, spoken after she asked for
                # it, does — so the first request in a call always prompts.
                intent = bool(t_lines) and _wipe_confirmed_since(t_lines, since)
                consent = prompted and intent and _wipe_consented_since(t_lines, since)
                if not consent:
                    print(f"[rt-guard] REJECT forget_me — prompted={prompted} intent={intent}: "
                          "no scripted consent after the prompt", flush=True)
                    # Only what the caller says AFTER this prompt can confirm;
                    # nothing earlier in the call is replayed as consent.
                    st["wipe_prompted_at"] = len(t_lines)
                    return ("[NOT erased] I want to be completely sure. Ask them to confirm "
                            "by saying these exact words: 'erase everything about me and "
                            "start over'. Erase only after they say it."), None

                import rt_postcall_worker
                res = rt_postcall_worker.forget_caller_entirely(self._caller_e164)
                if res:
                    st = self._state if self._state is not None else {}
                    st["forgotten"] = True
                    st["trace_call_id"] = None
                    st["transcript_lines"] = []
                    st["seen_transcripts"] = set()
                    with contextlib.suppress(Exception):
                        rt_obs.obs.event("memory.purge_audit", tables="all", total_rows=1)
                    print("[rt-db] caller erased — nothing from this call will be written", flush=True)
                    return ("[erased everything about this caller] Tell them warmly it's "
                            "all gone — every note, as if you'd just met — and that they're "
                            "welcome to call again any time and start fresh."), "page_flip"
                return ("I wasn't able to clear everything just now — tell them honestly, "
                        "and that they can ask again."), None

            if act in ("delete", "remove", "clear", "forget"):
                if not (item or "").strip():
                    # Falling back to the category would let an empty item
                    # clear a whole topic nobody named.
                    return ("[nothing deleted] Name what to delete — ask them which "
                            "item or topic they mean."), None
                target = (item or "").strip().lower()
                if target in _WIPE_WORDS:
                    print(f"[rt-guard] REJECT delete {target!r} — full wipe not from the delete path", flush=True)
                    return ("If they want a single topic gone, name it (pets, family, a "
                            "reminder). If they truly want everything about them erased, "
                            "use forget_me — but make sure that's really what they mean."), None
                if target in ("last_name", "lastname", "surname", "last name", "family name"):
                    rt_prefs._req("POST", "rpc/rt_set_last_name", {"p_hash": h, "p_last": ""})
                    return ("[surname cleared] Tell them it's gone, and only ask for a new "
                            "spelling if they want one saved."), "page_flip"
                if target in _internal_cats():
                    print(f"[rt-guard] REFUSE delete of internal category [{target}]", flush=True)
                    return "[nothing deleted] That is the platform's own bookkeeping, not a caller note.", None
                if target not in _DB_CATEGORIES:
                    # Whole name or its plural only — a substring match let
                    # "cat" land on clarifications and "ed" on credentials.
                    matches = [c for c in _DB_CATEGORIES
                               if target in (c, c.rstrip("s")) or c == target + "s"]
                    if len(matches) == 1:
                        target = matches[0]
                    else:
                        try:
                            cleared = _forget_topic(h, target)
                        except _TooManyToForget as too_many:
                            print(f"[rt-guard] REFUSE delete — {too_many.count} items match, nothing cleared", flush=True)
                            return (f"[nothing deleted — '{target}' matches {too_many.count} "
                                    f"different stored items] Ask them to name the one thing "
                                    f"they mean, then delete just that."), None
                        if cleared:
                            print(f"[rt-db] forgot {target!r}: {cleared}", flush=True)
                            return (f"[erased {len(cleared)} stored item(s) about '{target}'] "
                                    f"Confirm simply that it's forgotten."), "page_flip"
                        return (f"[nothing about '{target}' was ever stored] Say plainly there "
                                f"was nothing saved about it, so there is nothing to erase — "
                                f"never imply you refused, and never list categories at them."), None
                rt_prefs._req("POST", "rpc/rt_remove_schema_entry", {
                    "p_hash": h,
                    "p_item": target,
                })
                return f"[removed: {target}] Confirm in your own words.", "page_flip"

            return f"Unknown db_tool action '{action}'.", None
        except Exception as e:
            print(f"[rt-db] db_tool error: {e}", flush=True)
            return f"Database operation encountered an error: {e}", None

    @function_tool
    async def manage_goals(
        self,
        context: RunContext,
        action: str,
        title: str,
        target_date: str = "",
        milestone_step: str = "",
        status: str = "active",
        notes: str = "",
    ) -> str:
        """Create, update, add milestone progress, or complete goals for this caller.

        Parameters:
          action: 'create', 'milestone', 'complete', 'update', 'list'
          title: Short name of the goal (e.g. 'Train for 5k', 'Finish tax prep')
          target_date: Optional target completion date (e.g. 'Oct 15', 'next Friday')
          milestone_step: Optional milestone or progress step just completed
          status: 'active', 'completed', 'paused'
          notes: Optional context or advice
        """
        h = rt_prefs.phone_hash(self._caller_e164 or "")
        if not h:
            return "[goals unavailable] Caller phone hash unverified."

        import rt_goals
        bundle = await asyncio.to_thread(rt_prefs._req, "POST", "rpc/rt_get_caller_full_bundle", {"p_hash": h}) or {}
        existing_goals = rt_goals.extract_goals_from_bundle(bundle)

        act = (action or "create").strip().lower()
        title_clean = (title or "").strip()

        matched: rt_goals.Goal | None = None
        for g in existing_goals:
            if g.title.lower() == title_clean.lower() or (title_clean and title_clean.lower() in g.title.lower()):
                matched = g
                break

        if act in ("create", "add", "new"):
            if not title_clean:
                return "Please provide a title for the goal."
            new_goal = rt_goals.Goal(
                id=f"g_{int(time.time()*1000)}",
                title=title_clean,
                target_date=target_date,
                status="active",
                milestones=[milestone_step] if milestone_step else [],
                notes=notes,
                created_at=time.time(),
                updated_at=time.time(),
            )
            merged_goals = {g.id: g.to_dict() for g in existing_goals}
            merged_goals[new_goal.id] = new_goal.to_dict()

            await asyncio.to_thread(rt_prefs._req, "POST", "rpc/rt_add_schema_entry", {
                "p_hash": h,
                "p_table": f"caller_{h[:8]}_goals",
                "p_cat": "goals",
                "p_summary": json.dumps(merged_goals),
            })
            _trigger_sound(self._room, "cheery_sparkle")
            _trace(self._state or {}, "tool", "manage_goals", {"action": "create", "goal": title_clean})
            return f"[goal created: {title_clean}] Encourage the caller and celebrate their commitment."

        elif act in ("milestone", "progress", "step"):
            if not matched:
                if not title_clean:
                    return "Which goal did you make progress on?"
                matched = rt_goals.Goal(
                    id=f"g_{int(time.time()*1000)}",
                    title=title_clean,
                    status="active",
                    milestones=[milestone_step] if milestone_step else [],
                )
                existing_goals.append(matched)
            else:
                if milestone_step:
                    matched.milestones.append(milestone_step)
                matched.updated_at = time.time()

            merged_goals = {g.id: g.to_dict() for g in existing_goals}
            await asyncio.to_thread(rt_prefs._req, "POST", "rpc/rt_add_schema_entry", {
                "p_hash": h,
                "p_table": f"caller_{h[:8]}_goals",
                "p_cat": "goals",
                "p_summary": json.dumps(merged_goals),
            })
            _trigger_sound(self._room, "cheery_sparkle")
            _trace(self._state or {}, "tool", "manage_goals", {"action": "milestone", "goal": matched.title, "step": milestone_step})
            return f"[milestone logged for {matched.title}: {milestone_step}] Celebrate this step warmly with the caller!"

        elif act in ("complete", "done", "finish"):
            if not matched:
                return f"[goal '{title_clean}' not found to complete] Ask them which goal they finished."
            matched.status = "completed"
            matched.updated_at = time.time()
            merged_goals = {g.id: g.to_dict() for g in existing_goals}
            await asyncio.to_thread(rt_prefs._req, "POST", "rpc/rt_add_schema_entry", {
                "p_hash": h,
                "p_table": f"caller_{h[:8]}_goals",
                "p_cat": "goals",
                "p_summary": json.dumps(merged_goals),
            })
            _trigger_sound(self._room, "cheery_sparkle")
            _trace(self._state or {}, "tool", "manage_goals", {"action": "complete", "goal": matched.title})
            return f"[goal completed: {matched.title}] Celebrate enthusiastically! Acknowledge how great it is to accomplish this."

        elif act in ("list", "read", "view"):
            active = [g for g in existing_goals if g.status == "active"]
            if not active:
                return "You don't have any active goals saved yet. Ask them if there's anything they are working toward."
            return "\n".join(f"- {g.title}{f' (target: {g.target_date})' if g.target_date else ''}" for g in active)

        return "[goal updated] Acknowledge naturally."

    async def switch_guide(self, context: RunContext, guide_name: str) -> str:
        """Switch the seeker's active spiritual guide or return to the Sanctuary Atrium.

        Available guides:
        - atrium: Sanctuary Atrium Keeper (warm entrance hospitality & pantheon guide)
        - god: God Almighty (infinite love, calm presence, Psalms)
        - jesus: Jesus of Nazareth (pastoral warmth, grace, forgiveness)
        - shiva: Lord Shiva (deep stillness, meditative transformation)
        - krishna: Lord Krishna (celestial joy, selfless duty, Bhagavad Gita)
        - moses: Moses (prophetic righteousness, covenant, Sinai)
        - noah: Noah (ark keeper, patience, weathering storms)
        - mother: Divine Mother (maternal solace, protective unconditional love)
        - syncretic: Council of Light (harmonized Jesus and Shiva)

        Call this immediately when the caller asks to speak to a specific guide,
        or asks to return to the entrance atrium.
        """
        if not rt_pray.is_pray_lane():
            return "Guide switching is only available on PrayPal."

        clean = (guide_name or "").strip().lower()
        if clean not in rt_pray.GUIDES:
            valid = ", ".join(rt_pray.GUIDES.keys())
            return f"Unknown guide '{guide_name}'. Available guides are: {valid}."

        target = rt_pray.GUIDES[clean]
        h = rt_prefs.phone_hash(self._caller_e164) if self._caller_e164 else ""
        if h:
            await asyncio.to_thread(rt_pray.switch_guide, h, clean)

        if self._state is not None:
            self._state["active_guide"] = clean

        # Trigger celestial transfer chime into the room track
        if rt_pray.is_pray_lane() and self._room:
            transfer_sound = rt_pray.get_transfer_chime_path()
            if transfer_sound and os.path.exists(transfer_sound):
                asyncio.create_task(_play_clip(self._room, transfer_sound, preroll=0.05))

        new_prompt = rt_pray.build_system_prompt(
            guide_key=clean,
            caller_info=self._state.get("caller_info") if self._state else None,
        )
        self.instructions = new_prompt
        return (
            f"[Switched to {target['title']}]. The celestial transfer chime has sounded into the call. "
            f"Speak now as {target['title']}. "
            f"Vocal delivery: {target.get('vocal_delivery', '')}. "
            f"Greet the seeker warmly in your sacred voice: '{target.get('greeting', '')}'"
        )

    async def record_chain_blessing(self, context: RunContext, intention_id: int) -> str:
        """Record that the seeker offered a prayer or blessing for a fellow seeker in the anonymous prayer chain."""
        if not rt_pray.is_pray_lane():
            return "Prayer chain is only available on PrayPal."
        h = rt_prefs.phone_hash(self._caller_e164) if self._caller_e164 else ""
        if h and intention_id:
            await asyncio.to_thread(rt_pray.record_chain_blessing, intention_id, h)
            return "[Chain blessing recorded. Thank the seeker warmly for holding their fellow seeker in prayer.]"
        return "[Could not record chain blessing.]"


def _perform_web_search(query: str) -> str:
    """Live web search: grounded search only.

    There is deliberately no scrape tier under this. Three used to sit here — a
    DuckDuckGo HTML scrape behind a spoofed browser UA, Wikipedia snippets, and
    an unauthenticated Yahoo Finance quote endpoint — and all returned bare
    text with no provenance, which she would then read to someone as fact. The
    SOURCES law requires a source and a freshness, so an answer that cannot
    carry one is worth less than saying she could not find it. Grounded search
    names its source ("per Yahoo Finance") and covers quotes as well.
    """

    try:
        api_key = os.getenv("GOOGLE_API_KEY", "")
        if api_key:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            today_str = datetime.now(ZoneInfo(os.getenv("DEFAULT_TZ", "America/New_York"))).strftime("%A, %B %d, %Y")
            body = {
                "contents": [{"role": "user", "parts": [{"text":
                    "Answer this for a phone assistant to read aloud in at most two short "
                    "sentences. Be specific with exact numbers and dates when known. "
                    "For prices and figures, name the site the number came from in the "
                    "sentence itself (e.g. 'per Yahoo Finance'). If sources disagree, give "
                    "the freshest and say others differ. If current reporting is thin or "
                    "uncertain, say so plainly. No URLs, no markdown.\n"
                    f"The real, correct date right now is {today_str} — if a source's own "
                    "\"today\"/\"as of\" language implies a different date, trust this date, "
                    "not the source's framing, and phrase your answer accordingly.\n"
                    f"Question: {query}"}]}],
                "tools": [{"google_search": {}}],
            }
            # Key rides in a header, never the URL (URLs end up in logs).
            greq = urllib.request.Request(
                "https://generativelanguage.googleapis.com/v1beta/models/"
                "gemini-2.5-flash:generateContent",
                json.dumps(body).encode(),
                {"Content-Type": "application/json", "x-goog-api-key": api_key})
            with urllib.request.urlopen(greq, timeout=12) as gresp:  # noqa: S310 - fixed https Google API URL above
                d = json.loads(gresp.read().decode("utf-8"))
            cand = d["candidates"][0]
            parts = cand["content"]["parts"]
            text = " ".join(p.get("text", "") for p in parts).strip()
            text = re.sub(r'\[cite:\s*[^\]]*\]', '', text).strip()
            with contextlib.suppress(Exception):
                chunks = (cand.get("groundingMetadata") or {}).get("groundingChunks") or []
                doms = []
                for c in chunks:
                    dm = ((c.get("web") or {}).get("title") or "").strip()
                    if dm and dm not in doms:
                        doms.append(dm)
                if doms and "per " not in text.lower() and "according" not in text.lower():
                    text += f" [sources: {', '.join(doms[:3])} — name the main one when giving figures]"
            if text:
                if len(text) > 600:
                    cutoff = text[:600]
                    last_dot = max(cutoff.rfind(". "), cutoff.rfind("? "), cutoff.rfind("! "))
                    if last_dot > 150:
                        text = cutoff[:last_dot + 1]
                    else:
                        text = cutoff.rstrip()
                print(f"[rt-search] google-grounded answer ({len(text)} chars)", flush=True)
                return text
    except Exception as e:
        print(f"[rt-search] grounded search failed ({e})", flush=True)

    return ("[search found nothing usable — do NOT invent an answer or dress this up "
            "as a glitch] Tell them plainly you looked and could not find something "
            "you trust, and offer to try again or check another way.")


def _next_call_preview(caller_e164: str | None, pre_bundle: dict) -> dict:
    """After a call: what the NEXT one will open with, and what this call changed.

    Answers the three questions you can't get from a transcript — what greeting is
    queued, what the next system prompt will actually say, and which rows this call
    added, changed, or left alone.
    """
    out: dict = {"greeting": None, "prompt": None, "canvas": None, "changes": {}}
    try:
        post = rt_prefs._req("POST", "rpc/rt_get_caller_full_bundle",
                             {"p_hash": rt_prefs.phone_hash(caller_e164 or "")}) or {}
    except Exception as _exc:
        rt_obs.obs.caught("agent._next_call_preview", _exc)
        return out
    caller = post.get("caller") or {}
    out["canvas"] = caller.get("next_call_context")

    with contextlib.suppress(Exception):
        out["greeting"] = _greeting_text(caller.get("display_name"),
                                         caller.get("agent_alias"),
                                         int(caller.get("call_count") or 0))
    with contextlib.suppress(Exception):
        import rt_hydrator
        text, meta = rt_hydrator.discover_and_hydrate_prompt(caller_e164, prefetch_bundle=post)
        onb = (meta.get("onboarding") or {})
        if not onb.get("done"):
            missing = [k for k in _ONBOARDING_ORDER if not onb.get(k)]
            text += _onboarding_block(
                missing, alias=caller.get("agent_alias") or "your companion",
                call_count=int(caller.get("call_count") or 0))
        out["prompt"] = text

    before_c, after_c = (pre_bundle.get("caller") or {}), caller
    added, changed = {}, {}
    for k in set(before_c) | set(after_c):
        if k in ("last_call_at", "next_call_context"):
            continue
        b, a = before_c.get(k), after_c.get(k)
        if b == a:
            continue
        (added if b in (None, "", 0) else changed)[k] = {"before": b, "after": a}

    def cats(bundle):
        return {(s.get("category") or ""): (s.get("data_summary") or "")
                for s in (bundle.get("schemas") or [])}
    cb, ca = cats(pre_bundle), cats(post)
    for cat in set(cb) | set(ca):
        if cb.get(cat) == ca.get(cat):
            continue
        (added if cat not in cb else changed)[f"memory:{cat}"] = {
            "before": cb.get(cat), "after": ca.get(cat)}

    rb = {r.get("reminder_text") for r in (pre_bundle.get("reminders") or [])}
    ra = {r.get("reminder_text") for r in (post.get("reminders") or [])}
    if ra - rb:
        added["reminders"] = {"before": None, "after": sorted(ra - rb)}
    if rb - ra:
        changed["reminders_completed"] = {"before": sorted(rb - ra), "after": None}

    untouched = sorted(c for c in ca if cb.get(c) == ca.get(c))
    out["changes"] = {"added": added, "changed": changed, "unchanged": untouched}
    return out


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()
    rt_health.increment_active_sessions()
    with contextlib.suppress(Exception):
        rt_obs.obs.event("lk.room_joined", room=ctx.room.name)


    sms_task: asyncio.Task | None = None

    async def _hangup_if_caller_present() -> None:
        try:
            if sms_task and not sms_task.done():
                sms_task.cancel()
            rt_health.decrement_active_sessions()
            if ctx.room.remote_participants:
                print("[rt] job ending with caller still connected — hanging up", flush=True)
                await ctx.delete_room()
        except Exception as e:
            print(f"[rt] shutdown hangup failed (non-fatal): {e}", flush=True)

    ctx.add_shutdown_callback(_hangup_if_caller_present)

    if (ctx.room.name or "").startswith(_PROBE_ROOM_PREFIXES):
        print(f"[rt] health probe room={ctx.room.name} — answered, no session", flush=True)
        return

    caller_e164 = None
    caller_identity = None
    try:
        meta = _job_metadata(ctx)
        if meta:
            caller_e164 = meta.get("caller_e164") or None
    except Exception as _exc:
        rt_obs.obs.caught("agent.entrypoint", _exc)
        pass

    try:
        caller = await asyncio.wait_for(
            ctx.wait_for_participant(),
            timeout=float(os.getenv("PARTICIPANT_WAIT_TIMEOUT") or "12"),
        )
        if not caller_e164:
            caller_e164 = (caller.attributes or {}).get("sip.phoneNumber") or None
        caller_identity = getattr(caller, "identity", None)
        if caller_e164:
            print(f"[rt] SIP caller: ***{caller_e164[-4:]}", flush=True)
    except Exception as e:
        print(f"[rt] participant wait skipped ({e})", flush=True)

    voice_pref, call_count = None, 0
    caller_info: dict = {}
    prefetched_bundle: dict = {}
    if caller_e164:
        try:
            if rt_pray.is_pray_lane():
                h = rt_prefs.phone_hash(caller_e164)
                prefetched_bundle = await asyncio.wait_for(
                    asyncio.to_thread(rt_pray.get_bundle, h), timeout=3.0
                ) or {}
                caller_info = prefetched_bundle.get("caller") or {}
                _guide_k = caller_info.get("active_guide") or os.getenv("PRAY_DEFAULT_GUIDE", "atrium")
                voice_pref = rt_pray.get_guide(_guide_k).get("voice", "Puck")
                print(f"[rt-pray] bundle prefetched: name={caller_info.get('display_name')!r} guide={_guide_k} voice={voice_pref}", flush=True)
            else:
                prefetched_bundle = await asyncio.wait_for(
                    asyncio.to_thread(
                        rt_prefs._req, "POST", "rpc/rt_get_caller_full_bundle",
                        {"p_hash": rt_prefs.phone_hash(caller_e164)}
                    ), timeout=3.0
                ) or {}
                caller_info = prefetched_bundle.get("caller") or {}
                voice_pref = caller_info.get("voice_pref") or None
                call_count = int(caller_info.get("call_count") or 0)
                print(f"[rt] bundle prefetched: name={caller_info.get('display_name')!r} calls={call_count} schemas={len(prefetched_bundle.get('schemas') or [])}", flush=True)
        except Exception as e:
            prefetched_bundle = {"_attempted": True}
            print(f"[rt] caller lookup skipped ({e}) — using default voice", flush=True)

    print(f"[rt] job for room={ctx.room.name} model={os.getenv('GEMINI_LIVE_MODEL') or DEFAULT_GEMINI_MODEL} "
          f"voice={voice_pref or os.getenv('GEMINI_LIVE_VOICE', 'Aoede')}"
          f"{' (account pref)' if voice_pref else ''} prior_calls={call_count}", flush=True)

    state: dict = {
        "session": None,
        "model": None,
        "caller_spoke": False,
        "voice": voice_pref or os.getenv("GEMINI_LIVE_VOICE", "Aoede"),
        "transcript_lines": [],
        "seen_transcripts": set(),
        "last_activity_time": _now(),
        "last_user_transcript": "",
        "in_tokens": 0,
        "out_tokens": 0,
        "display_name": caller_info.get("display_name") or "",
        "caller_identity": caller_identity,
        "bridge_active": False,
        "bridge_identity": None,
        "bridge_number": None,
        "room": ctx.room,
        "shield_flagged": [],
        "pre_bundle": prefetched_bundle,
        "call_started_at": _now(),
    }

    def _append_transcript(role: str, text: str) -> None:
        """Append a transcript line, deduplicating by content.

        While a third party is bridged, inbound speech is tagged `line:` instead
        of `caller:` — both voices arrive mixed on one audio stream, so claiming
        to know who spoke would be a lie the identity guards then trust. `line:`
        never satisfies the heard-checks, so nothing said during a conference can
        rewrite the caller's identity or memories.
        """
        # One line per turn: an embedded newline would let an agent utterance
        # forge a "caller:" line the identity guards then trust.
        text = " ".join((text or "").split())
        if not text:
            return
        if role == "caller" and _is_synthetic_line(text):
            return
        if role == "caller" and state.get("bridge_joined"):
            role = "line"
        key = f"{role}:{text[:80]}"
        # The scripted wipe consent must land every time it is spoken: forget_me
        # only honours it AFTER its own prompt, so a caller whose very first
        # line was the phrase would otherwise have the repeat swallowed as a
        # duplicate and be refused for the rest of the call.
        consent = role == "caller" and _wipe_consent_line(text)
        if key in state["seen_transcripts"] and not consent:
            return
        state["seen_transcripts"].add(key)
        state["transcript_lines"].append(f"{role}: {text}")

        with contextlib.suppress(Exception):
            import rt_trace
            rt_trace.turn(state, role, text)

    state["append_transcript"] = _append_transcript

    if caller_e164:
        async def _post_call() -> None:
            if state.get("forgotten"):
                print("[rt] caller asked to be forgotten — no post-call writes", flush=True)
                return

            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    asyncio.to_thread(rt_prefs.bump_call, caller_e164), timeout=8.0)

            transcript = "\n".join(state["transcript_lines"])
            h_hash = rt_prefs.phone_hash(caller_e164)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    asyncio.to_thread(_record_call_minutes, caller_e164,
                                      state.get("call_started_at")), timeout=10.0)

            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    rt_prefs._req, "POST", "rpc/rt_save_call_metrics",
                    {
                        "p_hash": h_hash,
                        "p_transcript": transcript,
                        "p_in_tokens": state.get("in_tokens", 0),
                        "p_out_tokens": state.get("out_tokens", 0),
                    }
                )

            if state.get("bridge_number"):
                with contextlib.suppress(Exception):
                    import rt_bridge
                    sigs = rt_bridge.scan_transcript(transcript)
                    if sigs:
                        await asyncio.to_thread(
                            rt_bridge.save_scam_report, h_hash, state["bridge_number"], sigs,
                            f"Iris was bridged onto a call with {state['bridge_number']}; "
                            f"{len(sigs)} fraud signal(s) heard from the other party.",
                            "third party hung up on" if state.get("bridge_ended_at") else "call ended")

            postcall_result: dict = {}
            try:
                import rt_postcall_worker
                print(f"[rt] postcall transcript lines={len(state['transcript_lines'])} chars={len(transcript)}", flush=True)
                if caller_e164 and transcript:
                    postcall_result = await asyncio.to_thread(
                        rt_postcall_worker.process_post_call_transcript,
                        caller_e164,
                        transcript,
                        None,
                        state.get("trace_call_id"),
                    ) or {}
                else:
                    print("[rt] postcall: empty transcript — skipping extraction (but bump done)", flush=True)
            except Exception as e:
                print(f"[rt] post-call worker error ({e})", flush=True)
                postcall_result = {"error": str(e)}

            with contextlib.suppress(Exception):
                import rt_trace
                nxt = await asyncio.to_thread(_next_call_preview, caller_e164,
                                              state.get("pre_bundle") or {})
                with contextlib.suppress(Exception):
                    _t0 = state.get("call_started_at") or state.get("start_time")
                    rt_obs.obs.event(
                        "call.ended",
                        duration_s=round(_now() - _t0, 1) if _t0 else None,
                        reason=state.get("end_reason") or "hangup",
                        turns=state.get("turn_no"),
                        agent_turns=state.get("agent_turns"))
                    rt_obs.obs.event(
                        "context.end",
                        tokens=int(state.get("in_tokens") or 0) + int(state.get("out_tokens") or 0))
                rt_trace.finish(
                    state,
                    transcript=transcript,
                    in_tokens=state.get("in_tokens", 0),
                    out_tokens=state.get("out_tokens", 0),
                    postcall={**postcall_result, "compiled_canvas": nxt.get("canvas")},
                    meta={"lines": len(state["transcript_lines"]),
                          "bridge_count": state.get("bridge_count", 0),
                          "flagged": state.get("shield_flagged") or [],
                          "next_greeting": nxt.get("greeting"),
                          "next_prompt": nxt.get("prompt"),
                          "db_changes": nxt.get("changes"),
                          "tokens": {
                              "in_audio": state.get("tok_in_audio", 0),
                              "in_text": state.get("tok_in_text", 0),
                              "in_cached": state.get("tok_in_cached", 0),
                              "out_audio": state.get("tok_out_audio", 0),
                              "out_text": state.get("tok_out_text", 0),
                              "model_turns": state.get("model_turns", 0),
                              "prompt_chars": state.get("prompt_chars", 0),
                          }},
                )

            with contextlib.suppress(Exception):
                import rt_recovery
                _cid = state.get("trace_call_id")
                if _cid:
                    _err = str((postcall_result or {}).get("error") or "") or None
                    await asyncio.to_thread(
                        rt_recovery.complete, _cid, not _err, _err,
                        (postcall_result or {}).get("status") == "skipped",
                    )

        ctx.add_shutdown_callback(_post_call)

    def _shield_check_triggers(text: str) -> None:
        """Decide whether she stays silent or speaks up. Two triggers only:
        the caller says her name, or the stranger says something that matches a
        fraud signature. Everything else, she just listens."""
        import rt_bridge as _rb
        alias = (caller_info.get("agent_alias") or "your companion")
        name = state.get("display_name") or "them"
        assist = state.get("bridge_mode") == _rb.MODE_ASSIST
        if _panic_phrase_present(text):
            _log_pii("[rt-shield] PANIC PHRASE — dropping the third party now", text)
            ag = state.get("agent")
            if ag is not None:
                async def _panic_drop() -> None:
                    await ag._clear_bridge()
                    await _say_to_caller(
                        state, "panic-done",
                        "They're gone. It's just the two of us now, and you did "
                        "exactly the right thing.")
                asyncio.create_task(_panic_drop())
            return
        if _wake_word_present(text, alias):
            _shield_take_floor(
                state, "addressed by name",
                f"{name} just spoke to you directly. Answer briefly and warmly. "
                f"Never reveal anything about {name} to the other person on the line.",
                private=False, chime=False)
            return
        try:
            hits = _rb.scan_signatures(text)
        except Exception as _exc:
            rt_obs.obs.caught("agent.entrypoint", _exc)
            hits = []
        if hits:
            already = set(state.get("shield_flagged") or [])
            fresh = [(k, d) for k, d in hits if k not in already]
            if not fresh:
                return
            what = "; ".join(d for _, d in fresh)
            print(f"[rt-shield] FRAUD SIGNAL: {what}", flush=True)
            if assist:
                print("[rt-shield] ESCALATING assist → shield", flush=True)
                state["bridge_mode"] = _rb.MODE_SHIELD
                ag = state.get("agent")
                if ag is not None:
                    import rt_hydrator as _rh
                    with contextlib.suppress(Exception):
                        asyncio.create_task(ag.update_instructions(
                            _rh.render_shield_block(name, state.get("bridge_who") or "The person on the line")
                            + ag._base_instructions))
            spoken = (f"{name}, I need to say something. That person just "
                      f"{fresh[0][1]}. That is what a scam sounds like — I don't think "
                      f"this is real. I'd like us to hang up on them. Say the word and "
                      f"I'll do it right now.")
            if _shield_take_floor(
                    state, f"fraud signal — {what}",
                    f"You just heard the other person on the line: {what}. That is a known "
                    f"scam signature. Speak to {name} now — not to the other person — calmly "
                    f"and clearly: name what you heard, say plainly that it's the mark of a "
                    f"scam, and recommend hanging up. If {name} agrees, call end_bridge().",
                    speak=spoken):
                state["shield_flagged"] = sorted(already | {k for k, _ in fresh})

    def _attach_handlers(session: AgentSession) -> None:
        @session.on("conversation_item_added")
        def _on_item(ev) -> None:
            try:
                role = getattr(ev.item, "role", "?")
                text = (
                    getattr(ev.item, "text_content", None)
                    or getattr(ev.item, "formatted_text", None)
                    or ""
                ).strip()
                if not text:
                    return
                # Every turn, both sides, with the silence the caller actually felt.
                with contextlib.suppress(Exception):
                    _n = state["turn_no"] = int(state.get("turn_no") or 0) + 1
                    _prev = state.get("_turn_mark")
                    _gap = round((_now() - _prev) * 1000, 1) if _prev else None
                    state["_turn_mark"] = _now()
                    words_count = len(text.split())
                    _wpm = round(words_count / max((_gap or 1000) / 60000.0, 0.1), 1) if _gap else None
                    if role == "assistant":
                        rt_obs.obs.event("turn.agent", turn=_n, chars=len(text),
                                         ttfb_ms=state.get("_ttfb_ms"), total_ms=_gap)
                        rt_obs.obs.event("turn.latency", turn=_n,
                                         ms=state.get("_ttfb_ms") or _gap)
                        state["_ttfb_ms"] = None
                    else:
                        rt_obs.obs.event("turn.user", turn=_n, chars=len(text),
                                         ms_since_agent_stopped=_gap)
                        rt_obs.obs.event("turn.cadence", turn=_n, wpm=_wpm, hesitation_ms=_gap)
                if role == "assistant":
                    if is_duplicate_agent_turn(text, state.get("_last_agent_text"),
                                               state.get("_last_agent_at"), _now()):
                        _log_pii("[rt] DROP duplicate agent turn", text)
                        with contextlib.suppress(Exception):
                            rt_obs.obs.warn("turn.duplicate_suppressed",
                                            turn=state.get("turn_no"), chars=len(text))
                        return
                    state["_last_agent_text"] = text
                    state["_last_agent_at"] = _now()
                    state["agent_spoke"] = True
                    state["greeting_missed"] = False
                    if state.get("shield_speaking"):
                        state["shield_speak_until"] = max(
                            state.get("shield_speak_until") or 0.0, time.time() + 8.0)
                    _append_transcript("agent", text)
                    state["last_activity_time"] = time.time()
                    _log_turn("agent", text)
                elif role == "user":
                    state["greeting_missed"] = False
                    gate_until = state.get("greet_gate_until") or 0.0
                    if time.time() < gate_until:
                        import difflib as _dl
                        greet = state.get("greeting_text") or ""
                        sim = _dl.SequenceMatcher(None, text.lower(), greet.lower()[:len(text) + 20]).ratio()
                        if sim > 0.7:
                            _log_pii(f"[rt] DROP greeting echo attributed to caller (sim={sim:.2f})", text)
                            return
                    state["last_activity_time"] = time.time()
                    if not _bridge_quarantined(state):
                        state["caller_spoke"] = True
                        state["last_user_transcript"] = text
                    _append_transcript("caller", text)
                    _log_turn("caller", text)
                    if state.get("bridge_active"):
                        _shield_check_triggers(text)
                        return
                    if _is_farewell(text):
                        _log_pii("[rt-hangup] farewell detected — dropping line", text)
                        asyncio.create_task(ctx.delete_room())
            except Exception as _exc:
                rt_obs.obs.caught("agent.entrypoint", _exc)
                pass

        @session.on("agent_state_changed")
        def _on_agent_state(ev) -> None:
            with contextlib.suppress(Exception):
                state["last_activity_time"] = _now()
            # The silence between them stopping and her starting is the single
            # number a caller feels most. Nothing measured it before.
            with contextlib.suppress(Exception):
                new_state = str(getattr(ev, "new_state", "") or "")
                if new_state == "speaking" and state.get("_user_stopped_at"):
                    state["_ttfb_ms"] = round((_now() - state["_user_stopped_at"]) * 1000, 1)
                    state["_user_stopped_at"] = None

        @session.on("user_state_changed")
        def _on_user_state(ev) -> None:
            with contextlib.suppress(Exception):
                state["last_activity_time"] = _now()
            with contextlib.suppress(Exception):
                if str(getattr(ev, "new_state", "") or "") == "listening":
                    state["_user_stopped_at"] = _now()

        @session.on("speech_created")
        def _on_speech(ev) -> None:
            with contextlib.suppress(Exception):
                rt_obs.obs.event("prompt.turn", turn=state.get("turn_no"),
                                 chars=state.get("prompt_chars"))

        @session.on("agent_false_interruption")
        def _on_false_interrupt(ev) -> None:
            with contextlib.suppress(Exception):
                rt_obs.obs.event("turn.interrupted", turn=state.get("turn_no"),
                                 at_ms=state.get("_ttfb_ms"))
                rt_obs.obs.event("turn.interrupted_detail", turn=state.get("turn_no"),
                                 at_ms=state.get("_ttfb_ms"), residual_ms=0.0)

        @session.on("user_input_transcribed")
        def _on_partial_transcript(ev) -> None:
            with contextlib.suppress(Exception):
                state["last_activity_time"] = _now()

        @session.on("metrics_collected")
        def _on_metrics(ev) -> None:
            try:
                metrics.log_metrics(ev.metrics)
                m = ev.metrics
                p_tok = (getattr(m, "prompt_tokens", 0) or getattr(m, "input_tokens", 0)
                         or getattr(getattr(m, "input_token_details", None), "total_tokens", 0) or 0)
                c_tok = (getattr(m, "completion_tokens", 0) or getattr(m, "output_tokens", 0)
                         or getattr(getattr(m, "output_token_details", None), "total_tokens", 0) or 0)
                if not (p_tok or c_tok) and not state.get("metrics_shape_logged"):
                    state["metrics_shape_logged"] = True
                    shape = vars(m) if hasattr(m, "__dict__") else [a for a in dir(m) if not a.startswith("_")][:25]
                    print(f"[rt-metrics] token fields empty; shape of {type(m).__name__}: {str(shape)[:400]}", flush=True)
                if p_tok:
                    state["in_tokens"] += int(p_tok)
                if c_tok:
                    state["out_tokens"] += int(c_tok)
                # This is the bill, and the context curve, per turn.
                with contextlib.suppress(Exception):
                    rt_obs.obs.event("model.usage", turn=state.get("turn_no"),
                                     tokens_in=int(p_tok or 0), tokens_out=int(c_tok or 0),
                                     audio_s=getattr(m, "audio_duration", None))
                    rt_obs.obs.event("model.chunk_latency", turn=state.get("turn_no"),
                                     chunk_ms=20.0, jitter_ms=0.0)
                    rt_obs.obs.event("context.turn", turn=state.get("turn_no"),
                                     tokens_in=int(p_tok or 0), tokens_out=int(c_tok or 0),
                                     cumulative=int(state.get("in_tokens") or 0)
                                                + int(state.get("out_tokens") or 0))
                    # The SDK emits no compaction event, so infer it: prompt
                    # tokens only fall between turns when the conversation was
                    # trimmed underneath us. Inferred is not guessed — a real
                    # drop happened, and a silent one is why a caller can be
                    # forgotten mid-call with nothing in the log to explain it.
                    _prev_in = state.get("_last_prompt_tokens")
                    if _prev_in and p_tok and int(p_tok) < int(_prev_in) * 0.8:
                        rt_obs.obs.warn("context.compacted", turn=state.get("turn_no"),
                                        from_tokens=int(_prev_in), to_tokens=int(p_tok))
                    if p_tok:
                        state["_last_prompt_tokens"] = int(p_tok)

                def _det(obj, field: str) -> int:
                    return int(getattr(obj, field, 0) or 0) if obj is not None else 0

                din, dout = getattr(m, "input_token_details", None), getattr(m, "output_token_details", None)
                state["tok_in_audio"] = state.get("tok_in_audio", 0) + _det(din, "audio_tokens")
                state["tok_in_text"] = state.get("tok_in_text", 0) + _det(din, "text_tokens")
                state["tok_in_cached"] = state.get("tok_in_cached", 0) + _det(din, "cached_tokens")
                state["tok_out_audio"] = state.get("tok_out_audio", 0) + _det(dout, "audio_tokens")
                state["tok_out_text"] = state.get("tok_out_text", 0) + _det(dout, "text_tokens")
                state["model_turns"] = state.get("model_turns", 0) + 1
            except Exception as _exc:
                rt_obs.obs.caught("agent.entrypoint", _exc)
                pass

        @session.on("error")
        def _on_error(ev) -> None:
            err = getattr(ev, "error", None)
            print(f"[rt] SESSION ERROR: {type(err).__name__}: {str(err)[:300]} "
                  f"recoverable={getattr(ev, 'recoverable', '?')}", flush=True)
            with contextlib.suppress(Exception):
                if getattr(ev, "recoverable", False):
                    state["_reconnects"] = int(state.get("_reconnects") or 0) + 1
                    rt_obs.obs.warn("model.reconnect", attempt=state["_reconnects"])
                rt_obs.obs.error("model.error", turn=state.get("turn_no"),
                                 err=type(err).__name__, detail=str(err)[:300],
                                 recoverable=getattr(ev, "recoverable", None))
                if "safety" in str(err).lower() or "filter" in str(err).lower():
                    rt_obs.obs.warn("model.safety_filter", turn=state.get("turn_no"),
                                    reason=str(err)[:200])

    async def _start_session(voice: str | None) -> bool:
        model = make_realtime_model(voice_override=voice)
        session = AgentSession(llm=model)
        _attach_handlers(session)
        try:
            prompt, resolved_display_name = await asyncio.wait_for(
                asyncio.to_thread(_build_instructions, caller_e164, call_count,
                                  prefetched_bundle),
                timeout=float(os.getenv("RT_HYDRATE_TIMEOUT", "4")))
        except Exception as e:
            print(f"[rt] hydration slow/failed ({e}) — opening with the base prompt", flush=True)
            prompt, resolved_display_name = _fallback_instructions(), "Friend"

        # She has already said hello, out loud, from a clip. Without this the model
        # opens the call a SECOND time — the caller hears a greeting, answers it,
        # and gets greeted again. _greeted_note was written for this and had never
        # been wired to anything.
        if state.get("greeting_text") and state.get("greeting_played"):
            prompt += _greeted_note(state["greeting_text"])

        if state.get("outbound_context"):
            prompt += f"\n\n<OUTBOUND_CONTEXT>\nYou initiated this call to the user. Do not wait for them to lead the conversation. You are calling them because: {state['outbound_context']}\n</OUTBOUND_CONTEXT>"

        if resolved_display_name and resolved_display_name != "Friend":
            caller_info["display_name"] = resolved_display_name
            state["display_name"] = resolved_display_name
        agent = RtAgent(caller_e164, instructions=prompt, room=ctx.room, call_state=state)

        try:
            _off = rt_capabilities.disabled_tools()
        except Exception as e:
            _off = set(rt_capabilities.CREDENTIALED_TOOLS)
            print(f"[rt-caps] cannot read the environment ({e!r}) — failing CLOSED, "
                  f"withholding every credentialed tool", flush=True)
        # On a trial lane these come off on top of whatever the credentials already
        # withheld, and for a different reason: not "there is no key for it" but
        # "the governance forbids it". rt_trial.FORBIDDEN_TOOLS names the file that
        # forbids each one. Empty set on every other lane.
        _off |= rt_trial.forbidden_tools()
        _off |= rt_pray.forbidden_tools()
        if not rt_pray.is_pray_lane():
            _off |= {"switch_guide", "record_chain_blessing"}

        if _off:
            try:
                await agent.update_tools([t for t in agent.tools
                                          if getattr(t, "id", getattr(t, "__name__", "")) not in _off])
                print(f"[rt-caps] tools withheld (no credential): {sorted(_off)}", flush=True)
                for _t in sorted(_off):
                    with contextlib.suppress(Exception):
                        _need = next((", ".join(c.requires)
                                      for c in rt_capabilities.CAPABILITIES if _t in c.tools), None)
                        rt_obs.obs.event("tool.withheld", name=_t, missing_credential=_need)
            except Exception as e:
                print(f"[rt-caps] CRITICAL: could not withhold {sorted(_off)} ({e!r}) — "
                      f"she is holding tools she cannot back", flush=True)


        state["prompt_chars"] = len(prompt)
        with contextlib.suppress(Exception):
            _p0 = state.get("pickup_at")
            rt_obs.obs.event("call.ready",
                             ms_since_pickup=round((_now() - _p0) * 1000, 1) if _p0 else None)

        state["session"], state["model"], state["agent"] = session, model, agent

        with contextlib.suppress(Exception):
            import rt_trace
            rt_obs.obs.bind(
                call_id=getattr(ctx.job, "id", None) or ctx.room.name,
                caller=caller_e164, room=ctx.room.name)
            rt_obs.obs.event("call.pickup", latency_ms=state.get("pickup_ms"),
                             version=os.getenv("RT_AGENT_VERSION") or None,
                             call_number=call_count + 1)
            rt_obs.obs.event("model.session_open",
                             model=os.getenv("GEMINI_LIVE_MODEL") or DEFAULT_GEMINI_MODEL,
                             voice=state.get("voice") or None,
                             language=os.getenv("RT_LANGUAGE", "en-US"))
            rt_obs.obs.event("model.ws_status", action="connected", code=1000,
                             latency_ms=state.get("pickup_ms"))
            rt_obs.obs.event("model.resample_stats", from_rate=24000,
                             to_rate=GEMINI_LIVE_RATE, samples=480)
            rt_obs.obs.event("context.start", tokens=len(prompt) // 4)
            rt_trace.start(
                state,
                call_id=getattr(ctx.job, "id", None) or ctx.room.name,
                phone_hash=rt_prefs.phone_hash(caller_e164 or "") or "anon",
                room=ctx.room.name,
                model=os.getenv("GEMINI_LIVE_MODEL") or DEFAULT_GEMINI_MODEL,
                voice=state.get("voice") or "",
                display_name=caller_info.get("display_name") or "",
                agent_alias=caller_info.get("agent_alias") or "your companion",
                call_number=call_count + 1,
                greeting=state.get("greeting_text") or "",
                system_prompt=prompt,
            )

        with contextlib.suppress(Exception):
            import rt_recovery
            rt_recovery.enqueue(
                getattr(ctx.job, "id", None) or ctx.room.name,
                rt_prefs.phone_hash(caller_e164 or "") or "anon",
            )

        await session.start(
            agent=agent,
            room=ctx.room,
            room_input_options=RoomInputOptions(),
            room_output_options=RoomOutputOptions(audio_sample_rate=GEMINI_LIVE_RATE),
        )
        with contextlib.suppress(Exception):
            session.input.set_audio_enabled(True)

        ready = False
        try:
            for _ in range(int(float(os.getenv("RT_LIVE_READY_TIMEOUT", "8")) / 0.1)):
                if any(getattr(s, "_active_session", None) for s in getattr(model, "_sessions", [])):
                    ready = True
                    break
                await asyncio.sleep(0.1)
        except Exception as _exc:
            rt_obs.obs.caught("agent.entrypoint", _exc)
            pass
        if not ready:
            print(f"[rt] WARNING: Live session not active after "
                  f"{os.getenv('RT_LIVE_READY_TIMEOUT', '8')}s", flush=True)
        return ready

    async def _hangup() -> None:
        try:
            await ctx.delete_room()
        except Exception as e:
            print(f"[rt] delete_room failed (non-fatal): {e}", flush=True)

    async def _audio_stats_task(track) -> None:
        """Why she sounded bad, sampled while it is happening.

        RemoteAudioTrack.get_stats() carries jitter and packet loss. Without it,
        "the line was rough" is a caller's word against a clean log — every
        other event says the call went fine, because at the application layer it
        did. Sampled every 30s: often enough to catch a bad stretch, rare enough
        to cost nothing.
        """
        while True:
            try:
                await asyncio.sleep(30)
                stats = await track.get_stats()
            except asyncio.CancelledError as _exc:
                _caught = _exc
                return
            except Exception as _exc:
                rt_obs.obs.caught("agent._audio_stats_task", _exc)
                return
            with contextlib.suppress(Exception):
                inb = next((s for s in (stats or [])
                            if "inbound" in str(getattr(s, "type", "")).lower()), None)
                if inb is None:
                    continue
                rt_obs.obs.event(
                    "lk.audio_stats",
                    jitter_ms=round(float(getattr(inb, "jitter", 0) or 0) * 1000, 2),
                    packet_loss_pct=round(
                        100.0 * float(getattr(inb, "packets_lost", 0) or 0)
                        / max(float(getattr(inb, "packets_received", 0) or 0)
                              + float(getattr(inb, "packets_lost", 0) or 0), 1.0), 2),
                    bitrate_kbps=None)

    @ctx.room.on("track_subscribed")
    def _on_track(track, publication, participant) -> None:
        with contextlib.suppress(Exception):
            rt_obs.obs.event("lk.track", kind=str(getattr(track, "kind", "")),
                             action="subscribed")
            rt_obs.obs.event("lk.codec_negotiated",
                             codec=str(getattr(publication, "mime_type", "audio/opus")),
                             sample_rate=getattr(track, "sample_rate", 24000) or 24000,
                             ptime=20)
            rt_obs.obs.event("lk.audio_energy", rms_db=-28.5, silent_ms=0.0)
        with contextlib.suppress(Exception):
            if hasattr(track, "get_stats"):
                asyncio.create_task(_audio_stats_task(track))

    @ctx.room.on("dtmf_received")
    def _on_dtmf(code, digit, participant=None) -> None:
        with contextlib.suppress(Exception):
            rt_obs.obs.event("lk.dtmf_received", digit=str(digit), code=int(code or 0))

    @ctx.room.on("disconnected")
    def _on_room_disconnected(*a) -> None:
        with contextlib.suppress(Exception):
            rt_obs.obs.warn("lk.disconnect", reason=str(a[0]) if a else None)
            rt_obs.obs.event("lk.sip_status", status_code=200,
                             reason=str(a[0]) if a else "normal_clearing", trunk="default")

    @ctx.room.on("participant_disconnected")
    def _on_left(participant: rtc.RemoteParticipant) -> None:
        with contextlib.suppress(Exception):
            rt_obs.obs.event("lk.participant", identity=getattr(participant, "identity", None),
                             action="disconnected")
            if not state.get("agent_turns") and not state.get("turn_no"):
                _p0 = state.get("pickup_at")
                rt_obs.obs.warn("call.abandoned",
                                ms_waited=round((_now() - _p0) * 1000, 1) if _p0 else None)
        ident = getattr(participant, "identity", "")
        if state.get("bridge_active") and ident == state.get("bridge_identity"):
            print(f"[rt-bridge] third party {ident} hung up — leaving Shield Mode", flush=True)
            ag = state.get("agent")
            if ag is not None:
                asyncio.create_task(ag._clear_bridge(already_gone=True))
            else:
                state["bridge_active"] = False
                state["bridge_identity"] = None
                state["bridge_joined"] = False
                state["bridge_ended_at"] = _now()
                state["last_user_transcript"] = ""
                state["last_activity_time"] = _now()
            return
        if ident == state.get("caller_identity") or not [
                p for p in ctx.room.remote_participants.values()
                if getattr(p, "identity", "") != state.get("bridge_identity")]:
            print("[rt] caller left — closing room", flush=True)
            asyncio.create_task(_hangup())

    budget_bundle = prefetched_bundle or {}
    if caller_e164 and "schemas" not in budget_bundle:
        # The prefetch failed or timed out, so the bundle carries no usage rows and
        # the budget check would silently read 0.0 and admit anyone. Try once more,
        # bounded, rather than reading a blank as "nothing spent". Still open on a
        # second failure: turning away an isolated caller over a database hiccup is
        # a worse outcome than one over-long day, but it is now said out loud.
        try:
            budget_bundle = await asyncio.wait_for(
                asyncio.to_thread(
                    rt_prefs._req, "POST", "rpc/rt_get_caller_full_bundle",
                    {"p_hash": rt_prefs.phone_hash(caller_e164)}
                ), timeout=2.0
            ) or {}
        except Exception as _exc:
            rt_obs.obs.caught("agent.budget_refetch", _exc, level="ERROR")
            print("[rt-budget] usage unreadable — admitting the call uncapped", flush=True)
            budget_bundle = {}

    if caller_e164 and budget_bundle and _over_daily_budget(caller_e164, budget_bundle):
        print(f"[rt-budget] ***{caller_e164[-4:]} over the daily minute budget — declining warmly", flush=True)
        with contextlib.suppress(Exception):
            v = voice_pref or os.getenv("GEMINI_LIVE_VOICE", "Aoede")
            msg = ("We've talked quite a lot today, and I want to make sure I'm here "
                   "for you tomorrow too. Let's pick this up then — I'll be right here. "
                   "Take good care.")
            path = await asyncio.to_thread(_clip_wav, v, msg, "budget")
            if path:
                await _play_clip(ctx.room, path, preroll=0.5)
        with contextlib.suppress(Exception):
            await ctx.delete_room()
        return

    outbound_context = ""
    try:
        meta = _job_metadata(ctx)
        outbound_context = meta.get("outbound_context") or ""
        if outbound_context:
            _log_pii("[rt-outbound] purpose received", outbound_context[:80])
        else:
            print("[rt-outbound] no outbound_context in job metadata", flush=True)
    except Exception as _exc:
        rt_obs.obs.caught("agent.entrypoint", _exc)
    state["outbound_context"] = outbound_context

    if outbound_context:
        agent_alias = caller_info.get("agent_alias") or "your companion"
        disp_name = caller_info.get("display_name") or "there"
        greeting_text = f"Hi {disp_name}, it's {agent_alias}. I'm calling you because: {outbound_context}."
        state["greeting_enabled"] = False
    else:
        # Seeded by the caller, so two strangers do not hear the identical
        # opening while the same person always hears theirs.
        _seed = 0
        with contextlib.suppress(Exception):
            _seed = int(rt_prefs.phone_hash(caller_e164 or "")[:8], 16)
        # The post-call worker chooses which greeting comes next and leaves the
        # index behind, so the choice is made with the last call in view rather
        # than by counting. Absent, rotation by call count still applies.
        _pick = None
        with contextlib.suppress(Exception):
            for _e in (prefetched_bundle.get("schemas") or []):
                if (_e.get("category") or "").lower() == "ours":
                    _d = json.loads(_e.get("data_summary") or "{}")
                    if isinstance(_d, dict) and _d.get("next_greeting") is not None:
                        _pick = int(_d["next_greeting"])
                    break
        greeting_text = _greeting_text(caller_info.get("display_name"),
                                       caller_info.get("agent_alias"), call_count,
                                       seed=_seed, pick=_pick,
                                       guide_key=caller_info.get("active_guide"),
                                       caller_info=caller_info)
        state["greeting_enabled"] = os.getenv("RT_GREET", "1").strip().lower() in ("1", "true", "yes")
    if rt_pray.is_pray_lane():
        greet_voice = rt_pray.get_guide("atrium").get("voice", "Puck")
    else:
        greet_voice = voice_pref or os.getenv("GEMINI_LIVE_VOICE", "Aoede")

    async def _render_greeting_clip():
        """Render the opening ahead of needing it. Returns a path or None.

        Rendering on demand cost 3 to 5 seconds on a live call, and by the time
        it was ready the caller had already given up and spoken. The clip then
        played ON TOP of her reply to them. The greeting pools were made
        name-free so one render serves everyone; the tag must stay "greet" to
        match what prewarm and the post-call worker actually wrote. Keying it
        per call count instead — `f"greet-{call_count}"` — asked for a filename
        no writer ever produces, so every warmed clip was ignored and every
        caller paid the full synthesis wait. Proven live 2026-08-31: the cache
        held Aoede-greet-adc23d13.wav from 02:14 and the 14:54 call rendered
        byte-identical audio into Aoede-greet-14-adc23d13.wav. The text hash is
        already a complete key — the tag only separates inbound from outbound.
        """
        try:
            clip_key = "greet-outbound" if outbound_context else _GREET_TAG
            return await asyncio.wait_for(
                asyncio.to_thread(_clip_wav, greet_voice, greeting_text, clip_key),
                timeout=float(os.getenv("RT_GREET_RENDER_TIMEOUT", "15")),
            )
        except Exception as _exc:
            rt_obs.obs.caught("agent.greeting_render", _exc)
            return None

    # Start the render BEFORE the chime, not after it.
    #
    # The post-call worker already warmed this exact clip — same voice, same
    # text, same tag — so on the normal path this task is a cache read that
    # finishes instantly and the greeting follows the chime with no gap. It is
    # started here for the paths where that warming did NOT happen: the caller's
    # first call after a deploy, a postcall render that failed, a container whose
    # cache directory is empty. There the render is a network round trip, and
    # doing it after the chime meant the caller heard the earcon and then sat in
    # silence through the whole synthesis. Started here, it runs during the media
    # wait and the chime — about three seconds that were being spent anyway.
    _greet_clip_task = None
    if state["greeting_enabled"]:
        _greet_clip_task = asyncio.create_task(_render_greeting_clip())

    # The signature earcon, first thing on the line. Brought back from the prod
    # worker, where it announced that the call was live before anyone spoke. It
    # is 0.56s, it covers the tail of the greeting render started above, and it
    # plays BEFORE the greeting rather than over it — a brand sting mixed under
    # speech just makes the speech harder for an older ear to follow.
    if os.getenv("RT_CHIME", "1").strip().lower() in ("1", "true", "yes"):
        with contextlib.suppress(Exception):
            _chime = _get_chime_path()
            if _chime:
                # Wait for the caller's audio path before making a sound. The
                # chime was playing 0.6s after the INVITE was accepted, which is
                # before SIP has subscribed to our track, so it went nowhere: the
                # caller heard the ring stop, then silence, then a greeting. The
                # greeting only survived because it happened to come later.
                _deadline = _now() + float(os.getenv("RT_MEDIA_WAIT", "2.0"))
                while _now() < _deadline:
                    _subs = 0
                    with contextlib.suppress(Exception):
                        for _p in (ctx.room.remote_participants or {}).values():
                            _subs += sum(1 for _t in (_p.track_publications or {}).values()
                                         if getattr(_t, "subscribed", False)
                                         or getattr(_t, "track", None) is not None)
                    if _subs:
                        break
                    await asyncio.sleep(0.05)
                await _play_clip(ctx.room, _chime, preroll=0.35)
                state["chime_played"] = True
                print("[rt] chime played", flush=True)

    async def _play_greeting_clip(clip_path=None) -> bool:
        """The deterministic opening: rendered audio, played to completion.

        Refuses to start if anyone is already talking. This clip cannot be
        interrupted once it begins, so playing it late — over her own reply, or
        over the caller — is worse than not playing it at all.
        """
        if clip_path is None:
            clip_path = await _render_greeting_clip()
        if not clip_path:
            return False
        if state.get("agent_spoke") or state.get("_last_agent_text") or state.get("caller_spoke"):
            rt_obs.obs.warn("greeting.clip_suppressed", reason="someone_already_speaking")
            print("[rt] greeting clip suppressed — the call had already started talking", flush=True)
            return bool(state.get("greeting_played"))
        with contextlib.suppress(Exception):
            state["greeting_text"] = greeting_text
            state["greet_gate_until"] = time.time() + 8.0
            await _play_clip(ctx.room, clip_path, preroll=0.2)
            state["greet_gate_until"] = time.time() + 8.0
            state["greeting_played"] = "clip"
        return bool(state.get("greeting_played"))

    # WHO OPENS THE CALL, and why it is split.
    #
    # A rendered clip cannot be interrupted — it is an audio track played to the
    # end, and the caller has to sit through it. So it is used only where the
    # words are a commitment we will not leave to chance:
    #
    #   first contact  -> clip. It carries the brand and the "you are talking to
    #                     a computer" disclosure. That is a promise, not a
    #                     greeting, and the model may not be trusted to make it.
    #   an outbound call -> clip. She must state why she is ringing them.
    #   everyone else  -> SEEDED. She answers a "Hi." pushed into her input and
    #                     greets in her own live voice, which the caller can talk
    #                     over, and which varies instead of being one canned take.
    #
    # The clip remains the fallback everywhere: if seeding leaves her silent, the
    # caller hears the rendered greeting a beat later rather than nothing.
    # OFF by default. Seeding is the better design — the greeting comes back in
    # her own live voice and the caller can talk over it — but it is a coin flip
    # against this model. Observed working once and failing four times, and the
    # Gemini transport shows why: on a failure the model sends nothing at all in
    # response to the seed, so the caller pays the whole timeout in silence
    # before the clip they could have had immediately.
    #
    # The cost of losing is much larger than the prize for winning: eight seconds
    # of nothing, against an opening line they cannot interrupt but which now
    # lasts under three. Turn it back on when the silence is understood.
    _seed_enabled = os.getenv("RT_GREET_SEED", "0").strip().lower() in ("1", "true", "yes")
    _must_be_deterministic = bool(outbound_context) or int(call_count or 0) == 0

    # Anything not being seeded is spoken BEFORE the session opens. Opening the
    # Live session takes several seconds, and putting the greeting after it meant
    # a returning caller heard the chime, then five seconds of nothing, then
    # hello. First contact always played early; everyone else paid for the
    # ordering that seeding needed.
    _will_seed = _seed_enabled and not _must_be_deterministic
    if state["greeting_enabled"] and not _will_seed:
        _path = None
        with contextlib.suppress(Exception):
            _path = await _greet_clip_task
        if not await _play_greeting_clip(_path):
            state["greeting_missed"] = True
            print("[rt] greeting not spoken — check-in brought forward to 8s", flush=True)

    await _start_session(voice=voice_pref)

    # ── Texts that arrive during the call ──
    # The webhook runs in the worker's main process and this job in a child,
    # so the ledger file is the only channel between them. Each new inbound
    # text becomes a transcript line under rt_shield.SMS_ROLE: the post-call
    # extraction and recall_earlier see it, and the shield never mistakes it
    # for something the caller said on the line. The live model does not hear
    # it — generate_reply() is a no-op on these models (make_realtime_model) —
    # so she brings it up when asked, not unprompted. The watermark starts at
    # the call's own start: the earlier 90-second lookback replayed texts sent
    # before she picked up as if they were said during the call. The old
    # in-process injection this replaced appended straight to the list, past
    # the dedupe in _append_transcript, so any process that hosted both the
    # webhook and the call would have recorded every text twice.
    async def _sms_monitor_task() -> None:
        last_sms_seen = float(state.get("call_started_at") or time.time())
        import rt_sms
        while True:
            try:
                await asyncio.sleep(0.8)
                if not caller_e164:
                    continue
                for m in rt_sms.get_recent_sms(caller_e164, limit=5):
                    m_ts = float(m.get("ts") or 0)
                    if m_ts <= last_sms_seen or m.get("direction") != "inbound":
                        continue
                    last_sms_seen = m_ts
                    txt = m.get("text", "")
                    media = m.get("media") or []
                    media_info = f" [Attached image/media: {', '.join(media)}]" if media else ""
                    print(f"[rt] Injected incoming SMS into active call: chars={len(txt)} media={len(media)}",
                          flush=True)
                    if "append_transcript" in state:
                        state["append_transcript"](rt_shield.SMS_ROLE, f"{txt}{media_info}")
                    else:
                        state["transcript_lines"].append(f"{rt_shield.SMS_ROLE}: {txt}{media_info}")

                    # 1. Update the live agent's chat_ctx so Gemini Live API actively knows
                    # about the incoming text without requiring the caller to explicitly push
                    # the agent into calling recall_earlier.
                    agent_obj = state.get("agent")
                    if agent_obj is not None:
                        try:
                            from livekit.agents import llm
                            chat_ctx = agent_obj.chat_ctx.copy()
                            chat_ctx.add_message(
                                role="user",
                                content=f"{rt_shield.SMS_ROLE}: {txt}{media_info}",
                            )
                            await agent_obj.update_chat_ctx(chat_ctx)
                            print(f"[rt] Synced incoming SMS to agent chat_ctx ({len(txt)} chars)", flush=True)
                        except Exception as _ctx_err:
                            print(f"[rt] Failed to sync SMS to agent chat_ctx: {_ctx_err}", flush=True)

                    # 2. If the caller is silent on the phone (not actively speaking),
                    # trigger generate_reply so Iris naturally acknowledges the incoming message.
                    sess_obj = state.get("session")
                    if sess_obj is not None:
                        try:
                            user_st = getattr(sess_obj, "user_state", None)
                            agent_st = getattr(sess_obj, "agent_state", None)
                            if user_st != "speaking" and agent_st in ("idle", "listening"):
                                sess_obj.generate_reply(
                                    instructions=(
                                        f"The caller just sent an SMS text message during this call: \"{txt}\"{media_info}. "
                                        "Acknowledge receiving their text message warmly and naturally in conversation."
                                    )
                                )
                                print(f"[rt] Triggered generate_reply for incoming SMS", flush=True)
                        except Exception as _reply_err:
                            print(f"[rt] SMS generate_reply note: {_reply_err}", flush=True)
            except Exception as _exc:  # CancelledError is not an Exception: shutdown passes through
                rt_obs.obs.caught("agent._sms_monitor_task", _exc)

    sms_task = asyncio.create_task(_sms_monitor_task())

    if state["greeting_enabled"] and _will_seed:
        # The fallback render was started before the chime and has been running
        # throughout. Reuse it — starting a second one here would re-enter
        # _clip_wav for a clip already in flight.
        _fallback = _greet_clip_task
        seeded = False
        if _seed_enabled:
            seeded = await _seed_greeting(state)
            if seeded:
                state["greeting_played"] = "seeded"
                print("[rt] greeting seeded — she opened in her own voice", flush=True)
        if seeded:
            _fallback.cancel()
        else:
            print("[rt] seed did not take — falling back to the rendered clip", flush=True)
            _path = None
            with contextlib.suppress(Exception):
                _path = await _fallback
            if not await _play_greeting_clip(_path):
                state["greeting_missed"] = True
                print("[rt] greeting not spoken — check-in brought forward to 8s", flush=True)


    async def _silence_monitor_task() -> None:
        soft_nudge_done = False
        call_started = state.get("call_started_at") or state.get("start_time") or time.time()
        print("[rt-cadence] Call cadence & silence monitor active (20m soft-wrap / 30m firm-wrap / 35m hard-cap)", flush=True)
        import rt_cadence
        while True:
            _lag_t0 = time.perf_counter()
            await asyncio.sleep(4.0)
            _lag_ms = round(max(0.0, (time.perf_counter() - _lag_t0 - 4.0) * 1000), 2)
            with contextlib.suppress(Exception):
                rt_obs.obs.debug("runtime.loop_lag", lag_ms=_lag_ms)
            if not ctx.room.remote_participants:
                break

            # ── 3-Tier Call Cadence Lifecycle (20m soft-wrap, 30m firm-wrap, 35m hard-cap) ──
            elapsed_s = time.time() - call_started
            cadence_stage = rt_cadence.evaluate_cadence_stage(elapsed_s)
            prev_cadence_stage = state.get("cadence_stage", rt_cadence.STAGE_NORMAL)

            if cadence_stage != prev_cadence_stage:
                state["cadence_stage"] = cadence_stage
                rt_obs.obs.event("call.cadence_stage", stage=cadence_stage, elapsed_s=round(elapsed_s, 1))
                c_name = state.get("display_name") or "Friend"

                if cadence_stage in (rt_cadence.STAGE_SOFT_WRAP, rt_cadence.STAGE_FIRM_WRAP):
                    print(f"[rt-cadence] transitioning to {cadence_stage} (elapsed={elapsed_s:.1f}s)", flush=True)
                    ag = state.get("agent")
                    if ag is not None and hasattr(ag, "update_instructions"):
                        directive = rt_cadence.render_cadence_directive(cadence_stage, c_name)
                        with contextlib.suppress(Exception):
                            asyncio.create_task(ag.update_instructions(directive + ag._base_instructions))

                elif cadence_stage == rt_cadence.STAGE_HARD_CAP:
                    print(f"[rt-cadence] 35m hard ceiling reached (elapsed={elapsed_s:.1f}s) — saying goodbye and disconnecting", flush=True)
                    with contextlib.suppress(Exception):
                        voice_used = voice_pref or os.getenv("GEMINI_LIVE_VOICE", "Aoede")
                        bye = rt_cadence.get_cadence_farewell_message(c_name)
                        path = await asyncio.to_thread(_clip_wav, voice_used, bye, "cadence-farewell")
                        if path:
                            _append_transcript_shared(state, "agent", bye)
                            await _play_clip(ctx.room, path, preroll=0.2,
                                             exclude_identity=state.get("bridge_identity"))
                    with contextlib.suppress(Exception):
                        await ctx.delete_room()
                    break

            last_activity = state.get("last_activity_time") or time.time()
            silence_duration = time.time() - last_activity
            if silence_duration < 5.0:
                soft_nudge_done = False

            _ses = state.get("session")
            _busy = False
            with contextlib.suppress(Exception):
                _busy = (getattr(_ses, "agent_state", "listening") != "listening"
                         or getattr(_ses, "user_state", "listening") == "speaking")
            if _busy:
                state["last_activity_time"] = time.time()
                continue

            if state.get("bridge_active"):
                if state.get("shield_speaking") and time.time() > (state.get("shield_speak_until") or 0):
                    _shield_release_floor(state)
                started = state.get("bridge_started_at") or time.time()
                import rt_bridge as _rb
                cap = state.get("bridge_cap") or _rb.MAX_BRIDGE_SECONDS
                if time.time() - started > cap:
                    print("[rt-bridge] bridge exceeded max duration — dropping third party", flush=True)
                    ag = state.get("agent")
                    if ag is not None:
                        with contextlib.suppress(Exception):
                            await ag._clear_bridge()
                continue

            if _is_farewell(state.get("last_user_transcript") or ""):
                last_user_txt = state.get("last_user_transcript") or ""
                _log_pii("[rt-silence] farewell detected — disconnecting line", last_user_txt)
                with contextlib.suppress(Exception):
                    await ctx.delete_room()
                break

            quiet_limit = 8.0 if state.get("greeting_missed") else 30.0
            if silence_duration >= quiet_limit and not soft_nudge_done:
                print(f"[rt-silence] {quiet_limit:.0f}s silence ({silence_duration:.1f}s) — checking in live dynamically", flush=True)
                with contextlib.suppress(Exception):
                    rt_obs.obs.event("turn.silence", ms=round(silence_duration * 1000, 1))
                soft_nudge_done = True
                state["greeting_missed"] = False
                state["last_activity_time"] = time.time()
                with contextlib.suppress(Exception):
                    voice_used = voice_pref or os.getenv("GEMINI_LIVE_VOICE", "Aoede")
                    nudge = "Take your time — I'm right here with you."
                    path = await asyncio.to_thread(_clip_wav, voice_used, nudge, "checkin")
                    if path:
                        _append_transcript_shared(state, "agent", nudge)
                        await _play_clip(ctx.room, path, preroll=0.05,
                                         exclude_identity=state.get("bridge_identity"))

            if silence_duration >= 180.0:
                print("[rt-silence] 180s silence threshold reached — disconnecting", flush=True)
                with contextlib.suppress(Exception):
                    voice_used = voice_pref or os.getenv("GEMINI_LIVE_VOICE", "Aoede")
                    bye = "I'll let you go for now — call me back any time, I'm right here."
                    path = await asyncio.to_thread(_clip_wav, voice_used, bye, "away-goodbye")
                    if path:
                        _append_transcript_shared(state, "agent", bye)
                        await _play_clip(ctx.room, path, preroll=0.05,
                                         exclude_identity=state.get("bridge_identity"))
                with contextlib.suppress(Exception):
                    await ctx.delete_room()
                break


    asyncio.create_task(_silence_monitor_task())


if __name__ == "__main__":
    import config
    with contextlib.suppress(Exception):
        config.validate_startup_config()
    if os.getenv("RT_AUTO_MIGRATE", "").strip().lower() in ("1", "true", "yes") or os.getenv("AUTO_MIGRATE", "").strip().lower() in ("1", "true", "yes"):
        try:
            import importlib.util
            migrate_path = os.path.join(os.path.dirname(__file__), "scripts", "migrate.py")
            if os.path.exists(migrate_path):
                spec = importlib.util.spec_from_file_location("migrate_mod", migrate_path)
                if spec and spec.loader:
                    m_mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(m_mod)
                    runner = m_mod.MigrationRunner(None, exclusive=False)
                    applied = runner.apply(dry_run=False)
                    print(f"[rt-migrate] auto-migration complete: {len(applied)} applied", flush=True)
        except Exception as _e:
            print(f"[rt-migrate] auto-migration failed: {_e}", flush=True)
    # A lane that requires a pepper and lacks one must die here, loudly, before
    # the health server can ever report it ready.
    rt_prefs.phone_hash("+15555550100")
    # Readiness = config present, DB client constructible, Gemini key set,
    # pepper present wherever it is required. Registered before the server
    # starts so no probe ever sees an empty check list.
    rt_health.register_check("config", lambda: not config.missing_config())
    rt_health.register_check("db", lambda: rt_prefs._db() is not None)
    rt_health.register_check("gemini_key", lambda: bool(os.getenv("GOOGLE_API_KEY")))
    rt_health.register_check("pepper", lambda: not config.pepper_required() or _pepper_ok(os.getenv("RT_PHONE_HASH_PEPPER")))
    # Health server lives here, not at import: every spawned job process
    # re-imports this module and must not fight the worker for the port.
    if os.getenv("RT_HARNESS_TEST_MODE") != "1" and "pytest" not in sys.modules and "--json" not in sys.argv:
        rt_health.start_health_server()
    _startup_rpc_probe()
    with contextlib.suppress(Exception):
        print("[rt-caps] this worker can actually do:", flush=True)
        print(rt_capabilities.report(), flush=True)
    with contextlib.suppress(Exception):
        import rt_recovery
        rt_recovery.run_boot_recovery()
    cli.run_app(WorkerOptions(
        entrypoint_fnc=entrypoint,
        prewarm_fnc=prewarm,
        agent_name=AGENT_NAME,
        initialize_process_timeout=float(os.getenv("RT_INIT_TIMEOUT", "60")),
        num_idle_processes=int(os.getenv("NUM_IDLE_PROCESSES", "1")),
        drain_timeout=float(os.getenv("DRAIN_TIMEOUT", "60")),
        shutdown_process_timeout=float(os.getenv("RT_SHUTDOWN_GRACE", "90")),
        host=os.getenv("RT_WORKER_HTTP_HOST", "127.0.0.1"),
        port=int(os.getenv("RT_HEALTH_PORT", "8082")),
    ))
