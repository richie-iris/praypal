"""rt_costs.py — what a call actually costs, so COGS isn't a guess.

Every rate lives in ONE table below and is overridable by environment variable,
because published prices move: the model rates were read off the web in
August 2026, the Twilio Elastic SIP Trunking rates (US pay-as-you-go, per
minute) on 2026-09-02 when Twilio replaced Telnyx as the carrier (ADR 0006).
Treat the output as an estimate with its assumptions shown, never as a bill.

A call costs money in four places:
  · the live model — audio in and out, billed per token
  · the small model — postcall extraction, canvas compile, lookups, research
  · speech synthesis — the greeting, announcements, warnings, check-ins
  · the phone line — LiveKit agent minutes plus Twilio carrier minutes,
    doubled while a second party is bridged on
"""
from __future__ import annotations

import rt_obs

import contextlib
import os

RATES: dict[str, float] = {
    "COST_LIVE_IN_PER_M": 3.00,
    "COST_LIVE_IN_TEXT_PER_M": 0.75,
    "COST_LIVE_OUT_PER_M": 12.00,
    "COST_LIVE_OUT_TEXT_PER_M": 4.50,
    "COST_FLASH_IN_PER_M": 0.30,
    "COST_FLASH_OUT_PER_M": 2.50,
    "COST_TTS_PER_M": 6.00,
    "COST_LIVEKIT_AGENT_MIN": 0.010,
    "COST_TWILIO_INBOUND_TOLLFREE_MIN": 0.0130,
    "COST_TWILIO_INBOUND_LOCAL_MIN": 0.0034,
    "COST_TWILIO_OUTBOUND_MIN": 0.0011,
}

_FLASH_CALL_IN, _FLASH_CALL_OUT = 1800, 350
_TTS_TOKENS_PER_SEC = 25
_CLIP_SECONDS = 4


def rate(name: str) -> float:
    raw = os.getenv(name)
    if raw:
        try:
            return float(raw)
        except ValueError as _exc:
            rt_obs.obs.caught("rt_costs.rate", _exc)
            pass
    return RATES[name]


