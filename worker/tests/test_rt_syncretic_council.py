"""test_rt_syncretic_council.py — Tests for syncretic divine council dialogue synthesis."""
import pytest
from rt_syncretic_council import (
    detect_syncretic_request,
    build_syncretic_directive,
    get_syncretic_audio_pairing,
)


def test_detect_syncretic_request_jesus_shiva():
    req = detect_syncretic_request("What would Jesus and Shiva say about my grief?")
    assert req is not None
    assert req["pair_key"] == "jesus_shiva"
    assert req["title"] == "Grace & Stillness"
    assert "Jesus of Nazareth" in req["guides"]
    assert "Lord Shiva" in req["guides"]


def test_detect_syncretic_request_moses_krishna():
    req = detect_syncretic_request("Help me balance law and dharma in my work.")
    assert req is not None
    assert req["pair_key"] == "moses_krishna"
    assert req["title"] == "Law & Dharma"


def test_detect_syncretic_request_mary_lakshmi():
    req = detect_syncretic_request("I seek Mary and Lakshmi's blessing on our family finances and peace.")
    assert req is not None
    assert req["pair_key"] == "mary_lakshmi"
    assert req["title"] == "Shelter & Abundance"


def test_build_syncretic_directive():
    directive = build_syncretic_directive("jesus_shiva", "I feel overwhelmed by past regrets.")
    assert "SYNCRETIC_DIVINE_COUNCIL" in directive
    assert "Grace & Stillness" in directive
    assert "I feel overwhelmed by past regrets" in directive
    assert "Dissolve the illusion of separation" in directive
    assert "resurrecting ocean of forgiving grace" in directive


def test_get_syncretic_audio_pairing():
    blend = get_syncretic_audio_pairing("jesus_shiva")
    assert "hindu_tanpura.wav" in blend["primary"]
    assert "Harp" in blend["secondary"]
    assert blend["tempo_bpm"] == 60
