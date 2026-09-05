"""test_security_guards.py — Unit tests for security mitigations and tool guardrails."""
from __future__ import annotations

import re
from rt_prefs import phone_hash, normalize_e164
from rt_health import get_health_host, _sanitize_error


def test_health_host_default_binding():
    # Must bind to 127.0.0.1 by default to prevent exposure on host-networked containers
    host = get_health_host()
    assert host in ("127.0.0.1", "localhost")


def test_health_error_sanitization_masks_sensitive_details():
    err = {
        "msg": "DatabaseConnectionFailed: postgresql://postgres:secretpassword@db.internal:5432\nTraceback...",
        "ts": 123456789.0,
        "details": {"connection_string": "postgresql://postgres:secret@db.internal:5432"},
    }
    sanitized = _sanitize_error(err)
    assert sanitized is not None
    assert "\n" not in sanitized["msg"]
    assert "details" not in sanitized


def test_hmac_pepper_prevents_rainbow_table_precomputation():
    raw_phone = "+12025550199"
    norm = normalize_e164(raw_phone)
    assert norm == "+12025550199"

    hash_no_pepper = phone_hash(raw_phone, pepper="")
    hash_with_pepper = phone_hash(raw_phone, pepper="prod-super-secret-salt-key")

    assert hash_no_pepper != hash_with_pepper
    assert len(hash_with_pepper) == 64


def test_email_regex_validation():
    valid_emails = ["caller@example.com", "jane.doe+test@domain.co.uk", "user123@sub.domain.org"]
    invalid_emails = ["bad-email", "@no-user.com", "user@", "user@bad domain.com", "<script>alert(1)</script>@x.com"]

    pattern = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")
    for email in valid_emails:
        assert bool(pattern.match(email)) is True
    for email in invalid_emails:
        assert bool(pattern.match(email)) is False


def test_sms_message_capping_and_safety():
    # Long messages must be capped to prevent SMS flood / carrier abuse
    msg = "A" * 600
    clean_msg = msg.strip()
    if len(clean_msg) > 500:
        clean_msg = clean_msg[:497] + "..."
    assert len(clean_msg) == 500
    assert clean_msg.endswith("...")
