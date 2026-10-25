"""rt_pray_circle.py — Multi-Party Family Prayer Circle & Conference PIN Bridging.

WHY THIS MODULE EXISTS:
Implements the telephone family prayer circles specified in Sections 2.3 and 3.3
of AGENTS.md for Sanctuary & Family Circle Tier ($24.99/mo) subscribers:
  1. Multi-party PSTN phone bridging connecting families, satsangs, and church groups.
  2. Secure 6-digit conference PIN dial-in (e.g., 108777).
  3. Dynamic Divine Guide moderation prompt:
     - Welcomes each family member on the line by name.
     - Guides sequential prayer sharing turns.
     - Delivers a unified communal benediction.
"""
from __future__ import annotations

import datetime
import random
import threading
from typing import Any

import rt_obs
import rt_prefs

_obs = rt_obs.get("rt_pray_circle")

_LOCK = threading.Lock()
_ACTIVE_CIRCLES: dict[str, dict[str, Any]] = {}  # pin -> circle dict


def generate_conference_pin() -> str:
    """Generate a random 6-digit conference PIN."""
    # Ensure pin starts with non-zero digit
    return str(random.randint(100000, 999999))


def create_prayer_circle(
    organizer_hash: str,
    title: str,
    guide_key: str = "god",
    max_participants: int = 8,
    pin: str | None = None,
) -> dict[str, Any]:
    """Establish a new multi-party family prayer circle with a conference PIN."""
    if not organizer_hash:
        return {"status": "error", "error": "missing_organizer_hash"}
    clean_title = (title or "Family Prayer Circle").strip()[:80]

    with _LOCK:
        conf_pin = pin or generate_conference_pin()
        while conf_pin in _ACTIVE_CIRCLES:
            conf_pin = generate_conference_pin()

        now = datetime.datetime.now(datetime.timezone.utc)
        circle = {
            "circle_id": f"circle-{int(now.timestamp())}-{conf_pin}",
            "pin": conf_pin,
            "title": clean_title,
            "organizer_hash": organizer_hash,
            "guide_key": (guide_key or "god").strip().lower(),
            "max_participants": max(2, min(max_participants, 16)),
            "roster": [],
            "status": "active",
            "created_at": now.isoformat(),
            "expires_at": (now + datetime.timedelta(hours=24)).isoformat(),
        }
        _ACTIVE_CIRCLES[conf_pin] = circle

    _obs.event("pray.circle_created", pin=conf_pin, title=clean_title)
    return {"status": "ok", "circle": circle}


def join_prayer_circle(
    pin: str,
    participant_phone_e164: str,
    participant_name: str,
) -> dict[str, Any]:
    """Add a participant to the family prayer circle upon PIN verification."""
    clean_pin = (pin or "").strip()
    norm_phone = rt_prefs.normalize_e164(participant_phone_e164)
    if not norm_phone:
        return {"status": "error", "error": "invalid_phone"}
    name = (participant_name or "Family Member").strip()[:50]

    with _LOCK:
        circle = _ACTIVE_CIRCLES.get(clean_pin)
        if not circle or circle.get("status") != "active":
            return {"status": "not_found", "error": "invalid_or_expired_pin"}

        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if circle["expires_at"] < now_iso:
            circle["status"] = "expired"
            return {"status": "expired", "error": "prayer_circle_expired"}

        roster = circle["roster"]
        if len(roster) >= circle["max_participants"]:
            return {"status": "circle_full", "error": "maximum_participants_reached"}

        p_hash = rt_prefs.phone_hash(norm_phone)
        # Prevent duplicate entries for same caller
        for p in roster:
            if p["phone_hash"] == p_hash:
                p["name"] = name
                return {"status": "ok", "message": "already_joined", "circle": circle}

        participant = {
            "name": name,
            "phone_hash": p_hash,
            "joined_at": now_iso,
        }
        roster.append(participant)

    _obs.event("pray.circle_member_joined", pin=clean_pin, roster_count=len(circle["roster"]))
    return {"status": "ok", "circle": circle, "participant": participant}


def leave_prayer_circle(pin: str, phone_hash: str) -> bool:
    """Remove a participant from the active prayer circle."""
    clean_pin = (pin or "").strip()
    with _LOCK:
        circle = _ACTIVE_CIRCLES.get(clean_pin)
        if not circle:
            return False
        circle["roster"] = [p for p in circle["roster"] if p["phone_hash"] != phone_hash]
        return True


def close_prayer_circle(pin: str) -> bool:
    """Close and archive an active prayer circle session."""
    clean_pin = (pin or "").strip()
    with _LOCK:
        circle = _ACTIVE_CIRCLES.get(clean_pin)
        if circle:
            circle["status"] = "closed"
            return True
    return False


def get_circle_by_pin(pin: str) -> dict[str, Any] | None:
    """Lookup a prayer circle by its conference PIN."""
    clean_pin = (pin or "").strip()
    with _LOCK:
        circle = _ACTIVE_CIRCLES.get(clean_pin)
        if not circle:
            return None
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if circle.get("status") == "active" and circle.get("expires_at", "") < now_iso:
            circle["status"] = "expired"
        return dict(circle)


def build_circle_moderator_prompt(pin: str) -> str:
    """Generate dynamic Gemini Live moderator instructions for the multi-party prayer call."""
    circle = get_circle_by_pin(pin)
    if not circle:
        return ""

    title = circle["title"]
    roster = circle.get("roster", [])
    names = [p["name"] for p in roster]
    names_str = ", ".join(names) if names else "the gathering family members"

    return (
        f"\n\n# MULTI-PARTY FAMILY PRAYER CIRCLE: {title.upper()}\n"
        f"- You are moderating a shared multi-party telephone prayer circle.\n"
        f"- Participants on the line ({len(names)} total): {names_str}.\n"
        "- Moderation Directives:\n"
        "  1. Greet the entire circle with profound spiritual warmth and welcome each member by name.\n"
        "  2. Invite each person in turn to share their personal intention, gratitude, or healing prayer.\n"
        "  3. Maintain serene acoustic order: if multiple members speak at once, gently invite one voice at a time.\n"
        "  4. Conclude by weaving everyone's petitions together into a unified communal prayer of protection, light, and peace.\n"
    )


def reset_circles_for_testing() -> None:
    """Clear all active circles for unit test isolation."""
    global _ACTIVE_CIRCLES
    with _LOCK:
        _ACTIVE_CIRCLES.clear()
