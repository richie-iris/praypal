"""rt_prompts.py — Prompt engineering, onboarding blocks, dynamic greetings, and turn deduplication.

Extracts prompt generation and instruction assembly from agent.py.
"""
from __future__ import annotations

import contextlib
import os
from typing import Any

import rt_obs

_TOOL_NAMES = (
    "db_tool", "web_search", "recall_earlier", "find_number", "bridge_call",
    "press_keys", "listen_only", "end_bridge", "end_call",
    "save_email", "send_email", "send_calendar_invite", "send_sms",
    "schedule_reminder_call",
)
_CALLER_BLOCK_TOKENS = 325
RT_CTX_MIN_HISTORY = int(os.getenv("RT_CTX_MIN_HISTORY", "1700"))

_ONBOARDING_ORDER = ("name", "intro", "ownership", "first_fact")

_ONBOARDING_STEP_TEXT = {
    "name": ('Get their name in the natural flow of talking: "Who do I have the pleasure of speaking with?" '
             'Save it immediately with db_tool(action="name", item="<their name>"). If they give you a name '
             'to call you (like Iris), receive it with genuine delight and save it. Never interrogate or make them repeat themselves.'),
    "intro": ("Somewhere in the conversation, let them know you're {alias} and you're theirs — "
              "they can call you anytime, about anything or nothing. One warm line, not a speech."),
    "ownership": ("When it fits naturally, mention ONE thing they can shape — that you'll remember "
                  "what they tell you, that they can set rules for you, or that they can even give "
                  "you a different name. One at a time, offered like a gift, never as a feature list."),
    "first_fact": "Draw out one personal thing (family, a pet, a hobby) and save it the moment it's shared, confirming in your own words.",
}

# Greeting variants
_GREET_FIRST: tuple[str, ...] = (
    "Hello there, this is Phone-Pal, your voice companion. I'm so glad you called today — who do I have the pleasure of speaking with?",
    "Hello there, this is Phone-Pal, your voice companion. I'm really glad you reached out — what's your name?",
    "Hello there, this is Phone-Pal, your voice companion. I'm so happy to connect with you — who am I speaking with today?",
    "Hello there, this is Phone-Pal, your voice companion. It's wonderful to meet you — what should I call you?",
)

_GREET_UNNAMED: tuple[str, ...] = (
    "Hi there! It's so good to hear from you again — what's your name?",
    "Hello! Great to talk with you again — what name do you go by?",
    "Hi! Welcome back to Phone-Pal — what name should I call you?",
    "Hello there! So glad you called back — what's your name?",
)

_GREET_KNOWN: tuple[str, ...] = (
    "Hey {display_name}! So wonderful to hear from you. How are you doing today?",
    "Hi {display_name}! How has your day been going?",
    "Hey {display_name}, so glad you called. What's new with you?",
    "Hi {display_name}! How've you been feeling lately?",
    "Hey {display_name}, great to hear your voice. What's on your mind today?",
    "Hi {display_name} — how are things going with you today?",
)


