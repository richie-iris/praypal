"""test_carrier_cap_gate.py — the daily spend cap Twilio cannot enforce for us.

Telnyx refused the call itself past $25/day and the dial paths were written
leaning on that. Twilio has no such switch (ADR 0006), so the cap is
rt_carrier's, checked before every unattended dial, failing closed.
"""
from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest

import rt_bridge
import rt_capabilities
import rt_carrier
import rt_prefs


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(rt_carrier, "_CACHE", None)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok-test")  # noqa: S105 - dummy value for the stubbed lane
    monkeypatch.delenv("TWILIO_DAILY_SPEND_USD", raising=False)


def _usage(price, monkeypatch, calls=None):
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
        if calls is not None:
            calls.append((req.full_url, {k.lower(): v for k, v in req.header_items()}))
        if isinstance(price, Exception):
            raise price
        body = {"usage_records": [] if price is None else [{"category": "totalprice", "price": price}]}
        return _R(json.dumps(body).encode())
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)


# ── reading the account ──────────────────────────────────────────────────────

def test_spend_is_read_from_todays_totalprice(monkeypatch):
    calls: list = []
    _usage("3.25", monkeypatch, calls)
    assert rt_carrier.spend_today_usd() == 3.25
    url, headers = calls[0]
    assert url == "https://api.twilio.com/2010-04-01/Accounts/ACtest/Usage/Records/Today.json?Category=totalprice"
    assert headers["authorization"].startswith("Basic ")


def test_spend_is_cached_for_a_minute_and_fresh_bypasses_it(monkeypatch):
    calls: list = []
    _usage("1.00", monkeypatch, calls)
    assert rt_carrier.spend_today_usd() == 1.0
    _usage("9.00", monkeypatch, calls)
    assert rt_carrier.spend_today_usd() == 1.0, "a second read inside the TTL must not hit the network"
    assert rt_carrier.spend_today_usd(fresh=True) == 9.0
    assert len(calls) == 2, "the first read and the fresh one; the cached read hit nothing"


@pytest.mark.parametrize("failure", [
    urllib.error.HTTPError("https://api.twilio.com/x", 401, "Unauthorized", {}, io.BytesIO(b"")),
    TimeoutError("the read operation timed out"),
    ValueError("not json"),
])
def test_unreadable_usage_is_none_never_an_exception(monkeypatch, failure):
    _usage(failure, monkeypatch)
    assert rt_carrier.spend_today_usd() is None


def test_empty_usage_list_is_unknown_not_zero(monkeypatch):
    _usage(None, monkeypatch)
    assert rt_carrier.spend_today_usd() is None


def test_no_credentials_reads_nothing(monkeypatch):
    calls: list = []
    _usage("1.00", monkeypatch, calls)
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    assert rt_carrier.spend_today_usd() is None
    assert calls == []


# ── the cap ──────────────────────────────────────────────────────────────────

def test_cap_defaults_and_parses(monkeypatch):
    assert rt_carrier.cap_usd() == 25.0
    monkeypatch.setenv("TWILIO_DAILY_SPEND_USD", "5.50")
    assert rt_carrier.cap_usd() == 5.5
    monkeypatch.setenv("TWILIO_DAILY_SPEND_USD", "not-a-number")
    assert rt_carrier.cap_usd() == 25.0
    monkeypatch.setenv("TWILIO_DAILY_SPEND_USD", "-5")
    assert rt_carrier.cap_usd() == 0.0, "a negative cap means stop, not spend backwards"


@pytest.mark.parametrize("spend, allowed, reason", [
    ("0", True, "under_cap"),
    ("24.99", True, "under_cap"),
    ("25", False, "daily_cap_spent"),
    ("25.01", False, "daily_cap_spent"),
    ("900", False, "daily_cap_spent"),
])
def test_outbound_allowed_against_the_cap(monkeypatch, spend, allowed, reason):
    _usage(spend, monkeypatch)
    got_allowed, got_reason, detail = rt_carrier.outbound_allowed()
    assert (got_allowed, got_reason) == (allowed, reason)
    assert detail == {"spend_usd": float(spend), "cap_usd": 25.0}


def test_outbound_fails_closed_when_the_bill_cannot_be_read(monkeypatch):
    _usage(TimeoutError("slow"), monkeypatch)
    allowed, reason, _ = rt_carrier.outbound_allowed()
    assert allowed is False and reason == "usage_unreadable"


def test_outbound_fails_closed_without_credentials(monkeypatch):
    monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
    allowed, reason, _ = rt_carrier.outbound_allowed()
    assert allowed is False and reason == "no_twilio_credentials"


# ── the gate in front of the bridge ──────────────────────────────────────────

def _no_db_needed(monkeypatch, calls):
    monkeypatch.setattr(rt_prefs, "_db", lambda: object())
    monkeypatch.setattr(rt_prefs, "_req",
                        lambda m, p, b=None, *a, **k: calls.append((m, p)) or ({"schemas": []} if "bundle" in p else {}))


def test_bridge_refuses_and_writes_nothing_when_the_cap_is_spent(monkeypatch):
    calls: list = []
    _no_db_needed(monkeypatch, calls)
    _usage("30.00", monkeypatch)
    with pytest.raises(rt_bridge.DialRefused) as ei:
        rt_bridge.check_and_record_dial("a" * 64, "+19175551234")
    assert "budget" in str(ei.value).lower()
    assert calls == [], "the cap is decided before the ledger is even read"


def test_bridge_refuses_when_the_bill_is_unreadable(monkeypatch):
    calls: list = []
    _no_db_needed(monkeypatch, calls)
    _usage(TimeoutError("slow"), monkeypatch)
    with pytest.raises(rt_bridge.DialRefused):
        rt_bridge.check_and_record_dial("a" * 64, "+19175551234")
    assert calls == []


def test_bridge_proceeds_to_the_ledger_when_under_the_cap(monkeypatch):
    calls: list = []
    _no_db_needed(monkeypatch, calls)
    _usage("1.00", monkeypatch)
    rt_bridge.check_and_record_dial("a" * 64, "+19175551234")
    assert any("rt_add_schema_entry" in p for _, p in calls), "an allowed dial is still written down"


# ── the gate in front of the scheduler ───────────────────────────────────────

def test_scheduler_refuses_an_unattended_dial_over_the_cap(monkeypatch):
    import rt_scheduler
    monkeypatch.setenv("SIP_OUTBOUND_TRUNK_ID", "ST_test")
    monkeypatch.setenv("RT_SCHEDULER_ENABLED", "1")
    _usage("30.00", monkeypatch)
    res = rt_scheduler._execute_outbound_call_job(
        {"payload": {"caller_e164": "+19175551234", "message": "hi"}, "phone_hash": "h"})
    assert res["error"] is True and "daily_cap_spent" in res["message"]


# ── the capability gate ──────────────────────────────────────────────────────

@pytest.mark.parametrize("tool", ["bridge_call", "schedule_reminder_call"])
def test_dial_tools_need_the_twilio_credentials(monkeypatch, tool):
    monkeypatch.setenv("SIP_OUTBOUND_TRUNK_ID", "ST_test")
    monkeypatch.setenv("RT_SCHEDULER_ENABLED", "1")
    assert rt_capabilities.enabled(tool), "with credentials and a trunk the tool is offered"
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "")
    assert not rt_capabilities.enabled(tool), "a lane that cannot read the bill must not be offered a dial tool"
    assert tool in rt_capabilities.disabled_tools()
