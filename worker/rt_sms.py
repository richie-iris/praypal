"""rt_sms.py — Twilio SMS/MMS: sending, webhook authentication, and the message ledger.

Twilio is the only messaging carrier. The Telnyx branch came out 2026-09-02:
send_sms preferred Twilio whenever its credentials were present, so every real
text went through the one branch no test covered, while the harness kept
asserting the Telnyx endpoint the lane never used. Later the same day the
voice side followed (ADR 0006): Twilio is the only carrier there is.

Three things live here because they share one credential:

  send_sms()                the REST call, and the only path that sends.
  webhook_is_from_twilio()  proves a POST to /sms/incoming was signed with our
                            auth token. Until 2026-09-02 nothing checked, so a
                            request naming any caller's number in `From` got
                            that caller's memories summarised back to it.
  the ledger                the last 30 texts per caller, shared between the
                            webhook process and the call process through the
                            filesystem, keyed by phone_hash. It used to be keyed
                            by the digits of the number, in /tmp, forever —
                            which docs/RETENTION.md said never happens.
"""
from __future__ import annotations

import rt_obs

import base64
import contextlib
import hashlib
import hmac
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping
from dotenv import load_dotenv

import fcntl

load_dotenv(".env.local")
load_dotenv(".env")

_obs = rt_obs.get("rt_sms")

_TWILIO_API = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"


def _net_where(url: str) -> dict:
    """host and path only. Never the payload — the body is the caller's words."""
    try:
        parts = urllib.parse.urlsplit(url or "")
        return {"host": parts.hostname or "", "path": parts.path or "/"}
    except Exception as _exc:
        _obs.caught("rt_sms._net_where", _exc)
        return {"host": "", "path": ""}


def _mask(e164: str | None) -> str:
    """'+19174030642' → '***0642' — enough to correlate a log, not to dial."""
    digits = re.sub(r"\D", "", str(e164 or ""))
    return f"***{digits[-4:]}" if digits else "***"


# ── Webhook authentication ───────────────────────────────────────────────────
# Twilio signs every request it makes: base64(HMAC-SHA1(auth_token, url +
# every POST field, key then value, sorted by key)). The url is the one typed
# into the Twilio console, not the one the worker sees behind Caddy, which is
# why rt_health offers several candidates.

