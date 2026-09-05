#!/usr/bin/env python3
"""rt_email.py — Email and Calendar Invite dispatch engine via Resend.

Provides email sending, HTML formatting, and 1-click .ics calendar invite generation
for Iris Phone Pal.
"""
import rt_obs
import base64
import contextlib
import html
import json
import os
import re
import time
import uuid
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv(".env.local")
load_dotenv(".env")

RESEND_API_URL = "https://api.resend.com/emails"

_OBS_HOST = "api.resend.com"
_OBS_PATH = "/emails"
with contextlib.suppress(Exception):
    _obs_parsed = urllib.parse.urlsplit(RESEND_API_URL)
    _OBS_HOST = _obs_parsed.hostname or _OBS_HOST
    _OBS_PATH = _obs_parsed.path or _OBS_PATH


PLATFORM_SENDER_FALLBACK = "onboarding@resend.dev"

# A text field that *starts* like a property ("ATTENDEE:", "ORGANIZER;CN=") is an
# injection attempt, not a title. Escaping alone would neutralise it, but a
# planner-written title has no honest reason to look like this, so refuse.
_PROPERTY_LIKE = re.compile(r"^[A-Z-]+[:;]")


def platform_sender_address() -> str:
    """The bare address of the platform sender — the only ORGANIZER an invite may name."""
    raw = os.getenv("RESEND_FROM_EMAIL", f"Voice Companion <{PLATFORM_SENDER_FALLBACK}>")
    m = re.search(r"<([^<>\s]+@[^<>\s]+)>", raw)
    addr = (m.group(1) if m else raw).strip()
    return addr if "@" in addr else PLATFORM_SENDER_FALLBACK


def ics_text(value, *, field: str = "text") -> str:
    """RFC 5545 TEXT escaping with CR/LF removed and property-like prefixes refused.

    Newlines are stripped rather than escaped: a line break in a title or a
    location is the only way to smuggle a new property (ATTENDEE, ORGANIZER)
    into the VEVENT, and neither field has a legitimate use for one.
    """
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    text = " ".join(text.split())
    if _PROPERTY_LIKE.match(text):
        raise ValueError(f"{field} looks like an iCalendar property")
    return (text.replace("\\", "\\\\")
                .replace(";", "\\;")
                .replace(",", "\\,"))


def generate_ics(title: str, date_time_str: str, duration_mins: int = 30, location: str = "") -> str:
    """Generate a standard RFC 5545 iCalendar string for 1-click calendar invites.

    Raises ValueError when title/location begin with a property-like token; the
    caller must not send an invite it could not build safely.
    """
    dt = None
    try:
        dt = datetime.fromisoformat(date_time_str.replace("Z", "+00:00"))
    except Exception as _exc:
        rt_obs.obs.caught("rt_email.generate_ics", _exc)
        dt = datetime.now(timezone.utc) + timedelta(days=1)
        dt = dt.replace(hour=14, minute=0, second=0, microsecond=0)

    safe_title = ics_text(title, field="title") or "Appointment"
    safe_location = ics_text(location, field="location") or "Phone Call / Online"

    start_str = dt.strftime("%Y%m%dT%H%M%SZ")
    end_dt = dt + timedelta(minutes=duration_mins)
    end_str = end_dt.strftime("%Y%m%dT%H%M%SZ")
    stamp_str = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    uid = f"iris-evt-{stamp_str}-{hash(title) & 0xffff}@iris.church"

    ics_content = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//Voice Companion//NONSGML Calendar Event//EN\r\n"
        "METHOD:REQUEST\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"DTSTAMP:{stamp_str}\r\n"
        f"DTSTART:{start_str}\r\n"
        f"DTEND:{end_str}\r\n"
        f"ORGANIZER;CN=Voice Companion:mailto:{platform_sender_address()}\r\n"
        f"SUMMARY:{safe_title}\r\n"
        f"LOCATION:{safe_location}\r\n"
        "DESCRIPTION:Scheduled appointment via Voice Companion.\r\n"
        "STATUS:CONFIRMED\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    return ics_content


from typing import Optional


def is_allowed_recipient(to_email, verified_email) -> bool:
    """True only when `to_email` IS the verified on-file address.

    The planner's payload["to_email"] is model-written and therefore reachable
    from the transcript; the only address a post-call email may go to is the
    one the caller verified. Exact match after strip()/casefold(); empty on
    either side refuses.
    """
    a = (to_email or "").strip().casefold()
    b = (verified_email or "").strip().casefold()
    return bool(a) and bool(b) and a == b


