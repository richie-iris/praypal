"""test_f_g4_prefs_bridge_http.py — contract tests for hardening findings #4, #14, #21.

Group G4: rt_prefs.py / rt_bridge.py / rt_http.py.

  #4  phone_hash: RT_REQUIRE_PEPPER=1 + no pepper -> RuntimeError naming RT_PHONE_HASH_PEPPER;
      pepper -> HMAC-SHA256 unchanged; unpeppered fallback prints a ONE-TIME "unpeppered"
      warning tracked by the module global rt_prefs._WARNED_UNPEPPERED.
  #14 check_and_record_dial: a ledger read that raises FAILS CLOSED (DialRefused, spoken
      wording, no ledger write); rt_bridge.MAX_BRIDGES_PER_CALL == 4.
  #21 rt_prefs._req routes through rt_http.PooledHttpClient.request; transient failures
      (non-HTTP URLError, socket timeout, HTTP 502/503/504) are retried up to 3 total
      attempts with backoff 0.3s then 0.9s via a monkeypatchable module-level _SLEEP;
      HTTP 4xx and other 5xx raise immediately; success still returns parsed JSON or None.

No network, no live DB: the socket layer is stubbed at urllib.request.urlopen AND
urllib.request.OpenerDirector.open so both the legacy path and rt_http's pooled opener land
on the same scripted outcome sequence.
"""
from __future__ import annotations

import email.message
import hashlib
import hmac
import io
import json
import socket
import time
import urllib.error
import urllib.request

import pytest

import rt_bridge
import rt_carrier
import rt_http
import rt_prefs


@pytest.fixture(autouse=True)
def _carrier_cap_open(monkeypatch):
    """check_and_record_dial asks Twilio what today has cost before it reads the
    ledger. These are the LEDGER's tests, so the carrier gate is held open and
    never reaches the network; test_carrier_cap_gate.py owns the gate itself."""
    monkeypatch.setattr(rt_carrier, "outbound_allowed",
                        lambda fresh=False: (True, "under_cap", {"spend_usd": 0.0, "cap_usd": 25.0}))

_URL = "https://testprojectref.supabase.co"
_KEY = "test-service-role-key"
_E164 = "+15551234567"
_SHA = hashlib.sha256(_E164.encode()).hexdigest()
_PEPPER = "secret-salt-123-long-enough-to-key"
_PEPPER2 = "other-salt-that-is-also-long"


# ─── shared stubs ────────────────────────────────────────────────────────────

class _FakeResponse:
    """Minimal stand-in for http.client.HTTPResponse as urllib hands it back."""

    def __init__(self, body: bytes = b"", status: int = 200,
                 content_type: str = "application/json; charset=utf-8"):
        self._body = body
        self.status = status
        self.code = status
        self.headers = email.message.Message()
        self.headers["Content-Type"] = content_type

    def read(self, *_a) -> bytes:
        return self._body

    def getcode(self) -> int:
        return self.status

    def getheader(self, name: str, default=None):
        return self.headers.get(name, default)

    def info(self):
        return self.headers

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _NetworkExhausted(AssertionError):
    """Raised when code under test opens the socket more times than scripted."""


def _http_error(code: int) -> urllib.error.HTTPError:
    hdrs = email.message.Message()
    hdrs["Content-Type"] = "application/json"
    return urllib.error.HTTPError(f"{_URL}/rest/v1/rpc/rt_get_caller", code, f"HTTP {code}",
                                  hdrs, io.BytesIO(b'{"message": "boom"}'))


def _wire_network(monkeypatch, outcomes: list) -> list:
    """Script the socket layer. Each open consumes one outcome: an exception is raised,
    anything else is returned as the response. Returns the list of Request objects seen."""
    seq = list(outcomes)
    attempts: list = []

    def _next(req):
        attempts.append(req)
        if not seq:
            raise _NetworkExhausted(
                f"opened the network {len(attempts)} times but only "
                f"{len(outcomes)} outcome(s) were scripted")
        out = seq.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, *a, **k: _next(req))
    monkeypatch.setattr(urllib.request.OpenerDirector, "open",
                        lambda self, req, *a, **k: _next(req))
    return attempts


def _patch_sleep(monkeypatch) -> list:
    """Capture the retry backoff. The contract requires a module-level _SLEEP (= time.sleep)
    that tests can monkeypatch; the retry loop lives in rt_http (rt_prefs may re-export it)."""
    sleeps: list = []
    found = False
    for mod in (rt_http, rt_prefs):
        if hasattr(mod, "_SLEEP"):
            monkeypatch.setattr(mod, "_SLEEP", lambda s: sleeps.append(s))
            found = True
    if not found:
        raise AttributeError(
            "contract #21: neither rt_http nor rt_prefs defines a module-level _SLEEP")
    # Any leftover direct time.sleep must not stall the suite — and must not be counted.
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)
    return sleeps


@pytest.fixture
def db_env(monkeypatch):
    """Make rt_prefs._db() return a usable (url, key) without touching a real project."""
    monkeypatch.setenv("SUPABASE_URL", _URL)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", _KEY)
    monkeypatch.setattr(rt_prefs, "ALLOWED_REFS", {"testprojectref"})
    assert rt_prefs._db() == (_URL, _KEY), "fixture: _db() must accept the test project"
    return _URL, _KEY


def _bridge_db(monkeypatch) -> None:
    """check_and_record_dial refuses before its first RPC unless rt_prefs._db() says a
    database is configured; the fake-_req bridge tests need that gate open."""
    monkeypatch.setattr(rt_prefs, "_db", lambda: (_URL, _KEY))


def _body_of(data) -> dict | None:
    if data is None:
        return None
    if isinstance(data, (bytes, bytearray)):
        return json.loads(data.decode("utf-8"))
    if isinstance(data, str):
        return json.loads(data)
    return data


# ─── #4  phone_hash pepper policy ────────────────────────────────────────────

@pytest.mark.parametrize("env_pepper", [None, "", "   "])
def test_f04_require_pepper_raises_when_pepper_missing(monkeypatch, env_pepper):
    if env_pepper is None:
        monkeypatch.delenv("RT_PHONE_HASH_PEPPER", raising=False)
    else:
        monkeypatch.setenv("RT_PHONE_HASH_PEPPER", env_pepper)
    monkeypatch.setenv("RT_REQUIRE_PEPPER", "1")
    with pytest.raises(RuntimeError, match="RT_PHONE_HASH_PEPPER"):
        rt_prefs.phone_hash(_E164)


