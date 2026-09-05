"""rt_logger.py — Structured JSON Logger for Phone-Pal (Iris).

Emits structured JSON log lines for observability, alerting, and log aggregation.
Supports Sentry error monitoring and external HTTP log shipping sinks.
"""
from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

# --- PII scrubbing (must be defined before sentry_sdk.init so before_send can reference it) ---
# Emails first so a "+1..." local-part is swallowed as [email] rather than half-eaten as [phone].
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Longest / most specific alternatives first so "+1 973 400 5897" is eaten whole rather
# than leaving "+1 " behind. Separators are space, dot or dash; a leading "1" is optional
# on the national forms. The bare run is the catch-all for "9734005897" / "19734005897"
# and longer plus-less international numbers; over-redacting an epoch or an id is the
# cheap side of that trade.
_SEP = r"[\s.\-]"
_PHONE_RE = re.compile(
    r"\+\d{10,15}"                                                     # E.164
    rf"|\+\d{{1,3}}{_SEP}?\(?\d{{3}}\)?{_SEP}?\d{{3}}{_SEP}?\d{{4}}(?!\d)"   # +1 973 400 5897 / +1 (973) 400-5897
    rf"|(?<!\d)(?:1{_SEP}?)?\(\d{{3}}\){_SEP}?\d{{3}}{_SEP}?\d{{4}}(?!\d)"   # (NNN) NNN-NNNN
    rf"|(?<!\d)(?:1{_SEP})?\d{{3}}{_SEP}\d{{3}}{_SEP}\d{{4}}(?!\d)"          # NNN-NNN-NNNN / NNN NNN NNNN / NNN.NNN.NNNN
    r"|(?<!\d)\d{10,15}(?!\d)"                                        # bare digit run
)
# Keys whose entire value is a caller transcript / message body: dropped wholesale at any depth.
_SENSITIVE_KEYS = frozenset({"transcript", "transcript_lines", "text", "body", "message_body"})
# Keys whose value IS a phone number: replaced wholesale so a shape the regex does not
# know (7-digit local, int, "ext 12") cannot ride through under a self-describing key.
_PHONE_KEYS = frozenset({"caller_phone", "phone", "phone_number", "caller_number",
                         "from_number", "to_number", "e164", "number", "bridge_number",
                         "caller_e164", "to_e164"})
# Keys whose numeric value is a clock reading, never a phone: exempt from the digit-count
# rule below so Sentry span timestamps (10-digit epoch floats) survive the scrub intact.
_TIME_KEYS = frozenset({"ts", "timestamp", "start_timestamp", "received", "uptime_seconds"})
_DIGITS_RE = re.compile(r"\d")
# Top-level Sentry event sections that are pure SDK bookkeeping (ids, versions, platform).
# Every OTHER section is scrubbed — an allowlist, so a section this list has never heard of
# (contexts, threads, stacktrace, tags, request, spans, ...) is scrubbed by default.
_STRUCTURAL_SECTIONS = frozenset({
    "event_id", "timestamp", "start_timestamp", "platform", "level", "sdk", "type",
    "release", "dist", "environment", "modules", "debug_meta", "transaction_info",
})


def scrub_text(s: str) -> str:
    """Redact phone numbers and email addresses from a string."""
    s = _EMAIL_RE.sub("[email]", s)
    return _PHONE_RE.sub("[phone]", s)


def _looks_like_phone_number(value: Any) -> bool:
    """A whole number with >= 10 digits: a phone stored as int (15551234567) rides through
    the string regex untouched, so the digit count is judged on the stringified value.
    Fractional floats are measurements / epoch clocks, never phones, and stay."""
    if isinstance(value, bool):
        return False
    if isinstance(value, float) and not value.is_integer():
        return False
    if isinstance(value, (int, float)):
        return len(_DIGITS_RE.findall(str(value))) >= 10
    return False


def _scrub_value(value: Any, key: Any = None) -> Any:
    """Recursively redact strings; drop transcript / phone-keyed values wholesale."""
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for k, v in value.items():
            if k in _SENSITIVE_KEYS:
                out[k] = "[redacted]"
            elif k in _PHONE_KEYS:
                out[k] = "[phone]" if v not in (None, "") else v
            else:
                out[k] = _scrub_value(v, k)
        return out
    if isinstance(value, (list, tuple)):
        return [_scrub_value(v) for v in value]
    if key not in _TIME_KEYS and _looks_like_phone_number(value):
        return "[phone]"
    return value


def _scrub_event(event: dict[str, Any], hint: Any) -> dict[str, Any] | None:
    """Sentry before_send hook. Returns None (drops the event) if scrubbing itself fails —
    an unscrubbed event must never leave the process."""
    try:
        for section in list(event):
            if section not in _STRUCTURAL_SECTIONS:
                event[section] = _scrub_value(event[section])
        return event
    except Exception:
        return None


# Optional Sentry integration
_SENTRY_INITIALIZED = False
_SENTRY_DSN = os.getenv("SENTRY_DSN", "").strip()
if _SENTRY_DSN:
    try:
        import sentry_sdk

        sentry_sdk.init(
            dsn=_SENTRY_DSN,
            environment=os.getenv("SENTRY_ENVIRONMENT", os.getenv("RT_ENV", "production")),
            traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
            # Caller PII (numbers, transcripts) must never reach Sentry.
            send_default_pii=False,
            include_local_variables=False,
            before_send=_scrub_event,
        )
        _SENTRY_INITIALIZED = True
    except Exception as exc:
        print(f"Failed to initialize Sentry: {exc}", file=sys.stderr)

# Log levels — INFO by default; DEBUG is opt-in because it echoes call-flow detail.
LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
CURRENT_LOG_LEVEL = LEVELS.get(os.getenv("LOG_LEVEL", "INFO").upper(), 20)

