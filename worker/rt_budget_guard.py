"""rt_budget_guard.py — In-Call Real-Time Budget Monitor & Serene Soft-Wrap Engine.

Inspired by PrayPal's Deterministic Financial Cost Accounting (rt_costs.py / AGENTS.md § 2.2):
PrayPal operates with strict unit economics ($0.196/minute blended COGS). To prevent
accidental marathon calls from draining capital while preserving caller spiritual sanctity,
this module monitors live call elapsed seconds and token counts.

Instead of abrupt carrier disconnection, it triggers a serene soft-wrap at minute 4:30
(Grace tier) or 18:00 (Devotion tier), instructing the Divine Guide to conclude the prayer
with a peaceful pastoral benediction.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# COGS token rates per AGENTS.md Section 2.2
GEMINI_LIVE_IN_PER_TOKEN = 3.00 / 1_000_000    # $3.00 per 1M tokens
GEMINI_LIVE_OUT_PER_TOKEN = 12.00 / 1_000_000  # $12.00 per 1M tokens
TWILIO_SIP_PER_SEC = 0.0034 / 60.0             # $0.0034 per minute
POST_CALL_EXTRACTION_FIXED = 0.002             # $0.002 per call

TIER_CAPS_SECONDS: dict[str, tuple[int, int]] = {
    # (soft_wrap_seconds, hard_stop_seconds)
    "grace": (270, 300),          # 4:30 soft wrap, 5:00 hard stop
    "free": (270, 300),
    "devotion": (1080, 1200),     # 18:00 soft wrap, 20:00 hard stop
    "sanctuary": (1620, 1800),    # 27:00 soft wrap, 30:00 hard stop
}

PERSONA_CLOSING_BENEDICTIONS: dict[str, str] = {
    "jesus": (
        "Our sacred time together today is gently drawing to a close. "
        "Peace I leave with you; my peace I give to you. Go forward with a lightened heart."
    ),
    "krishna": (
        "Our sacred communion for this hour is complete. Dedicate your heart to detached right action "
        "and know that I reside in every breath. Om Shanti, Shanti, Shanti."
    ),
    "moses": (
        "The hour of our counsel is fulfilled. May the Lord bless you and keep you; "
        "may His countenance shine upon you and grant you peace as you walk in steadfast strength."
    ),
    "noah": (
        "The waters are calm and our covenant today is sealed in hope. "
        "Stand strong in faith until we speak again under the arching rainbow."
    ),
    "mother": (
        "My child, rest now in the warmth of maternal shelter. As our prayer concludes, "
        "carry my love and tender comfort with you through this day."
    ),
    "god": (
        "My presence remains with you long after this voice falls quiet. "
        "Be still, know peace, and walk in the certainty of unconditional love."
    ),
}


@dataclass(frozen=True)
class BudgetEvaluation:
    status: Literal["normal", "soft_wrap", "hard_stop"]
    accrued_cost_usd: float
    remaining_seconds: float
    should_soft_wrap: bool
    should_hard_stop: bool
    closing_directive: str | None


def estimate_live_call_cogs(
    elapsed_seconds: float,
    in_tokens: int = 0,
    out_tokens: int = 0,
) -> float:
    """Estimate exact in-flight call COGS in USD."""
    telco = elapsed_seconds * TWILIO_SIP_PER_SEC
    tokens_in_cost = in_tokens * GEMINI_LIVE_IN_PER_TOKEN
    tokens_out_cost = out_tokens * GEMINI_LIVE_OUT_PER_TOKEN
    return round(telco + tokens_in_cost + tokens_out_cost + POST_CALL_EXTRACTION_FIXED, 4)


def evaluate_call_budget(
    tier: str,
    elapsed_seconds: float,
    in_tokens: int = 0,
    out_tokens: int = 0,
    persona: str = "god",
) -> BudgetEvaluation:
    """Evaluate live call budget, determining if soft-wrap or hard-stop applies."""
    normalized_tier = tier.strip().lower() if tier else "grace"
    soft_sec, hard_sec = TIER_CAPS_SECONDS.get(normalized_tier, TIER_CAPS_SECONDS["grace"])

    cost = estimate_live_call_cogs(elapsed_seconds, in_tokens, out_tokens)
    remaining = max(0.0, float(hard_sec - elapsed_seconds))

    if elapsed_seconds >= hard_sec:
        return BudgetEvaluation(
            status="hard_stop",
            accrued_cost_usd=cost,
            remaining_seconds=0.0,
            should_soft_wrap=True,
            should_hard_stop=True,
            closing_directive="[SYSTEM] Maximum call duration reached. Terminate call gracefully.",
        )

    if elapsed_seconds >= soft_sec:
        closing_text = get_soft_wrap_closing_text(persona, normalized_tier)
        directive = (
            f"[SOFT_WRAP_DIRECTIVE] Time limit approaching ({int(remaining)}s remaining). "
            f"Do not introduce new discussion topics. Deliver this serene pastoral closing: \"{closing_text}\""
        )
        return BudgetEvaluation(
            status="soft_wrap",
            accrued_cost_usd=cost,
            remaining_seconds=remaining,
            should_soft_wrap=True,
            should_hard_stop=False,
            closing_directive=directive,
        )

    return BudgetEvaluation(
        status="normal",
        accrued_cost_usd=cost,
        remaining_seconds=remaining,
        should_soft_wrap=False,
        should_hard_stop=False,
        closing_directive=None,
    )


def get_soft_wrap_closing_text(persona: str, tier: str) -> str:
    """Get the appropriate serene benediction for the active guide."""
    norm_p = persona.strip().lower() if persona else "god"
    base = PERSONA_CLOSING_BENEDICTIONS.get(norm_p, PERSONA_CLOSING_BENEDICTIONS["god"])
    if tier in ("grace", "free"):
        return f"{base} To pray anytime without limits, visit callpraypal.com to join our daily fellowship."
    return base