def test_f04_require_pepper_explicit_empty_pepper_arg_raises(monkeypatch):
    # The effective pepper is the argument when given, so pepper="" is "no pepper"
    # even when the env has one — and under RT_REQUIRE_PEPPER=1 that is refused.
    monkeypatch.setenv("RT_PHONE_HASH_PEPPER", "env-pepper")
    monkeypatch.setenv("RT_REQUIRE_PEPPER", "1")
    with pytest.raises(RuntimeError, match="RT_PHONE_HASH_PEPPER"):
        rt_prefs.phone_hash(_E164, pepper="")


def test_f04_require_pepper_with_pepper_is_hmac_unchanged(monkeypatch, capsys):
    # Real-length peppers: a required lane refuses anything under 16 chars (round 3).
    monkeypatch.setenv("RT_PHONE_HASH_PEPPER", _PEPPER)
    monkeypatch.setenv("RT_REQUIRE_PEPPER", "1")
    expected_env = hmac.new(_PEPPER.encode(), _E164.encode(), hashlib.sha256).hexdigest()
    expected_arg = hmac.new(_PEPPER2.encode(), _E164.encode(), hashlib.sha256).hexdigest()
    assert rt_prefs.phone_hash("555-123-4567") == expected_env
    assert rt_prefs.phone_hash(_E164, pepper=_PEPPER2) == expected_arg
    out = capsys.readouterr()
    assert "unpeppered" not in (out.out + out.err).lower()


@pytest.mark.parametrize("require", [None, "0"])
def test_f04_unpeppered_fallback_warns_exactly_once(monkeypatch, capsys, require):
    monkeypatch.delenv("RT_PHONE_HASH_PEPPER", raising=False)
    if require is None:
        monkeypatch.delenv("RT_REQUIRE_PEPPER", raising=False)
    else:
        monkeypatch.setenv("RT_REQUIRE_PEPPER", require)
    # Module global that makes the warning one-time; reset so this test owns the first hit.
    monkeypatch.setattr(rt_prefs, "_WARNED_UNPEPPERED", False)

    assert rt_prefs.phone_hash(_E164) == _SHA  # SHA-256 fallback unchanged
    out = capsys.readouterr()
    text = out.out + out.err
    assert sum("unpeppered" in ln.lower() for ln in text.splitlines()) == 1, text
    assert rt_prefs._WARNED_UNPEPPERED is True

    assert rt_prefs.phone_hash("555-123-4567") == _SHA
    out = capsys.readouterr()
    assert "unpeppered" not in (out.out + out.err).lower()


# ─── #14 bridge ledger fails closed + per-call cap constant ──────────────────

def test_f14_max_bridges_per_call_constant():
    assert rt_bridge.MAX_BRIDGES_PER_CALL == 4
    assert isinstance(rt_bridge.MAX_BRIDGES_PER_CALL, int)
    assert rt_bridge.MAX_BRIDGES_PER_CALL <= rt_bridge.MAX_BRIDGES_PER_DAY


@pytest.mark.parametrize("boom", [
    RuntimeError("db down: connection refused"),
    urllib.error.URLError("name resolution failed"),
    TimeoutError("timed out"),
])
def test_f14_ledger_read_error_refuses_dial_and_never_writes(monkeypatch, boom):
    calls: list = []

    def fake_req(method, path, body=None, *a, **kw):
        calls.append((method, path, body))
        if "rt_get_caller_full_bundle" in path:
            raise boom
        return {}

    _bridge_db(monkeypatch)
    monkeypatch.setattr(rt_prefs, "_req", fake_req)
    h = "a" * 64
    with pytest.raises(rt_bridge.DialRefused) as ei:
        rt_bridge.check_and_record_dial(h, "+19175551234")

    msg = str(ei.value)
    # Spoken-style: a real sentence for the caller's ear, not a leaked exception.
    assert len(msg.split()) >= 5, msg
    assert type(boom).__name__ not in msg
    assert str(boom) not in msg
    # FAIL CLOSED: no dial is recorded on top of an empty ledger.
    writes = [c for c in calls if "rt_add_schema_entry" in c[1]]
    assert writes == []


def test_f14_ledger_read_ok_still_records_dial(monkeypatch):
    calls: list = []

    def fake_req(method, path, body=None, *a, **kw):
        calls.append((method, path, body))
        if "rt_get_caller_full_bundle" in path:
            return {"schemas": []}
        return {}

    _bridge_db(monkeypatch)
    monkeypatch.setattr(rt_prefs, "_req", fake_req)
    h = "b" * 64
    rt_bridge.check_and_record_dial(h, "+19175551234")
    writes = [c for c in calls if "rt_add_schema_entry" in c[1]]
    assert len(writes) == 1
    body = writes[0][2]
    assert body["p_hash"] == h and body["p_cat"] == rt_bridge.BRIDGE_CATEGORY
    ledger = json.loads(body["p_summary"])
    today = ledger[rt_bridge._today()]
    assert today["count"] == 1 and today["numbers"] == ["+19175551234"]


# ─── #21 _req routes through rt_http with bounded, classified retries ────────

def test_f21_req_routes_through_rt_http_client(monkeypatch, db_env):
    url, key = db_env
    seen: list = []

    def fake_request(self, method, url_, headers=None, data=None, *a, **kw):
        seen.append({"method": method, "url": url_, "headers": dict(headers or {}), "data": data})
        return {"call_count": 7}

    monkeypatch.setattr(rt_http.PooledHttpClient, "request", fake_request)

    def _bypass(*_a, **_k):
        raise AssertionError("rt_prefs._req bypassed rt_http and opened the socket directly")

    monkeypatch.setattr(urllib.request, "urlopen", _bypass)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", _bypass)

    res = rt_prefs._req("POST", "rpc/rt_get_caller", {"p_hash": "abc"})
    assert res == {"call_count": 7}
    assert len(seen) == 1
    call = seen[0]
    assert call["method"].upper() == "POST"
    assert call["url"] == f"{url}/rest/v1/rpc/rt_get_caller"
    assert call["headers"].get("apikey") == key
    assert call["headers"].get("Authorization") == f"Bearer {key}"
    assert _body_of(call["data"]) == {"p_hash": "abc"}


def test_f21_transient_urlerror_retried_until_success(monkeypatch, db_env):
    sleeps = _patch_sleep(monkeypatch)
    attempts = _wire_network(monkeypatch, [
        urllib.error.URLError("connection reset by peer"),
        urllib.error.URLError("connection reset by peer"),
        _FakeResponse(b'{"call_count": 3}'),
    ])
    assert rt_prefs._req("POST", "rpc/rt_get_caller", {"p_hash": "abc"}) == {"call_count": 3}
    assert len(attempts) == 3
    assert sleeps == [pytest.approx(0.3), pytest.approx(0.9)]


