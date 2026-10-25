"""test_rt_inbound_cap.py — Inbound carrier spend limit verification."""
from __future__ import annotations

import json
import pytest
import rt_carrier


@pytest.fixture(autouse=True)
def _fresh_carrier_state(monkeypatch):
    monkeypatch.setattr(rt_carrier, "_CACHE", None)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok-test")
    monkeypatch.delenv("TWILIO_DAILY_SPEND_USD", raising=False)


def _mock_usage(price, monkeypatch):
    class _R:
        def __init__(self, body):
            self._b = body

        def read(self):
            return self._b

        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False

    def _urlopen(req, timeout=None):
        if isinstance(price, Exception):
            raise price
        body = {"usage_records": [] if price is None else [{"category": "totalprice", "price": price}]}
        return _R(json.dumps(body).encode())

    monkeypatch.setattr(rt_carrier.urllib.request, "urlopen", _urlopen)


def test_inbound_allowed_under_cap(monkeypatch):
    _mock_usage(10.50, monkeypatch)
    allowed, reason, detail = rt_carrier.inbound_allowed(fresh=True)
    assert allowed is True
    assert reason == "under_cap"
    assert detail["spend_usd"] == 10.50
    assert detail["cap_usd"] == 25.0


def test_inbound_refused_at_cap(monkeypatch):
    _mock_usage(25.00, monkeypatch)
    allowed, reason, detail = rt_carrier.inbound_allowed(fresh=True)
    assert allowed is False
    assert reason == "daily_cap_spent"
    assert detail["spend_usd"] == 25.00


def test_inbound_refused_over_cap(monkeypatch):
    _mock_usage(32.40, monkeypatch)
    allowed, reason, detail = rt_carrier.inbound_allowed(fresh=True)
    assert allowed is False
    assert reason == "daily_cap_spent"
    assert detail["spend_usd"] == 32.40


def test_inbound_custom_cap(monkeypatch):
    monkeypatch.setenv("TWILIO_DAILY_SPEND_USD", "15.00")
    _mock_usage(16.00, monkeypatch)
    allowed, reason, detail = rt_carrier.inbound_allowed(fresh=True)
    assert allowed is False
    assert reason == "daily_cap_spent"
    assert detail["cap_usd"] == 15.00


def test_inbound_allowed_when_no_credentials(monkeypatch):
    monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    allowed, reason, detail = rt_carrier.inbound_allowed(fresh=True)
    assert allowed is True
    assert reason == "no_twilio_credentials"


def test_inbound_passthrough_on_api_error(monkeypatch):
    import urllib.error
    _mock_usage(urllib.error.URLError("503 Service Unavailable"), monkeypatch)
    allowed, reason, detail = rt_carrier.inbound_allowed(fresh=True)
    assert allowed is True
    assert reason == "usage_unreadable_passthrough"
