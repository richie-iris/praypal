"""rt_scheduler.py — Future-dated job scheduler for Phone-Pal.

Stores and executes jobs that need to happen in the future:
  · outbound_call — call the user back with a message
  · research      — run rt_executor search + synthesis, store result
  · send_sms      — fire an SMS at a future time
  · send_email    — fire an email at a future time
  · reminder      — insert a reminder that becomes visible at a future date

Jobs live in rt.scheduled_jobs (Supabase) and are polled by a runner.
The postcall action planner creates them; the runner executes them.

Run standalone:  python rt_scheduler.py          (polls every 60s)
Run one pass:    python rt_scheduler.py --once   (execute pending, exit)
"""
from __future__ import annotations

import rt_obs

import contextlib
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv(".env.local")
load_dotenv(".env")

import rt_bridge
import rt_prefs
import rt_timezone

JOBS_CATEGORY = "scheduled_jobs"

MAX_JOBS_PER_CALL = 5
MAX_DAYS_OUT = 90
MAX_ATTEMPTS = 3
# Per-caller daily caps. MAX_JOBS_PER_CALL only bounds one planner run; a
# chatty day of five calls could still queue 25 sends/dials against one number.
MAX_JOBS_PER_DAY = 10
MAX_RESEARCH_JOBS_PER_DAY = 3


def _normalize_iso(raw: str) -> str:
    """Coerce a model-written timestamp into something fromisoformat accepts.

    A real reminder call was silently lost to this. The model wrote
    "2026-08-22T00:05:00Z+00:00" — a Zulu marker AND an explicit offset — and
    the old `.replace("Z", "+00:00")` turned it into a string with two offsets,
    which raised, which returned None, which the companion then explained to the
    caller by inventing a reason. Two identical requests either side of it
    parsed fine, so the failure looked arbitrary from the outside.

    Accepts: trailing Z or z, a redundant Z before a real offset, +0000 without
    the colon, and surrounding whitespace. A naive string is returned unchanged
    and localised by the caller.
    """
    s = (raw or "").strip()
    if not s:
        return s
    s = s.replace(" ", "T", 1) if ("T" not in s and " " in s) else s
    s = re.sub(r"[Zz](?=\s*[+-]\d{2}:?\d{2}$)", "", s).strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s)
    return s


CALLING_WINDOW = (8, 21)
CALLING_WINDOW_LIVE = (7, 23)


def calling_window(requested_live: bool = False) -> tuple[int, int]:
    """The hours an outbound call may be placed, local to the person being called.

    (8, 21) is the TCPA limit — 8am to 9pm at the CALLED party's local time. The
    wider live window only applies when the caller themselves asked for a callback
    during a call, which is the one case where consent is on the record.
    """
    lo, hi = CALLING_WINDOW_LIVE if requested_live else CALLING_WINDOW
    env = os.getenv("RT_CALLBACK_HOURS_LIVE" if requested_live else "RT_CALLBACK_HOURS", "")
    if "-" in env:
        with contextlib.suppress(Exception):
            lo, hi = (int(x) for x in env.split("-", 1))
    return lo, hi


def _mask(e164) -> str:
    """Last four digits only — a dial target is PII and the log is not the place for it."""
    return f"***{str(e164 or '')[-4:]}"


def callee_zone(e164: str | None) -> tuple[str, bool]:
    """(zone, known) for the person being called. See rt_timezone."""
    zone, known = rt_timezone.zone_or_default(e164, os.getenv("DEFAULT_TZ", "America/New_York"))
    with contextlib.suppress(Exception):
        rt_obs.obs.event("scheduler.tz_resolved", tz=zone, method="known_prefix" if known else "default")
    return zone, known


def local_hour(dt, e164: str | None = None) -> int:
    """The hour of `dt` where the CALLEE is, not where the server is.

    This used to be DEFAULT_TZ for everybody, so the window was enforced against
    New York regardless of who was being rung. For a UK number that is five hours
    out — enough to place a call at 3am while the check reports a civil 22:00.
    """
    try:
        from zoneinfo import ZoneInfo
        zone, _ = callee_zone(e164)
        return dt.astimezone(ZoneInfo(zone)).hour
    except Exception as _exc:
        # Fail closed: the UTC hour is not the callee's hour, and a window
        # check against the wrong clock is how a 3am call passes as 22:00.
        rt_obs.obs.caught("rt_scheduler.local_hour", _exc)
        with contextlib.suppress(Exception):
            rt_obs.obs.warn("guard.decision", guard="local_hour", allowed=False,
                            reason="tz_unresolvable", err=type(_exc).__name__)
        raise ScheduleRefused("can't work out the local time for that number, so no call can be placed") from _exc


class ScheduleRefused(Exception):
    """A refusal with a reason a person can hear.

    schedule_job returned a bare None for six different situations — unknown
    type, unparseable time, bad timezone, too far out, in the past, and quiet
    hours — so the tool could not tell policy from breakage and told a caller
    "I wasn't able to schedule that for some reason". That is the malfunction
    costume the LIMITS law exists to forbid. Policy refusals now carry their
    reason; genuine failures still return None.
    """


