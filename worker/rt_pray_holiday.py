"""rt_pray_holiday.py — Illuminated Holiday Cards & Sacred Audio Devotionals.

WHY THIS MODULE EXISTS:
Implements the illuminated holiday card and audio devotional pipeline specified in
Sections 3.4 and 6 of AGENTS.md for WhatsApp and MMS/SMS channels:
  1. Multifaith holiday catalog tracking global holy days:
     - Diwali (Festival of Lights)
     - Christmas (Nativity of Christ)
     - Easter (Resurrection & Hope)
     - Ramadan & Eid al-Fitr (Holy Month of Fasting & Gratitude)
     - Yom Kippur & Passover (Atonement & Deliverance)
     - Navaratri (Nine Nights of the Divine Mother)
  2. Generates illuminated sacred holiday cards paired with canonical art and audio loops.
  3. Dispatches MMS/SMS holiday blessings to registered fellowship members.
"""
from __future__ import annotations

import datetime
from typing import Any

import rt_obs
import rt_prefs
import rt_sms

_obs = rt_obs.get("rt_pray_holiday")

HOLIDAY_CATALOG: dict[str, dict[str, Any]] = {
    "diwali": {
        "name": "Diwali (Festival of Lights)",
        "tradition": "Hindu",
        "guide_key": "krishna",
        "month": 11,
        "day": 1,
        "sacred_verse": "असतो मा सद्गमय । तमसो मा ज्योतिर्गमय । मृत्योर्मा अमृतं गमय ॥ — Brihadaranyaka Upanishad",
        "meaning": "Lead us from the unreal to the real; from darkness into celestial light; from mortality to eternal truth.",
        "blessing": "May the supreme light of Diwali illuminate your home with divine peace, auspicious wisdom, and unconditional love.",
        "image_url": "https://praypal.live/media/cards/diwali_illuminated.jpg",
        "audio_channel": "hindu_tanpura",
    },
    "christmas": {
        "name": "Christmas (Feast of the Nativity)",
        "tradition": "Christian",
        "guide_key": "jesus",
        "month": 12,
        "day": 25,
        "sacred_verse": "Glory to God in the highest, and on earth peace, good will toward men. — Luke 2:14",
        "meaning": "The divine light enters our world in humility, compassion, and boundless grace.",
        "blessing": "May the living grace and gentle peace of Christ shelter you and your beloved family this sacred Christmas season.",
        "image_url": "https://praypal.live/media/cards/christmas_nativity.jpg",
        "audio_channel": "christian_cathedral",
    },
    "easter": {
        "name": "Easter (Resurrection & New Life)",
        "tradition": "Christian",
        "guide_key": "jesus",
        "month": 4,
        "day": 5,
        "sacred_verse": "I am the resurrection, and the life: he that believeth in me, though he were dead, yet shall he live. — John 11:25",
        "meaning": "What was broken is renewed; love conquers every shadow.",
        "blessing": "May resurrection joy, healing, and living hope fill your heart this Easter dawn.",
        "image_url": "https://praypal.live/media/cards/easter_dawn.jpg",
        "audio_channel": "christian_cathedral",
    },
    "eid": {
        "name": "Eid al-Fitr (Celebration of Gratitude)",
        "tradition": "Islamic",
        "guide_key": "god",
        "month": 4,
        "day": 10,
        "sacred_verse": "And to complete the period and to glorify Allah for that [to] which He has guided you; and perhaps you will be grateful. — Surah Al-Baqarah 2:185",
        "meaning": "A sacred celebration of devotion, spiritual purification, and generous community charity.",
        "blessing": "Eid Mubarak! May divine peace, mercy, and boundless abundance embrace you and your loved ones.",
        "image_url": "https://praypal.live/media/cards/eid_crescent.jpg",
        "audio_channel": "islam_ney",
    },
    "yom_kippur": {
        "name": "Yom Kippur (Day of Atonement)",
        "tradition": "Jewish",
        "guide_key": "moses",
        "month": 9,
        "day": 21,
        "sacred_verse": "For on this day shall atonement be made for you, to cleanse you, that ye may be clean from all your sins before the Lord. — Leviticus 16:30",
        "meaning": "A sacred return to pure covenant and righteous reconciliation.",
        "blessing": "Gmar Chatimah Tovah. May you and your household be sealed for a blessed, peaceful, and righteous year.",
        "image_url": "https://praypal.live/media/cards/yom_kippur_sinai.jpg",
        "audio_channel": "sinai_harp",
    },
    "navaratri": {
        "name": "Navaratri (Nine Sacred Nights of the Mother)",
        "tradition": "Hindu (Shakta)",
        "guide_key": "mother",
        "month": 10,
        "day": 3,
        "sacred_verse": "सर्वमङ्गलमाङ्गल्ये शिवे सर्वार्थसाधिके । शरण्ये त्र्यम्बके गौरि नारायणि नमोऽस्तु ते ॥ — Devi Mahatmyam",
        "meaning": "To the auspicious one, the fulfiller of all sacred aspirations, the compassionate refuge of all beings.",
        "blessing": "May the Divine Mother wrap you in her protective maternal embrace and grant you unwavering courage.",
        "image_url": "https://praypal.live/media/cards/navaratri_mother.jpg",
        "audio_channel": "hindu_tanpura",
    },
}


