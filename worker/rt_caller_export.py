"""rt_caller_export.py — Cryptographic Spiritual Journey Export & Sacred Journal.

Inspired by PrayPal's HMAC-SHA256 Privacy Architecture (ADR-0002):
While `forget_me` provides the sacred right to erasure, `rt_caller_export`
provides the right to spiritual portability. Callers can export their walking
history of faith, chronicled answered prayers, loved ones prayer tree, and
token blessings into an illuminated Markdown or structured JSON journal.

Includes cryptographic HMAC integrity sealing so exports can be verified as
authentic records of the caller's sanctuary journey without revealing plaintext
phone numbers.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Any, Literal


def compute_export_seal(phone_hash: str, payload_str: str, pepper: str | None = None) -> str:
    """Compute a cryptographic seal for an exported spiritual journal."""
    active_pepper = pepper or os.getenv("RT_PHONE_HASH_PEPPER", "praypal_sacred_pepper_default")
    key = f"{phone_hash}:{active_pepper}".encode("utf-8")
    return hmac.new(key, payload_str.encode("utf-8"), hashlib.sha256).hexdigest()


def generate_spiritual_journal(
    phone_hash: str,
    preferred_name: str | None = None,
    preferred_guide: str | None = None,
    loved_ones: list[dict[str, Any]] | None = None,
    answered_prayers: list[dict[str, Any]] | None = None,
    intentions: list[dict[str, Any]] | None = None,
    tokens: dict[str, Any] | None = None,
    export_format: Literal["markdown", "json"] = "markdown",
    pepper: str | None = None,
) -> str:
    """Generate an illuminated Spiritual Journal export for a caller."""
    name = preferred_name or "Beloved Soul"
    guide = preferred_guide or "Divine Presence"
    loved = loved_ones or []
    answered = answered_prayers or []
    active_intentions = intentions or []
    token_data = tokens or {"balance": 0, "candles_lit": 0, "sponsored_devotions": 0}

    timestamp_iso = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime())

    export_obj = {
        "title": f"Sacred Spiritual Journal of {name}",
        "timestamp": timestamp_iso,
        "phone_hash": phone_hash,
        "preferred_guide": guide,
        "loved_ones_tree": loved,
        "answered_prayers_chronicle": answered,
        "active_intentions": active_intentions,
        "blessings_and_tokens": token_data,
    }

    raw_json = json.dumps(export_obj, sort_keys=True)
    seal = compute_export_seal(phone_hash, raw_json, pepper=pepper)
    export_obj["integrity_seal"] = seal

    if export_format == "json":
        return json.dumps(export_obj, indent=2)

    # Markdown illuminated format
    md_lines = [
        f"# 🕊️ Sacred Spiritual Journal & Prayer Chronicle",
        f"**Sanctuary Member**: {name}  ",
        f"**Guiding Presence**: {guide}  ",
        f"**Generated**: {timestamp_iso}  ",
        f"**Sanctuary Token Record**: {phone_hash[:12]}...  ",
        "",
        "---",
        "",
        "## 1. Answered Prayers Chronicle",
    ]

    if answered:
        for item in answered:
            date_str = item.get("date", "Undated")
            title = item.get("petition", "Prayer Petition")
            testimony = item.get("testimony", "Grace received and answered in peace.")
            md_lines.append(f"- **{date_str} — {title}**")
            md_lines.append(f"  *Grace Received*: {testimony}")
    else:
        md_lines.append("*No answered prayers recorded yet. Keep faithful watch.*")

    md_lines.extend([
        "",
        "## 2. Loved Ones & Intercessory Prayer Tree",
    ])

    if loved:
        for p in loved:
            p_name = p.get("name", "Loved One")
            rel = p.get("relationship", "Family")
            note = p.get("intention", "Held in perpetual blessing")
            md_lines.append(f"- **{p_name}** ({rel}): {note}")
    else:
        md_lines.append("*No loved ones listed in intercessory tree.*")

    md_lines.extend([
        "",
        "## 3. Active Prayer Intentions",
    ])

    if active_intentions:
        for intent in active_intentions:
            title = intent.get("intention", "Active petition")
            status = intent.get("status", "Active")
            md_lines.append(f"- [{status.upper()}] {title}")
    else:
        md_lines.append("*All recorded prayer intentions are at rest in peace.*")

    md_lines.extend([
        "",
        "## 4. Blessings & Sanctuary Token Ledger",
        f"- **Available Blessing Tokens**: {token_data.get('balance', 0)}",
        f"- **Perpetual Altar Candles Lit**: {token_data.get('candles_lit', 0)}",
        f"- **Sponsored Elder Devotions**: {token_data.get('sponsored_devotions', 0)}",
        "",
        "---",
        f"*Cryptographic Authenticity Seal: `{seal}`*",
    ])

    return "\n".join(md_lines)


def verify_export_seal(journal_json_str: str, pepper: str | None = None) -> bool:
    """Verify that an exported journal's seal matches its contents."""
    try:
        data = json.loads(journal_json_str)
    except Exception:
        return False

    seal = data.get("integrity_seal")
    phone_hash = data.get("phone_hash")
    if not seal or not phone_hash:
        return False

    data_copy = dict(data)
    data_copy.pop("integrity_seal", None)
    raw = json.dumps(data_copy, sort_keys=True)
    expected_seal = compute_export_seal(phone_hash, raw, pepper=pepper)
    return hmac.compare_digest(seal, expected_seal)


def generate_sms_export_digest(
    phone_hash: str,
    answered_count: int,
    recent_answered: list[str] | None = None,
) -> str:
    """Generate a brief SMS-friendly spiritual journey digest."""
    items = recent_answered or []
    snippet = ""
    if items:
        snippet = " Latest: " + "; ".join(items[:2])
    return (
        f"🕊️ PrayPal Sacred Record: You have {answered_count} answered prayers chronicled in grace.{snippet} "
        f"Text JOURNAL for full encrypted export."
    )
