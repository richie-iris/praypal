"""rt_capabilities.py — what this worker can ACTUALLY do, decided by credentials.

Born from a real call on 2026-08-14: the companion told a caller "I just sent
you a text to test it" when no SMS had been attempted and no SMS credential has
ever existed in this lane. The model wasn't malicious — it was offered a
send_sms tool, the tool failed at runtime, and it improvised around the failure.

The fix is structural, not persuasive. A tool whose credential is absent is
never registered on the model at all, and the system prompt tells her plainly
what she cannot do — so the honest sentence ("I can't send texts, but I'll
remember and remind you next call") is the only sentence available. Phone-Pal's
first brand law: she never claims an ability she does not have.

Evaluated lazily against the process environment, which is fixed at boot on
the box (worker.env). A deploy that adds the Twilio credentials turns SMS on
at the next worker restart, and nothing else needs to change.

ONE TABLE. The law used to live in three hand-synced structures — a
requirements dict, an if-ladder of prompt lines, and a tools-line builder — so
a tool could be gated in one and invisible to the others. It was: bridge_call
placed real outbound SIP calls and appeared in none of them, dialling with
sip_trunk_id="" in every lane that had no trunk, failing at runtime, and
handing the model exactly the improvise-around-a-failure moment this module
exists to prevent. Everything below now derives from CAPABILITIES, so a new
tool cannot be gated in one place and forgotten in the others.
"""
from __future__ import annotations

import rt_obs

import os
from dataclasses import dataclass

from dotenv import load_dotenv
load_dotenv(".env.local")
load_dotenv(".env")


_FALSY = {"0", "false", "no", "off", ""}


def _has(*names: str) -> bool:
    """True only if every named var is set to something that isn't a plain
    'off' value. A bare non-empty-string check let RT_SCHEDULER_ENABLED=0 —
    the natural way an operator types 'disabled' — stay truthy and silently
    keep real outbound calling live (review finding, 2026-08-15)."""
    return all((os.getenv(n) or "").strip().lower() not in _FALSY for n in names)


@dataclass(frozen=True)
class Capability:
    """One ability, and everything that follows from having or lacking it."""

    tools: tuple[str, ...]
    requires: tuple[str, ...]
    prompt_label: str
    absent_line: str | None = None

    def available(self) -> bool:
        return _has(*self.requires)