def test_f21_transient_gives_up_after_three_total_attempts(monkeypatch, db_env):
    sleeps = _patch_sleep(monkeypatch)
    attempts = _wire_network(monkeypatch, [
        urllib.error.URLError("connection reset by peer"),
        urllib.error.URLError("connection reset by peer"),
        urllib.error.URLError("connection reset by peer"),
        _FakeResponse(b'{"never": "reached"}'),  # a 4th attempt would consume this
    ])
    with pytest.raises(urllib.error.URLError) as ei:
        rt_prefs._req("POST", "rpc/rt_get_caller", {"p_hash": "abc"})
    assert not isinstance(ei.value, urllib.error.HTTPError)
    assert not isinstance(ei.value, _NetworkExhausted)
    assert len(attempts) == 3
    assert sleeps == [pytest.approx(0.3), pytest.approx(0.9)]


@pytest.mark.parametrize("boom", [
    socket.timeout("timed out"),
    urllib.error.URLError(socket.timeout("timed out")),
])
def test_f21_socket_timeout_retried(monkeypatch, db_env, boom):
    sleeps = _patch_sleep(monkeypatch)
    attempts = _wire_network(monkeypatch, [boom, _FakeResponse(b'{"ok": true}')])
    assert rt_prefs._req("POST", "rpc/rt_get_caller", {"p_hash": "abc"}) == {"ok": True}
    assert len(attempts) == 2
    assert sleeps == [pytest.approx(0.3)]


@pytest.mark.parametrize("code", [502, 503, 504])
def test_f21_http_gateway_errors_retried(monkeypatch, db_env, code):
    sleeps = _patch_sleep(monkeypatch)
    attempts = _wire_network(monkeypatch, [_http_error(code), _FakeResponse(b'{"ok": true}')])
    assert rt_prefs._req("POST", "rpc/rt_get_caller", {"p_hash": "abc"}) == {"ok": True}
    assert len(attempts) == 2
    assert sleeps == [pytest.approx(0.3)]


def test_f21_http_gateway_errors_capped_at_three_attempts(monkeypatch, db_env):
    sleeps = _patch_sleep(monkeypatch)
    attempts = _wire_network(monkeypatch, [
        _http_error(503), _http_error(502), _http_error(504),
        _FakeResponse(b'{"never": "reached"}'),
    ])
    with pytest.raises(urllib.error.HTTPError) as ei:
        rt_prefs._req("POST", "rpc/rt_get_caller", {"p_hash": "abc"})
    assert ei.value.code == 504
    assert len(attempts) == 3
    assert sleeps == [pytest.approx(0.3), pytest.approx(0.9)]


@pytest.mark.parametrize("code", [400, 401, 404, 409, 500, 501])
def test_f21_non_transient_http_errors_raise_immediately(monkeypatch, db_env, code):
    sleeps = _patch_sleep(monkeypatch)
    attempts = _wire_network(monkeypatch, [_http_error(code), _FakeResponse(b'{"ok": true}')])
    with pytest.raises(urllib.error.HTTPError) as ei:
        rt_prefs._req("POST", "rpc/rt_get_caller", {"p_hash": "abc"})
    assert ei.value.code == code
    assert len(attempts) == 1
    assert sleeps == []


def test_f21_success_returns_parsed_json_or_none(monkeypatch, db_env):
    attempts = _wire_network(monkeypatch, [_FakeResponse(b'{"a": 1}'), _FakeResponse(b"")])
    assert rt_prefs._req("POST", "rpc/rt_get_caller", {"p_hash": "abc"}) == {"a": 1}
    assert rt_prefs._req("GET", "rt_callers?select=id&limit=1") is None
    assert len(attempts) == 2


# ─── round 2: #21 mutating RPCs are sent once; deadline_s bounds the whole call ──

class _FakeClock:
    """Monotonic stand-in: the scripted network and the backoff sleep advance it."""

    def __init__(self):
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, s: float) -> None:
        self.now += s


def _wire_timed_network(monkeypatch, clock: _FakeClock, outcomes: list) -> list:
    """Like _wire_network, but each outcome is (seconds_taken, result) and the
    timeout urllib was handed is recorded next to the request."""
    seq = list(outcomes)
    attempts: list = []

    def _next(req, timeout=None, **_k):
        attempts.append({"req": req, "timeout": timeout})
        if not seq:
            raise _NetworkExhausted(
                f"opened the network {len(attempts)} times but only "
                f"{len(outcomes)} outcome(s) were scripted")
        took, out = seq.pop(0)
        clock.advance(took)
        if isinstance(out, BaseException):
            raise out
        return out

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, *a, **k: _next(req, **k))
    monkeypatch.setattr(urllib.request.OpenerDirector, "open",
                        lambda self, req, *a, **k: _next(req, **k))
    return attempts


def _patch_timed_sleep(monkeypatch, clock: _FakeClock) -> list:
    sleeps: list = []

    def _sleep(s):
        sleeps.append(s)
        clock.advance(s)

    monkeypatch.setattr(rt_http, "_SLEEP", _sleep)
    monkeypatch.setattr(rt_http, "_CLOCK", clock)
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)
    return sleeps


@pytest.mark.parametrize("boom", [
    socket.timeout("timed out"),
    urllib.error.URLError(socket.timeout("timed out")),
    _http_error(503),
])
def test_f21_r2_bump_call_timeout_is_sent_exactly_once(monkeypatch, db_env, boom):
    # A read-timeout AFTER the bytes left may already be committed server-side;
    # re-sending rt_bump_call would count the call twice. One send, then raise.
    sleeps = _patch_sleep(monkeypatch)
    attempts = _wire_network(monkeypatch, [boom, _FakeResponse(b'{"never": "reached"}')])
    with pytest.raises(Exception) as ei:
        rt_prefs._req("POST", "rpc/rt_bump_call", {"p_hash": "abc"})
    assert not isinstance(ei.value, _NetworkExhausted)
    assert len(attempts) == 1
    assert sleeps == []


@pytest.mark.parametrize("rpc", [
    "rt_set_brand_new_pref", "rt_add_brand_new_thing", "rt_bump_x", "rt_schedule_call",
    "rt_update_reminder", "rt_forget_fact", "rt_cancel_call", "rt_remove_loved_one",
    "rt_add_schema_entry", "rt_wipe_all_data", "rt_save_call_metrics",
])
def test_f21_r2_mutating_prefix_rpcs_never_retried(monkeypatch, db_env, rpc):
    sleeps = _patch_sleep(monkeypatch)
    attempts = _wire_network(monkeypatch, [socket.timeout("timed out"),
                                           _FakeResponse(b'{"never": "reached"}')])
    with pytest.raises(Exception) as ei:
        rt_prefs._req("POST", f"rpc/{rpc}", {"p_hash": "abc"})
    assert not isinstance(ei.value, _NetworkExhausted)
    assert len(attempts) == 1
    assert sleeps == []