def _parse_when(raw: str | datetime, caller_e164: str | None = None) -> datetime:
    """Parse an ISO datetime or natural language relative time (e.g. 'tomorrow 10am', 'in a couple of hours', 'after lunch')."""
    if isinstance(raw, datetime):
        return raw

    raw_str = (raw or "").strip()
    if not raw_str:
        raise ValueError("Empty run_at string")

    zone_name, _ = callee_zone(caller_e164)
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(zone_name)
    except Exception:
        tz = timezone.utc
    now = datetime.now(tz)

    if re.match(r"^\d{4}-\d{2}-\d{2}", raw_str):
        try:
            norm = _normalize_iso(raw_str)
            dt = datetime.fromisoformat(norm)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tz)
            return dt
        except Exception as exc:
            raise ValueError(f"Invalid ISO datetime: {raw!r}") from exc

    try:
        norm = _normalize_iso(raw_str)
        dt = datetime.fromisoformat(norm)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt
    except (ValueError, TypeError):
        pass

    lower = raw_str.lower().strip()
    _WORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
              "seven": "7", "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12"}
    for w, n in _WORDS.items():
        lower = re.sub(r"\b" + w + r"\b", n, lower)

    # "in a couple [of] hours / in an hour / in a minute"
    if re.search(r"^in a couple (?:of )?hours?$", lower):
        return now + timedelta(hours=2)
    if re.search(r"^in an hour$", lower):
        return now + timedelta(hours=1)
    if re.search(r"^in a (?:few|couple) (?:of )?mins?|minutes?$", lower):
        return now + timedelta(minutes=15)

    m_in = re.match(r"^in\s+(\d+)\s*(hour|hr|minute|min|day)s?$", lower)
    if m_in:
        amt = int(m_in.group(1))
        unit = m_in.group(2)
        if "hour" in unit or "hr" in unit:
            return now + timedelta(hours=amt)
        elif "min" in unit:
            return now + timedelta(minutes=amt)
        elif "day" in unit:
            return now + timedelta(days=amt)

    base_date = now.date()
    if "tomorrow" in lower or "first thing tomorrow" in lower:
        base_date = base_date + timedelta(days=1)
        lower = lower.replace("tomorrow", "").replace("first thing", "").replace("at", "").strip()
    elif "today" in lower or "tonight" in lower:
        lower = lower.replace("today", "").replace("tonight", "").replace("at", "").strip()

    # Colloquial times of day
    if "after lunch" in lower:
        return datetime(base_date.year, base_date.month, base_date.day, 13, 0, tzinfo=tz)
    if not lower or "first thing" in lower or lower in ("morning", "am", "in the morning"):
        return datetime(base_date.year, base_date.month, base_date.day, 9, 0, tzinfo=tz)
    if "noon" in lower or "midday" in lower:
        return datetime(base_date.year, base_date.month, base_date.day, 12, 0, tzinfo=tz)
    if lower in ("afternoon", "pm", "in the afternoon"):
        return datetime(base_date.year, base_date.month, base_date.day, 14, 0, tzinfo=tz)
    if lower in ("evening", "night", "tonight", "in the evening"):
        return datetime(base_date.year, base_date.month, base_date.day, 18, 0, tzinfo=tz)

    # "half past four" / "half past 4"
    m_half = re.match(r"^half past\s*([1-9]|1[0-2])\s*(am|pm)?$", lower)
    if m_half:
        hr = int(m_half.group(1))
        ampm = m_half.group(2)
        if ampm == "pm" and hr < 12:
            hr += 12
        elif ampm == "am" and hr == 12:
            hr = 0
        elif not ampm and hr < 8:  # assume afternoon if 1-7
            hr += 12
        return datetime(base_date.year, base_date.month, base_date.day, hr, 30, tzinfo=tz)

    m_time = re.match(r"^([1-9]|1[0-2]|2[0-3])(?::([0-5]\d))?\s*(am|pm)?$", lower)
    if m_time:
        hr = int(m_time.group(1))
        minute = int(m_time.group(2) or 0)
        ampm = m_time.group(3)
        if ampm == "pm" and hr < 12:
            hr += 12
        elif ampm == "am" and hr == 12:
            hr = 0
        return datetime(base_date.year, base_date.month, base_date.day, hr, minute, tzinfo=tz)

    raise ValueError(f"Unparseable datetime: {raw!r}")


def _check_daily_caps(phone_hash: str, job_type: str) -> None:
    """Refuse when today's job count for this caller is at the cap — or unknowable.

    Fails closed: if rt_count_jobs_today errors, returns nothing, or returns a
    shape we can't read, the insert does not happen. A cap that silently waves
    jobs through when the DB is flaky is not a cap. Sits OUTSIDE the persist
    try/except below on purpose — that block swallows exceptions into None.
    """
    try:
        raw = rt_prefs._req("POST", "rpc/rt_count_jobs_today", {"p_hash": phone_hash})
        if isinstance(raw, (str, bytes)):
            raw = json.loads(raw)
        if isinstance(raw, list) and len(raw) == 1:
            raw = raw[0]
        if not isinstance(raw, dict):
            raise ValueError(f"unexpected count payload: {type(raw).__name__}")
        total, research = raw.get("total"), raw.get("research")
        if not isinstance(total, int) or not isinstance(research, int) \
                or isinstance(total, bool) or isinstance(research, bool):
            raise ValueError("count payload missing total/research")
    except Exception as e:
        if "404" in str(e) or "Not Found" in str(e):
            print("[rt-scheduler] warning: rt_count_jobs_today RPC missing (404) — daily cap check bypassed", flush=True)
            return
        print(f"[rt-scheduler] REFUSE cannot verify daily count: {e}", flush=True)
        with contextlib.suppress(Exception):
            rt_obs.obs.warn("guard.decision", guard="schedule_job", allowed=False,
                            reason="count_unverifiable", type=job_type, err=type(e).__name__)
        raise ScheduleRefused("can't verify today's job count, so nothing new can be scheduled right now") from e

    if total >= MAX_JOBS_PER_DAY:
        print(f"[rt-scheduler] REFUSE daily cap: {total} >= {MAX_JOBS_PER_DAY}", flush=True)
        with contextlib.suppress(Exception):
            rt_obs.obs.warn("guard.decision", guard="schedule_job", allowed=False,
                            reason="daily_cap", type=job_type, total=total, cap=MAX_JOBS_PER_DAY)
        raise ScheduleRefused(f"you've already got {MAX_JOBS_PER_DAY} things lined up today, that's the limit")

    if job_type == "research" and research >= MAX_RESEARCH_JOBS_PER_DAY:
        print(f"[rt-scheduler] REFUSE research cap: {research} >= {MAX_RESEARCH_JOBS_PER_DAY}", flush=True)
        with contextlib.suppress(Exception):
            rt_obs.obs.warn("guard.decision", guard="schedule_job", allowed=False,
                            reason="research_cap", type=job_type, research=research,
                            cap=MAX_RESEARCH_JOBS_PER_DAY)
        raise ScheduleRefused(f"you can only look into {MAX_RESEARCH_JOBS_PER_DAY} things a day, and that's used up")