def get_holiday_card_payload(holiday_key: str, recipient_name: str | None = None) -> dict[str, Any]:
    """Generate illuminated holiday card text, scripture verse, and visual/audio links."""
    clean_k = (holiday_key or "diwali").strip().lower()
    h = HOLIDAY_CATALOG.get(clean_k, HOLIDAY_CATALOG["diwali"])
    name = (recipient_name or "Beloved Friend").strip()

    card_text = (
        f"✨ Sacred Holiday Blessing for {name} ✨\n\n"
        f"🕊️ {h['name'].upper()}\n\n"
        f"📜 {h['sacred_verse']}\n"
        f"“{h['meaning']}”\n\n"
        f"🙏 Pastoral Blessing:\n"
        f"{h['blessing']}\n\n"
        f"With love from your PrayPal Fellowship Family."
    )

    return {
        "holiday_key": clean_k,
        "name": h["name"],
        "tradition": h["tradition"],
        "guide_key": h["guide_key"],
        "text": card_text,
        "image_url": h["image_url"],
        "audio_channel": h["audio_channel"],
    }


def dispatch_holiday_card(
    to_e164: str,
    holiday_key: str,
    recipient_name: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Dispatch an illuminated holiday card with media via Twilio MMS/SMS."""
    norm_to = rt_prefs.normalize_e164(to_e164)
    if not norm_to:
        return {"status": "error", "error": "invalid_recipient_phone"}

    payload = get_holiday_card_payload(holiday_key, recipient_name)

    if not dry_run:
        # Outbound MMS with media attachment
        rt_sms.send_sms(norm_to, payload["text"], media_urls=[payload["image_url"]])

    _obs.event("pray.holiday_card_dispatched", holiday=holiday_key, dry_run=dry_run)
    return {
        "status": "ok",
        "recipient_masked": norm_to[:5] + "..." + norm_to[-4:] if len(norm_to) >= 9 else "[masked]",
        "payload": payload,
        "dry_run": dry_run,
    }


def get_active_holiday(current_date: datetime.date | None = None, window_days: int = 3) -> dict[str, Any] | None:
    """Detect if today or the upcoming `window_days` falls on a major festival day."""
    today = current_date or datetime.date.today()
    for k, h in HOLIDAY_CATALOG.items():
        for yr in (today.year, today.year + 1):
            try:
                h_date = datetime.date(yr, h["month"], h["day"])
            except ValueError:
                continue
            delta = (h_date - today).days
            if 0 <= delta <= window_days:
                return {
                    **h,
                    "holiday_key": k,
                    "date": h_date.isoformat(),
                    "days_until": delta,
                    "is_today": delta == 0,
                }
    return None