def test_f21_r2_table_write_never_retried_but_read_is(monkeypatch, db_env):
    sleeps = _patch_sleep(monkeypatch)
    attempts = _wire_network(monkeypatch, [socket.timeout("timed out"),
                                           _FakeResponse(b'{"never": "reached"}')])
    with pytest.raises(Exception) as ei:
        rt_prefs._req("PATCH", "rt_callers?phone_hash=eq.abc", {"voice_pref": "x"})
    assert not isinstance(ei.value, _NetworkExhausted)
    assert len(attempts) == 1 and sleeps == []

    attempts = _wire_network(monkeypatch, [socket.timeout("timed out"),
                                           _FakeResponse(b'[{"id": 1}]')])
    assert rt_prefs._req("GET", "rt_callers?select=id&limit=1") == [{"id": 1}]
    assert len(attempts) == 2


def test_f21_r2_reads_still_get_three_attempts(monkeypatch, db_env):
    sleeps = _patch_sleep(monkeypatch)
    attempts = _wire_network(monkeypatch, [
        socket.timeout("timed out"), socket.timeout("timed out"),
        _FakeResponse(b'{"schemas": []}'),
    ])
    assert rt_prefs._req("POST", "rpc/rt_get_caller_full_bundle",
                         {"p_hash": "abc"}) == {"schemas": []}
    assert len(attempts) == 3
    assert sleeps == [pytest.approx(0.3), pytest.approx(0.9)]


def test_f21_r2_deadline_clamps_each_attempt_timeout(monkeypatch, db_env):
    clock = _FakeClock()
    sleeps = _patch_timed_sleep(monkeypatch, clock)
    attempts = _wire_timed_network(monkeypatch, clock, [
        (0.5, urllib.error.URLError("connection reset by peer")),
        (0.1, _FakeResponse(b'{"ok": true}')),
    ])
    assert rt_prefs._req("POST", "rpc/rt_get_caller", {"p_hash": "abc"},
                         deadline_s=2.0) == {"ok": True}
    assert len(attempts) == 2
    # First attempt: min(per-attempt 10s, budget 2.0s). Second: what was left after
    # 0.5s on the wire and the 0.3s backoff.
    assert attempts[0]["timeout"] == pytest.approx(2.0)
    assert attempts[1]["timeout"] == pytest.approx(1.2)
    assert sleeps == [pytest.approx(0.3)]


def test_f21_r2_deadline_skips_retry_that_cannot_finish(monkeypatch, db_env):
    clock = _FakeClock()
    sleeps = _patch_timed_sleep(monkeypatch, clock)
    attempts = _wire_timed_network(monkeypatch, clock, [
        (0.4, urllib.error.URLError("connection reset by peer")),
        (0.1, _FakeResponse(b'{"never": "reached"}')),
    ])
    # 0.4s elapsed + 0.3s backoff = 0.7s > 0.5s budget: no second send, no sleep.
    with pytest.raises(urllib.error.URLError) as ei:
        rt_prefs._req("POST", "rpc/rt_get_caller", {"p_hash": "abc"}, deadline_s=0.5)
    assert not isinstance(ei.value, _NetworkExhausted)
    assert len(attempts) == 1
    assert sleeps == []


def test_f21_r2_deadline_none_keeps_per_attempt_timeout(monkeypatch, db_env):
    clock = _FakeClock()
    _patch_timed_sleep(monkeypatch, clock)
    attempts = _wire_timed_network(monkeypatch, clock, [
        (30.0, urllib.error.URLError("connection reset by peer")),
        (0.1, _FakeResponse(b'{"ok": true}')),
    ])
    assert rt_prefs._req("POST", "rpc/rt_get_caller", {"p_hash": "abc"}) == {"ok": True}
    assert len(attempts) == 2
    assert attempts[0]["timeout"] == pytest.approx(10.0)
    assert attempts[1]["timeout"] == pytest.approx(10.0)


def test_f21_r2_rt_http_header_comment_is_honest():
    # urllib forces "Connection: close" on the wire; the module must not claim a pool.
    import inspect
    doc = (inspect.getdoc(rt_http) or "").lower()
    assert "no connection pool" in doc or "fresh socket" in doc
    assert "persistent" not in doc


# ─── round 2: #4 RT_REQUIRE_PEPPER truthiness ────────────────────────────────

@pytest.mark.parametrize("flag", ["true", "TRUE", " True ", "yes", "YES", "1", " 1 ",
                                  "required", "maybe", "on"])
def test_f04_r2_require_pepper_truthy_spellings_raise(monkeypatch, flag):
    monkeypatch.delenv("RT_PHONE_HASH_PEPPER", raising=False)
    monkeypatch.setenv("RT_REQUIRE_PEPPER", flag)
    with pytest.raises(RuntimeError, match="RT_PHONE_HASH_PEPPER"):
        rt_prefs.phone_hash(_E164)


@pytest.mark.parametrize("flag", ["", "0", "false", "no", "off", " OFF ", "False"])
def test_f04_r2_require_pepper_falsy_spellings_fall_back(monkeypatch, capsys, flag):
    monkeypatch.delenv("RT_PHONE_HASH_PEPPER", raising=False)
    monkeypatch.setenv("RT_REQUIRE_PEPPER", flag)
    monkeypatch.setattr(rt_prefs, "_WARNED_UNPEPPERED", False)
    assert rt_prefs.phone_hash(_E164) == _SHA
    assert "unpeppered" in capsys.readouterr().out.lower()


# ─── round 2: #14 / #17 ledger read AND write fail closed ────────────────────

def _capture_obs_events(monkeypatch) -> list:
    import rt_obs
    events: list = []
    monkeypatch.setattr(rt_obs.obs, "event",
                        lambda name, **f: events.append((name, f)))
    return events


@pytest.mark.parametrize("bundle", [None, [], "nope", 0])
def test_f14_r2_non_dict_bundle_refuses_and_never_writes(monkeypatch, bundle):
    # None is what _req returns when the DB is unconfigured or the ref is refused —
    # `or {}` used to turn that into an empty ledger and let the dial through.
    calls: list = []
    events = _capture_obs_events(monkeypatch)

    def fake_req(method, path, body=None, *a, **kw):
        calls.append((method, path, body))
        return bundle if "rt_get_caller_full_bundle" in path else {}

    _bridge_db(monkeypatch)
    monkeypatch.setattr(rt_prefs, "_req", fake_req)
    with pytest.raises(rt_bridge.DialRefused) as ei:
        rt_bridge.check_and_record_dial("c" * 64, "+19175551234")
    assert len(str(ei.value).split()) >= 5
    assert [c for c in calls if "rt_add_schema_entry" in c[1]] == []
    assert not [e for e in events if e[0] == "lk.sip"]
    assert any(e[0] == "guard.decision" and e[1].get("allowed") is False
               and e[1].get("reason") == "ledger_unavailable" for e in events)