def schedule_job(
    phone_hash: str,
    job_type: str,
    payload: dict,
    run_at: str | datetime,
    caller_e164: str | None = None,
    requested_live: bool = False,
    exempt_from_caps: bool = False,
) -> dict | None:
    """Create a scheduled job. Returns the created job dict or None on failure.

    exempt_from_caps is for MOVING a job that already counted against today's
    cap (the quiet-hours deferral) — it adds no new touch, and refusing it
    would silently drop a promised callback. Nothing that originates from the
    planner or a tool call may pass it.
    """
    if job_type not in ("outbound_call", "research", "send_sms", "send_email", "reminder"):
        print(f"[rt-scheduler] REFUSE unknown job type: {job_type!r}", flush=True)
        with contextlib.suppress(Exception):
            rt_obs.obs.warn("guard.decision", guard="schedule_job", allowed=False,
                            reason="unknown_job_type", type=str(job_type)[:40])
        return None

    try:
        run_at_dt = _parse_when(run_at, caller_e164=caller_e164)
    except (ValueError, TypeError) as e:
        print(f"[rt-scheduler] REFUSE unparseable run_at: {run_at!r} ({e})", flush=True)
        with contextlib.suppress(Exception):
            rt_obs.obs.warn("guard.decision", guard="schedule_job", allowed=False,
                            reason="unparseable_run_at", type=job_type)
        return None

    max_future = datetime.now(timezone.utc) + timedelta(days=MAX_DAYS_OUT)
    if run_at_dt > max_future:
        print(f"[rt-scheduler] REFUSE job too far out: {run_at_dt} > {MAX_DAYS_OUT} days", flush=True)
        with contextlib.suppress(Exception):
            rt_obs.obs.warn("guard.decision", guard="schedule_job", allowed=False,
                            reason="beyond_max_days", type=job_type,
                            max_days=MAX_DAYS_OUT, run_at=run_at_dt.isoformat())
        raise ScheduleRefused(f"you can't schedule further than {MAX_DAYS_OUT} days ahead")

    if run_at_dt < datetime.now(timezone.utc) - timedelta(minutes=5):
        print(f"[rt-scheduler] REFUSE job in the past: {run_at_dt}", flush=True)
        with contextlib.suppress(Exception):
            rt_obs.obs.warn("guard.decision", guard="schedule_job", allowed=False,
                            reason="in_the_past", type=job_type,
                            run_at=run_at_dt.isoformat())
        raise ScheduleRefused("that time has already passed")

    if job_type == "outbound_call":
        hour = local_hour(run_at_dt, caller_e164)
        lo, hi = calling_window(requested_live)
        if not (lo <= hour < hi):
            print(f"[rt-scheduler] REFUSE outbound call outside quiet hours: "
                  f"{run_at_dt} (local hour {hour}, window {lo}-{hi})", flush=True)
            with contextlib.suppress(Exception):
                rt_obs.obs.warn("guard.decision", guard="schedule_job", allowed=False,
                                reason="quiet_hours", type=job_type,
                                local_hour=hour, window_lo=lo, window_hi=hi,
                                requested_live=bool(requested_live))
            raise ScheduleRefused(
                f"you only place calls between {lo % 12 or 12}{'am' if lo < 12 else 'pm'} and "
                f"{hi % 12 or 12}{'am' if hi < 12 else 'pm'}, and that time is outside it")

    if not exempt_from_caps:
        _check_daily_caps(phone_hash, job_type)

    job = {
        "phone_hash": phone_hash,
        "job_type": job_type,
        "payload": payload,
        "run_at": run_at_dt.isoformat(),
        "status": "pending",
        "attempts": 0,
        "caller_e164": caller_e164,
    }

    try:
        if job_type == "outbound_call":
            _new_id = rt_prefs._req("POST", "rpc/rt_schedule_job_superseding", {
                "p_hash": phone_hash,
                "p_type": job_type,
                "p_payload": json.dumps(payload),
                "p_run_at": run_at_dt.isoformat(),
            })
        else:
            _new_id = rt_prefs._req("POST", "rpc/rt_schedule_job", {
                "p_hash": phone_hash,
                "p_type": job_type,
                "p_payload": json.dumps(payload),
                "p_run_at": run_at_dt.isoformat(),
            })
        print(f"[rt-scheduler] job scheduled: type={job_type} run_at={run_at_dt.isoformat()}", flush=True)
        _job_id = None
        with contextlib.suppress(Exception):
            _job_id = int(_new_id) if isinstance(_new_id, (int, str)) else None
        with contextlib.suppress(Exception):
            rt_obs.obs.event("job.scheduled", job_id=_job_id, type=job_type,
                             run_at=run_at_dt.isoformat(),
                             superseding=(job_type == "outbound_call"),
                             requested_live=bool(requested_live),
                             payload_keys=len(payload or {}))
        return job
    except Exception as e:
        print(f"[rt-scheduler] schedule failed: {e}", flush=True)
        rt_obs.obs.caught("rt_scheduler.schedule_job.persist", e, level="ERROR")
        with contextlib.suppress(Exception):
            rt_obs.obs.error("job.failed", type=job_type, attempt=0, phase="schedule",
                             err=type(e).__name__)
        return None


