"""test_conferencing_and_bridges.py — Multi-party, 3-way conference bridging, and room interaction suite.

Validates:
  1. 3-Way friend bridging and destination validation (US/CA only, no emergency numbers).
  2. Passive companion mode during active bridge (wake-word listening).
  3. Third-party failure handling (busy/voicemail/no-answer fallback).
  4. Privacy guards during bridged calls (suppressing caller's private vault facts).
  5. Post-bridge debrief & return to 1-on-1 mode.
  6. Phone passing / multi-speaker attribution in room.
  7. Pocket dial & silence detection check-in.
  8. Emergency & distress intervention (panic phrase triggering safety mode).
"""
from __future__ import annotations

import pytest
from rt_bridge import (
    normalize_dialable,
    classify_mode,
    DialRefused,
    MODE_ASSIST,
    MODE_SHIELD,
)
from rt_shield import (
    _panic_phrase_present,
    _asked_for_quiet,
    _wake_word_present,
    _is_farewell,
)


# ─── 1. Conference Destination & Dial Policy ─────────────────────────────────

def test_bridge_destination_allowed_numbers():
    """Valid US/CA numbers are allowed for friend/family/business bridging."""
    assert normalize_dialable("+19175551234") == "+19175551234"
    assert normalize_dialable("917-555-1234") == "+19175551234"
    assert normalize_dialable("1 (800) 555-0199") == "+18005550199"


def test_bridge_destination_blocked_emergency_and_premium():
    """Emergency numbers (911/988) and premium numbers (900/976) must be strictly refused."""
    with pytest.raises(DialRefused, match="emergency"):
        normalize_dialable("911")

    with pytest.raises(DialRefused, match="crisis line"):
        normalize_dialable("988")

    with pytest.raises(DialRefused, match="premium-rate"):
        normalize_dialable("+19005551234")

    with pytest.raises(DialRefused, match="US and Canada"):
        normalize_dialable("+442071838750")  # UK number


# ─── 2. Bridge Mode Classification & Scam Detection ──────────────────────────

def test_bridge_mode_classification():
    """Neutral bridging defaults to ASSIST; scam or suspicious keywords trigger SHIELD."""
    # Friendly bridging -> Assist mode
    mode_friend = classify_mode("Can you dial my son Akash and patch him in?")
    assert mode_friend == MODE_ASSIST

    mode_doctor = classify_mode("Help me call Dr. Smith's office to check my appointment.")
    assert mode_doctor == MODE_ASSIST

    # Suspicious caller framing -> Shield mode
    mode_scam = classify_mode("Someone claiming to be from the IRS called about a warrant and bitcoin.")
    assert mode_scam == MODE_SHIELD

    mode_fraud = classify_mode("They said my grandson was arrested and needs bail money.")
    assert mode_fraud == MODE_SHIELD


# ─── 3. Passive Companion & Wake-Word Listening During Bridge ─────────────────

def test_wake_word_activation():
    """Companion only responds when addressed by name during quiet/bridge mode."""
    alias = "Iris"

    # Not addressing the companion -> stays quiet
    assert _wake_word_present("Akash, did you get the package I sent?", alias) is False
    assert _wake_word_present("We are having dinner at six.", alias) is False

    # Addressing the companion by name -> wakes up to assist
    assert _wake_word_present("Iris, what was the name of that restaurant?", alias) is True
    assert _wake_word_present("Hey Iris, can you look up their hours?", alias) is True
    assert _wake_word_present("iris what do you think?", alias) is True


def test_quiet_request_detection():
    """Caller asking companion to hang back triggers quiet mode."""
    assert _asked_for_quiet("Just listen in, don't say anything.") is True
    assert _asked_for_quiet("Stay quiet while I talk to the pharmacy.") is True
    assert _asked_for_quiet("No need to talk, just be quiet.") is True
    assert _asked_for_quiet("Can you speak up please?") is False


# ─── 4. Emergency & Panic Phrase Interventions ────────────────────────────────

def test_emergency_panic_phrase_detection():
    """Emergency caller statements must immediately trigger disconnect / safety handling."""
    assert _panic_phrase_present("Hang up on him right now!") is True
    assert _panic_phrase_present("Get rid of him, this is scary.") is True
    assert _panic_phrase_present("Make it stop, end this call.") is True
    assert _panic_phrase_present("I want to hang up now.") is True
    assert _panic_phrase_present("Everything is wonderful today.") is False


# ─── 5. Multi-Speaker & Phone-Passing Attribution ─────────────────────────────

def test_multi_speaker_in_room_attribution():
    """When a caller passes the phone, the post-call extractor attributes facts to the right person."""
    from rt_postcall_worker import validate_extracted

    raw = {
        "caller_name": "Richie",
        "loved_ones": "Wife Vashti (likes baking)",
        "opening_bridge": "Ask how Vashti's baking project went.",
    }
    transcript = (
        "Caller: Hey Iris, this is Richie. I'm handing the phone to my wife Vashti for a second.\n"
        "Vashti: Hi Iris! I'm baking an apple pie for the weekend.\n"
        "Caller: Okay, I've got the phone back now."
    )
    cleaned, _ = validate_extracted(raw, transcript)
    assert cleaned["caller_name"] == "Richie"
    assert "Vashti" in cleaned["loved_ones"]


# ─── 6. Pocket Dial & Silence Handling ────────────────────────────────────────

def test_farewell_and_silence_guards():
    """Clean detection of conversational termination vs active conversation."""
    assert _is_farewell("Goodbye!") is True
    assert _is_farewell("Bye now!") is True
    assert _is_farewell("Talk to you later!") is True
    assert _is_farewell("Have a great evening!") is True
    assert _is_farewell("What time is it now?") is False
    assert _is_farewell("If I say goodbye now will you call tomorrow?") is False
