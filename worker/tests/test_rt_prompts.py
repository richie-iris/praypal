"""test_rt_prompts.py — Unit tests for prompt building, greetings, and turn deduplication."""
from __future__ import annotations

from rt_prompts import (
    _greeting_text,
    _greeted_note,
    _onboarding_block,
    is_duplicate_agent_turn,
    _fit_context_window,
)


def test_greeting_first_contact():
    # First contact (call_count == 0) should use brand and disclosure lines
    g0 = _greeting_text(None, None, call_count=0, seed=0)
    assert "Phone-Pal" in g0
    assert "companion" in g0


def test_greeting_unnamed_caller():
    # Caller on 2nd call without a saved name should be asked for their name
    g1 = _greeting_text(None, "Samantha", call_count=1)
    assert "name" in g1.lower()


def test_greeting_known_caller():
    # Known caller gets short, warm, reusable greeting with their name
    g_known = _greeting_text("Richie", "Iris", call_count=2)
    assert "?" in g_known
    assert "Richie" in g_known
    assert "What's your name?" not in g_known


def test_greeted_note():
    note = _greeted_note("Hello there!")
    assert "Hello there!" in note
    assert "Do not greet again" in note


def test_onboarding_block():
    block = _onboarding_block(["name", "intro"], alias="Iris", call_count=0)
    assert "STILL TO COME" in block
    assert "name" in block
    assert "Iris" in block

    empty_block = _onboarding_block([])
    assert empty_block == ""


def test_is_duplicate_agent_turn():
    now = 100.0
    prev_at = 95.0  # 5s ago

    # Exact match within window
    assert is_duplicate_agent_turn("Nelda, huh?", "Nelda, huh?", prev_at, now) is True

    # Truncated prefix within window
    assert is_duplicate_agent_turn(
        "Nelda, huh?",
        "Nelda, huh? I like it! I'll remember that for next time.",
        prev_at,
        now,
    ) is True

    # Outside window (> 12s)
    old_at = 80.0
    assert is_duplicate_agent_turn("Nelda, huh?", "Nelda, huh?", old_at, now) is False

    # Different text
    assert is_duplicate_agent_turn("Hello!", "How can I help you?", prev_at, now) is False


def test_fit_context_window():
    trigger, target, note = _fit_context_window(6000, 4000)
    assert trigger > target
    assert target >= 3000
