"""test_rt_onboarding.py — Unit tests for the 4-step relationship onboarding funnel."""
from __future__ import annotations

import json
from rt_postcall_worker import update_onboarding
from rt_prompts import _onboarding_block


def test_onboarding_step_progression():
    from unittest.mock import patch

    with patch("rt_prefs._req", return_value=True):
        # Call 1: Unnamed caller, intro spoken
        transcript_1 = """
        Agent: Hello! I'm your companion, a friendly voice you can call anytime day or night.
        Caller: Hello there, thanks for picking up.
        """
        pre_caller = {"display_name": "Friend", "loved_ones": ""}
        pre_schemas = []

        res_1 = update_onboarding("hash123", transcript_1, pre_caller, pre_schemas)
        assert res_1.get("intro") is True
        assert res_1.get("name") is None
        assert res_1.get("done") is False

        # Call 2: Caller shares their name and a hobby fact
        transcript_2 = """
        Caller: My name is Arthur and I love gardening.
        Agent: Wonderful to meet you Arthur!
        """
        extracted_2 = {"caller_name": "Arthur"}
        pre_schemas_2 = [
            {"category": "onboarding", "data_summary": json.dumps(res_1)},
            {"category": "hobbies", "data_summary": json.dumps({"gardening": "loves heirloom tomatoes"})},
        ]

        res_2 = update_onboarding("hash123", transcript_2, pre_caller, pre_schemas_2, extracted=extracted_2)
        assert res_2.get("intro") is True
        assert res_2.get("name") is True
        assert res_2.get("first_fact") is True
        assert res_2.get("done") is False

        # Call 3: Companion explains ownership/rules
        transcript_3 = """
        Agent: Remember you can always change my name or set rules for how I speak, this space belongs to you.
        Caller: That's great to know.
        """
        pre_schemas_3 = [
            {"category": "onboarding", "data_summary": json.dumps(res_2)},
            {"category": "hobbies", "data_summary": json.dumps({"gardening": "loves heirloom tomatoes"})},
        ]

        res_3 = update_onboarding("hash123", transcript_3, {"display_name": "Arthur"}, pre_schemas_3)
        assert res_3.get("ownership") is True
        assert res_3.get("done") is True


def test_onboarding_block_rendering():
    missing = ["name", "ownership"]
    block = _onboarding_block(missing, alias="Clara", call_count=1)

    assert "# STILL TO COME" in block
    assert "their name" in block
    assert "db_tool" in block
    assert "set rules for you" in block
