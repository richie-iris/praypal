"""test_rt_sms_inbound.py — the inbound text path: media, gating, dedupe, logs."""
from __future__ import annotations

import email.message
import io
import json
import urllib.error
import urllib.request
from collections import OrderedDict

import pytest

import rt_prefs
import rt_sms
import rt_sms_inbound

PEPPER = "test-pepper-0123456789abcdef-long-enough"
TEXTER = "+15005550006"
OURS = "+15005550001"
TWILIO_MEDIA = "https://api.twilio.com/2010-04-01/Accounts/ACx/Messages/MMx/Media/MEx"


@pytest.fixture
def lane(tmp_path, monkeypatch):
    monkeypatch.setenv("RT_SMS_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("RT_PHONE_HASH_PEPPER", PEPPER)
    monkeypatch.delenv("RT_REQUIRE_PEPPER", raising=False)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACx")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok-test-not-real")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", OURS)
    monkeypatch.setenv("RT_SMS_ENABLED", "1")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr(rt_sms, "_HISTORY", {})
    monkeypatch.setattr(rt_sms, "_LAST_SWEEP", 0.0)
    monkeypatch.setattr(rt_sms_inbound, "_SEEN_SIDS", OrderedDict())
    rt_sms_inbound.reset_sms_rate_limits()
    # No Supabase in tests: the bundle lookup returns nothing.
    monkeypatch.setattr(rt_prefs, "_req", lambda *a, **k: {})
    return tmp_path


class _FakeResp:
    def __init__(self, body: bytes, ctype: str = "image/jpeg", length: str | None = None):
        self._body = body
        self.headers = email.message.Message()
        self.headers["Content-Type"] = ctype
        self.headers["Content-Length"] = str(len(body)) if length is None else length

    def read(self, n=-1):
        return self._body if n is None or n < 0 else self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _redirect(to: str, code: int = 302):
    hdrs = email.message.Message()
    hdrs["Location"] = to
    return urllib.error.HTTPError("https://api.twilio.com/x", code, "Found", hdrs, io.BytesIO(b""))


class _Opener:
    """Records every request; answers from a scripted list."""

    def __init__(self, script):
        self.script = list(script)
        self.requests: list[tuple[str, dict]] = []

    def open(self, req, timeout=None):
        self.requests.append((req.full_url, {k.lower(): v for k, v in req.header_items()}))
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


# ── media ────────────────────────────────────────────────────────────────────

def test_media_credentials_ride_only_the_first_hop_to_api_twilio_com(lane, monkeypatch):
    opener = _Opener([_redirect("https://media.twiliocdn.example/abc?sig=1"),
                      _FakeResp(b"\xff\xd8jpegbytes")])
    monkeypatch.setattr(rt_sms_inbound, "_OPENER", opener)

    got = rt_sms_inbound.fetch_twilio_media(TWILIO_MEDIA)

    assert got == (b"\xff\xd8jpegbytes", "image/jpeg")
    first_url, first_headers = opener.requests[0]
    second_url, second_headers = opener.requests[1]
    assert first_url == TWILIO_MEDIA and first_headers["authorization"].startswith("Basic ")
    assert second_url == "https://media.twiliocdn.example/abc?sig=1"
    assert "authorization" not in second_headers


@pytest.mark.parametrize("url", [
    "https://evil.example/twilio.com",
    "https://api.twilio.com.evil.example/x",
    "https://evil.example/?u=api.twilio.com",
    "http://api.twilio.com/2010-04-01/x",
    "https://twilio.com/x",
    "",
])
def test_media_lookalike_hosts_never_get_a_request(lane, monkeypatch, url):
    opener = _Opener([])
    monkeypatch.setattr(rt_sms_inbound, "_OPENER", opener)
    assert rt_sms_inbound.fetch_twilio_media(url) is None
    assert opener.requests == []


def test_media_redirect_to_plain_http_is_refused(lane, monkeypatch):
    opener = _Opener([_redirect("http://media.twiliocdn.example/abc")])
    monkeypatch.setattr(rt_sms_inbound, "_OPENER", opener)
    assert rt_sms_inbound.fetch_twilio_media(TWILIO_MEDIA) is None
    assert len(opener.requests) == 1


def test_media_redirect_chain_is_bounded(lane, monkeypatch):
    hops = [_redirect(f"https://cdn{i}.example/x") for i in range(10)]
    opener = _Opener(hops)
    monkeypatch.setattr(rt_sms_inbound, "_OPENER", opener)
    assert rt_sms_inbound.fetch_twilio_media(TWILIO_MEDIA) is None
    assert len(opener.requests) == rt_sms_inbound._MEDIA_MAX_HOPS + 1


