"""rt_pray_bank.py — The Prayer Bank & "Tokens to Blessings" Economy.

WHY THIS MODULE EXISTS:
Implements the sacred micro-economy specified in Section 3.9 of AGENTS.md:
  1. Blessing Tokens:
     - Callers receive tokens with subscriptions or gratitude gifts.
     - Redemption:
       · light_altar_candle: 1 token (illuminates 7-day perpetual sanctuary altar candle).
       · sponsor_elder_devotion: 5 tokens (enqueues automated morning prayer call to an elder).
       · commission_studio_hymn: 10 tokens (commissions dedicated vocal hymn/bhajan).
  2. The Reciprocity Grace Fund:
     - When a prayer is answered, callers deposit a gratitude blessing.
     - Gratitude blessings fund free prayer calls for callers in severe grief or
       financial hardship who dial in on the Grace Tier ($0.98 unit COGS grant).
  3. Strict Confessional Privacy:
     - Callers indexed solely by HMAC-SHA256 phone hash.
     - forget_caller_bank() atomically wipes all tokens and intentions.
"""
from __future__ import annotations

import datetime
import os
import threading
import time
from typing import Any

import rt_obs
import rt_prefs

_obs = rt_obs.get("rt_pray_bank")

# Token costs
COST_LIGHT_CANDLE = 1
COST_SPONSOR_ELDER = 5
COST_STUDIO_HYMN = 10

# In-memory store with persistence support
_LOCK = threading.Lock()
_BALANCES: dict[str, int] = {}
_TRANSACTIONS: list[dict[str, Any]] = []
_ALTAR_CANDLES: list[dict[str, Any]] = []
_SPONSORED_ELDERS: list[dict[str, Any]] = []
_GRACE_FUND_TOKENS: int = 25  # Seeded with initial community blessings


def get_balance(phone_hash: str) -> int:
    """Return the available blessing token balance for a caller."""
    if not phone_hash:
        return 0
    with _LOCK:
        return _BALANCES.get(phone_hash, 0)


def grant_tokens(phone_hash: str, count: int, reason: str = "subscription") -> int:
    """Grant blessing tokens to a caller."""
    if not phone_hash or count <= 0:
        return get_balance(phone_hash)
    with _LOCK:
        current = _BALANCES.get(phone_hash, 0)
        new_balance = current + count
        _BALANCES[phone_hash] = new_balance
        _TRANSACTIONS.append({
            "phone_hash": phone_hash,
            "type": "grant",
            "count": count,
            "balance": new_balance,
            "reason": reason,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        })
    _obs.event("pray.tokens_granted", count=count, reason=reason)
    return new_balance


def light_altar_candle(phone_hash: str, intention_text: str, tradition: str = "Universal") -> dict[str, Any]:
    """Redeem 1 token to illuminate a 7-day perpetual sanctuary altar candle."""
    if not phone_hash:
        return {"status": "error", "error": "missing_phone_hash"}
    if not intention_text or not intention_text.strip():
        return {"status": "error", "error": "empty_intention"}

    with _LOCK:
        bal = _BALANCES.get(phone_hash, 0)
        if bal < COST_LIGHT_CANDLE:
            return {"status": "insufficient_tokens", "required": COST_LIGHT_CANDLE, "balance": bal}

        _BALANCES[phone_hash] = bal - COST_LIGHT_CANDLE
        now = datetime.datetime.now(datetime.timezone.utc)
        expires_at = now + datetime.timedelta(days=7)
        candle = {
            "candle_id": f"candle-{int(now.timestamp())}-{len(_ALTAR_CANDLES)+1}",
            "phone_hash": phone_hash,
            "intention": intention_text.strip()[:280],
            "tradition": tradition,
            "lit_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
            "active": True,
        }
        _ALTAR_CANDLES.append(candle)
        _TRANSACTIONS.append({
            "phone_hash": phone_hash,
            "type": "redeem_candle",
            "cost": COST_LIGHT_CANDLE,
            "balance": _BALANCES[phone_hash],
            "candle_id": candle["candle_id"],
            "timestamp": now.isoformat(),
        })

    _obs.event("pray.candle_lit", tradition=tradition)
    return {"status": "ok", "candle": candle, "remaining_balance": _BALANCES[phone_hash]}