def estimate(call: dict, events: list[dict] | None = None) -> dict:
    """Cost breakdown for one traced call. Returns dollars and its own workings."""
    events = events or []
    secs = float(call.get("duration_sec") or 0)
    mins = secs / 60.0

    live_in = int(call.get("in_tokens") or 0)
    live_out = int(call.get("out_tokens") or 0)

    tk = ((call.get("meta") or {}).get("tokens") or {})
    in_audio, in_text = int(tk.get("in_audio") or 0), int(tk.get("in_text") or 0)
    out_audio, out_text = int(tk.get("out_audio") or 0), int(tk.get("out_text") or 0)
    if in_audio or in_text:
        in_other = max(live_in - in_audio - in_text, 0)
        out_other = max(live_out - out_audio - out_text, 0)
        live = ((in_audio + in_other) / 1e6) * rate("COST_LIVE_IN_PER_M") + \
               (in_text / 1e6) * rate("COST_LIVE_IN_TEXT_PER_M") + \
               ((out_audio + out_other) / 1e6) * rate("COST_LIVE_OUT_PER_M") + \
               (out_text / 1e6) * rate("COST_LIVE_OUT_TEXT_PER_M")
    else:
        live = (live_in / 1e6) * rate("COST_LIVE_IN_PER_M") + \
               (live_out / 1e6) * rate("COST_LIVE_OUT_PER_M")

    tool_calls = sum(1 for e in events
                     if (e.get("name") or "") in ("web_search", "find_number"))
    flash_calls = tool_calls + 2
    flash = flash_calls * ((_FLASH_CALL_IN / 1e6) * rate("COST_FLASH_IN_PER_M") +
                           (_FLASH_CALL_OUT / 1e6) * rate("COST_FLASH_OUT_PER_M"))

    clips = 1 + (2 if call.get("bridge_number") else 0)
    tts = clips * (_CLIP_SECONDS * _TTS_TOKENS_PER_SEC / 1e6) * rate("COST_TTS_PER_M")

    _self_hosted = (os.getenv("LIVEKIT_SELF_HOSTED", "") or "").strip().lower() \
        not in ("", "0", "false", "no", "off")
    livekit = 0.0 if _self_hosted else mins * rate("COST_LIVEKIT_AGENT_MIN")
    _TOLLFREE_NPA = {"800", "833", "844", "855", "866", "877", "888"}
    _num = (os.getenv("RT_INBOUND_NUMBER") or os.getenv("RT_PUBLIC_NUMBER") or "").strip()
    _digits = "".join(c for c in _num if c.isdigit())
    _tollfree = len(_digits) >= 11 and _digits[1:4] in _TOLLFREE_NPA
    _in_rate = rate("COST_TWILIO_INBOUND_TOLLFREE_MIN") if _tollfree \
        else rate("COST_TWILIO_INBOUND_LOCAL_MIN")
    telco_in = mins * _in_rate
    telco_out = mins * rate("COST_TWILIO_OUTBOUND_MIN") if call.get("bridge_number") else 0.0
    telephony = livekit + telco_in + telco_out

    total = live + flash + tts + telephony

    with contextlib.suppress(Exception):
        rt_obs.obs.event(
            "cost.call",
            call_id=call.get("call_id") or call.get("room"),
            usd_total=round(total, 4),
            model_usd=round(live + flash + tts, 4),
            sip_usd=round(telephony, 4),
            minutes=round(mins, 2),
            bridged=bool(call.get("bridge_number")),
        )

    return {
        "total": round(total, 4),
        "per_minute": round(total / mins, 4) if mins > 0.05 else None,
        "breakdown": {
            "live model": round(live, 4),
            "small model": round(flash, 4),
            "speech clips": round(tts, 4),
            "LiveKit agent minutes": round(livekit, 4),
            "Twilio inbound SIP": round(telco_in, 4),
            "Twilio outbound SIP": round(telco_out, 4),
        },
        "workings": {
            "vendors": "live=Gemini Live, small=Gemini 2.5 Flash, clips=Gemini TTS",
            "live tokens in/out": f"{live_in:,} / {live_out:,}",
            "small-model calls": flash_calls,
            "speech clips": clips,
            "duration minutes": round(mins, 2),
            "bridged": bool(call.get("bridge_number")),
            "twilio_inbound_rate": "$%.5f/min (%s%s)" % (
                _in_rate, "toll-free" if _tollfree else "local DID",
                " " + _num if _num else " — number unknown, assumed local"),
            "twilio_outbound_rate": f"${rate('COST_TWILIO_OUTBOUND_MIN'):.4f}/min",
            "livekit_agent_rate": ("self-hosted — no per-minute charge; the server "
                                   "is part of the host's flat monthly cost"
                                   if _self_hosted else
                                   f"${rate('COST_LIVEKIT_AGENT_MIN'):.3f}/min"),
        },
    }


def summarize(calls: list[dict], events: list[dict] | None = None) -> dict:
    """Totals across calls, plus what a caller costs per month at this rate."""
    events = events or []
    by_call: dict[str, list] = {}
    for e in events:
        by_call.setdefault(e.get("call_id"), []).append(e)

    real_calls = [c for c in calls if c.get("phone_hash") != "probe" and not str(c.get("room") or "").startswith("probe-")]
    calls_to_process = real_calls if real_calls else calls

    total = 0.0
    mins = 0.0
    per_call = []
    for c in calls_to_process:
        est = estimate(c, by_call.get(c.get("call_id"), []))
        per_call.append(est["total"])
        total += est["total"]
        mins += float(c.get("duration_sec") or 0) / 60.0

    n = len(calls_to_process) or 1

    avg = total / n

    _cap = None
    with contextlib.suppress(Exception):
        _raw_cap = (os.getenv("RT_DAILY_MINUTES_CAP") or "").strip()
        _cap = float(_raw_cap) if _raw_cap else None
    with contextlib.suppress(Exception):
        rt_obs.obs.event("budget.spent", minutes_today=round(mins, 2), cap=_cap,
                         calls=len(calls_to_process), usd=round(total, 2))

    return {
        "calls": len(calls),
        "total": round(total, 2),
        "avg_per_call": round(avg, 4),
        "avg_minutes": round(mins / n, 2),
        "per_minute": round(total / mins, 4) if mins > 0.05 else None,
        "monthly_per_daily_caller": round(avg * 30, 2),
    }
