"""test_rt_devotion_scheduler.py — Tests for the Daily Scheduled Devotion Dispatcher."""
from __future__ import annotations

from unittest.mock import patch
import pytest
import rt_devotion_scheduler


@pytest.fixture(autouse=True)
def clean_devotions():
    rt_devotion_scheduler.reset_devotions_for_testing()
    yield
    rt_devotion_scheduler.reset_devotions_for_testing()


def test_devotional_content_curation():
    # Krishna devotion -> Bhagavad Gita
    krishna_content = rt_devotion_scheduler.get_devotional_content("krishna", index=0)
    assert "Gita" in krishna_content["passage"]
    assert krishna_content["verse"]
    assert krishna_content["reflection"]

    # Jesus devotion -> Gospels / Psalms
    jesus_content = rt_devotion_scheduler.get_devotional_content("jesus", index=0)
    assert "Matthew" in jesus_content["passage"] or "Psalm" in jesus_content["passage"]

    # Unknown guide falls back to universal God
    fallback = rt_devotion_scheduler.get_devotional_content("unknown_guide", index=0)
    assert fallback["passage"]


def test_format_devotional_sms():
    sms = rt_devotion_scheduler.format_devotional_sms(
        guide_key="krishna",
        recipient_name="Arjun",
        preferred_name_for_god="Govinda",
        index=0,
    )
    assert "PrayPal Daily Devotion for Arjun" in sms
    assert "Bhagavad Gita" in sms
    assert "Govinda" in sms
    assert "Reply PRAY" in sms


def test_schedule_and_cancel_devotion():
    h = "hash_arjun"
    res = rt_devotion_scheduler.schedule_devotion(
        phone_hash=h,
        phone_e164="+15552345678",
        time_of_day="morning",
        channel="sms",
        guide_key="krishna",
        recipient_name="Arjun",
    )
    assert res["status"] == "ok"
    assert res["schedule"]["active"] is True
    assert res["schedule"]["time_of_day"] == "morning"

    active = rt_devotion_scheduler.get_active_schedules()
    assert len(active) == 1
    assert active[0]["phone_hash"] == h

    # Cancel schedule
    cancelled = rt_devotion_scheduler.cancel_devotion(h)
    assert cancelled is True
    assert len(rt_devotion_scheduler.get_active_schedules()) == 0


def test_dispatch_devotion_now():
    h = "hash_mary"
    rt_devotion_scheduler.schedule_devotion(
        phone_hash=h,
        phone_e164="+15553456789",
        time_of_day="morning",
        channel="sms",
        guide_key="mother",
        recipient_name="Mary",
    )

    with patch("rt_sms.send_sms") as mock_send:
        res = rt_devotion_scheduler.dispatch_devotion_now(h, dry_run=False)
        assert res["status"] == "ok"
        mock_send.assert_called_once()
        args, kwargs = mock_send.call_args
        assert args[0] == "+15553456789"
        assert "PrayPal Daily Devotion for Mary" in args[1]