def _mask_phone(e164: str | None) -> str:
    if not e164:
        return "[none]"
    s = str(e164).strip()
    return f"{s[:5]}...{s[-4:]}" if len(s) >= 9 else "[masked]"


def sponsor_elder_devotion(
    sponsor_hash: str,
    elder_e164: str,
    guide_key: str = "jesus",
    custom_note: str | None = None,
) -> dict[str, Any]:
    """Redeem 5 tokens to sponsor an automated morning prayer devotion call to an elder parent."""
    if not sponsor_hash:
        return {"status": "error", "error": "missing_sponsor_hash"}
    norm_elder = rt_prefs.normalize_e164(elder_e164)
    if not norm_elder:
        return {"status": "error", "error": "invalid_elder_phone"}

    with _LOCK:
        bal = _BALANCES.get(sponsor_hash, 0)
        if bal < COST_SPONSOR_ELDER:
            return {"status": "insufficient_tokens", "required": COST_SPONSOR_ELDER, "balance": bal}

        _BALANCES[sponsor_hash] = bal - COST_SPONSOR_ELDER
        now = datetime.datetime.now(datetime.timezone.utc)
        elder_entry = {
            "sponsor_hash": sponsor_hash,
            "elder_hash": rt_prefs.phone_hash(norm_elder),
            "elder_e164_masked": _mask_phone(norm_elder),
            "guide_key": guide_key,
            "note": (custom_note or "").strip()[:200],
            "status": "enqueued",
            "created_at": now.isoformat(),
        }
        _SPONSORED_ELDERS.append(elder_entry)
        _TRANSACTIONS.append({
            "phone_hash": sponsor_hash,
            "type": "sponsor_elder",
            "cost": COST_SPONSOR_ELDER,
            "balance": _BALANCES[sponsor_hash],
            "timestamp": now.isoformat(),
        })

    _obs.event("pray.elder_sponsored", guide=guide_key)
    return {"status": "ok", "devotion": elder_entry, "remaining_balance": _BALANCES[sponsor_hash]}


def commission_studio_hymn(phone_hash: str, hymn_title: str, dedication: str | None = None) -> dict[str, Any]:
    """Redeem 10 tokens to commission a studio-recorded sacred hymn."""
    if not phone_hash:
        return {"status": "error", "error": "missing_phone_hash"}
    if not hymn_title:
        return {"status": "error", "error": "missing_hymn_title"}

    with _LOCK:
        bal = _BALANCES.get(phone_hash, 0)
        if bal < COST_STUDIO_HYMN:
            return {"status": "insufficient_tokens", "required": COST_STUDIO_HYMN, "balance": bal}

        _BALANCES[phone_hash] = bal - COST_STUDIO_HYMN
        now = datetime.datetime.now(datetime.timezone.utc)
        record = {
            "phone_hash": phone_hash,
            "hymn_title": hymn_title.strip()[:100],
            "dedication": (dedication or "").strip()[:150],
            "status": "queued",
            "timestamp": now.isoformat(),
        }
        _TRANSACTIONS.append({
            "phone_hash": phone_hash,
            "type": "commission_hymn",
            "cost": COST_STUDIO_HYMN,
            "balance": _BALANCES[phone_hash],
            "timestamp": now.isoformat(),
        })

    _obs.event("pray.hymn_commissioned", title=hymn_title)
    return {"status": "ok", "commission": record, "remaining_balance": _BALANCES[phone_hash]}


