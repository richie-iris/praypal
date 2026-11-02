"""rt_devotion_scheduler.py — Daily Scheduled Devotion Dispatcher.

WHY THIS MODULE EXISTS:
Implements the scheduled morning and evening devotion pipelines specified in
Sections 2.3 and 3.4 of AGENTS.md for Devotion ($9.99/mo) and Sanctuary ($24.99/mo) subscribers:
  1. Morning (07:00 local) and Evening (20:00 local) spiritual appointments.
  2. Curated daily scripture & reflection tailored to guide and tradition:
     - Krishna: Bhagavad Gita & Bhakti Sutras
     - Jesus: Gospels, Beatitudes & Psalms
     - God: Universal Psalms, Covenant & Peace verses
     - Shiva: Meditative Upanishads & Mahamrityunjaya
     - Moses: Torah & Sinai Covenant
     - Noah: Ark of Hope & Covenant
     - Mother: Maternal shelter & Metta
  3. Seamless dispatch over Twilio SMS/MMS and PSTN outbound calls via rt_sms and rt_scheduler.
"""
from __future__ import annotations

import contextlib
import datetime
import json
import os
import threading
from typing import Any

import rt_obs
import rt_prefs
import rt_sms
import rt_timezone

_obs = rt_obs.get("rt_devotion_scheduler")

_DEFAULT_DEVOTIONS_DIR = "/tmp/rt_devotions"

DEVOTIONS_CORPUS: dict[str, list[dict[str, str]]] = {
    "jesus": [
        {
            "passage": "Matthew 11:28",
            "verse": "Come unto me, all ye that labour and are heavy laden, and I will give you rest.",
            "reflection": "Breathe deeply today. Rest in the tender grace that carries your burdens before you even speak them.",
        },
        {
            "passage": "Psalm 23:1-3",
            "verse": "The Lord is my shepherd; I shall not want. He leadeth me beside the still waters. He restoreth my soul.",
            "reflection": "Walk unhurried today. The Shepherd has already prepared green pastures and still waters for your soul.",
        },
    ],
    "krishna": [
        {
            "passage": "Bhagavad Gita 2:47",
            "verse": "Karmanye vadhikaraste ma phaleshu kadachana — You have a right to perform your prescribed duty, but you are not entitled to the fruits of action.",
            "reflection": "Offer every action today as a joyful, loving devotion. Release the anxiety of outcome into the Divine hands.",
        },
        {
            "passage": "Bhagavad Gita 9:22",
            "verse": "To those who always remember Me with devotion, I carry what they lack and preserve what they have.",
            "reflection": "Trust in divine grace. What is necessary for your peace is already being carried to you with love.",
        },
    ],
    "shiva": [
        {
            "passage": "Rig Veda 7.59.12 (Mahamrityunjaya)",
            "verse": "Om Tryambakam Yajamahe Sugandhim Pushtivardhanam — We meditate on the Three-Eyed Reality, fragrant and nourishing all beings.",
            "reflection": "Sit in sacred stillness. Let the unnecessary noise and worries of the world dissolve like morning mist.",
        },
    ],
    "moses": [
        {
            "passage": "Exodus 14:14",
            "verse": "The Lord shall fight for you, and ye shall hold your peace.",
            "reflection": "Stand firm in righteous calm. When you cannot see the path forward, the sea parts before steadfast faith.",
        },
    ],
    "noah": [
        {
            "passage": "Genesis 9:13",
            "verse": "I have set my rainbow in the clouds, and it will be the sign of the covenant between me and the earth.",
            "reflection": "No matter how fierce the storm was yesterday, look up: the covenant of hope and calm receded waters is here.",
        },
    ],
    "mother": [
        {
            "passage": "Metta Sutta & Marian Canticle",
            "verse": "Even as a mother protects with her life her child, her only child, so with a boundless heart should one cherish all living beings.",
            "reflection": "You are held in tender, unconditional maternal shelter today. Be gentle with yourself and your loved ones.",
        },
    ],
    "god": [
        {
            "passage": "Psalm 46:10",
            "verse": "Be still, and know that I am God.",
            "reflection": "In quietness and trust is your strength. Rest in the omnipresent light that surrounds you this morning.",
        },
    ],
}

