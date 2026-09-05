"""rt_trace.py — record what actually happened on a call, so it can be debugged later.

Every write here is fire-and-forget on a background thread and swallows its own
errors. Tracing must never slow a call down, and must never be the reason a call
fails: if the database is unreachable the caller should not be able to tell.

What gets kept: the exact system prompt the model received, the greeting, the
transcript, every tool call with its arguments and result, every guard decision,
bridge events, and both postcall passes.
"""
from __future__ import annotations

import rt_obs

import json
import os
import subprocess
import threading
import time

import rt_prefs

_ENABLED = os.getenv("RT_TRACE", "1").strip().lower() in ("1", "true", "yes")
_MAX_FIELD = 8000


def _version() -> str:
    """The commit the worker is running, so a call can be tied to its code."""
    v = os.getenv("RT_AGENT_VERSION", "").strip()
    if v:
        return v
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],  # noqa: S603,S607 - fixed argv, git from PATH by design
                             cwd=os.path.dirname(os.path.abspath(__file__)),
                             capture_output=True, text=True, timeout=3)
        return (out.stdout or "").strip() or "unknown"
    except Exception as _exc:
        rt_obs.obs.caught("rt_trace._version", _exc)
        return "unknown"


_VERSION = _version()


def _clip(v, limit: int = _MAX_FIELD):
    if isinstance(v, str) and len(v) > limit:
        return v[:limit] + f"\n… [{len(v) - limit} more chars]"
    return v


def _fire(fn, *args, **kwargs) -> None:
    if not _ENABLED:
        return

    def _run() -> None:
        try:
            fn(*args, **kwargs)
        except Exception as e:
            print(f"[rt-trace] {getattr(fn, '__name__', 'write')} failed (non-fatal): {e}", flush=True)

    threading.Thread(target=_run, daemon=True).start()


def start(state: dict, *, call_id: str, phone_hash: str, room: str, model: str,
          voice: str, display_name: str, agent_alias: str, call_number: int,
          greeting: str, system_prompt: str) -> None:
    """Open the record for a call. Also stamps state so events can be attributed."""
    state["trace_call_id"] = call_id
    state["trace_t0"] = time.time()

    def _w() -> None:
        rt_prefs._req("POST", "rpc/rt_call_start", {
            "p_call_id": call_id, "p_hash": phone_hash, "p_room": room,
            "p_version": _VERSION, "p_model": model, "p_voice": voice,
            "p_name": display_name, "p_alias": agent_alias,
            "p_number": int(call_number or 0), "p_greeting": _clip(greeting, 500),
            "p_prompt": _clip(system_prompt, 20000),
        }, _skip_audit=True)
    _fire(_w)

    for _role, _text in (state.pop("_pending_turns", None) or []):
        turn(state, _role, _text)

    print(f"[rt-trace] call {call_id} opened (agent {_VERSION})", flush=True)


def event(state: dict, kind: str, name: str, detail: dict | None = None) -> None:
    """Record one thing that happened: a tool call, a guard decision, a bridge step."""
    call_id = (state or {}).get("trace_call_id")
    if not call_id:
        return
    elapsed = int((time.time() - (state.get("trace_t0") or time.time())) * 1000)
    safe = {k: _clip(v, 4000) for k, v in (detail or {}).items()}

    def _w() -> None:
        rt_prefs._req("POST", "rpc/rt_call_event", {
            "p_call_id": call_id, "p_elapsed": elapsed, "p_kind": kind,
            "p_name": name, "p_detail": safe,
        }, _skip_audit=True)
    _fire(_w)


def turn(state: dict, role: str, text: str) -> None:
    """Persist one spoken turn AT THE MOMENT IT IS SPOKEN.

    The in-memory `transcript_lines` list is the fast path the live call reads
    from, but it dies with the process. A container rebuild, an OOM or a SIGKILL
    mid-call used to destroy the conversation and the memory it would have
    become, leaving nothing behind to recover from. These rows are what make
    that recoverable: rt_call_transcript() rebuilds the same text from them, and
    the boot sweep re-queues any call whose extraction never ran.

    Fire-and-forget on a background thread like every other trace write — a slow
    or unreachable database must never add latency to someone's phone call.
    """
    if not _ENABLED:
        return
    text = (text or "").strip()
    if not text:
        return
    if not (state or {}).get("trace_call_id"):
        pending = state.setdefault("_pending_turns", [])
        if len(pending) < 200:
            pending.append((role, text))
        return
    event(state, "turn", role, {"text": text})


def finish(state: dict, *, transcript: str, in_tokens: int, out_tokens: int,
           postcall: dict | None = None, meta: dict | None = None) -> None:
    """Close the record. Runs inline in the postcall path, which already has time."""
    call_id = (state or {}).get("trace_call_id")
    if not call_id or not _ENABLED:
        return
    try:
        rt_prefs._req("POST", "rpc/rt_call_finish", {
            "p_call_id": call_id,
            "p_transcript": _clip(transcript, 40000),
            "p_in": int(in_tokens or 0), "p_out": int(out_tokens or 0),
            "p_bridge_number": state.get("bridge_number"),
            "p_bridge_mode": state.get("bridge_mode"),
            "p_postcall": json.loads(json.dumps(postcall or {}, default=str)),
            "p_meta": meta or {},
        }, _skip_audit=True)
        print(f"[rt-trace] call {call_id} closed", flush=True)
    except Exception as e:
        print(f"[rt-trace] finish failed (non-fatal): {e}", flush=True)