def _tool_decl_tokens(agent_cls: Any = None) -> int:
    """Estimated tokens required for tool declarations."""
    import inspect
    try:
        if agent_cls is None:
            return 1300
        total = 0
        for name in _TOOL_NAMES:
            fn = getattr(agent_cls, name, None)
            doc = inspect.getdoc(getattr(fn, "__wrapped__", fn)) if fn else None
            total += len(doc or "")
        return (total // 4) or 1300
    except Exception as _exc:
        rt_obs.obs.caught("rt_prompts._tool_decl_tokens", _exc)
        return 1300


def _prompt_floor_tokens(agent_cls: Any = None) -> int:
    """The context every turn carries before a single word of conversation."""
    try:
        import rt_hydrator
        return len(rt_hydrator.STENCIL_TEMPLATE) // 4 + _tool_decl_tokens(agent_cls) + _CALLER_BLOCK_TOKENS
    except Exception as _exc:
        rt_obs.obs.caught("rt_prompts._prompt_floor_tokens", _exc)
        return _tool_decl_tokens(agent_cls) + _CALLER_BLOCK_TOKENS


def _fit_context_window(trigger: int, target: int, agent_cls: Any = None) -> tuple[int, int, str]:
    """Keep the window above the prompt floor, raising it when it is set too low."""
    floor = _prompt_floor_tokens(agent_cls)
    want_target = max(target, floor + RT_CTX_MIN_HISTORY)
    want_trigger = max(trigger, want_target + 2000)
    if want_target == target and want_trigger == trigger:
        return trigger, target, ""
    return want_trigger, want_target, (
        f"prompt floor is ~{floor} tokens and target {target} left only "
        f"{max(target - floor, 0)} for the conversation"
    )


def _onboarding_block(
    missing: list[str],
    alias: str = "your companion",
    call_count: int = 0,
) -> str:
    """Only the onboarding beats not yet completed."""
    if not missing:
        return ""
    steps = "\n".join(
        f"- {_ONBOARDING_STEP_TEXT[k].format(alias=alias)}"
        for k in _ONBOARDING_ORDER
        if k in missing
    )
    catch_the_name = ""
    if "name" in missing and int(call_count or 0) >= 1:
        catch_the_name = (
            "\n\nYou have talked with them before and still have no name for them, so the line "
            "you already opened this call with asked for it out loud. Listen for the answer and "
            "save it the moment it comes with db_tool(action=\"name\", item=\"<their name>\"). "
            "Don't let the call end without it — but don't ask a second time either, they have "
            "been asked once already."
        )
    return (
        "\n\n# STILL TO COME (things you haven't gotten to with them yet)\n"
        "You are a friend on the phone FIRST — talk with them, follow what they care about, "
        "let the conversation breathe. These are not a script and not an intake form: weave "
        "in at most ONE of them per call, only when it fits, and never at the cost of "
        "actually listening.\n" + steps + catch_the_name
    )


def _fallback_instructions() -> str:
    """The prompt opened with when the database is too slow to wait for."""
    return (
        os.getenv("RT_SYSTEM_PROMPT", "")
        or "You are a genuine, warm voice companion and trusted friend on the phone. "
        "Speak in 1-2 short, natural, unhurried sentences. Listen actively, validate feelings, and care. "
        "You are a computer companion, never a biological person. Emergencies → 911; "
        "mental-health crisis → 988. If a detail from earlier feels faded, say so honestly rather than inventing a memory."
    ) + _onboarding_block(list(_ONBOARDING_ORDER))


def _build_instructions(
    caller_e164: str | None = None,
    call_count: int = 0,
    prefetch_bundle: dict | None = None,
) -> tuple[str, str]:
    """Compile mission prompt stencil and dynamic caller context.

    Returns (prompt_text, resolved_display_name).
    """
    try:
        import rt_hydrator

        with contextlib.suppress(Exception):
            _b = prefetch_bundle or {}
            rt_obs.obs.event(
                "prompt.hydrate_in",
                facts=len(_b.get("facts") or []),
                schemas=len(_b.get("schemas") or []),
                reminders=len(_b.get("reminders") or []),
                source=("prefetch" if prefetch_bundle else "fetch"),
            )
        hydrated, meta = rt_hydrator.discover_and_hydrate_prompt(
            caller_e164, prefetch_bundle=prefetch_bundle or {}
        )
        caller_data = (
            meta.get("caller_data")
            or (prefetch_bundle.get("caller") if prefetch_bundle else {})
            or {}
        )
        raw_alias = (caller_data.get("agent_alias") or "").strip()
        agent_alias = (
            "your companion"
            if (not raw_alias or raw_alias.lower() in ("iris", "your companion"))
            else raw_alias
        )
        onb = meta.get("onboarding") or {}
        if not onb.get("done"):
            missing = [k for k in _ONBOARDING_ORDER if not onb.get(k)]
            hydrated += _onboarding_block(
                missing,
                alias=agent_alias,
                call_count=int(caller_data.get("call_count") or 0),
            )
        resolved_name = caller_data.get("display_name") or "Friend"
        return hydrated, resolved_name
    except Exception as e:
        print(f"[rt] prompt hydration fallback ({e})", flush=True)
        return _fallback_instructions(), "Friend"


def _greeting_text(
    display_name: str | None,
    alias: str | None,
    call_count: int = 1,
    seed: int = 0,
    pick: int | None = None,
) -> str:
    """Deterministic, short opening greeting."""
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
    idx = pick if pick is not None else n
    return _GREET_KNOWN[idx % len(_GREET_KNOWN)].format(display_name=name)


def _greeted_note(text: str) -> str:
    """Instruction suffix so the model never greets a second time."""
    return (
        f"\n\n(You already opened this call by saying: \"{text}\". Do not greet again — "
        f"wait for the caller and respond naturally to whatever they say.)"
    )


def is_duplicate_agent_turn(
    text: str,
    prev: str | None,
    prev_at: float | None,
    now: float,
    window: float = 12.0,
) -> bool:
    """Is this assistant turn a repeat or prefix truncation of the one before it?"""
    if not prev or prev_at is None:
        return False
    if (now - prev_at) > window:
        return False
    t = " ".join((text or "").lower().split())
    p = " ".join((prev or "").lower().split())
    if not t or not p:
        return False
    if t == p:
        return True
    if len(t) >= 6 and p.startswith(t):
        return True
    return False
