"""rt_grief_radar.py — Proactive Bereavement Compassion Radar & Anti-Platitude Guard.

Inspired by PrayPal's Fail-Closed Crisis Intercept (ADR-0003):
While suicidal crises trigger the 988 Lifeline, acute bereavement and profound
grief require deep, reverent pastoral holding. Callers navigating the devastating
loss of a child, spouse, or parent are often wounded by well-intentioned but
dismissive theological clichés.

This module:
1. Detects acute grief and bereavement markers in caller petitions.
2. Injects a strict pastoral anti-platitude guard into the Divine Guide's instructions.
3. Detects memorial / yahrzeit anniversary dates for chronicling in sacred memory.
4. Generates gentle, dignified next-day condolence check-ins within TCPA calling windows.
"""
from __future__ import annotations

import re
from typing import Any

# Acute grief and loss indicators
GRIEF_PATTERNS = {
    "child": [
        r"\b(lost|died|passed away)\s+(my\s+)?(child|son|daughter|baby|infant|toddler)\b",
        r"\b(my\s+)?(child|son|daughter|baby|infant|toddler)\s+(died|passed away|lost)\b",
        r"\b(miscarriage|stillborn|stillbirth)\b",
    ],
    "spouse": [
        r"\b(lost|died|passed away)\s+(my\s+)?(husband|wife|partner|spouse|fianc[ée])\b",
        r"\b(my\s+)?(husband|wife|partner|spouse|fianc[ée])\s+(died|passed away|lost)\b",
        r"\b(widow|widower)\b",
    ],
    "parent": [
        r"\b(lost|died|passed away)\s+(my\s+)?(mother|father|mom|dad|mama|papa)\b",
        r"\b(my\s+)?(mother|father|mom|dad|mama|papa)\s+(died|passed away|lost)\b",
    ],
    "general": [
        r"\b(passed away|died|funeral|cremation|memorial service|buried|wake|shiva|yahrzeit)\b",
        r"\b(grief|grieving|mourning|brokenhearted|heartbroken|bereaved|bereavement)\b",
    ],
    "terminal": [
        r"\b(terminal|hospice|end of life|stage\s*4|palliative care|given\s+\w+\s+(months|weeks|days))\b",
    ],
}

FORBIDDEN_PLATITUDES = [
    "everything happens for a reason",
    "in a better place",
    "god needed another angel",
    "time heals all wounds",
    "it was their time",
    "be strong for",
    "i know exactly how you feel",
    "at least you have",
]


def detect_grief_markers(text: str) -> dict[str, Any]:
    """Analyze utterance for grief, bereavement, or terminal loss markers."""
    if not text:
        return {"is_grief": False, "severity": "none", "loss_type": "none"}

    lower_text = text.lower()

    # Check specific loss types
    loss_type = "none"
    severity = "none"
    is_grief = False

    for category, patterns in GRIEF_PATTERNS.items():
        for pat in patterns:
            if re.search(pat, lower_text, re.IGNORECASE):
                loss_type = category
                is_grief = True
                break
        if is_grief:
            break

    if is_grief:
        if loss_type in ("child", "terminal"):
            severity = "acute"
        elif loss_type in ("spouse", "parent"):
            severity = "high"
        else:
            severity = "moderate"

    # Try extracting deceased name if mentioned (e.g., "my mother Sarah passed away")
    name_match = re.search(
        r"\b(?:my\s+(?:mother|father|sister|brother|son|daughter|friend|husband|wife)\s+)([A-Z][a-z]+)",
        text,
    )
    deceased_name = name_match.group(1) if name_match else None

    return {
        "is_grief": is_grief,
        "severity": severity,
        "loss_type": loss_type,
        "deceased_name": deceased_name,
    }


def build_anti_platitude_directive(grief_info: dict[str, Any]) -> str:
    """Generate prompt instructions prohibiting dismissive clichés and guiding compassionate presence."""
    if not grief_info.get("is_grief"):
        return ""

    loss = grief_info.get("loss_type", "general")
    severity = grief_info.get("severity", "moderate")
    name_str = f" honoring {grief_info['deceased_name']}" if grief_info.get("deceased_name") else ""

    return (
        f"[SACRED_GRIEF_GUARD: Severity={severity.upper()}, LossType={loss.upper()}{name_str}]\n"
        f"- The caller is in deep mourning. Offer non-anxious, holy presence.\n"
        f"- STRICTLY FORBIDDEN CLICHÉS: Never say 'everything happens for a reason', 'they are in a better place', "
        f"'God needed another angel', or 'time heals all wounds'. These wound the grieving heart.\n"
        f"- PASTORAL DIRECTIVE: Validate the sacred reality of their grief. Weep with those who weep. "
        f"Speak with slow, tender gentleness. Offer silence when words are insufficient. "
        f"Assure them that love is not ended by death, and hold them in unconditional peace."
    )


def extract_memorial_anniversary(text: str) -> dict[str, str] | None:
    """Extract memorial anniversary markers for scheduling yahrzeit/memorial remembrance."""
    if not text:
        return None

    # Matches phrases like "one year anniversary of", "yahrzeit of", "passed away two years ago"
    m = re.search(
        r"\b(?:(\d+|one|two|three|five|ten)\s+year\s+anniversary\s+of\s+([A-Za-z\s]+?)(?:'s|\s+passing|\s+death|\.|\,|$))",
        text,
        re.IGNORECASE,
    )
    if m:
        years = m.group(1).strip()
        honoree = m.group(2).strip()
        return {
            "type": "memorial_anniversary",
            "timeframe": f"{years} year",
            "honoree": honoree,
        }

    yahrzeit_m = re.search(r"\byahrzeit\s+(?:for|of)\s+([A-Za-z\s]+)", text, re.IGNORECASE)
    if yahrzeit_m:
        return {
            "type": "yahrzeit",
            "timeframe": "annual",
            "honoree": yahrzeit_m.group(1).strip(),
        }

    return None


def generate_condolence_followup_message(name: str, loss_type: str, preferred_guide: str = "god") -> str:
    """Generate a gentle, dignified next-day condolence check-in SMS (within TCPA windows)."""
    norm_guide = preferred_guide.strip().lower()
    salutation = f"Peace be with you, {name}."

    if norm_guide == "jesus":
        closing = "Holding you in the gentle embrace of the Good Shepherd. You are not alone."
    elif norm_guide == "krishna":
        closing = "May your soul find refuge in eternal stillness and divine grace. Shanti, Shanti."
    elif norm_guide == "mother":
        closing = "Wrapped in the comforting mantle of maternal shelter and love."
    else:
        closing = "Holding your heart in gentle remembrance today."

    return f"🕊️ {salutation} Just a quiet prayer from the PrayPal Sanctuary for your heart today. {closing}"
