"""test_call_cadence.py — Unit tests for the 3-tier call cadence lifecycle manager.

Verifies:
1. Cadence stage transitions (0-15m normal, 20m soft-wrap, 30m firm-wrap, 35m hard-cap).
2. Environment variable overrides for thresholds.
3. Cadence directive rendering and formatting with caller names.
4. Farewell message generation for hard cap termination.
"""

import rt_cadence


def test_cadence_stage_transitions():
    """Verify standard default thresholds: 20m soft, 30m firm, 35m hard cap."""
    # 0 mins -> normal
    assert rt_cadence.evaluate_cadence_stage(0.0) == rt_cadence.STAGE_NORMAL
    # 10 mins (600s) -> normal
    assert rt_cadence.evaluate_cadence_stage(600.0) == rt_cadence.STAGE_NORMAL
    # 15 mins (900s) -> normal
    assert rt_cadence.evaluate_cadence_stage(900.0) == rt_cadence.STAGE_NORMAL
    # 19.9 mins (1194s) -> normal
    assert rt_cadence.evaluate_cadence_stage(1194.0) == rt_cadence.STAGE_NORMAL
    # 20.0 mins (1200s) -> soft_wrap
    assert rt_cadence.evaluate_cadence_stage(1200.0) == rt_cadence.STAGE_SOFT_WRAP
    # 25.0 mins (1500s) -> soft_wrap
    assert rt_cadence.evaluate_cadence_stage(1500.0) == rt_cadence.STAGE_SOFT_WRAP
    # 30.0 mins (1800s) -> firm_wrap
    assert rt_cadence.evaluate_cadence_stage(1800.0) == rt_cadence.STAGE_FIRM_WRAP
    # 34.9 mins (2094s) -> firm_wrap
    assert rt_cadence.evaluate_cadence_stage(2094.0) == rt_cadence.STAGE_FIRM_WRAP
    # 35.0 mins (2100s) -> hard_cap
    assert rt_cadence.evaluate_cadence_stage(2100.0) == rt_cadence.STAGE_HARD_CAP
    # 45.0 mins (2700s) -> hard_cap
    assert rt_cadence.evaluate_cadence_stage(2700.0) == rt_cadence.STAGE_HARD_CAP


def test_cadence_custom_threshold_parameters():
    """Verify explicit threshold overrides passed to evaluate_cadence_stage."""
    # Custom: 5m soft, 10m firm, 12m hard
    assert rt_cadence.evaluate_cadence_stage(240.0, soft_mins=5.0, firm_mins=10.0, hard_mins=12.0) == "normal"
    assert rt_cadence.evaluate_cadence_stage(300.0, soft_mins=5.0, firm_mins=10.0, hard_mins=12.0) == "soft_wrap"
    assert rt_cadence.evaluate_cadence_stage(600.0, soft_mins=5.0, firm_mins=10.0, hard_mins=12.0) == "firm_wrap"
    assert rt_cadence.evaluate_cadence_stage(720.0, soft_mins=5.0, firm_mins=10.0, hard_mins=12.0) == "hard_cap"


def test_cadence_env_overrides(monkeypatch):
    """Verify environment variable overrides for cadence configuration."""
    monkeypatch.setenv("RT_CALL_SOFT_WRAP_MINS", "10")
    monkeypatch.setenv("RT_CALL_FIRM_WRAP_MINS", "15")
    monkeypatch.setenv("RT_CALL_HARD_CAP_MINS", "18")

    soft, firm, hard = rt_cadence.get_cadence_thresholds()
    assert soft == 10.0
    assert firm == 15.0
    assert hard == 18.0

    # 10 mins (600s) should now be soft_wrap
    assert rt_cadence.evaluate_cadence_stage(600.0) == rt_cadence.STAGE_SOFT_WRAP
    # 15 mins (900s) should now be firm_wrap
    assert rt_cadence.evaluate_cadence_stage(900.0) == rt_cadence.STAGE_FIRM_WRAP
    # 18 mins (1080s) should now be hard_cap
    assert rt_cadence.evaluate_cadence_stage(1080.0) == rt_cadence.STAGE_HARD_CAP


def test_cadence_directive_rendering():
    """Verify directive formatting, name interpolation, and instructions."""
    # Soft wrap directive
    soft_dir = rt_cadence.render_cadence_directive(rt_cadence.STAGE_SOFT_WRAP, "Richie")
    assert "20-MINUTE CHECKPOINT" in soft_dir
    assert "Richie" in soft_dir
    assert "steer the conversation toward a warm close" in soft_dir

    # Firm wrap directive
    firm_dir = rt_cadence.render_cadence_directive(rt_cadence.STAGE_FIRM_WRAP, "Richie")
    assert "30-MINUTE CEILING" in firm_dir
    assert "Richie" in firm_dir
    assert "MUST warmly and definitively say goodbye" in firm_dir
    assert "end_call()" in firm_dir

    # Normal stage returns empty directive
    assert rt_cadence.render_cadence_directive(rt_cadence.STAGE_NORMAL) == ""


def test_cadence_farewell_message():
    """Verify hard cap spoken message formatting."""
    named_msg = rt_cadence.get_cadence_farewell_message("Richie")
    assert "Richie" in named_msg
    assert "Take wonderful care" in named_msg

    default_msg = rt_cadence.get_cadence_farewell_message(None)
    assert "Take wonderful care" in default_msg