@pytest.mark.parametrize("row", [
    "{not json", "[1, 2, 3]", '"a string"', "42", "null",
])
def test_f17_r2_corrupt_ledger_row_refuses_and_never_writes(monkeypatch, row):
    calls: list = []
    events = _capture_obs_events(monkeypatch)

    def fake_req(method, path, body=None, *a, **kw):
        calls.append((method, path, body))
        if "rt_get_caller_full_bundle" in path:
            return {"schemas": [{"category": rt_bridge.BRIDGE_CATEGORY, "data_summary": row}]}
        return {}

    _bridge_db(monkeypatch)
    monkeypatch.setattr(rt_prefs, "_req", fake_req)
    with pytest.raises(rt_bridge.DialRefused):
        rt_bridge.check_and_record_dial("d" * 64, "+19175551234")
    assert [c for c in calls if "rt_add_schema_entry" in c[1]] == []
    assert not [e for e in events if e[0] == "lk.sip"]


@pytest.mark.parametrize("row", ["{not json", "[1, 2]", "7"])
def test_f17_r2_load_log_raises_on_corrupt_row(row):
    with pytest.raises(ValueError):
        rt_bridge._load_log([{"category": rt_bridge.BRIDGE_CATEGORY, "data_summary": row}])


def test_f17_r2_load_log_empty_or_absent_is_empty_dict():
    assert rt_bridge._load_log(None) == {}
    assert rt_bridge._load_log([]) == {}
    assert rt_bridge._load_log([{"category": "other", "data_summary": "{not json"}]) == {}
    assert rt_bridge._load_log([{"category": rt_bridge.BRIDGE_CATEGORY,
                                 "data_summary": ""}]) == {}
    assert rt_bridge._load_log([{"category": rt_bridge.BRIDGE_CATEGORY,
                                 "data_summary": '{"2020-01-01": {"count": 2}}'}]) == \
        {"2020-01-01": {"count": 2}}


@pytest.mark.parametrize("write_outcome", [
    RuntimeError("db down: connection refused"),
    urllib.error.URLError("connection reset by peer"),
    TimeoutError("timed out"),
])
def test_f14_r2_ledger_write_failure_refuses_and_never_authorises_sip(monkeypatch, write_outcome):
    calls: list = []
    events = _capture_obs_events(monkeypatch)

    def fake_req(method, path, body=None, *a, **kw):
        calls.append((method, path, body))
        if "rt_get_caller_full_bundle" in path:
            return {"schemas": []}
        if "rt_add_schema_entry" in path:
            raise write_outcome
        return {}

    _bridge_db(monkeypatch)
    monkeypatch.setattr(rt_prefs, "_req", fake_req)
    with pytest.raises(rt_bridge.DialRefused) as ei:
        rt_bridge.check_and_record_dial("e" * 64, "+19175551234")
    msg = str(ei.value)
    assert len(msg.split()) >= 5
    assert type(write_outcome).__name__ not in msg
    # The write was attempted exactly once, and the SIP leg was never authorised.
    assert len([c for c in calls if "rt_add_schema_entry" in c[1]]) == 1
    assert not [e for e in events if e[0] == "lk.sip"]
    assert any(e[0] == "guard.decision" and e[1].get("allowed") is False
               and e[1].get("reason") == "ledger_write_failed" for e in events)


def test_f14_r2_happy_path_writes_then_authorises_sip(monkeypatch):
    calls: list = []
    events = _capture_obs_events(monkeypatch)

    def fake_req(method, path, body=None, *a, **kw):
        calls.append((method, path, body))
        if "rt_get_caller_full_bundle" in path:
            return {"schemas": []}
        return None  # public.rt_add_schema_entry RETURNS VOID: a committed write is None

    _bridge_db(monkeypatch)
    monkeypatch.setattr(rt_prefs, "_req", fake_req)
    rt_bridge.check_and_record_dial("f" * 64, "+19175551234")
    writes = [c for c in calls if "rt_add_schema_entry" in c[1]]
    assert len(writes) == 1 and writes[0][2]["p_cat"] == "bridge_log"
    sip = [e for e in events if e[0] == "lk.sip"]
    assert len(sip) == 1 and sip[0][1].get("status") == "authorized"
    # Order: the ledger write precedes the authorisation.
    assert calls[-1][1].endswith("rt_add_schema_entry")


def test_f14_r2_daily_cap_spent_refuses_before_any_write(monkeypatch):
    calls: list = []
    events = _capture_obs_events(monkeypatch)
    today = rt_bridge._today()
    row = json.dumps({today: {"count": rt_bridge.MAX_BRIDGES_PER_DAY, "numbers": []}})

    def fake_req(method, path, body=None, *a, **kw):
        calls.append((method, path, body))
        if "rt_get_caller_full_bundle" in path:
            return {"schemas": [{"category": rt_bridge.BRIDGE_CATEGORY, "data_summary": row}]}
        return {}

    _bridge_db(monkeypatch)
    monkeypatch.setattr(rt_prefs, "_req", fake_req)
    with pytest.raises(rt_bridge.DialRefused):
        rt_bridge.check_and_record_dial("a" * 64, "+19175551234")
    assert [c for c in calls if "rt_add_schema_entry" in c[1]] == []
    assert not [e for e in events if e[0] == "lk.sip"]


# ═══ round 3 ═══════════════════════════════════════════════════════════════════
# #14 a VOID write authorises; an unconfigured DB refuses before any RPC.
# #17 a malformed day entry is a corrupt ledger, never "count 0".
# #21 rt_http sends once by default; only the read allow-list is ever re-sent.
# #4  pepper_ok gates the pepper; RT_REQUIRE_PEPPER fails closed.
# INTERNAL_CATS is the one shared list of platform-owned categories.

_BUNDLE_EMPTY = json.dumps({"schemas": []}).encode()


def _sip_events(events: list) -> list:
    return [e for e in events if e[0] == "lk.sip"]


def _guard_reasons(events: list, allowed: bool) -> set:
    return {e[1].get("reason") for e in events
            if e[0] == "guard.decision" and e[1].get("guard") == "bridge_cap"
            and e[1].get("allowed") is allowed}


def _rpc_of(req) -> str:
    return urllib.parse.urlsplit(req.full_url).path.rsplit("/", 1)[-1]


