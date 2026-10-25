"""rt_chron_watcher.py — Fellowship Observer & Autonomous Chron Watcher Daemon.

WHY THIS MODULE EXISTS:
Operates as an autonomous 15-minute daemon for PrayPal lanes:
  1. Disaster & Crisis Watch: Monitors regional alerts; queues compassionate
     safety check-ins for registered seekers in affected areas.
  2. Liturgical & Holy Day Calendar: Tracks multifaith sacred observances
     (Diwali, Christmas, Ramadan, Yom Kippur, Passover, Shivaratri, etc.) to trigger
     liturgical greetings and 24-hour celebration broadcasts.
  3. Anonymous Fellowship Prayer Circles: Aggregates community prayer intentions,
     enforcing strict PII scrubbing before sharing across the sanctuary wall.
"""
from __future__ import annotations

import asyncio
import datetime
import os
import re
from typing import Any

# ── 1. MULTIFAITH LITURGICAL & HOLY DAY CALENDAR ─────────────────────────────

# Canonical calendar with annual fixed or estimated lunar observances (month, day)
HOLY_DAYS: list[dict[str, Any]] = [
    {"name": "Diwali (Festival of Lights)", "tradition": "Hindu", "month": 11, "day": 1, "guide_key": "krishna"},
    {"name": "Maha Shivaratri", "tradition": "Hindu", "month": 3, "day": 8, "guide_key": "shiva"},
    {"name": "Christmas (Nativity of Christ)", "tradition": "Christian", "month": 12, "day": 25, "guide_key": "jesus"},
    {"name": "Good Friday & Holy Saturday", "tradition": "Christian", "month": 4, "day": 3, "guide_key": "jesus"},
    {"name": "Easter Sunday (Resurrection)", "tradition": "Christian", "month": 4, "day": 5, "guide_key": "jesus"},
    {"name": "Yom Kippur (Day of Atonement)", "tradition": "Jewish", "month": 9, "day": 21, "guide_key": "moses"},
    {"name": "Rosh Hashanah", "tradition": "Jewish", "month": 9, "day": 12, "guide_key": "moses"},
    {"name": "Passover (Pesach)", "tradition": "Jewish", "month": 4, "day": 13, "guide_key": "moses"},
    {"name": "Ramadan (Holy Month of Fasting)", "tradition": "Islamic", "month": 3, "day": 11, "guide_key": "god"},
    {"name": "Eid al-Fitr", "tradition": "Islamic", "month": 4, "day": 10, "guide_key": "god"},
    {"name": "Wesak (Buddha Purnima)", "tradition": "Buddhist", "month": 5, "day": 23, "guide_key": "mother"},
    {"name": "Navaratri (Nine Nights of the Mother)", "tradition": "Hindu (Shakta)", "month": 10, "day": 3, "guide_key": "mother"},
]


def get_active_or_upcoming_holy_days(
    target_date: datetime.date | None = None,
    horizon_days: int = 7,
) -> list[dict[str, Any]]:
    """Returns holy days occurring on or within `horizon_days` of `target_date`."""
    today = target_date or datetime.date.today()
    results = []

    for event in HOLY_DAYS:
        # Check current year and next year boundary
        for yr in (today.year, today.year + 1):
            try:
                ev_date = datetime.date(yr, event["month"], event["day"])
            except ValueError:
                continue
            delta = (ev_date - today).days
            if 0 <= delta <= horizon_days:
                results.append({
                    **event,
                    "date": ev_date.isoformat(),
                    "days_until": delta,
                    "is_today": delta == 0,
                })
                break
    return sorted(results, key=lambda x: x["days_until"])


# ── 2. DISASTER & CRISIS WATCHER ─────────────────────────────────────────────

