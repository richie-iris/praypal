"""test_rt_pray_holiday.py — Tests for Illuminated Holiday Cards & Audio Devotionals."""
from __future__ import annotations

import datetime
from unittest.mock import patch
import pytest
import rt_pray_holiday


def test_get_holiday_card_payload():
    diwali = rt_pray_holiday.get_holiday_card_payload("diwali", recipient_name="Ramesh")
    assert "DIWALI" in diwali["text"]
    assert "Ramesh" in diwali["text"]
    assert "Upanishad" in diwali["text"]
    assert diwali["image_url"]
    assert diwali["audio_channel"] == "hindu_tanpura"

    christmas = rt_pray_holiday.get_holiday_card_payload("christmas", recipient_name="Grace")
    assert "CHRISTMAS" in christmas["text"]
    assert "Luke 2:14" in christmas["text"]
    assert christmas["audio_channel"] == "christian_cathedral"


def test_dispatch_holiday_card_mms():
    with patch("rt_sms.send_sms") as mock_send:
        res = rt_pray_holiday.dispatch_holiday_card(
            to_e164="+15554443333",
            holiday_key="easter",
            recipient_name="John",
            dry_run=False,
        )
        assert res["status"] == "ok"
        mock_send.assert_called_once()
        args, kwargs = mock_send.call_args
        assert args[0] == "+15554443333"
        assert "EASTER" in args[1]
        assert "media_urls" in kwargs
        assert len(kwargs["media_urls"]) == 1
        assert "easter_dawn.jpg" in kwargs["media_urls"][0]


def test_get_active_holiday_detection():
    # Test date exactly on Dec 25
    xmas_date = datetime.date(2026, 12, 25)
    active = rt_pray_holiday.get_active_holiday(xmas_date, window_days=0)
    assert active is not None
    assert active["holiday_key"] == "christmas"
    assert active["is_today"] is True

    # Test date far from holidays
    calm_date = datetime.date(2026, 7, 15)
    assert rt_pray_holiday.get_active_holiday(calm_date, window_days=2) is None
