"""test_rt_pray.py — Test suite for PrayPal agent seam, crisis shield, pantheon, and isolated memory."""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

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


def test_pray_db_rpcs():
    with patch.object(rt_pray, "_rpc") as mock_rpc:
        mock_rpc.return_value = {"caller": {"display_name": "Mary"}, "memories": [], "intentions": []}
        bundle = rt_pray.get_bundle("hash123")
        assert bundle["caller"]["display_name"] == "Mary"
        mock_rpc.assert_called_with("rt_pray_get_bundle", {"p_hash": "hash123"})

        mock_rpc.return_value = {"phone_hash": "hash123"}
        caller = rt_pray.get_caller("hash123")
        assert caller["phone_hash"] == "hash123"

        mock_rpc.return_value = {"phone_hash": "hash123", "active_guide": "jesus"}
        upserted = rt_pray.upsert_caller("hash123", display_name="John", active_guide="jesus")
        assert upserted["active_guide"] == "jesus"

        mock_rpc.return_value = 42
        mem_id = rt_pray.add_memory("hash123", "loved_one", "Pray for sister", subject="Sister")
        assert mem_id == 42

        mock_rpc.return_value = 101
        int_id = rt_pray.add_intention("hash123", "Guidance on new career", tradition="christian")
        assert int_id == 101

        mock_rpc.return_value = True
        assert rt_pray.forget_caller("hash123") is True


def test_build_system_prompt_with_memory():
    caller_info = {
        "display_name": "Samuel",
        "preferred_name_for_god": "Father",
    }
    memories = [
        {"category": "loved_one", "subject_name": "Hannah", "content": "Praying for Hannah's recovery"},
        {"category": "confession", "subject_name": None, "content": "Struggled with anger at work"},
    ]
    intentions = [
        {"intention_text": "Peace in the home", "is_answered": False},
        {"intention_text": "Safe journey to Jerusalem", "is_answered": True},
    ]

    prompt = rt_pray.build_system_prompt(
        guide_key="jesus",
        caller_tradition="christian",
        caller_info=caller_info,
        memories=memories,
        intentions=intentions,
    )

    assert "Samuel" in prompt
    assert "Father" in prompt
    assert "Hannah's recovery" in prompt
    assert "Struggled with anger" in prompt
    assert "Peace in the home" in prompt
    assert "[ANSWERED]" in prompt
    assert "The Good Shepherd" in prompt


def test_build_sms_prompt():
    prompt = rt_pray.build_sms_prompt(
        guide_key="shiva",
        tradition="hindu",
        caller_name="Arjun",
        memories=[{"content": "Seeking courage"}],
        intentions=[{"intention_text": "Strength in meditation"}],
        thread_context="Arjun: Om Namah Shivaya",
    )
    assert "PrayPal" in prompt
    assert "Arjun" in prompt
    assert "Lord Shiva" in prompt
    assert "hindu" in prompt
    assert "Seeking courage" in prompt
    assert "Strength in meditation" in prompt


def test_process_pray_postcall():
    transcript = """
Agent: Peace be with you. What is on your heart today?
Caller: My name is David. I've been feeling so burdened with guilt about my brother Aaron. Please pray that we reconcile.
Agent: Peace be upon you David. Forgiveness is always at hand.
"""
    mock_extracted = {
        "caller_name": "David",
        "active_guide": "god",
        "spiritual_tradition": "jewish",
        "preferred_name_for_god": "Hashem",
        "memories": [
            {"category": "confession", "subject": "brother Aaron", "content": "Guilt about dispute with Aaron", "vocab": "reconciliation"},
            {"category": "loved_one", "subject": "Aaron", "content": "Brother Aaron", "vocab": None},
        ],
        "intentions": [
            {"text": "Reconcile with brother Aaron", "tradition": "jewish", "circle": False}
        ]
    }

    with patch.dict(os.environ, {"GOOGLE_API_KEY": "test_key"}), \
         patch.object(rt_pray, "_call_gemini_json", return_value=mock_extracted), \
         patch.object(rt_pray, "upsert_caller") as mock_upsert, \
         patch.object(rt_pray, "add_memory") as mock_add_mem, \
         patch.object(rt_pray, "add_intention") as mock_add_int:

        result = rt_pray.process_pray_postcall("hash_david", transcript)

        assert result["status"] == "ok"
        assert result["caller_name"] == "David"
        assert result["memories_saved"] == 2
        assert result["intentions_saved"] == 1

        mock_upsert.assert_called_once_with(
            phone_hash="hash_david",
            display_name="David",
            active_guide="god",
            tradition="jewish",
            name_for_god="Hashem",
        )
        assert mock_add_mem.call_count == 2
        assert mock_add_int.call_count == 1


def test_postcall_diversion_to_pray():
    import rt_postcall_worker

    transcript = "Caller: Hello\nAgent: Peace be with you."
    with patch.dict(os.environ, {"AGENT_NAME": "pray-pal"}), \
         patch.object(rt_pray, "process_pray_postcall", return_value={"status": "ok", "diverted": True}) as mock_pray_postcall:

        res = rt_postcall_worker.process_post_call_transcript("+18005550199", transcript)
        assert res.get("diverted") is True
        mock_pray_postcall.assert_called_once()