_PLAN_TYPE_MAP = {
    "schedule_outbound_call": "outbound_call",
    "schedule_research": "research",
    "schedule_sms": "send_sms",
    "schedule_email": "send_email",
    "set_dated_reminder": "reminder",
}

# Immediate planner actions. Only sends — nothing else fires right after a call.
_IMMEDIATE_TYPE_MAP = {
    "send_email_summary": "send_email",
    "send_email": "send_email",
    "send_sms_summary": "send_sms",
    "send_sms": "send_sms",
}


def schedule_jobs_from_plan(phone_hash: str, actions: list[dict],
                            caller_e164: str | None = None) -> list[dict]:
    """Execute a batch of scheduled jobs from the postcall action planner.

    actions: list of {"action": str, "payload": dict, "run_at": str, "reason": str}
    Returns the list of successfully scheduled jobs.
    """
    created = []
    for i, action in enumerate(actions[:MAX_JOBS_PER_CALL]):
        act = action.get("action", "").strip()
        payload = action.get("payload") or {}
        run_at = action.get("run_at", "").strip()
        reason = action.get("reason", "").strip()

        if not act or not run_at:
            print(f"[rt-scheduler] skip action {i}: missing action or run_at", flush=True)
            with contextlib.suppress(Exception):
                rt_obs.obs.warn("guard.decision", guard="schedule_jobs_from_plan",
                                allowed=False, reason="missing_action_or_run_at",
                                index=i, has_action=bool(act), has_run_at=bool(run_at))
            continue

        # Strict: only a planner verb maps to a job type. A raw type name
        # ("outbound_call") is not a planner action and must not pass through.
        job_type = _PLAN_TYPE_MAP.get(act)
        if not job_type:
            print(f"[rt-scheduler] skip action {i}: unknown planner action {act[:40]!r}", flush=True)
            with contextlib.suppress(Exception):
                rt_obs.obs.warn("guard.decision", guard="schedule_jobs_from_plan",
                                allowed=False, reason="unknown_action", index=i,
                                action=act[:40])
            continue

        payload["reason"] = reason
        payload["caller_e164"] = caller_e164
        # The recipient is resolved from the on-file address at send time; a
        # model-written one never reaches the job row.
        payload.pop("to_email", None)

        try:
            result = schedule_job(phone_hash, job_type, payload, run_at, caller_e164)
        except ScheduleRefused as e:
            print(f"[rt-scheduler] planner job refused: {e}", flush=True)
            result = None
        if result:
            created.append(result)

    return created


def _execute_sms_job(job: dict) -> dict:
    """Execute a send_sms job."""
    import rt_sms
    payload = job.get("payload") or {}
    to = payload.get("caller_e164") or ""
    body = payload.get("body") or payload.get("message") or ""
    if not to or not body:
        return {"error": True, "message": "missing to or body"}
    return rt_sms.send_sms(to, body)


def _execute_email_job(job: dict) -> dict:
    """Execute a send_email job — to the verified on-file address ONLY.

    payload["to_email"] is ignored entirely. It was model-written from the
    transcript, so anyone on the line (or a spoofed line) could steer a recap
    of an older adult's call to an address of their choosing. The recipient is
    looked up fresh at send time; no address on file means no send.
    """
    import rt_email
    payload = job.get("payload") or {}
    phone_hash = str(job.get("phone_hash") or payload.get("phone_hash") or "").strip()
    subject = payload.get("subject") or "From Phone-Pal"
    body = payload.get("body") or ""
    if not body:
        return {"error": True, "message": "missing body"}
    # No hash, no lookup: an empty p_hash must never reach the bundle RPC.
    if not phone_hash:
        with contextlib.suppress(Exception):
            rt_obs.obs.warn("guard.decision", guard="execute_email_job", allowed=False,
                            reason="missing_phone_hash")
        return {"error": True, "message": "missing phone_hash"}

    to = ""
    try:
        bundle = rt_prefs._req("POST", "rpc/rt_get_caller_full_bundle", {"p_hash": phone_hash})
        caller = (bundle or {}).get("caller") if isinstance(bundle, dict) else None
        to = str((caller or {}).get("email") or "").strip() if isinstance(caller, dict) else ""
    except Exception as e:
        rt_obs.obs.caught("rt_scheduler._execute_email_job", e)
        to = ""
    if not to:
        with contextlib.suppress(Exception):
            rt_obs.obs.warn("guard.decision", guard="execute_email_job", allowed=False,
                            reason="no_verified_email")
        return {"error": True, "message": "no verified email on file"}
    with contextlib.suppress(Exception):
        # Telemetry only: a payload address that is not the on-file one is the
        # exact steering attempt this guard exists for. Worth counting.
        if payload.get("to_email") and not rt_email.is_allowed_recipient(payload.get("to_email"), to):
            rt_obs.obs.warn("guard.decision", guard="execute_email_job", allowed=True,
                            reason="payload_to_email_overridden")
    return rt_email.send_email(to, subject, body, verified_email=to)


