"""test_rt_sms.py — Twilio sending, webhook signatures, and the per-caller ledger.

Written 2026-09-02 alongside the fixes it pins: the harness had asserted the
Telnyx endpoint while every real text went through Twilio, the webhook had
no signature check, and the ledger kept plaintext numbers in /tmp forever.
"""
from __future__ import annotations

import base64
import io
import json
import os
import stat
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

import rt_prefs
import rt_sms

PEPPER = "test-pepper-0123456789abcdef-long-enough"
TO = "+15005550006"


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    d = tmp_path / "ledger"
    monkeypatch.setenv("RT_SMS_LEDGER_DIR", str(d))
    monkeypatch.setenv("RT_PHONE_HASH_PEPPER", PEPPER)
    monkeypatch.delenv("RT_REQUIRE_PEPPER", raising=False)
    monkeypatch.delenv("RT_SMS_LEDGER_RETENTION_DAYS", raising=False)
    monkeypatch.setattr(rt_sms, "_HISTORY", {})
    monkeypatch.setattr(rt_sms, "_LAST_SWEEP", 0.0)
    return d


@pytest.fixture
def twilio_env(monkeypatch, ledger):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest0000000000000000000000000000")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok-test-not-real")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+15005550001")
    monkeypatch.delenv("RT_PUBLIC_NUMBER", raising=False)
    return ledger


