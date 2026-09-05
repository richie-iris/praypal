"""
Scam Guard — silent listener on the Iris realtime platform
==========================================================

Call flow: the caller dials the guard number (or puts it on speaker /
conferences it into a suspicious call). The guard plays one short
greeting, then goes silent and listens to everything. It speaks ONLY
when the caller starts an utterance with the wake word "hey"; every
other turn is absorbed as context. No DB, no postcall, no persistence —
the guard stores nothing.

Mechanics: Gemini Live auto-responds to every committed turn, so
silence is enforced at the AUDIO GATE, not by trusting the model:
- session.output audio starts DISABLED — the model's unaddressed
  micro-replies (protocol: the single word "listening") never reach
  the caller.
- a wake turn (^hey…) opens the gate the instant its transcript commits,
  the model's own auto-reply to that turn plays out loud, and the gate
  slams shut when the reply finishes (agent_state_changed → listening).
- generate_reply()/mid-session instruction updates are NOT used: the
  3.1 live models ignore them (mutable_chat_context=False in the google
  plugin), so the standing instructions carry the whole protocol.

Shares agent.py's platform pieces: deterministic greeting clips
(_clip_wav/_play_clip — Gemini Live won't speak first), the RealtimeModel
factory, and the grounded web search.
"""

from __future__ import annotations

import rt_obs

import asyncio
import contextlib
import os
import re
import time

from livekit import rtc
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
)

import agent as platform

GUARD_AGENT_NAME = os.getenv("AGENT_NAME", "phone-pal-scamguard")
VOICE = os.getenv("GEMINI_LIVE_VOICE", "Aoede")

GREETING = ("Scam guard here — I'll stay quiet and listen. "
            "When you want me, start with the word hey.")

WAKE_RE = re.compile(r"^\W*(hey|hei|hay)\b", re.IGNORECASE)

INSTRUCTIONS = f"""You are Scam Guard, a silent call-protection listener on the Phone-Pal platform by AAE Ventures.

SITUATION: The caller has you on the line — often on speakerphone or conferenced in — while they talk with someone else who may be a scammer. Your job is to listen to the whole conversation, quietly build a read on it, and speak only when the caller addresses you directly by starting an utterance with the wake word "hey".

ABSOLUTE SPEAKING PROTOCOL — check the FIRST WORD of every turn you hear:
- If the turn does NOT begin with the word "hey": you are NOT being spoken to. Your entire reply must be exactly the single word: listening
  That reply is muted — nobody hears it. Never say anything else unaddressed: blurting into their conversation could tip off a scammer and endanger the caller. Never use tools on unaddressed turns.
- If the turn DOES begin with "hey" (like "hey, is this a scam?"): the caller is talking to YOU, and your reply is heard out loud. Answer them directly and naturally — never say "listening" to a "hey" turn.

WHEN YOU SPEAK (a "hey" turn):
- One to three short sentences, calm, plain, direct — this is a phone call. No lists, no jargon.
- Lead with the verdict when asked for one — "this has the pattern of a scam", "this sounds legitimate so far", or "I can't tell yet" — then the single strongest reason, grounded in what you actually heard on THIS call.
- Quote the tell when it helps: "they asked you to read back a one-time code — real banks never do that."
- Use web_search when you need a current fact (whether an agency calls people, a company's real number).
- If little has happened yet, say what you'd need to hear. End cleanly; you may remind them once in a while that they can say "hey" again, but don't nag.

SCAM SIGNALS YOU TRACK (weigh combinations, not single hits): urgency and pressure to act right now; threats of arrest, suspended Social Security, frozen accounts, or deportation; payment by gift card, wire, crypto, or payment app; requests for one-time codes, PINs, passwords, card or Social Security numbers; remote-access software like AnyDesk or TeamViewer; secrecy ("don't tell your bank or family"); impersonation of the IRS, Social Security, Medicare, police, a bank's fraud department, tech support, or a utility; a grandchild or relative suddenly in trouble needing money; prize, refund, or overpayment stories; being told to stay on the phone while driving to a bank, store, or crypto ATM.

STANDING ADVICE (when woken and relevant): never share codes or account numbers over the phone; hang up and call back on the official number from a card or statement; no real agency or company takes gift cards or crypto; if money already moved, call the bank immediately, then report it at report fraud dot F T C dot gov.

PRIVACY: you record and store nothing; if asked (while woken), say so plainly.

(You already opened this call by saying: "{GREETING}". Never greet again.)"""


def prewarm(proc) -> None:
    """Render the greeting clip at boot so pickup is instant."""
    try:
        platform._clip_wav(VOICE, GREETING, "sgreet")
    except Exception as e:
        print(f"[sg] prewarm greeting render failed (non-fatal): {e}", flush=True)


