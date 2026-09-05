"""rt_sms_inbound.py — what happens when someone texts Iris.

rt_health receives Twilio's POST, proves it is Twilio's
(rt_sms.webhook_is_from_twilio) and hands the fields to accept_webhook(),
which answers in milliseconds. The reply itself — a memory bundle, up to two
media fetches, one Gemini call — is generated and sent on a worker thread,
because Twilio abandons a webhook after 15 seconds and retries it, and this
path can take 45 (two 12 s media fetches, a 20 s model call, the bundle).
Until 2026-09-02 it ran inside the request,
on a single-threaded server that also served /ready, so one slow text blocked
the readiness probe for the whole container and the retry produced a second
reply and a second ledger row. MessageSid dedupe closes the second half.
"""
from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import rt_obs
import rt_prefs
import rt_sms

_obs = rt_obs.get("rt_sms_inbound")

_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"

# Twilio's own MMS ceiling. The old fetch read the whole body into memory and
# then base64-encoded it into the model request with no bound at all.
_MEDIA_MAX_BYTES = 5 * 1024 * 1024
_MEDIA_MAX_HOPS = 3
_MEDIA_TIMEOUT_S = 12
_MEDIA_HOST = "api.twilio.com"
_MEDIA_CARRY_FORWARD_S = 900

_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="sms-inbound")
_SEEN_SIDS: OrderedDict[str, float] = OrderedDict()
_SEEN_MAX = 2000
_SEEN_LOCK = threading.Lock()

_mask = rt_sms._mask


# ── Media ────────────────────────────────────────────────────────────────────

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse to follow redirects automatically; fetch_twilio_media follows
    them itself so the Authorization header never travels to the next host.
    urllib's default handler copies every header but Content-* onto the
    redirected request."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _twilio_basic_auth() -> str | None:
    sid = (os.getenv("TWILIO_ACCOUNT_SID") or "").strip()
    token = (os.getenv("TWILIO_AUTH_TOKEN") or "").strip()
    if not sid or not token:
        return None
    return "Basic " + base64.b64encode(f"{sid}:{token}".encode("utf-8")).decode("ascii")


def fetch_twilio_media(url: str) -> tuple[bytes, str] | None:
    """Download one MMS attachment. Returns (bytes, mime) or None.

    The account credentials go on exactly one request: the first hop, and
    only when its host is api.twilio.com. Review 2026-09-02: the check used
    to be `"twilio.com" in url`, so a MediaUrl0 of
    https://evil.example/twilio.com received the account SID and auth token
    as Basic auth. Redirects (Twilio media 302s to a CDN) are followed by
    hand, without the header, https only, at most _MEDIA_MAX_HOPS deep.
    Anything that is not an image under _MEDIA_MAX_BYTES is dropped.
    """
    parts = urllib.parse.urlsplit(url or "")
    if parts.scheme != "https" or (parts.hostname or "").lower() != _MEDIA_HOST:
        print(f"[rt-sms-inbound] media refused: host={parts.hostname!r} scheme={parts.scheme!r}", flush=True)
        return None
    auth = _twilio_basic_auth()
    current = url
    for hop in range(_MEDIA_MAX_HOPS + 1):
        cur = urllib.parse.urlsplit(current)
        if cur.scheme != "https":
            print(f"[rt-sms-inbound] media refused: redirect to scheme={cur.scheme!r}", flush=True)
            return None
        headers = {"User-Agent": "Phone-Pal/1.0"}
        if hop == 0 and auth:
            headers["Authorization"] = auth
        req = urllib.request.Request(current, headers=headers)  # noqa: S310 - host checked above, https only
        try:
            resp = _OPENER.open(req, timeout=_MEDIA_TIMEOUT_S)  # noqa: S310
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                loc = e.headers.get("Location") if e.headers is not None else None
                if not loc:
                    return None
                current = urllib.parse.urljoin(current, loc)
                continue
            print(f"[rt-sms-inbound] media fetch HTTP {e.code}", flush=True)
            return None
        except Exception as exc:
            _obs.caught("rt_sms_inbound.fetch_twilio_media", exc)
            return None
        with resp:
            length = resp.headers.get("Content-Length") or ""
            if length.isdigit() and int(length) > _MEDIA_MAX_BYTES:
                print(f"[rt-sms-inbound] media refused: {length} bytes", flush=True)
                return None
            data = resp.read(_MEDIA_MAX_BYTES + 1)
            if len(data) > _MEDIA_MAX_BYTES:
                print("[rt-sms-inbound] media refused: over size cap", flush=True)
                return None
            mime = (resp.headers.get_content_type() or "").lower()
            if not mime.startswith("image/"):
                print(f"[rt-sms-inbound] media skipped: {mime or 'unknown type'}", flush=True)
                return None
            return data, mime
    print("[rt-sms-inbound] media refused: too many redirects", flush=True)
    return None


# ── Generation ───────────────────────────────────────────────────────────────

