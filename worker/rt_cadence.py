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

STAGE_NORMAL: Final[str] = "normal"
STAGE_SOFT_WRAP: Final[str] = "soft_wrap"
STAGE_FIRM_WRAP: Final[str] = "firm_wrap"
STAGE_HARD_CAP: Final[str] = "hard_cap"


def get_cadence_thresholds() -> tuple[float, float, float]:
    """Return (soft_wrap_mins, firm_wrap_mins, hard_cap_mins) configured via environment."""
    try:
        soft = float(os.getenv("RT_CALL_SOFT_WRAP_MINS", str(DEFAULT_SOFT_WRAP_MINS)))
    except (ValueError, TypeError):
        soft = DEFAULT_SOFT_WRAP_MINS

    try:
        firm = float(os.getenv("RT_CALL_FIRM_WRAP_MINS", str(DEFAULT_FIRM_WRAP_MINS)))
    except (ValueError, TypeError):
        firm = DEFAULT_FIRM_WRAP_MINS

    try:
        hard = float(os.getenv("RT_CALL_HARD_CAP_MINS", str(DEFAULT_HARD_CAP_MINS)))
    except (ValueError, TypeError):
        hard = DEFAULT_HARD_CAP_MINS

    return soft, firm, hard


def evaluate_cadence_stage(
    elapsed_seconds: float,
    soft_mins: float | None = None,
    firm_mins: float | None = None,
    hard_mins: float | None = None,
) -> str:
    """Evaluate the cadence stage based on call elapsed seconds."""
    s_mins, f_mins, h_mins = get_cadence_thresholds()
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


def render_cadence_directive(stage: str, caller_name: str | None = None) -> str:
    """Render the dynamic system prompt directive injected when crossing a cadence threshold."""
    name = (caller_name or "the caller").strip()

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


def get_cadence_farewell_message(caller_name: str | None = None) -> str:
    """Return the spoken farewell message used if the hard ceiling is reached."""
    name = (caller_name or "").strip()
    if name and name.lower() not in ("friend", "caller", "unknown"):
        return f"Take wonderful care, {name} — let's catch up again soon! Bye bye."
    return "Take wonderful care — let's catch up again soon! Bye bye."