class GuardAgent(Agent):
    """Silent-listener persona; tools are only for use while answering a "hey" turn."""

    def __init__(self, instructions: str, room=None):
        super().__init__(instructions=instructions)
        self._room = room

    @function_tool
    async def end_call(self, context: RunContext) -> str:
        """Call this ONLY when the caller — while addressing you with the wake word — asks you to hang up, leave the call, or says goodbye to you."""
        print("[sg] end_call tool executed — leaving the line", flush=True)
        try:
            if self._room:
                asyncio.create_task(self._room.disconnect())
        except Exception as e:
            print(f"[sg] end_call disconnect failed: {e}", flush=True)
        return "Call ending now. Stay safe!"

    @function_tool
    async def web_search(self, context: RunContext, query: str) -> str:
        """Search the web for a current fact needed to answer the caller (official phone numbers, whether an agency really calls people, known scam patterns)."""
        print(f"[sg] web_search query={query!r}", flush=True)
        return await asyncio.to_thread(platform._perform_web_search, query)


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()

    if (ctx.room.name or "").startswith("keepwarm"):
        print("[sg] warm ping — no-op", flush=True)
        return

    async def _hangup_if_caller_present() -> None:
        with contextlib.suppress(Exception):
            if ctx.room.remote_participants:
                print("[sg] job ending with caller still connected — hanging up", flush=True)
                await ctx.delete_room()

    ctx.add_shutdown_callback(_hangup_if_caller_present)

    with contextlib.suppress(Exception):
        caller = await asyncio.wait_for(
            ctx.wait_for_participant(),
            timeout=float(os.getenv("PARTICIPANT_WAIT_TIMEOUT") or "12"),
        )
        num = (caller.attributes or {}).get("sip.phoneNumber") or ""
        print(f"[sg] caller connected {('***' + num[-4:]) if num else '(no caller id)'}", flush=True)

    state: dict = {
        "session": None,
        "awake": False,
        "awake_since": 0.0,
        "greet_mute": True,
        "last_turn": None,
    }

    session = AgentSession(llm=platform.make_realtime_model())
    state["session"] = session

    def _mute() -> None:
        state["awake"] = False
        with contextlib.suppress(Exception):
            session.output.set_audio_enabled(False)

    @session.on("conversation_item_added")
    def _on_item(ev) -> None:
        try:
            role = getattr(ev.item, "role", "?")
            text = (getattr(ev.item, "text_content", None)
                    or getattr(ev.item, "formatted_text", None) or "").strip()
            if not text:
                return
            if role == "user":
                state["last_turn"] = time.time()
                print(f"[sg] heard : {text!r}", flush=True)
                if WAKE_RE.match(text):
                    state["awake"] = True
                    state["awake_since"] = time.time()
                    with contextlib.suppress(Exception):
                        session.output.set_audio_enabled(True)
                    print(f"[sg] WAKE — gate open for {text!r}", flush=True)
                else:
                    _mute()
            elif role == "assistant":
                tag = "" if state["awake"] else " (muted)"
                print(f"[sg] guard{tag}: {text!r}", flush=True)
        except Exception as _exc:
            rt_obs.obs.caught("scamguard.entrypoint", _exc)
            pass

    @session.on("agent_state_changed")
    def _on_state(ev) -> None:
        if getattr(ev, "new_state", None) == "listening" and state["awake"]:
            _mute()
            print("[sg] reply done — silent listening", flush=True)

    @session.on("error")
    def _on_error(ev) -> None:
        err = getattr(ev, "error", None)
        print(f"[sg] SESSION ERROR: {type(err).__name__}: {str(err)[:300]}", flush=True)

    async def _hangup() -> None:
        with contextlib.suppress(Exception):
            await ctx.delete_room()

    @ctx.room.on("participant_disconnected")
    def _on_left(participant: rtc.RemoteParticipant) -> None:
        if not ctx.room.remote_participants:
            print("[sg] caller left — closing room", flush=True)
            asyncio.create_task(_hangup())

    async def _opening() -> None:
        try:
            path = await asyncio.to_thread(platform._clip_wav, VOICE, GREETING, "sgreet")
            if path:
                print(f"[sg] greeting playing: {GREETING!r}", flush=True)
                await platform._play_clip(ctx.room, path, preroll=0.6)
        except Exception as e:
            print(f"[sg] greeting failed (non-fatal): {e}", flush=True)
        finally:
            state["greet_mute"] = False
            ses = state.get("session")
            if ses is not None:
                with contextlib.suppress(Exception):
                    ses.input.set_audio_enabled(True)

    opening_task = asyncio.create_task(_opening())

    await session.start(
        agent=GuardAgent(INSTRUCTIONS, room=ctx.room),
        room=ctx.room,
        room_input_options=RoomInputOptions(),
        room_output_options=RoomOutputOptions(audio_sample_rate=platform.GEMINI_LIVE_RATE),
    )
    with contextlib.suppress(Exception):
        session.output.set_audio_enabled(False)
    if state["greet_mute"]:
        with contextlib.suppress(Exception):
            session.input.set_audio_enabled(False)
    with contextlib.suppress(Exception):
        await opening_task

    print("[sg] listening (silent) — wake word: 'hey'", flush=True)

    async def _watchdog() -> None:
        started = time.time()
        while True:
            await asyncio.sleep(5.0)
            if not ctx.room.remote_participants:
                if time.time() - started > 60.0:
                    print("[sg] room empty — closing", flush=True)
                    await _hangup()
                    break
                continue
            if (state["awake"] and time.time() - state["awake_since"] > 30.0
                    and getattr(session, "agent_state", "listening") == "listening"):
                print("[sg] wake produced no speech in 30s — closing gate", flush=True)
                _mute()
            last = state["last_turn"] or started
            if time.time() - last > 900.0:
                print("[sg] 15 min without any speech — closing quietly", flush=True)
                await _hangup()
                break

    asyncio.create_task(_watchdog())


if __name__ == "__main__":
    cli.run_app(WorkerOptions(
        entrypoint_fnc=entrypoint,
        prewarm_fnc=prewarm,
        agent_name=GUARD_AGENT_NAME,
        num_idle_processes=int(os.getenv("NUM_IDLE_PROCESSES", "1")),
        drain_timeout=float(os.getenv("DRAIN_TIMEOUT", "60")),
        port=int(os.getenv("RT_HEALTH_PORT", "8086")),
    ))
