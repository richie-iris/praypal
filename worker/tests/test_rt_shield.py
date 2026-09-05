"""test_rt_shield.py — Comprehensive unit tests for ScamGuard fraud detection, panic drop, and wake-words."""
from __future__ import annotations

from rt_shield import (
    _panic_phrase_present,
    _asked_for_quiet,
    _wake_word_present,
    _is_farewell,
)
from rt_bridge import scan_signatures, scan_transcript, classify_mode, MODE_SHIELD, MODE_ASSIST


def test_panic_phrase_detection():
    # True positives
    assert _panic_phrase_present("Please hang up on him right now!") is True
    assert _panic_phrase_present("Get rid of them, I'm scared") is True
    assert _panic_phrase_present("Make it stop, drop the call") is True
    assert _panic_phrase_present("End this call immediately") is True

    # True negatives
    assert _panic_phrase_present("I hung up on him yesterday afternoon") is False
    assert _panic_phrase_present("Let's talk about the grocery list") is False


def test_fraud_signatures_all_categories():
    # 1. Gift cards
    hits = scan_signatures("Go to Walmart and purchase three Apple gift cards.")
    assert any(k == "payment_gift_cards" for k, _ in hits)

    # 2. Wire / Crypto
    hits = scan_signatures("Deposit $2,000 at the nearest Bitcoin ATM machine.")
    assert any(k == "payment_wire_crypto" for k, _ in hits)

    # 3. Secrecy
    hits = scan_signatures("Keep this a secret between us, don't tell your family.")
    assert any(k == "secrecy" for k, _ in hits)

    # 4. Authority claim
    hits = scan_signatures("I'm calling from the IRS and there is a warrant for your arrest.")
    assert any(k == "authority_claim" for k, _ in hits)

    # 5. Grandchild in trouble
    hits = scan_signatures("Your grandson was in a terrible accident and is in jail needing bail.")
    assert any(k == "grandchild_trouble" for k, _ in hits)

    # 6. Remote access
    hits = scan_signatures("Download AnyDesk and let me connect to your computer.")
    assert any(k == "remote_access" for k, _ in hits)

    # 7. Credential request
    hits = scan_signatures("Read me the verification code you just received by text.")
    assert any(k == "credential_request" for k, _ in hits)

    # 8. Urgency threat
    hits = scan_signatures("Do this right now or your bank account will be frozen.")
    assert any(k == "urgency_threat" for k, _ in hits)


def test_scan_transcript_speaker_isolation():
    # Caller repeating the scam phrase must NOT trigger false positive
    caller_only = """
    caller: He told me on the phone that I need to buy Apple gift cards.
    caller: Can you believe that?
    """
    assert len(scan_transcript(caller_only, speaker_prefix="line:")) == 0

    # Stranger speaking the phrase MUST trigger positive
    stranger_speaking = """
    caller: Hello?
    line: You must purchase Apple gift cards to clear your tax bill.
    """
    stranger_hits = scan_transcript(stranger_speaking, speaker_prefix="line:")
    assert len(stranger_hits) >= 1
    assert stranger_hits[0][0] == "payment_gift_cards"


def test_wake_word_present():
    assert _wake_word_present("Hey Clara, what do you think of this?", "Clara") is True
    assert _wake_word_present("Clara, can you hear this guy?", "Clara") is True
    assert _wake_word_present("What do you think of this, friend?", "Clara") is False
    assert _wake_word_present("I'm talking to my doctor", "your companion") is False


def test_asked_for_quiet():
    assert _asked_for_quiet("Just listen for a second") is True
    assert _asked_for_quiet("Stay quiet while I speak to him") is True
    assert _asked_for_quiet("Don't say anything yet") is True
    assert _asked_for_quiet("I like listening to classical music") is False


def test_classify_bridge_mode():
    assert classify_mode("Call Dr. Robert's clinic for an appointment") == MODE_ASSIST
    assert classify_mode("I got a weird call saying I have a warrant from the IRS") == MODE_SHIELD
    assert classify_mode("Someone said my grandson is arrested") == MODE_SHIELD


def test_is_farewell():
    assert _is_farewell("Goodbye, talk later!") is True
    assert _is_farewell("Bye bye") is True
    assert _is_farewell("Have a wonderful evening dear") is True
    assert _is_farewell("If I say goodbye now will you call tomorrow?") is False
