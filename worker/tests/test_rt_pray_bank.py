"""test_rt_pray_bank.py — Tests for the Prayer Bank & Tokens to Blessings Economy."""
from __future__ import annotations

import pytest
import rt_pray_bank


@pytest.fixture(autouse=True)
def clean_bank():
    rt_pray_bank.reset_bank_for_testing()
    yield
    rt_pray_bank.reset_bank_for_testing()


def test_token_granting_and_balance():
    h = "caller_hash_1"
    assert rt_pray_bank.get_balance(h) == 0

    new_bal = rt_pray_bank.grant_tokens(h, 10, reason="devotion_tier_sub")
    assert new_bal == 10
    assert rt_pray_bank.get_balance(h) == 10


def test_light_altar_candle_redemption():
    h = "caller_hash_candle"
    # Insufficient tokens
    res = rt_pray_bank.light_altar_candle(h, "Peace for my grandmother")
    assert res["status"] == "insufficient_tokens"

    # Grant and redeem
    rt_pray_bank.grant_tokens(h, 3)
    res = rt_pray_bank.light_altar_candle(h, "Peace for my grandmother", tradition="Christian")
    assert res["status"] == "ok"
    assert res["remaining_balance"] == 2
    assert res["candle"]["active"] is True
    assert "candle-" in res["candle"]["candle_id"]

    candles = rt_pray_bank.get_active_candles()
    assert len(candles) == 1
    assert candles[0]["intention"] == "Peace for my grandmother"


def test_sponsor_elder_devotion():
    h = "caller_hash_sponsor"
    # Insufficient tokens
    res = rt_pray_bank.sponsor_elder_devotion(h, "+15559876543", guide_key="krishna", custom_note="Morning Gita chant")
    assert res["status"] == "insufficient_tokens"

    # Grant 5 tokens
    rt_pray_bank.grant_tokens(h, 5)
    res = rt_pray_bank.sponsor_elder_devotion(h, "+15559876543", guide_key="krishna", custom_note="Morning Gita chant")
    assert res["status"] == "ok"
    assert res["remaining_balance"] == 0
    assert res["devotion"]["guide_key"] == "krishna"
    assert res["devotion"]["status"] == "enqueued"


def test_commission_studio_hymn():
    h = "caller_hash_hymn"
    rt_pray_bank.grant_tokens(h, 12)
    res = rt_pray_bank.commission_studio_hymn(h, "Amazing Grace (Cathedral Organ)", "In memory of my mother")
    assert res["status"] == "ok"
    assert res["remaining_balance"] == 2
    assert res["commission"]["status"] == "queued"


def test_reciprocity_grace_fund():
    h = "caller_grateful"
    rt_pray_bank.grant_tokens(h, 5)

    stats_before = rt_pray_bank.get_grace_fund_stats()
    initial_tokens = stats_before["grace_fund_tokens"]

    # Deposit gratitude blessing for answered prayer
    res = rt_pray_bank.deposit_gratitude_blessing(h, "Mother's cancer surgery was successful and clean margins!", 2)
    assert res["status"] == "ok"
    assert res["deposited"] == 2
    assert res["remaining_balance"] == 3
    assert res["grace_fund_total"] == initial_tokens + 2

    # Seeker claims grant for free call
    claim_res = rt_pray_bank.claim_grace_fund_grant("seeker_in_need")
    assert claim_res["status"] == "ok"
    assert claim_res["granted"] is True
    assert claim_res["grace_fund_total"] == initial_tokens + 1


def test_forget_caller_bank():
    h = "caller_to_wipe"
    rt_pray_bank.grant_tokens(h, 10)
    rt_pray_bank.light_altar_candle(h, "Intention to wipe")
    assert rt_pray_bank.get_balance(h) == 9

    assert rt_pray_bank.forget_caller_bank(h) is True
    assert rt_pray_bank.get_balance(h) == 0
    assert not any(c["phone_hash"] == h for c in rt_pray_bank.get_active_candles())