def _no_audit(monkeypatch) -> None:
    """The fire-and-forget rt_audit thread would consume a scripted outcome; keep
    these wire-level tests to the two RPCs the ledger itself makes."""
    monkeypatch.setattr(rt_prefs, "audit", lambda *_a, **_k: None)


@pytest.mark.parametrize("void_reply", [
    _FakeResponse(b"", 204, content_type=""),
    _FakeResponse(b"", 200),
    _FakeResponse(b"null", 200),
])
def test_f14_r3_void_write_reply_authorises_dial(monkeypatch, db_env, void_reply):
    _no_audit(monkeypatch)
    # public.rt_add_schema_entry RETURNS VOID: the committed write comes back as an
    # empty body (or JSON null). Round 2 read that as a failure and refused every
    # production bridge AFTER the ledger row had already been written.
    events = _capture_obs_events(monkeypatch)
    attempts = _wire_network(monkeypatch, [_FakeResponse(_BUNDLE_EMPTY), void_reply])
    rt_bridge.check_and_record_dial("a" * 64, "+19175551234")
    assert [_rpc_of(r) for r in attempts] == ["rt_get_caller_full_bundle", "rt_add_schema_entry"]
    body = json.loads(attempts[1].data.decode())
    assert body["p_cat"] == rt_bridge.BRIDGE_CATEGORY
    assert json.loads(body["p_summary"])[rt_bridge._today()] == {"count": 1,
                                                                  "numbers": ["+19175551234"]}
    sip = _sip_events(events)
    assert len(sip) == 1 and sip[0][1].get("status") == "authorized"
    assert "ledger_write_failed" not in _guard_reasons(events, False)


def test_f14_r3_write_http_error_still_refuses(monkeypatch, db_env):
    _no_audit(monkeypatch)
    # The void-is-success rule must not swallow a real failure: HTTP errors raise.
    events = _capture_obs_events(monkeypatch)
    attempts = _wire_network(monkeypatch, [_FakeResponse(_BUNDLE_EMPTY), _http_error(500)])
    with pytest.raises(rt_bridge.DialRefused):
        rt_bridge.check_and_record_dial("a" * 64, "+19175551234")
    assert [_rpc_of(r) for r in attempts].count("rt_add_schema_entry") == 1
    assert not _sip_events(events)
    assert "ledger_write_failed" in _guard_reasons(events, False)


def test_f14_r3_write_timeout_is_sent_once_and_refuses(monkeypatch, db_env):
    _no_audit(monkeypatch)
    # A write that times out is NOT re-sent (it may have committed) and does not dial.
    events = _capture_obs_events(monkeypatch)
    sleeps = _patch_sleep(monkeypatch)
    attempts = _wire_network(monkeypatch, [_FakeResponse(_BUNDLE_EMPTY),
                                           socket.timeout("timed out"),
                                           _FakeResponse(b"", 204)])
    with pytest.raises(rt_bridge.DialRefused):
        rt_bridge.check_and_record_dial("a" * 64, "+19175551234")
    assert [_rpc_of(r) for r in attempts] == ["rt_get_caller_full_bundle", "rt_add_schema_entry"]
    assert sleeps == []
    assert not _sip_events(events)


@pytest.mark.parametrize("unset", ["SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"])
def test_f14_r3_unconfigured_db_refuses_before_any_rpc(monkeypatch, unset):
    monkeypatch.setenv("SUPABASE_URL", _URL)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", _KEY)
    monkeypatch.setattr(rt_prefs, "ALLOWED_REFS", {"testprojectref"})
    monkeypatch.delenv(unset, raising=False)
    assert rt_prefs._db() is None
    events = _capture_obs_events(monkeypatch)
    calls: list = []
    monkeypatch.setattr(rt_prefs, "_req", lambda *a, **k: calls.append(a) or None)
    attempts = _wire_network(monkeypatch, [])
    with pytest.raises(rt_bridge.DialRefused) as ei:
        rt_bridge.check_and_record_dial("a" * 64, "+19175551234")
    assert len(str(ei.value).split()) >= 5
    assert calls == [] and attempts == []
    assert "ledger_unavailable" in _guard_reasons(events, False)
    assert not _sip_events(events)