def _execute_research_job(job: dict) -> dict:
    """Execute a research job — search + synthesize, store result."""
    import rt_executor
    import rt_postcall_worker
    payload = job.get("payload") or {}
    ask = payload.get("ask") or payload.get("question") or ""
    phone_hash = job.get("phone_hash") or ""
    if not ask or not phone_hash:
        return {"error": True, "message": "missing ask or phone_hash"}

    api_key = os.getenv("GOOGLE_API_KEY", "")
    if not api_key:
        return {"error": True, "message": "no GOOGLE_API_KEY"}

    try:
        schemas = (rt_prefs._req("POST", "rpc/rt_get_caller_full_bundle",
                                  {"p_hash": phone_hash}) or {}).get("schemas") or []
    except Exception as _exc:
        rt_obs.obs.caught("rt_scheduler._execute_research_job", _exc)
        schemas = []

    tid = rt_executor.capture(phone_hash, ask, schemas=schemas)
    if not tid:
        return {"error": True, "message": "task capture refused"}

    try:
        schemas = (rt_prefs._req("POST", "rpc/rt_get_caller_full_bundle",
                                  {"p_hash": phone_hash}) or {}).get("schemas") or []
    except Exception as _exc:
        rt_obs.obs.caught("rt_scheduler._execute_research_job", _exc)
        pass

    updates = rt_executor.execute_open_tasks(
        phone_hash, api_key, rt_postcall_worker._gemini_json, schemas)
    return {"error": False, "tasks_run": len(updates), "tid": tid}


def _execute_reminder_job(job: dict) -> dict:
    """Execute a reminder job — insert a reminder row."""
    payload = job.get("payload") or {}
    phone_hash = job.get("phone_hash") or ""
    text = payload.get("text") or payload.get("reminder") or ""
    if not phone_hash or not text:
        return {"error": True, "message": "missing phone_hash or text"}

    due = payload.get("due")
    if not isinstance(due, str) or not due.strip() or due.strip().lower() in ("null", "none"):
        due = None
    try:
        rt_prefs._req("POST", "rpc/rt_add_reminder", {
            "p_hash": phone_hash,
            "p_text": text,
            "p_due": due,
        })
        return {"error": False, "message": f"reminder set: {text[:60]}"}
    except Exception as e:
        rt_obs.obs.caught("rt_scheduler._execute_reminder_job", e)
        return {"error": True, "message": str(e)}


def _next_window_open(now, lo: int, e164: str | None = None):
    """The next moment the calling window opens, in the callee's timezone."""
    try:
        from zoneinfo import ZoneInfo
        zone, _ = callee_zone(e164)
        tz = ZoneInfo(zone)
    except Exception as _exc:
        rt_obs.obs.caught("rt_scheduler._next_window_open", _exc)
        return now + timedelta(hours=1)
    local = now.astimezone(tz)
    target = local.replace(hour=lo, minute=5, second=0, microsecond=0)
    if target <= local:
        target = target + timedelta(days=1)
    return target