def send_email(to_email: str, subject: str, body_text: str, html_body: str = "",
               ics_event: Optional[dict] = None, api_key: Optional[str] = None, *,
               verified_email: Optional[str] = None) -> dict:
    """Send an email with optional 1-click .ics calendar invite attachment via Resend API.

    Refuses outright unless `to_email` IS `verified_email` — the on-file address
    the caller confirmed. Every caller must pass it; a call site that forgets
    is treated as unverified, not as trusted, so the guard cannot be bypassed
    by omission. Nothing is sent on refusal.
    """
    if not is_allowed_recipient(to_email, verified_email):
        with contextlib.suppress(Exception):
            rt_obs.obs.warn("guard.decision", guard="send_email", allowed=False,
                            reason="recipient_not_verified",
                            has_verified=bool((verified_email or "").strip()))
        return {"error": True, "message": "recipient not verified"}

    key = api_key or os.getenv("RESEND_API_KEY", "")
    if not key:
        return {"error": True, "message": "RESEND_API_KEY is not configured."}

    to_email = (to_email or "").strip()
    if not to_email or "@" not in to_email:
        return {"error": True, "message": f"Invalid recipient email: {to_email!r}"}

    from_email = os.getenv("RESEND_FROM_EMAIL", "Voice Companion <onboarding@resend.dev>")

    if html_body:
        # A caller-supplied HTML part is trusted by contract; the default part is
        # built from model/transcript-reachable text and must be inert markup.
        html_content = html_body
    else:
        safe_body = html.escape(body_text or "", quote=True).replace("\n", "<br>")
        html_content = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; color: #1f2937;">
        <h2 style="color: #4f46e5; margin-bottom: 16px;">Voice Companion</h2>
        <div style="background-color: #f9fafb; border-left: 4px solid #6366f1; padding: 16px; border-radius: 4px; line-height: 1.6;">{safe_body}</div>
        <p style="margin-top: 24px; font-size: 13px; color: #6b7280; text-align: center;">Sent with warmth by your Voice Companion</p>
    </div>
    """

    # Header line: CR/LF would let a planner-written subject grow extra headers.
    safe_subject = " ".join(str(subject or "").replace("\r", " ").replace("\n", " ").split())
    payload = {
        "from": from_email,
        "to": [to_email],
        "subject": safe_subject,
        "text": body_text,
        "html": html_content,
    }

    if ics_event:
        title = ics_event.get("title", "Appointment")
        date_str = ics_event.get("date_time", "")
        location = ics_event.get("location", "")
        try:
            ics_data = generate_ics(title, date_str, location=location)
        except ValueError as e:
            # Refuse the whole send: an invite we could not build safely is not
            # something to quietly drop while the covering email still goes out.
            with contextlib.suppress(Exception):
                rt_obs.obs.warn("guard.decision", guard="send_email", allowed=False,
                                reason="ics_rejected", detail=str(e)[:120])
            return {"error": True, "message": f"calendar invite refused: {e}"}
        encoded_ics = base64.b64encode(ics_data.encode("utf-8")).decode("ascii")
        payload["attachments"] = [{
            "filename": f"{re.sub(r'[^a-zA-Z0-9]', '_', str(title))[:60] or 'invite'}.ics",
            "content": encoded_ics,
            "content_type": "text/calendar",
        }]

    _obs_t0 = time.perf_counter()
    _obs_bytes = None
    with contextlib.suppress(Exception):
        _obs_bytes = len(json.dumps(payload, default=str).encode("utf-8"))

    try:
        from rt_http import http_client
        from rt_logger import get_logger
        log = get_logger("rt_email")
        res_data = http_client.request(
            "POST",
            RESEND_API_URL,
            headers={"Authorization": f"Bearer {key}",
                     # One send, ever: a retry after an ambiguous failure is a
                     # duplicate email, and the key lets Resend dedupe even that.
                     "Idempotency-Key": str(uuid.uuid4())},
            data=payload,
            timeout=12.0,
            retries=0,
        )
        with contextlib.suppress(Exception):
            rt_obs.obs.event(
                "net.request",
                host=_OBS_HOST,
                path=_OBS_PATH,
                method="POST",
                api="resend",
                ms=round((time.perf_counter() - _obs_t0) * 1000, 1),
                bytes=_obs_bytes,
                accepted=bool(isinstance(res_data, dict) and res_data.get("id")),
                attachments=1 if ics_event else 0,
            )
            rt_obs.obs.event(
                "email.dispatch_detail",
                ms=round((time.perf_counter() - _obs_t0) * 1000, 1),
                message_id=str(res_data.get("id") if isinstance(res_data, dict) else res_data),
                has_ics=bool(ics_event),
            )
        if isinstance(res_data, dict) and res_data.get("id"):
            log.info(f"Email sent successfully to {to_email}", email_id=res_data.get("id"), recipient=to_email)
            return {"error": False, "id": res_data.get("id")}
        return {"error": False, "id": str(res_data)}
    except urllib.error.HTTPError as e:
        with contextlib.suppress(Exception):
            rt_obs.obs.event(
                "net.failed",
                host=_OBS_HOST,
                path=_OBS_PATH,
                method="POST",
                api="resend",
                ms=round((time.perf_counter() - _obs_t0) * 1000, 1),
                status=getattr(e, "code", None),
                bytes=_obs_bytes,
                err="HTTPError",
            )
        err_body = e.read().decode("utf-8", errors="ignore")
        log.error(f"Resend HTTP Error {e.code}: {err_body}", recipient=to_email, status=e.code)
        return {"error": True, "message": f"HTTP {e.code}: {err_body}"}
    except Exception as e:
        with contextlib.suppress(Exception):
            rt_obs.obs.event(
                "net.failed",
                host=_OBS_HOST,
                path=_OBS_PATH,
                method="POST",
                api="resend",
                ms=round((time.perf_counter() - _obs_t0) * 1000, 1),
                status=getattr(e, "code", None),
                bytes=_obs_bytes,
                err=type(e).__name__,
            )
        log.error(f"Email send failed: {e}", recipient=to_email)
        return {"error": True, "message": str(e)}


if __name__ == "__main__":
    print("Testing rt_email module...")
    test_key = os.getenv("RESEND_API_KEY", "")
    print(f"API Key present: {bool(test_key)}")
    ics = generate_ics("Dr. Bergman Appointment", "2026-08-13T10:30:00Z", location="Newton Medical Center")
    print("Generated ICS length:", len(ics))
