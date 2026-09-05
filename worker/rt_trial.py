"""rt_trial.py — the seam that makes the trial lane a different agent.

WHY THIS FILE EXISTS. On 2026-09-03 the trial-pal lane was stood up with its own
box, number, Supabase project, LiveKit keys and pepper. Everything was separate
except the one thing a caller can hear: both boxes rsync the same worker/, so
the new number answered "Hello there, this is Phone-Pal, your voice companion."
The greeting is a literal string in agent.py and no environment variable changes
it. Separate infrastructure, same program.

So the difference has to be made in code, and this is where. Every function here
is inert unless AGENT_NAME names a trial lane, which is why dev and prod cannot
be altered by anything in this module: is_trial_lane() is false there and every
caller falls through to what it did before.

IT FAILS CLOSED, AND THAT IS THE POINT. kb/00-governance/ is pinned into the
system prompt precisely because a retrieval miss on a compliance rule is a
protocol deviation. If this lane cannot load that pinned block, it has no
governance, and an agent with no governance must not answer a trial
participant. governance_or_refuse() raises rather than degrading, and the
entrypoint is expected to end the call rather than improvise — the same rule
the bridge ledger and rt_carrier already follow.

WHAT IS NOT HERE YET. Retrieval of study facts, the escalate tool, verbatim
capture, and identity verification. Until those exist this agent can talk,
grounded in the pinned rules, and end the call. That is deliberately almost
nothing: kb/00-governance/00-charter-and-scope.md says "unsure whether to
answer or capture: capture. Unsure whether to capture or refuse: refuse", and
a lane that cannot capture is a lane that refuses.
"""
from __future__ import annotations

import contextlib
import os

import rt_obs

# ─────────────────────────────────────────────────────────────────────────────
# Which lanes are trial lanes
# ─────────────────────────────────────────────────────────────────────────────

# Matched against AGENT_NAME, which render-config.sh writes into worker.env from
# lane.env. RT_TRIAL_LANE=1 forces it on for a lane named something else; there
# is deliberately no way to force it OFF for a name in this tuple, because the
# failure that matters is a trial lane quietly running as the companion.
TRIAL_AGENT_NAMES: tuple[str, ...] = ("trial-pal", "trial-pal-dev", "trial-pal-prod")

_TRUTHY = {"1", "true", "yes", "on"}


def agent_name() -> str:
    return (os.getenv("AGENT_NAME") or "").strip()


def is_trial_lane() -> bool:
    """True only on a trial lane. Every other function here is a no-op unless this is."""
    if (os.getenv("RT_TRIAL_LANE") or "").strip().lower() in _TRUTHY:
        return True
    return agent_name() in TRIAL_AGENT_NAMES


def study() -> str | None:
    """The protocol this lane serves, e.g. NCT01960348. None means the whole store."""
    return (os.getenv("RT_TRIAL_STUDY") or "").strip() or None


# ─────────────────────────────────────────────────────────────────────────────
# Tools a trial agent must not be holding
# ─────────────────────────────────────────────────────────────────────────────

# Each entry names the governance file that forbids it, so the reason survives
# the next person reading this list and wondering why the agent is so bare.
# These are WITHHELD, not merely unmentioned: rt_capabilities' first lesson was
# that a tool the model can see, it will eventually call and then improvise
# around the failure.
FORBIDDEN_TOOLS: dict[str, str] = {
    # 03-grounding-and-refusal: "You may assert a study fact only if a retrieved
    # document supports it. Not general medical knowledge." A search engine is
    # exactly the general medical knowledge that rule excludes.
    "web_search": "03-grounding-and-refusal",
    # 11-escalation-routing: escalation goes to named destinations on a clock.
    # Patching a participant through to an arbitrary number is not that, and
    # 06-emergency-escalation wants emergency services dialled by the caller.
    "find_number": "11-escalation-routing",
    "bridge_call": "11-escalation-routing",
    "press_keys": "11-escalation-routing",
    "listen_only": "11-escalation-routing",
    "end_bridge": "11-escalation-routing",
    # 10-records-and-audit: "Never write into a case report form, never sign,
    # never use anyone's credentials." Email and calendar invites from the agent
    # are unattributed writing that reads as a record.
    "send_email": "10-records-and-audit",
    "send_calendar_invite": "10-records-and-audit",
    # 07-privacy-and-verification plus the charter: the companion's memory store
    # is free-form and keyed to a caller who has not been verified here. Study
    # capture is a structured record with a timestamp and a route, and it does
    # not exist yet — so nothing is stored at all rather than stored wrongly.
    "db_tool": "07-privacy-and-verification",
    "recall_earlier": "07-privacy-and-verification",
    "manage_goals": "07-privacy-and-verification",
    # Both are legitimate for a trial eventually — visit reminders are exactly
    # what a protocol concierge sends. Neither is credentialled on this lane
    # today, and turning them on means writing the trial versions first.
    "send_sms": "not-yet-written",
    "schedule_reminder_call": "not-yet-written",
}