def _execute_outbound_call_job(job: dict) -> dict:
    """Execute an outbound call job — call the user with a message.

    This dispatches a LiveKit SIP call to the caller's phone. The agent
    that picks up will have the job's message in its prompt context.
    """
    payload = job.get("payload") or {}
    caller_e164 = payload.get("caller_e164") or ""
    message = payload.get("message") or payload.get("context") or ""
    phone_hash = job.get("phone_hash") or ""

    if not caller_e164:
        return {"error": True, "message": "no caller_e164 for outbound call"}

    import rt_capabilities
    if not rt_capabilities.enabled("schedule_reminder_call"):
        print(f"[rt-scheduler] REFUSE outbound dial to {_mask(caller_e164)} — capability not enabled", flush=True)
        return {"error": True, "message": "outbound calling not enabled in this environment"}

    trunk_id = os.getenv("SIP_OUTBOUND_TRUNK_ID", "").strip()
    if not trunk_id:
        return {"error": True, "message": "no SIP_OUTBOUND_TRUNK_ID configured"}

    # The carrier's daily cap. Telnyx refused the call itself past $25/day and
    # this path leaned on that; Twilio cannot, so the worker asks the account
    # what today has cost and refuses on that answer — or on no answer.
    import rt_carrier
    allowed, why, detail = rt_carrier.outbound_allowed()
    if not allowed:
        print(f"[rt-scheduler] REFUSE outbound dial to {_mask(caller_e164)} — carrier cap: {why}", flush=True)
        return {"error": True, "message": f"carrier spend cap unmet ({why}); not dialled"}

    # The same dial policy the in-call bridge uses. It was missing here, and the
    # asymmetry ran the wrong way: bridge_call has a human on the line and was
    # guarded, while THIS path dials unattended and was not. Proven 2026-08-30 by
    # scheduling a UK number — every one of our checks passed it and Telnyx
    # refused it 350ms later with "not included in whitelisted countries USA".
    # The carrier should be the backstop, not the only stop.
    try:
        caller_e164 = rt_bridge.normalize_dialable(caller_e164)
    except rt_bridge.DialRefused as e:
        print(f"[rt-scheduler] REFUSE outbound dial to {_mask(caller_e164)} — {e}", flush=True)
        with contextlib.suppress(Exception):
            rt_obs.obs.warn("guard.decision", guard="execute_outbound_call",
                            allowed=False, reason="dial_policy", detail=str(e)[:120])
        return {"error": True, "message": f"dial policy refused this number: {e}"}

    # The window is checked again HERE, against the clock at the moment of dialling,
    # not the clock the job was written against. A job that is retried, reclaimed,
    # or picked up by a runner that was down overnight would otherwise ring an older
    # adult at whatever hour the runner happened to wake up. The strict TCPA window
    # applies: a stored job carries no record of live consent.
    now = datetime.now(timezone.utc)
    try:
        hour = local_hour(now, caller_e164)
    except ScheduleRefused as e:
        # Unknown local time is a refusal, not a retry: the clock will not
        # resolve itself in sixty seconds, and dialling blind is the harm.
        print(f"[rt-scheduler] REFUSE outbound dial to {_mask(caller_e164)} — {e}", flush=True)
        return {"error": True, "message": f"local time unknown; not dialled: {e}"}
    zone, zone_known = callee_zone(caller_e164)
    lo, hi = calling_window(False)
    if not (lo <= hour < hi):
        # Deferring must not look like a failure: the runner polls every 60s and
        # MAX_ATTEMPTS is 3, so returning an error here would retire a promised
        # callback inside three minutes of quiet hours. Move it to the next time
        # the window opens instead, and let this row retire as superseded.
        nxt = _next_window_open(now, lo, caller_e164)
        print(f"[rt-scheduler] DEFER outbound dial to {_mask(caller_e164)} — local hour {hour} "
              f"outside window {lo}-{hi}; moving to {nxt.isoformat()}", flush=True)
        with contextlib.suppress(Exception):
            rt_obs.obs.warn("guard.decision", guard="execute_outbound_call", allowed=False,
                            reason="quiet_hours", local_hour=hour, zone=zone,
                            zone_known=zone_known, window_lo=lo, window_hi=hi,
                            deferred_to=nxt.isoformat())
        try:
            # exempt_from_caps: this job already counted toward today's cap when
            # it was first scheduled. Moving it is not a new touch; capping it
            # here would turn a promised callback into a silent no-show.
            schedule_job(phone_hash, "outbound_call", payload, nxt, caller_e164=caller_e164,
                         exempt_from_caps=True)
            return {"error": False,
                    "message": f"outside the calling window; moved to {nxt.isoformat()}"}
        except Exception as e:
            rt_obs.obs.caught("rt_scheduler.defer_outbound", e)
            return {"error": True,
                    "message": f"outside the calling window ({lo}-{hi} local); not dialled"}

    room_name = f"outbound-{caller_e164[-4:]}-{int(time.time())}"

    room_metadata = json.dumps({
        "outbound_context": message,
        "phone_hash": phone_hash,
        "caller_e164": caller_e164,
    })

    try:
        from livekit import api as _lkapi
        import asyncio

        async def _dial():
            lkapi = _lkapi.LiveKitAPI()
            agent_name = os.getenv("AGENT_NAME", "phone-pal-dev")
            try:
                await lkapi.room.create_room(_lkapi.CreateRoomRequest(
                    name=room_name,
                    empty_timeout=5 * 60,
                    metadata=room_metadata,
                ))

                try:
                    await lkapi.agent_dispatch.create_dispatch(_lkapi.CreateAgentDispatchRequest(
                        agent_name=agent_name,
                        room=room_name,
                        metadata=room_metadata,
                    ))
                    print(f"[rt-scheduler] dispatched agent '{agent_name}' to room {room_name}", flush=True)
                except Exception as e:
                    print(f"[rt-scheduler] agent dispatch warning: {e}", flush=True)

                await lkapi.sip.create_sip_participant(_lkapi.CreateSIPParticipantRequest(
                    sip_trunk_id=trunk_id,
                    sip_call_to=caller_e164,
                    room_name=room_name,
                    participant_identity=f"caller-{caller_e164[-4:]}",
                    participant_name="Caller",
                    play_dialtone=False,
                ))
            finally:
                await lkapi.aclose()

        asyncio.run(_dial())
        print(f"[rt-scheduler] dispatched outbound call to {_mask(caller_e164)} in room {room_name}", flush=True)
        return {"error": False, "message": f"dialed {_mask(caller_e164)} in {room_name}"}

    except Exception as e:
        print(f"[rt-scheduler] outbound dial failed: {e}", flush=True)
        return {"error": True, "message": f"dial failed: {e}"}


_EXECUTORS = {
    "send_sms": _execute_sms_job,
    "send_email": _execute_email_job,
    "research": _execute_research_job,
    "reminder": _execute_reminder_job,
    "outbound_call": _execute_outbound_call_job,
}


def execute_job(job: dict) -> dict:
    """Execute a single job by type. Returns result dict."""
    job_type = job.get("job_type") or job.get("type") or ""
    _job_id = None
    _attempt = None
    with contextlib.suppress(Exception):
        _job_id = job.get("id")
        _attempt = int(job.get("attempts") or 0)
    executor = _EXECUTORS.get(job_type)
    if not executor:
        with contextlib.suppress(Exception):
            rt_obs.obs.error("job.failed", job_id=_job_id, type=str(job_type)[:40],
                             attempt=_attempt, err="unknown_job_type")
        return {"error": True, "message": f"unknown job type: {job_type!r}"}
    _t0 = time.perf_counter()
    try:
        result = executor(job)
    except Exception as e:
        print(f"[rt-scheduler] job execution failed ({job_type}): {e}", flush=True)
        with contextlib.suppress(Exception):
            _ms = round((time.perf_counter() - _t0) * 1000, 1)
            rt_obs.obs.caught("rt_scheduler.execute_job", e, level="ERROR", type=job_type)
            rt_obs.obs.event("job.executed", job_id=_job_id, type=job_type, ms=_ms, ok=False)
            rt_obs.obs.error("job.failed", job_id=_job_id, type=job_type, attempt=_attempt,
                             ms=_ms, err=type(e).__name__)
        return {"error": True, "message": str(e)}
    with contextlib.suppress(Exception):
        _ms = round((time.perf_counter() - _t0) * 1000, 1)
        _ok = not bool(result.get("error")) if isinstance(result, dict) else True
        rt_obs.obs.event("job.executed", job_id=_job_id, type=job_type, ms=_ms, ok=_ok)
        if not _ok:
            rt_obs.obs.error("job.failed", job_id=_job_id, type=job_type, attempt=_attempt,
                             ms=_ms, err="executor_reported_error")
    return result


