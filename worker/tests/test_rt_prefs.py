"""test_rt_prefs.py — Unit tests for caller phone normalization and utilities."""
from __future__ import annotations

from rt_prefs import normalize_e164, phone_hash, despell


def test_normalize_e164_valid_numbers():
    assert normalize_e164("+15551234567") == "+15551234567"
    # 10 digits US assumed +1
    assert normalize_e164("5551234567") == "+15551234567"
    # Formatted strings
    assert normalize_e164("(555) 123-4567") == "+15551234567"
    assert normalize_e164("+44 20 7123 4567") == "+442071234567"


def test_normalize_e164_blocked_and_invalid():
    assert normalize_e164("anonymous") is None
    assert normalize_e164("PRIVATE") is None
    assert normalize_e164("unknown") is None
    assert normalize_e164("") is None
    assert normalize_e164("123") is None  # Too short


def test_phone_hash():
    h1 = phone_hash("+15551234567")
    h2 = phone_hash("555-123-4567")
    assert h1 is not None
    assert len(h1) == 64  # SHA256 hex length
    assert h1 == h2  # Normalized hash must be deterministic


def test_phone_hash_with_pepper():
    # Pepper produces distinct, deterministic HMAC-SHA256 hashes
    h_plain = phone_hash("+15551234567", pepper="")
    h_pep1 = phone_hash("+15551234567", pepper="secret-salt-123")
    h_pep2 = phone_hash("555-123-4567", pepper="secret-salt-123")
    h_pep_other = phone_hash("+15551234567", pepper="different-salt")

    assert h_pep1 != h_plain
    assert h_pep1 == h_pep2
    assert h_pep1 != h_pep_other
    assert len(h_pep1) == 64


def test_despell():
    assert despell("r i c h i e") == "richie"
    assert despell("R-I-C-H-I-E") == "RICHIE"
    assert despell("hello world") == "hello world"
