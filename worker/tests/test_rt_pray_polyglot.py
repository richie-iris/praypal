"""test_rt_pray_polyglot.py — Tests for Real-Time Polyglot Cross-Generational Prayer Bridge."""
from __future__ import annotations

import pytest
import rt_pray_polyglot


def test_detect_prayer_language():
    # Devanagari (Hindi)
    assert rt_pray_polyglot.detect_prayer_language("ॐ नमः शिवाय") == "hi"
    assert rt_pray_polyglot.detect_prayer_language("हे प्रभु हमारी रक्षा करें") == "hi"

    # Gujarati
    assert rt_pray_polyglot.detect_prayer_language("જય શ્રી કૃષ્ણ") == "gu"

    # Arabic
    assert rt_pray_polyglot.detect_prayer_language("بِسْمِ اللَّهِ الرَّحْمَٰنِ الرَّحِيمِ") == "ar"

    # Hebrew
    assert rt_pray_polyglot.detect_prayer_language("שְׁמַע יִשְׂרָאֵל יְהוָה אֱלֹהֵינוּ יְהוָה אֶחָד") == "he"

    # Spanish
    assert rt_pray_polyglot.detect_prayer_language("Dios te bendiga con oracion y paz") == "es"

    # English fallback
    assert rt_pray_polyglot.detect_prayer_language("Please pray for my mother's health.") == "en"


def test_build_polyglot_directive():
    directive = rt_pray_polyglot.build_polyglot_directive(
        elder_lang="gu",
        youth_lang="en",
        elder_name="Baa (Grandmother)",
    )
    assert "REAL-TIME POLYGLOT PRAYER BRIDGE (GUJARATI & ENGLISH)" in directive
    assert "Baa (Grandmother)" in directive
    assert "જય શ્રી કૃષ્ણ" in directive
    assert "translate and repeat" in directive


def test_format_bilingual_prayer_card():
    card = rt_pray_polyglot.format_bilingual_prayer_card(
        original_text="सर्वे भवन्तु सुखिनः सर्वे सन्तु निरामयाः",
        translated_text="May all sentient beings be joyful and free from illness.",
        source_lang="hi",
        target_lang="en",
    )
    assert "PrayPal Cross-Generational Prayer" in card
    assert "[Hindi]:" in card
    assert "सर्वे भवन्तु सुखिनः" in card
    assert "[English Translation]:" in card
    assert "May all sentient beings be joyful" in card
