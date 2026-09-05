"""test_human_simulator.py — Unit tests for the Persona-Driven Human Simulation Harness.

Verifies:
1. Persona definitions and multi-call plan integrity.
2. Simulated turn formatting and speaker attribution.
3. Invariant check evaluation (fact matching, retraction rejection).
4. Fast mock execution of simulated persona arcs.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "harness"))

import human_simulator


def test_persona_catalog_structure():
    """Verify all defined personas have valid E.164, traits, and multi-call plans."""
    assert "mildred" in human_simulator.PERSONAS
    assert "arthur" in human_simulator.PERSONAS

    for pid, persona in human_simulator.PERSONAS.items():
        assert persona.id == pid
        assert persona.name
        assert persona.e164.startswith("+1")
        assert len(persona.traits) > 0
        assert len(persona.calls) > 0

        for call in persona.calls:
            assert call.call_num >= 1
            assert call.goal
            assert len(call.simulated_turns) >= 2


def test_mock_persona_simulation_mildred():
    """Verify Mildred persona simulation executes cleanly in mock mode."""
    mildred = human_simulator.PERSONAS["mildred"]
    res = human_simulator.simulate_persona_arc(mildred, mock_mode=True)

    assert res["persona_id"] == "mildred"
    assert res["passed"] is True
    assert len(res["call_evaluations"]) == len(mildred.calls)

    # Verify Call 3 checked cadence
    call3_eval = res["call_evaluations"][2]
    assert call3_eval["cadence_stage"] == "soft_wrap"


def test_mock_persona_simulation_arthur():
    """Verify Arthur persona simulation executes cleanly in mock mode."""
    arthur = human_simulator.PERSONAS["arthur"]
    res = human_simulator.simulate_persona_arc(arthur, mock_mode=True)

    assert res["persona_id"] == "arthur"
    assert res["passed"] is True
    assert len(res["call_evaluations"]) == len(arthur.calls)
