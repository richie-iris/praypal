"""rt_cadence.py — Call duration & conversational cadence lifecycle manager.

Manages the 3-tier natural friendship cadence for phone calls:
  Tier 1 (0 – 15m): Sweet spot / daily check-in (unrushed, lean context).
  Tier 2 (20 – 25m): Long-call gentle pivot (naturally steer toward warm wrap-up).
  Tier 3 (30 – 35m): Marathon ceiling & firm sign-off (loving, definitive farewell).
"""

from __future__ import annotations

import os
from typing import Final

# Default cadence thresholds in minutes
DEFAULT_SOFT_WRAP_MINS: Final[float] = 20.0   # Soft wrap: steer towards natural conclusion
DEFAULT_FIRM_WRAP_MINS: Final[float] = 30.0   # Firm wrap: warm, definitive sign-off
DEFAULT_HARD_CAP_MINS: Final[float] = 35.0    # Hard cap: respectful automatic disconnection

# PrayPal Grace Tier default cadence thresholds in minutes (Section 2.3 of AGENTS.md)
PRAY_DEFAULT_SOFT_WRAP_MINS: Final[float] = 5.0    # Soft wrap: steer toward prayer benediction
PRAY_DEFAULT_FIRM_WRAP_MINS: Final[float] = 7.0    # Firm wrap: warm final blessing
PRAY_DEFAULT_HARD_CAP_MINS: Final[float] = 10.0    # Hard cap: disconnect after benediction

STAGE_NORMAL: Final[str] = "normal"
STAGE_SOFT_WRAP: Final[str] = "soft_wrap"
STAGE_FIRM_WRAP: Final[str] = "firm_wrap"
STAGE_HARD_CAP: Final[str] = "hard_cap"


def is_pray_lane() -> bool:
    """True when running in the PrayPal spiritual telephony lane."""
    try:
        import rt_pray
        return rt_pray.is_pray_lane()
    except Exception:
        return os.getenv("AGENT_NAME", "").strip().lower() in ("pray-pal", "praypal") or os.getenv("RT_PRAY_LANE") == "1"


def get_cadence_thresholds(is_pray: bool | None = None) -> tuple[float, float, float]:
    """Return (soft_wrap_mins, firm_wrap_mins, hard_cap_mins) configured via environment.

    On PrayPal lanes, defaults to Grace Tier limits (5m soft, 7m firm, 10m hard)
    to protect the working capital reserve from marathon calls ($0.196/min COGS).
    """
    pray = is_pray_lane() if is_pray is None else is_pray
    default_soft = PRAY_DEFAULT_SOFT_WRAP_MINS if pray else DEFAULT_SOFT_WRAP_MINS
    default_firm = PRAY_DEFAULT_FIRM_WRAP_MINS if pray else DEFAULT_FIRM_WRAP_MINS
    default_hard = PRAY_DEFAULT_HARD_CAP_MINS if pray else DEFAULT_HARD_CAP_MINS

    try:
        soft = float(os.getenv("RT_CALL_SOFT_WRAP_MINS", str(default_soft)))
    except (ValueError, TypeError):
        soft = default_soft

    try:
        firm = float(os.getenv("RT_CALL_FIRM_WRAP_MINS", str(default_firm)))
    except (ValueError, TypeError):
        firm = default_firm

    try:
        hard = float(os.getenv("RT_CALL_HARD_CAP_MINS", str(default_hard)))
    except (ValueError, TypeError):
        hard = default_hard

    return soft, firm, hard


def evaluate_cadence_stage(
    elapsed_seconds: float,
    soft_mins: float | None = None,
    firm_mins: float | None = None,
    hard_mins: float | None = None,
    is_pray: bool | None = None,
) -> str:
    """Evaluate the cadence stage based on call elapsed seconds."""
    s_mins, f_mins, h_mins = get_cadence_thresholds(is_pray=is_pray)
    if soft_mins is not None:
        s_mins = soft_mins
    if firm_mins is not None:
        f_mins = firm_mins
    if hard_mins is not None:
        h_mins = hard_mins

    elapsed_mins = max(0.0, elapsed_seconds) / 60.0

    if elapsed_mins >= h_mins:
        return STAGE_HARD_CAP
    elif elapsed_mins >= f_mins:
        return STAGE_FIRM_WRAP
    elif elapsed_mins >= s_mins:
        return STAGE_SOFT_WRAP
    return STAGE_NORMAL


def render_cadence_directive(stage: str, caller_name: str | None = None, is_pray: bool | None = None) -> str:
    """Render the dynamic system prompt directive injected when crossing a cadence threshold."""
    pray = is_pray_lane() if is_pray is None else is_pray
    name = (caller_name or ("the seeker" if pray else "the caller")).strip()

    if pray:
        if stage == STAGE_SOFT_WRAP:
            return (
                "\n\n# CONVERSATIONAL CADENCE (5-MINUTE PRAYER WRAP-UP)\n"
                f"- You have been praying and reflecting warmly with {name} for about 5 minutes.\n"
                "- Over your next turn, offer a gentle final benediction or prayer blessing, letting them know they remain held in sacred grace.\n"
                "- Gently conclude the devotion without abruptly hanging up.\n"
            )
        elif stage == STAGE_FIRM_WRAP:
            return (
                "\n\n# CONVERSATIONAL CADENCE (7-MINUTE PRAYER CEILING)\n"
                f"- The prayer call has reached 7 minutes. You MUST offer your definitive benediction and warm spiritual farewell now.\n"
                f"- Speak your parting blessing and invoke peace upon {name}.\n"
                "- Call end_call() right after delivering your blessing.\n"
            )
        return ""

    if stage == STAGE_SOFT_WRAP:
        return (
            "\n\n# CONVERSATIONAL CADENCE (20-MINUTE CHECKPOINT)\n"
            f"- You have been chatting warmly with {name} for about 20 minutes. This has been a complete success.\n"
            "- Over your next 1-2 turns, naturally steer the conversation toward a warm close or forward-looking recap.\n"
            "- Do NOT abruptly cut them off or sound like a robot. Use natural friend phrasing:\n"
            "  e.g. 'I'm so glad we got to catch up on this today! Before I let you get back to your afternoon, did you want me to note down anything else?'\n"
            "- If they share a new detail, acknowledge it with warmth and then gently close.\n"
        )
    elif stage == STAGE_FIRM_WRAP:
        return (
            "\n\n# CONVERSATIONAL CADENCE (30-MINUTE CEILING)\n"
            f"- The call has reached 30 minutes. You MUST warmly and definitively say goodbye on your very next turn.\n"
            "- Do NOT introduce new questions or start new topics. Firmly and affectionately conclude:\n"
            f"  e.g. '{name}, I’ve loved talking with you today, but I’ve got to let you go so you can enjoy the rest of your day! Let’s pick right back up next time, okay?'\n"
            "- Call end_call() right after delivering your warm farewell.\n"
        )
    return ""


def get_cadence_farewell_message(caller_name: str | None = None, is_pray: bool | None = None) -> str:
    """Return the spoken farewell message used if the hard ceiling is reached."""
    pray = is_pray_lane() if is_pray is None else is_pray
    name = (caller_name or "").strip()
    if pray:
        if name and name.lower() not in ("friend", "caller", "unknown", "seeker"):
            return f"May divine peace, grace, and light be with you always, {name}. Amen. Goodbye."
        return "May divine peace, grace, and light be with you always. Amen. Goodbye."

    if name and name.lower() not in ("friend", "caller", "unknown"):
        return f"Take wonderful care, {name} — let's catch up again soon! Bye bye."
    return "Take wonderful care — let's catch up again soon! Bye bye."
