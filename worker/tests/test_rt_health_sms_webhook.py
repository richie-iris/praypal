"""test_rt_health_sms_webhook.py — /sms/incoming end to end over a real socket."""
from __future__ import annotations

import http.server
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict

import pytest

import rt_health
import rt_sms
import rt_sms_inbound

PEPPER = "test-pepper-0123456789abcdef-long-enough"
TOKEN = "tok-test-not-real"  # noqa: S105 - dummy value for the stubbed lane
PUBLIC_URL = "https://lane.example/sms/incoming"


def _free_port() -> int:
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        return sk.getsockname()[1]


@pytest.fixture(scope="module")
def base_url():
    mp = pytest.MonkeyPatch()
    mp.setattr(rt_health, "_SERVER_THREAD", None)
    mp.setattr(rt_health, "_SERVER_PORT", None)
    mp.setattr(rt_health, "_SERVER_HOST", None)
    port = _free_port()
    thread = rt_health.start_health_server(port=port, host="127.0.0.1")
    assert thread is not None, "could not bind an ephemeral health server"
    yield f"http://127.0.0.1:{port}"
    mp.undo()


@pytest.fixture
def lane(tmp_path, monkeypatch):
    monkeypatch.setenv("RT_SMS_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("RT_PHONE_HASH_PEPPER", PEPPER)
    monkeypatch.delenv("RT_REQUIRE_PEPPER", raising=False)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("RT_SMS_WEBHOOK_URL", PUBLIC_URL)
    monkeypatch.setattr(rt_sms, "_HISTORY", {})
    monkeypatch.setattr(rt_sms_inbound, "_SEEN_SIDS", OrderedDict())
    queued: list[tuple] = []
    monkeypatch.setattr(rt_sms_inbound, "_dispatch", lambda fn, *args: queued.append(args))
    return queued


def _post(base_url, path, params, headers=None):
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(f"{base_url}{path}", data=data, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded",
                                          **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode("utf-8"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8"), dict(e.headers)


FORM = {"From": "+15005550006", "To": "+15005550001", "Body": "",
        "NumMedia": "1", "MediaUrl0": "https://api.twilio.com/m/ME1", "MessageSid": "SM1"}


def test_server_handles_requests_on_threads():
    """Serial HTTPServer let one slow text hold /ready for the whole container."""
    assert issubclass(rt_health._Server, http.server.ThreadingHTTPServer)
    assert rt_health._Server.daemon_threads is True


def test_unsigned_post_is_refused_and_nothing_is_queued(base_url, lane):
    status, body, _ = _post(base_url, "/sms/incoming", FORM)
    assert status == 403 and lane == []


def test_forged_signature_is_refused(base_url, lane):
    forged = rt_sms.twilio_signature("wrong-token", PUBLIC_URL, FORM)
    status, _, _ = _post(base_url, "/sms/incoming", FORM, {"X-Twilio-Signature": forged})
    assert status == 403 and lane == []

    tampered = {**FORM, "From": "+15005550099"}
    real = rt_sms.twilio_signature(TOKEN, PUBLIC_URL, FORM)
    status, _, _ = _post(base_url, "/sms/incoming", tampered, {"X-Twilio-Signature": real})
    assert status == 403 and lane == []


def test_signed_post_is_acknowledged_empty_and_queued(base_url, lane):
    """`Body=` is blank on a photo-only MMS and is part of what Twilio signs;
    dropping blank fields (parse_qs's default) would fail every such text."""
    sig = rt_sms.twilio_signature(TOKEN, PUBLIC_URL, FORM)
    status, body, headers = _post(base_url, "/sms/incoming", FORM, {"X-Twilio-Signature": sig})
    assert status == 200
    assert body.strip().endswith("<Response/>")
    assert headers.get("Content-Type", "").startswith("application/xml")
    assert lane == [("+15005550006", "+15005550001", "", ["https://api.twilio.com/m/ME1"], "SM1")]


def test_url_is_rebuilt_from_forwarded_headers_when_not_configured(base_url, lane, monkeypatch):
    monkeypatch.delenv("RT_SMS_WEBHOOK_URL", raising=False)
    sig = rt_sms.twilio_signature(TOKEN, "https://lane.example/sms/incoming?lane=dev", FORM)
    status, _, _ = _post(base_url, "/sms/incoming?lane=dev", FORM,
                         {"X-Twilio-Signature": sig, "X-Forwarded-Proto": "https",
                          "X-Forwarded-Host": "lane.example"})
    assert status == 200 and len(lane) == 1

    # Twilio's own validator accepts the URL with or without an explicit :443.
    sig443 = rt_sms.twilio_signature(TOKEN, "https://lane.example:443/sms/incoming", {**FORM, "MessageSid": "SM2"})
    status, _, _ = _post(base_url, "/sms/incoming", {**FORM, "MessageSid": "SM2"},
                         {"X-Twilio-Signature": sig443, "X-Forwarded-Proto": "https",
                          "X-Forwarded-Host": "lane.example"})
    assert status == 200 and len(lane) == 2


def test_no_auth_token_refuses_everything(base_url, lane, monkeypatch):
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    sig = rt_sms.twilio_signature(TOKEN, PUBLIC_URL, FORM)
    status, _, _ = _post(base_url, "/sms/incoming", FORM, {"X-Twilio-Signature": sig})
    assert status == 403 and lane == []


def test_retried_delivery_is_acknowledged_but_not_queued_twice(base_url, lane):
    sig = rt_sms.twilio_signature(TOKEN, PUBLIC_URL, FORM)
    for _ in range(3):
        status, _, _ = _post(base_url, "/sms/incoming", FORM, {"X-Twilio-Signature": sig})
        assert status == 200
    assert len(lane) == 1


def test_oversized_body_is_refused_before_parsing(base_url, lane):
    big = {**FORM, "Body": "x" * (rt_health.MAX_WEBHOOK_BYTES + 10)}
    sig = rt_sms.twilio_signature(TOKEN, PUBLIC_URL, big)
    status, _, _ = _post(base_url, "/sms/incoming", big, {"X-Twilio-Signature": sig})
    assert status == 413 and lane == []


def test_unknown_post_path_is_404_and_health_still_answers(base_url, lane):
    status, _, _ = _post(base_url, "/nope", FORM)
    assert status == 404
    with urllib.request.urlopen(f"{base_url}/health", timeout=3) as r:
        assert r.status == 200 and json.loads(r.read())["service"] == "phone-pal-worker"