def deposit_gratitude_blessing(phone_hash: str, answered_prayer_summary: str, token_amount: int = 1) -> dict[str, Any]:
    """Deposit a gratitude blessing into the Reciprocity Grace Fund following an answered prayer."""
    global _GRACE_FUND_TOKENS
    if token_amount <= 0:
        return {"status": "error", "error": "invalid_amount"}

    with _LOCK:
        bal = _BALANCES.get(phone_hash, 0)
        if bal < token_amount:
            return {"status": "insufficient_tokens", "required": token_amount, "balance": bal}

        _BALANCES[phone_hash] = bal - token_amount
        _GRACE_FUND_TOKENS += token_amount
        now = datetime.datetime.now(datetime.timezone.utc)
        _TRANSACTIONS.append({
            "phone_hash": phone_hash,
            "type": "grace_fund_deposit",
            "tokens": token_amount,
            "answered_summary": (answered_prayer_summary or "").strip()[:200],
            "grace_fund_total": _GRACE_FUND_TOKENS,
            "timestamp": now.isoformat(),
        })

    _obs.event("pray.grace_fund_deposit", tokens=token_amount)
    return {
        "status": "ok",
        "deposited": token_amount,
        "remaining_balance": _BALANCES[phone_hash],
        "grace_fund_total": _GRACE_FUND_TOKENS,
    }


def claim_grace_fund_grant(seeker_hash: str) -> dict[str, Any]:
    """Allocate a free 5-minute prayer call grant from the Reciprocity Grace Fund for a seeker in hardship."""
    global _GRACE_FUND_TOKENS
    with _LOCK:
        if _GRACE_FUND_TOKENS <= 0:
            return {"status": "fund_exhausted", "grace_fund_total": 0}

        _GRACE_FUND_TOKENS -= 1
        now = datetime.datetime.now(datetime.timezone.utc)
        _TRANSACTIONS.append({
            "phone_hash": seeker_hash,
            "type": "grace_fund_grant",
            "grant_units": 1,
            "grace_fund_total": _GRACE_FUND_TOKENS,
            "timestamp": now.isoformat(),
        })

    _obs.event("pray.grace_fund_grant")
    return {"status": "ok", "granted": True, "grace_fund_total": _GRACE_FUND_TOKENS}


def get_active_candles() -> list[dict[str, Any]]:
    """Return all currently active perpetual candles."""
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with _LOCK:
        return [c for c in _ALTAR_CANDLES if c["active"] and c["expires_at"] > now_iso]


def get_grace_fund_stats() -> dict[str, Any]:
    """Return community Grace Fund metrics."""
    with _LOCK:
        return {
            "grace_fund_tokens": _GRACE_FUND_TOKENS,
            "active_candles": len([c for c in _ALTAR_CANDLES if c["active"]]),
            "sponsored_elders": len(_SPONSORED_ELDERS),
            "total_transactions": len(_TRANSACTIONS),
        }


def forget_caller_bank(phone_hash: str) -> bool:
    """Atomically wipe all bank accounts, intentions, and transaction logs for a forgotten caller."""
    if not phone_hash:
        return False
    global _BALANCES, _TRANSACTIONS, _ALTAR_CANDLES, _SPONSORED_ELDERS
    with _LOCK:
        _BALANCES.pop(phone_hash, None)
        _TRANSACTIONS = [t for t in _TRANSACTIONS if t.get("phone_hash") != phone_hash]
        _ALTAR_CANDLES = [c for c in _ALTAR_CANDLES if c.get("phone_hash") != phone_hash]
        _SPONSORED_ELDERS = [e for e in _SPONSORED_ELDERS if e.get("sponsor_hash") != phone_hash and e.get("elder_hash") != phone_hash]
    return True


def reset_bank_for_testing() -> None:
    """Reset store state for clean unit test isolation."""
    global _BALANCES, _TRANSACTIONS, _ALTAR_CANDLES, _SPONSORED_ELDERS, _GRACE_FUND_TOKENS
    with _LOCK:
        _BALANCES.clear()
        _TRANSACTIONS.clear()
        _ALTAR_CANDLES.clear()
        _SPONSORED_ELDERS.clear()
        _GRACE_FUND_TOKENS = 25
