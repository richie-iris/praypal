"""test_rt_pray.py — Test suite for PrayPal agent seam, crisis shield, and pantheon."""
from __future__ import annotations

import os
from unittest.mock import patch

import rt_pray


def test_is_pray_lane():
    # 1. On default / phone-pal lane -> False
    with patch.dict(os.environ, {"AGENT_NAME": "phone-pal-prod", "RT_PRAY_LANE": "0"}):
        assert not rt_pray.is_pray_lane()

    # 2. On trial-pal lane -> False
    with patch.dict(os.environ, {"AGENT_NAME": "trial-pal", "RT_PRAY_LANE": "0"}):
        assert not rt_pray.is_pray_lane()

    # 3. On pray-pal lane by name -> True
    with patch.dict(os.environ, {"AGENT_NAME": "pray-pal"}):
        assert rt_pray.is_pray_lane()

    # 4. On pray-pal lane by env switch -> True
    with patch.dict(os.environ, {"AGENT_NAME": "custom-agent", "RT_PRAY_LANE": "1"}):
        assert rt_pray.is_pray_lane()


def test_crisis_intercept_safety():
    # Safe text
    is_crisis, text = rt_pray.check_crisis("I would like to pray for my family today.")
    assert not is_crisis
    assert text is None

    # Crisis trigger
    is_crisis, text = rt_pray.check_crisis("I can't take this anymore, I want to kill myself.")
    assert is_crisis
    assert "988" in text
    assert "Suicide & Crisis Lifeline" in text


def test_pantheon_guides():
    expected_guides = ["god", "jesus", "shiva", "krishna", "moses", "noah", "mother", "syncretic"]
    for key in expected_guides:
        guide = rt_pray.get_guide(key)
        assert guide["title"]
        assert guide["voice"] in ("Alnilam", "Algieba", "Algenib", "Aoede", "Achernar")
        assert guide["greeting"]
        assert guide["cadence"]


def test_syncretic_jesus_shiva_advice():
    advice = rt_pray.syncretic_jesus_shiva_advice("forgiving my brother")
    assert "Jesus" in advice
    assert "Shiva" in advice
    assert "stillness" in advice.lower()
    assert "grace" in advice.lower()


def test_filter_tools():
    class DummyTool:
        def __init__(self, name):
            self.__name__ = name

    tools = [
        DummyTool("db_tool"),
        DummyTool("send_sms"),
        DummyTool("bridge_call"),
        DummyTool("find_number"),
    ]

    # On phone-pal: tools preserved
    with patch.dict(os.environ, {"AGENT_NAME": "phone-pal", "RT_PRAY_LANE": "0"}):
        assert len(rt_pray.filter_tools(tools)) == 4

    # On pray-pal: bridge_call and find_number withheld
    with patch.dict(os.environ, {"AGENT_NAME": "pray-pal"}):
        filtered = rt_pray.filter_tools(tools)
        assert len(filtered) == 2
        names = [t.__name__ for t in filtered]
        assert "bridge_call" not in names
        assert "find_number" not in names
        assert "db_tool" in names
        assert "send_sms" in names


def test_forbidden_tools_and_greeting():
    with patch.dict(os.environ, {"AGENT_NAME": "phone-pal", "RT_PRAY_LANE": "0"}):
        assert rt_pray.forbidden_tools() == set()
        assert rt_pray.greeting() is None

    with patch.dict(os.environ, {"AGENT_NAME": "pray-pal"}):
        assert "bridge_call" in rt_pray.forbidden_tools()
        assert "find_number" in rt_pray.forbidden_tools()
        greet = rt_pray.greeting("jesus")
        assert "Peace be with you" in greet

    # Verify agent.py greeting interception
    import agent
    with patch.dict(os.environ, {"AGENT_NAME": "pray-pal"}):
        greet = agent._greeting_text("John", "iris", call_count=0)
        assert "Peace be with you" in greet
        # Verify instructions
        prompt, name = agent._build_instructions()
        assert name == "the seeker"
        assert "PrayPal" in prompt
