"""test_rt_goals.py — Unit tests for goal extraction, hydration, and lifecycle tracking."""
from __future__ import annotations

import json
import rt_goals
import rt_facts


def test_goal_creation_and_dict():
    g = rt_goals.Goal(
        id="g_123",
        title="Complete 10k run",
        category="fitness",
        target_date="2026-10-15",
        milestones=["Ran 3 miles today"],
        notes="Pacing at 9min/mile",
    )
    d = g.to_dict()
    assert d["id"] == "g_123"
    assert d["title"] == "Complete 10k run"
    assert d["status"] == "active"
    assert "Ran 3 miles today" in d["milestones"]

    g2 = rt_goals.Goal.from_dict(d)
    assert g2.id == g.id
    assert g2.title == g.title
    assert g2.target_date == g.target_date


def test_extract_goals_from_bundle():
    bundle = {
        "schemas": [
            {
                "category": "goals",
                "data_summary": json.dumps({
                    "g_1": {
                        "id": "g_1",
                        "title": "Launch new podcast",
                        "status": "active",
                        "target_date": "Next month",
                        "milestones": ["Recorded episode 1"],
                    },
                    "g_2": {
                        "id": "g_2",
                        "title": "Learn Italian",
                        "status": "completed",
                    }
                })
            }
        ]
    }
    goals = rt_goals.extract_goals_from_bundle(bundle)
    assert len(goals) == 2
    active = [g for g in goals if g.status == "active"]
    assert len(active) == 1
    assert active[0].title == "Launch new podcast"


def test_format_goals_canvas():
    goals = [
        rt_goals.Goal(id="1", title="Write chapter 1", target_date="Friday", milestones=["Outlined characters"]),
        rt_goals.Goal(id="2", title="Past goal", status="completed"),
    ]
    canvas = rt_goals.format_goals_canvas(goals)
    assert "<ACTIVE_GOALS>" in canvas
    assert "Write chapter 1" in canvas
    assert "(target: Friday)" in canvas
    assert "Outlined characters" in canvas
    assert "Past goal" not in canvas


def test_goal_fact_derivation():
    extracted = {
        "goals": {
            "launch-startup": {
                "title": "Launch Startup",
                "target": "Q4",
                "status": "active",
            }
        }
    }
    facts = rt_facts.derive_facts(extracted)
    assert len(facts) == 1
    assert facts[0]["p_kind"] == "goal"
    assert "launch-startup" in facts[0]["p_norm_key"]


import pytest
from unittest.mock import patch, MagicMock
from agent import RtAgent


@pytest.mark.asyncio
async def test_manage_goals_tool_execution():
    agent = RtAgent(caller_e164="+19174030642")
    context = MagicMock()

    mock_bundle = {"schemas": []}
    posted = []

    def fake_req(method, endpoint, body=None):
        if "rt_get_caller_full_bundle" in endpoint:
            return mock_bundle
        if "rt_add_schema_entry" in endpoint and body:
            mock_bundle["schemas"] = [{"category": "goals", "data_summary": body.get("p_summary")}]
        posted.append((endpoint, body))
        return None

    with patch("rt_prefs._req", side_effect=fake_req):
        # 1. Create Goal
        res_create = await agent.manage_goals(context, action="create", title="Get in best shape for 52nd birthday", target_date="January 2, 2028")
        assert "[goal created:" in res_create
        assert any("rt_add_schema_entry" in p[0] for p in posted)

        # 2. Add milestone
        res_step = await agent.manage_goals(context, action="milestone", title="Get in best shape", milestone_step="Ran 3 miles")
        assert "[milestone logged" in res_step

        # 3. List
        res_list = await agent.manage_goals(context, action="list", title="")
        assert "Get in best shape" in res_list

        # 4. Complete
        res_done = await agent.manage_goals(context, action="complete", title="Get in best shape")
        assert "[goal completed:" in res_done
