"""test_rt_assistant_friend.py — Unit tests verifying the assistant friend persona and capability matrix."""
from __future__ import annotations

import rt_self
import rt_capabilities
import rt_hydrator
import json


def test_assistant_friend_persona_render():
    rendered = rt_self.render()
    assert "proactive personal voice assistant" in rendered
    assert "track it or set a reminder" in rendered
    assert "Proactively follow up on previously discussed goals" in rendered


def test_manage_goals_capability_presence():
    caps = [cap.prompt_label for cap in rt_capabilities.CAPABILITIES]
    assert any("manage_goals" in c for c in caps)


def test_prompt_hydration_includes_goals_and_assistant_identity():
    bundle = {
        "caller": {
            "display_name": "Richie",
            "agent_alias": "Iris",
        },
        "schemas": [
            {
                "category": "goals",
                "data_summary": json.dumps({
                    "g_101": {
                        "id": "g_101",
                        "title": "Build Phone Pal V2",
                        "target_date": "This Week",
                        "milestones": ["Completed goal architecture"],
                    }
                })
            }
        ],
        "reminders": [
            {
                "reminder_text": "Call the doctor for checkup",
                "due_time_str": "Tomorrow 10am"
            }
        ]
    }
    prompt, _ = rt_hydrator.discover_and_hydrate_prompt("+19174030642", prefetch_bundle=bundle)
    assert "proactive assistant" in prompt
    assert "<ACTIVE_GOALS>" in prompt
    assert "Build Phone Pal V2" in prompt
    assert "Call the doctor for checkup" in prompt
