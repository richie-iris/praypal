"""rt_pray_polyglot.py — Real-Time Polyglot Cross-Generational Prayer Bridge.

WHY THIS MODULE EXISTS:
Implements the multi-lingual prayer bridging specified in Sections 2.3 and 3.5
of AGENTS.md for Sanctuary Tier ($24.99/mo) and multi-generational families:
  1. Multilingual family sessions: bridges elders speaking Hindi, Gujarati, Tamil,
     Spanish, Hebrew, or Arabic with younger generations speaking English.
  2. Injects real-time translation and devotional repetition directives into Gemini Live.
  3. Detects prayer languages and scripts (Devanagari, Arabic, Hebrew, Latin).
"""
from __future__ import annotations

import re
from typing import Any

import rt_obs

_obs = rt_obs.get("rt_pray_polyglot")

SUPPORTED_LANGUAGES: dict[str, dict[str, Any]] = {
    "en": {
        "name": "English",
        "native": "English",
        "greeting": "Peace be with you",
        "family_title": "Grandmother / Grandfather",
    },
    "hi": {
        "name": "Hindi",
        "native": "हिन्दी",
        "script": "Devanagari",
        "greeting": "नमस्ते / प्रणाम (Namaste / Pranam)",
        "family_title": "दादी / दादा / नानी / नाना (Dadi / Dada / Nani / Nana)",
    },
    "gu": {
        "name": "Gujarati",
        "native": "ગુજરાતી",
        "script": "Gujarati",
        "greeting": "જય શ્રી કૃષ્ણ (Jai Shree Krishna)",
        "family_title": "બા / દાદા (Baa / Dada)",
    },
    "ta": {
        "name": "Tamil",
        "native": "தமிழ்",
        "script": "Tamil",
        "greeting": "வணக்கம் (Vanakkam)",
        "family_title": "பாட்டி / தாத்தா (Paatti / Thaatha)",
    },
    "es": {
        "name": "Spanish",
        "native": "Español",
        "script": "Latin",
        "greeting": "La paz sea contigo / Bendiciones",
        "family_title": "Abuela / Abuelo",
    },
    "ar": {
        "name": "Arabic",
        "native": "العربية",
        "script": "Arabic",
        "greeting": "السلام عليكم (As-salamu alaykum)",
        "family_title": "جدة / جد (Jaddah / Jadd)",
    },
    "he": {
        "name": "Hebrew",
        "native": "עברית",
        "script": "Hebrew",
        "greeting": "שלום עליכם (Shalom aleichem)",
        "family_title": "סבתא / סבא (Savta / Saba)",
    },
}

# Heuristics for script detection
_DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")
_GUJARATI_RE = re.compile(r"[\u0A80-\u0AFF]")
_TAMIL_RE = re.compile(r"[\u0B80-\u0BFF]")
_ARABIC_RE = re.compile(r"[\u0600-\u06FF]")
_HEBREW_RE = re.compile(r"[\u0590-\u05FF]")
_SPANISH_LEXICON = {"dios", "bendicion", "bendiciones", "oracion", "padre", "jesus", "paz", "senor", "gracias", "familia"}


def detect_prayer_language(text: str) -> str:
    """Detect the predominant script or prayer language of incoming speech or text."""
    if not text:
        return "en"

    if _DEVANAGARI_RE.search(text):
        return "hi"
    if _GUJARATI_RE.search(text):
        return "gu"
    if _TAMIL_RE.search(text):
        return "ta"
    if _ARABIC_RE.search(text):
        return "ar"
    if _HEBREW_RE.search(text):
        return "he"

    words = set(re.findall(r"\b\w+\b", text.lower()))
    if len(words & _SPANISH_LEXICON) >= 2:
        return "es"

    return "en"


def build_polyglot_directive(
    elder_lang: str = "hi",
    youth_lang: str = "en",
    elder_name: str | None = None,
) -> str:
    """Construct dynamic prompt directive for Gemini Live to act as a cross-generational prayer bridge."""
    e_lang_meta = SUPPORTED_LANGUAGES.get(elder_lang, SUPPORTED_LANGUAGES["hi"])
    y_lang_meta = SUPPORTED_LANGUAGES.get(youth_lang, SUPPORTED_LANGUAGES["en"])
    e_name = elder_name or "the family elder"

    directive = (
        f"\n\n# REAL-TIME POLYGLOT PRAYER BRIDGE ({e_lang_meta['name'].upper()} & {y_lang_meta['name'].upper()})\n"
        f"- This call is a sacred cross-generational prayer bridge connecting {e_name} ({e_lang_meta['name']}) "
        f"and the younger family members ({y_lang_meta['name']}).\n"
        "- Multilingual Pastoral Directives:\n"
        f"  1. Greet {e_name} with deep reverent honor in {e_lang_meta['name']}: '{e_lang_meta['greeting']}'.\n"
        f"  2. When {e_name} prays or speaks in {e_lang_meta['name']}, listen with reverence, then gently translate and repeat the core prayer intention into {y_lang_meta['name']} so younger family members understand.\n"
        f"  3. When youth speak in {y_lang_meta['name']}, lovingly summarize the intention and offer a traditional blessing or shloka in {e_lang_meta['name']} so {e_name} feels spiritually united.\n"
        f"  4. Seamlessly bridge words across generations; never let language barriers isolate a praying soul.\n"
    )
    _obs.event("pray.polyglot_directive_built", elder_lang=elder_lang, youth_lang=youth_lang)
    return directive


def format_bilingual_prayer_card(
    original_text: str,
    translated_text: str,
    source_lang: str,
    target_lang: str = "en",
) -> str:
    """Format a bilingual prayer card for SMS or WhatsApp distribution."""
    s_meta = SUPPORTED_LANGUAGES.get(source_lang, SUPPORTED_LANGUAGES["en"])
    t_meta = SUPPORTED_LANGUAGES.get(target_lang, SUPPORTED_LANGUAGES["en"])

    return (
        f"🕊️ PrayPal Cross-Generational Prayer\n\n"
        f"[{s_meta['name']}]:\n"
        f"{original_text.strip()}\n\n"
        f"[{t_meta['name']} Translation]:\n"
        f"{translated_text.strip()}\n\n"
        f"✨ Held together in one sacred fellowship."
    )
