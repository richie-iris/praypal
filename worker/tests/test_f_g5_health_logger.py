"""test_f_g5_health_logger.py — Contract tests for hardening group G5.

Findings covered:
  #9  rt_health readiness checks (register_check / run_checks / GET /ready 503),
      "ready" flag on /health and /live, RT_DOCKER_HEALTH_PORT / RT_DOCKER_HEALTH_HOST
      with legacy HEALTH_PORT / HEALTH_HOST fallback.
  #15 rt_logger Sentry hardening (send_default_pii=False, include_local_variables=False,
      before_send=_scrub_event), _scrub_event / scrub_text redaction, LOG_LEVEL default INFO.
      Round 2: spaced / dotted / bare-digit phone forms, every non-structural Sentry section
      scrubbed (contexts, threads, stacktrace, tags, request), JsonLogger payload scrubbed
      before stdout AND before the LOG_SHIPPER queue / wire body.
  config: PROD_AGENT_NAMES, pepper_required(), missing_config() names RT_PHONE_HASH_PEPPER
      when the lane requires a pepper.

These tests are written test-first: they must FAIL on the pre-hardening code and pass
only once the contract is implemented exactly as written. No network, no live DB.
"""
from __future__ import annotations

import http.server
import importlib
import json
import queue
import socket
import threading
import urllib.error
import urllib.request

import pytest