# Optional HTTP Log Shipper Background Worker
_LOG_SHIPPER_URL = os.getenv("LOG_SHIPPER_URL", "").strip()
_LOG_SHIPPER_KEY = os.getenv("LOG_SHIPPER_API_KEY", "").strip()
_log_queue: queue.Queue[dict[str, Any]] | None = None
_shipper_thread: threading.Thread | None = None


def _shipper_worker() -> None:
    """Background worker that drains log queue and posts to HTTP log sink."""
    global _log_queue
    while True:
        if _log_queue is None:
            break
        item = _log_queue.get()
        if item is None:
            break
        try:
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "phone-pal-logger/1.0",
            }
            if _LOG_SHIPPER_KEY:
                headers["Authorization"] = f"Bearer {_LOG_SHIPPER_KEY}"
            if not _LOG_SHIPPER_URL.lower().startswith(("https://", "http://")):
                break  # misconfigured sink: never open file:/other schemes from a log path
            req = urllib.request.Request(  # noqa: S310 - http(s) enforced above
                _LOG_SHIPPER_URL,
                # Already scrubbed on enqueue; scrubbed again here so nothing that reaches
                # the queue by another path leaves the box raw.
                data=json.dumps(_scrub_value(item)).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as _:  # noqa: S310 - http(s) enforced above
                pass
        except Exception as _exc:
            _err_msg = f"log_shipper_failed: {_exc}"  # caught log shipper error
            pass  # Avoid recursion or crashing if log shipper is down
        finally:
            _log_queue.task_done()


if _LOG_SHIPPER_URL:
    _log_queue = queue.Queue(maxsize=1000)
    _shipper_thread = threading.Thread(target=_shipper_worker, daemon=True)
    _shipper_thread.start()


class JsonLogger:
    """Lightweight structured JSON logger with context binding and error monitoring."""

    def __init__(self, module_name: str = "rt", context: dict[str, Any] | None = None):
        self.module_name = module_name
        self.context: dict[str, Any] = dict(context or {})

    def bind(self, **kwargs: Any) -> JsonLogger:
        """Create a child logger with bound contextual metadata."""
        new_ctx = {**self.context, **kwargs}
        return JsonLogger(self.module_name, context=new_ctx)

    def _log(
        self,
        level: str,
        msg: str,
        extra: dict[str, Any] | None = None,
        exc_info: Exception | None = None,
    ) -> None:
        lvl_val = LEVELS.get(level.upper(), 20)
        if lvl_val < CURRENT_LOG_LEVEL:
            return

        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "level": level.upper(),
            "module": self.module_name,
            "msg": msg,
        }

        # Merge bound context
        if self.context:
            payload.update(self.context)

        # Merge invocation extras
        if extra:
            payload.update({k: v for k, v in extra.items() if v is not None})

        # Add exception details if present
        if exc_info:
            payload["exception_type"] = type(exc_info).__name__
            payload["exception_msg"] = str(exc_info)

        # Scrub ONCE, before any sink: stdout, Sentry extras and the shipper queue all
        # read this same dict, so a bound caller_phone or a transcript extra never leaves
        # the process raw. A scrub that cannot run drops the record (fail-closed).
        try:
            payload = _scrub_value(payload)
        except Exception:
            return

        line = json.dumps(payload)
        if lvl_val >= 40:
            print(line, file=sys.stderr, flush=True)
        else:
            print(line, file=sys.stdout, flush=True)

        # Ship to Sentry if level is ERROR or CRITICAL
        if lvl_val >= 40 and _SENTRY_INITIALIZED:
            try:
                import sentry_sdk

                with sentry_sdk.push_scope() as scope:
                    scope.set_tag("module", self.module_name)
                    for k, v in payload.items():
                        if k not in ("ts", "level", "module", "msg"):
                            scope.set_extra(k, v)
                    if exc_info:
                        sentry_sdk.capture_exception(exc_info)
                    else:
                        sentry_sdk.capture_message(payload["msg"], level=level.lower())
            except Exception as _exc:
                _sentry_err = f"sentry_log_failed: {_exc}"  # caught sentry error
                pass

        # Send to HTTP log shipper queue if enabled
        if _log_queue is not None:
            try:
                _log_queue.put_nowait(payload)
            except queue.Full as _exc:
                _queue_err = f"log_queue_full: {_exc}"  # caught queue full error
                pass

    def info(self, msg: str, **kwargs: Any) -> None:
        self._log("INFO", msg, kwargs)

    def warn(self, msg: str, **kwargs: Any) -> None:
        self._log("WARN", msg, kwargs)

    def warning(self, msg: str, **kwargs: Any) -> None:
        self._log("WARN", msg, kwargs)

    def error(self, msg: str, exc_info: Exception | None = None, **kwargs: Any) -> None:
        self._log("ERROR", msg, kwargs, exc_info=exc_info)

    def exception(self, msg: str, exc_info: Exception | None = None, **kwargs: Any) -> None:
        if exc_info is None and sys.exc_info()[1] is not None:
            exc_info = sys.exc_info()[1]
        self._log("ERROR", msg, kwargs, exc_info=exc_info)

    def debug(self, msg: str, **kwargs: Any) -> None:
        self._log("DEBUG", msg, kwargs)


def get_logger(module_name: str = "rt") -> JsonLogger:
    """Get a JsonLogger instance for a module."""
    return JsonLogger(module_name)


if __name__ == "__main__":
    log = get_logger("test")
    log.info("Structured JSON logging initialized", call_id="test_123", duration_ms=14.2)
    bound_log = log.bind(caller_phone="+15551234567")
    bound_log.warn("Low credit warning", balance_remaining=0.50)
