"""rt_carrier.py — the Twilio account as the backstop behind every unattended dial.

Telnyx enforced a daily spend cap on its own side: when the outbound voice
profile reached $25 the carrier refused the call, and the scheduler's own
checks were written knowing that backstop was there ("the carrier should be
the backstop, not the only stop"). Twilio has no such switch — a usage trigger
can only call a webhook — so when the carrier changed (2026-09-02, ADR 0006)
the cap moved here. Before any dial the worker asks Twilio what today has
cost so far and refuses when the answer is at or over TWILIO_DAILY_SPEND_USD,
or when there is no answer at all. Fail closed, like the bridge ledger: an
unreadable bill looks exactly like an empty one.

The usage trigger twilio-setup.sh creates still exists. It is the alarm bell
(/twilio/usage in rt_health.py), not the brake.
"""
from __future__ import annotations

import rt_obs

import base64
import contextlib
import json
import os
import threading
import time
import urllib.parse
import urllib.request

_obs = rt_obs.get("rt_carrier")

_DEFAULT_CAP_USD = 25.0
_CACHE_TTL_S = 60.0
_USAGE_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Usage/Records/Today.json?Category=totalprice"

_LOCK = threading.Lock()
_CACHE: tuple[float, float] | None = None   # (monotonic fetched_at, spend_usd)


def cap_usd() -> float:
    raw = (os.getenv("TWILIO_DAILY_SPEND_USD") or "").strip()
    if not raw:
        return _DEFAULT_CAP_USD
    try:
        return max(0.0, float(raw))
    except ValueError:
        print(f"[rt-carrier] TWILIO_DAILY_SPEND_USD={raw!r} is not a number; using {_DEFAULT_CAP_USD}", flush=True)
        return _DEFAULT_CAP_USD


def _credentials() -> tuple[str, str] | None:
    sid = (os.getenv("TWILIO_ACCOUNT_SID") or "").strip()
    token = (os.getenv("TWILIO_AUTH_TOKEN") or "").strip()
    return (sid, token) if sid and token else None


def _fetch_spend(sid: str, token: str) -> float:
    """One GET of today's totalprice record. Raises on anything but a number."""
    url = _USAGE_URL.format(sid=sid)
    auth = base64.b64encode(f"{sid}:{token}".encode("utf-8")).decode("ascii")
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})  # noqa: S310 - fixed https endpoint
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=8) as resp:  # noqa: S310 - fixed https endpoint
        raw = resp.read()
    parts = urllib.parse.urlsplit(url)
    with contextlib.suppress(Exception):
        _obs.event("net.request", method="GET", ms=round((time.perf_counter() - t0) * 1000, 1),
                   status=200, bytes=len(raw), host=parts.hostname or "", path=parts.path)
    data = json.loads(raw.decode("utf-8"))
    records = data.get("usage_records") or []
    if not records:
        # A brand-new day with no usage still returns one record with price 0;
        # an empty list is an answer about a different question.
        raise ValueError("no totalprice record in today's usage")
    return float(records[0].get("price") or 0.0)


def spend_today_usd(fresh: bool = False) -> float | None:
    """Today's account spend in USD, cached for a minute. None means unknown."""
    global _CACHE
    creds = _credentials()
    if creds is None:
        return None
    with _LOCK:
        if not fresh and _CACHE is not None and time.monotonic() - _CACHE[0] < _CACHE_TTL_S:
            return _CACHE[1]
    try:
        spend = _fetch_spend(*creds)
    except Exception as exc:
        _obs.caught("rt_carrier.spend_today_usd", exc)
        return None
    with _LOCK:
        _CACHE = (time.monotonic(), spend)
    return spend


def outbound_allowed(fresh: bool = False) -> tuple[bool, str, dict]:
    """(allowed, reason, detail) for one unattended dial, decided now.

    Reasons: no_twilio_credentials, usage_unreadable, daily_cap_spent,
    under_cap. Every decision leaves one guard.decision line so a day that
    went quiet because the cap tripped is visible, not mysterious.
    """
    cap = cap_usd()
    if _credentials() is None:
        allowed, reason, spend = False, "no_twilio_credentials", None
    else:
        spend = spend_today_usd(fresh=fresh)
        if spend is None:
            allowed, reason = False, "usage_unreadable"
        elif spend >= cap:
            allowed, reason = False, "daily_cap_spent"
        else:
            allowed, reason = True, "under_cap"
    detail = {"spend_usd": spend, "cap_usd": cap}
    with contextlib.suppress(Exception):
        _obs.event("guard.decision", guard="carrier_cap", allowed=allowed, reason=reason, **detail)
    return allowed, reason, detail


def on_usage_trigger() -> dict:
    """The alarm rang (/twilio/usage). The body is not trusted; the account is
    asked again, and the answer is logged loudly either way."""
    spend = spend_today_usd(fresh=True)
    cap = cap_usd()
    tripped = spend is not None and spend >= cap
    out = {"spend_usd": spend, "cap_usd": cap, "tripped": tripped}
    with contextlib.suppress(Exception):
        _obs.warn("budget.carrier_cap", source="usage_trigger", **out)
    print(f"[rt-carrier] usage trigger: spend_today={spend} cap={cap} tripped={tripped}", flush=True)
    return out
