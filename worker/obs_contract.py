"""obs_contract.py — the observability contract, as data.

This file is the promise: every event listed here MUST be emitted by the running
worker, and every module listed here MUST be exercised by the harness. It is not
documentation of what we happen to log. It is the specification of what we owe,
and `scripts/check_observability.py` fails the build when reality falls short.

WHY A CONTRACT INSTEAD OF MORE LOGGING. Anyone can add log lines and claim
coverage. A claim like "100% of what is loggable is logged" is unfalsifiable
until someone writes down what "everything" means. This file writes it down, so
the number is checkable by the client rather than asserted by us. When the audit
asks "how do you know?", the answer is a command they can run themselves.

TWO STREAMS, ON PURPOSE:

  OPERATIONAL (rt_obs -> stdout, JSON, real time)
      Sizes, durations, counts, ids, outcomes. Never content. Safe to ship to
      any aggregator, dashboard, or on-call pager.

  COMPLIANCE (rt_trace -> rt.call_events, durable, access controlled)
      The full record: exact prompts, transcripts, tool arguments and results.
      Lives in the database behind the service role, never in a log stream.

The split is a privacy decision, not an accident. These callers tell a companion
things at two in the morning that they have told no one else. An audit trail
that satisfies a compliance officer must not also be the thing that leaks a
lonely person's medical history into a log aggregator that half the company can
read. Content goes where access can be revoked; telemetry goes where it can be
graphed.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Event:
    name: str
    why: str
    fields: tuple[str, ...] = ()
    stream: str = "operational"     # operational | compliance | both
    critical: bool = False          # a gap here fails the build, not just the report


def _e(name, why, fields=(), stream="operational", critical=False):
    return Event(name, why, tuple(fields), stream, critical)


# ─────────────────────────────────────────────────────────────
# CALL LIFECYCLE — how many, how long, how they ended
# ─────────────────────────────────────────────────────────────
LIFECYCLE = [
    _e("call.pickup", "the call was answered, and how fast",
       ("call_id", "caller", "latency_ms", "version", "call_number"), "both", True),
    _e("call.ready", "prompt built and session open — the caller can now speak",
       ("call_id", "ms_since_pickup"), critical=True),
    _e("call.ended", "duration, why it ended, how much was said",
       ("call_id", "duration_s", "reason", "turns", "agent_turns"), "both", True),
    _e("call.abandoned", "the caller hung up before she spoke",
       ("call_id", "ms_waited"), "both"),
]

# ─────────────────────────────────────────────────────────────
# TURN PERFORMANCE — the number the caller actually feels
# ─────────────────────────────────────────────────────────────
TURNS = [
    _e("turn.user", "the caller said something",
       ("call_id", "turn", "chars", "ms_since_agent_stopped"), "both"),
    _e("turn.agent", "she answered, and how long the silence was first",
       ("call_id", "turn", "chars", "ttfb_ms", "total_ms"), "both", True),
    _e("turn.latency", "user stopped -> first audio out. The felt responsiveness.",
       ("call_id", "turn", "ms"), critical=True),
    _e("turn.interrupted", "she was talked over",
       ("call_id", "turn", "at_ms")),
    _e("turn.interrupted_detail", "offset into agent utterance when interrupted and residual audio",
       ("call_id", "turn", "at_ms", "residual_ms")),
    _e("turn.silence", "a gap neither of them filled",
       ("call_id", "ms")),
    _e("turn.cadence", "words per minute and cadence metrics",
       ("call_id", "turn", "wpm", "hesitation_ms")),
    _e("audio.cue_latency", "earcon trigger to playback start latency",
       ("call_id", "name", "ms")),
]

# ─────────────────────────────────────────────────────────────
# PROMPT & CONTEXT — what she was given, and what got cut
# ─────────────────────────────────────────────────────────────
CONTEXT = [
    _e("prompt.hydrate_in", "what the hydrator was handed before it built anything",
       ("call_id", "facts", "schemas", "reminders", "source"), "both", True),
    _e("prompt.built", "final prompt size against budget, and what it cost",
       ("call_id", "chars", "budget", "ms", "facts_rendered", "facts_below_fold"),
       "both", True),
    _e("prompt.trimmed", "something was cut to fit — what, and how much",
       ("call_id", "what", "from_chars", "to_chars", "reason"), "both", True),
    _e("prompt.over_budget", "it did not fit even after trimming",
       ("call_id", "chars", "budget"), "both", True),
    _e("prompt.turn", "the prompt actually sent on THIS turn, every turn",
       ("call_id", "turn", "chars"), "compliance", True),
    _e("context.start", "context window size at the start of the call",
       ("call_id", "tokens"), critical=True),
    _e("context.turn", "context growth per turn",
       ("call_id", "turn", "tokens_in", "tokens_out", "cumulative")),
    _e("context.end", "context window size when the call ended",
       ("call_id", "tokens", "pct_of_window"), critical=True),
    _e("context.compacted", "the conversation was trimmed mid-call",
       ("call_id", "turn", "from_tokens", "to_tokens"), "both", True),
]

# ─────────────────────────────────────────────────────────────
# TOOLS — every one, arguments in and results out
# ─────────────────────────────────────────────────────────────
TOOLS = [
    _e("tool.call", "a tool was invoked, with what arguments",
       ("call_id", "turn", "name", "args"), "both", True),
    _e("tool.result", "what came back, and how long it took",
       ("call_id", "turn", "name", "ms", "ok", "result"), "both", True),
    _e("tool.failed", "it raised, and the model had to cope",
       ("call_id", "turn", "name", "ms", "err"), "both", True),
    _e("tool.withheld", "the capability gate refused to offer it",
       ("call_id", "name", "missing_credential"), "both", True),
    _e("search.cache_stats", "web search cache hit/miss and query stats",
       ("call_id", "hit", "query_chars", "results_chars")),
    _e("directory.lookup_detail", "phone directory lookup candidate details",
       ("call_id", "candidates", "confidence", "source")),
    _e("sms.dispatch_detail", "SMS segments, carrier SID and latency",
       ("call_id", "segments", "ms", "sid")),
    _e("sms.inbound_received", "a text reached the webhook: size, media, and whether Twilio was retrying",
       ("chars", "has_media", "sid", "duplicate")),
    _e("sms.webhook_rejected", "a POST to /sms/incoming did not carry a valid Twilio signature",
       ("reason", "urls_tried"), "both", True),
    _e("email.dispatch_detail", "Email message ID, attachment and latency",
       ("call_id", "ms", "message_id", "has_ics")),
    _e("scheduler.tz_resolved", "caller timezone resolution method",
       ("call_id", "tz", "method")),
]

# ─────────────────────────────────────────────────────────────
# MODEL, NETWORK, TRANSPORT
# ─────────────────────────────────────────────────────────────
MODEL = [
    _e("model.session_open", "which model, which voice, which settings",
       ("call_id", "model", "voice", "temperature", "language"), "both", True),
    _e("model.usage", "tokens in and out, per turn — this is the bill",
       ("call_id", "turn", "tokens_in", "tokens_out", "audio_s"), "both", True),
    _e("model.error", "the model failed or refused",
       ("call_id", "turn", "err", "detail"), "both", True),
    _e("model.reconnect", "the realtime session dropped and came back",
       ("call_id", "attempt", "ms_down"), "both", True),
    _e("model.ws_status", "websocket connection lifecycle and close codes",
       ("call_id", "action", "code", "latency_ms")),
    _e("model.chunk_latency", "audio streaming chunk latency and jitter",
       ("call_id", "turn", "chunk_ms", "jitter_ms")),
    _e("model.safety_filter", "Gemini safety filter / finish reason trigger",
       ("call_id", "turn", "reason")),
    _e("model.resample_stats", "audio rate conversion metrics",
       ("call_id", "from_rate", "to_rate", "samples")),
]
NETWORK = [
    _e("net.request", "an outbound HTTP call: where, how long, what status",
       ("call_id", "host", "path", "ms", "status", "bytes"), critical=True),
    _e("net.failed", "it timed out or refused",
       ("call_id", "host", "path", "ms", "err"), "both", True),
    _e("net.retry", "we tried again",
       ("call_id", "host", "attempt")),
    _e("net.rpc_latency", "PostgREST RPC endpoint latency and status",
       ("call_id", "rpc", "ms", "status")),
    _e("runtime.loop_lag", "asyncio event loop scheduling delay",
       ("lag_ms",)),
]
LIVEKIT = [
    _e("lk.room_joined", "the worker joined the room",
       ("call_id", "room", "ms")),
    _e("lk.participant", "someone joined or left",
       ("call_id", "identity", "action")),
    _e("lk.track", "an audio track was published or subscribed",
       ("call_id", "kind", "action")),
    _e("lk.sip", "SIP signalling: invite, answer, bye, and failures",
       ("call_id", "action", "status", "trunk"), "both", True),
    _e("lk.sip_status", "SIP signaling response and release cause codes",
       ("call_id", "status_code", "reason", "trunk")),
    _e("lk.codec_negotiated", "negotiated audio codec, sample rate and ptime",
       ("call_id", "codec", "sample_rate", "ptime")),
    _e("lk.audio_energy", "audio frame RMS and dead-air detection",
       ("call_id", "rms_db", "silent_ms")),
    _e("lk.dtmf_received", "DTMF digit received from caller",
       ("call_id", "digit", "code")),
    _e("lk.disconnect", "the transport dropped",
       ("call_id", "reason"), "both", True),
    _e("lk.audio_stats", "jitter, packet loss, bitrate — why she sounded bad",
       ("call_id", "jitter_ms", "packet_loss_pct", "bitrate_kbps")),
]

# ─────────────────────────────────────────────────────────────
# MEMORY — what was read, what was written, what was refused
# ─────────────────────────────────────────────────────────────
MEMORY = [
    _e("memory.bundle", "the caller bundle was fetched, and how fast",
       ("call_id", "ms", "facts", "schemas", "cached"), critical=True),
    _e("memory.saved", "something was written to the caller's record",
       ("call_id", "kind", "count"), "both", True),
    _e("memory.refused", "a guard rejected a write — WHY it was not saved",
       ("call_id", "kind", "value", "reason"), "both", True),
    _e("memory.forgotten", "a forget-me erased data — what and how much",
       ("call_id", "tables", "rows"), "both", True),
    _e("memory.fact_mutation", "fact insertions, updates, supersessions",
       ("call_id", "domain", "action", "confidence")),
    _e("memory.dualwrite_audit", "schema rows vs next_call_context consistency",
       ("call_id", "schema_count", "canvas_facts", "mismatch")),
    _e("memory.purge_audit", "count of purged records per table",
       ("call_id", "tables", "total_rows")),
]
POSTCALL = [
    _e("postcall.queued", "the job was enqueued",
       ("call_id", "job_id"), "both", True),
    _e("postcall.started", "the worker picked it up",
       ("call_id", "job_id", "attempt"), "both", True),
    _e("postcall.queue_lag", "delay between hangup and worker processing",
       ("call_id", "job_id", "lag_ms")),
    _e("postcall.extracted", "pass 1 finished — which keys came back",
       ("call_id", "ms", "keys", "ok"), "both", True),
    _e("postcall.facts", "the dual write result, in full",
       ("call_id", "insert", "confirm", "supersede", "failed", "skipped", "refused"),
       "both", True),
    _e("postcall.compiled", "pass 2 built next_call_context",
       ("call_id", "ms", "chars", "ok"), "both", True),
    _e("postcall.failed", "the whole job failed and will retry or retire",
       ("call_id", "job_id", "attempt", "err"), "both", True),
]
JOBS = [
    _e("job.scheduled", "future work was queued",
       ("call_id", "type", "run_at", "job_id"), "both", True),
    _e("job.executed", "it ran",
       ("job_id", "type", "ms", "ok"), "both", True),
    _e("job.failed", "it did not",
       ("job_id", "type", "attempt", "err"), "both", True),
    _e("job.reclaimed", "a stranded job was recovered",
       ("job_id", "stranded_for_s"), "both"),
]
SAFETY = [
    _e("guard.decision", "a guard allowed or refused something",
       ("call_id", "guard", "allowed", "reason"), "both", True),
    _e("scam.signal", "a scam signature matched",
       ("call_id", "signature", "confidence"), "both", True),
    _e("scam.risk_breakdown", "sub-scores for fraud signals",
       ("call_id", "urgency", "monetary", "impersonation", "score")),
    _e("pii.redaction", "count and category of scrubbed PII",
       ("call_id", "category", "count")),
    _e("budget.spent", "the caller's minute budget moved",
       ("call_id", "minutes_today", "cap")),
    _e("budget.carrier_cap", "Twilio's daily spend alarm rang; what the account said today has cost",
       ("spend_usd", "cap_usd", "tripped", "source"), "both", True),
    _e("cost.call", "what this call cost, itemised",
       ("call_id", "usd_total", "model_usd", "sip_usd", "minutes"), "both", True),
]

ALL_EVENTS: list[Event] = (LIFECYCLE + TURNS + CONTEXT + TOOLS + MODEL + NETWORK
                           + LIVEKIT + MEMORY + POSTCALL + JOBS + SAFETY)

GROUPS: dict[str, list[Event]] = {
    "lifecycle": LIFECYCLE, "turns": TURNS, "context": CONTEXT, "tools": TOOLS,
    "model": MODEL, "network": NETWORK, "livekit": LIVEKIT, "memory": MEMORY,
    "postcall": POSTCALL, "jobs": JOBS, "safety": SAFETY,
}

# Modules the harness must exercise. A module absent from the suite is a module
# whose behaviour nobody has ever asserted.
MUST_BE_TESTED: tuple[str, ...] = (
    "agent", "config", "rt_bridge", "rt_capabilities", "rt_carrier", "rt_costs", "rt_directory",
    "rt_email", "rt_executor", "rt_facts", "rt_http", "rt_hydrator", "rt_logger",
    "rt_obs", "rt_patterns", "rt_postcall_worker", "rt_prefs", "rt_recovery",
    "rt_scheduler", "rt_self", "rt_sms", "rt_sms_inbound", "rt_trace", "scamguard",
)


def event_names() -> set[str]:
    return {e.name for e in ALL_EVENTS}


def critical_events() -> list[Event]:
    return [e for e in ALL_EVENTS if e.critical]


def summary() -> str:
    lines = [f"{len(ALL_EVENTS)} events across {len(GROUPS)} groups; "
             f"{len(critical_events())} critical; "
             f"{len(MUST_BE_TESTED)} modules must be tested"]
    for g, evs in GROUPS.items():
        lines.append(f"  {g:<11} {len(evs):>3} events  "
                     f"({sum(1 for e in evs if e.critical)} critical)")
    return "\n".join(lines)


if __name__ == "__main__":
    print(summary())
