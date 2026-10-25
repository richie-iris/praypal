"""test_rt_grief_radar.py — Tests for grief radar and anti-platitude pastoral directive."""
import pytest
from rt_grief_radar import (
    detect_grief_markers,
    build_anti_platitude_directive,
    extract_memorial_anniversary,
    generate_condolence_followup_message,
)


def test_detect_grief_markers_acute_loss():
    res = detect_grief_markers("I am calling because I lost my daughter last month and cannot find peace.")
    assert res["is_grief"] is True
    assert res["loss_type"] == "child"
    assert res["severity"] == "acute"


def test_detect_grief_markers_spouse():
    res = detect_grief_markers("My husband passed away recently and the house is so empty.")
    assert res["is_grief"] is True
    assert res["loss_type"] == "spouse"
    assert res["severity"] == "high"


def test_detect_grief_markers_none():
    res = detect_grief_markers("I am praying for guidance on my upcoming job interview.")
    assert res["is_grief"] is False
    assert res["severity"] == "none"


def test_build_anti_platitude_directive():
    grief_info = {"is_grief": True, "severity": "acute", "loss_type": "child", "deceased_name": "Hannah"}
    directive = build_anti_platitude_directive(grief_info)

    assert "SACRED_GRIEF_GUARD" in directive
    assert "ACUTE" in directive
    assert "Hannah" in directive
    # Check that harmful platitudes are explicitly banned
    assert "everything happens for a reason" in directive
    assert "they are in a better place" in directive
    assert "time heals all wounds" in directive
    assert "Validate the sacred reality of their grief" in directive


def test_extract_memorial_anniversary():
    ann = extract_memorial_anniversary("Today is the one year anniversary of my father's passing.")
    assert ann is not None
    assert ann["type"] == "memorial_anniversary"
    assert ann["timeframe"] == "one year"

    yahrzeit = extract_memorial_anniversary("We are lighting a candle for the yahrzeit of Rebecca.")
    assert yahrzeit is not None
    assert yahrzeit["type"] == "yahrzeit"
    assert "Rebecca" in yahrzeit["honoree"]


def test_generate_condolence_followup_message():
    msg = generate_condolence_followup_message("David", "spouse", preferred_guide="jesus")
    assert "David" in msg
    assert "Good Shepherd" in msg
    assert "🕊️" in msg