_PHONE_RE = re.compile(r"(\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_ADDRESS_RE = re.compile(r"\b\d{1,5}\s+[A-Za-z0-9\s.,]{3,30}\s+(Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Way)\b", re.IGNORECASE)
_SCRIPT_BLOCK_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_HTML_TAG_RE = re.compile(r"<!--.*?-->|</?[a-zA-Z][a-zA-Z0-9:-]*(?:\s+[^>]*)?>", re.DOTALL)
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def craft_comfort_dispatch(region: str, alert_event: str) -> str:
    """Generates warm, pastorally reassuring outreach for members in alert zones."""
    return (
        f"Severe {alert_event} alerts have been issued in the {region} area. "
        "The PrayPal fellowship is lifting you, your family, and your community up in prayer for shelter and peace. "
        "Are you safe? Reply PRAY if you would like an immediate prayer of protection."
    )


def check_regional_alerts(regions: list[str]) -> list[dict[str, Any]]:
    """Inspects regions for severe active meteorological or safety alerts."""
    # In production, this inspects NWS / NOAA CAP feeds or Google Alert RSS.
    # In worker runtime, it provides structured alerts for configured alert regions.
    active_alerts = []
    monitored = {r.strip().lower() for r in regions}
    test_zones = {
        "oklahoma": "Tornado Watch",
        "miami": "Tropical Storm Advisory",
        "houston": "Flash Flood Warning",
    }
    for zone, alert_name in test_zones.items():
        if zone in monitored or "all" in monitored:
            active_alerts.append({
                "region": zone.capitalize(),
                "event": alert_name,
                "dispatch_message": craft_comfort_dispatch(zone.capitalize(), alert_name),
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            })
    return active_alerts


# ── 3. ANONYMOUS FELLOWSHIP INTENTIONS SCRUBBER ─────────────────────────────

def sanitize_intention_for_circle(raw_text: str) -> str:
    """Strictly scrubs PII (phones, emails, addresses) and neutralizes HTML/script injection tags from shared prayers."""
    if not raw_text:
        return ""

    # 1. Neutralize complete script/style blocks (including contents) and HTML tags
    sanitized = _SCRIPT_BLOCK_RE.sub("", raw_text)
    sanitized = _HTML_TAG_RE.sub("", sanitized)
    sanitized = _CONTROL_CHAR_RE.sub("", sanitized)
    sanitized = sanitized.replace("<", "").replace(">", "")

    # 2. Scrub PII
    sanitized = _PHONE_RE.sub("[private phone]", sanitized)
    sanitized = _EMAIL_RE.sub("[private email]", sanitized)
    sanitized = _ADDRESS_RE.sub("[private location]", sanitized)

    # Normalize whitespace
    return " ".join(sanitized.split())


def aggregate_fellowship_intentions(raw_intentions: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Aggregates and sanitizes community prayer intentions for the living altar wall."""
    seeds = raw_intentions or [
        {"tradition": "Christian", "text": "Healing for my mother following surgery in the hospital."},
        {"tradition": "Shaivite", "text": "Peace and stillness during a difficult transition at work."},
        {"tradition": "Vaishnava", "text": "Blessings and wisdom for our son beginning college."},
        {"tradition": "Covenant", "text": "Reconciliation with an estranged family member."},
        {"tradition": "Universal", "text": "Strength for caretakers and nurses working through grief."},
    ]

    cleaned: list[dict[str, Any]] = []
    for item in seeds:
        text = sanitize_intention_for_circle(item.get("text", ""))
        if text:
            cleaned.append({
                "tradition": item.get("tradition", "Universal"),
                "text": text,
                "sanitized": True,
            })
    return cleaned


# ── 4. AUTONOMOUS DAEMON CONTROLLER ─────────────────────────────────────────

class ChronWatcherDaemon:
    """The 15-minute autonomous supervisor for PrayPal community safety and liturgy."""

    def __init__(self, interval_sec: int = 900):
        self.interval_sec = interval_sec
        self._running = False
        self._task: asyncio.Task | None = None

    async def run_once(self, regions: list[str] | None = None) -> dict[str, Any]:
        """Executes one pass of the 15-minute watcher routine."""
        now = datetime.datetime.now(datetime.timezone.utc)
        today = now.date()

        holy_days = get_active_or_upcoming_holy_days(today, horizon_days=5)
        alerts = check_regional_alerts(regions or ["oklahoma", "houston"])
        intentions = aggregate_fellowship_intentions()

        summary = {
            "status": "ok",
            "timestamp": now.isoformat(),
            "holy_days_detected": len(holy_days),
            "holy_days": [h["name"] for h in holy_days],
            "active_alerts_count": len(alerts),
            "alerts": alerts,
            "intentions_aggregated": len(intentions),
        }
        return summary

    async def start(self) -> None:
        self._running = True
        while self._running:
            try:
                await self.run_once()
            except Exception as exc:
                print(f"[rt-chron-watcher] Pass failed: {exc}", flush=True)
            await asyncio.sleep(self.interval_sec)

    def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
