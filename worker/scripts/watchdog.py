#!/usr/bin/env python3
"""watchdog.py — does the line still answer, and does the number still reach it?

The README has described this file for weeks and it did not exist, which is why
RT_ALERT_SMS_TO, RT_ALERT_SLACK_WEBHOOK and RT_WATCHDOG_STATE were declared in
.env.example and read by nothing. The suite cannot see this: it never dials, and
its own runbook says LINE DEAD outranks every test result. Nothing was watching.

Two questions, in the order that matters:

  1. Does a worker answer?  Dispatch a job into a `watchdog-` room. agent.py
     treats that prefix as a probe — it joins, proves it is alive, and leaves
     without opening a model session, so this costs no tokens and leaves no call
     record. Registration alone is not proof; a registered worker with a dead
     job runner still never picks up.
  2. Does the public number still route to that worker?  A live worker behind a
     deleted dispatch rule is a silent outage: everything looks healthy and the
     phone rings out. prod lost exactly this once.

Alerts fire on a CHANGE of state, never on every run — a watchdog that reports
"fine" every five minutes is one nobody reads. Recovery is announced too, so a
silent alert channel is distinguishable from a healthy line.

    python scripts/watchdog.py            # one pass, exit 0 healthy / 1 not
    python scripts/watchdog.py --verbose  # say what it found either way
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env.dev")
load_dotenv(ROOT / ".env.local")
load_dotenv(ROOT / ".env")

AGENT = os.getenv("AGENT_NAME", "phone-pal-dev")
NUMBER = os.getenv("RT_PUBLIC_NUMBER", "")
STATE_PATH = Path(os.getenv("RT_WATCHDOG_STATE", os.path.join(tempfile.gettempdir(), "iris-watchdog.json")))
ANSWER_TIMEOUT = float(os.getenv("RT_WATCHDOG_ANSWER_TIMEOUT", "20"))


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def _save_state(st: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(st, indent=1))
    except Exception as e:
        print(f"[watchdog] could not persist state ({e}) — will re-alert next run", flush=True)


async def _answers() -> tuple[bool, str]:
    """Dispatch a probe and wait for the worker to actually join the room."""
    from livekit import api as lkapi

    room = f"watchdog-{int(time.time())}"
    api = lkapi.LiveKitAPI()
    try:
        try:
            await api.agent_dispatch.create_dispatch(
                lkapi.CreateAgentDispatchRequest(agent_name=AGENT, room=room))
        except Exception as e:
            return False, f"dispatch refused: {str(e)[:120]}"

        deadline = time.time() + ANSWER_TIMEOUT
        last_err = ""
        while time.time() < deadline:
            await asyncio.sleep(1.0)
            try:
                res = await api.room.list_participants(
                    lkapi.ListParticipantsRequest(room=room))
                if res.participants:
                    return True, f"worker joined in {ANSWER_TIMEOUT - (deadline - time.time()):.0f}s"
            except Exception as e:
                last_err = str(e)[:80]  # transient while the room spins up; keep polling
                continue
        return False, f"no worker joined within {ANSWER_TIMEOUT:.0f}s" + (f" (last error: {last_err})" if last_err else "")
    finally:
        with_suppress = getattr(api, "aclose", None)
        with contextlib.suppress(Exception):  # probe room cleanup is best-effort
            await api.room.delete_room(lkapi.DeleteRoomRequest(room=room))
        if with_suppress:
            with contextlib.suppress(Exception):  # closing a dead client is fine
                await api.aclose()


async def _routes() -> tuple[bool, str]:
    """Is RT_PUBLIC_NUMBER still pointed at this agent by a dispatch rule?"""
    if not NUMBER:
        return True, "no RT_PUBLIC_NUMBER set — routing not checked"
    from livekit import api as lkapi

    api = lkapi.LiveKitAPI()
    try:
        try:
            rules = await api.sip.list_dispatch_rule(lkapi.ListSIPDispatchRuleRequest())
        except AttributeError:
            rules = await api.sip.list_sip_dispatch_rule(lkapi.ListSIPDispatchRuleRequest())
        items = getattr(rules, "items", None) or []
        for r in items:
            blob = str(r)
            if AGENT in blob and (not getattr(r, "inbound_numbers", None)
                                  or NUMBER in blob):
                return True, f"a dispatch rule still points {NUMBER} at {AGENT}"
        return False, f"NO dispatch rule sends {NUMBER} to {AGENT} ({len(items)} rule(s) exist)"
    except Exception as e:
        return True, f"routing not verifiable ({str(e)[:90]})"
    finally:
        with contextlib.suppress(Exception):  # closing a dead client is fine
            await api.aclose()


def _alert(subject: str, body: str) -> None:
    sent = []
    to = (os.getenv("RT_ALERT_SMS_TO") or "").strip()
    if to:
        try:
            import rt_sms
            r = rt_sms.send_sms(to, f"{subject} — {body}"[:300])
            sent.append("sms:" + ("ok" if not r.get("error") else "failed"))
        except Exception as e:
            sent.append(f"sms:failed({str(e)[:40]})")
    to_email = (os.getenv("RT_ALERT_EMAIL_TO") or "").strip()
    if to_email:
        try:
            import rt_email
            # Operator-configured alert address: it IS the verified recipient.
            r = rt_email.send_email(to_email, subject, body, verified_email=to_email)
            sent.append("email:" + ("ok" if not r.get("error") else "failed"))
        except Exception as e:
            sent.append(f"email:failed({str(e)[:40]})")
    hook = (os.getenv("RT_ALERT_SLACK_WEBHOOK") or "").strip()
    if hook.startswith("https://"):  # webhooks are https; anything else is a misconfig, not a sink
        try:
            import urllib.request
            req = urllib.request.Request(  # noqa: S310 - https enforced above
                hook, data=json.dumps({"text": f"*{subject}*\n{body}"}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=10)  # noqa: S310 - https enforced above
            sent.append("slack:ok")
        except Exception as e:
            sent.append(f"slack:failed({str(e)[:40]})")
    print(f"[watchdog] ALERT {subject} :: {body} :: {sent or 'no channel configured'}", flush=True)


def main() -> None:
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    answers, why_a = asyncio.run(_answers())
    routes, why_r = asyncio.run(_routes()) if answers else (False, "not checked — line is down")
    healthy = answers and routes
    _host = (os.getenv("LIVEKIT_URL") or "?").replace("wss://", "").replace("ws://", "")
    detail = f"agent={AGENT} livekit={_host} | answers: {why_a} | routing: {why_r}"

    prev = _load_state()
    was_healthy = prev.get("healthy")
    if was_healthy is None or bool(was_healthy) != healthy:
        if healthy:
            _alert(f"LINE RECOVERED — {NUMBER or AGENT}", detail)
        else:
            _alert(f"LINE DEAD — {NUMBER or AGENT}", detail)
    elif verbose:
        print(f"[watchdog] unchanged ({'healthy' if healthy else 'down'}) :: {detail}", flush=True)

    _save_state({"healthy": healthy, "at": int(time.time()), "detail": detail})
    if verbose or not healthy:
        print(f"[watchdog] {'HEALTHY' if healthy else 'DOWN'} :: {detail}", flush=True)
    sys.exit(0 if healthy else 1)


if __name__ == "__main__":
    main()