def test_f14_r3_refused_ref_refuses_before_any_rpc(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://someotherref.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", _KEY)
    monkeypatch.setattr(rt_prefs, "ALLOWED_REFS", {"testprojectref"})
    events = _capture_obs_events(monkeypatch)
    calls: list = []
    monkeypatch.setattr(rt_prefs, "_req", lambda *a, **k: calls.append(a) or None)
    with pytest.raises(rt_bridge.DialRefused):
        rt_bridge.check_and_record_dial("a" * 64, "+19175551234")
    assert calls == []
    assert "ledger_unavailable" in _guard_reasons(events, False)


# ─── #17 malformed day entries ──────────────────────────────────────────────

@pytest.mark.parametrize("entry", [
    "3", 3, 3.0, [1, 2, 3], None, True,
    {"count": None}, {"count": "3"}, {"count": 2.0}, {"count": True}, {"count": False},
    {"count": -1}, {"count": [1]}, {"count": {"n": 1}},
    {"count": 1, "numbers": "+19175550000"}, {"count": 1, "numbers": 7},
])
def test_f17_r3_malformed_day_entry_is_corrupt_ledger(monkeypatch, entry):
    calls: list = []
    events = _capture_obs_events(monkeypatch)
    row = json.dumps({rt_bridge._today(): entry})

    def fake_req(method, path, body=None, *a, **kw):
        calls.append((method, path, body))
        if "rt_get_caller_full_bundle" in path:
            return {"schemas": [{"category": rt_bridge.BRIDGE_CATEGORY, "data_summary": row}]}
        return None

    _bridge_db(monkeypatch)
    monkeypatch.setattr(rt_prefs, "_req", fake_req)
    with pytest.raises(rt_bridge.DialRefused) as ei:
        rt_bridge.check_and_record_dial("g" * 64, "+19175551234")
    assert len(str(ei.value).split()) >= 5
    assert [c for c in calls if "rt_add_schema_entry" in c[1]] == []
    assert not _sip_events(events)
    assert "ledger_corrupt" in _guard_reasons(events, False)
    assert "under_cap" not in _guard_reasons(events, True)


@pytest.mark.parametrize("entry, used", [
    ({"count": 0}, 0), ({"count": 3}, 3), ({"count": 3, "numbers": None}, 3),
    ({"numbers": ["+19175550000"]}, 0), ({}, 0),
])
def test_f17_r3_well_formed_day_entry_counts(monkeypatch, entry, used):
    calls: list = []
    events = _capture_obs_events(monkeypatch)
    day = rt_bridge._today()

    def fake_req(method, path, body=None, *a, **kw):
        calls.append((method, path, body))
        if "rt_get_caller_full_bundle" in path:
            return {"schemas": [{"category": rt_bridge.BRIDGE_CATEGORY,
                                 "data_summary": json.dumps({day: entry})}]}
        return None

    _bridge_db(monkeypatch)
    monkeypatch.setattr(rt_prefs, "_req", fake_req)
    rt_bridge.check_and_record_dial("g" * 64, "+19175551234")
    writes = [c for c in calls if "rt_add_schema_entry" in c[1]]
    assert len(writes) == 1
    today = json.loads(writes[0][2]["p_summary"])[day]
    assert today["count"] == used + 1
    assert today["numbers"][-1] == "+19175551234"
    assert len(_sip_events(events)) == 1


def test_f17_r3_day_entry_helper():
    assert rt_bridge._day_entry({}, "2020-01-01") == (0, [])
    assert rt_bridge._day_entry({"2020-01-01": {"count": 2, "numbers": ["+1"]}},
                                "2020-01-01") == (2, ["+1"])
    for bad in ("x", 0, None, [], {"count": "2"}, {"count": True}, {"count": -1},
                {"count": 1, "numbers": "no"}):
        with pytest.raises(ValueError):
            rt_bridge._day_entry({"2020-01-01": bad}, "2020-01-01")


@pytest.mark.parametrize("raw", [0, [], False, 7, [1], "0", "[]", "false"])
def test_f17_r3_load_log_falsy_non_null_summary_is_corrupt(raw):
    # `or "{}"` used to turn 0 / [] / false into an empty ledger.
    with pytest.raises(ValueError):
        rt_bridge._load_log([{"category": rt_bridge.BRIDGE_CATEGORY, "data_summary": raw}])


def test_f17_r3_load_log_null_summary_is_empty():
    assert rt_bridge._load_log([{"category": rt_bridge.BRIDGE_CATEGORY,
                                 "data_summary": None}]) == {}
    assert rt_bridge._load_log([{"category": rt_bridge.BRIDGE_CATEGORY}]) == {}
    assert rt_bridge._load_log([{"category": rt_bridge.BRIDGE_CATEGORY,
                                 "data_summary": {"2020-01-01": {"count": 1}}}]) == \
        {"2020-01-01": {"count": 1}}


# ─── INTERNAL_CATS ──────────────────────────────────────────────────────────

def test_r3_internal_cats_is_the_shared_platform_list():
    cats = rt_prefs.INTERNAL_CATS
    assert isinstance(cats, frozenset)
    assert {"bridge_log", "scam_reports", "daily_minutes"} <= cats
    assert {rt_bridge.BRIDGE_CATEGORY, rt_bridge.SCAM_CATEGORY, rt_prefs.CRED_CATEGORY} <= cats
    import rt_executor
    assert rt_executor.TASK_CATEGORY in cats
    # Everything agent.py already refuses stays refused once it imports the shared list.
    import agent as agent_mod
    assert set(agent_mod._INTERNAL_CATS) <= cats
    assert agent_mod._BUDGET_CATEGORY in cats
    assert all(c == c.lower() and c.strip() == c for c in cats)


# ─── #21 send-once default; read allow-list ─────────────────────────────────

def test_f21_r3_rt_http_default_is_send_once(monkeypatch):
    import inspect
    assert inspect.signature(rt_http.PooledHttpClient.request).parameters["retries"].default == 0
    sleeps = _patch_sleep(monkeypatch)
    attempts = _wire_network(monkeypatch, [socket.timeout("timed out"),
                                           _FakeResponse(b'{"never": "reached"}')])
    with pytest.raises(Exception) as ei:
        rt_http.http_client.request("POST", "https://api.resend.com/emails",
                                    headers={"Authorization": "Bearer x"},
                                    data={"to": "a@b.c"})
    assert not isinstance(ei.value, _NetworkExhausted)
    assert len(attempts) == 1 and sleeps == []


def test_f21_r3_rt_http_opt_in_retries_still_work(monkeypatch):
    sleeps = _patch_sleep(monkeypatch)
    attempts = _wire_network(monkeypatch, [socket.timeout("timed out"),
                                           _FakeResponse(b'{"ok": true}')])
    assert rt_http.http_client.request("GET", "https://example.com/x", retries=2) == {"ok": True}
    assert len(attempts) == 2 and sleeps == [pytest.approx(0.3)]


_R3_MUTATING = [
    "rt_upsert_fact", "rt_postcall_claim", "rt_call_event", "rt_audit", "rt_call_start",
    "rt_call_finish", "rt_postcall_enqueue", "rt_postcall_complete", "rt_postcall_recover",
    "rt_purge_forgotten", "rt_purge_old_transcripts", "rt_reclaim_stale_jobs",
    "rt_retire_fact", "rt_forget_caller", "rt_cancel_jobs_for", "rt_update_job_status",
    "rt_schedule_job_superseding", "rt_set_caller_email", "rt_set_last_name",
    "rt_complete_reminder",
    # Named like a read, but it UPDATEs rows to 'running': a claim, sent once.
    "rt_get_pending_jobs",
    # Unknown names are sent once until proven a read.
    "rt_brand_new_thing", "rt_do_something",
]
_R3_READS = [
    "rt_get_caller", "rt_get_caller_full_bundle", "rt_get_facts", "rt_get_all_callers",
    "rt_count_jobs_today", "rt_console_calls", "rt_console_events",
    "rt_call_transcript", "rt_postcall_queue_stats",
]


@pytest.mark.parametrize("rpc", _R3_MUTATING)
def test_f21_r3_non_read_rpcs_sent_once(monkeypatch, db_env, rpc):
    sleeps = _patch_sleep(monkeypatch)
    attempts = _wire_network(monkeypatch, [socket.timeout("timed out"),
                                           _FakeResponse(b'{"never": "reached"}')])
    with pytest.raises(Exception) as ei:
        rt_prefs._req("POST", f"rpc/{rpc}", {"p_hash": "abc"}, _skip_audit=True)
    assert not isinstance(ei.value, _NetworkExhausted)
    assert len(attempts) == 1 and sleeps == []


@pytest.mark.parametrize("rpc", _R3_READS)
def test_f21_r3_read_rpcs_are_retried(monkeypatch, db_env, rpc):
    sleeps = _patch_sleep(monkeypatch)
    attempts = _wire_network(monkeypatch, [socket.timeout("timed out"),
                                           _FakeResponse(b'{"ok": true}')])
    assert rt_prefs._req("POST", f"rpc/{rpc}", {"p_hash": "abc"}) == {"ok": True}
    assert len(attempts) == 2 and sleeps == [pytest.approx(0.3)]


_WRITE_VERBS = ("set_", "add_", "bump_", "schedule_", "update_", "forget_", "cancel_",
                "remove_", "upsert_", "purge_", "wipe_", "save_", "complete_", "enqueue",
                "claim", "recover", "retire_", "reclaim_", "audit", "call_event",
                "call_start", "call_finish", "pending_jobs")


def test_f21_r3_every_rpc_literal_in_worker_is_classified():
    """Walk every "rpc/rt_" literal in worker/*.py (non-test). Each name is either on
    the read allow-list (re-sent on a transient failure) or is sent once. No name
    carrying a write verb may be on the read side, and the read side is exactly
    the set the contract names."""
    import pathlib
    import re as _re
    worker = pathlib.Path(rt_prefs.__file__).resolve().parent
    names: set[str] = set()
    for py in sorted(worker.glob("*.py")):
        if py.name.startswith("test_"):
            continue
        names |= set(_re.findall(r"rpc/(rt_[a-z0-9_]+)", py.read_text(encoding="utf-8")))
    assert len(names) >= 30, sorted(names)
    reads = {n for n in names if rt_prefs._is_read("POST", f"rpc/{n}")}
    once = names - reads
    for n in reads:
        assert not any(v in n for v in _WRITE_VERBS), f"{n} carries a write verb but is retried"
        assert n.startswith(("rt_get_", "rt_count_", "rt_console_")) \
            or n in ("rt_call_transcript", "rt_postcall_queue_stats"), n
    assert "rt_get_pending_jobs" in once
    for n in ("rt_upsert_fact", "rt_postcall_claim", "rt_call_event", "rt_audit",
              "rt_add_schema_entry", "rt_bump_call", "rt_schedule_job"):
        assert n in once, n
    # Reads present in this tree: exact, so a newly added rt_get_* is a conscious choice.
    assert reads == {n for n in names if n.startswith(("rt_get_", "rt_count_", "rt_console_"))
                     or n in ("rt_call_transcript", "rt_postcall_queue_stats")} - {"rt_get_pending_jobs"}


def test_f21_r3_is_read_table_paths():
    assert rt_prefs._is_read("GET", "rt_callers?select=id") is True
    assert rt_prefs._is_read("HEAD", "rt_callers") is True
    for verb in ("POST", "PATCH", "PUT", "DELETE"):
        assert rt_prefs._is_read(verb, "rt_callers?phone_hash=eq.x") is False


# ─── #4 pepper_ok gates the pepper ──────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    None, "", "   ", "#secret-salt-123-long-enough", "short-pepper-15",
    "replace-me-with-a-real-pepper", "EXAMPLE-pepper-value-here", "changeme-changeme-changeme",
    "TODO: set a real pepper here", "xxxxxxxxxxxxxxxxxxxx",
])
def test_f04_r3_pepper_ok_inline_rejects(bad):
    assert rt_prefs._pepper_ok_inline(bad) is False
    assert rt_prefs.pepper_ok(bad) is False