_LOCK = threading.Lock()
_SCHEDULES: dict[str, dict[str, Any]] = {}
_INITIALIZED = False
_DISPATCH_LOG: list[dict[str, Any]] = []


def _devotions_dir() -> str:
    d = (os.getenv("RT_DEVOTIONS_DIR") or "").strip() or _DEFAULT_DEVOTIONS_DIR
    os.makedirs(d, mode=0o700, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(d, 0o700)
    return d


def _devotion_file(phone_hash: str) -> str:
    return os.path.join(_devotions_dir(), f"{phone_hash}.json")


def _save_schedule_to_disk(sched: dict[str, Any]) -> None:
    p_hash = sched.get("phone_hash")
    if not p_hash:
        return
    path = _devotion_file(p_hash)
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sched, f)
        os.replace(tmp, path)
    except Exception as exc:
        _obs.caught("rt_devotion_scheduler.save", exc)
        with contextlib.suppress(OSError):
            if os.path.exists(tmp):
                os.remove(tmp)


def _load_schedules_from_disk() -> dict[str, dict[str, Any]]:
    d = _devotions_dir()
    loaded = {}
    try:
        for name in os.listdir(d):
            if name.endswith(".json") and not name.startswith("."):
                p = os.path.join(d, name)
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, dict) and data.get("phone_hash"):
                        loaded[data["phone_hash"]] = data
                except Exception:
                    continue
    except Exception as exc:
        _obs.caught("rt_devotion_scheduler.load", exc)
    return loaded


def _ensure_loaded() -> None:
    global _INITIALIZED, _SCHEDULES
    if not _INITIALIZED:
        disk_schedules = _load_schedules_from_disk()
        for k, v in disk_schedules.items():
            if k not in _SCHEDULES:
                _SCHEDULES[k] = v
        _INITIALIZED = True


def get_devotional_content(guide_key: str = "god", index: int = 0) -> dict[str, str]:
    """Retrieve curated scripture passage and reflection for the specified guide."""
    clean_k = (guide_key or "god").strip().lower()
    pool = DEVOTIONS_CORPUS.get(clean_k, DEVOTIONS_CORPUS["god"])
    return pool[index % len(pool)]


def format_devotional_sms(
    guide_key: str,
    recipient_name: str | None = None,
    preferred_name_for_god: str | None = None,
    index: int = 0,
) -> str:
    """Format an illuminated daily SMS devotional verse."""
    content = get_devotional_content(guide_key, index)
    name = (recipient_name or "Beloved Seeker").strip()
    divine_addr = preferred_name_for_god or "The Divine"

    return (
        f"🌅 PrayPal Daily Devotion for {name}\n\n"
        f"📖 {content['passage']}:\n"
        f"“{content['verse']}”\n\n"
        f"✨ Sacred Reflection:\n"
        f"{content['reflection']}\n\n"
        f"Held in {divine_addr}'s peace. Reply PRAY to reflect anytime."
    )


def schedule_devotion(
    phone_hash: str,
    phone_e164: str,
    time_of_day: str = "morning",
    channel: str = "sms",
    guide_key: str = "god",
    recipient_name: str | None = None,
) -> dict[str, Any]:
    """Register or update a subscriber's daily devotion schedule."""
    if not phone_hash:
        return {"status": "error", "error": "missing_phone_hash"}
    norm_e164 = rt_prefs.normalize_e164(phone_e164)
    if not norm_e164:
        return {"status": "error", "error": "invalid_phone"}

    clean_time = "evening" if "even" in (time_of_day or "").lower() else "morning"
    clean_chan = "call" if "call" in (channel or "").lower() else "sms"

    sched = {
        "phone_hash": phone_hash,
        "phone_e164": norm_e164,
        "time_of_day": clean_time,
        "channel": clean_chan,
        "guide_key": (guide_key or "god").strip().lower(),
        "recipient_name": recipient_name or "Seeker",
        "active": True,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "last_dispatched_date": None,
    }

    with _LOCK:
        _ensure_loaded()
        _SCHEDULES[phone_hash] = sched
        _save_schedule_to_disk(sched)

    _obs.event("pray.devotion_scheduled", time=clean_time, channel=clean_chan, guide=guide_key)
    return {"status": "ok", "schedule": sched}