def twilio_signature(auth_token: str, url: str, params: Mapping[str, str | list[str]]) -> str:
    """The signature Twilio would put in X-Twilio-Signature for this request."""
    s = url
    for k in sorted(params):
        v = params[k]
        if isinstance(v, (list, tuple)):
            for item in sorted(str(x) for x in v):
                s += k + item
        else:
            s += k + str(v)
    digest = hmac.new(auth_token.encode("utf-8"), s.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def webhook_is_from_twilio(signature: str | None, urls: Iterable[str],
                           params: Mapping[str, str | list[str]]) -> bool:
    """True only when `signature` matches one of `urls` under our auth token.

    Fails closed: no token configured, no header, or no match all refuse. The
    refusal is telemetry (`sms.webhook_rejected`) so a probe against the
    endpoint shows up, but nothing about the request body is logged — the
    body is whatever the sender chose to put there.
    """
    token = (os.getenv("TWILIO_AUTH_TOKEN") or "").strip()
    if not token:
        _obs.warn("sms.webhook_rejected", reason="no_auth_token")
        return False
    given = (signature or "").strip()
    if not given:
        _obs.warn("sms.webhook_rejected", reason="no_signature")
        return False
    given_b = given.encode("utf-8", errors="replace")
    tried = 0
    for url in urls:
        if not url:
            continue
        tried += 1
        expected = twilio_signature(token, url, params).encode("ascii")
        if hmac.compare_digest(expected, given_b):
            return True
    _obs.warn("sms.webhook_rejected", reason="bad_signature" if tried else "no_url", urls_tried=tried)
    return False


# ── The ledger ───────────────────────────────────────────────────────────────
# One JSON file per caller, named by phone_hash, holding the newest _MAX_ITEMS
# texts in both directions. The webhook process writes inbound texts here and
# the call process polls it (agent._sms_monitor_task), which is how a text
# sent mid-call reaches the transcript across the LiveKit process boundary.

_DEFAULT_LEDGER_DIR = "/tmp/rt_sms"  # noqa: S108 - created 0700, files 0600; RT_SMS_LEDGER_DIR overrides
_DEFAULT_RETENTION_DAYS = 30
_MAX_ITEMS = 30
_SWEEP_EVERY_S = 3600.0
_LOCK_NAME = ".lock"

_HISTORY: dict[str, list[dict]] = {}   # per-hash mirror, read only when the file is unwritable
_SMS_LOCK = threading.Lock()
_LAST_SWEEP = 0.0


def _ledger_dir() -> str:
    d = (os.getenv("RT_SMS_LEDGER_DIR") or "").strip() or _DEFAULT_LEDGER_DIR
    os.makedirs(d, mode=0o700, exist_ok=True)
    # makedirs honours the umask and does nothing to a directory that already
    # exists, so the mode is set explicitly: the files inside are message
    # bodies and must not be readable by every process on the box.
    with contextlib.suppress(OSError):
        os.chmod(d, 0o700)
    return d


def _retention_days() -> int:
    raw = (os.getenv("RT_SMS_LEDGER_RETENTION_DAYS") or "").strip()
    try:
        days = int(raw) if raw else _DEFAULT_RETENTION_DAYS
    except ValueError:
        days = _DEFAULT_RETENTION_DAYS
        print(f"[rt-sms] RT_SMS_LEDGER_RETENTION_DAYS={raw!r} is not a number; using {days}", flush=True)
    # Same floor as rt_purge_old_transcripts: a misconfiguration cannot wipe
    # every thread in one sweep.
    return max(1, days)


def _ledger_key(phone_e164: str | None) -> str | None:
    """phone_hash of the number, or None — and None means nothing is stored.

    A lane that requires a pepper and has none raises inside phone_hash; the
    ledger then fails closed instead of falling back to a filename anyone
    with the number could derive.
    """
    if not phone_e164:
        return None
    try:
        import rt_prefs
        return rt_prefs.phone_hash(phone_e164)
    except Exception as exc:
        _obs.caught("rt_sms._ledger_key", exc)
        return None


def _ledger_path(key: str) -> str:
    return os.path.join(_ledger_dir(), f"{key}.json")


@contextlib.contextmanager
def _ledger_lock():
    """One writer at a time across processes.

    The webhook process and a call job both append to the same caller's file
    (an inbound text and a send_sms from the call). Two read-modify-writes
    that interleave drop one of them; the atomic rename in _write_items only
    guarantees a reader never sees half a file, not that both writes survive.
    """
    with _SMS_LOCK:
        fd = os.open(os.path.join(_ledger_dir(), _LOCK_NAME), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _read_items(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        _obs.caught("rt_sms._read_items", exc)
        return []
    return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []


def _prune(items: list[dict], now: float | None = None) -> list[dict]:
    cutoff = (now if now is not None else time.time()) - _retention_days() * 86400.0
    kept = []
    for it in items:
        ts = it.get("ts")
        # A non-numeric timestamp is a corrupt row; it ages out like an old one.
        if isinstance(ts, (int, float)) and float(ts) >= cutoff:
            kept.append(it)
    return kept[-_MAX_ITEMS:]


def _write_items(path: str, items: list[dict]) -> None:
    if not items:
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)
        return
    tmp = f"{path}.tmp.{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(items, f)
    os.replace(tmp, path)


def record_sms(phone_e164: str, direction: str, text: str,
               media_urls: str | list[str] | None = None, sid: str | None = None) -> None:
    """Append one text to the caller's ledger. `sid` is Twilio's message id,
    kept so a retried webhook delivery can be recognised (seen_sid)."""
    key = _ledger_key(phone_e164)
    if not key:
        return
    m_list: list[str] = []
    if isinstance(media_urls, (list, tuple)):
        m_list = [str(u) for u in media_urls if u]
    elif media_urls and str(media_urls).strip():
        m_list = [str(media_urls).strip()]
    item = {
        "direction": direction,
        "text": text or "",
        "media": m_list,
        "ts": time.time(),
        "sid": (sid or "").strip() or None,
    }
    try:
        with _ledger_lock():
            path = _ledger_path(key)
            items = _prune(_read_items(path))
            items.append(item)
            items = items[-_MAX_ITEMS:]
            _write_items(path, items)
            _HISTORY[key] = list(items)
    except Exception as exc:
        _obs.caught("rt_sms.record_sms", exc)
        with _SMS_LOCK:
            hist = _HISTORY.setdefault(key, [])
            hist.append(item)
            del hist[:-_MAX_ITEMS]
    _maybe_sweep()


def get_recent_sms(phone_e164: str, limit: int = 5) -> list[dict]:
    """The newest `limit` texts for this caller, oldest first."""
    key = _ledger_key(phone_e164)
    if not key or limit <= 0:
        return []
    try:
        with _SMS_LOCK:
            path = _ledger_path(key)
            # The file is the truth whenever it exists — even pruned to nothing.
            # The mirror is only for a ledger directory that could not be written.
            if os.path.exists(path):
                return _prune(_read_items(path))[-limit:]
            return _prune(list(_HISTORY.get(key, [])))[-limit:]
    except Exception as exc:
        _obs.caught("rt_sms.get_recent_sms", exc)
        with _SMS_LOCK:
            return _prune(list(_HISTORY.get(key, [])))[-limit:]


def seen_sid(phone_e164: str, sid: str | None) -> bool:
    """True if a text with this Twilio message id is already in the ledger."""
    want = (sid or "").strip()
    if not want:
        return False
    return any(it.get("sid") == want for it in get_recent_sms(phone_e164, limit=_MAX_ITEMS))


def forget(phone_e164: str) -> bool:
    """Drop the caller's ledger. Part of forget_me: the wipe RPC cannot reach a
    file on the worker's disk, and until 2026-09-02 nothing else removed it."""
    key = _ledger_key(phone_e164)
    if not key:
        return False
    try:
        with _ledger_lock():
            _HISTORY.pop(key, None)
            path = _ledger_path(key)
            existed = os.path.exists(path)
            with contextlib.suppress(FileNotFoundError):
                os.remove(path)
        return existed
    except Exception as exc:
        _obs.caught("rt_sms.forget", exc)
        return False


def purge_expired(now: float | None = None) -> dict[str, int]:
    """Apply the retention window to every ledger file. Returns counts."""
    now = now if now is not None else time.time()
    out = {"files": 0, "removed": 0, "dropped_items": 0}
    try:
        with _ledger_lock():
            d = _ledger_dir()
            for name in os.listdir(d):
                path = os.path.join(d, name)
                if ".tmp." in name:
                    # A writer that died between the temp write and the rename.
                    with contextlib.suppress(OSError):
                        if now - os.path.getmtime(path) > _SWEEP_EVERY_S:
                            os.remove(path)
                    continue
                if not name.endswith(".json"):
                    continue
                out["files"] += 1
                before = _read_items(path)
                after = _prune(before, now)
                if len(after) != len(before):
                    out["dropped_items"] += len(before) - len(after)
                    _write_items(path, after)
                    if not after:
                        out["removed"] += 1
                        _HISTORY.pop(name[:-5], None)
    except Exception as exc:
        _obs.caught("rt_sms.purge_expired", exc)
    return out


def _maybe_sweep() -> None:
    """The scheduler's hourly housekeeping runs in another container with its
    own /tmp, so the sweep rides on ledger writes, at most once an hour."""
    global _LAST_SWEEP
    now = time.monotonic()
    if now - _LAST_SWEEP < _SWEEP_EVERY_S:
        return
    _LAST_SWEEP = now
    purge_expired()


# ── Sending ──────────────────────────────────────────────────────────────────

def send_sms(to_e164: str, body: str, from_number: str | None = None,
             media_url: str | list[str] | None = None) -> dict:
    """Send an SMS, or an MMS when media_url is given, through Twilio.

    to_e164: recipient in E.164 (+1XXXXXXXXXX)
    body: message text, capped at 1600 chars (ten segments)
    from_number: sender override; otherwise TWILIO_FROM_NUMBER, then RT_PUBLIC_NUMBER
    media_url: one URL or a list, each becomes a MediaUrl field

    Returns {"error": bool, "sid": str | None, "message": str}. Every refusal
    happens before the socket, and no failure raises — a raise here would end
    a live call.
    """
    account_sid = (os.getenv("TWILIO_ACCOUNT_SID") or "").strip()
    token = (os.getenv("TWILIO_AUTH_TOKEN") or "").strip()
    if not account_sid or not token:
        return {"error": True, "sid": None,
                "message": "TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN not configured."}
    if not to_e164 or not str(to_e164).startswith("+"):
        return {"error": True, "sid": None, "message": f"Invalid recipient: {to_e164!r}"}

    body = (body or "").strip()
    if not body and not media_url:
        return {"error": True, "sid": None, "message": "Empty message body."}
    if len(body) > 1600:
        body = body[:1597] + "..."

    # No built-in default sender: the old fallback was the Telnyx toll-free
    # number, which the Twilio account does not own, so the send failed at
    # the carrier after the model had already promised the text.
    sender = ((from_number or "").strip()
              or (os.getenv("TWILIO_FROM_NUMBER") or "").strip()
              or (os.getenv("RT_PUBLIC_NUMBER") or "").strip())
    if not sender or not sender.startswith("+"):
        return {"error": True, "sid": None,
                "message": "No sender number: set TWILIO_FROM_NUMBER (E.164)."}

    params: list[tuple[str, str]] = [("To", to_e164), ("From", sender)]
    if body:
        params.append(("Body", body))
    media_list: list[str] = []
    if isinstance(media_url, (list, tuple)):
        media_list = [str(m).strip() for m in media_url if m and str(m).strip()]
    elif media_url and str(media_url).strip():
        media_list = [str(media_url).strip()]
    for m in media_list:
        params.append(("MediaUrl", m))

    url = _TWILIO_API.format(sid=account_sid)
    auth = base64.b64encode(f"{account_sid}:{token}".encode("utf-8")).decode("ascii")
    data = urllib.parse.urlencode(params).encode("utf-8")
    _t0 = time.perf_counter()
    try:
        req = urllib.request.Request(  # noqa: S310 - fixed https endpoint
            url,
            data=data,
            headers={
                "Authorization": f"Basic {auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=12) as resp:  # noqa: S310 - fixed https endpoint
            _raw = resp.read()
            _ms = round((time.perf_counter() - _t0) * 1000, 1)
            with contextlib.suppress(Exception):
                _obs.event("net.request", method="POST", ms=_ms,
                           status=getattr(resp, "status", None), bytes=len(_raw),
                           **_net_where(url))
            res = json.loads(_raw.decode("utf-8"))
            msg_id = str(res.get("sid") or "")
            status = str(res.get("status") or "queued")
            segments = max(1, (len(body) + 152) // 153) if len(body) > 160 else 1
            with contextlib.suppress(Exception):
                _obs.event("sms.dispatch_detail", segments=segments, ms=_ms, sid=msg_id)
            print(f"[rt-sms] sent to {_mask(to_e164)}: id={msg_id} status={status}", flush=True)
            record_sms(to_e164, "outbound", body, media_list or None, sid=msg_id)
            return {"error": False, "sid": msg_id, "message": status}
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="ignore")
        with contextlib.suppress(Exception):
            _ms = round((time.perf_counter() - _t0) * 1000, 1)
            _where = _net_where(url)
            _code = getattr(e, "code", None)
            _obs.event("net.request", method="POST", ms=_ms,
                       status=_code, bytes=len(err_body), **_where)
            _obs.warn("net.failed", method="POST", ms=_ms,
                      status=_code, err=f"HTTPError {_code}", **_where)
        print(f"[rt-sms] HTTP {e.code} sending to {_mask(to_e164)}: {err_body[:300]}", flush=True)
        return {"error": True, "sid": None, "message": f"HTTP {e.code}: {err_body[:200]}"}
    except Exception as e:
        with contextlib.suppress(Exception):
            _obs.warn("net.failed", method="POST",
                      ms=round((time.perf_counter() - _t0) * 1000, 1),
                      err=type(e).__name__, **_net_where(url))
        print(f"[rt-sms] send failed to {_mask(to_e164)}: {e}", flush=True)
        return {"error": True, "sid": None, "message": str(e)}
