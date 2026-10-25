"""test_rt_caller_export.py — Tests for cryptographic caller spiritual journal export."""
import json
import pytest
from rt_caller_export import (
    compute_export_seal,
    generate_spiritual_journal,
    verify_export_seal,
    generate_sms_export_digest,
)

PHONE_HASH = "a" * 64
PEPPER = "test_pepper_at_least_16_chars"


def test_generate_markdown_journal():
    answered = [
        {"date": "2026-09-01", "petition": "Mother's health", "testimony": "Surgery succeeded with quick recovery."},
    ]
    loved = [
        {"name": "Eleanor", "relationship": "Mother", "intention": "Strength and joy"},
    ]
    intentions = [
        {"intention": "Peace in the home", "status": "active"},
    ]
    tokens = {"balance": 12, "candles_lit": 3, "sponsored_devotions": 1}

    md = generate_spiritual_journal(
        phone_hash=PHONE_HASH,
        preferred_name="Thomas",
        preferred_guide="Jesus of Nazareth",
        loved_ones=loved,
        answered_prayers=answered,
        intentions=intentions,
        tokens=tokens,
        export_format="markdown",
        pepper=PEPPER,
    )

    assert "# 🕊️ Sacred Spiritual Journal" in md
    assert "Thomas" in md
    assert "Mother's health" in md
    assert "Eleanor" in md
    assert "Cryptographic Authenticity Seal" in md


def test_generate_and_verify_json_journal():
    answered = [
        {"date": "2026-08-15", "petition": "Job interview", "testimony": "Offer received."},
    ]
    json_str = generate_spiritual_journal(
        phone_hash=PHONE_HASH,
        preferred_name="Priya",
        preferred_guide="Lord Krishna",
        answered_prayers=answered,
        export_format="json",
        pepper=PEPPER,
    )

    data = json.loads(json_str)
    assert data["phone_hash"] == PHONE_HASH
    assert data["preferred_guide"] == "Lord Krishna"
    assert "integrity_seal" in data

    # Verify seal validates
    assert verify_export_seal(json_str, pepper=PEPPER) is True

    # Tampering with payload fails verification
    tampered_data = dict(data)
    tampered_data["preferred_guide"] = "Different Guide"
    tampered_json = json.dumps(tampered_data)
    assert verify_export_seal(tampered_json, pepper=PEPPER) is False


def test_sms_export_digest():
    digest = generate_sms_export_digest(
        phone_hash=PHONE_HASH,
        answered_count=4,
        recent_answered=["Mother recovered", "New apartment secured"],
    )
    assert "4 answered prayers" in digest
    assert "Mother recovered" in digest
    assert "JOURNAL" in digest