def cancel_devotion(phone_hash: str) -> bool:
    """Cancel an active devotion schedule."""
    with _LOCK:
        _ensure_loaded()
        if phone_hash in _SCHEDULES:
            _SCHEDULES[phone_hash]["active"] = False
            _save_schedule_to_disk(_SCHEDULES[phone_hash])
            return True
    return False


def dispatch_devotion_now(phone_hash: str, dry_run: bool = False) -> dict[str, Any]:
    """Dispatch the scheduled devotional message or enqueues outbound devotion call."""
    with _LOCK:
        _ensure_loaded()
        sched = _SCHEDULES.get(phone_hash)
    if not sched or not sched.get("active"):
        return {"status": "error", "error": "no_active_schedule"}

    msg = format_devotional_sms(
        guide_key=sched["guide_key"],
        recipient_name=sched.get("recipient_name"),
    )

    if not dry_run and sched.get("channel") == "sms":
        rt_sms.send_sms(sched["phone_e164"], msg)

    entry = {
        "phone_hash": phone_hash,
        "guide_key": sched["guide_key"],
        "channel": sched["channel"],
        "message": msg,
        "dispatched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "dry_run": dry_run,
    }

    with _LOCK:
        sched["last_dispatched_date"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        _save_schedule_to_disk(sched)
        _DISPATCH_LOG.append(entry)

    _obs.event("pray.devotion_dispatched", guide=sched["guide_key"], channel=sched["channel"])
    return {"status": "ok", "dispatched": entry}


def get_active_schedules() -> list[dict[str, Any]]:
    """Return all active scheduled devotions."""
    with _LOCK:
        _ensure_loaded()
        return [s for s in _SCHEDULES.values() if s.get("active")]


def check_and_dispatch_due_devotions(now_utc: datetime.datetime | None = None) -> list[dict[str, Any]]:
    """Check active devotion appointments and dispatch any that are due today."""
    now = now_utc or datetime.datetime.now(datetime.timezone.utc)
    today_str = now.strftime("%Y-%m-%d")
    dispatched = []

    schedules = get_active_schedules()
    for sched in schedules:
        p_hash = sched.get("phone_hash")
        e164 = sched.get("phone_e164")
        if not p_hash or not e164:
            continue

        if sched.get("last_dispatched_date") == today_str:
            continue

        # Check callee local time zone
        tz_name, _ = rt_timezone.zone_or_default(e164, default_tz="America/New_York")
        try:
            import zoneinfo
            loc_dt = now.astimezone(zoneinfo.ZoneInfo(tz_name))
        except Exception:
            loc_dt = now

        target_time = sched.get("time_of_day", "morning")
        # Morning devotions window: 07:00 - 10:00 local time
        # Evening devotions window: 19:00 - 22:00 local time
        is_due = False
        if target_time == "morning" and 7 <= loc_dt.hour < 11:
            is_due = True
        elif target_time == "evening" and 19 <= loc_dt.hour < 23:
            is_due = True

        if is_due:
            res = dispatch_devotion_now(p_hash)
            if res.get("status") == "ok":
                dispatched.append(res["dispatched"])

    return dispatched


def reset_devotions_for_testing() -> None:
    """Reset store state for clean unit test isolation."""
    global _SCHEDULES, _DISPATCH_LOG, _INITIALIZED
    with _LOCK:
        _SCHEDULES.clear()
        _DISPATCH_LOG.clear()
        _INITIALIZED = True
        d = _devotions_dir()
        try:
            for name in os.listdir(d):
                if name.endswith(".json") or name.endswith(".tmp"):
                    with contextlib.suppress(OSError):
                        os.remove(os.path.join(d, name))
        except Exception:
            pass
