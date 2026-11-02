"""rt_phonetics.py — Synchronized Phonetic Enunciation Engine.

WHY THIS MODULE EXISTS:
Sacred prayer and chanting across ancient traditions (Vedic Sanskrit, Biblical Hebrew,
Classical Arabic, Galilean Aramaic, Greek, and Latin) carry precise sacred resonance
and syllable stress. This engine:
  1. Maintains an authentic multi-tradition catalog of sacred chants, mantras, and prayers.
  2. Dynamically parses syllables and phonetic stress indicators.
  3. Formats and broadcasts synchronized phonetic packets over LiveKit Room Data Channels
     so mobile and browser altars display synchronized Devanagari, Hebrew, Arabic,
     phonetic pronunciation guides, and English meanings in real-time.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

# ── SACRED CHANTS & MANTRAS CATALOG ─────────────────────────────────────────

SACRED_CHANTS: dict[str, dict[str, Any]] = {
    "om_namah_shivaya": {
        "id": "om_namah_shivaya",
        "title": "Shiva Panchakshara Mantra",
        "tradition": "Hindu (Shaivite)",
        "language": "Sanskrit",
        "script": "Devanagari",
        "original_script": "ॐ नमः शिवाय",
        "romanized": "Om Namah Shivaya",
        "phonetic_guide": "OM na-MAH shi-VAH-ya",
        "guide_key": "shiva",
        "english_meaning": "I bow to Shiva, the sacred stillness and divine reality within.",
        "syllables": [
            {"text": "Om", "phonetic": "OM", "stressed": True, "meaning": "Primordial Sound"},
            {"text": "Na", "phonetic": "na", "stressed": False, "meaning": "Earth / Creation"},
            {"text": "mah", "phonetic": "MAH", "stressed": True, "meaning": "Water / Sustenance"},
            {"text": "Shi", "phonetic": "shi", "stressed": False, "meaning": "Fire / Transformation"},
            {"text": "va", "phonetic": "VAH", "stressed": True, "meaning": "Air / Movement"},
            {"text": "ya", "phonetic": "ya", "stressed": False, "meaning": "Space / Divine Soul"},
        ],
    },
    "mahamrityunjaya": {
        "id": "mahamrityunjaya",
        "title": "Maha Mrityunjaya Mantra (Healing & Liberation)",
        "tradition": "Hindu (Vedic)",
        "language": "Sanskrit",
        "script": "Devanagari",
        "original_script": "ॐ त्र्यम्बकं यजामहे सुगन्धिं पुष्टिवर्धनम् उर्वारुकमिव बन्धनान् मृत्योर्मुक्षीय मामृतात्",
        "romanized": "Om Tryambakam Yajamahe Sugandhim Pushti-Vardhanam Urvarukamiva Bandhanan Mrityor Mukshiya Maamritat",
        "phonetic_guide": "OM try-AM-ba-kam ya-JAA-ma-hay soo-GAN-dhim poosh-ti var-DHA-nam oor-vaa-roo-KUM-i-va bun-DHA-naan mrit-YOR mook-SHEE-ya maam-ri-TAAT",
        "guide_key": "shiva",
        "english_meaning": "We worship the Three-Eyed One, fragrant and nourishing all beings. As a ripe cucumber is released from its vine, may we be liberated from death and mortality into immortality.",
        "syllables": [
            {"text": "Om", "phonetic": "OM", "stressed": True, "meaning": "Sacred Source"},
            {"text": "Tryam-ba-kam", "phonetic": "try-AM-ba-kam", "stressed": True, "meaning": "Three-Eyed Lord"},
            {"text": "Ya-ja-ma-he", "phonetic": "ya-JAA-ma-hay", "stressed": False, "meaning": "We honor and meditate upon"},
            {"text": "Su-gan-dhim", "phonetic": "soo-GAN-dhim", "stressed": True, "meaning": "Fragrant presence"},
            {"text": "Push-ti-var-dha-nam", "phonetic": "poosh-ti var-DHA-nam", "stressed": False, "meaning": "Nourisher of all life"},
            {"text": "Ur-va-ru-ka-miv", "phonetic": "oor-vaa-roo-KUM-i-va", "stressed": False, "meaning": "Like a vine-bound fruit"},
            {"text": "Ban-dha-nat", "phonetic": "bun-DHA-naat", "stressed": True, "meaning": "From attachment & bondage"},
            {"text": "Mrit-yor-muk-shi-ya", "phonetic": "mrit-YOR mook-SHEE-ya", "stressed": True, "meaning": "Liberate us from mortality"},
            {"text": "Maam-ri-tat", "phonetic": "maam-ri-TAAT", "stressed": True, "meaning": "Into radiant immortality"},
        ],
    },
    "gayatri_mantra": {
        "id": "gayatri_mantra",
        "title": "Gayatri Mantra (Celestial Illumination)",
        "tradition": "Universal Vedic",
        "language": "Sanskrit",
        "script": "Devanagari",
        "original_script": "ॐ भूर्भुवः स्वः तत्सवितुर्वरेण्यं भर्गो देवस्य धीमहि धियो यो नः प्रचोदयात्",
        "romanized": "Om Bhur Bhuvah Svah Tat Savitur Varenyam Bhargo Devasya Dhimahi Dhiyo Yo Nah Prachodayat",
        "phonetic_guide": "OM bhoor bhoo-VAH SVA-ha tat sa-vi-TOOR va-RAY-nyam BHAR-go day-VAS-ya DEE-ma-hee DHEE-yo yo NAH pra-CHO-da-yaat",
        "guide_key": "god",
        "english_meaning": "We meditate on the adorable, radiant glory of the Divine Sun of Consciousness; may that divine light illuminate our intellect and understanding.",
        "syllables": [
            {"text": "Om", "phonetic": "OM", "stressed": True, "meaning": "Supreme Reality"},
            {"text": "Bhur", "phonetic": "bhoor", "stressed": False, "meaning": "Physical Realm"},
            {"text": "Bhu-vah", "phonetic": "bhoo-VAH", "stressed": True, "meaning": "Astral Realm"},
            {"text": "Svah", "phonetic": "SVA-ha", "stressed": True, "meaning": "Celestial Realm"},
            {"text": "Tat-sa-vi-tur", "phonetic": "tat sa-vi-TOOR", "stressed": False, "meaning": "That Divine Source"},
            {"text": "Va-ren-yam", "phonetic": "va-RAY-nyam", "stressed": True, "meaning": "Most Adorable & Supreme"},
            {"text": "Bhar-go", "phonetic": "BHAR-go", "stressed": True, "meaning": "Spiritual Radiance"},
            {"text": "De-vas-ya", "phonetic": "day-VAS-ya", "stressed": False, "meaning": "Of the Divine Light"},
            {"text": "Dhi-ma-hi", "phonetic": "DEE-ma-hee", "stressed": True, "meaning": "We meditate upon"},
            {"text": "Dhi-yo-yo-nah", "phonetic": "DHEE-yo yo NAH", "stressed": False, "meaning": "Our thoughts and awakening"},
            {"text": "Pra-cho-da-yat", "phonetic": "pra-CHO-da-yaat", "stressed": True, "meaning": "May it enlighten and inspire"},
        ],
    },
    "hare_krishna": {
        "id": "hare_krishna",
        "title": "Maha Mantra (The Great Chant of Joy)",
        "tradition": "Hindu (Vaishnava)",
        "language": "Sanskrit",
        "script": "Devanagari",
        "original_script": "हरे कृष्ण हरे कृष्ण कृष्ण कृष्ण हरे हरे । हरे राम हरे राम राम राम हरे हरे ॥",
        "romanized": "Hare Krishna Hare Krishna Krishna Krishna Hare Hare, Hare Rama Hare Rama Rama Rama Hare Hare",
        "phonetic_guide": "ha-RAY KRISH-na ha-RAY KRISH-na KRISH-na KRISH-na ha-RAY ha-RAY, ha-RAY RAH-ma ha-RAY RAH-ma RAH-ma RAH-ma ha-RAY ha-RAY",
        "guide_key": "krishna",
        "english_meaning": "O Divine Energy (Hara) and All-Attractive Lord (Krishna, Rama), please engage my heart in Your pure loving service and joy.",
        "syllables": [
            {"text": "Ha-re", "phonetic": "ha-RAY", "stressed": True, "meaning": "Divine Maternal Grace"},
            {"text": "Krish-na", "phonetic": "KRISH-na", "stressed": True, "meaning": "All-Attractive Joy"},
            {"text": "Ha-re", "phonetic": "ha-RAY", "stressed": True, "meaning": "Divine Grace"},
            {"text": "Krish-na", "phonetic": "KRISH-na", "stressed": True, "meaning": "All-Attractive Joy"},
            {"text": "Krish-na", "phonetic": "KRISH-na", "stressed": True, "meaning": "All-Attractive Joy"},
            {"text": "Krish-na", "phonetic": "KRISH-na", "stressed": True, "meaning": "All-Attractive Joy"},
            {"text": "Ha-re", "phonetic": "ha-RAY", "stressed": True, "meaning": "Divine Grace"},
            {"text": "Ha-re", "phonetic": "ha-RAY", "stressed": True, "meaning": "Divine Grace"},
            {"text": "Ha-re", "phonetic": "ha-RAY", "stressed": True, "meaning": "Divine Grace"},
            {"text": "Ra-ma", "phonetic": "RAH-ma", "stressed": True, "meaning": "Source of Sublime Peace"},
            {"text": "Ha-re", "phonetic": "ha-RAY", "stressed": True, "meaning": "Divine Grace"},
            {"text": "Ra-ma", "phonetic": "RAH-ma", "stressed": True, "meaning": "Source of Sublime Peace"},
            {"text": "Ra-ma", "phonetic": "RAH-ma", "stressed": True, "meaning": "Source of Sublime Peace"},
            {"text": "Ra-ma", "phonetic": "RAH-ma", "stressed": True, "meaning": "Source of Sublime Peace"},
            {"text": "Ha-re", "phonetic": "ha-RAY", "stressed": True, "meaning": "Divine Grace"},
            {"text": "Ha-re", "phonetic": "ha-RAY", "stressed": True, "meaning": "Divine Grace"},
        ],
    },
    "shema_yisrael": {
        "id": "shema_yisrael",
        "title": "The Shema (Declaration of Divine Unity)",
        "tradition": "Judaism & Covenant",
        "language": "Hebrew",
        "script": "Hebrew",
        "original_script": "שְׁמַע יִשְׂרָאֵל יְהוָה אֱלֹהֵינוּ יְהוָה אֶחָד",
        "romanized": "Shema Yisrael Adonai Eloheinu Adonai Echad",
        "phonetic_guide": "she-MAH yis-ra-EL a-do-NAI e-lo-HAY-nu a-do-NAI e-CHAD",
        "guide_key": "moses",
        "english_meaning": "Hear, O Israel: The Lord is our God, the Lord is One.",
        "syllables": [
            {"text": "She-ma", "phonetic": "she-MAH", "stressed": True, "meaning": "Hear and Listen Deeply"},
            {"text": "Yis-ra-el", "phonetic": "yis-ra-EL", "stressed": True, "meaning": "People of the Covenant"},
            {"text": "A-do-nai", "phonetic": "a-do-NAI", "stressed": True, "meaning": "The Lord"},
            {"text": "E-lo-hei-nu", "phonetic": "e-lo-HAY-nu", "stressed": False, "meaning": "Our God"},
            {"text": "A-do-nai", "phonetic": "a-do-NAI", "stressed": True, "meaning": "The Lord"},
            {"text": "E-chad", "phonetic": "e-CHAD", "stressed": True, "meaning": "One, Indivisible, Absolute"},
        ],
    },
    "aaronic_blessing": {
        "id": "aaronic_blessing",
        "title": "The Priestly Blessing (Birkat Kohanim)",
        "tradition": "Judaism & Abrahamic Covenant",
        "language": "Hebrew",
        "script": "Hebrew",
        "original_script": "יְבָרֶכְךָ יְהוָה וְיִשְׁמְרֶךָ יָאֵר יְהוָה פָּנָיו אֵלֶיךָ וִיחֻנֶּךָּ יִשָּׂא יְהוָה פָּנָיו אֵלֶיךָ וְיָשֵׂם לְךָ שָׁלוֹם",
        "romanized": "Yevarechecha Adonai Veyishmerecha, Ya'er Adonai Panav Elecha Viychuneka, Yissa Adonai Panav Elecha Veyasem Lecha Shalom",
        "phonetic_guide": "ye-va-re-che-CHA a-do-NAI ve-yish-me-RE-cha, ya-AYR a-do-NAI pa-NAV e-LE-cha vee-choo-NE-ka, yis-SA a-do-NAI pa-NAV e-LE-cha ve-ya-SAYM le-CHA sha-LOM",
        "guide_key": "moses",
        "english_meaning": "May the Lord bless you and keep you; may the Lord make His face shine upon you and be gracious to you; may the Lord lift up His countenance upon you and give you peace.",
        "syllables": [
            {"text": "Ye-va-re-che-cha", "phonetic": "ye-va-re-che-CHA", "stressed": True, "meaning": "May the Lord bless you"},
            {"text": "Ve-yish-me-re-cha", "phonetic": "ve-yish-me-RE-cha", "stressed": False, "meaning": "And keep and protect you"},
            {"text": "Ya-er", "phonetic": "ya-AYR", "stressed": True, "meaning": "Make His face shine"},
            {"text": "Pa-nav-e-le-cha", "phonetic": "pa-NAV e-LE-cha", "stressed": False, "meaning": "His countenance toward you"},
            {"text": "Viy-chu-ne-ka", "phonetic": "vee-choo-NE-ka", "stressed": True, "meaning": "And be gracious to you"},
            {"text": "Yis-sa", "phonetic": "yis-SA", "stressed": True, "meaning": "May He lift up"},
            {"text": "Ve-ya-sem", "phonetic": "ve-ya-SAYM", "stressed": False, "meaning": "And grant unto you"},
            {"text": "Sha-lom", "phonetic": "sha-LOM", "stressed": True, "meaning": "Wholeness, Rest, and Peace"},
        ],
    },
    "lords_prayer_aramaic": {
        "id": "lords_prayer_aramaic",
        "title": "The Lord's Prayer (Original Galilean Aramaic)",
        "tradition": "Christianity (Galilean)",
        "language": "Aramaic",
        "script": "Syriac / Aramaic",
        "original_script": "ܐܒܘܢ ܕܒܫܡܝܐ ܢܬܩܕܫ ܫܡܟ ܬܐܬܐ ܡܠܟܘܬܟ",
        "romanized": "Abwun d'bwashmaya Nethqadash shmakh Teethee malkuthakh",
        "phonetic_guide": "ab-WOON d'bwash-MAH-ya neth-qa-DASH shmakh tee-THEE mal-koo-THAKH",
        "guide_key": "jesus",
        "english_meaning": "O Birther of the Cosmos, who shimmers in the celestial space, hallowed be Your Name. May Your sacred Kingdom unfold within us.",
        "syllables": [
            {"text": "Ab-wun", "phonetic": "ab-WOON", "stressed": True, "meaning": "Beloved Parent of the Universe"},
            {"text": "D'bwash-ma-ya", "phonetic": "d'bwash-MAH-ya", "stressed": True, "meaning": "Who resides in shimmering heavens"},
            {"text": "Neth-qa-dash", "phonetic": "neth-qa-DASH", "stressed": True, "meaning": "May Your Name be sanctified"},
            {"text": "Shmakh", "phonetic": "shmakh", "stressed": False, "meaning": "Your holy Name and essence"},
            {"text": "Tee-thee", "phonetic": "tee-THEE", "stressed": True, "meaning": "May it come and take root"},
            {"text": "Mal-ku-thakh", "phonetic": "mal-koo-THAKH", "stressed": True, "meaning": "Your loving Sovereign Reign"},
        ],
    },
    "kyrie_eleison": {
        "id": "kyrie_eleison",
        "title": "Kyrie Eleison (Chant of Mercy & Grace)",
        "tradition": "Christianity (Ancient Ecumenical)",
        "language": "Ancient Greek",
        "script": "Greek",
        "original_script": "Κύριε ἐλέησον, Χριστὲ ἐλέησον, Κύριε ἐλέησον",
        "romanized": "Kyrie Eleison, Christe Eleison, Kyrie Eleison",
        "phonetic_guide": "KEE-ree-ay eh-LAY-ee-sohn, KHRIS-tay eh-LAY-ee-sohn, KEE-ree-ay eh-LAY-ee-sohn",
        "guide_key": "jesus",
        "english_meaning": "Lord have mercy; Christ have mercy; Lord have mercy.",
        "syllables": [
            {"text": "Ky-ri-e", "phonetic": "KEE-ree-ay", "stressed": True, "meaning": "Lord and Shepherd"},
            {"text": "E-le-i-son", "phonetic": "eh-LAY-ee-sohn", "stressed": True, "meaning": "Have mercy and tender compassion"},
            {"text": "Chris-te", "phonetic": "KHRIS-tay", "stressed": True, "meaning": "Christ, Anointed Light"},
            {"text": "E-le-i-son", "phonetic": "eh-LAY-ee-sohn", "stressed": True, "meaning": "Have mercy and tender compassion"},
        ],
    },
    "al_fatiha": {
        "id": "al_fatiha",
        "title": "Surah Al-Fatiha (The Opening Blessing)",
        "tradition": "Islam (Universal Monotheism)",
        "language": "Arabic",
        "script": "Arabic",
        "original_script": "بِسْمِ اللَّهِ الرَّحْمَٰنِ الرَّحِيمِ الْحَمْدُ لِلَّهِ رَبِّ الْعَالَمِينَ",
        "romanized": "Bismillahi r-rahmani r-rahim, Alhamdu lillahi rabbi l-alamin",
        "phonetic_guide": "bis-mil-LAA-hir rah-MAA-nir ra-HEEM, al-HAM-du lil-LAA-hi rab-bil aa-la-MEEN",
        "guide_key": "god",
        "english_meaning": "In the name of God, the Entirely Merciful, the Especially Merciful. All praise is due to God, Lord of all the worlds.",
        "syllables": [
            {"text": "Bis-mil-lah", "phonetic": "bis-mil-LAAH", "stressed": True, "meaning": "In the Name of God"},
            {"text": "Ir-rah-man", "phonetic": "ir-rah-MAAN", "stressed": True, "meaning": "The Compassionate Source"},
            {"text": "Ir-ra-heem", "phonetic": "ir-ra-HEEM", "stressed": True, "meaning": "The Infinitely Merciful"},
            {"text": "Al-ham-du", "phonetic": "al-HAM-du", "stressed": True, "meaning": "All Praise and Gratitude"},
            {"text": "Lil-lah", "phonetic": "lil-LAAH", "stressed": False, "meaning": "Belongs to God alone"},
            {"text": "Rab-bil-a-la-meen", "phonetic": "rab-bil aa-la-MEEN", "stressed": True, "meaning": "Sustainer of all creation"},
        ],
    },
    "om_mani_padme_hum": {
        "id": "om_mani_padme_hum",
        "title": "Mantra of Compassion (Avalokiteshvara)",
        "tradition": "Buddhist Contemplative",
        "language": "Sanskrit / Tibetan",
        "script": "Tibetan / Devanagari",
        "original_script": "ཨོཾ་མ་ཎི་པ་དྨེ་ཧཱུྃ",
        "romanized": "Om Mani Padme Hum",
        "phonetic_guide": "OM ma-NEE pad-MAY HOOM",
        "guide_key": "mother",
        "english_meaning": "The jewel is in the lotus: transform impurity of body, speech, and mind into pure compassion and wisdom.",
        "syllables": [
            {"text": "Om", "phonetic": "OM", "stressed": True, "meaning": "Universal Sacred Body & Sound"},
            {"text": "Ma-ni", "phonetic": "ma-NEE", "stressed": True, "meaning": "The Jewel of Altruistic Intention"},
            {"text": "Pad-me", "phonetic": "pad-MAY", "stressed": True, "meaning": "The Pure Lotus of Wisdom"},
            {"text": "Hum", "phonetic": "HOOM", "stressed": True, "meaning": "Indivisible Unity of Wisdom & Compassion"},
        ],
    },
    "devi_suktam": {
        "id": "devi_suktam",
        "title": "Ya Devi Sarva-Bhuteshu (Divine Mother's Abode)",
        "tradition": "Hindu (Shakta / Devi)",
        "language": "Sanskrit",
        "script": "Devanagari",
        "original_script": "या देवी सर्वभूतेषु मातृरूपेण संस्थिता । नमस्तस्यै नमस्तस्यै नमस्तस्यै नमो नमः ॥",
        "romanized": "Ya Devi Sarva-Bhuteshu Matri-Rupena Samsthita, Namas-Tasyai Namas-Tasyai Namas-Tasyai Namo Namah",
        "phonetic_guide": "yaa DAY-vee sar-va BHOO-tay-shoo MAA-tree roo-PAY-na sum-sthi-TAA, na-mas TAS-yai na-mas TAS-yai na-mas TAS-yai na-MO na-MAH",
        "guide_key": "mother",
        "english_meaning": "To the Divine Mother who resides in all beings in the form of unconditional maternal love and shelter, reverent salutations again and again.",
        "syllables": [
            {"text": "Yaa-de-vi", "phonetic": "yaa DAY-vee", "stressed": True, "meaning": "To that Divine Mother"},
            {"text": "Sar-va-bhu-te-shu", "phonetic": "sar-va BHOO-tay-shoo", "stressed": False, "meaning": "Dwelling in all living beings"},
            {"text": "Ma-tri-ru-pe-na", "phonetic": "MAA-tree roo-PAY-na", "stressed": True, "meaning": "In the form of Motherly Care"},
            {"text": "Sam-sthi-ta", "phonetic": "sum-sthi-TAA", "stressed": False, "meaning": "Ever Established and Abiding"},
            {"text": "Na-mas-tas-yai", "phonetic": "na-mas TAS-yai", "stressed": True, "meaning": "Reverent Bow to Her"},
            {"text": "Na-mo-na-mah", "phonetic": "na-MO na-MAH", "stressed": True, "meaning": "Salutations again and again"},
        ],
    },
}


# ── SYLLABLE STRESS PARSER ──────────────────────────────────────────────────

_STRESS_PATTERNS = re.compile(r"[A-Z]{2,}|[ÁÉÍÓÚĀĪŪṬḌŚṢ]")


def parse_syllables_and_stress(raw_phonetic: str) -> list[dict[str, Any]]:
    """Parses a hyphenated or space-separated phonetic guide into syllable items.

    Upper-case segments (e.g. 'VAH' in 'shi-VAH-ya') are marked stressed.
    """
    if not raw_phonetic:
        return []

    tokens = [t for t in re.split(r"[\s,\.-]+", raw_phonetic) if t.strip()]
    results: list[dict[str, Any]] = []

    for idx, token in enumerate(tokens):
        stressed = bool(_STRESS_PATTERNS.search(token))
        # Keep clean text for display
        clean_text = token.capitalize() if not stressed else token
        results.append({
            "index": idx,
            "syllable": token,
            "display": clean_text,
            "stressed": stressed,
        })

    return results


# ── CATALOG QUERY HELPERS ───────────────────────────────────────────────────

def get_sacred_chant(chant_id: str | None) -> dict[str, Any] | None:
    """Lookup chant by exact id or fuzzy matching."""
    if not chant_id:
        return None
    key = chant_id.strip().lower()
    if key in SACRED_CHANTS:
        return SACRED_CHANTS[key]

    # Fuzzy match on title or key parts
    for k, v in SACRED_CHANTS.items():
        if key in k or key in v.get("title", "").lower():
            return v
    return None


def list_sacred_chants(tradition: str | None = None, guide_key: str | None = None) -> list[dict[str, Any]]:
    """List chants filtered by tradition or pantheon guide."""
    results = list(SACRED_CHANTS.values())
    if tradition:
        t_low = tradition.strip().lower()
        results = [c for c in results if t_low in c.get("tradition", "").lower()]
    if guide_key:
        g_low = guide_key.strip().lower()
        results = [c for c in results if c.get("guide_key", "").lower() == g_low]
    return results


# ── DATA CHANNEL DISPATCHER ─────────────────────────────────────────────────

def build_enunciation_payload(
    chant_id_or_key: str,
    active_syllable_idx: int = 0,
) -> dict[str, Any]:
    """Constructs a JSON-serializable LiveKit Room Data Channel payload.

    Broadcast on topic 'phonetics' so the client altar visualizer highlights
    the exact syllable in real-time as the voice model speaks.
    """
    chant = get_sacred_chant(chant_id_or_key)
    if not chant:
        # Fallback dynamic payload for arbitrary text
        parsed = parse_syllables_and_stress(chant_id_or_key)
        return {
            "type": "sanctuary_phonetics",
            "id": "custom",
            "title": "Sacred Prayer",
            "tradition": "Universal",
            "language": "Universal",
            "script": "Latin",
            "original_script": chant_id_or_key,
            "romanized": chant_id_or_key,
            "phonetic_guide": chant_id_or_key,
            "english_meaning": chant_id_or_key,
            "active_syllable_idx": active_syllable_idx,
            "syllables": parsed,
            "timestamp_ms": int(time.time() * 1000),
        }

    total_syllables = len(chant.get("syllables", []))
    idx = max(0, min(active_syllable_idx, total_syllables - 1)) if total_syllables > 0 else 0

    return {
        "type": "sanctuary_phonetics",
        "id": chant["id"],
        "title": chant["title"],
        "tradition": chant["tradition"],
        "language": chant["language"],
        "script": chant["script"],
        "original_script": chant["original_script"],
        "romanized": chant["romanized"],
        "phonetic_guide": chant["phonetic_guide"],
        "english_meaning": chant["english_meaning"],
        "guide_key": chant.get("guide_key", "atrium"),
        "active_syllable_idx": idx,
        "syllables": chant.get("syllables", []),
        "timestamp_ms": int(time.time() * 1000),
    }


async def broadcast_enunciation_packet(
    room: Any,
    payload: dict[str, Any],
    topic: str = "phonetics",
) -> bool:
    """Asynchronously dispatches enunciation payload to LiveKit Room Data Channel.

    Gracefully handles disconnected rooms, mock environments, or missing participants.
    """
    if not room:
        return False

    try:
        data_str = json.dumps(payload, ensure_ascii=False)
        payload_bytes = data_str.encode("utf-8")

        # LiveKit Room participant dispatch
        lp = getattr(room, "local_participant", None)
        if lp and hasattr(lp, "publish_data"):
            res = lp.publish_data(payload_bytes, topic=topic)
            if hasattr(res, "__await__"):
                await res
            return True

        # Room level dispatch fallback
        if hasattr(room, "publish_data"):
            res = room.publish_data(payload_bytes, topic=topic)
            if hasattr(res, "__await__"):
                await res
            return True

        return False
    except Exception as exc:
        print(f"[rt-phonetics] Data channel broadcast failed: {exc}", flush=True)
        return False