def test_media_over_the_size_cap_is_dropped_by_header_and_by_body(lane, monkeypatch):
    cap = rt_sms_inbound._MEDIA_MAX_BYTES
    monkeypatch.setattr(rt_sms_inbound, "_OPENER", _Opener([_FakeResp(b"x", length=str(cap + 1))]))
    assert rt_sms_inbound.fetch_twilio_media(TWILIO_MEDIA) is None

    monkeypatch.setattr(rt_sms_inbound, "_MEDIA_MAX_BYTES", 16)
    monkeypatch.setattr(rt_sms_inbound, "_OPENER", _Opener([_FakeResp(b"y" * 17, length="")]))
    assert rt_sms_inbound.fetch_twilio_media(TWILIO_MEDIA) is None
    monkeypatch.setattr(rt_sms_inbound, "_OPENER", _Opener([_FakeResp(b"y" * 16, length="")]))
    assert rt_sms_inbound.fetch_twilio_media(TWILIO_MEDIA) == (b"y" * 16, "image/jpeg")


def test_media_that_is_not_an_image_is_skipped(lane, monkeypatch):
    monkeypatch.setattr(rt_sms_inbound, "_OPENER", _Opener([_FakeResp(b"BEGIN:VCARD", ctype="text/vcard")]))
    assert rt_sms_inbound.fetch_twilio_media(TWILIO_MEDIA) is None


def test_media_http_error_and_transport_error_are_none(lane, monkeypatch):
    err = urllib.error.HTTPError(TWILIO_MEDIA, 404, "gone", email.message.Message(), io.BytesIO(b""))
    monkeypatch.setattr(rt_sms_inbound, "_OPENER", _Opener([err]))
    assert rt_sms_inbound.fetch_twilio_media(TWILIO_MEDIA) is None
    monkeypatch.setattr(rt_sms_inbound, "_OPENER", _Opener([TimeoutError("slow")]))
    assert rt_sms_inbound.fetch_twilio_media(TWILIO_MEDIA) is None


# ── process_incoming_sms ─────────────────────────────────────────────────────

def test_inbound_is_recorded_before_any_gate_and_no_reply_when_disabled(lane, monkeypatch):
    monkeypatch.setenv("RT_SMS_ENABLED", "0")
    reply = rt_sms_inbound.process_incoming_sms(TEXTER, "call me back", ["https://api.twilio.com/m"],
                                                message_sid="SM1")
    assert reply is None
    got = rt_sms.get_recent_sms(TEXTER)
    assert len(got) == 1 and got[0]["direction"] == "inbound"
    assert got[0]["text"] == "call me back" and got[0]["sid"] == "SM1"
    assert got[0]["media"] == ["https://api.twilio.com/m"]


def test_photo_only_text_is_recorded_as_a_photo(lane):
    rt_sms_inbound.process_incoming_sms(TEXTER, "", ["https://api.twilio.com/m"])
    assert rt_sms.get_recent_sms(TEXTER)[0]["text"] == "[Sent a photo]"


def test_canned_reply_without_a_google_key(lane):
    reply = rt_sms_inbound.process_incoming_sms(TEXTER, "hi")
    assert reply and "next time" in reply