def forbidden_tools() -> set[str]:
    """Tool names to withhold on a trial lane. Empty everywhere else."""
    return set(FORBIDDEN_TOOLS) if is_trial_lane() else set()


# ─────────────────────────────────────────────────────────────────────────────
# What the caller hears
# ─────────────────────────────────────────────────────────────────────────────

# 01-identity-disclosure-and-recording: "Say you are an automated assistant at
# the opening of every call, unprompted. Never claim to be a person, a nurse, or
# a named individual." So this is not a persona with a name — it opens by saying
# what it is. One line, because the rule is that the disclosure is unmissable,
# not that it is long.
_GREETING = (
    "Hello, this is an automated assistant calling on behalf of the research study team. "
    "I'm not a person, and I'm not a nurse. Before we go on, can I check who I'm speaking with?"
)


def greeting() -> str | None:
    """The opening line, or None on a lane that is not a trial lane."""
    return _GREETING if is_trial_lane() else None


def identity_line() -> str | None:
    """Replaces the companion identity at the top of the system prompt."""
    if not is_trial_lane():
        return None
    return (
        "You are an automated assistant for a clinical research study, speaking with a "
        "trial participant by telephone. You are not a person, not a nurse, and not a "
        "clinician, and you never imply otherwise. You act under the Investigator's "
        "delegation and it is narrower than what you are capable of."
    )


# ─────────────────────────────────────────────────────────────────────────────
# The pinned governance block
# ─────────────────────────────────────────────────────────────────────────────

_PROMPT_RPC = "rpc/rt_kb_prompt"


def pinned_governance() -> str | None:
    """The pinned governance block for this lane's study, from kb.prompt_build.

    Read through rt_prefs._req because that is where the Supabase auth headers,
    the bounded transient retry and the net.* telemetry already live; a second
    HTTP path here would be a second thing to get wrong.
    """
    if not is_trial_lane():
        return None
    try:
        import rt_prefs
        rows = rt_prefs._req("POST", _PROMPT_RPC, {"p_study": study()})
    except Exception as e:
        rt_obs.obs.caught("rt_trial.pinned_governance", e)
        return None
    if isinstance(rows, list):
        rows = rows[0] if rows else None
    if isinstance(rows, dict):
        text = rows.get("prompt_text") or rows.get("pinned") or rows.get("text")
    else:
        text = rows if isinstance(rows, str) else None
    text = (text or "").strip()
    return text or None


class TrialGovernanceUnavailable(RuntimeError):
    """Raised when a trial lane cannot load the rules it is required to follow."""


def governance_or_refuse() -> str:
    """The pinned block, or raise. Never returns an ungoverned prompt.

    A companion with a degraded prompt is a worse companion. A trial agent with
    a degraded prompt is an agent telling a participant something nobody
    approved, which is a protocol deviation and, if it touches risk or
    procedure, reportable. So this does not fall back.
    """
    text = pinned_governance()
    if not text:
        with contextlib.suppress(Exception):
            rt_obs.obs.warn("trial.governance_missing", agent=agent_name(), study=study() or "")
        raise TrialGovernanceUnavailable(
            f"{agent_name()} is a trial lane and kb.prompt_build returned no pinned "
            f"governance for study={study() or 'any'}. Run scripts/kb_load.py --apply "
            f"against this lane. Refusing to answer a participant without it.")
    with contextlib.suppress(Exception):
        rt_obs.obs.event("trial.governance_loaded", chars=len(text), study=study() or "")
    return text