def fetch_pending_jobs() -> list[dict]:
    """Fetch all jobs that are due for execution."""
    try:
        result = rt_prefs._req("POST", "rpc/rt_get_pending_jobs", {})
        if isinstance(result, list):
            return result
        return []
    except Exception as e:
        print(f"[rt-scheduler] fetch pending failed: {e}", flush=True)
        rt_obs.obs.caught("rt_scheduler.fetch_pending_jobs", e, level="ERROR")
        return []


def mark_job_done(job_id: int, result: dict, attempts: int = MAX_ATTEMPTS) -> None:
    """Mark a job completed, failed, or returned to 'pending' for a retry.

    A transient failure (a momentary carrier/LiveKit hiccup) used to be
    indistinguishable from a permanent one: ANY error went straight to
    'failed', so MAX_ATTEMPTS's retry budget was defined but unreachable —
    a promised callback could silently vanish on the first bad network
    blip. Only exhausting the attempt budget (already enforced at claim
    time by rt_get_pending_jobs's `attempts < 3`) is terminal now.
    """
    if result.get("error") and attempts < MAX_ATTEMPTS:
        status = "pending"
    elif result.get("error"):
        status = "failed"
    else:
        status = "done"
    try:
        rt_prefs._req("POST", "rpc/rt_update_job_status", {
            "p_id": job_id, "p_status": status, "p_result": json.dumps(result),
        })
        with contextlib.suppress(Exception):
            if status == "pending":
                rt_obs.obs.warn("job.scheduled", job_id=job_id, phase="retry",
                                attempt=attempts, max_attempts=MAX_ATTEMPTS)
            elif status == "failed":
                rt_obs.obs.error("job.failed", job_id=job_id, attempt=attempts,
                                 phase="retire", err="attempts_exhausted")
    except Exception as e:
        print(f"[rt-scheduler] mark done failed for job {job_id}: {e}", flush=True)
        rt_obs.obs.caught("rt_scheduler.mark_job_done", e, level="ERROR",
                          job_id=job_id, status=status)


def run_once() -> int:
    """Execute all pending jobs. Returns count executed.

    Runs the retention sweep first when it is due, so `--once` (cron-style
    deployments) performs the purge and not only the poll loop does.
    """
    _housekeeping_if_due()
    jobs = fetch_pending_jobs()
    if not jobs:
        return 0

    print(f"[rt-scheduler] {len(jobs)} pending job(s) found", flush=True)
    executed = 0
    for job in jobs:
        job_id = job.get("id")
        job_type = job.get("job_type", "?")
        attempts = int(job.get("attempts") or 0)

        print(f"[rt-scheduler] executing job {job_id} type={job_type} attempt={attempts}", flush=True)
        result = execute_job(job)
        mark_job_done(job_id, result, attempts=attempts)
        executed += 1
        print(f"[rt-scheduler] job {job_id} → {'OK' if not result.get('error') else 'FAILED'}: "
              f"{result.get('message', '')[:80]}", flush=True)

    return executed


import signal

_RUNNING = True
_LAST_HOUSEKEEPING = 0.0
_HOUSEKEEPING_EVERY_S = 3600


def _housekeeping() -> dict:
    """Run the retention purges. Each RPC is wrapped on its own so one failing
    never starves the other — forgotten callers must still be purged when the
    transcript sweep breaks, and vice versa."""
    out: dict = {}
    try:
        out["purge_forgotten"] = rt_prefs._req("POST", "rpc/rt_purge_forgotten", {})
    except Exception as e:
        rt_obs.obs.caught("rt_scheduler.housekeeping.purge_forgotten", e)
        out["purge_forgotten"] = f"error:{type(e).__name__}"
    try:
        days = int(os.getenv("RT_TRANSCRIPT_RETENTION_DAYS", "30"))
        out["purge_old_transcripts"] = rt_prefs._req("POST", "rpc/rt_purge_old_transcripts",
                                                     {"p_days": days})
    except Exception as e:
        rt_obs.obs.caught("rt_scheduler.housekeeping.purge_old_transcripts", e)
        out["purge_old_transcripts"] = f"error:{type(e).__name__}"
    print(f"[rt-scheduler] housekeeping: {out}", flush=True)
    with contextlib.suppress(Exception):
        rt_obs.obs.event("scheduler.housekeeping", **{k: str(v)[:60] for k, v in out.items()})
    return out


def _housekeeping_if_due() -> bool:
    """Run _housekeeping at most hourly (and on the first call after boot).

    Shared by run_loop and run_once so a `--once` pass sweeps too. The stamp
    is set even when the sweep raises: a broken purge must not be retried
    every poll.
    """
    global _LAST_HOUSEKEEPING
    if time.time() - _LAST_HOUSEKEEPING < _HOUSEKEEPING_EVERY_S:
        return False
    try:
        _housekeeping()
    except Exception as e:
        rt_obs.obs.caught("rt_scheduler.housekeeping", e)
    _LAST_HOUSEKEEPING = time.time()
    return True


def _handle_shutdown_signal(signum, frame) -> None:
    global _RUNNING
    print(f"[rt-scheduler] received signal {signum}, initiating graceful shutdown...", flush=True)
    _RUNNING = False