def _generate_sms_reply(api_key: str, system_prompt: str, user_text: str,
                        media_parts: list[tuple[bytes, str]] | None = None) -> str:
    """One Gemini 2.5 Flash call with Google Search grounding; returns the text."""
    parts: list[dict[str, Any]] = [{"text": user_text}]
    for data, mime in (media_parts or [])[:2]:
        parts.append({"inline_data": {"mime_type": mime,
                                      "data": base64.b64encode(data).decode("ascii")}})
    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": parts}],
        "tools": [{"googleSearch": {}}],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 800,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    req = urllib.request.Request(  # noqa: S310 - fixed https endpoint
        _GEMINI_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 - fixed https endpoint
        data = json.loads(resp.read().decode("utf-8"))
        cand = (data.get("candidates") or [{}])[0]
        parts_list = cand.get("content", {}).get("parts", [])
        reply = "".join(p.get("text", "") for p in parts_list).strip()
        return re.sub(r'^["\']|["\']$', "", reply).strip()


def _caller_context(from_e164: str) -> tuple[str, str, str]:
    """(display_name, agent_alias, context line) from the caller's bundle."""
    display_name, agent_alias = "there", "Iris"
    facts: list[str] = []
    h = rt_prefs.phone_hash(from_e164)
    if h:
        with contextlib.suppress(Exception):
            bundle = rt_prefs._req("POST", "rpc/rt_get_caller_full_bundle", {"p_hash": h}) or {}
            caller = bundle.get("caller") or {}
            if caller.get("display_name"):
                display_name = caller["display_name"]
            if caller.get("agent_alias"):
                agent_alias = caller["agent_alias"]
            if caller.get("loved_ones"):
                facts.append(f"Loved ones & pets: {caller['loved_ones']}")
            if caller.get("next_call_context"):
                facts.append(f"Recent context: {caller['next_call_context']}")
            for schema in bundle.get("schemas", []):
                cat = schema.get("category", "")
                data = schema.get("data_summary", "")
                if cat in ("family", "pet", "pets", "goals", "health", "hobbies") and data:
                    facts.append(f"{cat}: {data}")
    context = "; ".join(facts[:6]) if facts else "No prior history recorded yet."
    return display_name, agent_alias, context


def process_incoming_sms(from_number: str, body: str, media_urls: list[str] | None = None,
                         message_sid: str | None = None) -> str | None:
    """Record an inbound text and compose Iris's reply. None means "do not reply".

    The ledger write comes first, before any gate: the call process polls it
    for texts sent mid-call, and that must work even on a lane where replies
    are switched off.
    """
    from_e164 = rt_prefs.normalize_e164(from_number)
    if not from_e164:
        print("[rt-sms-inbound] ignored a text from an unparseable number", flush=True)
        return None
    clean_body = (body or "").strip()
    media = [m for m in (media_urls or []) if m]
    logged_text = clean_body or ("[Sent a photo]" if media else "")

    _obs.event("sms.inbound_received", chars=len(clean_body), has_media=bool(media),
               sid=(message_sid or None), duplicate=False)
    print(f"[rt-sms-inbound] Received message from {_mask(from_e164)}: "
          f"chars={len(clean_body)} media={len(media)}", flush=True)
    rt_sms.record_sms(from_e164, "inbound", logged_text, media, sid=message_sid)

    import rt_pray
    if rt_pray.is_pray_lane():
        is_crisis, crisis_reply = rt_pray.check_crisis(clean_body)
        if is_crisis and crisis_reply:
            print(f"[rt-sms-inbound] PrayPal 988 crisis intercept triggered for {_mask(from_e164)}", flush=True)
            return crisis_reply

    import rt_capabilities
    if not rt_capabilities.enabled("send_sms"):
        # Same law as the send_sms tool: no reply is promised that cannot arrive.
        print("[rt-sms-inbound] recorded, not replying: send_sms is not enabled in this lane", flush=True)
        return None

    api_key = (os.getenv("GOOGLE_API_KEY") or "").strip()
    if not api_key:
        return "Thanks for your text! I'll talk with you next time we chat on the phone."

    display_name, agent_alias, context_str = _caller_context(from_e164)

    recent_history = rt_sms.get_recent_sms(from_e164, limit=6)
    history_lines = []
    for msg in recent_history:
        sender_lbl = display_name if msg.get("direction") == "inbound" else agent_alias
        history_lines.append(f"{sender_lbl}: {msg.get('text', '')}")
    thread_context = "\n".join(history_lines) if history_lines else f"{display_name}: {logged_text}"

    # No new photo: carry forward one they sent in the last few minutes so a
    # follow-up question about it still has the picture.
    active_media = list(media)
    if not active_media:
        now = time.time()
        for m in reversed(recent_history):
            if m.get("media") and (now - float(m.get("ts") or 0)) < _MEDIA_CARRY_FORWARD_S:
                active_media = list(m["media"])
                break
    media_parts: list[tuple[bytes, str]] = []
    for m_url in active_media[:2]:
        got = fetch_twilio_media(m_url)
        if got:
            media_parts.append(got)

    if rt_pray.is_pray_lane():
        h_hash = rt_prefs.phone_hash(from_e164)
        bundle = rt_pray.get_bundle(h_hash) if h_hash else {}
        caller_info = bundle.get("caller") or {}
        guide_key = caller_info.get("active_guide") or os.getenv("PRAY_DEFAULT_GUIDE", "god")
        tradition = caller_info.get("spiritual_tradition") or "universal"
        caller_name = caller_info.get("display_name") or "Friend"
        memories = bundle.get("memories") or []
        intentions = bundle.get("intentions") or []

        system_prompt = rt_pray.build_sms_prompt(
            guide_key=guide_key,
            tradition=tradition,
            caller_name=caller_name,
            memories=memories,
            intentions=intentions,
            thread_context=thread_context,
        )
    else:
        system_prompt = (
            f"You are {agent_alias}, texting back your close friend {display_name} on their mobile phone.\n"
            f"Context on {display_name}: {context_str}\n\n"
            f"RECENT CHAT THREAD:\n{thread_context}\n\n"
            f"TEXTING STYLE RULES (CRITICAL - DO NOT VIOLATE):\n"
            f"1. LENGTH: 1 short sentence or phrase only. Maximum 120 characters. Real people text in quick, punchy bursts.\n"
            f"2. TONE: Warm, casual, authentic friend texting on an iPhone. Relaxed, punchy, and modern ('haha', 'yup', 'got it!').\n"
            f"3. ZERO FOOTERS / NO ROBOTIC SIGN-OFFS: NEVER say 'I am Iris...', NEVER say 'Always here if you want to chat or call', NEVER say 'How can I assist you?'. Real friends NEVER add signatures or customer support closings.\n"
            f"4. SEARCH & LIVE FACTS: Google Search is enabled. When asked about stocks, live events, or links, answer with current real-world facts.\n"
            f"5. PHOTOS & IMAGES: If an image is attached or recently discussed, look at it directly and describe what is actually in the picture with 100% accuracy (clothing colors, bike, whether a helmet is worn, etc.). Never make up clothing or details.\n"
            f"6. NO ESSAYS: This is SMS text messaging, NOT a phone conversation or an email."
        )

    try:
        reply = _generate_sms_reply(api_key, system_prompt, clean_body or "(sent a photo)", media_parts)
        if rt_pray.is_pray_lane() and not reply:
            return "Peace be with you. Your prayer is held close to heart."
        return reply or f"Got your text, {display_name}! Good to hear from you."
    except Exception as exc:
        print(f"[rt-sms-inbound] Error generating SMS reply: {type(exc).__name__}", flush=True)
        _obs.caught("rt_sms_inbound.process_incoming_sms", exc)
        if rt_pray.is_pray_lane():
            return "Peace be with you. I am holding your prayer close to heart."
        return f"Hey {display_name}! Got your message — looking forward to our next phone chat soon."


# ── The webhook side ─────────────────────────────────────────────────────────

def handle_incoming(texter_e164: str, our_number: str, body: str,
                    media_urls: list[str], message_sid: str) -> None:
    """Worker-thread body: compose the reply and send it back through the REST
    API from the number they texted, so the thread stays on one number."""
    try:
        reply = process_incoming_sms(texter_e164, body, media_urls, message_sid=message_sid)
        if not reply:
            return
        res = rt_sms.send_sms(texter_e164, reply, from_number=(our_number or "").strip() or None)
        if res.get("error"):
            print(f"[rt-sms-inbound] reply to {_mask(texter_e164)} failed: {res.get('message')}", flush=True)
        else:
            print(f"[rt-sms-inbound] Replying to {_mask(texter_e164)}: sid={res.get('sid')} chars={len(reply)}",
                  flush=True)
    except Exception as exc:
        _obs.caught("rt_sms_inbound.handle_incoming", exc)


def _dispatch(fn, *args) -> None:
    _EXECUTOR.submit(fn, *args)


def _first_sighting(sid: str) -> bool:
    with _SEEN_LOCK:
        if sid in _SEEN_SIDS:
            return False
        _SEEN_SIDS[sid] = time.time()
        while len(_SEEN_SIDS) > _SEEN_MAX:
            _SEEN_SIDS.popitem(last=False)
        return True


def accept_webhook(from_number: str, to_number: str, body: str,
                   media_urls: list[str] | None, message_sid: str | None) -> bool:
    """Called by rt_health once the signature is proven. Returns True when the
    message was queued, False when Twilio was retrying one already handled."""
    sid = (message_sid or "").strip()
    media = list(media_urls or [])
    if sid and (not _first_sighting(sid) or rt_sms.seen_sid(from_number, sid)):
        _obs.event("sms.inbound_received", chars=len(body or ""), has_media=bool(media),
                   sid=sid, duplicate=True)
        return False
    _dispatch(handle_incoming, from_number, to_number, body or "", media, sid)
    return True