# Order here is the order the TOOLS line renders in the prompt.
CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        tools=("db_tool", "recall_earlier", "manage_goals"),
        requires=("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"),
        prompt_label="db_tool, manage_goals",
        absent_line=(
            "You CANNOT look anything up about this caller or save anything "
            "for next time — the memory store is unreachable. Say so plainly "
            "rather than inventing what you remember."),
    ),
    Capability(
        tools=("web_search",),
        requires=("GOOGLE_API_KEY",),
        prompt_label="web_search",
        absent_line=(
            "You CANNOT search the web. If asked something you don't know, "
            "say you don't know rather than guessing from memory."),
    ),
    Capability(
        tools=("find_number",),
        requires=("GOOGLE_API_KEY",),
        prompt_label="find_number",
        absent_line=(
            "You CANNOT look up phone numbers for businesses or services."),
    ),
    # The Twilio pair sits on both dial tools since 2026-09-02: rt_carrier
    # asks the account what today has cost before any dial, and without the
    # credentials that question has no answer, so the tool is never offered.
    Capability(
        tools=("bridge_call",),
        requires=("SIP_OUTBOUND_TRUNK_ID", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"),
        prompt_label="bridge_call",
        absent_line=(
            "You CANNOT dial anyone or bring a third person onto this call — "
            "there is no outbound line here. Never say you are connecting "
            "them, ringing someone, or putting a call through."),
    ),
    Capability(
        tools=("send_sms",),
        requires=("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "RT_SMS_ENABLED"),
        prompt_label="send_sms",
        absent_line=(
            "You CANNOT send text messages. If asked, say so plainly and offer "
            "what you truly can: remember it and bring it up on the next call."),
    ),
    Capability(
        tools=("send_email", "send_calendar_invite"),
        requires=("RESEND_API_KEY",),
        prompt_label="send_email, send_calendar_invite",
        absent_line=(
            "You CANNOT send emails or calendar invites. Never offer or suggest "
            "sending emails or calendar invites. If asked directly, say so plainly "
            "and keep all interaction on the phone line."),
    ),
    Capability(
        tools=("schedule_reminder_call",),
        requires=("SIP_OUTBOUND_TRUNK_ID", "RT_SCHEDULER_ENABLED", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"),
        prompt_label="schedule_reminder_call",
        absent_line=(
            "You CANNOT call anyone back — there is no outbound-calling tool "
            "here. db_tool(action='remind') only surfaces text on their NEXT "
            "call; never say 'I'll call you' or 'I'll call you back.'"),
    ),
)

# Tools that need no credential and are therefore always real: they act only on
# the call already in progress.
ALWAYS_AVAILABLE: frozenset[str] = frozenset({
    "press_keys", "listen_only", "end_bridge", "end_call", "save_email",
})

# Every tool that depends on a credential, computed from the table without
# reading the environment. This is what the gate withholds when it cannot
# evaluate the environment at all: a gate that exists to take abilities away
# must fail closed, or its failure hands the model the exact unbacked tools it
# was built to hide.
CREDENTIALED_TOOLS: frozenset[str] = frozenset(
    t for cap in CAPABILITIES for t in cap.tools)


def disabled_tools() -> set[str]:
    """Tool names that must not be offered to the model in this environment."""
    off: set[str] = set()
    for cap in CAPABILITIES:
        if not cap.available():
            off.update(cap.tools)
    return off


def enabled(tool: str) -> bool:
    return tool not in disabled_tools()


def cannot_do_lines() -> list[str]:
    """Plain-language lines for the system prompt: what she must say she can't do.

    Written as the honest alternative, not just the prohibition — the voice
    principle is 'I can't send a text, but I'll remember to remind you next
    call' is a complete and honorable sentence.
    """
    return [cap.absent_line for cap in CAPABILITIES
            if cap.absent_line and not cap.available()]


def tools_line(base: str = "") -> str:
    """The TOOLS list for the prompt — only what is real in this environment.

    `base` is prepended verbatim for callers that want to force-name tools; it
    is no longer where the credentialed tools hide, which is how bridge_call
    stayed ungated.
    """
    names = [cap.prompt_label for cap in CAPABILITIES if cap.available()]
    return ", ".join(([base] if base else []) + names)


def report() -> str:
    """One line per capability, for the worker to state at boot.

    A lane that cannot dial, cannot text, or cannot remember should say so on
    the way up rather than discovering it mid-call.
    """
    rows = []
    for cap in CAPABILITIES:
        ok = cap.available()
        missing = "" if ok else f"  (needs {', '.join(cap.requires)})"
        rows.append(f"  {'ON ' if ok else 'off'}  {cap.prompt_label}{missing}")
    return "\n".join(rows)


def missing_from(required: list[str]) -> list[str]:
    """Which of these named tools this environment cannot actually back.

    Raises on a name no capability declares, so a typo in a deploy's required
    list fails loudly instead of silently asserting nothing.
    """
    known = {t for cap in CAPABILITIES for t in cap.tools} | set(ALWAYS_AVAILABLE)
    unknown = sorted(set(required) - known)
    if unknown:
        raise ValueError(f"no capability declares: {', '.join(unknown)}")
    return sorted(t for t in required if not enabled(t))


if __name__ == "__main__":
    import sys

    print("[rt-caps] this worker can actually do:")
    print(report())

    argv = sys.argv[1:]
    required: list[str] = []
    if "--require" in argv:
        rest = argv[argv.index("--require") + 1:]
        required = [t for chunk in rest for t in chunk.replace(",", " ").split() if t]

    if not required:
        sys.exit(0)

    try:
        off = missing_from(required)
    except ValueError as e:
        rt_obs.obs.caught("rt_capabilities.module", e)
        sys.exit(f"[rt-caps] {e}")

    if off:
        sys.exit(
            f"[rt-caps] REQUIRED capability missing: {', '.join(off)}\n"
            f"[rt-caps] this lane is expected to provide it and cannot. She will "
            f"tell callers she can't, politely and correctly, and nothing else "
            f"will look wrong — which is why this is checked here.")
    print(f"[rt-caps] all {len(required)} required capabilities present")