@pytest.mark.parametrize("good", [_PEPPER, _PEPPER2, "a" * 16, "  padded-real-pepper-1234  "])
def test_f04_r3_pepper_ok_inline_accepts(good):
    assert rt_prefs._pepper_ok_inline(good) is True


def test_f04_r3_pepper_ok_matches_config_when_present():
    import config
    fn = getattr(config, "pepper_ok", None)
    if fn is None:
        pytest.skip("config.pepper_ok not landed yet (G5)")
    for v in (None, "", "changeme-changeme-changeme", "short", _PEPPER, "a" * 16):
        assert rt_prefs.pepper_ok(v) is fn(v) is rt_prefs._pepper_ok_inline(v), v


def test_f04_r3_pepper_ok_is_never_looser_than_inline(monkeypatch):
    import config
    monkeypatch.setattr(config, "pepper_ok", lambda v: True, raising=False)
    assert rt_prefs.pepper_ok("changeme-changeme-changeme") is False
    assert rt_prefs.pepper_ok(_PEPPER) is True


@pytest.mark.parametrize("weak", [
    "changeme-changeme-changeme", "short-pepper-15", "#commented-out-pepper-value",
    "replace_with_real_pepper_now",
])
@pytest.mark.parametrize("flag", ["1", "true", "required"])
def test_f04_r3_unusable_pepper_counts_as_missing_when_required(monkeypatch, weak, flag):
    monkeypatch.setenv("RT_PHONE_HASH_PEPPER", weak)
    monkeypatch.setenv("RT_REQUIRE_PEPPER", flag)
    with pytest.raises(RuntimeError, match="RT_PHONE_HASH_PEPPER"):
        rt_prefs.phone_hash(_E164)
    with pytest.raises(RuntimeError, match="RT_PHONE_HASH_PEPPER"):
        rt_prefs.phone_hash(_E164, pepper=weak)


def test_f04_r3_unusable_pepper_not_required_warns_once_and_still_keys(monkeypatch, capsys):
    monkeypatch.setenv("RT_PHONE_HASH_PEPPER", "short-pepper-15")
    monkeypatch.setenv("RT_REQUIRE_PEPPER", "0")
    monkeypatch.setattr(rt_prefs, "_WARNED_WEAK_PEPPER", False)
    expected = hmac.new(b"short-pepper-15", _E164.encode(), hashlib.sha256).hexdigest()
    assert rt_prefs.phone_hash(_E164) == expected
    assert rt_prefs.phone_hash(_E164) != _SHA
    out = capsys.readouterr().out.lower()
    assert sum("unusable" in ln for ln in out.splitlines()) == 1
    rt_prefs.phone_hash(_E164)
    assert "unusable" not in capsys.readouterr().out.lower()


@pytest.mark.parametrize("flag, expected", [
    ("", False), ("0", False), ("false", False), ("no", False), ("off", False),
    (" OFF ", False), ("False", False),
    ("1", True), ("true", True), ("yes", True), ("required", True), ("maybe", True),
    ("on", True), ("2", True), ("nope", True),
])
def test_f04_r3_pepper_required_fails_closed(monkeypatch, flag, expected):
    monkeypatch.setenv("RT_REQUIRE_PEPPER", flag)
    assert rt_prefs.pepper_required() is expected
    monkeypatch.delenv("RT_REQUIRE_PEPPER", raising=False)
    assert rt_prefs.pepper_required() is False


def test_f04_r3_pepper_required_is_never_looser_than_config(monkeypatch):
    import config
    monkeypatch.setenv("RT_REQUIRE_PEPPER", "0")
    monkeypatch.setattr(config, "pepper_required", lambda: True)
    assert rt_prefs.pepper_required() is True
