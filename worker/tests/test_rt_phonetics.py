"""test_rt_phonetics.py — Validation for Synchronized Phonetic Enunciation Engine.

Tests:
1. Sacred chants catalog coverage across Vedic, Abrahamic, Christian, Islamic, Buddhist traditions.
2. Syllable stress parsing with uppercase/accent identification.
3. LiveKit data channel payload formatting and boundary handling.
4. Async broadcasting with mock room & participant interfaces.
"""
from __future__ import annotations

import json
import pytest
from rt_phonetics import (
    SACRED_CHANTS,
    build_enunciation_payload,
    broadcast_enunciation_packet,
    get_sacred_chant,
    list_sacred_chants,
    parse_syllables_and_stress,
)


def test_sacred_chants_catalog_breadth():
    """Verify catalog has authentic representation across all core traditions."""
    assert len(SACRED_CHANTS) >= 8
    
    traditions = {c["tradition"].lower() for c in SACRED_CHANTS.values()}
    assert any("hindu" in t or "vedic" in t for t in traditions)
    assert any("judaism" in t or "covenant" in t for t in traditions)
    assert any("christian" in t for t in traditions)
    assert any("islam" in t for t in traditions)
    assert any("buddhist" in t for t in traditions)

    # Every chant must have required keys
    for k, chant in SACRED_CHANTS.items():
        assert chant["id"] == k
        assert chant["title"]
        assert chant["original_script"]
        assert chant["romanized"]
        assert chant["phonetic_guide"]
        assert chant["english_meaning"]
        assert len(chant["syllables"]) > 0


def test_syllable_stress_parsing():
    """Verify upper-case segments are identified as stressed syllables."""
    phonetic = "OM na-MAH shi-VAH-ya"
    parsed = parse_syllables_and_stress(phonetic)
    assert len(parsed) == 6
    
    assert parsed[0]["syllable"] == "OM"
    assert parsed[0]["stressed"] is True
    
    assert parsed[1]["syllable"] == "na"
    assert parsed[1]["stressed"] is False

    assert parsed[2]["syllable"] == "MAH"
    assert parsed[2]["stressed"] is True

    assert parsed[3]["syllable"] == "shi"
    assert parsed[3]["stressed"] is False

    assert parsed[4]["syllable"] == "VAH"
    assert parsed[4]["stressed"] is True


def test_chant_lookup_and_filtering():
    """Verify exact and fuzzy lookup and guide-based filtering."""
    shiva = get_sacred_chant("om_namah_shivaya")
    assert shiva is not None
    assert shiva["guide_key"] == "shiva"

    # Fuzzy match by title component
    shema = get_sacred_chant("shema")
    assert shema is not None
    assert "Shema" in shema["title"]
    assert shema["guide_key"] == "moses"

    # Filter by guide
    krishna_chants = list_sacred_chants(guide_key="krishna")
    assert any(c["id"] == "hare_krishna" for c in krishna_chants)

    # Filter by tradition
    christian_chants = list_sacred_chants(tradition="christian")
    assert any(c["id"] == "lords_prayer_aramaic" for c in christian_chants)


def test_build_enunciation_payload():
    """Verify JSON structure sent over LiveKit Room Data Channel."""
    payload = build_enunciation_payload("shema_yisrael", active_syllable_idx=2)
    assert payload["type"] == "sanctuary_phonetics"
    assert payload["id"] == "shema_yisrael"
    assert payload["script"] == "Hebrew"
    assert payload["active_syllable_idx"] == 2
    assert payload["timestamp_ms"] > 0
    assert len(payload["syllables"]) > 0

    # Test clamping on index overflow
    payload_overflow = build_enunciation_payload("shema_yisrael", active_syllable_idx=999)
    assert payload_overflow["active_syllable_idx"] == len(payload_overflow["syllables"]) - 1

    # Test fallback on custom prayer string
    custom = build_enunciation_payload("Lead us not into temptation but deliver us from evil")
    assert custom["type"] == "sanctuary_phonetics"
    assert custom["id"] == "custom"
    assert len(custom["syllables"]) > 0


@pytest.mark.asyncio
async def test_broadcast_enunciation_packet_success():
    """Verify broadcast works with mock LiveKit room."""
    published_packets = []

    class MockLocalParticipant:
        async def publish_data(self, data: bytes, topic: str):
            published_packets.append({"data": data, "topic": topic})

    class MockRoom:
        def __init__(self):
            self.local_participant = MockLocalParticipant()

    room = MockRoom()
    payload = build_enunciation_payload("om_namah_shivaya", active_syllable_idx=0)
    success = await broadcast_enunciation_packet(room, payload, topic="phonetics")

    assert success is True
    assert len(published_packets) == 1
    assert published_packets[0]["topic"] == "phonetics"
    
    decoded = json.loads(published_packets[0]["data"].decode("utf-8"))
    assert decoded["id"] == "om_namah_shivaya"


@pytest.mark.asyncio
async def test_broadcast_enunciation_packet_none_room():
    """Verify graceful handling when room is disconnected or None."""
    payload = build_enunciation_payload("om_namah_shivaya")
    assert await broadcast_enunciation_packet(None, payload) is False
