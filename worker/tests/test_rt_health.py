"""test_rt_health.py — Unit tests for health & readiness server."""
from __future__ import annotations

import json
import urllib.request
from rt_health import (
    start_health_server,
    get_health_port,
    increment_active_sessions,
    decrement_active_sessions,
    get_active_sessions,
)


def test_session_counter():
    initial = get_active_sessions()
    c1 = increment_active_sessions()
    assert c1 == initial + 1
    c2 = decrement_active_sessions()
    assert c2 == initial


def test_health_server_endpoint():
    start_health_server()
    port = get_health_port()

    # Test /health
    url = f"http://127.0.0.1:{port}/health"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=3) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode("utf-8"))
        assert data["status"] == "ok"
        assert data["service"] == "phone-pal-worker"
        assert "uptime_seconds" in data
        assert "active_sessions" in data

    # Test /ready
    url_ready = f"http://127.0.0.1:{port}/ready"
    with urllib.request.urlopen(urllib.request.Request(url_ready), timeout=3) as resp:
        assert resp.status == 200
        data_ready = json.loads(resp.read().decode("utf-8"))
        assert data_ready["status"] == "ok"


def test_health_server_error_sanitization():
    from rt_health import _sanitize_error
    raw_error = {
        "msg": "Traceback (most recent call last):\n  File 'agent.py', line 123 in foo\nSecretKeyError: secret_123456",
        "ts": 1700000000.0,
        "details": {"internal_key": "hidden"},
    }
    sanitized = _sanitize_error(raw_error)
    assert sanitized is not None
    assert "Traceback" in sanitized["msg"]
    assert "\n" not in sanitized["msg"]  # Multi-line tracebacks stripped
    assert "details" not in sanitized   # Internal details object omitted from public payload