class _Resp:
    def __init__(self, body: bytes, status: int = 200):
        self._body, self.status = body, status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _record_sends(monkeypatch, reply=None, raise_exc=None):
    captured: dict = {}

    def _urlopen(req, *a, **kw):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        captured["params"] = urllib.parse.parse_qsl(req.data.decode("utf-8"), keep_blank_values=True)
        captured["timeout"] = kw.get("timeout")
        if raise_exc is not None:
            raise raise_exc
        return _Resp(json.dumps(reply or {"sid": "SMfake", "status": "queued"}).encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    return captured


# ── signatures ───────────────────────────────────────────────────────────────

def test_signature_matches_twilio_documented_example():
    """The worked example from Twilio's security docs: token 12345 over this
    URL and these five fields must yield exactly this signature."""
    url = "https://mycompany.com/myapp.php?foo=1&bar=2"
    params = {"CallSid": "CA1234567890ABCDE", "Caller": "+12349013030", "Digits": "1234",
              "From": "+12349013030", "To": "+18005551212"}
    assert rt_sms.twilio_signature("12345", url, params) == "0/KCTR6DLpKmkAf8muzZqo1nDgQ="


def test_signature_sorts_repeated_fields_by_value():
    single = rt_sms.twilio_signature("t", "https://x/y", {"A": ["2", "1"], "B": ""})
    assert single == rt_sms.twilio_signature("t", "https://x/y", {"A": ["1", "2"], "B": ""})


def test_webhook_accepts_only_a_matching_signature(monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")
    params = {"From": TO, "Body": "", "MessageSid": "SM1"}
    good_url = "https://lane.example/sms/incoming"
    sig = rt_sms.twilio_signature("tok", good_url, params)

    assert rt_sms.webhook_is_from_twilio(sig, ["https://other.example/sms/incoming", good_url], params)
    assert not rt_sms.webhook_is_from_twilio(sig, ["https://other.example/sms/incoming"], params)
    assert not rt_sms.webhook_is_from_twilio(sig[:-2] + "==", [good_url], params)
    assert not rt_sms.webhook_is_from_twilio(sig, [good_url], {**params, "Body": "changed"})
    assert not rt_sms.webhook_is_from_twilio(None, [good_url], params)
    assert not rt_sms.webhook_is_from_twilio("", [good_url], params)
    assert not rt_sms.webhook_is_from_twilio(sig, [], params)


def test_webhook_fails_closed_without_a_token(monkeypatch):
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    params = {"From": TO}
    sig = rt_sms.twilio_signature("", "https://lane.example/sms/incoming", params)
    assert not rt_sms.webhook_is_from_twilio(sig, ["https://lane.example/sms/incoming"], params)


# ── the ledger ───────────────────────────────────────────────────────────────

def test_ledger_file_is_named_by_phone_hash_not_the_number(ledger):
    rt_sms.record_sms(TO, "inbound", "hello", sid="SM1")
    files = sorted(p.name for p in ledger.iterdir() if p.suffix == ".json")
    assert files == [f"{rt_prefs.phone_hash(TO)}.json"]
    assert "5005550006" not in files[0]
    assert len(files[0]) == 64 + len(".json")


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes")
def test_ledger_directory_and_files_are_private(ledger):
    rt_sms.record_sms(TO, "inbound", "hello")
    assert stat.S_IMODE(ledger.stat().st_mode) == 0o700
    for p in ledger.iterdir():
        if p.suffix == ".json":
            assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_get_recent_returns_the_newest_n_oldest_first(ledger):
    for i in range(4):
        rt_sms.record_sms(TO, "inbound" if i % 2 else "outbound", f"m{i}")
    got = rt_sms.get_recent_sms(TO, limit=2)
    assert [m["text"] for m in got] == ["m2", "m3"]
    assert rt_sms.get_recent_sms(TO, limit=0) == []
    assert rt_sms.get_recent_sms("", limit=5) == []


def test_ledger_keeps_only_thirty_entries(ledger):
    for i in range(40):
        rt_sms.record_sms(TO, "inbound", f"m{i}")
    got = rt_sms.get_recent_sms(TO, limit=100)
    assert len(got) == 30 and got[0]["text"] == "m10" and got[-1]["text"] == "m39"


def test_retention_drops_old_entries_on_write_and_on_read(ledger, monkeypatch):
    monkeypatch.setenv("RT_SMS_LEDGER_RETENTION_DAYS", "1")
    rt_sms.record_sms(TO, "inbound", "old")
    path = ledger / f"{rt_prefs.phone_hash(TO)}.json"
    items = json.loads(path.read_text())
    items[0]["ts"] = time.time() - 2 * 86400
    path.write_text(json.dumps(items))

    assert rt_sms.get_recent_sms(TO) == []
    rt_sms.record_sms(TO, "outbound", "new")
    assert [m["text"] for m in json.loads(path.read_text())] == ["new"]


def test_retention_window_has_a_one_day_floor(monkeypatch, capsys):
    monkeypatch.setenv("RT_SMS_LEDGER_RETENTION_DAYS", "0")
    assert rt_sms._retention_days() == 1
    monkeypatch.setenv("RT_SMS_LEDGER_RETENTION_DAYS", "-5")
    assert rt_sms._retention_days() == 1
    monkeypatch.setenv("RT_SMS_LEDGER_RETENTION_DAYS", "garbage")
    assert rt_sms._retention_days() == 30
    assert "RT_SMS_LEDGER_RETENTION_DAYS" in capsys.readouterr().out
    monkeypatch.delenv("RT_SMS_LEDGER_RETENTION_DAYS", raising=False)
    assert rt_sms._retention_days() == 30


def test_forget_removes_the_file_and_the_mirror(ledger):
    rt_sms.record_sms(TO, "inbound", "hello")
    assert rt_sms.forget(TO) is True
    assert not any(p.suffix == ".json" for p in ledger.iterdir())
    assert rt_sms.get_recent_sms(TO) == []
    assert rt_sms.forget(TO) is False


def test_seen_sid_recognises_a_recorded_twilio_message_id(ledger):
    rt_sms.record_sms(TO, "inbound", "hello", sid="SMabc")
    assert rt_sms.seen_sid(TO, "SMabc")
    assert not rt_sms.seen_sid(TO, "SMxyz")
    assert not rt_sms.seen_sid(TO, "")
    assert not rt_sms.seen_sid(TO, None)


def test_purge_expired_sweeps_every_file_and_removes_the_empty_ones(ledger, monkeypatch):
    monkeypatch.setenv("RT_SMS_LEDGER_RETENTION_DAYS", "1")
    other = "+15005550007"
    rt_sms.record_sms(TO, "inbound", "keep")
    rt_sms.record_sms(other, "inbound", "stale")
    stale = ledger / f"{rt_prefs.phone_hash(other)}.json"
    stale.write_text(json.dumps([{"direction": "inbound", "text": "stale", "media": [],
                                  "ts": time.time() - 5 * 86400, "sid": None}]))
    orphan = ledger / "deadbeef.json.tmp.999"
    orphan.write_text("[]")
    os.utime(orphan, (time.time() - 7200, time.time() - 7200))
    corrupt = ledger / f"{rt_prefs.phone_hash('+15005550008')}.json"
    corrupt.write_text(json.dumps([{"direction": "inbound", "text": "bad ts", "media": [], "ts": "yesterday"}]))

    out = rt_sms.purge_expired()
    assert out == {"files": 3, "removed": 2, "dropped_items": 2}
    assert not stale.exists() and not orphan.exists() and not corrupt.exists()
    assert [m["text"] for m in rt_sms.get_recent_sms(TO)] == ["keep"]


def test_ledger_stores_nothing_when_the_number_cannot_be_hashed(ledger, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("RT_PHONE_HASH_PEPPER is not set but RT_REQUIRE_PEPPER is on")
    monkeypatch.setattr(rt_prefs, "phone_hash", _boom)
    rt_sms.record_sms(TO, "inbound", "hello")
    # Fail-closed happens before the directory is even created.
    assert not ledger.exists() or not any(p.suffix == ".json" for p in ledger.iterdir())
    assert rt_sms.get_recent_sms(TO) == []
    assert rt_sms.forget(TO) is False


# ── sending ──────────────────────────────────────────────────────────────────

def test_send_sms_posts_the_twilio_form(twilio_env, monkeypatch):
    cap = _record_sends(monkeypatch)
    res = rt_sms.send_sms(TO, "your appointment is Thursday at ten")

    sid = os.environ["TWILIO_ACCOUNT_SID"]
    assert cap["url"] == f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    assert cap["method"] == "POST" and cap["timeout"] == 12
    expected_auth = "Basic " + base64.b64encode(f"{sid}:tok-test-not-real".encode()).decode()
    assert cap["headers"]["authorization"] == expected_auth
    assert cap["headers"]["content-type"] == "application/x-www-form-urlencoded"
    assert cap["params"] == [("To", TO), ("From", "+15005550001"),
                             ("Body", "your appointment is Thursday at ten")]
    assert res == {"error": False, "sid": "SMfake", "message": "queued"}


def test_send_sms_records_the_outbound_text_with_its_sid(twilio_env, monkeypatch):
    _record_sends(monkeypatch, reply={"sid": "SM777", "status": "accepted"})
    rt_sms.send_sms(TO, "hello", media_url=["https://lane.example/media/jasper.jpg"])
    got = rt_sms.get_recent_sms(TO)
    assert len(got) == 1
    assert got[0]["direction"] == "outbound" and got[0]["sid"] == "SM777"
    assert got[0]["media"] == ["https://lane.example/media/jasper.jpg"]


def test_send_sms_media_urls_become_repeated_mediaurl_fields(twilio_env, monkeypatch):
    cap = _record_sends(monkeypatch)
    rt_sms.send_sms(TO, "", media_url=["https://a.example/1.jpg", "", "https://a.example/2.jpg"])
    assert [v for k, v in cap["params"] if k == "MediaUrl"] == ["https://a.example/1.jpg", "https://a.example/2.jpg"]
    assert not any(k == "Body" for k, _ in cap["params"])


def test_send_sms_sender_precedence_argument_env_public_then_refusal(twilio_env, monkeypatch):
    cap = _record_sends(monkeypatch)
    rt_sms.send_sms(TO, "hi", from_number="+15552220000")
    assert dict(cap["params"])["From"] == "+15552220000"
    rt_sms.send_sms(TO, "hi")
    assert dict(cap["params"])["From"] == "+15005550001"

    monkeypatch.setenv("TWILIO_FROM_NUMBER", "   ")
    monkeypatch.setenv("RT_PUBLIC_NUMBER", "+15553330000")
    rt_sms.send_sms(TO, "hi")
    assert dict(cap["params"])["From"] == "+15553330000"

    cap.clear()
    monkeypatch.setenv("RT_PUBLIC_NUMBER", "")
    res = rt_sms.send_sms(TO, "hi")
    assert res["error"] and "TWILIO_FROM_NUMBER" in res["message"]
    assert cap == {}, "a send with no sender must be refused before the socket"


@pytest.mark.parametrize("env, to, body", [
    ({"TWILIO_ACCOUNT_SID": ""}, TO, "hello"),
    ({"TWILIO_AUTH_TOKEN": "  "}, TO, "hello"),
    ({}, "", "hello"),
    ({}, "15005550006", "hello"),
    ({}, TO, ""),
    ({}, TO, "   \n "),
    ({}, TO, None),
])
def test_send_sms_bad_input_never_reaches_the_network(twilio_env, monkeypatch, env, to, body):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    cap = _record_sends(monkeypatch)
    res = rt_sms.send_sms(to, body)
    assert res["error"] is True and res["sid"] is None and res["message"]
    assert cap == {}


def test_send_sms_http_and_transport_failures_are_values(twilio_env, monkeypatch):
    err = urllib.error.HTTPError("https://api.twilio.com/x", 422, "Unprocessable", {},
                                 io.BytesIO(b'{"message":"unregistered destination"}'))
    _record_sends(monkeypatch, raise_exc=err)
    res = rt_sms.send_sms(TO, "hello")
    assert res["error"] and res["sid"] is None and res["message"].startswith("HTTP 422:")
    assert rt_sms.get_recent_sms(TO) == [], "a failed send is not a sent text"

    _record_sends(monkeypatch, raise_exc=TimeoutError("the read operation timed out"))
    res = rt_sms.send_sms(TO, "hello")
    assert res["error"] and "timed out" in res["message"]


def test_send_sms_logs_neither_the_number_nor_the_body(twilio_env, monkeypatch, capsys):
    _record_sends(monkeypatch)
    rt_sms.send_sms(TO, "tell Marjorie the biopsy came back clear")
    out = capsys.readouterr().out
    assert TO not in out and "biopsy" not in out and "Marjorie" not in out
    assert "***0006" in out
