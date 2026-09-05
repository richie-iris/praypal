"""test_rt_postcall_enhancements.py — Unit tests for emotional bridge, milestones, and mood hydration."""
from __future__ import annotations

import json
from rt_postcall_worker import validate_extracted
from rt_hydrator import _build_milestones_block, discover_and_hydrate_prompt


def test_validate_extracted_emotional_fields():
    raw = {
        "caller_name": "Arthur",
        "opening_bridge": "Ask how his granddaughter's piano recital in Boston went.",
        "emotional_tone": "Warm and nostalgic, looking forward to the holidays.",
        "milestones": [
            {"event": "Piano recital", "date_text": "this Sunday", "detail": "Granddaughter performing Chopin"},
            {"event": "Eye appointment", "date_text": "next Wednesday", "detail": "Annual checkup"},
        ],
    }
    transcript = "Caller: Hi, I'm Arthur. My granddaughter has a piano recital this Sunday.\nAgent: That is wonderful!"
    cleaned, clar = validate_extracted(raw, transcript)

    assert cleaned["caller_name"] == "Arthur"
    assert "piano recital in Boston" in cleaned["opening_bridge"]
    assert "Warm and nostalgic" in cleaned["emotional_tone"]
    assert len(cleaned["milestones"]) == 2
    assert cleaned["milestones"][0]["event"] == "Piano recital"


def test_build_milestones_block():
    schemas = [
        {
            "category": "milestones",
            "data_summary": json.dumps({
                "milestones": [
                    {"event": "Daughter's wedding", "date_text": "next Friday", "detail": "Denver trip"},
                    {"event": "Retirement party", "date_text": "in two weeks", "detail": "at the community center"},
                ]
            }),
        }
    ]
    block = _build_milestones_block(schemas)
    assert "# UPCOMING MILESTONES & OCCASIONS" in block
    assert "Daughter's wedding (next Friday): Denver trip" in block
    assert "Retirement party (in two weeks): at the community center" in block


def test_hydrate_prompt_with_opening_bridge_and_mood():
    prefetch_bundle = {
        "caller": {
            "display_name": "Arthur",
            "caller_rules": "",
        },
        "schemas": [
            {
                "category": "ours",
                "data_summary": json.dumps({
                    "opening_bridge": "Ask how the garden harvest turned out.",
                    "emotional_tone": "Excited about growing heirloom tomatoes.",
                }),
            },
            {
                "category": "milestones",
                "data_summary": json.dumps({
                    "milestones": [
                        {"event": "Garden show", "date_text": "Saturday", "detail": "County fair exhibit"},
                    ]
                }),
            },
        ],
        "reminders": [],
        "facts": [],
    }

    prompt, meta = discover_and_hydrate_prompt("+15551234567", prefetch_bundle=prefetch_bundle)

    assert "RECOMMENDED OPENING HOOK: \"Ask how the garden harvest turned out.\"" in prompt
    assert "# CALLER EMOTIONAL STATE & MOOD" in prompt
    assert "Excited about growing heirloom tomatoes." in prompt
    assert "# UPCOMING MILESTONES & OCCASIONS" in prompt
    assert "Garden show (Saturday): County fair exhibit" in prompt


def test_apply_memory_commands_prunes_schemas_and_reminders():
    from unittest.mock import patch
    from rt_postcall_worker import apply_memory_commands

    pre_schemas = [
        {"category": "family", "data_summary": json.dumps({"wife": "Vashti", "son": "Akash"})},
        {"category": "work", "data_summary": json.dumps({"colleague": "Matt", "task": "review email"})},
    ]
    pre_reminders = [
        {"reminder_text": "Review Matt's email tonight"},
        {"reminder_text": "Buy groceries"},
    ]
    pre_caller = {
        "loved_ones": "Wife Vashti, Son Akash, Son Arjun"
    }

    rpc_calls = []
    def mock_safe_rpc(name, params):
        rpc_calls.append((name, params))
        if name == "rt_get_facts":
            return [{"id": 101, "norm_key": "family:vashti", "value_text": "Wife Vashti"}]
        return True

    with patch("rt_postcall_worker._safe_rpc", side_effect=mock_safe_rpc):
        cmds = ["forget Vashti", "Matt's email is done"]
        executed = apply_memory_commands("fake_hash", cmds, pre_schemas, pre_reminders, pre_caller=pre_caller)

    assert len(executed) == 2
    # Verify reminder completed
    completed_rems = [p["p_text"] for n, p in rpc_calls if n == "rt_complete_reminder"]
    assert "Review Matt's email tonight" in completed_rems

    # Verify fact retired
    retired_fact_ids = [p["p_id"] for n, p in rpc_calls if n == "rt_retire_fact"]
    assert 101 in retired_fact_ids

    # Verify loved_ones updated without Vashti
    updated_lo = [p["p_loved_ones"] for n, p in rpc_calls if n == "rt_set_loved_ones"]
    assert len(updated_lo) > 0
    assert "Vashti" not in updated_lo[0]
    assert "Son Akash" in updated_lo[0]