def test_generated_reply_is_used_and_generation_failure_falls_back(lane, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    seen: dict = {}

    def _gen(api_key, system_prompt, user_text, media_parts=None):
        seen.update(api_key=api_key, prompt=system_prompt, text=user_text, media=media_parts)
        return "yup, see you Thursday!"
    monkeypatch.setattr(rt_sms_inbound, "_generate_sms_reply", _gen)
    monkeypatch.setattr(rt_sms_inbound, "fetch_twilio_media", lambda url: (b"img", "image/png"))

    reply = rt_sms_inbound.process_incoming_sms(TEXTER, "see you thursday?", [TWILIO_MEDIA])
    assert reply == "yup, see you Thursday!"
    assert seen["api_key"] == "k" and seen["text"] == "see you thursday?"
    assert seen["media"] == [(b"img", "image/png")]
    assert "RECENT CHAT THREAD" in seen["prompt"] and "see you thursday?" in seen["prompt"]

    def _boom(*a, **k):
        raise RuntimeError("quota")
    monkeypatch.setattr(rt_sms_inbound, "_generate_sms_reply", _boom)
    reply = rt_sms_inbound.process_incoming_sms(TEXTER, "again?")
    assert reply and "Got your message" in reply


def test_recent_photo_is_carried_forward_into_a_follow_up(lane, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    fetched: list[str] = []
    monkeypatch.setattr(rt_sms_inbound, "fetch_twilio_media", lambda url: fetched.append(url) or (b"i", "image/jpeg"))
    monkeypatch.setattr(rt_sms_inbound, "_generate_sms_reply", lambda *a, **k: "cute dog!")

    rt_sms_inbound.process_incoming_sms(TEXTER, "", [TWILIO_MEDIA])
    fetched.clear()
    rt_sms_inbound.process_incoming_sms(TEXTER, "is he wearing a collar?")
    assert fetched == [TWILIO_MEDIA]


def test_unparseable_number_is_ignored_entirely(lane, tmp_path):
    assert rt_sms_inbound.process_incoming_sms("not a number", "hi") is None
    assert rt_sms_inbound.process_incoming_sms("", "hi") is None
    assert not any(p.suffix == ".json" for p in (tmp_path / "ledger").iterdir()) if (tmp_path / "ledger").exists() else True


def test_logs_carry_neither_the_number_nor_the_body(lane, capsys):
    rt_sms_inbound.process_incoming_sms(TEXTER, "the biopsy came back clear", message_sid="SM9")
    out = capsys.readouterr().out
    assert TEXTER not in out and "5005550006" not in out
    assert "biopsy" not in out
    assert "***0006" in out


# ── accept_webhook / handle_incoming ─────────────────────────────────────────

def test_accept_webhook_queues_once_per_message_sid(lane, monkeypatch):
    queued: list[tuple] = []
    monkeypatch.setattr(rt_sms_inbound, "_dispatch", lambda fn, *args: queued.append(args))

    assert rt_sms_inbound.accept_webhook(TEXTER, OURS, "hi", [], "SM1") is True
    assert rt_sms_inbound.accept_webhook(TEXTER, OURS, "hi", [], "SM1") is False
    assert rt_sms_inbound.accept_webhook(TEXTER, OURS, "hi", [], "SM2") is True
    assert rt_sms_inbound.accept_webhook(TEXTER, OURS, "hi", [], "") is True, "no sid: nothing to dedupe on"
    assert [q[4] for q in queued] == ["SM1", "SM2", ""]
    assert queued[0][:4] == (TEXTER, OURS, "hi", [])


def test_accept_webhook_recognises_a_sid_already_in_the_ledger(lane, monkeypatch):
    """A worker restart empties the in-memory set; the ledger still knows."""
    queued: list[tuple] = []
    monkeypatch.setattr(rt_sms_inbound, "_dispatch", lambda fn, *args: queued.append(args))
    rt_sms.record_sms(TEXTER, "inbound", "hi", sid="SMold")
    assert rt_sms_inbound.accept_webhook(TEXTER, OURS, "hi", [], "SMold") is False
    assert queued == []


def test_handle_incoming_replies_from_the_number_they_texted(lane, monkeypatch):
    monkeypatch.setattr(rt_sms_inbound, "process_incoming_sms", lambda *a, **k: "got it!")
    sent: dict = {}

    def _send(to, body, from_number=None, media_url=None):
        sent.update(to=to, body=body, from_number=from_number)
        return {"error": False, "sid": "SMr", "message": "queued"}
    monkeypatch.setattr(rt_sms, "send_sms", _send)

    rt_sms_inbound.handle_incoming(TEXTER, OURS, "hi", [], "SM1")
    assert sent == {"to": TEXTER, "body": "got it!", "from_number": OURS}


def test_handle_incoming_sends_nothing_when_there_is_no_reply(lane, monkeypatch):
    monkeypatch.setattr(rt_sms_inbound, "process_incoming_sms", lambda *a, **k: None)
    monkeypatch.setattr(rt_sms, "send_sms", lambda *a, **k: pytest.fail("must not send"))
    rt_sms_inbound.handle_incoming(TEXTER, OURS, "hi", [], "SM1")


def test_handle_incoming_never_raises(lane, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("model down")
    monkeypatch.setattr(rt_sms_inbound, "process_incoming_sms", _boom)
    rt_sms_inbound.handle_incoming(TEXTER, OURS, "hi", [], "SM1")


def test_generate_sms_reply_payload_shape(lane, monkeypatch):
    captured: dict = {}

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
        captured["url"] = req.full_url
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        return _R(json.dumps({"candidates": [{"content": {"parts": [{"text": "\"haha yup\""}]}}]}).encode())
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)

    out = rt_sms_inbound._generate_sms_reply("key", "sys", "hello", [(b"ab", "image/png")])
    assert out == "haha yup"
    assert captured["headers"]["x-goog-api-key"] == "key"
    p = captured["payload"]
    assert p["tools"] == [{"googleSearch": {}}]
    assert p["contents"][0]["parts"][0] == {"text": "hello"}
    assert p["contents"][0]["parts"][1]["inline_data"] == {"mime_type": "image/png", "data": "YWI="}


def test_check_sms_rate_limit_sliding_window():
    """Verify sliding-window bucket limits calls per caller to protect against DoW."""
    caller_hash = "h_test_caller_abc123"
    rt_sms_inbound.reset_sms_rate_limits()

    t0 = 1000.0
    # First 5 calls allowed
    for i in range(5):
        assert rt_sms_inbound.check_sms_rate_limit(caller_hash, now=t0 + i) is True

    # 6th call within window rejected
    assert rt_sms_inbound.check_sms_rate_limit(caller_hash, now=t0 + 10.0) is False

    # After window (60s) expires from first timestamp, old timestamps roll off
    assert rt_sms_inbound.check_sms_rate_limit(caller_hash, now=t0 + 61.0) is True


def test_process_incoming_sms_rate_limiting(lane, monkeypatch):
    """Verify process_incoming_sms respects rate limit and skips LLM/carrier spend."""
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
    monkeypatch.setattr(rt_sms_inbound, "_generate_sms_reply", lambda *a, **k: "Warm reply")

    # Send 5 messages: all get replies
    for i in range(5):
        reply = rt_sms_inbound.process_incoming_sms(TEXTER, f"Message {i}")
        assert reply == "Warm reply"

    # 6th message: rate limited, returns None (no carrier/LLM spend)
    rate_limited_reply = rt_sms_inbound.process_incoming_sms(TEXTER, "Flood message 6")
    assert rate_limited_reply is None

    # Life safety invariant: crisis message is NEVER blocked by rate limits
    monkeypatch.setenv("AGENT_NAME", "pray-pal")
    crisis_reply = rt_sms_inbound.process_incoming_sms(TEXTER, "I want to kill myself")
    assert crisis_reply is not None
    assert "988" in crisis_reply


def test_tcpa_opt_out_and_resubscribe(lane):
    """Test TCPA/CTIA standard opt-out keywords, suppression, and opt-in restoration."""
    # Ensure fresh starting state
    rt_sms.set_opt_out(TEXTER, False)
    assert not rt_sms.is_opted_out(TEXTER)

    # 1. STOP keyword triggers opt out
    reply = rt_sms_inbound.process_incoming_sms(TEXTER, "STOP")
    assert reply == rt_sms_inbound.OPT_OUT_CONFIRMATION
    assert rt_sms.is_opted_out(TEXTER)

    # 2. Subsequent normal text from opted-out user is dropped silently
    assert rt_sms_inbound.process_incoming_sms(TEXTER, "Hello are you there?") is None

    # 3. HELP keyword still responds with disclosures
    help_reply = rt_sms_inbound.process_incoming_sms(TEXTER, "HELP")
    assert help_reply == rt_sms_inbound.HELP_RESPONSE

    # 4. START keyword reactivates subscription
    start_reply = rt_sms_inbound.process_incoming_sms(TEXTER, "START")
    assert start_reply == rt_sms_inbound.OPT_IN_CONFIRMATION
    assert not rt_sms.is_opted_out(TEXTER)


def test_tcpa_send_sms_refusal_when_opted_out(lane, monkeypatch):
    """send_sms refuses transmission if recipient is opted out, unless allow_opt_out_reply=True."""
    rt_sms.set_opt_out(TEXTER, True)
    res = rt_sms.send_sms(TEXTER, "Good morning prayer devotional")
    assert res["error"] is True
    assert "opted out" in res["message"]

    # When allow_opt_out_reply=True (e.g. sending the unsubscribe confirmation), delivery is permitted past the gate
    class _DummyResp:
        def read(self):
            return json.dumps({"sid": "SM_mock_ok", "status": "queued"}).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(rt_sms.urllib.request, "urlopen", lambda req, timeout=None: _DummyResp())
    res_allowed = rt_sms.send_sms(TEXTER, "Opt out confirmation", allow_opt_out_reply=True)
    assert res_allowed["error"] is False
    assert res_allowed["sid"] == "SM_mock_ok"

    # Cleanup
    rt_sms.set_opt_out(TEXTER, False)

