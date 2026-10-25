"""test_rt_pray_circle.py — Tests for Multi-Party Family Prayer Circles & Conference PINs."""
from __future__ import annotations

import pytest
import rt_pray_circle


@pytest.fixture(autouse=True)
def clean_circles():
    rt_pray_circle.reset_circles_for_testing()
    yield
    rt_pray_circle.reset_circles_for_testing()


def test_create_prayer_circle():
    res = rt_pray_circle.create_prayer_circle(
        organizer_hash="hash_organizer_1",
        title="The Patel Family Satsang",
        guide_key="krishna",
        max_participants=6,
        pin="108108",
    )
    assert res["status"] == "ok"
    assert res["circle"]["pin"] == "108108"
    assert res["circle"]["title"] == "The Patel Family Satsang"
    assert res["circle"]["guide_key"] == "krishna"
    assert res["circle"]["status"] == "active"


def test_join_prayer_circle():
    rt_pray_circle.create_prayer_circle(
        organizer_hash="hash_organizer_1",
        title="Healing Circle for Grandma",
        pin="777888",
    )

    # Participant 1 joins
    j1 = rt_pray_circle.join_prayer_circle("777888", "+15551112222", "Priya")
    assert j1["status"] == "ok"
    assert j1["participant"]["name"] == "Priya"

    # Participant 2 joins
    j2 = rt_pray_circle.join_prayer_circle("777888", "+15553334444", "Rajesh")
    assert j2["status"] == "ok"
    assert j2["participant"]["name"] == "Rajesh"

    circle = rt_pray_circle.get_circle_by_pin("777888")
    assert len(circle["roster"]) == 2


def test_moderator_prompt_generation():
    rt_pray_circle.create_prayer_circle(
        organizer_hash="hash_organizer_1",
        title="Smith Family Thanksgiving Circle",
        pin="123456",
    )
    rt_pray_circle.join_prayer_circle("123456", "+15551112222", "David")
    rt_pray_circle.join_prayer_circle("123456", "+15553334444", "Sarah")

    prompt = rt_pray_circle.build_circle_moderator_prompt("123456")
    assert "MULTI-PARTY FAMILY PRAYER CIRCLE" in prompt
    assert "David, Sarah" in prompt
    assert "weave" in prompt.lower() or "unified communal prayer" in prompt.lower()


def test_capacity_limit_and_closing():
    rt_pray_circle.create_prayer_circle(
        organizer_hash="hash_organizer_1",
        title="Intimate Duo Prayer",
        max_participants=2,
        pin="222333",
    )
    rt_pray_circle.join_prayer_circle("222333", "+15551112222", "Member 1")
    rt_pray_circle.join_prayer_circle("222333", "+15552223333", "Member 2")

    # 3rd participant rejected
    j3 = rt_pray_circle.join_prayer_circle("222333", "+15553334444", "Member 3")
    assert j3["status"] == "circle_full"

    # Close circle
    assert rt_pray_circle.close_prayer_circle("222333") is True
    # Can no longer join closed circle
    j_after = rt_pray_circle.join_prayer_circle("222333", "+15554445555", "Member 4")
    assert j_after["status"] == "not_found"