def run_loop(interval_seconds: int | None = None) -> None:
    """Poll for pending jobs in a loop with graceful signal handling."""
    global _RUNNING, _LAST_HOUSEKEEPING
    _RUNNING = True
    if interval_seconds is None:
        try:
            interval_seconds = int(os.getenv("RT_SCHEDULER_INTERVAL_SECONDS", "60"))
        except ValueError as _exc:
            rt_obs.obs.caught("rt_scheduler.interval_parse", _exc)
            interval_seconds = 60

    print(f"[rt-scheduler] starting poll loop (every {interval_seconds}s)", flush=True)
    try:
        signal.signal(signal.SIGTERM, _handle_shutdown_signal)
        signal.signal(signal.SIGINT, _handle_shutdown_signal)
    except (ValueError, AttributeError) as _exc:
        rt_obs.obs.caught("rt_scheduler.signal_bind", _exc)  # In non-main thread during unit tests

    try:
        n = rt_prefs._req("POST", "rpc/rt_reclaim_stale_jobs", {"p_minutes": 10})
        if n:
            print(f"[rt-scheduler] boot reclaim: {n} job(s) stranded 'running' returned to pending", flush=True)
            with contextlib.suppress(Exception):
                _reclaimed = len(n) if isinstance(n, list) else int(n)
                rt_obs.obs.warn("job.reclaimed", jobs=_reclaimed, stranded_for_s=600,
                                phase="boot")
    except Exception as e:
        print(f"[rt-scheduler] boot reclaim failed (non-fatal): {e}", flush=True)
        rt_obs.obs.caught("rt_scheduler.boot_reclaim", e)

    heartbeat_path = os.getenv("RT_SCHEDULER_HEARTBEAT_FILE",
                               os.path.join(tempfile.gettempdir(), "phone_pal_scheduler_heartbeat"))

    while _RUNNING:
        # Hourly, and on the first poll after boot, so a restart never skips a sweep.
        _housekeeping_if_due()
        try:
            run_once()
            with contextlib.suppress(Exception):
                with open(heartbeat_path, "w") as f:
                    f.write(str(time.time()))
        except Exception as e:
            print(f"[rt-scheduler] poll loop error: {e}", flush=True)
            rt_obs.obs.caught("rt_scheduler.run_loop", e, level="ERROR")

        # Sleep in 1-second chunks to react immediately to shutdown signals
        for _ in range(max(1, interval_seconds)):
            if not _RUNNING:
                break
            time.sleep(1)

    print("[rt-scheduler] graceful shutdown complete.", flush=True)


def execute_immediate_actions(phone_hash: str, actions: list[dict],
                              caller_e164: str | None = None) -> list[dict]:
    """Execute actions that should happen RIGHT NOW (not in the future).

    These are the post-call actions like "send email summary" and "send SMS recap"
    that the planner decided should fire immediately after the call ends.

    Returns list of results.
    """
    results = []
    for action in actions[:MAX_JOBS_PER_CALL]:
        act = str((action or {}).get("action") or "").strip()
        payload = action.get("payload") or {}
        reason = str(action.get("reason") or "")

        # Only a send may fire immediately; a stray "outbound_call" here would
        # ring a phone with no run_at, no window check and no consent record.
        job_type = _IMMEDIATE_TYPE_MAP.get(act)
        if not job_type:
            print(f"[rt-scheduler] skip immediate action: not a send ({act[:40]!r})", flush=True)
            with contextlib.suppress(Exception):
                rt_obs.obs.warn("guard.decision", guard="execute_immediate_actions",
                                allowed=False, reason="not_a_send", action=act[:40])
            continue

        payload["caller_e164"] = caller_e164
        payload["phone_hash"] = phone_hash
        # See _execute_email_job: the recipient is never the planner's to say.
        payload.pop("to_email", None)

        # Immediate sends count against the same per-day cap as scheduled
        # ones — otherwise five calls in a day mean ten uncounted messages.
        try:
            _check_daily_caps(phone_hash, job_type)
        except ScheduleRefused as e:
            print(f"[rt-scheduler] immediate action refused: {act} — {e}", flush=True)
            results.append({"error": True, "message": "daily cap", "action": act, "reason": reason})
            continue

        job = {
            "phone_hash": phone_hash,
            "job_type": job_type,
            "payload": payload,
        }

        print(f"[rt-scheduler] immediate action: {act} reason={reason[:60]}", flush=True)
        result = execute_job(job)
        result["action"] = act
        result["reason"] = reason
        results.append(result)
        if not result.get("error"):
            _record_immediate_send(phone_hash, job_type, act, caller_e164, result)

    return results


def _record_immediate_send(phone_hash: str, job_type: str, act: str,
                           caller_e164: str | None, result: dict) -> None:
    """Make an immediate send visible to rt_count_jobs_today.

    The counter only sees rows in rt.scheduled_jobs, so a send that never had a
    row is invisible to the cap. Insert one dated now and close it at once.
    Bookkeeping after the fact: the message is already gone, so a failure
    here is logged, not raised.
    """
    try:
        new_id = rt_prefs._req("POST", "rpc/rt_schedule_job", {
            "p_hash": phone_hash,
            "p_type": job_type,
            "p_payload": json.dumps({"immediate": True, "action": act, "caller_e164": caller_e164}),
            "p_run_at": datetime.now(timezone.utc).isoformat(),
        })
        rt_prefs._req("POST", "rpc/rt_update_job_status", {
            "p_id": new_id, "p_status": "done",
            "p_result": json.dumps({"error": False, "immediate": True,
                                    "message": str(result.get("message") or "")[:120]}),
        })
    except Exception as e:
        print(f"[rt-scheduler] immediate send not recorded against daily cap: {e}", flush=True)
        rt_obs.obs.caught("rt_scheduler.record_immediate_send", e, type=job_type)


if __name__ == "__main__":
    if "--once" in sys.argv:
        n = run_once()
        print(f"Executed {n} job(s).")
    else:
        run_loop()
