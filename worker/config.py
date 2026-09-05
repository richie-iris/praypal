"""config.py — which .env this worker loads, and whether it can serve a call.

This used to carry a frozen `Settings` dataclass mirroring every environment
variable, a loader, and a field-by-field validator. Nothing in production ever
read it: every module calls os.getenv at the point of use, and agent.py imports
this module only to print the boot banner. The dataclass existed to be asserted
on by 642 lines of its own tests, which is not a reason for it to exist.

Two things are genuinely needed here — load the .env that ENV_MODE selects, and
say plainly at boot which credentials are missing.
"""
from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

env_mode = os.getenv("ENV_MODE", "").strip().lower()
if env_mode == "dev":
    load_dotenv(".env.dev")
elif env_mode == "test":
    load_dotenv(".env.test")
load_dotenv(".env.local")
load_dotenv(".env")


REQUIRED = (
    "LIVEKIT_URL",
    "LIVEKIT_API_KEY",
    "LIVEKIT_API_SECRET",
    "GOOGLE_API_KEY",
    "SUPABASE_URL",
    "SUPABASE_SERVICE_ROLE_KEY",
)

# Agent names that mean "this worker answers the real line". Shared by the boot guard
# (agent.py) and the SQL/deploy tooling so nobody re-types the list and drifts.
PROD_AGENT_NAMES = ("iris-phone", "phone-pal-prod")


# A pepper value that is really a template comment, a placeholder or a stub must count as
# MISSING: an HMAC keyed with "# REQUIRED" or "changeme" is as reversible as no HMAC at all,
# and the deploy that shipped it would otherwise pass /ready. Shared contract — rt_prefs and
# the reset script import pepper_ok() so every lane judges the same way.
PEPPER_MIN_LEN = 16
_PEPPER_PLACEHOLDERS = ("replace", "example", "changeme", "todo", "xxxx")
# Fail closed: only these spellings switch the pepper requirement OFF. "on", "maybe", "y",
# a typo — anything else — means required, so a mangled flag can never unlock unpeppered hashing.
_PEPPER_NOT_REQUIRED = frozenset({"", "0", "false", "no", "off"})


def pepper_problem(value: str | None) -> str | None:
    """Why `value` is not a usable pepper, or None when it is.

    Reasons: "empty", "starts with #" (a template comment read as the value), "too short"
    (< PEPPER_MIN_LEN chars), "placeholder" (contains a known stub word, any case).
    """
    v = (value or "").strip()
    if not v:
        return "empty"
    if v.startswith("#"):
        return "starts with #"
    if len(v) < PEPPER_MIN_LEN:
        return f"too short (< {PEPPER_MIN_LEN} chars)"
    low = v.lower()
    for mark in _PEPPER_PLACEHOLDERS:
        if mark in low:
            return f"placeholder (contains {mark!r})"
    return None


def pepper_ok(value: str | None) -> bool:
    """True iff `value` can key the phone HMAC: non-empty, not a comment, >= 16 chars, no stub."""
    return pepper_problem(value) is None


def pepper_required() -> bool:
    """RT_REQUIRE_PEPPER gate, fail-closed: only "", "0", "false", "no", "off" mean NOT required."""
    return os.getenv("RT_REQUIRE_PEPPER", "").strip().lower() not in _PEPPER_NOT_REQUIRED


def missing_config() -> list[str]:
    """The required environment variables that are absent, empty or (for the pepper) unusable.

    The pepper joins the list only when the lane demands it: an unpeppered dev box is
    fine, an unpeppered deploy lane cannot hash a phone (rt_prefs refuses), so /ready
    and the boot banner must say so instead of the first call finding out. A pepper that
    is set but unusable (see pepper_problem) is reported as missing for the same reason.
    """
    missing = [name for name in REQUIRED if not (os.getenv(name) or "").strip()]
    if pepper_required() and not pepper_ok(os.getenv("RT_PHONE_HASH_PEPPER")):
        missing.append("RT_PHONE_HASH_PEPPER")
    return missing


def _describe_missing(name: str) -> str:
    """Boot-banner line for one missing entry; the pepper says WHICH condition failed."""
    if name == "RT_PHONE_HASH_PEPPER":
        reason = pepper_problem(os.getenv("RT_PHONE_HASH_PEPPER")) or "unusable"
        return f"{name} is missing ({reason})"
    return f"{name} is missing"


def validate_startup_config() -> None:
    """Say at boot whether this worker can actually serve a call.

    Deliberately does not exit. A worker missing one credential is degraded, not
    useless, and turning that into a hard exit turns a partial misconfiguration
    into a fleet outage — every box refusing to boot at once, with nothing
    answering the line.
    """
    errors = missing_config()
    if errors:
        print("[config] ❌ STARTUP CONFIGURATION ERRORS — this worker will take "
              "calls it cannot serve:\n  - "
              + "\n  - ".join(_describe_missing(name) for name in errors),
              file=sys.stderr)
    else:
        print("[config] ✅ configuration validated for number="
              f"{os.getenv('RT_PUBLIC_NUMBER') or '(unset)'}", flush=True)


if __name__ == "__main__":
    validate_startup_config()
