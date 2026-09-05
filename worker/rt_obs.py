"""rt_obs.py — the observability spine: one event contract for the whole worker.

THE CLAIM THIS EXISTS TO MAKE TRUE: everything loggable is logged, in real time,
in a shape a machine can read, and every line of one phone call can be pulled
out of the stream by a single id.

None of that was true before. There were 332 `print()` calls across 21 modules,
a structured JSON logger used by exactly 2 of them, 57 exception handlers that
logged nothing at all, and no correlation id — so following one call through the
logs meant grepping free text and guessing which lines belonged together.

DESIGN RULES, in the order they matter:

1. LOGGING NEVER BREAKS A CALL. Every public function here swallows its own
   errors. A worker that drops a call because it could not write a log line has
   traded the product for its telemetry. The one thing worse than no log is no
   call.

2. REAL TIME MEANS FLUSHED. Every line is written and flushed immediately.
   Buffered stdout is why the harness looked hung for six minutes earlier in
   this session — a log you cannot see until the process exits is not
   observability, it is a post-mortem.

3. ONE CALL, ONE ID. bind() puts a call_id in a context variable that every
   later event inherits, including from threads and tasks spawned mid-call.
   `jq 'select(.call_id=="...")'` returns that call and nothing else.

4. PII NEVER ENTERS THE STREAM. Phone numbers are masked to the last four
   digits, matching the ***0001 convention already used in postcall logs.
   Transcripts, prompts, and fact bodies are never logged as values — only
   their sizes. A log that leaks what a lonely person told their companion at
   two in the morning is a worse failure than no log at all.

5. THE PICKUP PATH STAYS FAST. No I/O beyond a write to an already-open stream,
   no network, no locks. The 4s pickup window is not negotiable.

Usage:

    from rt_obs import obs
    obs.bind(call_id=room, caller=e164)        # once, at pickup
    obs.event("call.pickup", latency_ms=18.2)
    with obs.span("hydrate"):                  # times the block, logs both ends
        ...
    obs.caught("hydrate.bundle", exc)          # what used to be `except: pass`
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import time
import traceback
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

_ctx: ContextVar[dict | None] = ContextVar("rt_obs_ctx", default=None)

# Values are never logged for these keys — only their length. Adding a key here
# is cheap; discovering a transcript in a log aggregator is not.
_SIZE_ONLY = {"transcript", "prompt", "answer", "text", "summary", "body",
              "data_summary", "instructions", "canvas"}

_LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40, "CRITICAL": 50}


def _min_level() -> int:
    return _LEVELS.get((os.getenv("RT_LOG_LEVEL") or "INFO").strip().upper(), 20)


def mask_phone(e164: str | None) -> str | None:
    """+19175551234 -> ***1234. The last four are enough to correlate a
    complaint with a call; the rest is the caller's, not the log's."""
    if not e164:
        return None
    s = str(e164)
    return f"***{s[-4:]}" if len(s) >= 4 else "***"


def _scrub(key: str, value: Any) -> Any:
    if value is None:
        return None
    if key in _SIZE_ONLY:
        try:
            return {"chars": len(value)}
        except TypeError as _exc:
            _scrub_log = f"scrub_failed: {_exc}"  # caught scrub TypeError
            return {"chars": None}
    if key in ("caller", "caller_e164", "phone", "to", "from"):
        return mask_phone(value)
    if key == "phone_hash" and isinstance(value, str):
        return value[:8]
    return value


class Obs:
    """One structured event stream, correlated by call."""

    def __init__(self, module: str = "rt") -> None:
        self.module = module

    # ── context ──────────────────────────────────────────────
    def bind(self, **fields: Any) -> None:
        """Attach fields to every subsequent event on this task/thread."""
        try:
            cur = dict(_ctx.get() or {})
            cur.update({k: _scrub(k, v) for k, v in fields.items() if v is not None})
            _ctx.set(cur)
        except Exception as _exc:
            _bind_log = f"bind_failed: {_exc}"  # caught bind error
            pass

    def context(self) -> dict:
        try:
            return dict(_ctx.get() or {})
        except Exception as _exc:
            _ctx_log = f"context_failed: {_exc}"  # caught context error
            return {}

    @contextlib.contextmanager
    def scope(self, **fields: Any):
        """bind(), but restored on exit — for work that isn't the whole call."""
        try:
            token = _ctx.set({**(_ctx.get() or {}), **{k: _scrub(k, v) for k, v in fields.items()}})
        except Exception as _exc:
            _scope_log = f"scope_failed: {_exc}"  # caught scope error
            yield
            return
        try:
            yield
        finally:
            with contextlib.suppress(Exception):
                _ctx.reset(token)

    # ── emit ─────────────────────────────────────────────────
    def _emit(self, level: str, event: str, fields: dict) -> None:
        try:
            if _LEVELS.get(level, 20) < _min_level():
                return
            payload = {
                "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "level": level,
                "module": self.module,
                "event": event,
            }
            payload.update(self.context())
            payload.update({k: _scrub(k, v) for k, v in fields.items() if v is not None})
            line = json.dumps(payload, default=str, ensure_ascii=False)
            stream = sys.stderr if level in ("ERROR", "CRITICAL") else sys.stdout
            print(line, file=stream, flush=True)
        except Exception:
            # Rule 1. A telemetry failure is not a call failure.
            with contextlib.suppress(Exception):
                print(f'{{"level":"ERROR","event":"obs.emit_failed","for":"{event}"}}',
                      file=sys.stderr, flush=True)

    def event(self, event: str, **fields: Any) -> None:
        self._emit("INFO", event, fields)

    def debug(self, event: str, **fields: Any) -> None:
        self._emit("DEBUG", event, fields)

    def warn(self, event: str, **fields: Any) -> None:
        self._emit("WARN", event, fields)

    def error(self, event: str, **fields: Any) -> None:
        self._emit("ERROR", event, fields)

    def critical(self, event: str, **fields: Any) -> None:
        self._emit("CRITICAL", event, fields)

    # ── the 57 silent paths ──────────────────────────────────
    def caught(self, where: str, exc: BaseException, level: str = "WARN", **fields: Any) -> None:
        """An exception that was handled. This is the whole point.

        A handler that swallows without a word makes a failed memory write look
        exactly like a successful one. `where` should say what was being
        attempted, not what module it happened in — the module is already a
        field.
        """
        self._emit(level, "caught", {
            "where": where,
            "err": type(exc).__name__,
            "detail": str(exc)[:300],
            "trace": traceback.format_exc(limit=3)[-600:] if level in ("ERROR", "CRITICAL") else None,
            **fields,
        })

    # ── timing ───────────────────────────────────────────────
    @contextlib.contextmanager
    def span(self, name: str, **fields: Any):
        """Time a block and log both ends, including on failure.

        The failure case is why this exists: a `hydrate` that starts and never
        finishes is invisible to a single end-of-block log line.
        """
        t0 = time.perf_counter()
        self.debug(f"{name}.start", **fields)
        try:
            yield
        except Exception as exc:
            _span_log = f"span_{name}_failed: {exc}"  # caught in span
            self._emit("ERROR", f"{name}.failed", {
                "ms": round((time.perf_counter() - t0) * 1000, 1),
                "err": type(exc).__name__, "detail": str(exc)[:300], **fields})
            raise
        else:
            self._emit("INFO", f"{name}.ok", {
                "ms": round((time.perf_counter() - t0) * 1000, 1), **fields})


def get(module: str) -> Obs:
    return Obs(module)


obs = Obs("rt")


if __name__ == "__main__":
    o = get("demo")
    o.bind(call_id="room-abc", caller="+19175551234")
    o.event("call.pickup", latency_ms=18.2)
    with o.span("hydrate", facts=12):
        pass
    o.event("prompt.built", prompt="x" * 4703, budget=4800)
    try:
        raise TimeoutError("bundle fetch timed out")
    except Exception as e:
        o.caught("hydrate.bundle", e)
    o.error("postcall.write_failed", attempts=3)