import config
import rt_health
import rt_logger


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(url: str) -> tuple[int, dict]:
    """GET url; return (status, json_body) for both 2xx and HTTPError responses."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=3) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8")
        return err.code, (json.loads(raw) if raw.strip() else {})


class _Probe:
    """A readiness check whose outcome the test controls; always reset to healthy after use
    so leftover registrations never poison other test modules."""

    def __init__(self) -> None:
        self.ok = True
        self.raise_exc = False

    def __call__(self) -> bool:
        if self.raise_exc:
            raise RuntimeError("probe exploded")
        return self.ok

    def reset(self) -> None:
        self.ok = True
        self.raise_exc = False


@pytest.fixture(scope="module")
def health_base_url():
    """Start a fresh health server on an ephemeral port for this module only.

    Uses an explicit port so these tests don't depend on the env-resolution finding;
    the singleton globals are reset so a server started by another module is not reused.
    """
    mp = pytest.MonkeyPatch()
    mp.setattr(rt_health, "_SERVER_THREAD", None)
    mp.setattr(rt_health, "_SERVER_PORT", None)
    mp.setattr(rt_health, "_SERVER_HOST", None)
    port = _free_port()
    thread = rt_health.start_health_server(port=port, host="127.0.0.1")
    assert thread is not None, "could not bind an ephemeral health server for the test"
    yield f"http://127.0.0.1:{port}"
    mp.undo()


@pytest.fixture
def logger_env(monkeypatch):
    """Yield monkeypatch; afterwards restore rt_logger to the conftest baseline
    (LOG_LEVEL=DEBUG, no SENTRY_DSN) by reloading it, so later modules see the old state."""
    yield monkeypatch
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    importlib.reload(rt_logger)


# =========================================================================== #
# #9 — readiness checks
# =========================================================================== #
def test_f09_register_check_and_run_checks_report_each_check():
    probe = _Probe()
    probe.ok = False
    try:
        rt_health.register_check("g5_pass", lambda: True)
        rt_health.register_check("g5_fail", probe)
        results = rt_health.run_checks()
        assert isinstance(results, dict)
        assert results["g5_pass"] is True
        assert results["g5_fail"] is False
    finally:
        probe.reset()


def test_f09_run_checks_treats_raising_check_as_failed():
    probe = _Probe()
    probe.raise_exc = True
    try:
        rt_health.register_check("g5_raises", probe)
        results = rt_health.run_checks()  # must not propagate the exception
        assert results["g5_raises"] is False
    finally:
        probe.reset()


def test_f09_ready_returns_200_ok_when_all_checks_pass(health_base_url):
    probe = _Probe()
    try:
        rt_health.register_check("g5_all_good", probe)
        assert rt_health.run_checks()["g5_all_good"] is True
        status, body = _get(f"{health_base_url}/ready")
        assert status == 200
        assert body["status"] == "ok"
    finally:
        probe.reset()


def test_f09_ready_returns_503_degraded_with_failed_names(health_base_url):
    probe = _Probe()
    probe.ok = False
    try:
        rt_health.register_check("g5_db_down", probe)
        status, body = _get(f"{health_base_url}/ready")
        assert status == 503
        assert body["status"] == "degraded"
        assert "g5_db_down" in body["failed"]
    finally:
        probe.reset()
    # once the check recovers, /ready is 200 again (registry is live, not a snapshot)
    status, body = _get(f"{health_base_url}/ready")
    assert status == 200
    assert body["status"] == "ok"


def test_f09_ready_treats_raising_check_as_failed_over_http(health_base_url):
    probe = _Probe()
    probe.raise_exc = True
    try:
        rt_health.register_check("g5_boom", probe)
        status, body = _get(f"{health_base_url}/ready")
        assert status == 503
        assert body["status"] == "degraded"
        assert "g5_boom" in body["failed"]
    finally:
        probe.reset()


def test_f09_health_and_live_stay_200_but_carry_ready_flag(health_base_url):
    probe = _Probe()
    probe.ok = False
    try:
        rt_health.register_check("g5_liveness_probe", probe)
        for path in ("/health", "/live"):
            status, body = _get(f"{health_base_url}{path}")
            assert status == 200, f"{path} must stay a liveness 200 even when degraded"
            assert body["status"] == "ok"
            assert body["ready"] is False, f"{path} must report ready=False while a check fails"
    finally:
        probe.reset()
    for path in ("/health", "/live"):
        status, body = _get(f"{health_base_url}{path}")
        assert status == 200
        assert body["ready"] is True


def test_f09_port_prefers_rt_docker_health_port_then_legacy_then_8080(monkeypatch):
    monkeypatch.setattr(rt_health, "_SERVER_PORT", None)
    monkeypatch.delenv("RT_DOCKER_HEALTH_PORT", raising=False)
    monkeypatch.delenv("HEALTH_PORT", raising=False)
    assert rt_health.get_health_port() == 8080

    monkeypatch.setenv("HEALTH_PORT", "18282")
    assert rt_health.get_health_port() == 18282, "legacy HEALTH_PORT must still work as a fallback"

    monkeypatch.setenv("RT_DOCKER_HEALTH_PORT", "18181")
    assert rt_health.get_health_port() == 18181, "RT_DOCKER_HEALTH_PORT must win over legacy HEALTH_PORT"

    monkeypatch.delenv("HEALTH_PORT", raising=False)
    assert rt_health.get_health_port() == 18181


def test_f09_host_prefers_rt_docker_health_host_then_legacy_then_loopback(monkeypatch):
    monkeypatch.setattr(rt_health, "_SERVER_HOST", None)
    monkeypatch.delenv("RT_DOCKER_HEALTH_HOST", raising=False)
    monkeypatch.delenv("HEALTH_HOST", raising=False)
    assert rt_health.get_health_host() == "127.0.0.1"

    monkeypatch.setenv("HEALTH_HOST", "10.0.0.5")
    assert rt_health.get_health_host() == "10.0.0.5", "legacy HEALTH_HOST must still work as a fallback"

    monkeypatch.setenv("RT_DOCKER_HEALTH_HOST", "0.0.0.0")
    assert rt_health.get_health_host() == "0.0.0.0", "RT_DOCKER_HEALTH_HOST must win over legacy HEALTH_HOST"


def test_f09_start_health_server_binds_rt_docker_health_port(monkeypatch):
    port = _free_port()
    monkeypatch.setenv("RT_DOCKER_HEALTH_PORT", str(port))
    monkeypatch.setenv("RT_DOCKER_HEALTH_HOST", "127.0.0.1")
    monkeypatch.delenv("HEALTH_PORT", raising=False)
    monkeypatch.delenv("HEALTH_HOST", raising=False)
    monkeypatch.setattr(rt_health, "_SERVER_THREAD", None)
    monkeypatch.setattr(rt_health, "_SERVER_PORT", None)
    monkeypatch.setattr(rt_health, "_SERVER_HOST", None)

    thread = rt_health.start_health_server()
    assert thread is not None
    assert rt_health.get_health_port() == port
    status, body = _get(f"http://127.0.0.1:{port}/health")
    assert status == 200
    assert body["service"] == "phone-pal-worker"


# =========================================================================== #
# #15 — logger scrub + LOG_LEVEL default
# =========================================================================== #
_PHONES = ["+19734005897", "973-400-5897", "(973) 400-5897", "+15551234567"]


def test_f15_log_level_defaults_to_info(logger_env, capsys):
    logger_env.delenv("LOG_LEVEL", raising=False)
    logger_env.delenv("SENTRY_DSN", raising=False)
    importlib.reload(rt_logger)
    assert rt_logger.CURRENT_LOG_LEVEL == rt_logger.LEVELS["INFO"]

    log = rt_logger.get_logger("g5")
    log.debug("hidden at default level")
    assert capsys.readouterr().out.strip() == "", "DEBUG lines must not print at the INFO default"
    log.info("visible at default level")
    assert "visible at default level" in capsys.readouterr().out

    # explicit LOG_LEVEL still honoured
    logger_env.setenv("LOG_LEVEL", "DEBUG")
    importlib.reload(rt_logger)
    assert rt_logger.CURRENT_LOG_LEVEL == rt_logger.LEVELS["DEBUG"]


def test_f15_scrub_text_redacts_phones_and_emails_only():
    src = ("Meeting in room 12 at noon. Call +19734005897 or 973-400-5897 or (973) 400-5897; "
           "email Bob.Smith@example.com or jane_doe+tag@mail.example.org")
    out = rt_logger.scrub_text(src)
    assert isinstance(out, str)
    for phone in ("+19734005897", "973-400-5897", "(973) 400-5897", "4005897"):
        assert phone not in out
    assert "[phone]" in out
    assert "Bob.Smith@example.com" not in out
    assert "jane_doe+tag@mail.example.org" not in out
    assert "@" not in out
    assert "[email]" in out
    # ordinary text survives
    assert "Meeting in room 12 at noon" in out


def test_f15_scrub_event_redacts_recursively_and_drops_transcript_keys():
    event = {
        "message": "caller +19734005897 said hi, reply to a.b@x.org",
        "extra": {
            "note": "reach them at 973-400-5897",
            "transcript": "caller: my number is +15551234567",
            "transcript_lines": ["caller: hello", "agent: hi"],
            "message_body": "Dear Bob, call (973) 400-5897",
            "nested": {"text": "hello there", "phones": ["+15551234567", "(973) 400-5897"]},
        },
        "breadcrumbs": {
            "values": [
                {"message": "dialed (973) 400-5897", "data": {"body": "raw sms body", "to": "+15551234567"}},
            ]
        },
        "exception": {"values": [{"type": "ValueError", "value": "bad number +15551234567 for x@y.com"}]},
        "logentry": {"message": "sent to jane@doe.com", "params": ["+15551234567"]},
        "transaction": "keep-me",
    }
    out = rt_logger._scrub_event(event, {})
    assert isinstance(out, dict)
    dumped = json.dumps(out)

    # no raw phone digits or emails survive anywhere in the scrubbed event
    for phone in _PHONES:
        assert phone not in dumped
    for email in ("a.b@x.org", "x@y.com", "jane@doe.com"):
        assert email not in dumped
    assert "@" not in dumped

    # strings under message / extra / breadcrumbs / exception / logentry are scrubbed in place
    assert "[phone]" in out["message"] and "[email]" in out["message"]
    assert "[phone]" in out["extra"]["note"]
    assert "[phone]" in out["breadcrumbs"]["values"][0]["message"]
    assert out["breadcrumbs"]["values"][0]["data"]["to"] == "[phone]"
    assert "[phone]" in out["exception"]["values"][0]["value"]
    assert "[email]" in out["exception"]["values"][0]["value"]
    assert "[email]" in out["logentry"]["message"]
    assert out["logentry"]["params"] == ["[phone]"]

    # sensitive keys are dropped wholesale, at any depth, regardless of value type
    assert out["extra"]["transcript"] == "[redacted]"
    assert out["extra"]["transcript_lines"] == "[redacted]"
    assert out["extra"]["message_body"] == "[redacted]"
    assert out["extra"]["nested"]["text"] == "[redacted]"
    assert out["breadcrumbs"]["values"][0]["data"]["body"] == "[redacted]"

    # unrelated top-level fields are untouched
    assert out["transaction"] == "keep-me"


def test_f15_sentry_init_disables_pii_and_installs_scrubber(logger_env):
    sentry_sdk = pytest.importorskip("sentry_sdk")
    captured: dict = {}

    def fake_init(*args, **kwargs):
        captured.update(kwargs)
        if args:
            captured["_positional_dsn"] = args[0]

    logger_env.setattr(sentry_sdk, "init", fake_init)
    logger_env.setenv("SENTRY_DSN", "https://examplePublicKey@o0.ingest.sentry.io/0")
    logger_env.setenv("LOG_LEVEL", "DEBUG")
    importlib.reload(rt_logger)

    assert captured, "sentry_sdk.init must be called when SENTRY_DSN is set"
    assert rt_logger._SENTRY_INITIALIZED is True
    assert captured.get("send_default_pii") is False
    assert captured.get("include_local_variables") is False
    assert captured.get("before_send") is rt_logger._scrub_event

    # and the installed hook actually scrubs
    scrubbed = captured["before_send"]({"message": "ring +19734005897 / bob@example.com", "extra": {}}, {})
    assert "+19734005897" not in scrubbed["message"]
    assert "bob@example.com" not in scrubbed["message"]


# =========================================================================== #
# #15 round 2 — phone forms, whole-event scrub, JsonLogger / shipper redaction
# =========================================================================== #
_PHONE_FORMS = [
    "+19734005897",        # E.164
    "+1 973 400 5897",     # E.164 spaced
    "+1 (973) 400-5897",   # E.164 with parens
    "(973) 400-5897",      # (NNN) NNN-NNNN
    "973-400-5897",        # dashed
    "973 400 5897",        # spaced
    "973.400.5897",        # dotted
    "1-973-400-5897",      # leading 1
    "9734005897",          # bare 10 digits
    "19734005897",         # bare 11 digits
]


@pytest.mark.parametrize("phone", _PHONE_FORMS)
def test_f15_scrub_text_redacts_every_phone_form(phone):
    out = rt_logger.scrub_text(f"caller said their number is {phone} twice")
    assert phone not in out
    assert "4005897" not in out and "400 5897" not in out and "400.5897" not in out
    assert "[phone]" in out
    assert "caller said their number is" in out


def test_f15_scrub_text_leaves_ordinary_numbers_alone():
    src = "room 12 at noon, order 123456789, ts 2026-09-01T12:00:00Z, call-999, 45 minutes"
    assert rt_logger.scrub_text(src) == src


def test_f15_scrub_event_scrubs_contexts_threads_stacktrace_tags_request():
    event = {
        "event_id": "0123456789abcdef0123456789abcdef",
        "timestamp": 1756700000.0,
        "platform": "python",
        "sdk": {"name": "sentry.python", "version": "2.0.0"},
        "transaction": "keep-me",
        "tags": {"module": "agent", "caller": "+19734005897", "note": "email bob@example.com"},
        "contexts": {"call": {"from": "973 400 5897", "transcript": "caller: hi"}},
        "threads": {"values": [{"name": "MainThread", "crashed": False,
                                "stacktrace": {"frames": [{"context_line": "dial('973.400.5897')"}]}}]},
        "stacktrace": {"frames": [{"context_line": "send_sms('+1 973 400 5897', body)",
                                   "vars": {"body": "raw sms", "to": "9734005897"}}]},
        "request": {"url": "https://x/api?phone=19734005897", "data": {"text": "hello 973-400-5897"}},
        "user": {"id": "u1", "username": "+15551234567"},
        "spans": [{"description": "call (973) 400-5897"}],
    }
    out = rt_logger._scrub_event(event, {})
    assert isinstance(out, dict)
    dumped = json.dumps(out)
    for phone in _PHONE_FORMS + ["+15551234567"]:
        assert phone not in dumped, phone
    assert "4005897" not in dumped and "400 5897" not in dumped and "400.5897" not in dumped
    assert "bob@example.com" not in dumped and "@" not in dumped

    assert out["tags"]["caller"] == "[phone]"
    assert out["tags"]["module"] == "agent"
    assert out["contexts"]["call"]["from"] == "[phone]"
    assert out["contexts"]["call"]["transcript"] == "[redacted]"
    assert "[phone]" in out["threads"]["values"][0]["stacktrace"]["frames"][0]["context_line"]
    assert "[phone]" in out["stacktrace"]["frames"][0]["context_line"]
    assert out["stacktrace"]["frames"][0]["vars"]["body"] == "[redacted]"
    assert out["stacktrace"]["frames"][0]["vars"]["to"] == "[phone]"
    assert "[phone]" in out["request"]["url"]
    assert out["request"]["data"]["text"] == "[redacted]"
    assert out["user"]["username"] == "[phone]"
    assert "[phone]" in out["spans"][0]["description"]

    # structural bookkeeping survives byte-for-byte
    assert out["event_id"] == "0123456789abcdef0123456789abcdef"
    assert out["timestamp"] == 1756700000.0
    assert out["platform"] == "python"
    assert out["sdk"] == {"name": "sentry.python", "version": "2.0.0"}
    assert out["transaction"] == "keep-me"


def test_f15_scrub_event_drops_event_when_scrub_raises(monkeypatch):
    def boom(_v):
        raise RuntimeError("scrub exploded")
    monkeypatch.setattr(rt_logger, "_scrub_value", boom)
    assert rt_logger._scrub_event({"message": "+19734005897"}, {}) is None


def test_f15_scrub_value_redacts_phone_keys_wholesale():
    out = rt_logger._scrub_value({
        "caller_phone": "555-1234",      # 7-digit local: regex-blind, key is not
        "phone": 15551234567,            # int, not a str
        "phone_number": "ext 12",
        "from_number": "+19734005897",
        "call_id": "call-999",
        "caller_phone_hash": "deadbeef",
    })
    assert out["caller_phone"] == "[phone]"
    assert out["phone"] == "[phone]"
    assert out["phone_number"] == "[phone]"
    assert out["from_number"] == "[phone]"
    assert out["call_id"] == "call-999"
    assert out["caller_phone_hash"] == "deadbeef"


def test_f15_json_logger_stdout_is_redacted(logger_env, capsys):
    logger_env.setenv("LOG_LEVEL", "DEBUG")
    logger_env.delenv("SENTRY_DSN", raising=False)
    importlib.reload(rt_logger)

    log = rt_logger.get_logger("g5").bind(call_id="call-999", caller_phone="+15551234567")
    log.info("dialing 973 400 5897 for bob@example.com",
             step="greeting", transcript="caller: my number is 9734005897",
             note="try 973.400.5897", to="+1 (973) 400-5897")
    raw = capsys.readouterr().out.strip()
    data = json.loads(raw)

    for phone in _PHONE_FORMS + ["+15551234567"]:
        assert phone not in raw, phone
    assert "4005897" not in raw and "400 5897" not in raw and "400.5897" not in raw
    assert "bob@example.com" not in raw and "@" not in raw

    assert data["call_id"] == "call-999"
    assert data["step"] == "greeting"
    assert data["caller_phone"] == "[phone]"
    assert data["transcript"] == "[redacted]"
    assert data["to"] == "[phone]"
    assert "[phone]" in data["msg"] and "[email]" in data["msg"]
    assert "[phone]" in data["note"]


def test_f15_json_logger_stderr_error_and_exception_are_redacted(logger_env, capsys):
    logger_env.setenv("LOG_LEVEL", "DEBUG")
    logger_env.delenv("SENTRY_DSN", raising=False)
    importlib.reload(rt_logger)

    log = rt_logger.get_logger("g5")
    try:
        raise ValueError("bad number +19734005897 for jane@doe.com")
    except ValueError as exc:
        log.error("send failed to (973) 400-5897", exc_info=exc, recipient="jane@doe.com")
    raw = capsys.readouterr().err.strip()
    data = json.loads(raw)
    assert "+19734005897" not in raw and "(973) 400-5897" not in raw
    assert "jane@doe.com" not in raw and "@" not in raw
    assert data["level"] == "ERROR"
    assert data["exception_type"] == "ValueError"
    assert "[phone]" in data["exception_msg"] and "[email]" in data["exception_msg"]
    assert data["recipient"] == "[email]"


def test_f15_json_logger_shipper_queue_payload_is_redacted(logger_env, capsys):
    logger_env.setenv("LOG_LEVEL", "DEBUG")
    logger_env.delenv("SENTRY_DSN", raising=False)
    importlib.reload(rt_logger)
    q: queue.Queue = queue.Queue(maxsize=10)
    logger_env.setattr(rt_logger, "_log_queue", q)

    log = rt_logger.get_logger("g5").bind(caller_phone="+15551234567")
    log.warn("callback to 973 400 5897, cc bob@example.com",
             transcript_lines=["caller: 9734005897"], to="+1 973 400 5897")
    capsys.readouterr()  # stdout covered elsewhere; here the queue is what leaves the box
    item = q.get_nowait()
    dumped = json.dumps(item)
    for phone in _PHONE_FORMS + ["+15551234567"]:
        assert phone not in dumped, phone
    assert "4005897" not in dumped and "400 5897" not in dumped
    assert "bob@example.com" not in dumped and "@" not in dumped
    assert item["caller_phone"] == "[phone]"
    assert item["transcript_lines"] == "[redacted]"
    assert item["to"] == "[phone]"
    assert "[phone]" in item["msg"] and "[email]" in item["msg"]
    assert q.empty()


def test_f15_shipper_worker_wire_body_is_redacted(logger_env):
    """Even an item that reached the queue by some other path leaves the process scrubbed."""
    logger_env.delenv("SENTRY_DSN", raising=False)
    importlib.reload(rt_logger)
    sent: list[bytes] = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        sent.append(req.data)
        return _Resp()

    logger_env.setattr(rt_logger.urllib.request, "urlopen", fake_urlopen)
    logger_env.setattr(rt_logger, "_LOG_SHIPPER_URL", "https://logs.example.invalid/ingest")
    q: queue.Queue = queue.Queue()
    logger_env.setattr(rt_logger, "_log_queue", q)
    q.put({"msg": "raw +19734005897", "caller_phone": "973 400 5897", "transcript": "caller: hi"})
    q.put(None)  # sentinel: worker exits
    rt_logger._shipper_worker()

    assert len(sent) == 1
    body = sent[0].decode("utf-8")
    assert "+19734005897" not in body and "973 400 5897" not in body and "4005897" not in body
    wire = json.loads(body)
    assert wire["caller_phone"] == "[phone]"
    assert wire["transcript"] == "[redacted]"
    assert "[phone]" in wire["msg"]


def test_f15_json_logger_drops_record_when_scrub_raises(logger_env, capsys):
    logger_env.setenv("LOG_LEVEL", "DEBUG")
    logger_env.delenv("SENTRY_DSN", raising=False)
    importlib.reload(rt_logger)
    q: queue.Queue = queue.Queue()
    logger_env.setattr(rt_logger, "_log_queue", q)

    def boom(_v):
        raise RuntimeError("scrub exploded")
    logger_env.setattr(rt_logger, "_scrub_value", boom)
    rt_logger.get_logger("g5").info("caller +19734005897")  # must not raise
    captured = capsys.readouterr()
    assert "+19734005897" not in captured.out and "+19734005897" not in captured.err
    assert captured.out.strip() == ""
    assert q.empty()


# =========================================================================== #
# config — PROD_AGENT_NAMES, pepper_required, pepper in missing_config
# =========================================================================== #
def test_cfg_prod_agent_names_contract():
    assert config.PROD_AGENT_NAMES == ("iris-phone", "phone-pal-prod")
    assert isinstance(config.PROD_AGENT_NAMES, tuple)


@pytest.mark.parametrize("raw,expected", [
    ("1", True), ("true", True), ("TRUE", True), ("yes", True), (" Yes ", True),
    ("0", False), ("false", False), ("no", False), ("off", False), ("OFF", False), ("", False),
    ("  ", False),
    # Fail-closed: anything that is not an explicit "off" spelling means required.
    ("on", True), ("maybe", True), ("y", True), ("2", True), ("nope", True),
])
def test_cfg_pepper_required_truthiness(monkeypatch, raw, expected):
    monkeypatch.setenv("RT_REQUIRE_PEPPER", raw)
    assert config.pepper_required() is expected


def test_cfg_pepper_required_false_when_unset(monkeypatch):
    monkeypatch.delenv("RT_REQUIRE_PEPPER", raising=False)
    assert config.pepper_required() is False


def test_cfg_missing_config_names_pepper_only_when_required(monkeypatch):
    monkeypatch.delenv("RT_PHONE_HASH_PEPPER", raising=False)
    monkeypatch.delenv("RT_REQUIRE_PEPPER", raising=False)
    assert "RT_PHONE_HASH_PEPPER" not in config.missing_config()

    monkeypatch.setenv("RT_REQUIRE_PEPPER", "1")
    assert "RT_PHONE_HASH_PEPPER" in config.missing_config()

    monkeypatch.setenv("RT_PHONE_HASH_PEPPER", "   ")  # whitespace is not a pepper
    assert "RT_PHONE_HASH_PEPPER" in config.missing_config()

    monkeypatch.setenv("RT_PHONE_HASH_PEPPER", "secret-salt-1234")
    assert "RT_PHONE_HASH_PEPPER" not in config.missing_config()

    monkeypatch.delenv("RT_PHONE_HASH_PEPPER", raising=False)
    monkeypatch.setenv("RT_REQUIRE_PEPPER", "0")
    assert "RT_PHONE_HASH_PEPPER" not in config.missing_config()


def test_cfg_missing_config_keeps_required_entries_with_pepper(monkeypatch):
    for name in config.REQUIRED:
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("RT_REQUIRE_PEPPER", "true")
    monkeypatch.delenv("RT_PHONE_HASH_PEPPER", raising=False)
    monkeypatch.setenv("LIVEKIT_URL", "")
    missing = config.missing_config()
    assert missing == ["LIVEKIT_URL", "RT_PHONE_HASH_PEPPER"]


def test_cfg_validate_startup_config_surfaces_pepper(monkeypatch, capsys):
    for name in config.REQUIRED:
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("RT_REQUIRE_PEPPER", "yes")
    monkeypatch.delenv("RT_PHONE_HASH_PEPPER", raising=False)
    config.validate_startup_config()
    err = capsys.readouterr().err
    assert "RT_PHONE_HASH_PEPPER is missing" in err


def test_cfg_readiness_config_check_fails_without_required_pepper(monkeypatch):
    """The 'config' readiness check agent.py registers is `not config.missing_config()`."""
    for name in config.REQUIRED:
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("RT_REQUIRE_PEPPER", "1")
    monkeypatch.delenv("RT_PHONE_HASH_PEPPER", raising=False)
    try:
        rt_health.register_check("g5_config", lambda: not config.missing_config())
        assert rt_health.run_checks()["g5_config"] is False
        monkeypatch.setenv("RT_PHONE_HASH_PEPPER", "secret-salt-1234")
        assert rt_health.run_checks()["g5_config"] is True
    finally:
        rt_health.register_check("g5_config", lambda: True)


# =========================================================================== #
# round 3 — config.pepper_ok / fail-closed pepper_required / comment-valued pepper
# =========================================================================== #
@pytest.mark.parametrize("value,ok", [
    (None, False), ("", False), ("   ", False),
    ("# REQUIRED", False), ("#abcdefghijklmnopqrstuvwxyz", False),   # template comment read as value
    ("short-pepper", False), ("fifteen-chars-x", False),                # < 16 chars
    ("REPLACE-ME-WITH-A-REAL-SECRET", False), ("example-pepper-value-1", False),
    ("ChangeMe-please-now-1", False), ("todo-set-this-later-1", False),
    ("xxxxxxxxxxxxxxxxxxxx", False),                                    # placeholders, any case
    ("secret-salt-1234", True), ("  padded-pepper-value-9  ", True),
    ("Zq8!vB2#kL9@mN4$pR7", True),
])
def test_cfg_pepper_ok_matrix(value, ok):
    assert config.pepper_ok(value) is ok


def test_cfg_pepper_problem_names_the_failed_condition():
    assert config.pepper_problem("") == "empty"
    assert config.pepper_problem("# REQUIRED") == "starts with #"
    assert "too short" in config.pepper_problem("tiny")
    assert "placeholder" in config.pepper_problem("changeme-changeme-changeme")
    assert config.pepper_problem("secret-salt-1234") is None


@pytest.mark.parametrize("raw", ["yes", "TRUE", "maybe", "1", "on", "y", "required", "0x1"])
def test_cfg_pepper_required_fail_closed_unknown_means_required(monkeypatch, raw):
    monkeypatch.setenv("RT_REQUIRE_PEPPER", raw)
    assert config.pepper_required() is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "", " False ", "NO", "Off"])
def test_cfg_pepper_required_only_explicit_off_spellings_disable(monkeypatch, raw):
    monkeypatch.setenv("RT_REQUIRE_PEPPER", raw)
    assert config.pepper_required() is False


@pytest.mark.parametrize("bad", ["# REQUIRED", "#", "changeme", "REPLACE_ME_WITH_32_RANDOM_BYTES",
                                 "short", "xxxxxxxxxxxxxxxxxxxxxxxx"])
def test_cfg_missing_config_rejects_unusable_pepper(monkeypatch, bad):
    for name in config.REQUIRED:
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("RT_REQUIRE_PEPPER", "1")
    monkeypatch.setenv("RT_PHONE_HASH_PEPPER", bad)
    assert config.missing_config() == ["RT_PHONE_HASH_PEPPER"]
    monkeypatch.setenv("RT_PHONE_HASH_PEPPER", "a-real-pepper-of-32-random-bytes")
    assert config.missing_config() == []


def test_cfg_unusable_pepper_is_fine_when_not_required(monkeypatch):
    for name in config.REQUIRED:
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("RT_REQUIRE_PEPPER", "off")
    monkeypatch.setenv("RT_PHONE_HASH_PEPPER", "# REQUIRED")
    assert config.missing_config() == []


@pytest.mark.parametrize("bad,reason", [
    ("", "empty"), ("# REQUIRED", "starts with #"), ("tiny", "too short"),
    ("changeme-changeme-changeme", "placeholder"),
])
def test_cfg_validate_startup_config_says_which_pepper_condition_failed(monkeypatch, capsys, bad, reason):
    for name in config.REQUIRED:
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("RT_REQUIRE_PEPPER", "yes")
    monkeypatch.setenv("RT_PHONE_HASH_PEPPER", bad)
    config.validate_startup_config()
    err = capsys.readouterr().err
    assert "RT_PHONE_HASH_PEPPER is missing" in err
    assert reason in err
    assert bad not in err or bad == ""   # the rejected value itself never hits the log


def test_cfg_readiness_config_check_fails_on_comment_valued_pepper(monkeypatch):
    for name in config.REQUIRED:
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("RT_REQUIRE_PEPPER", "1")
    monkeypatch.setenv("RT_PHONE_HASH_PEPPER", "# REQUIRED")
    try:
        rt_health.register_check("g5_config_r3", lambda: not config.missing_config())
        assert rt_health.run_checks()["g5_config_r3"] is False
        monkeypatch.setenv("RT_PHONE_HASH_PEPPER", "secret-salt-1234")
        assert rt_health.run_checks()["g5_config_r3"] is True
    finally:
        rt_health.unregister_check("g5_config_r3")


# =========================================================================== #
# round 3 — rt_logger: extra phone keys, numeric values with >= 10 digits
# =========================================================================== #
@pytest.mark.parametrize("key", ["e164", "number", "bridge_number", "caller_e164", "phone",
                                 "to_e164", "from_number", "caller_phone", "phone_number",
                                 "caller_number", "to_number"])
def test_f15_scrub_value_redacts_every_phone_key(key):
    out = rt_logger._scrub_value({key: "ext 12", "nested": [{key: 5551234}]})
    assert out[key] == "[phone]"
    assert out["nested"][0][key] == "[phone]"


@pytest.mark.parametrize("value", [15551234567, 9734005897, 19734005897, 447911123456,
                                   -15551234567, 15551234567.0])
def test_f15_scrub_value_redacts_numeric_values_with_ten_plus_digits(value):
    assert rt_logger._scrub_value(value) == "[phone]"
    assert rt_logger._scrub_value({"call_meta": {"dialed": value}}) == {"call_meta": {"dialed": "[phone]"}}
    assert rt_logger._scrub_value([value]) == ["[phone]"]


@pytest.mark.parametrize("value", [0, 42, 555123456, True, False, 3.14, 1756700000.25, None])
def test_f15_scrub_value_leaves_short_numbers_bools_and_fractions_alone(value):
    assert rt_logger._scrub_value({"n": value})["n"] == value


def test_f15_scrub_value_keeps_epoch_timestamps_under_time_keys():
    ev = {"spans": [{"timestamp": 1756700000, "start_timestamp": 1756699999.0, "dialed": 15551234567}],
          "ts": 1756700000}
    out = rt_logger._scrub_value(ev)
    assert out["spans"][0]["timestamp"] == 1756700000
    assert out["spans"][0]["start_timestamp"] == 1756699999.0
    assert out["spans"][0]["dialed"] == "[phone]"
    assert out["ts"] == 1756700000


def test_f15_json_logger_redacts_new_keys_and_int_phones_on_stdout(logger_env, capsys):
    logger_env.setenv("LOG_LEVEL", "DEBUG")
    importlib.reload(rt_logger)
    log = rt_logger.JsonLogger("g5").bind(bridge_number="+19734005897", e164="+15551234567")
    log.info("dial", to_e164="+15559876543", number=5551234, dialed=15551234567, call_id="c-1")
    out = capsys.readouterr().out
    rec = json.loads(out.strip().splitlines()[-1])
    assert rec["bridge_number"] == rec["e164"] == rec["to_e164"] == rec["number"] == "[phone]"
    assert rec["dialed"] == "[phone]"
    assert rec["call_id"] == "c-1"
    assert "9734005897" not in out and "5551234567" not in out and "5559876543" not in out


# =========================================================================== #
# round 3 — rt_health: the db verdict is cached 5 s; everything else stays live
# =========================================================================== #
def test_f09_db_check_verdict_is_cached_for_five_seconds(monkeypatch):
    assert rt_health.DEFAULT_CHECK_TTLS["db"] == 5.0
    clock = [1000.0]
    monkeypatch.setattr(rt_health.time, "monotonic", lambda: clock[0])
    calls = []

    def probe():
        calls.append(1)
        return len(calls) == 1          # healthy once, then down
    try:
        rt_health.register_check("db", probe)
        assert rt_health.run_checks()["db"] is True
        clock[0] += 4.9
        assert rt_health.run_checks()["db"] is True      # cached: the probe was not re-run
        assert len(calls) == 1
        clock[0] += 0.2                                  # 5.1 s after the first probe
        assert rt_health.run_checks()["db"] is False     # expired: re-evaluated live
        assert len(calls) == 2
    finally:
        rt_health.unregister_check("db")
    assert "db" not in rt_health.run_checks()


def test_f09_unnamed_checks_are_never_cached(monkeypatch):
    clock = [50.0]
    monkeypatch.setattr(rt_health.time, "monotonic", lambda: clock[0])
    probe = _Probe()
    try:
        rt_health.register_check("g5_live", probe)
        assert rt_health.run_checks()["g5_live"] is True
        probe.ok = False
        assert rt_health.run_checks()["g5_live"] is False    # same instant, still live
        probe.ok = True
        assert rt_health.run_checks()["g5_live"] is True
    finally:
        rt_health.unregister_check("g5_live")


def test_f09_explicit_ttl_caches_and_reregister_drops_the_cache(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(rt_health.time, "monotonic", lambda: clock[0])
    probe = _Probe()
    try:
        rt_health.register_check("g5_ttl", probe, ttl=2)
        assert rt_health.run_checks()["g5_ttl"] is True
        probe.ok = False
        assert rt_health.run_checks()["g5_ttl"] is True      # held by the 2 s ttl
        rt_health.register_check("g5_ttl", probe, ttl=2)     # re-register: cache dropped
        assert rt_health.run_checks()["g5_ttl"] is False
        probe.raise_exc = True
        clock[0] += 3
        assert rt_health.run_checks()["g5_ttl"] is False     # a raising check fails, and is cached
        probe.reset()
        assert rt_health.run_checks()["g5_ttl"] is False     # healthy again, verdict still held
        clock[0] += 3
        assert rt_health.run_checks()["g5_ttl"] is True
    finally:
        rt_health.unregister_check("g5_ttl")
